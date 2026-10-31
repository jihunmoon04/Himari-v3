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

    private bool IsChatWindow(IntPtr window) => window != IntPtr.Zero &&
        (window == new WindowInteropHelper(this).Handle || (dialogueHost is not null && window == new WindowInteropHelper(dialogueHost).Handle));

    private bool EligibleWorkWindow(IntPtr window)
    {
        if (window == IntPtr.Zero || !IsWindow(window) || !IsWindowVisible(window) || OwnWindow(window)) return false;
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
    }

    private void UpdateFullscreenPresentation()
    {
        if (shellFullscreen is null) return;
        IntPtr foreground = GetForegroundWindow();
        bool preserve = IsChatWindow(foreground) && EligibleWorkWindow(lastWorkWindow) && !IsIconic(lastWorkWindow)
            && GetWindowRect(lastWorkWindow, out var rect)
            && CoversMonitor(rect, System.Windows.Forms.Screen.FromHandle(lastWorkWindow).Bounds);
        shellFullscreen.Mark(new WindowInteropHelper(this).Handle, preserve && IsVisible);
        if (dialogueHost is not null) shellFullscreen.Mark(new WindowInteropHelper(dialogueHost).Handle, preserve && dialogueHost.IsVisible);
        if (preserve && IsVisible) fullscreenTimer.Start(); else fullscreenTimer.Stop();
    }

    private static bool CoversMonitor(NativeRect rect, System.Drawing.Rectangle monitor) =>
        rect.Left <= monitor.Left && rect.Top <= monitor.Top && rect.Right >= monitor.Right && rect.Bottom >= monitor.Bottom;

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
