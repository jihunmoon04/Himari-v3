using System.IO;

namespace Himari.Workbench;

public enum RunnerPhase { Checking, NotConfigured, Off, Starting, AwaitingConfirm, Running, Stopping, Unverifiable, Error, Exiting }

public enum RunnerConnection { None, Connected, Disconnected, Unknown, UnknownIdentity, StopReceived, Starting }

/// <summary>What the status bar and the tray show (§5.2, §5.7). Built only from the last <c>status</c> and the state file.</summary>
public sealed record RunnerView(RunnerPhase Phase, RunnerConnection Connection = RunnerConnection.None, string? Instance = null, int? Pid = null,
    string? Name = null, bool Owned = false, string? Detail = null, bool ExitStopPending = false, Registration Registration = Registration.Unknown,
    string? RegistrationWhy = null)
{
    public bool CanStart => Phase is RunnerPhase.Off or RunnerPhase.Error && Registration is not (Registration.Missing or Registration.Broken);
    public bool CanStop => Phase is RunnerPhase.Running && Instance is not null;
    public bool IsProblem => Phase is RunnerPhase.Unverifiable or RunnerPhase.Error;

    public string Text => Phase switch
    {
        RunnerPhase.Checking => "실행기: 확인 중…",
        RunnerPhase.NotConfigured => "실행기: hub 명령이 설정되지 않음",
        RunnerPhase.Off => "실행기: 꺼짐" + Registration switch
        {
            Registration.Missing => " · 이 기기는 아직 실행기로 등록되지 않음(터미널에서 himari-hub runner setup)",
            Registration.Broken => $" · 실행기 등록에 문제가 있음({RegistrationWhy ?? "이유 모름"})",
            _ => "",
        },
        RunnerPhase.Starting => "실행기: 켜는 중…",
        RunnerPhase.AwaitingConfirm => "실행기: 켜는 중(확인 대기)",
        RunnerPhase.Stopping => "실행기: 끄는 중(작업 정리, 최대 1분 45초)",
        RunnerPhase.Unverifiable => $"실행기: 확인 불가 · {Detail}",
        RunnerPhase.Error => $"실행기: 오류 · {Detail}",
        RunnerPhase.Exiting => "실행기: 앱을 끝내는 중",
        _ => "실행기: " + string.Join(" · ", RunningParts()),
    };

    /// <summary>Shorter text for the tray tooltip (at most 127 characters).</summary>
    public string Tooltip
    {
        get
        {
            string text = "HIMARI 작업 앱 · " + Text;
            return text.Length <= 127 ? text : text[..126] + "…";
        }
    }

    private IEnumerable<string> RunningParts()
    {
        yield return Connection switch
        {
            RunnerConnection.Connected => "연결됨",
            RunnerConnection.Disconnected => "연결 안 됨",
            RunnerConnection.UnknownIdentity => "떠 있음 · 누구인지 모름",
            RunnerConnection.StopReceived => "끄기 요청을 받음",
            RunnerConnection.Starting => "시작하는 중",
            _ => "떠 있음 · 연결 모름",
        };
        if (!string.IsNullOrWhiteSpace(Name)) yield return Name;
        if (Instance is not null) yield return Owned ? "앱이 켠 것" : "다른 곳에서 켠 것";
        if (ExitStopPending) yield return "끄기를 요청했지만 아직 떠 있음";
    }
}

/// <summary>The runner a person is asked to confirm before stopping it (§5.4).</summary>
public sealed record StopTarget(string Instance, bool Owned, int? Pid, string? Name);

