using System.Runtime.InteropServices;
using System.Windows.Interop;
using Windows.Media.Control;

namespace Himari.Companion;

// V3-P90 §2, §9: reading Windows for the sleep rules (SleepPolicy.cs) — the screen's power state, a key
// pressed anywhere (raw input: that one was pressed, never which), what plays, a call, a fullscreen window —
// and what follows a change: the signal goes at once, waking stretches, a reminder told while asleep stays up.
public partial class MainWindow
{
    private readonly SleepPolicy sleepPolicy = new();
    private bool displayOn = true;
    private bool quietPlaying, quietCall;
    private IReadOnlyList<MediaSession> mediaSessions = [];  // V3-P90 §5.3: a video's own session gone is its end
    private DateTime lastQuietRead = DateTime.MinValue;
    private bool reminderAwake;  // a reminder wakes it for its words only (§2.4): no stretch, asleep after
    private CancellationToken reminderLive;  // the live that woke it: only that one ends it (PR #164 re-review P2-3)
    private bool bubbleUntilAwake;  // that reminder's bubble stays until the user wakes it (§2.4)
    private DateTime stretchUntil = DateTime.MinValue;  // a wake's words wait for the stretch (§5.2)
    private TaskCompletionSource presenceNudge = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private HwndSource? sleepSource;
    private IntPtr displayNotification;

    private static readonly Guid ConsoleDisplayState = new("6FE69556-704A-47A0-8F24-C28D936FDA47");
    private const int WmPowerBroadcast = 0x0218, PbtPowerSettingChange = 0x8013, WmInput = 0x00FF;
    private const uint RidevInputSink = 0x00000100, RidevRemove = 0x00000001;

    [StructLayout(LayoutKind.Sequential)] private struct RawInputDevice { internal ushort UsagePage, Usage; internal uint Flags; internal IntPtr Target; }
    [DllImport("user32.dll", SetLastError = true)] private static extern bool RegisterRawInputDevices(RawInputDevice[] devices, uint count, uint size);
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr RegisterPowerSettingNotification(IntPtr recipient, ref Guid setting, int flags);
    [DllImport("user32.dll")] private static extern bool UnregisterPowerSettingNotification(IntPtr handle);

    private void SetupSleepControls()
    {
        IntPtr handle = new WindowInteropHelper(this).Handle;
        sleepSource = HwndSource.FromHwnd(handle);
        sleepSource?.AddHook(SleepHook);
        var setting = ConsoleDisplayState;
        displayNotification = RegisterPowerSettingNotification(handle, ref setting, 0);  // DEVICE_NOTIFY_WINDOW_HANDLE
        RegisterRawInputDevices([new RawInputDevice { UsagePage = 1, Usage = 6, Flags = RidevInputSink, Target = handle }], 1,
            (uint)Marshal.SizeOf<RawInputDevice>());
    }

    private void CleanupSleepControls()
    {
        if (displayNotification != IntPtr.Zero) { UnregisterPowerSettingNotification(displayNotification); displayNotification = IntPtr.Zero; }
        RegisterRawInputDevices([new RawInputDevice { UsagePage = 1, Usage = 6, Flags = RidevRemove, Target = IntPtr.Zero }], 1,
            (uint)Marshal.SizeOf<RawInputDevice>());
        sleepSource?.RemoveHook(SleepHook); sleepSource = null;
    }

    private IntPtr SleepHook(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (message == WmInput && sleepPolicy.Asleep && !sleepPolicy.Locked) WakeFromUser();  // a key: awake at once
        else if (message == WmPowerBroadcast && wParam == PbtPowerSettingChange && lParam != IntPtr.Zero
            && Marshal.PtrToStructure<Guid>(lParam) == ConsoleDisplayState)
        {
            // POWERBROADCAST_SETTING: the GUID, a DWORD length, then the data: 0 off, 1 on, 2 dimmed (still on)
            displayOn = Marshal.ReadInt32(lParam, 20) != 0;
            ObserveSleep();
        }
        return IntPtr.Zero;
    }

