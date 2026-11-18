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
//   stderr-flood                (over 1 MB of stderr, then a last line, for the output test)
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
// stderr-flood: over 1 MB of stderr, then one last line, exit 1 (the app must show that last line).
if (args.Length > 0 && args[0] == "stderr-flood")
{
    string filler = new('x', 199);
    for (int i = 0; i < 8000; i++) Console.Error.WriteLine(filler);
    Console.Error.WriteLine("the real failure");
    return 1;
}
if (args.Length > 0 && args[0] == "new-id") { Console.WriteLine(InstanceId.Create(system)); return 0; }
if (args.Length == 0 || args[0] != "runner") { Console.Error.WriteLine("usage: runner status|start|stop --json"); return 2; }
string verb = args.Length > 1 ? args[1] : "";
string? instanceArg = Option("--instance");
bool json = args.Contains("--json");

// The terminal forms (no --json) behave the same and print the result as text (H10, §5.4).
switch (verb)
{
    case "status" when json:
        if (config.StatusFail) { Console.Error.WriteLine("fake hub: status failed Authorization: Bearer secret-token-value"); return 1; }
        if (config.OldHub) { Console.Error.WriteLine("himari-hub runner: error: unrecognized arguments: --json"); return 2; }
        await Task.Delay(config.StatusDelayMs);
        if (Status() is not { } reply) { Console.Error.WriteLine("himari-hub: runner.transition.lock is busy"); return 1; }
        Print(reply);
        return 0;
    case "start":
        return await Start();
    case "stop" when instanceArg is not null:
        return await Stop(instanceArg);
    case "stop" when args.Contains("--legacy"):
        return await StopLegacy();
    case "stop":
        return await StopWhoever();
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

// JSON with --json; otherwise the same fields as one line of text for a person.
void Print(object value)
{
    string body = JsonSerializer.Serialize(value);
    if (json) { Console.WriteLine(body); return; }
    Console.WriteLine(string.Join(", ", JsonNode.Parse(body)!.AsObject().Select(pair => $"{pair.Key}: {pair.Value}")));
}

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

// The creation time as the runner reads it from its own handle (H9) and as status and stop compare it (H14, H15).
string? StartedAt(int pid) => Native.StartedAt(pid);

// H14: under the transition lock, the runner lock decides; the state file names the instance only when its pid's
// creation time matches started_at. Null when the transition lock stays busy: the status is a failure then.
object? Status()
{
    var (registration, why) = Registration();
    using var transition = Transition();
    if (transition is null) return null;
    using (var probe = TryLock(LockPath(), TimeSpan.Zero))
        if (probe is not null) return new { running = false, instance = (string?)null, pid = (int?)null, started_at = (string?)null, registration, why };
    var read = RunnerProtocol.ReadStateFile(home);
    string? instance = null; int? pid = null; string? started = null;
    if (read.File is { Pid: { } p } state && StartedAt(p) is { } created && created == state.StartedAt)
    { instance = state.Instance; pid = p; started = state.StartedAt; }
    if (config.InstanceNull) { instance = null; }
    return new { running = true, instance, pid, started_at = started, registration, why };
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
    // H10: the child and everything under it can be ended and confirmed gone through this Job (see the cancel below).
    // A child that already ended cannot join; it then has nothing left to end and the results below still follow.
    System.ComponentModel.Win32Exception? notTracked = null;
    Microsoft.Win32.SafeHandles.SafeFileHandle? tracking = null;
    try { tracking = Native.TrackingJob(child.SafeHandle); }
    catch (System.ComponentModel.Win32Exception ex) { notTracked = ex; }
    using var trackingHandle = tracking;
    var until = DateTime.UtcNow + TimeSpan.FromMilliseconds(config.StartConfirmMs);
    while (DateTime.UtcNow < until)
    {
        // H10: success only once H14 confirms it: the runner lock is held and the state file of that holder names this id.
        if (RunnerProtocol.ReadStateFile(home).File?.Instance == id && Status() is { } confirmed
            && JsonNode.Parse(JsonSerializer.Serialize(confirmed)) is { } seen && (string?)seen["instance"] == id)
        {
            await Task.Delay(config.StartHangMs); // a start command that hangs after its runner is up (timeout tests)
            Print(new { instance = id, pid = (int?)seen["pid"], started_at = (string?)seen["started_at"] });
            return 0;
        }
        if (child.HasExited)
        {
            // H10: "running" only when H14 confirms another runner is up (its instance, or null when unknown);
            // otherwise the child simply failed.
            JsonNode? other = child.ExitCode == 3 && Status() is { } now ? JsonNode.Parse(JsonSerializer.Serialize(now)) : null;
            if (other is not null && (bool?)other["running"] == true) Print(new { error = "running", instance = (string?)other["instance"] });
            else Print(new { error = "failed", exit_code = child.ExitCode });
            return 1;
        }
        await Task.Delay(50);
    }
    // "cancelled" only once the child and all its descendants are seen gone; otherwise "unconfirmed".
    bool gone = tracking is not null ? Native.TerminateAllAndWait(tracking, 5000) : child.HasExited && notTracked is not null;
    Print(gone ? new { error = "cancelled" } : (object)new { error = "unconfirmed" });
    return 1;
}

async Task<int> Stop(string id)
{
    if (!InstanceId.TryParse(id, out _)) { Console.Error.WriteLine("himari-hub: bad --instance"); return 2; }
    // §5.4: always one result; a target that cannot be checked now is "unconfirmed".
    if (Status() is not { } now) { Console.Error.WriteLine("himari-hub: runner.transition.lock is busy"); Print(new { result = "unconfirmed" }); return 1; }
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
        // H14 each time: off → stopped; a confirmed other instance → the target ended and another took over.
        if (Status() is { } seen && JsonNode.Parse(JsonSerializer.Serialize(seen)) is { } after)
        {
            if ((bool?)after["running"] == false) { Print(new { result = "stopped" }); return 0; }
            if ((string?)after["instance"] is { } other && other != id) { Print(new { result = "other_instance" }); return 1; }
        }
        await Task.Delay(100);
    }
    // H15: the forced stop happens under the transition lock, so no other runner can take the runner lock or publish
    // its state in between; the state file is removed only while it still names the killed instance.
    using var transition = Transition();
    if (transition is null) { Print(new { result = "unconfirmed" }); return 0; }
    using (var probe = TryLock(LockPath(), TimeSpan.Zero)) if (probe is not null) { Print(new { result = "stopped" }); return 0; }
    // §5.4: another instance in the state file → other_instance (the target ended); no state, no process to open or
    // another creation time → the target cannot be confirmed: unconfirmed, nothing is killed.
    var read = RunnerProtocol.ReadStateFile(home);
    if (read.File is not { Instance: { } shown } state) { Print(new { result = "unconfirmed" }); return 0; }
    if (shown != id) { Print(new { result = "other_instance" }); return 0; }
    // One handle: its creation time is checked and the process is ended through it, so a reused pid is never hit.
    // The runner's Job (KILL_ON_JOB_CLOSE) ends everything under it when the runner's handle to it closes.
    using (var target = state.Pid is { } targetPid ? Native.Open(targetPid) : null)
    {
        if (target is null || Native.StartedAt(target) != state.StartedAt) { Print(new { result = "unconfirmed" }); return 0; }
        if (!Native.TerminateAndWait(target, 5000)) { Print(new { result = "unconfirmed" }); return 0; }
    }
    // The OS releases the dead runner's lock; "killed" only once that is seen.
    using (var released = TryLock(LockPath(), TimeSpan.FromSeconds(5)))
    {
        if (released is null) { Print(new { result = "unconfirmed" }); return 0; }
        if (RunnerProtocol.ReadStateFile(home).File?.Instance == id) try { File.Delete(StatePath()); } catch (IOException) { }
    }
    Print(new { result = "killed" });
    return 0;
}