/// <summary>
/// Keeps one hub runner (one hub home) for the app (§5): reads its status, starts and stops it one operation at a time,
/// and remembers which instance the app started. It has no UI dependency so it can move into a merged app (§8).
/// </summary>
public sealed class RunnerSupervisor
{
    public static readonly TimeSpan StatusTimeout = TimeSpan.FromSeconds(60);
    public static readonly TimeSpan StartTimeout = TimeSpan.FromSeconds(90);
    // The hub waits RUNNER_STOP (90 s) plus 15 s before killing (§5.4); this is that with room for the launcher.
    public static readonly TimeSpan StopTimeout = TimeSpan.FromSeconds(150);
    public static readonly TimeSpan ExitAckWait = TimeSpan.FromSeconds(5);
    private const long RecheckEveryMs = 15_000;

    private readonly SettingsStore store;
    private readonly Func<WorkbenchSettings, IHubCommandRunner?> hubFor;
    private readonly ISystemInfo system;
    private readonly WorkbenchLog log;
    private readonly SemaphoreSlim queue = new(1, 1);
    private readonly object gate = new();
    private volatile bool exiting;
    private int exitRequested;

    private StatusReply? status;
    private string? statusFailure;
    private string? operationError;
    private RunnerPhase? operation;
    private StateFileRead file = new(StateFileKind.Missing, null);
    private bool autoStart;
    private string? recheckKey;
    private long lastCheckMs = long.MinValue;
    private bool checkQueued;

    public RunnerSupervisor(SettingsStore store, Func<WorkbenchSettings, IHubCommandRunner?> hubFor, ISystemInfo system, WorkbenchLog log)
    {
        this.store = store; this.hubFor = hubFor; this.system = system; this.log = log;
        View = new RunnerView(RunnerPhase.Checking);
    }

    public event Action<RunnerView>? Changed;
    public RunnerView View { get; private set; }
    public bool Exiting => exiting;
    public StatusReply? LastStatus { get { lock (gate) return status; } }
    private string Home => store.Current.EffectiveHubHome;

    /// <summary>
    /// True while the app owns, or may own, a runner: the hub home and command must not change then (§5.1), so that
    /// the app never loses track of a runner in another home.
    /// </summary>
    public bool HoldsRunner { get { var s = store.Current; return s.OwnedInstance is not null || s.PendingInstance is not null; } }

    /// <summary>At app start (§5.3): settle the pending and owned instances, then start the runner if linked and off.</summary>
    public Task InitializeAsync()
    {
        autoStart = store.Current.Linked;
        return RefreshAsync();
    }

    /// <summary>Asks the hub (<c>runner status</c>) and applies the ownership rules; may start the runner at app start.</summary>
    public Task RefreshAsync() => Queue(async () => { await CheckCore(); await AutoStartCore(); return true; }, false);

    /// <summary>The person pressed "켜기". Returns null, or why it did not start.</summary>
    public Task<string?> StartAsync() => Queue<string?>(async () =>
    {
        autoStart = false;
        await CheckCore();
        StatusReply? reply; lock (gate) reply = status;
        if (reply is null) return "실행기 상태를 확인하지 못해 켜지 않았습니다";
        if (store.Current.PendingInstance is not null) return "앞서 켠 실행기를 확인하는 중입니다. 잠시 뒤 다시 하세요";
        if (reply.Running) return "실행기가 이미 떠 있습니다";
        if (RegistrationProblem(reply) is { } problem) return problem;
        return await StartCore();
    }, "앱을 끝내는 중입니다");

    /// <summary>Checks the status and returns the runner to stop, for the person to confirm (§5.4).</summary>
    public Task<(StopTarget? Target, string? Problem)> GetStopTargetAsync() => Queue<(StopTarget?, string?)>(async () =>
    {
        await CheckCore();
        StatusReply? reply; RunnerStateFile? state; lock (gate) { reply = status; state = file.File; }
        if (reply is null) return (null, "실행기 상태를 확인하지 못했습니다");
        if (!reply.Running) return (null, "실행기가 꺼져 있습니다");
        if (reply.Instance is null) return (null, "떠 있는 실행기가 누구인지 확인하지 못해 끄지 않습니다");
        string? name = state?.Instance == reply.Instance ? state.Name : null;
        return (new StopTarget(reply.Instance, reply.Instance == store.Current.OwnedInstance, reply.Pid, name), null);
    }, (null, "앱을 끝내는 중입니다"));

