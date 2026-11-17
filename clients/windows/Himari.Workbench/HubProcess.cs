using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;

namespace Himari.Workbench;

/// <summary>The hub command from the settings (§5.1): an absolute executable, an argument list and a working folder.</summary>
public sealed record HubCommand(string Executable, string[] Arguments, string WorkingDirectory)
{
    public bool SameAs(HubCommand? other) => other is not null && string.Equals(Executable, other.Executable, StringComparison.OrdinalIgnoreCase)
        && Arguments.SequenceEqual(other.Arguments) && string.Equals(WorkingDirectory, other.WorkingDirectory, StringComparison.OrdinalIgnoreCase);
}

public enum HubOutcome { Exited, NotFound, TimedOut, Failed }

/// <summary>What one hub command did: its exit, and its standard output and error as text.</summary>
public sealed record HubResult(HubOutcome Outcome, int? ExitCode, string StdOut, string StdErr)
{
    /// <summary>The failure line the app shows and logs (§5.1): the last stderr line, masked, at most 200 characters.</summary>
    public string Describe() => Outcome switch
    {
        HubOutcome.NotFound => "hub 명령의 실행 파일을 찾을 수 없습니다",
        HubOutcome.TimedOut => "hub 명령이 제시간에 끝나지 않았습니다",
        HubOutcome.Failed => Redaction.LastLine(StdErr) is { Length: > 0 } line ? line : "hub 명령을 실행하지 못했습니다",
        _ => Redaction.LastLine(StdErr) is { Length: > 0 } line ? line : $"hub 명령이 실패했습니다(종료 코드 {ExitCode})",
    };
}

/// <summary>Runs the hub command with extra arguments. Faked in the tests.</summary>
public interface IHubCommandRunner
{
    Task<HubResult> RunAsync(IReadOnlyList<string> arguments, TimeSpan timeout, CancellationToken cancel = default);
}

/// <summary>
/// Runs the hub command as a process: no shell, an argument list, no window (§5.1). The hub home goes in
/// <c>HIMARI_HUB_HOME</c>; no token is ever put in arguments or the environment.
/// </summary>
public sealed class ProcessHubCommandRunner(HubCommand command, string hubHome) : IHubCommandRunner
{
    // A detached runner may keep the inherited output pipe open after `runner start` returns, so the end of the
    // stream is waited for only this long after the command itself has exited.
    private static readonly TimeSpan StreamGrace = TimeSpan.FromSeconds(2);

    public async Task<HubResult> RunAsync(IReadOnlyList<string> arguments, TimeSpan timeout, CancellationToken cancel = default)
    {
        var info = new ProcessStartInfo(command.Executable)
        {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = command.WorkingDirectory,
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = true,
            StandardOutputEncoding = new UTF8Encoding(false), StandardErrorEncoding = new UTF8Encoding(false),
        };
        foreach (string argument in command.Arguments) info.ArgumentList.Add(argument);
        foreach (string argument in arguments) info.ArgumentList.Add(argument);
        info.Environment["HIMARI_HUB_HOME"] = hubHome;
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        using var process = new Process { StartInfo = info, EnableRaisingEvents = true };
        var exited = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        process.Exited += (_, _) => exited.TrySetResult();
        try
        {
            if (!File.Exists(command.Executable)) return new HubResult(HubOutcome.NotFound, null, "", "");
            if (!Directory.Exists(command.WorkingDirectory)) return new HubResult(HubOutcome.Failed, null, "", "hub 명령의 작업 폴더가 없습니다");
            process.Start();
        }
        catch (Win32Exception) { return new HubResult(HubOutcome.NotFound, null, "", ""); }
        catch (Exception ex) when (ex is InvalidOperationException or IOException or UnauthorizedAccessException)
        { return new HubResult(HubOutcome.Failed, null, "", "hub 명령을 실행하지 못했습니다"); }
        if (process.HasExited) exited.TrySetResult();
        try { process.StandardInput.Close(); } catch (IOException) { }
        var stdout = new StringBuilder(); var stderr = new StringBuilder();
        Task outRead = Pump(process.StandardOutput, stdout); Task errRead = Pump(process.StandardError, stderr);
        Task finished = await Task.WhenAny(exited.Task, Task.Delay(timeout, cancel)).ConfigureAwait(false);
        if (finished != exited.Task)
        {
            try { process.Kill(entireProcessTree: true); } catch (Exception ex) when (ex is InvalidOperationException or Win32Exception) { }
            return new HubResult(HubOutcome.TimedOut, null, Snapshot(stdout), Snapshot(stderr));
        }
        await Task.WhenAny(Task.WhenAll(outRead, errRead), Task.Delay(StreamGrace)).ConfigureAwait(false);
        return new HubResult(HubOutcome.Exited, process.ExitCode, Snapshot(stdout), Snapshot(stderr));
    }

    private static async Task Pump(StreamReader reader, StringBuilder into)
    {
        var buffer = new char[4096];
        try
        {
            int read;
            while ((read = await reader.ReadAsync(buffer).ConfigureAwait(false)) > 0)
                lock (into) { if (into.Length < 1_000_000) into.Append(buffer, 0, read); }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException) { }
    }

    private static string Snapshot(StringBuilder text) { lock (text) return text.ToString(); }
}
