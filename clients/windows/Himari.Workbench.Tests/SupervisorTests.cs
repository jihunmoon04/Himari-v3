using Himari.Workbench;

namespace Himari.Workbench.Tests;

/// <summary>§10 "RunnerSupervisor 단위 시험" with an in-process hub that follows the contract and a temporary hub home.</summary>
public sealed class SupervisorTests : IDisposable
{
    private readonly TempFolder temp = new();
    private readonly FakeSystem system = new();
    private readonly string home;
    private readonly SettingsStore store;
    private readonly ScriptedHub hub;

    public SupervisorTests()
    {
        home = temp.Sub("hub 홈");
        store = new SettingsStore(temp.Sub("설정"));
        store.Load();
        store.Update(s => s with { Server = "https://himari.example", Hub = new HubCommand(@"C:\uv.exe", ["run"], temp.Path), HubHome = home });
        hub = new ScriptedHub(home);
    }

    public void Dispose() => temp.Dispose();

    private RunnerSupervisor New() => new(store, _ => hub, system, WorkbenchLog.None);
    private string Id(long deadlineFromNow = 30_000, long? boot = null) => new InstanceId(boot ?? system.BootId, system.UptimeMs + deadlineFromNow, Guid.NewGuid().ToString("N")).ToString();
    private string[] StopFiles() => Directory.Exists(StopRequests.Folder(home)) ? Directory.GetFiles(StopRequests.Folder(home)).Select(path => Path.GetFileName(path)).ToArray() : [];

    [Fact]
    public async Task LinkedAppStartsAnOffRunnerAndOwnsIt()
    {
        var supervisor = New();
        await supervisor.InitializeAsync();
        string start = Assert.Single(hub.CallsOf("start"));
        string id = start.Split(' ')[3];
        Assert.True(InstanceId.TryParse(id, out var parsed));
        Assert.Equal(system.BootId, parsed.BootId);
        Assert.InRange(parsed.DeadlineMs - system.UptimeMs, 25_000, 30_000);
        Assert.Equal("runner start --instance " + id + " --json", start);
        Assert.Equal(id, store.Current.OwnedInstance);
        Assert.Null(store.Current.PendingInstance);
        supervisor.PollStateFile();
        Assert.Equal(RunnerPhase.Running, supervisor.View.Phase);
        Assert.Equal(RunnerConnection.Connected, supervisor.View.Connection);
        Assert.True(supervisor.View.Owned);
        Assert.Equal("실행기: 연결됨 · laptop · 앱이 켠 것", supervisor.View.Text);
    }

    [Fact]
    public async Task NotLinkedNeverStartsByItself()
    {
        store.Update(s => s with { Linked = false });
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Equal(RunnerPhase.Off, supervisor.View.Phase);
    }

    [Fact]
    public async Task ARunnerStartedElsewhereIsLeftAlone()
    {
        hub.Running = true; hub.Instance = Id(); hub.WriteState(hub.Instance, "disconnected");
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        supervisor.PollStateFile();
        Assert.Equal("실행기: 연결 안 됨 · laptop · 다른 곳에서 켠 것", supervisor.View.Text);
        Assert.Null(store.Current.OwnedInstance);
    }

    [Fact]
    public async Task StatusFailureMeansUnverifiableAndNoAutomaticAction()
    {
        hub.StatusFails = true; hub.StatusStdErr = "noise\nhttpx.HTTPError: 401 Authorization: Bearer abc.def";
        store.Update(s => s with { OwnedInstance = "7.1.aa000000000000000000000000000000" });
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Equal(RunnerPhase.Unverifiable, supervisor.View.Phase);
        Assert.Equal("실행기: 확인 불가 · httpx.HTTPError: 401 Authorization: …", supervisor.View.Text);
        Assert.Equal("7.1.aa000000000000000000000000000000", store.Current.OwnedInstance);
        // Once the hub answers, the start happens (the app-start decision was only postponed).
        hub.StatusFails = false;
        await supervisor.RefreshAsync();
        Assert.Single(hub.CallsOf("start"));
    }