    /// <summary>Stops exactly this instance (H15). <see cref="StopResult.OtherInstance"/> means: show the new target and ask again.</summary>
    public Task<(StopResult Result, string? Message)> StopAsync(string instance) => Queue<(StopResult, string?)>(async () =>
    {
        autoStart = false;
        if (hubFor(store.Current) is not { } hub) return (StopResult.Unreadable, "hub 명령이 설정되지 않았습니다");
        SetOperation(RunnerPhase.Stopping);
        log.Write($"stop {instance}");
        HubResult result;
        try { result = await hub.RunAsync(["runner", "stop", "--instance", instance, "--json"], StopTimeout); }
        finally { SetOperation(null); }
        StopResult outcome = result.Outcome == HubOutcome.Exited ? RunnerProtocol.ParseStop(result.StdOut) : StopResult.Unreadable;
        log.Write($"stop {instance}: {outcome} (exit {result.ExitCode?.ToString() ?? result.Outcome.ToString()})");
        switch (outcome)
        {
            case StopResult.Stopped or StopResult.Killed or StopResult.NotRunning or StopResult.RunnerKilled:
                store.Update(s => s.OwnedInstance == instance ? s with { OwnedInstance = null } : s);
                if (!exiting) await CheckCore(); else Publish();
                return (outcome, outcome == StopResult.Killed ? "실행기가 제시간에 끝나지 않아 강제로 껐습니다" : null);
            case StopResult.OtherInstance:
                if (!exiting) await CheckCore();
                return (outcome, "그 사이 다른 실행기로 바뀌어 끄지 않았습니다");
            case StopResult.Unconfirmed:
                SetError("끄기를 요청했지만 확인하지 못했습니다");
                return (outcome, "끄기를 요청했지만 확인하지 못했습니다");
            default:
                string line = result.Outcome == HubOutcome.Exited && Redaction.LastLine(result.StdErr).Length == 0 ? "hub가 끄기 결과를 알려 주지 않았습니다" : result.Describe();
                log.Write($"stop failed: {line}");
                SetError(line);
                return (outcome, line);
        }
    }, (StopResult.Unreadable, "앱을 끝내는 중입니다"));

    /// <summary>
    /// The app's settings changed (hub home or command, linkage). Forget the last status and ask again.
    /// </summary>
    public Task SettingsChangedAsync()
    {
        lock (gate) { status = null; statusFailure = null; operationError = null; file = new(StateFileKind.Missing, null); recheckKey = null; }
        Publish();
        return RefreshAsync();
    }

    /// <summary>
    /// Every 5 s (2 s with the window shown): read the state file only (§5.2). When it disagrees with the last status,
    /// ask the hub once more; while the status is unclear, ask again every 15 s.
    /// </summary>
    public void PollStateFile()
    {
        if (exiting) return;
        StateFileRead read = RunnerProtocol.ReadStateFile(Home);
        bool recheck = false;
        lock (gate)
        {
            file = read;
            if (operation is null && !checkQueued)
            {
                string? key = DisagreementKey();
                if (key is null) recheckKey = null;
                else if (key != recheckKey) { recheckKey = key; recheck = true; }
                if (!recheck && Unsettled() && system.UptimeMs - lastCheckMs >= RecheckEveryMs) recheck = true;
                if (recheck) checkQueued = true;
            }
        }
        Publish();
        if (recheck) _ = RefreshAsync();
    }

