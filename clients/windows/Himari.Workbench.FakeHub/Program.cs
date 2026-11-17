// A stand-in for `himari-hub` that speaks the workbench contract (docs/workbench-v0.1.md §5.3, §5.4, §9: H9, H10,
// H14, H15, H16) until the real hub has it. Used by the tests and to run the app end to end. It is not the hub:
// no server, no agents, no tokens. Knobs live in <hub home>\fake-hub.json.
//
//   runner status --json
//   runner start --instance <id> --json
//   runner stop --instance <id> --json
//   runner                      (the runner itself; the instance id comes from HIMARI_RUNNER_INSTANCE)
//   echo-args                   (prints its arguments, folder and hub home as JSON, for the no-shell test)
//   new-id                      (prints a fresh instance id, for trying it by hand)
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Himari.Workbench;

string home = Environment.GetEnvironmentVariable("HIMARI_HUB_HOME") is { Length: > 0 } h ? h
    : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".himari-hub");
Directory.CreateDirectory(home);
var config = FakeConfig.Load(home);
var system = new WindowsSystemInfo();

if (args.Length > 0 && args[0] == "echo-args")
{
    Console.WriteLine(JsonSerializer.Serialize(new { args = args[1..], cwd = Environment.CurrentDirectory, home }));
    return 0;
}
if (args.Length > 0 && args[0] == "new-id") { Console.WriteLine(InstanceId.Create(system)); return 0; }
if (args.Length == 0 || args[0] != "runner") { Console.Error.WriteLine("usage: runner status|start|stop --json"); return 2; }
string verb = args.Length > 1 ? args[1] : "";
string? instanceArg = Option("--instance");
bool json = args.Contains("--json");

switch (verb)
{
    case "status" when json:
        if (config.StatusFail) { Console.Error.WriteLine("fake hub: status failed Authorization: Bearer secret-token-value"); return 1; }
        if (config.OldHub) { Console.Error.WriteLine("himari-hub runner: error: unrecognized arguments: --json"); return 2; }
        await Task.Delay(config.StatusDelayMs);
        if (Status() is not { } reply) { Console.Error.WriteLine("himari-hub: runner.transition.lock is busy"); return 1; }
        Print(reply);
        return 0;
    case "start" when json:
        return await Start();
    case "stop" when json && instanceArg is not null:
        return await Stop(instanceArg);
    case "" or "run":
        return await RunRunner();
    default:
        Console.Error.WriteLine($"himari-hub runner: error: unrecognized arguments: {string.Join(' ', args[1..])}");
        return 2;
}

string? Option(string name)
{
    int at = Array.IndexOf(args, name);
    return at >= 0 && at + 1 < args.Length ? args[at + 1] : null;
}

void Print(object value) => Console.WriteLine(JsonSerializer.Serialize(value));

string LockPath() => Path.Combine(home, "runner.lock");
string StatePath() => Path.Combine(home, RunnerProtocol.StateFileName);
string StopFolder() => Path.Combine(home, "runner.stop.d");

