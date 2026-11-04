using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Threading;
using Himari.Companion;

internal static class HotkeyReturnChecks
{
    private const BindingFlags Inside = BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern bool SetProp(IntPtr window, string name, IntPtr value);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetProp(IntPtr window, string name);
    [DllImport("user32.dll")] private static extern IntPtr GetShellWindow();

    // Real, visible HWNDs in another process, offscreen and without activation.
    // This reproduces the Notepad property without touching the user's applications.
    internal static void Fixture()
    {
        var app = new System.Windows.Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        Window Make() => new() { Title = "HIMARI return-target fixture", ShowActivated = false, ShowInTaskbar = false,
            Left = -30000, Top = -30000, Width = 150, Height = 80 };
        var source = Make(); var other = Make();
        source.Show(); other.Show();
        var sourceHandle = new WindowInteropHelper(source).Handle;
        if (!SetProp(sourceHandle, "NonRudeHWND", new IntPtr(1))) throw new Exception("fixture property could not be set");
        Console.WriteLine(JsonSerializer.Serialize(new { source = sourceHandle.ToInt64(), other = new WindowInteropHelper(other).Handle.ToInt64() }));
        Console.Out.Flush();
        _ = Task.Run(async () => { await Console.In.ReadLineAsync(); await app.Dispatcher.InvokeAsync(() => app.Shutdown()); });
        app.Run();
    }

    private static void Wait(Func<bool> ready)
    {
        var frame = new DispatcherFrame(); var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(20) };
        var deadline = DateTime.UtcNow.AddSeconds(10);
        timer.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) frame.Continue = false; };
        try { if (!ready()) { timer.Start(); Dispatcher.PushFrame(frame); } }
        finally { timer.Stop(); }
        if (!ready()) throw new Exception("return-target fixture timed out");
    }

    internal static void Run(Action<bool, string> check)
    {
        using var fixture = Process.Start(new ProcessStartInfo(Environment.ProcessPath!, "--work-window-fixture")
        { UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden })!;
        var window = new MainWindow(true);
        object? Call(string name, params object?[] args) => typeof(MainWindow).GetMethod(name, Inside)!.Invoke(window, args);
        void Field(string name, object value) => typeof(MainWindow).GetField(name, Inside)!.SetValue(window, value);
        try
        {
            var ready = fixture.StandardOutput.ReadLineAsync(); Wait(() => ready.IsCompleted);
            using var handles = JsonDocument.Parse(ready.GetAwaiter().GetResult()!);
            IntPtr source = new(handles.RootElement.GetProperty("source").GetInt64());
            IntPtr other = new(handles.RootElement.GetProperty("other").GetInt64());
            check(GetProp(source, "NonRudeHWND") != IntPtr.Zero, "foreign fixture carries the same NonRudeHWND property as Notepad");
            check(!(bool)Call("ShellSwitcher", source)! && (bool)Call("EligibleWorkWindow", source)!,
                "an ordinary foreign application with NonRudeHWND is eligible for shortcut return");
            check((bool)Call("EligibleWorkWindow", other)!, "ordinary applications without NonRudeHWND remain eligible too");

            IntPtr current = other;
            Field("foregroundWindowProbe", new Func<IntPtr>(() => current));
            var restored = new List<IntPtr>();
            Field("restoreWindowProbe", new Func<IntPtr, bool>(target => { restored.Add(target); current = target; return true; }));
            var history = (WorkWindowHistory)typeof(MainWindow).GetField("workWindow", Inside)!.GetValue(window)!;
            var own = new WindowInteropHelper(window).EnsureHandle();
            bool Eligible(IntPtr target) => (bool)Call("EligibleWorkWindow", target)!;
            Call("ObserveForeground", other);
            check(history.Target == other, "initial external foreground is remembered");
            // The first shortcut must replace the older app even if WinEvent has not caught up.
            current = source; check(history.Enter(current, Eligible), "shortcut pins the actual source rather than the older observation");
            var editor = (TextBox)window.FindName("Input"); Call("OpenComposer"); editor.Text = "복귀해도 남길 초안";
            current = own; Call("ObserveForeground", other);
            check(history.Target == source, "a delayed event from an older app cannot overwrite the pinned shortcut source");
            Call("CloseChat");
            check(restored.Count == 1 && restored[0] == source && editor.Text == "복귀해도 남길 초안",
                "shortcut exit restores the pinned source and preserves the draft");

            current = source; Call("ObserveForeground", other);
            check(history.Target == source, "stale events are ignored outside chat as well");
            current = other; Call("ObserveForeground", other);
            check(history.Target == other && history.Enter(current, Eligible), "a genuinely new external visit replaces the previous shortcut pin");
            current = own; Call("ObserveForeground", source); Call("CloseChat");
            check(restored.Count == 2 && restored[1] == other, "the next shortcut round trip follows the new application");

            current = GetShellWindow(); Call("ObserveForeground", current);
            check(history.Target == IntPtr.Zero, "a desktop visit clears the previous application instead of resurrecting it");
            history.ObserveExternal(source, Eligible);
            check(!history.Enter(GetShellWindow(), Eligible) && history.Target == IntPtr.Zero,
                "shortcut entry from an excluded desktop also clears any old target");
        }
        finally
        {
            window.Close();
            if (!fixture.HasExited)
            {
                fixture.StandardInput.WriteLine("exit"); fixture.StandardInput.Flush();
                if (!fixture.WaitForExit(3000)) fixture.Kill();
            }
        }
    }
}