// §5.4, a stop without a target: find the running instance by H14, asking again every 1 s for 10 s while it is unknown.
async Task<int> StopWhoever()
{
    for (int attempt = 0; attempt < 10; attempt++)
    {
        if (Status() is { } now)
        {
            var node = JsonNode.Parse(JsonSerializer.Serialize(now))!;
            if (!node["running"]!.GetValue<bool>()) { Print(new { result = "not_running" }); return 0; }
            if (node["instance"]?.GetValue<string?>() is { } running) return await Stop(running);
        }
        await Task.Delay(1000);
    }
    Print(new { result = "unconfirmed" });
    return 0;
}

// §5.4 --legacy: only the old runner.stop file. New runners never follow it, so this never stops one of them.
async Task<int> StopLegacy()
{
    File.WriteAllText(Path.Combine(home, "runner.stop"), "");
    var until = DateTime.UtcNow + TimeSpan.FromMilliseconds(config.StopTimeoutMs);
    while (DateTime.UtcNow < until)
    {
        if (RunnerLockFree() is true) { Print(new { result = "stopped" }); return 0; }
        await Task.Delay(100);
    }
    Print(new { result = "unconfirmed" });
    return 0;
}

// H14 registration, from the hub home's files as the hub reads them: runner.json with a server, and a non-empty
// runner.token. fake-hub.json may override it ("registration", "why") to try the wizard's branches.
(string, string?) Registration()
{
    if (config.Registration is { } forced) return (forced, config.Why);
    string settings = Path.Combine(home, "runner.json");
    if (!File.Exists(settings)) return ("missing", "이 기기는 실행기로 등록되지 않았습니다 (himari-hub runner setup)");
    try
    {
        using var json = JsonDocument.Parse(File.ReadAllText(settings));
        if (!json.RootElement.TryGetProperty("server", out var server) || server.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(server.GetString()))
            return ("broken", "runner.json에 서버 주소가 없습니다");
    }
    catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException) { return ("broken", "runner.json을 읽지 못했습니다"); }
    string token = Path.Combine(home, "runner.token");
    if (!File.Exists(token)) return ("broken", "토큰 파일(runner.token)이 없습니다");
    // Never its contents (it may be a real hub home): only that it is not empty and can be opened for reading.
    try
    {
        if (new FileInfo(token).Length == 0) return ("broken", "토큰 파일이 비었습니다");
        using (new FileStream(token, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)) { }
    }
    catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { return ("broken", "토큰 파일(runner.token)을 읽지 못했습니다"); }
    return ("ok", null);
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
    // H15: into its own Job before anything else; a runner that cannot do that does not start (and writes no state).
    System.ComponentModel.Win32Exception? jobFailure = null;
    try { job = Native.JoinNewKillOnCloseJob(); }
    catch (System.ComponentModel.Win32Exception ex) { jobFailure = ex; }
    if (jobFailure is not null) { Console.Error.WriteLine($"himari-hub runner: could not join a Job: {jobFailure.Message}"); return 6; }
    startedAt = StartedAt(Environment.ProcessId);
    // H14 ⓐ / H16: each attempt takes runner.lock under the transition lock, and the transition lock is let go between
    // attempts (for 2 s), so a runner that is just ending can take it to remove its state and release runner.lock.
    FileStream? runnerLock = null;
    var lockUntil = DateTime.UtcNow + TimeSpan.FromSeconds(2);
    while (true)
    {
        using (var transition = Transition())
        {
            if (transition is null) return 5;
            runnerLock = TryLock(LockPath(), TimeSpan.Zero);
            if (runnerLock is not null)
            {
                if (!InstanceId.TryParse(id, out var parsed) || parsed.CannotStart(system)) { runnerLock.Dispose(); return 4; }
                WriteState(id, "connected", null);
                break;
            }
        }
        if (DateTime.UtcNow >= lockUntil) return 3;
        await Task.Delay(50);
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
            // H9: updated_at every 5 s also while finishing its work.
            var workUntil = DateTime.UtcNow.AddMilliseconds(config.StopWorkMs);
            while (DateTime.UtcNow < workUntil)
            {
                await Task.Delay(TimeSpan.FromMilliseconds(Math.Min(5000, Math.Max(0, (workUntil - DateTime.UtcNow).TotalMilliseconds))));
                if (DateTime.UtcNow < workUntil) WriteState(id, "stopping", ack);
            }
            break;
        }
        // Each heartbeat also drops requests that have become malformed or can no longer apply (§5.4, H15).
        if (DateTime.UtcNow >= nextBeat) { WriteState(id, "connected", ack); CleanStopFolder(id); nextBeat = DateTime.UtcNow.AddSeconds(5); }
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
    var value = new
    {
        instance = id, pid = Environment.ProcessId, started_at = startedAt, updated_at = DateTimeOffset.Now.ToString("o"),
        state, stop_ack = stopAck, name = config.Name, server = "example.invalid", error = (string?)null,
    };
    string temp = StatePath() + ".tmp";
    File.WriteAllText(temp, JsonSerializer.Serialize(value));
    File.Move(temp, StatePath(), overwrite: true);
}