    /// <summary>
    /// Tray exit and Windows logoff or shutdown (§5.3, §5.5), outside the operation queue: no hub command is run. A new
    /// stop request file goes to the owned instance and to the pending one, then this waits up to <paramref name="ackWait"/>
    /// for the runner to acknowledge. A start still running finishes on its own and only records its result.
    /// </summary>
    public ExitStopOutcome RequestStopForExit(TimeSpan ackWait)
    {
        exiting = true;
        if (Interlocked.Exchange(ref exitRequested, 1) == 1) return new ExitStopOutcome(0, false);
        Publish();
        string? owned = null, pending = null; bool linked = false; string home = "";
        // Read under the settings lock: a start either sees "exiting" and stops before writing its pending id,
        // or has written it already and gets a stop request here.
        store.TryUpdate(s => { owned = s.OwnedInstance; pending = s.PendingInstance; linked = s.Linked; home = s.EffectiveHubHome; return s; });
        if (!linked) { log.Write("exit: not linked, the runner is left running"); return new ExitStopOutcome(0, false); }
        var requests = new Dictionary<string, string>();
        foreach (string target in new[] { owned, pending }.OfType<string>().Distinct())
        {
            try { requests[target] = StopRequests.Write(home, target); log.Write($"exit: stop request {requests[target]}"); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException) { log.Write($"exit: stop request for {target} failed: {ex.Message}"); }
        }
        if (owned is not null && requests.ContainsKey(owned))
        {
            try { store.Update(s => s with { ExitStopRequested = owned }); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
        }
        if (requests.Count == 0) return new ExitStopOutcome(0, false);
        long until = system.UptimeMs + (long)ackWait.TotalMilliseconds;
        while (true)
        {
            StateFileRead read = RunnerProtocol.ReadStateFile(home);
            if (read.Kind == StateFileKind.Missing) break;
            if (read.File is { } state)
            {
                if (state.Instance is null || !requests.TryGetValue(state.Instance, out string? request)) break;
                if (state.StopAck == request) { log.Write($"exit: {state.Instance} acknowledged"); return new ExitStopOutcome(requests.Count, true); }
            }
            if (system.UptimeMs >= until) break;
            Thread.Sleep(100);
        }
        log.Write("exit: no acknowledgement");
        return new ExitStopOutcome(requests.Count, false);
    }

    private async Task<T> Queue<T>(Func<Task<T>> work, T whenExiting)
    {
        if (exiting) return whenExiting;
        await queue.WaitAsync().ConfigureAwait(false);
        try { return exiting ? whenExiting : await work().ConfigureAwait(false); }
        finally { lock (gate) checkQueued = false; queue.Release(); }
    }

    private async Task CheckCore()
    {
        if (hubFor(store.Current) is not { } hub)
        {
            lock (gate) { status = null; statusFailure = null; lastCheckMs = system.UptimeMs; }
            Publish();
            return;
        }
        HubResult result = await hub.RunAsync(["runner", "status", "--json"], StatusTimeout).ConfigureAwait(false);
        StatusReply? reply = result.Outcome == HubOutcome.Exited ? RunnerProtocol.ParseStatus(result.StdOut) : null;
        lock (gate) lastCheckMs = system.UptimeMs;
        if (reply is null)
        {
            string line = result.Outcome == HubOutcome.Exited && Redaction.LastLine(result.StdErr).Length == 0
                ? "hub가 실행기 상태를 알려 주지 않았습니다(runner status --json이 없는 옛 hub일 수 있음)" : result.Describe();
            log.Write($"status failed (exit {result.ExitCode?.ToString() ?? result.Outcome.ToString()}): {line}");
            lock (gate) { status = null; statusFailure = line; }
            Publish();
            return;
        }
        lock (gate) { status = reply; statusFailure = null; operationError = null; file = RunnerProtocol.ReadStateFile(Home); }
        ApplyOwnership(reply);
        Publish();
    }

    /// <summary>The ownership rules of §5.3 after a status the hub could answer.</summary>
    private void ApplyOwnership(StatusReply reply)
    {
        store.Update(s =>
        {
            string? owned = s.OwnedInstance, pending = s.PendingInstance, exitStop = s.ExitStopRequested;
            if (pending is not null)
            {
                if (!InstanceId.TryParse(pending, out var id)) pending = null;
                else if (reply.Instance == pending) { owned = pending; pending = null; }
                // Another boot: a runner with this id can never come up.
                else if (id.BootId != system.BootId) pending = null;
                // After the deadline, off or a confirmed other instance settles it; "instance: null" never does.
                else if (system.UptimeMs >= id.DeadlineMs && (!reply.Running || reply.Instance is not null)) pending = null;
            }
            if (owned is not null && owned != pending && (!reply.Running || reply.Instance is not null && reply.Instance != owned)) owned = null;
            if (exitStop is not null && (!reply.Running || reply.Instance is not null && reply.Instance != exitStop)) exitStop = null;
            if (owned == s.OwnedInstance && pending == s.PendingInstance && exitStop == s.ExitStopRequested) return s;
            log.Write($"ownership: owned {owned ?? "-"}, pending {pending ?? "-"}");
            return s with { OwnedInstance = owned, PendingInstance = pending, ExitStopRequested = exitStop };
        });
    }

    private async Task AutoStartCore()
    {
        if (!autoStart || exiting) return;
        WorkbenchSettings s = store.Current;
        StatusReply? reply; lock (gate) reply = status;
        if (!s.Linked) { autoStart = false; return; }
        // Unverifiable, or a start of ours not settled yet: no automatic action, try again at the next check.
        if (reply is null || s.PendingInstance is not null) return;
        if (reply.Running)
        {
            // The runner this app asked to stop when it last exited is still finishing: start once it is gone.
            if (reply.Instance is not null && reply.Instance == s.ExitStopRequested) return;
            autoStart = false;
            return;
        }
        autoStart = false;
        if (RegistrationProblem(reply) is not null) { Publish(); return; }
        await StartCore();
    }

    private async Task<string?> StartCore()
    {
        if (hubFor(store.Current) is not { } hub) return "hub 명령이 설정되지 않았습니다";
        string id;
        try { id = InstanceId.Create(system).ToString(); }
        catch (InvalidOperationException ex) { SetError(ex.Message); return ex.Message; }
        // Written before the hub is asked, so that an app that dies now still knows the id next time (§5.3).
        if (!store.TryUpdate(s => exiting ? null : s with { PendingInstance = id })) return "앱을 끝내는 중입니다";
        if (exiting) return "앱을 끝내는 중입니다";
        SetOperation(RunnerPhase.Starting);
        log.Write($"start {id}");
        HubResult result;
        try { result = await hub.RunAsync(["runner", "start", "--instance", id, "--json"], StartTimeout).ConfigureAwait(false); }
        finally { SetOperation(null); }
        StartReply? reply = result.Outcome == HubOutcome.Exited ? RunnerProtocol.ParseStart(result.StdOut) : null;
        if (reply is { Error: StartError.None } && reply.Instance == id)
        {
            // Even when the app is exiting by now, this only records the result: the runner has its stop request already.
            store.Update(s => s with { OwnedInstance = id, PendingInstance = s.PendingInstance == id ? null : s.PendingInstance });
            lock (gate) { status = new StatusReply(true, id, reply.Pid, reply.StartedAt, status?.Registration ?? Registration.Ok, null); operationError = null; }
            log.Write($"start {id}: started (pid {reply.Pid})");
            Publish();
            return null;
        }
        // Anything else leaves pending_instance: a launcher may still bring the real runner up before the deadline.
        string message = reply?.Error switch
        {
            StartError.Running => "다른 실행기가 이미 떠 있어 켜지 않았습니다",
            StartError.Failed => $"실행기가 뜨지 못했습니다(종료 코드 {reply.ExitCode?.ToString() ?? "?"})" + (Redaction.LastLine(result.StdErr) is { Length: > 0 } l ? $": {l}" : ""),
            StartError.Cancelled => "실행기가 제시간에 뜨지 않아 취소했습니다",
            StartError.Unconfirmed => "실행기를 켰는지 확인하지 못했습니다",
            _ => result.Outcome == HubOutcome.Exited && Redaction.LastLine(result.StdErr).Length == 0 ? "hub가 켜기 결과를 알려 주지 않았습니다" : result.Describe(),
        };
        log.Write($"start {id}: {reply?.Error.ToString() ?? result.Outcome.ToString()} (exit {result.ExitCode?.ToString() ?? "-"}): {message}");
        SetError(message);
        return message;
    }

    private static string? RegistrationProblem(StatusReply reply) => reply.Registration switch
    {
        Registration.Missing => "이 기기는 아직 실행기로 등록되지 않았습니다. 터미널에서 himari-hub runner setup을 먼저 하세요",
        Registration.Broken => $"실행기 등록에 문제가 있습니다: {reply.Why ?? "이유 모름"}",
        _ => null,
    };

    private void SetOperation(RunnerPhase? value) { lock (gate) operation = value; Publish(); }
    private void SetError(string message) { lock (gate) operationError = message; Publish(); }

    // Called under the lock.
    private bool Unsettled()
    {
        if (status is null || operationError is not null) return true;
        if (store.Current.PendingInstance is not null) return true;
        if (store.Current.ExitStopRequested is not null) return true;
        return status.Running && status.Instance is null;
    }

    // Called under the lock. Null when the state file agrees with the last status.
    private string? DisagreementKey()
    {
        if (status is null) return null;
        RunnerStateFile? state = file.File;
        bool fresh = file.Kind == StateFileKind.Ok && state!.IsFresh(system.Now);
        if (status.Running && status.Instance is not null)
            return fresh && state!.Instance == status.Instance ? null : $"{file.Kind}:{fresh}:{state?.Instance}:{status.Instance}";
        if (!status.Running && fresh) return $"appeared:{state!.Instance}";
        return null;
    }

    private void Publish()
    {
        RunnerView view;
        lock (gate) view = Compute();
        View = view;
        Changed?.Invoke(view);
    }

    // Called under the lock.
    private RunnerView Compute()
    {
        WorkbenchSettings s = store.Current;
        if (exiting) return new RunnerView(RunnerPhase.Exiting);
        if (operation is { } op) return new RunnerView(op);
        Registration registration = status?.Registration ?? Registration.Unknown;
        if (operationError is not null) return new RunnerView(RunnerPhase.Error, Detail: operationError, Registration: registration, RegistrationWhy: status?.Why);
        if (status is null)
            return statusFailure is not null ? new RunnerView(RunnerPhase.Unverifiable, Detail: statusFailure)
                : hubFor(s) is null ? new RunnerView(RunnerPhase.NotConfigured) : new RunnerView(RunnerPhase.Checking);
        if (s.PendingInstance is { } pending && status.Instance != pending) return new RunnerView(RunnerPhase.AwaitingConfirm);
        if (!status.Running) return new RunnerView(RunnerPhase.Off, Registration: registration, RegistrationWhy: status.Why);
        if (status.Instance is null) return new RunnerView(RunnerPhase.Running, RunnerConnection.UnknownIdentity, Pid: status.Pid, Registration: registration);
        RunnerStateFile? state = file.File;
        bool matches = file.Kind == StateFileKind.Ok && state!.IsFresh(system.Now) && state.Instance == status.Instance;
        RunnerConnection connection = !matches ? RunnerConnection.Unknown : state!.State switch
        {
            "connected" => RunnerConnection.Connected,
            "disconnected" => RunnerConnection.Disconnected,
            "stopping" => RunnerConnection.StopReceived,
            "starting" => RunnerConnection.Starting,
            _ => RunnerConnection.Unknown,
        };
        return new RunnerView(RunnerPhase.Running, connection, status.Instance, matches ? state!.Pid ?? status.Pid : status.Pid,
            matches ? state!.Name : null, status.Instance == s.OwnedInstance, matches ? state!.Error : null,
            status.Instance == s.ExitStopRequested, registration);
    }
}

public sealed record ExitStopOutcome(int RequestsWritten, bool Acknowledged);
