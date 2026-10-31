using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Interop;

namespace Himari.Companion;

public partial class MainWindow
{
    private delegate void ForegroundEvent(IntPtr hook, uint kind, IntPtr window, int objectId, int childId, uint thread, uint time);
    private ForegroundEvent? foregroundCallback;
    private IntPtr foregroundHook;
    private ShellFullscreen? shellFullscreen;
    private readonly System.Windows.Threading.DispatcherTimer fullscreenTimer = new() { Interval = TimeSpan.FromMilliseconds(300) };
    [DllImport("user32.dll")] private static extern IntPtr SetActiveWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern IntPtr GetShellWindow();
    [DllImport("user32.dll")] private static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module, ForegroundEvent callback, uint process, uint thread, uint flags);
    [DllImport("user32.dll")] private static extern bool UnhookWinEvent(IntPtr hook);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetProp(IntPtr window, string name);

    private bool IsChatWindow(IntPtr window) => window != IntPtr.Zero &&
        (window == new WindowInteropHelper(this).Handle || (dialogueHost is not null && window == new WindowInteropHelper(dialogueHost).Handle));

    private bool EligibleWorkWindow(IntPtr window)
    {
        if (window == IntPtr.Zero || !IsWindow(window) || !IsWindowVisible(window) || OwnWindow(window) || ShellSwitcher(window)) return false;
        var name = new StringBuilder(128); GetClassName(window, name, name.Capacity);
        return name.ToString() is not ("Shell_TrayWnd" or "Shell_SecondaryTrayWnd" or "Progman" or "WorkerW");
    }

    private void SetupForegroundTracking()
    {
        shellFullscreen = new ShellFullscreen();
        fullscreenTimer.Tick += (_, _) => UpdateFullscreenPresentation();
        workWindow.Remember(GetForegroundWindow(), EligibleWorkWindow);
        foregroundCallback = (_, _, window, _, _, _, _) =>
        {
            workWindow.Remember(window, EligibleWorkWindow);
            // Observe Alt+Tab as well as our own shortcut. No key interception is involved.
            UpdateFullscreenPresentation();
        };
        foregroundHook = SetWinEventHook(3, 3, IntPtr.Zero, foregroundCallback, 0, 0, 0);
        Deactivated += (_, _) => Dispatcher.BeginInvoke(new Action(UpdateFullscreenPresentation));
        UpdateFullscreenPresentation();
    }

    // The monitor of a fullscreen work window that is in front, or that we are chatting over.
    private System.Windows.Forms.Screen? FullscreenWorkScreen()
    {
        IntPtr foreground = GetForegroundWindow();
        if (!(IsChatWindow(foreground) || foreground == lastWorkWindow || ShellSwitcher(foreground)) || !EligibleWorkWindow(lastWorkWindow)
            || IsIconic(lastWorkWindow) || !GetWindowRect(lastWorkWindow, out var rect)) return null;
        var screen = System.Windows.Forms.Screen.FromHandle(lastWorkWindow);
        return CoversMonitor(rect, screen.Bounds) ? screen : null;
    }

    private void UpdateFullscreenPresentation()
    {
        if (shellFullscreen is null) return;
        // Mark before switching, while the fullscreen window is still in front: the Shell
        // raises the taskbar on activation, so marking after Alt+Q or Alt+Tab is too late.
        bool preserve = FullscreenWorkScreen() is not null;
        // The mark alone does not keep Windows 11's taskbar down; the dialogue host also
        // covers that monitor (see PositionDialogueHost), so follow changes in either direction.
        if (dialogueHost is { IsVisible: true } && !sizingScene && hostCoversScreen != CoverScreen())
            Dispatcher.BeginInvoke(new Action(UpdateSceneBounds));
        shellFullscreen.Mark(new WindowInteropHelper(this).Handle, preserve && IsVisible);
        if (dialogueHost is not null) shellFullscreen.Mark(new WindowInteropHelper(dialogueHost).Handle, preserve && dialogueHost.IsVisible);
        // Entering fullscreen (a video's button) changes no foreground window, so keep polling.
        if (IsVisible) fullscreenTimer.Start(); else fullscreenTimer.Stop();
    }

    // Chrome shrinks a fullscreen window by one pixel while it is in the background, so allow two.
    private static bool CoversMonitor(NativeRect rect, System.Drawing.Rectangle monitor) =>
        rect.Left <= monitor.Left + 2 && rect.Top <= monitor.Top + 2 && rect.Right >= monitor.Right - 2 && rect.Bottom >= monitor.Bottom - 2;

    // Alt+Tab's switcher (and its staging window) is in front between the two windows;
    // dropping the cover then leaves the character window uncovered when it activates.
    private static bool ShellSwitcher(IntPtr window)
    {
        if (window == IntPtr.Zero) return false;
        if (GetProp(window, "NonRudeHWND") != IntPtr.Zero) return true;
        var name = new StringBuilder(64); GetClassName(window, name, name.Capacity);
        return name.ToString() is "ForegroundStaging" or "XamlExplorerHostIslandWindow" or "MultitaskingViewFrame" or "TaskSwitcherWnd";
    }

    private void CleanupForegroundTracking()
    {
        fullscreenTimer.Stop();
        if (foregroundHook != IntPtr.Zero) UnhookWinEvent(foregroundHook);
        foregroundHook = IntPtr.Zero;
        shellFullscreen?.Dispose(); shellFullscreen = null;
    }
}

// Ask the Shell to retain fullscreen taskbar ordering only while chatting over a
// surviving fullscreen window. Never hide Explorer or change the user's taskbar settings.
internal sealed class ShellFullscreen : IDisposable
{
    [ComImport, Guid("602D4995-B13A-429B-A66E-1935E44F4317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface ITaskbarList2
    {
        void HrInit(); void AddTab(IntPtr window); void DeleteTab(IntPtr window);
        void ActivateTab(IntPtr window); void SetActiveAlt(IntPtr window);
        void MarkFullscreenWindow(IntPtr window, [MarshalAs(UnmanagedType.Bool)] bool fullscreen);
    }
    private ITaskbarList2? taskbar;
    private readonly Dictionary<IntPtr, bool> marked = new();
    internal void Forget(IntPtr window) => marked.Remove(window);
    internal ShellFullscreen()
    {
        try
        {
            var type = Type.GetTypeFromCLSID(new Guid("56FDF344-FD6D-11D0-958A-006097C9A090"));
            taskbar = (ITaskbarList2)Activator.CreateInstance(type!)!; taskbar.HrInit();
        }
        catch (COMException) { taskbar = null; }
    }
    internal void Mark(IntPtr window, bool fullscreen)
    {
        if (taskbar is null || window == IntPtr.Zero || (marked.TryGetValue(window, out bool prior) && prior == fullscreen)) return;
        try { taskbar.MarkFullscreenWindow(window, fullscreen); marked[window] = fullscreen; }
        catch (COMException ex) { System.Diagnostics.Trace.TraceWarning("HIMARI fullscreen ordering: {0}", ex.Message); }
    }
    public void Dispose()
    {
        foreach (var window in marked.Keys.ToArray()) Mark(window, false);
        if (taskbar is not null) Marshal.ReleaseComObject(taskbar);
        taskbar = null;
    }
}