    /// <summary>Reads Windows and asks the policy; a change is told and shown at once.</summary>
    private void ObserveSleep()
    {
        try
        {
            var now = DateTime.UtcNow;
            if (!sleepPolicy.Asleep && (now - lastQuietRead).TotalSeconds >= 5) { lastQuietRead = now; RefreshQuiet(); }
            bool quiet = quietPlaying || quietCall || FullscreenNow();
            if (sleepPolicy.Observe(now, now - TimeSpan.FromMilliseconds(IdleMilliseconds()), displayOn, sessionLocked, quiet)) SleepChanged();
        }
        catch (System.ComponentModel.Win32Exception) { }
    }

    /// <summary>The user is here: a key, a click on HIMARI, typing in its box, an unlock.</summary>
    private void WakeFromUser() { if (sleepPolicy.WakeNow()) SleepChanged(); }

    private void SleepChanged()
    {
        signalSchedule.Urgent();
        presenceNudge.TrySetResult();  // the presence loop sends now, not at its next turn
        if (sleepPolicy.Asleep) taskTracker.Reset();  // V3-P90 §6.1: sleep ends the task, and no end is told
        else
        {
            if (character.Manifest.Clips.TryGetValue("stretch", out var stretch))
                stretchUntil = DateTime.UtcNow + TimeSpan.FromTicks((long)(stretch.Length.Ticks / animationSpeed));
            if (bubbleUntilAwake) { bubbleUntilAwake = false; ScheduleBubbleClose(Message.Text); }  // now it may go
        }
        UpdateFrame();
    }

    /// <summary>The presence loop's wait: its period, or a sleep change.</summary>
    private async Task PresenceWait(TimeSpan period, CancellationToken ct)
    {
        var nudge = presenceNudge.Task;
        await Task.WhenAny(Task.Delay(period, ct), nudge);
        if (nudge.IsCompleted) presenceNudge = new(TaskCreationOptions.RunContinuationsAsynchronously);
        ct.ThrowIfCancellationRequested();
    }

    /// <summary>§2.2, read here only (not sent): something plays, a call. The user's sending settings do not matter.</summary>
    private void RefreshQuiet()
    {
        quietCall = ReadMicCamera() == true;
        _ = RefreshPlaying();
    }

    private async Task RefreshPlaying()
    {
        try
        {
            mediaManager ??= await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
            mediaSessions = mediaManager.GetSessions().Select(s => new MediaSession(ActivityCatalog.MediaAppName(s.SourceAppUserModelId) ?? "",
                s.GetPlaybackInfo()?.PlaybackStatus == GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing)).ToList();
            quietPlaying = mediaSessions.Any(s => s.Playing);
        }
        catch (Exception ex) when (ex is COMException or InvalidOperationException or UnauthorizedAccessException) { quietPlaying = false; }
    }

    private static bool FullscreenNow() => SHQueryUserNotificationState(out int state) == 0 && state is 2 or 3 or 4;  // busy, D3D, presentation

    /// <summary>§2.4: a wake for this device while asleep is a reminder — it wakes for its words, and its bubble
    /// stays. <paramref name="live"/>: the live that speaks them, the only one whose end puts it back to sleep.</summary>
    private void WakeJobArrived(CancellationToken live)
    {
        if (!sleepPolicy.Asleep || sleepPolicy.Locked) return;
        reminderAwake = true; reminderLive = live; bubbleUntilAwake = true;
        UpdateFrame();
    }

    /// <summary>A live ended: if it is the one that woke it, asleep again (the bubble stays). A live cancelled by a
    /// reconnect that ends after the new one started is not that one.</summary>
    private void WakeJobEnded(CancellationToken live)
    {
        if (!reminderAwake || reminderLive != live) return;
        reminderAwake = false; reminderLive = default;
        UpdateFrame();
    }

    /// <summary>A wake's words wait for the stretch of the wake that brought them (§5.2).</summary>
    private TimeSpan StretchLeft() => stretchUntil > DateTime.UtcNow ? stretchUntil - DateTime.UtcNow : TimeSpan.Zero;
}