    [Fact]
    public async Task InstanceNullIsUnknownIdentityAndKeepsOwnership()
    {
        string owned = Id();
        store.Update(s => s with { OwnedInstance = owned });
        hub.Running = true; hub.Instance = owned; hub.HideInstance = true;
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Equal(RunnerConnection.UnknownIdentity, supervisor.View.Connection);
        Assert.Equal("실행기: 떠 있음 · 누구인지 모름", supervisor.View.Text);
        Assert.False(supervisor.View.CanStop);
        Assert.Equal(owned, store.Current.OwnedInstance);
        var (target, problem) = await supervisor.GetStopTargetAsync();
        Assert.Null(target);
        Assert.NotNull(problem);
        Assert.Empty(hub.CallsOf("stop"));
    }

    [Theory]
    [InlineData("missing")]
    [InlineData("stale")]
    [InlineData("broken")]
    [InlineData("other")]
    public async Task StateFileThatDoesNotMatchAsksAgainOnceThenShowsUnknownConnection(string kind)
    {
        string id = Id();
        hub.Running = true; hub.Instance = id;
        string statePath = Path.Combine(home, RunnerProtocol.StateFileName);
        switch (kind)
        {
            case "stale": hub.WriteState(id, "connected", updated: DateTimeOffset.Now.AddSeconds(-40)); break;
            case "broken": File.WriteAllText(statePath, "{\"instance\":"); break;
            case "other": hub.WriteState(Id(), "connected"); break;
        }
        var supervisor = New();
        await supervisor.InitializeAsync();
        int before = hub.CallsOf("status").Length;
        supervisor.PollStateFile();
        await Wait.Until(() => hub.CallsOf("status").Length == before + 1);
        await Task.Delay(100);
        supervisor.PollStateFile();
        supervisor.PollStateFile();
        await Task.Delay(100);
        Assert.Equal(before + 1, hub.CallsOf("status").Length);
        Assert.Equal(RunnerPhase.Running, supervisor.View.Phase);
        Assert.Equal(RunnerConnection.Unknown, supervisor.View.Connection);
        Assert.Equal("실행기: 떠 있음 · 연결 모름 · 다른 곳에서 켠 것", supervisor.View.Text);
    }

    [Fact]
    public async Task ARunnerThatGoesAwayIsNoticedFromTheStateFile()
    {
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Equal(RunnerPhase.Running, supervisor.View.Phase);
        // Stopped from a terminal: the runner removes its state file and releases the lock.
        hub.Running = false; hub.Instance = null; File.Delete(Path.Combine(home, RunnerProtocol.StateFileName));
        supervisor.PollStateFile();
        await Wait.Until(() => supervisor.View.Phase == RunnerPhase.Off);
        Assert.Null(store.Current.OwnedInstance);
        Assert.Single(hub.CallsOf("start"));
    }

    [Fact]
    public async Task PendingThatCameUpBecomesOwned()
    {
        string pending = Id();
        store.Update(s => s with { PendingInstance = pending });
        hub.Running = true; hub.Instance = pending;
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Equal(pending, store.Current.OwnedInstance);
        Assert.Null(store.Current.PendingInstance);
        Assert.Empty(hub.CallsOf("start"));
        Assert.True(supervisor.View.Owned);
    }

    [Fact]
    public async Task PendingBeforeItsDeadlineIsKeptAndBlocksAutoStart()
    {
        string pending = Id(20_000);
        store.Update(s => s with { PendingInstance = pending });
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Equal(pending, store.Current.PendingInstance);
        Assert.Empty(hub.CallsOf("start"));
        Assert.Equal("실행기: 켜는 중(확인 대기)", supervisor.View.Text);
        Assert.Equal("앞서 켠 실행기를 확인하는 중입니다. 잠시 뒤 다시 하세요", await supervisor.StartAsync());
        // After the deadline an off status settles it, and the app-start decision is made then.
        system.Offset += 21_000;
        var again = New();
        await again.InitializeAsync();
        Assert.Null(store.Current.PendingInstance);
        Assert.Single(hub.CallsOf("start"));
    }