FileStream? TryLock(string path, TimeSpan wait)
{
    var until = DateTime.UtcNow + wait;
    while (true)
    {
        try { return new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
        catch (IOException) { if (DateTime.UtcNow >= until) return null; Thread.Sleep(50); }
    }
}

// H14: a blocking lock, waited for up to 2 s. Who cannot get it does not go on without it.
FileStream? Transition() => TryLock(Path.Combine(home, "runner.transition.lock"), TimeSpan.FromSeconds(2));

string StartedAt(Process process) => process.StartTime.ToUniversalTime().ToString("o");

// H14: under the transition lock, the runner lock decides; the state file names the instance only when its pid's
// creation time matches started_at. Null when the transition lock stays busy: the status is a failure then.
object? Status()
{
    using var transition = Transition();
    if (transition is null) return null;
    using (var probe = TryLock(LockPath(), TimeSpan.Zero))
        if (probe is not null) return new { running = false, instance = (string?)null, pid = (int?)null, started_at = (string?)null, registration = config.Registration, why = config.Why };
    var read = RunnerProtocol.ReadStateFile(home);
    string? instance = null; int? pid = null; string? started = null;
    if (read.File is { Pid: { } p } state)
    {
        try
        {
            using var process = Process.GetProcessById(p);
            if (StartedAt(process) == state.StartedAt) { instance = state.Instance; pid = p; started = state.StartedAt; }
        }
        catch (ArgumentException) { }
    }
    if (config.InstanceNull) { instance = null; }
    return new { running = true, instance, pid, started_at = started, registration = config.Registration, why = config.Why };
}

async Task<int> Start()
{
    string id = instanceArg ?? InstanceId.Create(system).ToString();
    if (!InstanceId.TryParse(id, out _)) { Console.Error.WriteLine("himari-hub: bad --instance"); Print(new { error = "failed", exit_code = 2 }); return 1; }
    if (config.StartReply is { } canned) { await Task.Delay(config.StartDelayMs); Console.WriteLine(canned); return 0; }
    var info = new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false, CreateNoWindow = true };
    info.ArgumentList.Add("runner");
    info.Environment["HIMARI_RUNNER_INSTANCE"] = id;
    info.Environment["HIMARI_HUB_HOME"] = home;
    using var child = Process.Start(info)!;
    var until = DateTime.UtcNow + TimeSpan.FromMilliseconds(config.StartConfirmMs);
    while (DateTime.UtcNow < until)
    {
        var read = RunnerProtocol.ReadStateFile(home);
        if (read.File is { } state && state.Instance == id) { Print(new { instance = id, pid = state.Pid, started_at = state.StartedAt }); return 0; }
        if (child.HasExited)
        {
            if (child.ExitCode == 3)
            {
                string? other = Status() is { } now ? JsonNode.Parse(JsonSerializer.Serialize(now))?["instance"]?.GetValue<string?>() : null;
                Print(new { error = "running", instance = other });
            }
            else Print(new { error = "failed", exit_code = child.ExitCode });
            return 1;
        }
        await Task.Delay(50);
    }
    try { child.Kill(entireProcessTree: true); child.WaitForExit(5000); } catch (InvalidOperationException) { }
    Print(child.HasExited ? new { error = "cancelled" } : (object)new { error = "unconfirmed" });
    return 1;
}

async Task<int> Stop(string id)
{
    if (!InstanceId.TryParse(id, out _)) { Console.Error.WriteLine("himari-hub: bad --instance"); return 2; }
    if (Status() is not { } now) { Console.Error.WriteLine("himari-hub: runner.transition.lock is busy"); return 1; }
    var node = JsonNode.Parse(JsonSerializer.Serialize(now))!;
    if (!node["running"]!.GetValue<bool>()) { Print(new { result = "not_running" }); return 0; }
    string? running = node["instance"]?.GetValue<string?>();
    if (running is not null && running != id) { Print(new { result = "other_instance" }); return 0; }
    Directory.CreateDirectory(StopFolder());
    string name = $"{id}.{Guid.NewGuid():N}";
    using (new FileStream(Path.Combine(StopFolder(), name), FileMode.CreateNew)) { }
    var until = DateTime.UtcNow + TimeSpan.FromMilliseconds(config.StopTimeoutMs);
    while (DateTime.UtcNow < until)
    {
        if (RunnerLockFree() is true) { Print(new { result = "stopped" }); return 0; }
        await Task.Delay(100);
    }
    // H15: the forced stop happens under the transition lock, so no other runner can take the runner lock or publish
    // its state in between; the state file is removed only while it still names the killed instance.
    using var transition = Transition();
    if (transition is null) { Print(new { result = "unconfirmed" }); return 0; }
    using (var probe = TryLock(LockPath(), TimeSpan.Zero)) if (probe is not null) { Print(new { result = "stopped" }); return 0; }
    var read = RunnerProtocol.ReadStateFile(home);
    if (read.File is not { } state || state.Instance != id) { Print(new { result = "other_instance" }); return 0; }
    bool gone = false;
    try
    {
        using var process = Process.GetProcessById(state.Pid ?? -1);
        if (StartedAt(process) != state.StartedAt) { Print(new { result = "other_instance" }); return 0; }
        process.Kill(entireProcessTree: true);
        if (!process.WaitForExit(5000)) { Print(new { result = "unconfirmed" }); return 0; }
    }
    catch (Exception ex) when (ex is ArgumentException or InvalidOperationException) { gone = true; }
    // The OS releases the dead runner's lock; "killed" only once that is seen.
    using (var released = TryLock(LockPath(), TimeSpan.FromSeconds(5)))
    {
        if (released is null) { Print(new { result = "unconfirmed" }); return 0; }
        if (RunnerProtocol.ReadStateFile(home).File?.Instance == id) try { File.Delete(StatePath()); } catch (IOException) { }
    }
    Print(new { result = gone ? "stopped" : "killed" });
    return 0;
}

