using System.IO;

namespace Himari.Workbench;

public enum HubCheckStage { Ok, ExecutableMissing, NotHubFolder, OldHub, CommandFailed, TimedOut, Busy }

/// <summary>What the first-run wizard found when it ran <c>runner status --json</c> (§5.6 step 3).</summary>
public sealed record HubCheck(HubCheckStage Stage, string Message, StatusReply? Status = null);

/// <summary>The wizard's defaults and checks (§5.6), kept apart from the window so they can be tested.</summary>
public static class HubSetup
{
    /// <summary>Generous: the first <c>uv run</c> may install the hub's dependencies.</summary>
    public static readonly TimeSpan FirstCheckTimeout = TimeSpan.FromMinutes(3);

    /// <summary><c>uv</c> found on <c>PATH</c> now, as an absolute path to store (later runs never search <c>PATH</c>).</summary>
    public static string? FindOnPath(string name = "uv.exe", string? path = null)
    {
        foreach (string folder in (path ?? Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            string trimmed = folder.Trim().Trim('"');
            if (trimmed.Length == 0 || !Path.IsPathFullyQualified(trimmed)) continue;
            string candidate = Path.Combine(trimmed, name);
            if (File.Exists(candidate)) return Path.GetFullPath(candidate);
        }
        return null;
    }

    public static bool IsHubFolder(string? folder) =>
        !string.IsNullOrWhiteSpace(folder) && Directory.Exists(folder) && File.Exists(Path.Combine(folder, "pyproject.toml"));

    /// <summary>The default hub command: <c>uv run --project &lt;hub folder&gt; himari-hub</c>, working in the hub folder.</summary>
    public static HubCommand UvCommand(string uv, string hubFolder) => new(uv, ["run", "--project", hubFolder, "himari-hub"], hubFolder);

    public static bool UsesUv(HubCommand command) =>
        string.Equals(Path.GetFileName(command.Executable), "uv.exe", StringComparison.OrdinalIgnoreCase)
        && command.Arguments.Length >= 3 && command.Arguments[0] == "run" && command.Arguments[1] == "--project";

    public static HubCheck Classify(HubCommand command, HubResult result)
    {
        if (result.Outcome == HubOutcome.NotFound) return new(HubCheckStage.ExecutableMissing, $"실행 파일이 없습니다: {command.Executable}");
        if (UsesUv(command) && !IsHubFolder(command.Arguments[2]))
            return new(HubCheckStage.NotHubFolder, $"hub 폴더가 아닙니다(pyproject.toml이 없음): {command.Arguments[2]}");
        if (result.Outcome == HubOutcome.TimedOut) return new(HubCheckStage.TimedOut, "hub 명령이 제시간에 끝나지 않았습니다");
        if (result is { Outcome: HubOutcome.Exited, ExitCode: 0 } && RunnerProtocol.ParseStatus(result.StdOut) is { } status)
            return new(HubCheckStage.Ok, "hub 명령이 동작합니다", status);
        // The hub works but a runner was starting or stopping just then: try again, the command is fine.
        if (result.Outcome == HubOutcome.Exited && RunnerProtocol.StatusError(result.StdOut) is ("busy", var why))
            return new(HubCheckStage.Busy, "실행기가 켜지거나 꺼지는 중이라 지금은 확인하지 못했습니다. 잠시 뒤 다시 하세요" + (why is { Length: > 0 } ? $" ({Redaction.LastLine(why)})" : ""));
        string line = Redaction.LastLine(result.StdErr);
        string all = result.StdErr + "\n" + result.StdOut;
        if (all.Contains("unrecognized arguments", StringComparison.OrdinalIgnoreCase) || all.Contains("invalid choice", StringComparison.OrdinalIgnoreCase)
            || all.Contains("No such option", StringComparison.OrdinalIgnoreCase) || result.Outcome == HubOutcome.Exited && result.ExitCode == 0)
            return new(HubCheckStage.OldHub, "runner status --json이 없는 옛 hub입니다. hub를 새로 받으세요" + (line.Length > 0 ? $" ({line})" : ""));
        return new(HubCheckStage.CommandFailed, UsesUv(command)
            ? $"hub 명령이 실패했습니다(의존성 설치 실패일 수 있음): {(line.Length > 0 ? line : $"종료 코드 {result.ExitCode}")}"
            : $"hub 명령이 실패했습니다: {(line.Length > 0 ? line : $"종료 코드 {result.ExitCode}")}");
    }

    /// <summary>Step 4: what the registration means for starting the runner.</summary>
    public static string? RegistrationMessage(StatusReply status) => status.Registration switch
    {
        Registration.Ok => null,
        Registration.Missing => "이 기기는 아직 실행기로 등록되지 않았습니다. 터미널에서 himari-hub runner setup을 먼저 하세요",
        Registration.Broken => $"실행기 등록에 문제가 있습니다: {status.Why ?? "이유 모름"}",
        _ => "hub가 실행기 등록 상태를 알려 주지 않았습니다",
    };
}