// H15: "<instance id>.<random>", the random part 1-64 of [0-9A-Za-z_-] (as the hub reads it); anything else is malformed.
string? RequestTarget(string name)
{
    int last = name.LastIndexOf('.');
    if (last <= 0 || last == name.Length - 1 || name.Length - last - 1 > 64) return null;
    if (!name[(last + 1)..].All(c => char.IsAsciiLetterOrDigit(c) || c is '_' or '-')) return null;
    return InstanceId.TryParse(name[..last], out _) ? name[..last] : null;
}

string? OwnRequest(string id)
{
    if (!Directory.Exists(StopFolder())) return null;
    return Directory.EnumerateFiles(StopFolder()).Select(path => Path.GetFileName(path)).FirstOrDefault(name => RequestTarget(name) == id);
}

// H15: drop requests whose target can never come up, and malformed ones; keep requests for ids still before their deadline.
void CleanStopFolder(string own)
{
    if (!Directory.Exists(StopFolder())) return;
    foreach (string path in Directory.EnumerateFiles(StopFolder()))
    {
        string name = Path.GetFileName(path);
        bool keep = RequestTarget(name) is { } target && InstanceId.TryParse(target, out var parsed) && (target == own || !parsed.CannotStart(system));
        if (!keep) try { File.Delete(path); } catch (IOException) { }
    }
}

partial class Program
{
    // The runner's Job handle, held for its whole life (H15), and its creation time read once from its own handle (H9).
    internal static Microsoft.Win32.SafeHandles.SafeFileHandle? job;
    internal static string? startedAt;
}

sealed record FakeConfig
{
    public string? Registration { get; init; }
    public string? Why { get; init; }
    public bool StatusFail { get; init; }
    public bool OldHub { get; init; }
    public bool InstanceNull { get; init; }
    public int StatusDelayMs { get; init; }
    public int StartConfirmMs { get; init; } = 15000;
    public int StartDelayMs { get; init; }
    public int StartHangMs { get; init; }
    public string? StartReply { get; init; }
    public int RunnerDelayMs { get; init; }
    // §5.4: RUNNER_STOP (90 s) + 15 s before the forced stop.
    public int StopTimeoutMs { get; init; } = 105_000;
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
