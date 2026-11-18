using System.IO;

namespace Himari.Workbench;

/// <summary>
/// <c>%LOCALAPPDATA%\HIMARI\workbench\workbench.log</c> (§5.1): what the app did with the runner, hub exit codes and
/// masked failure lines only. Nothing here is fatal.
/// </summary>
public sealed class WorkbenchLog(string? path)
{
    private const long MaxBytes = 1024 * 1024;
    private readonly object gate = new();

    public static WorkbenchLog Default() => new(Path.Combine(SettingsStore.DefaultDirectory, "workbench", "workbench.log"));
    public static readonly WorkbenchLog None = new(null);

    public void Write(string message)
    {
        if (path is null) return;
        lock (gate)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                if (File.Exists(path) && new FileInfo(path).Length > MaxBytes) File.Move(path, path + ".1", overwrite: true);
                File.AppendAllText(path, $"{DateTimeOffset.Now:yyyy-MM-dd HH:mm:ss.fff zzz} {Redaction.Mask(message)}{Environment.NewLine}");
            }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
        }
    }
}
