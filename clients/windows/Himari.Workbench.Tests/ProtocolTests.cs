using Himari.Workbench;

namespace Himari.Workbench.Tests;

public sealed class ProtocolTests
{
    [Fact]
    public void InstanceIdHasBootDeadlineAndGuid()
    {
        var system = new FakeSystem { BootId = 58, Offset = 10_000 };
        var id = InstanceId.Create(system);
        Assert.Equal(58, id.BootId);
        Assert.InRange(id.DeadlineMs, 40_000, 41_000);
        Assert.Matches("^58\\.\\d+\\.[0-9a-f]{32}$", id.ToString());
        Assert.True(InstanceId.TryParse(id.ToString(), out var parsed));
        Assert.Equal(id, parsed);
        Assert.False(parsed.CannotStart(system));
        system.Offset += 31_000;
        Assert.True(parsed.CannotStart(system));
        Assert.True(parsed.CannotStart(new FakeSystem { BootId = 59, Offset = 0 }));
    }

    [Theory]
    [InlineData("")]
    [InlineData("1.2")]
    [InlineData("1.2.3.4")]
    [InlineData("a.2.abc")]
    [InlineData("1.-2.abc")]
    [InlineData("1.2.ab/c")]
    [InlineData("1.2.")]
    [InlineData("1.2.abc")]
    [InlineData("1.2.0123456789abcdef0123456789abcdeg")]
    public void MalformedIdsAreRejected(string text) => Assert.False(InstanceId.TryParse(text, out _));

    [Fact]
    public void StatusIsParsed()
    {
        var reply = RunnerProtocol.ParseStatus("""{"running": true, "instance": "7.5.abc00000000000000000000000000000", "pid": 12, "started_at": "x", "registration": "ok", "why": null}""");
        Assert.NotNull(reply);
        Assert.True(reply.Running);
        Assert.Equal("7.5.abc00000000000000000000000000000", reply.Instance);
        Assert.Equal(12, reply.Pid);
        Assert.Equal(Registration.Ok, reply.Registration);
        var broken = RunnerProtocol.ParseStatus("""{"running": false, "instance": null, "pid": null, "started_at": null, "registration": "broken", "why": "토큰 파일이 비었음"}""");
        Assert.Equal(Registration.Broken, broken!.Registration);
        Assert.Equal("토큰 파일이 비었음", broken.Why);
    }

    [Theory]
    [InlineData("")]
    [InlineData("usage: himari-hub runner [-h]")]
    [InlineData("{\"instance\": null}")]
    [InlineData("[1, 2]")]
    [InlineData("{\"running\": \"yes\"}")]
    [InlineData("{\"running\": false}")]
    [InlineData("{\"running\": false, \"instance\": null, \"pid\": null, \"started_at\": null, \"registration\": \"later\", \"why\": null}")]
    [InlineData("{\"running\": false, \"registration\": \"ok\"}")]
    [InlineData("{\"running\": true, \"instance\": \"7.5.abc\", \"pid\": \"12\", \"started_at\": \"x\", \"registration\": \"ok\", \"why\": null}")]
    public void UnreadableStatusIsAFailure(string stdout) => Assert.Null(RunnerProtocol.ParseStatus(stdout));

    [Fact]
    public void OutputEndingInABrokenObjectIsUnreadableEvenAfterAGoodOne()
    {
        string good = "{\"running\": false, \"instance\": null, \"pid\": null, \"started_at\": null, \"registration\": \"ok\", \"why\": null}";
        Assert.Null(RunnerProtocol.ParseStatus(good + "\n{\"running\": tr"));
        Assert.Equal(StopResult.Unreadable, RunnerProtocol.ParseStop("{\"result\": \"stopped\"}\nTraceback (most recent call last):"));
    }

    [Fact]
    public void StatusAfterLauncherNoiseUsesTheLastObjectLine()
    {
        var reply = RunnerProtocol.ParseStatus("Resolved 12 packages\n{\"running\": false, \"instance\": null, \"pid\": null, \"started_at\": null, \"registration\": \"missing\", \"why\": null}\n");
        Assert.NotNull(reply);
        Assert.Equal(Registration.Missing, reply.Registration);
    }

    [Fact]
    public void AnInstanceThatIsNotAnIdIsTreatedAsUnknown()
    {
        var reply = RunnerProtocol.ParseStatus("""{"running": true, "instance": "../../x", "pid": 12, "started_at": "x", "registration": "ok", "why": null}""");
        Assert.Null(reply!.Instance);
    }

    [Fact]
    public void StartRepliesAreParsed()
    {
        Assert.Equal(StartError.None, RunnerProtocol.ParseStart("""{"instance": "7.5.abc00000000000000000000000000000", "pid": 3, "started_at": "t"}""").Error);
        var running = RunnerProtocol.ParseStart("""{"error": "running", "instance": "7.9.def00000000000000000000000000000"}""");
        Assert.Equal(StartError.Running, running.Error);
        Assert.Equal("7.9.def00000000000000000000000000000", running.Instance);
        Assert.Equal(3, RunnerProtocol.ParseStart("""{"error": "failed", "exit_code": 3}""").ExitCode);
        Assert.Equal(StartError.Cancelled, RunnerProtocol.ParseStart("""{"error": "cancelled"}""").Error);
        Assert.Equal(StartError.Unconfirmed, RunnerProtocol.ParseStart("""{"error": "unconfirmed"}""").Error);
        Assert.Equal(StartError.Failed, RunnerProtocol.ParseStart("""{"error": "something new"}""").Error);
        Assert.Equal(StartError.Unreadable, RunnerProtocol.ParseStart("nope").Error);
        // Success needs the whole result (§5.3): an instance alone does not move pending to owned.
        Assert.Equal(StartError.Unreadable, RunnerProtocol.ParseStart("""{"instance": "7.5.abc00000000000000000000000000000"}""").Error);
        Assert.Equal(StartError.Unreadable, RunnerProtocol.ParseStart("""{"instance": "7.5.abc00000000000000000000000000000", "pid": 3}""").Error);
    }

