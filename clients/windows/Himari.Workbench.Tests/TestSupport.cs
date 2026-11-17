using System.Diagnostics;
using System.Text.Json;
using Himari.Workbench;

namespace Himari.Workbench.Tests;

/// <summary>A temporary folder with a space and Hangul in its name (§10), removed afterwards.</summary>
public sealed class TempFolder : IDisposable
{
    public string Path { get; } = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"히마리 작업 앱 {Guid.NewGuid():N}");
    public TempFolder() => Directory.CreateDirectory(Path);
    public string Sub(string name) { string path = System.IO.Path.Combine(Path, name); Directory.CreateDirectory(path); return path; }
    public void Dispose()
    {
        for (int attempt = 0; attempt < 20; attempt++)
        {
            try { if (Directory.Exists(Path)) Directory.Delete(Path, true); return; }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { Thread.Sleep(250); }
        }
    }
}

/// <summary>Boot 7; the uptime runs with real time from a settable offset so the 5 s exit wait still ends.</summary>
public sealed class FakeSystem : ISystemInfo
{
    private readonly Stopwatch clock = Stopwatch.StartNew();
    public long BootId { get; set; } = 7;
    public long Offset { get; set; } = 1_000_000;
    public long UptimeMs => Offset + clock.ElapsedMilliseconds;
    public DateTimeOffset Now => DateTimeOffset.Now;
}

/// <summary>
/// An in-process hub that answers by the contract and writes the state file into a temporary hub home, so every
/// branch of §5.2–§5.5 can be set up exactly. It also records each call and how many overlapped.
/// </summary>
public sealed class ScriptedHub : IHubCommandRunner
{
    private readonly string home;
    private int active;
    public ScriptedHub(string home) { this.home = home; }

    public bool Running { get; set; }
    public string? Instance { get; set; }
    public bool HideInstance { get; set; }
    public string Registration { get; set; } = "ok";
    public string? Why { get; set; }
    public bool StatusFails { get; set; }
    /// <summary>The exit code of a status that still prints a complete reply (a nonzero one must not count).</summary>
    public int StatusExitCode { get; set; }
    public string StatusStdErr { get; set; } = "Traceback: boom";
    public string StatusStdOut { get; set; } = "";
    /// <summary>How long a status takes to answer, in ms.</summary>
    public int StatusDelay { get; set; }
    /// <summary>The next start's reply; null = start succeeds with the id it was given.</summary>
    public Func<string, string>? StartReply { get; set; }
    public TaskCompletionSource? StartGate { get; set; }
    public string StopResult { get; set; } = "stopped";
    public List<string[]> Calls { get; } = [];
    public int MaxConcurrent { get; private set; }

    public string[] CallsOf(string verb) => Calls.Where(call => call.Length > 1 && call[1] == verb).Select(call => string.Join(' ', call)).ToArray();

    public async Task<HubResult> RunAsync(IReadOnlyList<string> arguments, TimeSpan timeout, CancellationToken cancel = default)
    {
        int now = Interlocked.Increment(ref active);
        lock (Calls) { Calls.Add(arguments.ToArray()); MaxConcurrent = Math.Max(MaxConcurrent, now); }
        try
        {
            await Task.Delay(5);
            string verb = arguments[1];
            string? instance = arguments.Count > 3 ? arguments[3] : null;
            switch (verb)
            {
                case "status":
                    await Task.Delay(StatusDelay);
                    if (StatusFails) return new HubResult(HubOutcome.Exited, 1, StatusStdOut, StatusStdErr);
                    return Ok(StatusExitCode, new { running = Running, instance = Running && !HideInstance ? Instance : null, pid = Running ? 4242 : (int?)null,
                        started_at = Running ? "2026-11-18T00:00:00Z" : null, registration = Registration, why = Why });
                case "start":
                    if (StartGate is { } gate) await gate.Task;
                    if (StartReply is { } reply) return new HubResult(HubOutcome.Exited, 1, reply(instance!), "");
                    Running = true; Instance = instance; WriteState(instance!, "connected");
                    return Ok(new { instance, pid = 4242, started_at = "2026-11-18T00:00:00Z" });
                case "stop":
                    if (StopResult == "stopped" && Instance == instance) { Running = false; Instance = null; File.Delete(Path.Combine(home, RunnerProtocol.StateFileName)); }
                    return Ok(new { result = StopResult });
                default:
                    return new HubResult(HubOutcome.Exited, 2, "", "unknown");
            }
        }
        finally { Interlocked.Decrement(ref active); }
    }

    public void WriteState(string instance, string state, string? stopAck = null, DateTimeOffset? updated = null, string name = "laptop") =>
        File.WriteAllText(Path.Combine(home, RunnerProtocol.StateFileName), JsonSerializer.Serialize(new
        {
            instance, pid = 4242, started_at = "2026-11-18T00:00:00Z", updated_at = (updated ?? DateTimeOffset.Now).ToString("o"),
            state, stop_ack = stopAck, name, server = "example.invalid", error = (string?)null,
        }));

    private static HubResult Ok(object value) => Ok(0, value);
    private static HubResult Ok(int exitCode, object value) => new(HubOutcome.Exited, exitCode, JsonSerializer.Serialize(value) + "\n", "");
}

public static class FakeHubExe
{
    /// <summary>The built fake hub next to this test project (it is built first, see the project file).</summary>
    public static string PathName
    {
        get
        {
            var testBin = new DirectoryInfo(AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar));
            // …\Himari.Workbench.Tests\bin\<configuration>\<tfm>
            string configuration = testBin.Parent!.Name;
            string windows = testBin.Parent!.Parent!.Parent!.Parent!.FullName;
            return Path.Combine(windows, "Himari.Workbench.FakeHub", "bin", configuration, "net10.0-windows", "Himari.Workbench.FakeHub.exe");
        }
    }

    public static HubCommand Command(string workingDirectory) => new(PathName, [], workingDirectory);
}

public static class Wait
{
    public static async Task Until(Func<bool> condition, int timeoutMs = 10_000)
    {
        var watch = Stopwatch.StartNew();
        while (!condition())
        {
            if (watch.ElapsedMilliseconds > timeoutMs) throw new TimeoutException("condition not reached");
            await Task.Delay(25);
        }
    }
}
