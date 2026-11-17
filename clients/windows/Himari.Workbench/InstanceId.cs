using System.IO;
using System.Security.Cryptography;

namespace Himari.Workbench;

/// <summary>The boot, the uptime and the wall clock. Faked in the tests.</summary>
public interface ISystemInfo
{
    /// <summary>Windows <c>BootId</c>: rises with every boot.</summary>
    long BootId { get; }
    /// <summary>Milliseconds since boot (<c>GetTickCount64</c>): unaffected by clock changes.</summary>
    long UptimeMs { get; }
    DateTimeOffset Now { get; }
}

public sealed class WindowsSystemInfo : ISystemInfo
{
    private const string PrefetchKey = @"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters";
    private readonly Lazy<long> bootId = new(ReadBootId);
    public long BootId => bootId.Value;
    public long UptimeMs => Environment.TickCount64;
    public DateTimeOffset Now => DateTimeOffset.Now;

    private static long ReadBootId()
    {
        using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(PrefetchKey);
        return key?.GetValue("BootId") is int value ? (uint)value
            : throw new InvalidOperationException("Windows의 부팅 번호(BootId)를 읽지 못했습니다");
    }
}

/// <summary>
/// The runner instance id the app makes (§5.3): <c>&lt;boot id&gt;.&lt;deadline&gt;.&lt;GUID&gt;</c>, the deadline being the
/// uptime in milliseconds 30 seconds after the id was made. A runner that takes the lock after the deadline, or in a
/// later boot, ends at once without writing anything, so after the deadline no runner can come up with this id.
/// </summary>
public readonly record struct InstanceId(long BootId, long DeadlineMs, string Guid)
{
    public static readonly TimeSpan Lifetime = TimeSpan.FromSeconds(30);

    public override string ToString() => $"{BootId}.{DeadlineMs}.{Guid}";

    public static InstanceId Create(ISystemInfo system) =>
        new(system.BootId, system.UptimeMs + (long)Lifetime.TotalMilliseconds, System.Guid.NewGuid().ToString("N"));

    public static bool TryParse(string? text, out InstanceId id)
    {
        id = default;
        if (string.IsNullOrEmpty(text) || text.Length > 128) return false;
        string[] parts = text.Split('.');
        if (parts.Length != 3 || !Digits(parts[0]) || !Digits(parts[1]) || parts[2].Length == 0
            || !parts[2].All(c => char.IsAsciiHexDigit(c) || c == '-')) return false;
        if (!long.TryParse(parts[0], out long boot) || !long.TryParse(parts[1], out long deadline)) return false;
        id = new InstanceId(boot, deadline, parts[2]);
        return true;
    }

    /// <summary>True once no runner can ever come up with this id: another boot, or the deadline has passed.</summary>
    public bool CannotStart(ISystemInfo system) => BootId != system.BootId || system.UptimeMs >= DeadlineMs;

    private static bool Digits(string text) => text.Length is > 0 and <= 19 && text.All(char.IsAsciiDigit);
}

/// <summary>Stop requests (H15): one new file per request, never overwriting another request.</summary>
public static class StopRequests
{
    public static string Folder(string hubHome) => Path.Combine(hubHome, "runner.stop.d");

    /// <summary>Creates <c>runner.stop.d\&lt;instance&gt;.&lt;random&gt;</c> and returns its file name.</summary>
    public static string Write(string hubHome, string instance)
    {
        if (!InstanceId.TryParse(instance, out _)) throw new ArgumentException("not an instance id", nameof(instance));
        string folder = Folder(hubHome);
        Directory.CreateDirectory(folder);
        for (int attempt = 0; ; attempt++)
        {
            string name = $"{instance}.{RandomNumberGenerator.GetHexString(32, lowercase: true)}";
            try
            {
                using var file = new FileStream(Path.Combine(folder, name), FileMode.CreateNew, FileAccess.Write, FileShare.None);
                file.Flush(flushToDisk: true);
                return name;
            }
            catch (IOException) when (attempt < 3 && File.Exists(Path.Combine(folder, name))) { }
        }
    }
}
