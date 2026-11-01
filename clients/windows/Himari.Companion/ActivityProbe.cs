using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows;
using Microsoft.Win32;
using Windows.Media.Control;

namespace Himari.Companion;

// V3-P88: reading what the user is doing on this computer, and the "quiet" menu. The rules (classifying,
// the 60-second hold, what a body holds and when it goes) are in ActivitySignals.cs.
public partial class MainWindow
{
    private readonly ActivityTracker activityTracker = new();
    private readonly SignalSchedule signalSchedule = new();
    private SignalSettings signalSettings = new();
    private IReadOnlySet<string> titleExclusions = SignalSettings.Names(SignalSettings.DefaultTitleExclusions);
    private IReadOnlySet<string> micExclusions = SignalSettings.Names(SignalSettings.DefaultMicExclusions);
    private IReadOnlyDictionary<string, string> categoryOverrides = new Dictionary<string, string>();
    private bool serverTakesSignals;  // GET /api/device or hello said features.presence_signals = 1 (§14)
    private bool sessionLocked;
    private MediaInfo? latestMedia; private bool latestMediaKnown; private bool mediaReading;
    private DateTime lastMediaRead = DateTime.MinValue, lastMicRead = DateTime.MinValue;
    private bool? latestMicCamera;
    private GlobalSystemMediaTransportControlsSessionManager? mediaManager;
    private readonly Dictionary<string, string> programNames = new(StringComparer.OrdinalIgnoreCase);
    private DateTimeOffset? quietUntil;
    private bool signalControlsLoading;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
    [DllImport("shell32.dll")] private static extern int SHQueryUserNotificationState(out int state);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr OpenProcess(uint access, bool inherit, uint process);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder path, ref int size);
    private const uint ProcessQueryLimitedInformation = 0x1000;
    private const int RunningD3DFullscreen = 3;  // QUNS_RUNNING_D3D_FULL_SCREEN

    private void SetupActivitySignals()
    {
        SystemEvents.SessionSwitch += SessionSwitched;
        SystemEvents.PowerModeChanged += PowerModeChanged;
        var s = settings;
        foreach (var box in new[] { s.SendActivity, s.SendTitles, s.SendMedia, s.SendMicCamera })
        {
            box.Checked += (_, _) => SignalSettingsChanged();
            box.Unchecked += (_, _) => SignalSettingsChanged();
        }
        foreach (var text in new[] { s.TitleExclusions, s.MicExclusions, s.CategoryOverrides })
            text.LostFocus += (_, _) => SignalSettingsChanged();
        UpdateQuietMenu();
    }

    private void CleanupActivitySignals()
    {
        SystemEvents.SessionSwitch -= SessionSwitched;  // static events: they would keep this window alive
        SystemEvents.PowerModeChanged -= PowerModeChanged;
    }

    // SystemEvents call on their own thread.
    private void SessionSwitched(object? sender, SessionSwitchEventArgs e) => Dispatcher.BeginInvoke(new Action(() =>
    {
        if (e.Reason is SessionSwitchReason.SessionLock or SessionSwitchReason.ConsoleDisconnect or SessionSwitchReason.RemoteDisconnect)
        {
            sessionLocked = true; activityTracker.Reset(); signalSchedule.Urgent();
        }
        else if (e.Reason is SessionSwitchReason.SessionUnlock or SessionSwitchReason.ConsoleConnect or SessionSwitchReason.RemoteConnect)
        {
            if (!sessionLocked) return;
            sessionLocked = false; activityTracker.Reset(); signalSchedule.Mark("unlock");  // §3: the device takes over at once
        }
    }));

    private void PowerModeChanged(object? sender, PowerModeChangedEventArgs e) => Dispatcher.BeginInvoke(new Action(() =>
    {
        if (e.Mode != PowerModes.Resume) return;
        activityTracker.Reset();
        // A computer that locks on sleep wakes to its lock screen: the unlock is the return then.
        if (!sessionLocked) signalSchedule.Mark("resume");
    }));

    /// <summary>The user changed what is sent: read the controls, save, and send at once (§14).</summary>
    private void SignalSettingsChanged()
    {
        if (signalControlsLoading) return;
        ApplySignalSettings(ReadSignalControls());
        SchedulePreferencesSave();
        signalSchedule.Urgent();
    }

    private SignalSettings ReadSignalControls() => new(
        settings.SendActivity.IsChecked == true, settings.SendMedia.IsChecked == true, settings.SendTitles.IsChecked == true,
        settings.SendMicCamera.IsChecked == true, settings.TitleExclusions.Text, settings.MicExclusions.Text,
        settings.CategoryOverrides.Text);

    private void ApplySignalSettings(SignalSettings value)
    {
        signalSettings = value;
        titleExclusions = SignalSettings.Names(value.TitleExclusions);
        micExclusions = SignalSettings.Names(value.MicExclusions);
        categoryOverrides = SignalSettings.Overrides(value.CategoryOverrides);
        if (!value.Media) { latestMedia = null; latestMediaKnown = false; }
        if (!value.MicCamera) latestMicCamera = null;
    }

    private void LoadSignalControls(SignalSettings value)
    {
        signalControlsLoading = true;
        try
        {
            settings.SendActivity.IsChecked = value.Activity; settings.SendTitles.IsChecked = value.Titles;
            settings.SendMedia.IsChecked = value.Media; settings.SendMicCamera.IsChecked = value.MicCamera;
            settings.TitleExclusions.Text = value.TitleExclusions; settings.MicExclusions.Text = value.MicExclusions;
            settings.CategoryOverrides.Text = value.CategoryOverrides;
        }
        finally { signalControlsLoading = false; }
        ApplySignalSettings(value);
    }

    /// <summary>Reads what the loop sends next: the program in front, media, microphone and camera.</summary>
    private void ObserveSignals(DateTime now)
    {
        var s = signalSettings;
        if (!sessionLocked && s.Activity && ReadForeground() is { } observation && activityTracker.Observe(observation, now))
            signalSchedule.Urgent();
        if (s.Media)
        {
            if ((now - lastMediaRead).TotalSeconds >= 5 && !mediaReading) { lastMediaRead = now; _ = RefreshMedia(); }
            signalSchedule.SeeMedia(latestMedia, latestMediaKnown);
        }
        else signalSchedule.SeeMedia(null, false);
        if (s.MicCamera)
        {
            if ((now - lastMicRead).TotalSeconds >= 5) { lastMicRead = now; latestMicCamera = ReadMicCamera(); }
            signalSchedule.SeeMicCamera(latestMicCamera);
        }
        else signalSchedule.SeeMicCamera(null);
    }

    private Dictionary<string, object?> PresenceSignal(int idle, string zone, DateTime now) => PresenceBody.Build(
        idle, zone, serverTakesSignals, sessionLocked, signalSchedule.After, activityTracker.Current(now),
        activityTracker.Known, latestMedia, latestMediaKnown, latestMicCamera, signalSettings, titleExclusions);

    /// <summary>What is in front; null when it is HIMARI's own window (what was there is kept, §4.1).</summary>
    private ActivityObservation? ReadForeground()
    {
        IntPtr window = GetForegroundWindow();
        if (OwnWindow(window) || IsChatWindow(window)) return null;
        if (window == IntPtr.Zero || !IsWindowVisible(window)) return new ActivityObservation(null);
        var className = new StringBuilder(128); GetClassName(window, className, className.Capacity);
        if (className.ToString() is "Progman" or "WorkerW" or "Shell_TrayWnd" or "Shell_SecondaryTrayWnd") return new ActivityObservation(null);
        GetWindowThreadProcessId(window, out uint pid);
        string? path = ProcessPath(pid);
        string program = ActivityCatalog.ProgramKey(path) is { Length: > 0 } key ? key : SafeProcessName(pid);
        if (program.Length == 0) return new ActivityObservation("other");
        var title = new StringBuilder(512); GetWindowText(window, title, title.Capacity);
        bool exclusive = SHQueryUserNotificationState(out int state) == 0 && state == RunningD3DFullscreen;
        bool fullscreen = exclusive || (!IsIconic(window) && GetWindowRect(window, out var rect)
            && CoversMonitor(rect, System.Windows.Forms.Screen.FromHandle(window).Bounds));
        string category = ActivityCatalog.Classify(program, path, title.ToString(), exclusive, categoryOverrides);
        return new ActivityObservation(category, ProgramName(program, path), fullscreen, title.ToString());
    }

    private static string? ProcessPath(uint pid)
    {
        IntPtr handle = OpenProcess(ProcessQueryLimitedInformation, false, pid);  // works for elevated programs too
        if (handle == IntPtr.Zero) return null;
        try
        {
            var path = new StringBuilder(1024); int size = path.Capacity;
            return QueryFullProcessImageName(handle, 0, path, ref size) ? path.ToString() : null;
        }
        finally { CloseHandle(handle); }
    }

    private static string SafeProcessName(uint pid)
    {
        try { using var process = Process.GetProcessById((int)pid); return process.ProcessName; }
        catch (Exception ex) when (ex is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception) { return ""; }
    }

    /// <summary>The program's own name ("Visual Studio Code"), read once per program; else its file name.</summary>
    private string ProgramName(string program, string? path)
    {
        if (programNames.TryGetValue(path ?? program, out var known)) return known;
        string name = program;
        try
        {
            if (path is not null && FileVersionInfo.GetVersionInfo(path).FileDescription is { Length: > 0 } description) name = description.Trim();
        }
        catch (Exception ex) when (ex is FileNotFoundException or IOException or UnauthorizedAccessException) { }
        programNames[path ?? program] = name;
        return name;
    }

    /// <summary>What plays now through Windows' media controls (§5); unknown when they cannot be read.</summary>
    private async Task RefreshMedia()
    {
        mediaReading = true;
        try
        {
            mediaManager ??= await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
            var session = mediaManager.GetCurrentSession();
            MediaInfo? playing = null;
            if (session is not null && session.GetPlaybackInfo()?.PlaybackStatus == GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing)
            {
                var properties = await session.TryGetMediaPropertiesAsync();
                if (!string.IsNullOrWhiteSpace(properties?.Title))
                    playing = new MediaInfo(properties.Title, string.IsNullOrWhiteSpace(properties.Artist) ? null : properties.Artist,
                        ActivityCatalog.MediaAppName(session.SourceAppUserModelId));
            }
            if (signalSettings.Media) { latestMedia = playing; latestMediaKnown = true; }
        }
        catch (Exception ex) when (ex is COMException or InvalidOperationException or UnauthorizedAccessException)
        {
            latestMedia = null; latestMediaKnown = false;  // unknown: the field is left out
        }
        finally { mediaReading = false; }
    }

    /// <summary>§6.1: a program outside this app uses the microphone or the camera; null when it cannot be read.</summary>
    private bool? ReadMicCamera()
    {
        try
        {
            var entries = ConsentEntries("microphone").Concat(ConsentEntries("webcam")).ToList();
            return MicCamera.InUse(entries, micExclusions, Environment.ProcessPath ?? "Himari.Companion");
        }
        catch (Exception ex) when (ex is System.Security.SecurityException or UnauthorizedAccessException or IOException) { return null; }
    }

    private static List<(string App, long Started, long Stopped)> ConsentEntries(string capability)
    {
        var found = new List<(string, long, long)>();
        using var root = Registry.CurrentUser.OpenSubKey(
            @"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\" + capability);
        if (root is null) return found;
        foreach (string name in root.GetSubKeyNames())
        {
            using var key = root.OpenSubKey(name);
            if (key is null) continue;
            if (name == "NonPackaged")
            {
                foreach (string program in key.GetSubKeyNames())
                {
                    using var entry = key.OpenSubKey(program);
                    if (entry is not null) found.Add((program, Moment(entry, "LastUsedTimeStart"), Moment(entry, "LastUsedTimeStop")));
                }
            }
            else found.Add((name, Moment(key, "LastUsedTimeStart"), Moment(key, "LastUsedTimeStop")));
        }
        return found;
    }

    private static long Moment(RegistryKey key, string value) => key.GetValue(value) is long moment ? moment : 0;

    /// <summary>What was sent last, in the settings, so the user sees exactly what goes (§8).</summary>
    private void ShowSignalPreview(Dictionary<string, object?> body)
    {
        settings.SignalPreview.Text = serverTakesSignals
            ? JsonSerializer.Serialize(body, new JsonSerializerOptions { WriteIndented = true, Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping })
            : "이 서버는 아직 활동 신호를 받지 않습니다. 입력 여부와 시간대만 보냅니다.";
    }

    // --- quiet (§6.2) -------------------------------------------------------------------------

    private void QuietFromServer(JsonElement data, string property)
    {
        quietUntil = Str(data, property) is { } text && DateTimeOffset.TryParse(text, out var until) ? until : null;
        UpdateQuietMenu();
    }

    private void UpdateQuietMenu()
    {
        bool on = quietUntil is { } end && end > DateTimeOffset.Now;
        QuietState.Header = serverTakesSignals ? QuietTimes.Describe(quietUntil, DateTimeOffset.Now) : "서버가 아직 조용히를 지원하지 않아요";
        QuietHourMenu.IsEnabled = QuietMorningMenu.IsEnabled = serverTakesSignals && client is not null;
        QuietOffMenu.IsEnabled = serverTakesSignals && client is not null && on;
    }

    private void QuietHourClick(object sender, RoutedEventArgs e) => _ = SetQuiet(QuietTimes.OneHour(DateTimeOffset.Now));
    private void QuietMorningClick(object sender, RoutedEventArgs e) => _ = SetQuiet(QuietTimes.NextMorning(DateTimeOffset.Now));
    private void QuietOffClick(object sender, RoutedEventArgs e) => _ = SetQuiet(null);

    private async Task SetQuiet(DateTimeOffset? until)
    {
        if (client is not { } local) return;
        try
        {
            var answer = await local.SendAsync(System.Net.Http.HttpMethod.Put, "/api/presence/quiet",
                new Dictionary<string, object?> { ["until"] = until?.ToString("o") }, lifetime.Token);
            QuietFromServer(answer, "until");
        }
        catch (Exception ex) when (ex is ApiException or System.Net.Http.HttpRequestException or OperationCanceledException) { ShowError(ex); }
    }
}
