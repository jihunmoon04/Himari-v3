using System.Net;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;

namespace Himari.Companion;

// V3-P91 §7: HIMARI asks for the screen (its screen tool). The app captures by the same rules as a message's
// screen, shows that it looked, and posts the image to the request — or says why not.
public partial class MainWindow
{
    private static readonly TimeSpan ScreenGateWait = TimeSpan.FromSeconds(10);  // the server waits 15 (§4.3)
    private int screenLookNoticeVersion;

    private delegate bool EnumWindowsProc(IntPtr window, IntPtr data);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr data);
    [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr window, int attribute, out int value, int size);
    [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr window, int attribute, out NativeRect value, int size);
    private const int DwmCloaked = 14, DwmExtendedFrameBounds = 9;

    private async Task AnswerScreenRequest(ServerClient local, JsonElement data, CancellationToken ct)
    {
        string? requestId = Str(data, "request_id");
        if (string.IsNullOrEmpty(requestId)) return;
        try
        {
            // One capture at a time; what is checked is the monitor that is captured (§14 Q10).
            var (ran, outcome) = await captureGate.RunAsync(() => CaptureForRequestAsync(ct), ScreenGateWait, ct);
            if (!ran || outcome.Refusal is not null || outcome.Screen is null)
            {
                await local.RefuseScreenAsync(requestId, ran ? outcome.Refusal ?? "capture_failed" : "capture_failed", ct);
                return;
            }
            var captured = outcome.Screen;
            if (ScreenRefusalNow([]) is { } late)  // the last moment before it goes: turned off or locked since
            {
                await local.RefuseScreenAsync(requestId, late, ct);
                return;
            }
            await local.AnswerScreenAsync(requestId, captured.Bytes, captured.ContentType, ct);
            // §6: the job's moves are on the screen it last saw.
            if (Str(data, "job_id") is { Length: > 0 } jobId && Str(data, "conversation") is { } conversation && moveLifetime.CanBind(jobId))
                moveJobs[jobId] = new(conversation, new(jobId, captured.Geometry, captured.CapturedAt));
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { }
        catch (ApiException ex) when (ex.Status is HttpStatusCode.Gone or HttpStatusCode.NotFound) { /* §4.3: it ended; nothing to show */ }
        catch (Exception ex) when (ex is ApiException or System.Net.Http.HttpRequestException or OperationCanceledException)
        {
            System.Diagnostics.Trace.TraceWarning("HIMARI: could not answer a screen request: {0}", ex.Message);
        }
    }

    /// <summary>Why the screen may not go now: the setting, asleep or locked, and the programs given.</summary>
    private string? ScreenRefusalNow(IEnumerable<string?> programs) =>
        ScreenRequestPolicy.Refusal(signalSettings.ScreenLook, sleepPolicy.Asleep || sessionLocked, programs, titleExclusions);

    /// <summary>Chooses the monitor, then checks everything — the setting, the lock, the programs that show on that
    /// monitor — before the capture and again after it (<see cref="GuardedCapture"/>): turned off, locked, or an
    /// excluded window come in meanwhile, the image is dropped.</summary>
    private async Task<(string? Refusal, CapturedScreen? Screen)> CaptureForRequestAsync(CancellationToken ct)
    {
        var bounds = CaptureBounds();
        var monitor = new PixelRect(bounds.Left, bounds.Top, bounds.Width, bounds.Height);
        try
        {
            return await GuardedCapture.RunAsync(() => ScreenRefusalNow(ScreenWindows.On(VisibleWindows(), monitor)), async () =>
            {
                ShowScreenLookNotice();
                return await CaptureMonitorAsync(bounds, ct);
            });
        }
        catch (Exception ex) when (ex is not OperationCanceledException) { return ("capture_failed", null); }
    }

    /// <summary>The top-level windows that show: visible, not minimized, not cloaked (another virtual desktop), not
    /// HIMARI's own; each with its visible frame (not the resize border, which reaches the next monitor).</summary>
    private List<VisibleWindow> VisibleWindows()
    {
        var found = new List<VisibleWindow>();
        var programs = new Dictionary<uint, string>();
        EnumWindows((window, _) =>
        {
            if (!IsWindowVisible(window) || IsIconic(window) || OwnWindow(window)) return true;
            if (DwmGetWindowAttribute(window, DwmCloaked, out int cloaked, sizeof(int)) == 0 && cloaked != 0) return true;
            if (DwmGetWindowAttribute(window, DwmExtendedFrameBounds, out NativeRect frame, Marshal.SizeOf<NativeRect>()) != 0
                && !GetWindowRect(window, out frame)) return true;
            GetWindowThreadProcessId(window, out uint pid);
            if (!programs.TryGetValue(pid, out var program))
                programs[pid] = program = ActivityCatalog.ProgramKey(ProcessPath(pid)) is { Length: > 0 } key ? key : SafeProcessName(pid);
            found.Add(new(program.Length > 0 ? program : null, new(frame.Left, frame.Top, frame.Right - frame.Left, frame.Bottom - frame.Top)));
            return true;
        }, IntPtr.Zero);
        return found;
    }

    /// <summary>§7.2: a small "looking at the screen" for two seconds, shown while it captures (the window is left out of it).</summary>
    private async void ShowScreenLookNotice()
    {
        int version = ++screenLookNoticeVersion;
        ScreenLookNotice.Visibility = Visibility.Visible;
        await Task.Delay(2000);
        if (version == screenLookNoticeVersion) ScreenLookNotice.Visibility = Visibility.Collapsed;
    }

    /// <summary>§7.3, §14 Q9: the first time the server says it can ask for the screen, the saved "attach to every
    /// message" goes off once, with a note under it; it is not changed again.</summary>
    private void ScreenSharingOffOnce()
    {
        if (screenSharingReset) return;
        screenSharingReset = true;
        if (settings.ScreenSharing.IsChecked == true)
        {
            settings.ScreenSharing.IsChecked = false;
            settings.ScreenSharingNotice.Visibility = Visibility.Visible;
        }
        SchedulePreferencesSave();
    }
}