    [Theory]
    [InlineData("""{"result": "stopped"}""", StopResult.Stopped)]
    [InlineData("""{"result": "killed"}""", StopResult.Killed)]
    [InlineData("""{"result": "not_running"}""", StopResult.NotRunning)]
    [InlineData("""{"result": "other_instance"}""", StopResult.OtherInstance)]
    [InlineData("""{"result": "unconfirmed"}""", StopResult.Unconfirmed)]
    [InlineData("""{"result": "runner_killed"}""", StopResult.RunnerKilled)]
    [InlineData("""{"error": "unconfirmed"}""", StopResult.Unconfirmed)]
    [InlineData("""{"result": "maybe"}""", StopResult.Unreadable)]
    [InlineData("", StopResult.Unreadable)]
    public void StopResultsAreParsed(string stdout, StopResult expected) => Assert.Equal(expected, RunnerProtocol.ParseStop(stdout));

    [Fact]
    public void StateFileKinds()
    {
        using var temp = new TempFolder();
        Assert.Equal(StateFileKind.Missing, RunnerProtocol.ReadStateFile(temp.Path).Kind);
        Assert.Equal(StateFileKind.Missing, RunnerProtocol.ReadStateFile(Path.Combine(temp.Path, "없는 폴더")).Kind);
        string path = Path.Combine(temp.Path, RunnerProtocol.StateFileName);
        File.WriteAllText(path, "{\"instance\": ");
        Assert.Equal(StateFileKind.Broken, RunnerProtocol.ReadStateFile(temp.Path).Kind);
        File.WriteAllText(path, "{\"instance\": \"7.1.a\"}");
        Assert.Equal(StateFileKind.Broken, RunnerProtocol.ReadStateFile(temp.Path).Kind);
        // Fresh, the right instance and "connected", but without the other H9 keys: broken, never "연결됨" (§5.2).
        File.WriteAllText(path, $"{{\"instance\": \"7.1.{new string('a', 32)}\", \"updated_at\": \"{DateTimeOffset.Now:o}\", \"state\": \"connected\"}}");
        Assert.Equal(StateFileKind.Broken, RunnerProtocol.ReadStateFile(temp.Path).Kind);
        new ScriptedHub(temp.Path).WriteState("7.1.a0000000000000000000000000000000", "connected", "7.1.a0000000000000000000000000000000.ff", DateTimeOffset.Now.AddSeconds(-20));
        var read = RunnerProtocol.ReadStateFile(temp.Path);
        Assert.Equal(StateFileKind.Ok, read.Kind);
        Assert.Equal("7.1.a0000000000000000000000000000000.ff", read.File!.StopAck);
        Assert.False(read.File.IsFresh(DateTimeOffset.Now));
        Assert.True(read.File.IsFresh(DateTimeOffset.Now.AddSeconds(-10)));
    }

    [Theory]
    [InlineData("error: Authorization: Bearer abc.def.ghi", "error: Authorization: …")]
    [InlineData("bad header Authorization secret-value", "bad header Authorization …")]
    [InlineData("Authorization Basic dXNlcjpwYXNz", "Authorization …")]
    [InlineData("header {'Authorization': 'Bearer abc'}", "header {'Authorization': '…'}")]
    [InlineData("GET /x?token=s3cr3t&y=1 failed", "GET /x?token=…&y=1 failed")]
    [InlineData("config token=\"s3cr3t\" rejected", "config token=\"…\" rejected")]
    [InlineData("config token='s3cr3t'", "config token='…'")]
    [InlineData("header Bearer \"abc.def\"", "header Bearer \"…\"")]
    [InlineData("using bearer XYZ for now", "using bearer … for now")]
    [InlineData("plain failure", "plain failure")]
    public void SecretsAreMasked(string line, string expected) => Assert.Equal(expected, Redaction.Mask(line));

    [Fact]
    public void LastLineIsMaskedThenCutTo200()
    {
        Assert.Equal("second", Redaction.LastLine("first\r\nsecond\r\n\r\n"));
        string longLine = "token=" + new string('x', 400);
        Assert.Equal("token=…", Redaction.LastLine(longLine));
        string wide = new('가', 300);
        Assert.Equal(200, Redaction.LastLine(wide).Length);
        Assert.Equal("", Redaction.LastLine("  \n "));
    }

    [Fact]
    public void StopRequestsAreNewFilesEachTime()
    {
        using var temp = new TempFolder();
        string a = StopRequests.Write(temp.Path, "7.100.abc00000000000000000000000000000");
        string b = StopRequests.Write(temp.Path, "7.100.abc00000000000000000000000000000");
        Assert.NotEqual(a, b);
        Assert.Matches("^7\\.100\\.abc00000000000000000000000000000\\.[0-9a-f]{32}$", a);
        Assert.Equal(2, Directory.GetFiles(StopRequests.Folder(temp.Path)).Length);
        Assert.Throws<ArgumentException>(() => StopRequests.Write(temp.Path, "..\\evil"));
    }
}
