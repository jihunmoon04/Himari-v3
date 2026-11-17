using System.IO;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Himari.Workbench;

public enum Registration { Unknown, Ok, Missing, Broken }

/// <summary><c>runner status --json</c> (H14).</summary>
public sealed record StatusReply(bool Running, string? Instance, int? Pid, string? StartedAt, Registration Registration, string? Why);

public enum StartError { None, Running, Failed, Cancelled, Unconfirmed, Unreadable }

/// <summary><c>runner start --instance … --json</c> (H10): success, or one settled error.</summary>
public sealed record StartReply(StartError Error, string? Instance, int? Pid, string? StartedAt, int? ExitCode);

public enum StopResult { Unreadable, Stopped, Killed, NotRunning, OtherInstance, Unconfirmed, RunnerKilled }

public enum StateFileKind { Missing, Broken, Ok }

/// <summary><c>&lt;hub home&gt;\runner.state.json</c> (H9).</summary>
public sealed record RunnerStateFile(string? Instance, int? Pid, string? StartedAt, DateTimeOffset? UpdatedAt, string? State,
    string? StopAck, string? Name, string? Server, string? Error)
{
    public static readonly TimeSpan FreshFor = TimeSpan.FromSeconds(15);
    public bool IsFresh(DateTimeOffset now) => UpdatedAt is { } at && now - at <= FreshFor && at - now <= FreshFor;
}

public sealed record StateFileRead(StateFileKind Kind, RunnerStateFile? File);

/// <summary>The hub's JSON and file contract (§5.2–§5.4, §9), read leniently: anything unreadable is a failure, never a guess.</summary>
public static class RunnerProtocol
{
    public const string StateFileName = "runner.state.json";

