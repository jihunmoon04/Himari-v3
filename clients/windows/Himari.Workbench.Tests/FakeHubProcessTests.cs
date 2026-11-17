using System.Text.Json;
using Himari.Workbench;

namespace Himari.Workbench.Tests;

/// <summary>
/// The real process path against the fake hub executable: argument lists without a shell, a hub home with a space and
/// Hangul, and the whole start → connected → exit → stopped cycle by files and commands only.
/// </summary>
public sealed class FakeHubProcessTests : IDisposable
{
    private readonly TempFolder temp = new();
    private readonly string home;
    private readonly string work;

    public FakeHubProcessTests()
    {
        home = temp.Sub("hub 홈 (테스트)");
        work = temp.Sub("hub 폴더");
    }

    public void Dispose() => temp.Dispose();

    private void Configure(object config) =>
        File.WriteAllText(Path.Combine(home, "fake-hub.json"), JsonSerializer.Serialize(config, new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower }));

    [Fact]
    public async Task ArgumentsReachTheHubIntactWithoutAShell()
    {
        Assert.True(File.Exists(FakeHubExe.PathName), FakeHubExe.PathName);
        var command = new HubCommand(FakeHubExe.PathName, ["echo-args", "--project", @"D:\00_프로젝트\Himari v4", "a \"quoted\" & | > %PATH%"], work);
        var result = await new ProcessHubCommandRunner(command, home).RunAsync(["last arg", ""], TimeSpan.FromSeconds(30));
        Assert.Equal(HubOutcome.Exited, result.Outcome);
        using var json = JsonDocument.Parse(result.StdOut);
        string[] args = json.RootElement.GetProperty("args").EnumerateArray().Select(e => e.GetString()!).ToArray();
        Assert.Equal(new[] { "--project", @"D:\00_프로젝트\Himari v4", "a \"quoted\" & | > %PATH%", "last arg", "" }, args);
        Assert.Equal(work, json.RootElement.GetProperty("cwd").GetString());
        Assert.Equal(home, json.RootElement.GetProperty("home").GetString());
    }

    [Fact]
    public async Task MissingExecutableIsReported()
    {
        var result = await new ProcessHubCommandRunner(new HubCommand(Path.Combine(work, "없음.exe"), [], work), home).RunAsync(["runner"], TimeSpan.FromSeconds(5));
        Assert.Equal(HubOutcome.NotFound, result.Outcome);
    }

    [Fact]
    public async Task FullCycleAgainstTheFakeHub()
    {
        var store = new SettingsStore(temp.Sub("설정"));
        store.Load();
        store.Update(s => s with { Server = "https://himari.example", Hub = FakeHubExe.Command(work), HubHome = home });
        var system = new WindowsSystemInfo();
        var supervisor = new RunnerSupervisor(store, s => new ProcessHubCommandRunner(s.Hub!, s.EffectiveHubHome), system, WorkbenchLog.None);

        var watch = System.Diagnostics.Stopwatch.StartNew();
        await supervisor.InitializeAsync();
        // The runner keeps the inherited output pipe open: `start` must still return promptly.
        Assert.True(watch.Elapsed < TimeSpan.FromSeconds(15), watch.Elapsed.ToString());
        string owned = Assert.IsType<string>(store.Current.OwnedInstance);
        supervisor.PollStateFile();
        Assert.Equal("실행기: 연결됨 · laptop · 앱이 켠 것", supervisor.View.Text);

        var outcome = supervisor.RequestStopForExit(RunnerSupervisor.ExitAckWait);
        Assert.True(outcome.Acknowledged);
        var status = new ProcessHubCommandRunner(store.Current.Hub!, home);
        await Wait.Until(() => RunnerProtocol.ParseStatus(status.RunAsync(["runner", "status", "--json"], TimeSpan.FromSeconds(30)).Result.StdOut)?.Running == false, 15_000);
        Assert.False(File.Exists(Path.Combine(home, RunnerProtocol.StateFileName)));
        Assert.Empty(Directory.GetFiles(StopRequests.Folder(home)));

        // The next app start sees it gone, drops ownership and starts a new one.
        var next = new RunnerSupervisor(store, s => new ProcessHubCommandRunner(s.Hub!, s.EffectiveHubHome), system, WorkbenchLog.None);
        await next.InitializeAsync();
        Assert.NotEqual(owned, store.Current.OwnedInstance);
        var (target, _) = await next.GetStopTargetAsync();
        var (result, _) = await next.StopAsync(target!.Instance);
        Assert.Equal(StopResult.Stopped, result);
        Assert.Null(store.Current.OwnedInstance);
        Assert.Equal(RunnerPhase.Off, next.View.Phase);
    }

    [Fact]
    public async Task ATerminalRunnerIsNotStoppedByExit()
    {
        var command = FakeHubExe.Command(work);
        var runner = new ProcessHubCommandRunner(command, home);
        var started = RunnerProtocol.ParseStart((await runner.RunAsync(["runner", "start", "--json"], TimeSpan.FromSeconds(30))).StdOut);
        Assert.Equal(StartError.None, started.Error);
        try
        {
            var store = new SettingsStore(temp.Sub("설정"));
            store.Load();
            store.Update(s => s with { Server = "https://himari.example", Hub = command, HubHome = home });
            var supervisor = new RunnerSupervisor(store, s => new ProcessHubCommandRunner(s.Hub!, s.EffectiveHubHome), new WindowsSystemInfo(), WorkbenchLog.None);
            await supervisor.InitializeAsync();
            supervisor.PollStateFile();
            Assert.Equal("실행기: 연결됨 · laptop · 다른 곳에서 켠 것", supervisor.View.Text);
            Assert.Equal(0, supervisor.RequestStopForExit(RunnerSupervisor.ExitAckWait).RequestsWritten);
            Assert.True(RunnerProtocol.ParseStatus((await runner.RunAsync(["runner", "status", "--json"], TimeSpan.FromSeconds(30))).StdOut)!.Running);
        }
        finally { await runner.RunAsync(["runner", "stop", "--instance", started.Instance!, "--json"], TimeSpan.FromSeconds(30)); }
    }

    [Fact]
    public async Task OldHubAndFailingStatusAreUnverifiable()
    {
        var store = new SettingsStore(temp.Sub("설정"));
        store.Load();
        store.Update(s => s with { Server = "https://himari.example", Hub = FakeHubExe.Command(work), HubHome = home });
        var supervisor = new RunnerSupervisor(store, s => new ProcessHubCommandRunner(s.Hub!, s.EffectiveHubHome), new WindowsSystemInfo(), WorkbenchLog.None);
        Configure(new { OldHub = true });
        await supervisor.InitializeAsync();
        Assert.Equal(RunnerPhase.Unverifiable, supervisor.View.Phase);
        Assert.Contains("unrecognized arguments", supervisor.View.Detail);
        Configure(new { StatusFail = true });
        await supervisor.RefreshAsync();
        Assert.Equal("실행기: 확인 불가 · fake hub: status failed Authorization: …", supervisor.View.Text);
        Assert.Null(store.Current.OwnedInstance);
        Assert.Null(store.Current.PendingInstance);
    }
}