    [Fact]
    public async Task PendingWithInstanceNullIsKeptEvenAfterTheDeadline()
    {
        string pending = Id(-1);
        store.Update(s => s with { PendingInstance = pending });
        hub.Running = true; hub.Instance = Id(); hub.HideInstance = true;
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Equal(pending, store.Current.PendingInstance);
        Assert.Empty(hub.CallsOf("start"));
        hub.StatusFails = true;
        await supervisor.RefreshAsync();
        Assert.Equal(pending, store.Current.PendingInstance);
    }

    [Fact]
    public async Task PendingAfterTheDeadlineWithAConfirmedOtherInstanceIsDropped()
    {
        string pending = Id(-1);
        store.Update(s => s with { PendingInstance = pending });
        hub.Running = true; hub.Instance = Id();
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Null(store.Current.PendingInstance);
        Assert.Null(store.Current.OwnedInstance);
        Assert.Empty(hub.CallsOf("start"));
    }

    [Fact]
    public async Task PendingFromAnotherBootIsDropped()
    {
        string pending = Id(20_000, boot: system.BootId - 1);
        store.Update(s => s with { PendingInstance = pending });
        hub.Running = true; hub.Instance = Id(); hub.HideInstance = true;
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Null(store.Current.PendingInstance);
    }

    [Theory]
    [InlineData("{\"error\": \"running\", \"instance\": \"7.1.ab\"}", "다른 실행기가 이미 떠 있어 켜지 않았습니다")]
    [InlineData("{\"error\": \"failed\", \"exit_code\": 3}", "실행기가 뜨지 못했습니다(종료 코드 3)")]
    [InlineData("{\"error\": \"cancelled\"}", "실행기가 제시간에 뜨지 않아 취소했습니다")]
    [InlineData("{\"error\": \"unconfirmed\"}", "실행기를 켰는지 확인하지 못했습니다")]
    [InlineData("garbage", "hub가 켜기 결과를 알려 주지 않았습니다")]
    public async Task AnUnsuccessfulStartKeepsPending(string reply, string message)
    {
        hub.StartReply = _ => reply;
        var supervisor = New();
        await supervisor.InitializeAsync();
        string id = hub.CallsOf("start").Single().Split(' ')[3];
        Assert.Equal(id, store.Current.PendingInstance);
        Assert.Null(store.Current.OwnedInstance);
        Assert.Equal(RunnerPhase.Error, supervisor.View.Phase);
        Assert.Equal(message, supervisor.View.Detail);
    }

    [Fact]
    public async Task ASuccessShapedReplyFromAFailedStartKeepsPending()
    {
        hub.StartReply = id => $"{{\"instance\": \"{id}\", \"pid\": 1, \"started_at\": \"t\"}}";
        var supervisor = New();
        await supervisor.InitializeAsync();
        string id = hub.CallsOf("start").Single().Split(' ')[3];
        Assert.Equal(id, store.Current.PendingInstance);
        Assert.Null(store.Current.OwnedInstance);
    }

