using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Himari.Workbench;

/// <summary>
/// <c>%LOCALAPPDATA%\HIMARI\workbench.json</c> (§6, §7): the server address, the hub command and home, the switches,
/// and which runner instance the app owns or is starting. No token or secret is ever stored here.
/// </summary>
public sealed record WorkbenchSettings
{
    public int Version { get; init; } = 1;
    public string? Server { get; init; }
    public HubCommand? Hub { get; init; }
    public string? HubHome { get; init; }
    /// <summary>"앱과 함께 실행기 켜고 끄기" (§5.3), on by default.</summary>
    public bool Linked { get; init; } = true;
    /// <summary>Loopback <c>http</c> server addresses and the WebView2 developer tools (§4).</summary>
    public bool DeveloperMode { get; init; }
    public bool TrayNoticeShown { get; init; }
    public string? OwnedInstance { get; init; }
    public string? PendingInstance { get; init; }
    /// <summary>The owned instance the app asked to stop when it last exited (§5.3: "끄기를 요청했지만 아직 떠 있음").</summary>
    public string? ExitStopRequested { get; init; }

    public static string DefaultHubHome() =>
        Environment.GetEnvironmentVariable("HIMARI_HUB_HOME") is { Length: > 0 } home && Path.IsPathFullyQualified(home) ? home
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".himari-hub");

    [JsonIgnore]
    public string EffectiveHubHome => string.IsNullOrWhiteSpace(HubHome) ? DefaultHubHome() : HubHome;

    /// <summary>Complete enough to run: a server address and a hub command.</summary>
    [JsonIgnore]
    public bool IsComplete => !string.IsNullOrWhiteSpace(Server) && Hub is { Executable.Length: > 0, WorkingDirectory.Length: > 0 };
}

public enum SettingsLoadKind { Missing, Ok, Broken, Migrated, Unreadable }

public sealed record SettingsLoad(SettingsLoadKind Kind, WorkbenchSettings? Settings, string? MovedTo = null, string? Error = null);

/// <summary>
/// Reads and writes <c>workbench.json</c>. Only the process holding the single-instance mutex uses it (§3); within the
/// process every write goes through one lock and replaces the file atomically (§7).
/// </summary>
public sealed class SettingsStore(string directory)
{
    public const string FileName = "workbench.json";
    public const string MigratedSuffix = ".migrated";

    internal static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower, WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.Never,
        // Paths with Hangul stay readable in the file.
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.Create(System.Text.Unicode.UnicodeRanges.All),
    };

    private readonly object gate = new();
    private WorkbenchSettings current = new();

    public static string DefaultDirectory => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HIMARI");
    public string Directory { get; } = directory;
    public string PathName => Path.Combine(Directory, FileName);

    public WorkbenchSettings Current { get { lock (gate) return current; } }

    /// <summary>
    /// §8: after the merged app has taken over, <c>workbench.json</c> is gone and <c>workbench.json.migrated</c> is there.
    /// While both exist <c>workbench.json</c> is the source, so the old app still runs.
    /// </summary>
    public bool IsMigrated => !File.Exists(PathName) && File.Exists(PathName + MigratedSuffix);

    public SettingsLoad Load()
    {
        lock (gate)
        {
            if (IsMigrated) return new SettingsLoad(SettingsLoadKind.Migrated, null);
            string text;
            try { text = File.ReadAllText(PathName); }
            catch (Exception ex) when (ex is FileNotFoundException or DirectoryNotFoundException) { return new SettingsLoad(SettingsLoadKind.Missing, null); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { return new SettingsLoad(SettingsLoadKind.Unreadable, null, Error: ex.Message); }
            WorkbenchSettings? value = null;
            try { value = JsonSerializer.Deserialize<WorkbenchSettings>(text, Json); }
            catch (Exception ex) when (ex is JsonException or NotSupportedException or ArgumentException) { }
            if (value is null || value.Hub is { } hub && (hub.Executable is null || hub.Arguments is null || hub.WorkingDirectory is null))
            {
                try { return new SettingsLoad(SettingsLoadKind.Broken, null, MoveAside()); }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { return new SettingsLoad(SettingsLoadKind.Unreadable, null, Error: ex.Message); }
            }
            current = value;
            return new SettingsLoad(SettingsLoadKind.Ok, value);
        }
    }

    /// <summary>Applies a change under the lock and writes it atomically; returns the saved value.</summary>
    public WorkbenchSettings Update(Func<WorkbenchSettings, WorkbenchSettings> change)
    {
        lock (gate)
        {
            WorkbenchSettings next = change(current);
            if (next == current) return current;
            Write(next);
            current = next;
            return next;
        }
    }

    /// <summary>Like <see cref="Update"/>, but the change may decline (return null) after looking at the value under the lock.</summary>
    public bool TryUpdate(Func<WorkbenchSettings, WorkbenchSettings?> change)
    {
        lock (gate)
        {
            WorkbenchSettings? next = change(current);
            if (next is null) return false;
            if (next != current) { Write(next); current = next; }
            return true;
        }
    }

    private void Write(WorkbenchSettings value)
    {
        System.IO.Directory.CreateDirectory(Directory);
        string temp = Path.Combine(Directory, $"{FileName}.{Guid.NewGuid():N}.tmp");
        try
        {
            using (var stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                JsonSerializer.Serialize(stream, value, Json);
                stream.Flush(flushToDisk: true);
            }
            if (File.Exists(PathName)) File.Replace(temp, PathName, null, ignoreMetadataErrors: true);
            else File.Move(temp, PathName);
        }
        finally { if (File.Exists(temp)) File.Delete(temp); }
    }

    /// <summary>A broken file is never overwritten: it is moved to <c>workbench.json.broken-&lt;time&gt;</c>.</summary>
    private string MoveAside()
    {
        string stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss", System.Globalization.CultureInfo.InvariantCulture);
        string target = $"{PathName}.broken-{stamp}";
        for (int n = 2; File.Exists(target); n++) target = $"{PathName}.broken-{stamp}-{n}";
        File.Move(PathName, target);
        return target;
    }
}
