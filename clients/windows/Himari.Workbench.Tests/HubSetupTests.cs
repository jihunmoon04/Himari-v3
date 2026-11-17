using Himari.Workbench;

namespace Himari.Workbench.Tests;

/// <summary>§5.6: the wizard's defaults and how a failed <c>runner status --json</c> is explained.</summary>
public sealed class HubSetupTests : IDisposable
{
    private readonly TempFolder temp = new();
    public void Dispose() => temp.Dispose();

    [Fact]
    public void UvIsFoundOnPathAndStoredAbsolute()
    {
        string bin = temp.Sub("도구 bin");
        File.WriteAllText(Path.Combine(bin, "uv.exe"), "");
        string path = string.Join(Path.PathSeparator, "relative\\dir", "", temp.Sub("빈 폴더"), $"\"{bin}\"");
        Assert.Equal(Path.Combine(bin, "uv.exe"), HubSetup.FindOnPath(path: path));
        Assert.Null(HubSetup.FindOnPath(path: temp.Sub("다른 폴더")));
    }

    [Fact]
    public void DefaultCommandIsUvRunProject()
    {
        string hub = temp.Sub("Himari v4");
        var command = HubSetup.UvCommand(@"C:\tools\uv.exe", hub);
        Assert.Equal(new[] { "run", "--project", hub, "himari-hub" }, command.Arguments);
        Assert.Equal(hub, command.WorkingDirectory);
        Assert.True(HubSetup.UsesUv(command));
    }

    [Fact]
    public void FailuresAreExplainedByStage()
    {
        string hub = temp.Sub("hub");
        var uv = HubSetup.UvCommand(@"C:\tools\uv.exe", hub);
        Assert.Equal(HubCheckStage.ExecutableMissing, HubSetup.Classify(uv, new HubResult(HubOutcome.NotFound, null, "", "")).Stage);
        Assert.Equal(HubCheckStage.NotHubFolder, HubSetup.Classify(uv, new HubResult(HubOutcome.Exited, 2, "", "error: No `pyproject.toml` found")).Stage);
        File.WriteAllText(Path.Combine(hub, "pyproject.toml"), "");
        Assert.Equal(HubCheckStage.OldHub, HubSetup.Classify(uv, new HubResult(HubOutcome.Exited, 2, "", "himari-hub runner: error: unrecognized arguments: --json")).Stage);
        var failed = HubSetup.Classify(uv, new HubResult(HubOutcome.Exited, 1, "", "  × Failed to build `himari-hub`\n  token=abc"));
        Assert.Equal(HubCheckStage.CommandFailed, failed.Stage);
        Assert.Contains("의존성 설치 실패", failed.Message);
        Assert.Contains("token=…", failed.Message);
        Assert.DoesNotContain("abc", failed.Message);
        // A complete reply from a command that failed is still a failure (§5.2).
        Assert.NotEqual(HubCheckStage.Ok, HubSetup.Classify(uv, new HubResult(HubOutcome.Exited, 1, "{\"running\": false, \"registration\": \"ok\"}", "boom")).Stage);
        Assert.Equal(HubCheckStage.TimedOut, HubSetup.Classify(uv, new HubResult(HubOutcome.TimedOut, null, "", "")).Stage);
        var ok = HubSetup.Classify(uv, new HubResult(HubOutcome.Exited, 0, "{\"running\": false, \"registration\": \"missing\"}", ""));
        Assert.Equal(HubCheckStage.Ok, ok.Stage);
        Assert.Contains("runner setup", HubSetup.RegistrationMessage(ok.Status!));
        Assert.Null(HubSetup.RegistrationMessage(ok.Status! with { Registration = Registration.Ok }));
    }
}
