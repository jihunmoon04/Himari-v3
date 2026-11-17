using System.IO;
using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

namespace Himari.Workbench;

public enum SecondLaunch { Shown, OtherSession, NotResponding }

/// <summary>
/// One app per user across all logon sessions (§3): the mutex <c>Global\HIMARI.Workbench.&lt;SID&gt;</c>, openable only by
/// this user. Its holder alone writes <c>workbench.json</c> and manages the runner. A second launch asks the first over
/// the pipe <c>HIMARI.Workbench.&lt;SID&gt;</c> (this user only) to show its window; that is the only message it takes.
/// </summary>
public sealed class SingleInstance : IDisposable
{
    private static readonly TimeSpan AnswerTimeout = TimeSpan.FromSeconds(5);
    private const string Show = "show";
    private readonly Mutex mutex;
    private readonly CancellationTokenSource stop = new();

    private SingleInstance(Mutex mutex) { this.mutex = mutex; }

    public static string Sid => WindowsIdentity.GetCurrent().User!.Value;
    public static string MutexName => $@"Global\HIMARI.Workbench.{Sid}";
    public static string PipeName => $"HIMARI.Workbench.{Sid}";
    private static int SessionId => System.Diagnostics.Process.GetCurrentProcess().SessionId;

    /// <summary>The mutex if this process is the first; null if another process of this user holds it.</summary>
    public static SingleInstance? TryAcquire()
    {
        var security = new MutexSecurity();
        security.AddAccessRule(new MutexAccessRule(WindowsIdentity.GetCurrent().User!, MutexRights.FullControl, AccessControlType.Allow));
        Mutex mutex;
        try { mutex = MutexAcl.Create(false, MutexName, out _, security); }
        catch (UnauthorizedAccessException) { return null; }
        try
        {
            if (mutex.WaitOne(0)) return new SingleInstance(mutex);
        }
        catch (AbandonedMutexException) { return new SingleInstance(mutex); }
        mutex.Dispose();
        return null;
    }

    /// <summary>Listens for "show" from a second launch in this session; <paramref name="show"/> runs on a pool thread.</summary>
    public void Listen(Action show) => _ = Task.Run(async () =>
    {
        while (!stop.IsCancellationRequested)
        {
            try
            {
                using var server = new NamedPipeServerStream(PipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await server.WaitForConnectionAsync(stop.Token);
                using var talk = new CancellationTokenSource(AnswerTimeout);
                using var reader = new StreamReader(server, Encoding.UTF8, false, 256, leaveOpen: true);
                using var writer = new StreamWriter(server, new UTF8Encoding(false), 256, leaveOpen: true) { AutoFlush = true };
                await writer.WriteLineAsync($"session {SessionId}".AsMemory(), talk.Token);
                string? line = await reader.ReadLineAsync(talk.Token);
                if (line == Show) { show(); await writer.WriteLineAsync("ok".AsMemory(), talk.Token); }
            }
            catch (OperationCanceledException) when (stop.IsCancellationRequested) { return; }
            catch (Exception ex) when (ex is IOException or OperationCanceledException or UnauthorizedAccessException) { await Task.Delay(200); }
        }
    });

    /// <summary>A second launch: ask the first app to show its window, if it is in this Windows session.</summary>
    public static SecondLaunch SignalFirst()
    {
        try
        {
            using var client = new NamedPipeClientStream(".", PipeName, PipeDirection.InOut, PipeOptions.CurrentUserOnly);
            client.Connect((int)AnswerTimeout.TotalMilliseconds);
            using var talk = new CancellationTokenSource(AnswerTimeout);
            using var reader = new StreamReader(client, Encoding.UTF8, false, 256, leaveOpen: true);
            using var writer = new StreamWriter(client, new UTF8Encoding(false), 256, leaveOpen: true) { AutoFlush = true };
            string? hello = reader.ReadLineAsync(talk.Token).AsTask().GetAwaiter().GetResult();
            if (hello is null || !hello.StartsWith("session ", StringComparison.Ordinal)) return SecondLaunch.NotResponding;
            if (hello != $"session {SessionId}") { writer.WriteLine("bye"); return SecondLaunch.OtherSession; }
            writer.WriteLine(Show);
            return reader.ReadLineAsync(talk.Token).AsTask().GetAwaiter().GetResult() == "ok" ? SecondLaunch.Shown : SecondLaunch.NotResponding;
        }
        catch (Exception ex) when (ex is TimeoutException or IOException or OperationCanceledException or UnauthorizedAccessException)
        {
            return SecondLaunch.NotResponding;
        }
    }

    public void Dispose()
    {
        stop.Cancel();
        // Called on the thread that acquired it; otherwise the OS releases it when the process ends (as abandoned).
        try { mutex.ReleaseMutex(); } catch (ApplicationException) { }
        mutex.Dispose();
    }
}

/// <summary>"Windows에 로그인하면 앱 시작" (§5.5): one value under HKCU Run with the app's quoted absolute path.</summary>
public static class LoginStartup
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    public const string ValueName = "HIMARI Workbench";

    public static string Command => $"\"{Environment.ProcessPath}\"";

    public static bool IsEnabled
    {
        get
        {
            using var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKey);
            return key?.GetValue(ValueName) is string value && value.Length > 0;
        }
    }

    public static void Set(bool enabled)
    {
        using var key = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(RunKey, writable: true);
        if (enabled) key.SetValue(ValueName, Command, Microsoft.Win32.RegistryValueKind.String);
        else key.DeleteValue(ValueName, throwOnMissingValue: false);
    }
}
