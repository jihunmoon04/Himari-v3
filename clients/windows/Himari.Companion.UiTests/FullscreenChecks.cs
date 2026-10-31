using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using Himari.Companion;

internal static class FullscreenChecks
{
    private const BindingFlags Private = BindingFlags.NonPublic | BindingFlags.Instance;
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(System.Drawing.Point point);
    internal static void Fixture()
    {
        var app = new System.Windows.Application();
        var window = new Window { Title = "HIMARI fullscreen test fixture", WindowStyle = WindowStyle.None, ResizeMode = ResizeMode.NoResize,
            Left = 0, Top = 0, Width = SystemParameters.PrimaryScreenWidth, Height = SystemParameters.PrimaryScreenHeight, Background = Brushes.DarkSlateBlue };
        window.Loaded += (_, _) => { Console.WriteLine(new WindowInteropHelper(window).Handle.ToInt64()); Console.Out.Flush(); };
        _ = Task.Run(async () =>
        {
            while (await Console.In.ReadLineAsync() is { } command)
                await window.Dispatcher.InvokeAsync(() =>
                {
                    if (command == "normal") { window.Width = 500; window.Height = 400; window.Left = 100; window.Top = 100; window.UpdateLayout(); Console.WriteLine("normal"); Console.Out.Flush(); }
                    else if (command == "exit") window.Close();
                });
        });
        app.Run(window);
    }
    private static void Until(Func<bool> ready)
    {
        var frame = new DispatcherFrame(); var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(25) }; var deadline = DateTime.UtcNow.AddSeconds(10);
        timer.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) frame.Continue = false; };
        if (!ready()) { timer.Start(); Dispatcher.PushFrame(frame); timer.Stop(); }
        if (!ready()) throw new Exception("native fullscreen transition timed out");
    }
    internal static void Run(Action<bool, string> check)
    {
        using var fixture = Process.Start(new ProcessStartInfo(Environment.ProcessPath!, "--fullscreen-fixture") { UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true, CreateNoWindow = true })!;
        var w = new MainWindow(true) { Left = 500, Top = 300 };
        object? Call(string method, params object?[] args) => typeof(MainWindow).GetMethod(method, Private)!.Invoke(w, args);
        try
        {
            var ready = fixture.StandardOutput.ReadLineAsync(); Until(() => ready.IsCompleted);
            IntPtr fullscreen = new(long.Parse(ready.Result!));
            w.Show(); w.Activate(); Call("SetupForegroundTracking");
            var history = (WorkWindowHistory)typeof(MainWindow).GetField("workWindow", Private)!.GetValue(w)!;
            var shell = (ShellFullscreen)typeof(MainWindow).GetField("shellFullscreen", Private)!.GetValue(w)!;
            var marks = (Dictionary<IntPtr, bool>)typeof(ShellFullscreen).GetField("marked", Private)!.GetValue(shell)!;
            var hwnd = new WindowInteropHelper(w).Handle;
            var restore = typeof(MainWindow).GetMethod("RestoreWindow", BindingFlags.Static | BindingFlags.NonPublic)!;
            shell.Mark(hwnd, true); check(marks.GetValueOrDefault(hwnd), "native Shell accepts fullscreen marking for companion window");
            shell.Mark(hwnd, false); check(!marks.GetValueOrDefault(hwnd), "native Shell marking can be cleared");
            if (!(bool)restore.Invoke(null, new object[] { fullscreen })!)
            {
                Console.WriteLine("Native Shell fullscreen mark/reset passed. Foreground round-trip skipped: Windows denied foreground activation from this background test process.");
                return;
            }
            Until(() => history.Target == fullscreen);
            void Hotkey() => Call("HotKeyHook", hwnd, 0x0312, new IntPtr(0x4849), IntPtr.Zero, false);
            Hotkey(); Until(() => marks.GetValueOrDefault(hwnd));
            var input = (System.Windows.Controls.TextBox)w.FindName("Input"); input.Text = "전체화면에서도 보존할 초안";
            check((bool)Call("IsChatWindow", GetForegroundWindow())!, "Alt+Q enters detached chat over a fullscreen foreign process");
            var bottom = new System.Drawing.Point(50, (int)(SystemParameters.PrimaryScreenHeight * VisualTreeHelper.GetDpi(w).DpiScaleY) - 8);
            Until(() => WindowFromPoint(bottom) == fullscreen);
            Hotkey(); Until(() => GetForegroundWindow() == fullscreen && !marks.GetValueOrDefault(hwnd));
            check(input.Text == "전체화면에서도 보존할 초안", "Alt+Q restores fullscreen target and keeps draft");
            w.Activate(); Until(() => marks.GetValueOrDefault(hwnd));
            check(WindowFromPoint(bottom) == fullscreen, "ordinary activation (Alt+Tab path) retains fullscreen taskbar ordering");
            fixture.StandardInput.WriteLine("normal"); fixture.StandardInput.Flush(); var normal = fixture.StandardOutput.ReadLineAsync(); Until(() => normal.IsCompleted);
            Call("UpdateFullscreenPresentation");
            check(!marks.GetValueOrDefault(hwnd), "leaving fullscreen removes Shell marking without hiding taskbar manually");
            Console.WriteLine("Native fullscreen fixture: Alt+Q round trip, Alt+Tab activation, taskbar hit testing and state restoration passed.");
        }
        finally
        {
            w.Close();
            if (!fixture.HasExited) { fixture.StandardInput.WriteLine("exit"); fixture.StandardInput.Flush(); if (!fixture.WaitForExit(3000)) fixture.Kill(); }
        }
    }
}