    public static StatusReply? ParseStatus(string stdout)
    {
        using var json = ParseObject(stdout);
        if (json is null || !json.RootElement.TryGetProperty("running", out var running) || running.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return null;
        var root = json.RootElement;
        var registration = String(root, "registration") switch
        {
            "ok" => Registration.Ok, "missing" => Registration.Missing, "broken" => Registration.Broken, _ => Registration.Unknown,
        };
        // H14 names six keys; a reply missing one, or with a value of the wrong kind, or without a known registration
        // is incomplete: a failed status, never "off" (§5.2).
        if (registration == Registration.Unknown || !Has(root, "instance", JsonValueKind.String) || !Has(root, "pid", JsonValueKind.Number)
            || !Has(root, "started_at", JsonValueKind.String) || !Has(root, "why", JsonValueKind.String)) return null;
        string? instance = String(root, "instance");
        if (instance is not null && !InstanceId.TryParse(instance, out _)) instance = null;
        return new StatusReply(running.GetBoolean(), running.GetBoolean() ? instance : null, Int(root, "pid"), String(root, "started_at"), registration, String(root, "why"));
    }

    /// <summary>
    /// A status the hub answered with an error object instead (hub B1: <c>{"error": "busy", "why": …}</c> when
    /// runner.transition.lock stays taken, exit code 1): the error and its reason, or null.
    /// </summary>
    public static (string Error, string? Why)? StatusError(string stdout)
    {
        using var json = ParseObject(stdout);
        return json is not null && String(json.RootElement, "error") is { } error ? (error, String(json.RootElement, "why")) : null;
    }

    public static StartReply ParseStart(string stdout)
    {
        using var json = ParseObject(stdout);
        if (json is null) return new StartReply(StartError.Unreadable, null, null, null, null);
        var root = json.RootElement;
        if (String(root, "error") is { } error)
            return new StartReply(error switch
            {
                "running" => StartError.Running, "cancelled" => StartError.Cancelled, "unconfirmed" => StartError.Unconfirmed, _ => StartError.Failed,
            }, String(root, "instance"), Int(root, "pid"), null, Int(root, "exit_code"));
        // Success is the whole H10 result: the instance, its pid and its started_at (§5.3); anything less is unreadable.
        string? instance = String(root, "instance");
        int? pid = Int(root, "pid");
        string? startedAt = String(root, "started_at");
        return instance is null || pid is null || startedAt is null ? new StartReply(StartError.Unreadable, null, null, null, null)
            : new StartReply(StartError.None, instance, pid, startedAt, null);
    }

    public static StopResult ParseStop(string stdout)
    {
        using var json = ParseObject(stdout);
        if (json is null) return StopResult.Unreadable;
        string? value = String(json.RootElement, "result") ?? String(json.RootElement, "error");
        return value switch
        {
            "stopped" => StopResult.Stopped, "killed" => StopResult.Killed, "not_running" => StopResult.NotRunning,
            "other_instance" => StopResult.OtherInstance, "unconfirmed" => StopResult.Unconfirmed, "runner_killed" => StopResult.RunnerKilled,
            _ => StopResult.Unreadable,
        };
    }

    /// <summary>Reads the state file without touching any lock (§5.2). A file being replaced reads as broken this time.</summary>
    public static StateFileRead ReadStateFile(string hubHome)
    {
        string path = Path.Combine(hubHome, StateFileName);
        string text;
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            if (stream.Length > 64 * 1024) return new StateFileRead(StateFileKind.Broken, null);
            using var reader = new StreamReader(stream);
            text = reader.ReadToEnd();
        }
        catch (Exception ex) when (ex is FileNotFoundException or DirectoryNotFoundException) { return new StateFileRead(StateFileKind.Missing, null); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { return new StateFileRead(StateFileKind.Broken, null); }
        // The hub writes the whole file as one object (H9): nothing else in it is read.
        using var json = Object(text.Trim());
        if (json is null) return new StateFileRead(StateFileKind.Broken, null);
        var root = json.RootElement;
        DateTimeOffset? updated = String(root, "updated_at") is { } at && DateTimeOffset.TryParse(at, System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.AssumeUniversal, out var parsed) ? parsed : null;
        // H9 names nine keys: a file without one of them, with a value of the wrong kind, or an unknown state is broken
        // (shown as "연결 모름", §5.2), even when its instance matches.
        string? instance = String(root, "instance");
        if (instance is null || !InstanceId.TryParse(instance, out _) || updated is null || Int(root, "pid") is null || String(root, "started_at") is null
            || String(root, "state") is not ("starting" or "connected" or "disconnected" or "stopping")
            || !Has(root, "stop_ack", JsonValueKind.String) || !Has(root, "name", JsonValueKind.String)
            || !Has(root, "server", JsonValueKind.String) || !Has(root, "error", JsonValueKind.String))
            return new StateFileRead(StateFileKind.Broken, null);
        return new StateFileRead(StateFileKind.Ok, new RunnerStateFile(instance, Int(root, "pid"), String(root, "started_at"), updated,
            String(root, "state"), String(root, "stop_ack"), String(root, "name"), String(root, "server"), String(root, "error")));
    }

    /// <summary>
    /// The whole output as one object, or else its last non-empty line as one (a launcher such as <c>uv</c> may print
    /// before it). Never an earlier line: output that ends in a broken or cut-off object is unreadable (§5.2).
    /// </summary>
    private static JsonDocument? ParseObject(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        if (Object(text.Trim()) is { } whole) return whole;
        string last = text.Split('\n').Select(line => line.Trim()).Last(line => line.Length > 0);
        return Object(last);
    }

    /// <summary>The text as exactly one JSON object, or null.</summary>
    private static JsonDocument? Object(string candidate)
    {
        try
        {
            var json = JsonDocument.Parse(candidate);
            if (json.RootElement.ValueKind == JsonValueKind.Object) return json;
            json.Dispose();
        }
        catch (JsonException) { }
        return null;
    }

    private static string? String(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    /// <summary>The key is there, with a value of this kind or null.</summary>
    private static bool Has(JsonElement root, string name, JsonValueKind kind) =>
        root.TryGetProperty(name, out var value) && (value.ValueKind == kind || value.ValueKind == JsonValueKind.Null);

    private static int? Int(JsonElement root, string name) =>
        root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int number) ? number : null;
}

/// <summary>Hides credentials in hub output before it is shown or logged (§5.1).</summary>
public static partial class Redaction
{
    public const int MaxLine = 200;

    public static string Mask(string text)
    {
        text = AuthorizationValue().Replace(text, "$1…");
        text = BearerValue().Replace(text, "$1…");
        return TokenValue().Replace(text, "$1…");
    }

    /// <summary>The last non-empty line of the output, masked, then cut to 200 characters.</summary>
    public static string LastLine(string output)
    {
        string? line = output.Split('\n').Select(part => part.Trim()).LastOrDefault(part => part.Length > 0);
        if (line is null) return "";
        line = Mask(line);
        return line.Length <= MaxLine ? line : line[..(MaxLine - 1)] + "…";
    }

    // "Authorization: x", "Authorization=x", "'Authorization': 'x'" and "Authorization x" all hide x (§5.1).
    [GeneratedRegex(@"(?i)(authorization[""']?(?:\s*[:=]\s*|\s+)[""']?)(?:(?:bearer|basic|token)\s+)?[^\s""',;]+")]
    private static partial Regex AuthorizationValue();
    // A value may be quoted: bearer "x", token="x" and token='x' hide x too.
    [GeneratedRegex(@"(?i)(bearer\s+[""']?)(?!…)[^\s""',;]+")]
    private static partial Regex BearerValue();
    [GeneratedRegex(@"(?i)(token=[""']?)(?!…)[^\s""'&,;]+")]
    private static partial Regex TokenValue();
}