    [Fact]
    public async Task ExitDuringStartWritesStopRequestsWithoutWaitingForTheStartAndTheLateStartOnlyRecords()
    {
        hub.StartGate = new TaskCompletionSource();
        var supervisor = New();
        Task init = supervisor.InitializeAsync();
        await Wait.Until(() => hub.CallsOf("start").Length == 1);
        string id = hub.CallsOf("start").Single().Split(' ')[3];
        Assert.Equal(id, store.Current.PendingInstance);
        var watch = System.Diagnostics.Stopwatch.StartNew();
        var outcome = await Task.Run(() => supervisor.RequestStopForExit(RunnerSupervisor.ExitAckWait));
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(5.5));
        Assert.Equal(1, outcome.RequestsWritten);
        string request = Assert.Single(StopFiles());
        Assert.StartsWith(id + ".", request);
        int calls = hub.Calls.Count;
        hub.StartGate.SetResult();
        await init;
        Assert.Equal(id, store.Current.OwnedInstance);
        Assert.Null(store.Current.PendingInstance);
        Assert.Equal(calls, hub.Calls.Count);
        // Nothing new runs once exiting.
        Assert.Equal("앱을 끝내는 중입니다", await supervisor.StartAsync());
        await supervisor.RefreshAsync();
        Assert.Equal(calls, hub.Calls.Count);
    }

    [Fact]
    public async Task ExitWritesAStopRequestForTheOwnedAndThePendingInstance()
    {
        string owned = Id(), pending = Id();
        store.Update(s => s with { OwnedInstance = owned, PendingInstance = pending });
        hub.WriteState(owned, "connected");
        var supervisor = New();
        var ack = Task.Run(async () =>
        {
            await Wait.Until(() => StopFiles().Any(name => name.StartsWith(owned + ".")));
            string request = StopFiles().First(name => name.StartsWith(owned + "."));
            hub.WriteState(owned, "stopping", request);
        });
        var outcome = supervisor.RequestStopForExit(RunnerSupervisor.ExitAckWait);
        await ack;
        Assert.True(outcome.Acknowledged);
        Assert.Equal(2, outcome.RequestsWritten);
        Assert.Contains(StopFiles(), name => name.StartsWith(pending + "."));
        Assert.Equal(owned, store.Current.ExitStopRequested);
        Assert.Empty(hub.Calls);
    }

    [Fact]
    public async Task ExitKeepsWatchingForAPendingRunnerThatComesUpLate()
    {
        string pending = Id();
        store.Update(s => s with { PendingInstance = pending });
        var supervisor = New();
        // No state file yet: the pending runner is still coming up and must still be able to acknowledge (§5.5).
        var late = Task.Run(async () =>
        {
            await Wait.Until(() => StopFiles().Any(name => name.StartsWith(pending + ".")));
            await Task.Delay(1500);
            hub.WriteState(pending, "starting");
            await Task.Delay(300);
            hub.WriteState(pending, "stopping", StopFiles().First(name => name.StartsWith(pending + ".")));
        });
        var watch = System.Diagnostics.Stopwatch.StartNew();
        var outcome = supervisor.RequestStopForExit(RunnerSupervisor.ExitAckWait);
        await late;
        Assert.True(outcome.Acknowledged);
        Assert.InRange(watch.Elapsed.TotalSeconds, 1.5, 5.5);
    }

    [Fact]
    public async Task ASecondExitCallerReturnsOnlyWhenTheFirstIsDone()
    {
        string owned = Id();
        store.Update(s => s with { OwnedInstance = owned });
        hub.WriteState(owned, "connected");
        var supervisor = New();
        var first = Task.Run(() => supervisor.RequestStopForExit(TimeSpan.FromSeconds(2)));
        await Wait.Until(() => StopFiles().Length == 1);
        var watch = System.Diagnostics.Stopwatch.StartNew();
        var second = supervisor.RequestStopForExit(TimeSpan.FromSeconds(2));
        Assert.True(first.IsCompleted);
        Assert.InRange(watch.Elapsed.TotalSeconds, 1, 3);
        Assert.Equal(1, second.RequestsWritten);
        Assert.Single(StopFiles());
    }

    [Fact]
    public async Task APendingStartThatSucceedsAfterExitIsRememberedAsAskedToStop()
    {
        hub.StartGate = new TaskCompletionSource();
        var supervisor = New();
        Task init = supervisor.InitializeAsync();
        await Wait.Until(() => hub.CallsOf("start").Length == 1);
        string id = store.Current.PendingInstance!;
        supervisor.RequestStopForExit(TimeSpan.Zero);
        Assert.Equal(id, store.Current.ExitStopRequested);
        hub.StartGate.SetResult();
        await init;
        Assert.Equal(id, store.Current.OwnedInstance);
        Assert.Equal(id, store.Current.ExitStopRequested);
        // The next launch shows it as still finishing.
        var next = New();
        await next.InitializeAsync();
        next.PollStateFile();
        Assert.True(next.View.ExitStopPending);
        Assert.Single(hub.CallsOf("start"));
    }

    [Fact]
    public async Task AStatusAskedBeforeThePendingDeadlineDoesNotSettleIt()
    {
        string pending = Id(deadlineFromNow: 150);
        store.Update(s => s with { PendingInstance = pending, Linked = false });
        hub.StatusDelay = 400;
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Equal(pending, store.Current.PendingInstance);
        hub.StatusDelay = 0;
        await supervisor.RefreshAsync();
        Assert.Null(store.Current.PendingInstance);
    }

    [Fact]
    public void ExitWaitsAtMostFiveSecondsForAnAcknowledgement()
    {
        string owned = Id();
        store.Update(s => s with { OwnedInstance = owned });
        hub.WriteState(owned, "connected");
        var watch = System.Diagnostics.Stopwatch.StartNew();
        var outcome = New().RequestStopForExit(TimeSpan.FromSeconds(1));
        Assert.False(outcome.Acknowledged);
        Assert.InRange(watch.Elapsed.TotalSeconds, 0.9, 3);
    }

    [Fact]
    public void ExitWhenNotLinkedLeavesTheRunner()
    {
        store.Update(s => s with { Linked = false, OwnedInstance = Id() });
        var outcome = New().RequestStopForExit(RunnerSupervisor.ExitAckWait);
        Assert.Equal(0, outcome.RequestsWritten);
        Assert.Empty(StopFiles());
    }

    [Fact]
    public void ExitNeverTouchesARunnerItDoesNotOwn()
    {
        hub.WriteState(Id(), "connected");
        var outcome = New().RequestStopForExit(RunnerSupervisor.ExitAckWait);
        Assert.Equal(0, outcome.RequestsWritten);
        Assert.Empty(StopFiles());
    }

    [Fact]
    public async Task AfterACrashTheOwnedRunnerIsTakenOverAgain()
    {
        string owned = Id();
        store.Update(s => s with { OwnedInstance = owned });
        hub.Running = true; hub.Instance = owned; hub.WriteState(owned, "connected");
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Equal(owned, store.Current.OwnedInstance);
        Assert.True(supervisor.View.Owned);
    }

    [Fact]
    public async Task OwnershipIsDroppedWhenAnotherInstanceRuns()
    {
        store.Update(s => s with { OwnedInstance = Id() });
        hub.Running = true; hub.Instance = Id();
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Null(store.Current.OwnedInstance);
        Assert.False(supervisor.View.Owned);
    }

    [Fact]
    public async Task AfterAnExitStopTheStillRunningRunnerIsShownAndNotReplaced()
    {
        string owned = Id();
        store.Update(s => s with { OwnedInstance = owned, ExitStopRequested = owned });
        hub.Running = true; hub.Instance = owned; hub.WriteState(owned, "stopping");
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Equal("실행기: 끄기 요청을 받음 · laptop · 앱이 켠 것 · 끄기를 요청했지만 아직 떠 있음", supervisor.View.Text);
        // §5.3: a runner up at app start is left as it is; once it is gone the app does not start another by itself.
        hub.Running = false; hub.Instance = null;
        await supervisor.RefreshAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Null(store.Current.ExitStopRequested);
        Assert.Equal(RunnerPhase.Off, supervisor.View.Phase);
    }

    [Fact]
    public async Task StopPassesTheConfirmedInstanceAndForgetsOwnership()
    {
        var supervisor = New();
        await supervisor.InitializeAsync();
        string owned = store.Current.OwnedInstance!;
        var (target, problem) = await supervisor.GetStopTargetAsync();
        Assert.Null(problem);
        Assert.Equal(owned, target!.Instance);
        Assert.True(target.Owned);
        Assert.Equal(4242, target.Pid);
        var (result, _) = await supervisor.StopAsync(target.Instance);
        Assert.Equal(StopResult.Stopped, result);
        Assert.Equal($"runner stop --instance {owned} --json", Assert.Single(hub.CallsOf("stop")));
        Assert.Null(store.Current.OwnedInstance);
        Assert.Equal(RunnerPhase.Off, supervisor.View.Phase);
        Assert.Single(hub.CallsOf("start"));
    }

    [Fact]
    public async Task StopOfAChangedRunnerIsReportedForAskingAgain()
    {
        string owned = Id();
        store.Update(s => s with { OwnedInstance = owned });
        hub.Running = true; hub.Instance = owned;
        var supervisor = New();
        await supervisor.InitializeAsync();
        hub.StopResult = "other_instance";
        var (result, message) = await supervisor.StopAsync(owned);
        Assert.Equal(StopResult.OtherInstance, result);
        Assert.NotNull(message);
        Assert.Equal(owned, store.Current.OwnedInstance);
    }

    [Fact]
    public async Task UnconfirmedStopIsAnError()
    {
        var supervisor = New();
        await supervisor.InitializeAsync();
        hub.StopResult = "unconfirmed";
        var (result, message) = await supervisor.StopAsync(store.Current.OwnedInstance!);
        Assert.Equal(StopResult.Unconfirmed, result);
        Assert.Equal("끄기를 요청했지만 확인하지 못했습니다", message);
        Assert.Equal(RunnerPhase.Error, supervisor.View.Phase);
        Assert.NotNull(store.Current.OwnedInstance);
    }

    [Fact]
    public async Task UnverifiableStatusNeverStops()
    {
        var supervisor = New();
        await supervisor.InitializeAsync();
        hub.StatusFails = true;
        var (target, problem) = await supervisor.GetStopTargetAsync();
        Assert.Null(target);
        Assert.Equal("실행기 상태를 확인하지 못했습니다", problem);
        Assert.Empty(hub.CallsOf("stop"));
    }

    [Fact]
    public async Task OperationsRunOneAtATime()
    {
        store.Update(s => s with { Linked = false });
        var supervisor = New();
        await Task.WhenAll(Enumerable.Range(0, 12).Select(i => (i % 3) switch
        {
            0 => supervisor.StartAsync(),
            1 => supervisor.RefreshAsync(),
            _ => (Task)supervisor.GetStopTargetAsync(),
        }));
        Assert.Equal(1, hub.MaxConcurrent);
        Assert.Single(hub.CallsOf("start"));
    }

    [Fact]
    public async Task AStatusThatExitedWithAnErrorIsUnverifiableEvenWithAReply()
    {
        hub.StatusExitCode = 1;
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Equal(RunnerPhase.Unverifiable, supervisor.View.Phase);
        Assert.Equal(1, supervisor.LastStatusResult!.ExitCode);
    }

    [Fact]
    public async Task HubSettingsCannotChangeWhileTheAppHoldsARunner()
    {
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.NotNull(store.Current.OwnedInstance);
        string other = temp.Sub("다른 홈");
        Assert.Equal(RunnerSupervisor.HoldsRunnerMessage, await supervisor.ChangeSettingsAsync(s => s with { HubHome = other }));
        Assert.Equal(home, store.Current.HubHome);
        // Other settings still change.
        Assert.Null(await supervisor.ChangeSettingsAsync(s => s with { DeveloperMode = true }));
        Assert.True(store.Current.DeveloperMode);
    }

    [Fact]
    public async Task ASettingsChangeWaitsForAStartInTheQueueAndSeesItsInstance()
    {
        hub.StartGate = new TaskCompletionSource();
        var supervisor = New();
        Task init = supervisor.InitializeAsync();
        await Wait.Until(() => hub.CallsOf("start").Length == 1);
        string other = temp.Sub("다른 홈");
        Task<string?> change = supervisor.ChangeSettingsAsync(s => s with { HubHome = other });
        await Task.Delay(200);
        Assert.False(change.IsCompleted);
        hub.StartGate.SetResult();
        await init;
        Assert.Equal(RunnerSupervisor.HoldsRunnerMessage, await change);
        Assert.Equal(home, store.Current.HubHome);
    }

    [Fact]
    public async Task TheWizardsCheckWaitsInTheQueueAndNeverRunsAfterExit()
    {
        hub.StartGate = new TaskCompletionSource();
        var supervisor = New();
        Task init = supervisor.InitializeAsync();
        await Wait.Until(() => hub.CallsOf("start").Length == 1);
        Task<HubResult?> probe = supervisor.ProbeAsync(new HubCommand(@"C:\uv.exe", ["run"], temp.Path), temp.Sub("새 홈"), TimeSpan.FromSeconds(5));
        await Task.Delay(200);
        Assert.False(probe.IsCompleted);
        hub.StartGate.SetResult();
        await init;
        Assert.Equal(0, (await probe)!.ExitCode);
        Assert.Equal(1, hub.MaxConcurrent);
        supervisor.RequestStopForExit(TimeSpan.Zero);
        int calls = hub.Calls.Count;
        Assert.Null(await supervisor.ProbeAsync(new HubCommand(@"C:\uv.exe", ["run"], temp.Path), home, TimeSpan.FromSeconds(5)));
        Assert.Equal(calls, hub.Calls.Count);
    }

    [Fact]
    public async Task ABusyHubIsShownWithItsReason()
    {
        hub.StatusFails = true; hub.StatusStdErr = ""; hub.StatusStdOut = "{\"error\": \"busy\", \"why\": \"전이 잠금이 2초 넘게 잡혀 있음\"}";
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Equal("실행기: 확인 불가 · 전이 잠금이 2초 넘게 잡혀 있음", supervisor.View.Text);
        Assert.Empty(hub.CallsOf("start"));
    }

    [Fact]
    public async Task ChangingTheHubHomeAsksTheHubAgain()
    {
        store.Update(s => s with { Linked = false });
        var supervisor = New();
        await supervisor.InitializeAsync();
        int before = hub.CallsOf("status").Length;
        Assert.Null(await supervisor.ChangeSettingsAsync(s => s with { HubHome = temp.Sub("다른 홈") }));
        Assert.Equal(before + 1, hub.CallsOf("status").Length);
        Assert.Null(await supervisor.ChangeSettingsAsync(s => s with { Linked = true }));
        Assert.Equal(before + 1, hub.CallsOf("status").Length);
        Assert.Empty(hub.CallsOf("start"));
    }

    [Theory]
    [InlineData("missing", "이 기기는 아직 실행기로 등록되지 않음")]
    [InlineData("broken", "토큰 파일을 읽을 수 없음")]
    public async Task RegistrationProblemsBlockStarting(string registration, string shown)
    {
        hub.Registration = registration; hub.Why = registration == "broken" ? "토큰 파일을 읽을 수 없음" : null;
        var supervisor = New();
        await supervisor.InitializeAsync();
        Assert.Empty(hub.CallsOf("start"));
        Assert.Contains(shown, supervisor.View.Text);
        Assert.False(supervisor.View.CanStart);
        Assert.NotNull(await supervisor.StartAsync());
        Assert.Empty(hub.CallsOf("start"));
    }
}