// The runner lock probed under the transition lock (null: the transition lock stayed busy).
bool? RunnerLockFree()
{
    using var transition = Transition();
    if (transition is null) return null;
    using var probe = TryLock(LockPath(), TimeSpan.Zero);
    return probe is not null;
}

// The runner (H9, H10, H15, H16): lock, check the deadline, publish the state, follow its own stop requests.
async Task<int> RunRunner()
{
    await Task.Delay(config.RunnerDelayMs);
    string id = Environment.GetEnvironmentVariable("HIMARI_RUNNER_INSTANCE") is { Length: > 0 } given ? given : InstanceId.Create(system).ToString();
    FileStream? runnerLock;
    using (var transition = Transition())
    {
        if (transition is null) return 5;
        runnerLock = TryLock(LockPath(), TimeSpan.FromSeconds(2));
        if (runnerLock is null) return 3;
        if (!InstanceId.TryParse(id, out var parsed) || parsed.CannotStart(system)) { runnerLock.Dispose(); return 4; }
        WriteState(id, "connected", null);
    }
    try { File.Delete(Path.Combine(home, "runner.stop")); } catch (IOException) { }
    CleanStopFolder(id);
    string? ack = null;
    var nextBeat = DateTime.UtcNow.AddSeconds(5);
    while (true)
    {
        string? request = OwnRequest(id);
        if (request is not null && !config.IgnoreStop)
        {
            ack = request;
            WriteState(id, "stopping", ack);
            try { File.Delete(Path.Combine(StopFolder(), request)); } catch (IOException) { }
            await Task.Delay(config.StopWorkMs);
            break;
        }
        if (DateTime.UtcNow >= nextBeat) { WriteState(id, "connected", ack); nextBeat = DateTime.UtcNow.AddSeconds(5); }
        await Task.Delay(100);
    }
    // H14 ⓑ: remove the state and release the runner lock only while holding the transition lock (waits as long as it takes).
    FileStream? leaving;
    while ((leaving = Transition()) is null) { }
    using (leaving)
    {
        try { File.Delete(StatePath()); } catch (IOException) { }
        runnerLock.Dispose();
    }
    return 0;
}

void WriteState(string id, string state, string? stopAck)
{
    using var self = Process.GetCurrentProcess();
    var value = new
    {
        instance = id, pid = Environment.ProcessId, started_at = StartedAt(self), updated_at = DateTimeOffset.Now.ToString("o"),
        state, stop_ack = stopAck, name = config.Name, server = "example.invalid", error = (string?)null,
    };
    string temp = StatePath() + ".tmp";
    File.WriteAllText(temp, JsonSerializer.Serialize(value));
    File.Move(temp, StatePath(), overwrite: true);
}

string? OwnRequest(string id)
{
    if (!Directory.Exists(StopFolder())) return null;
    return Directory.EnumerateFiles(StopFolder()).Select(Path.GetFileName).FirstOrDefault(name => name!.StartsWith(id + ".", StringComparison.Ordinal));
}

// H15: drop requests whose target can never come up, and malformed ones; keep requests for ids still before their deadline.
void CleanStopFolder(string own)
{
    if (!Directory.Exists(StopFolder())) return;
    foreach (string path in Directory.EnumerateFiles(StopFolder()))
    {
        string name = Path.GetFileName(path);
        int last = name.LastIndexOf('.');
        bool keep = last > 0 && InstanceId.TryParse(name[..last], out var target) && (name[..last] == own || !target.CannotStart(system));
        if (!keep) try { File.Delete(path); } catch (IOException) { }
    }
}

sealed record FakeConfig
{
    public string Registration { get; init; } = "ok";
    public string? Why { get; init; }
    public bool StatusFail { get; init; }
    public bool OldHub { get; init; }
    public bool InstanceNull { get; init; }
    public int StatusDelayMs { get; init; }
    public int StartConfirmMs { get; init; } = 15000;
    public int StartDelayMs { get; init; }
    public string? StartReply { get; init; }
    public int RunnerDelayMs { get; init; }
    public int StopTimeoutMs { get; init; } = 20000;
    public int StopWorkMs { get; init; } = 500;
    public bool IgnoreStop { get; init; }
    public string Name { get; init; } = "laptop";

    public static FakeConfig Load(string home)
    {
        try
        {
            return JsonSerializer.Deserialize<FakeConfig>(File.ReadAllText(Path.Combine(home, "fake-hub.json")),
                new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower }) ?? new FakeConfig();
        }
        catch (Exception ex) when (ex is IOException or JsonException) { return new FakeConfig(); }
    }
}
