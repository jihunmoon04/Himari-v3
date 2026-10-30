using System.IO;
using System.Text.Json;

namespace Himari.Companion;

internal sealed record WindowPreferences(double Left, double Top, double CharacterHeight, bool MoveEnabled = true)
{
    internal static WindowPreferences Normalize(WindowPreferences value, double left, double top, double width, double height, double windowWidth, double windowHeight)
    {
        double x = double.IsFinite(value.Left) ? value.Left : left;
        double y = double.IsFinite(value.Top) ? value.Top : top;
        double size = double.IsFinite(value.CharacterHeight) ? Math.Clamp(value.CharacterHeight, 200, 400) : 330;
        // Keep the title and controls reachable after a monitor is removed.
        x = Math.Clamp(x, left, Math.Max(left, left + width - windowWidth));
        y = Math.Clamp(y, top, Math.Max(top, top + height - windowHeight));
        return new(x, y, size, value.MoveEnabled);
    }
    internal static string PathName => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HIMARI", "window.json");
    internal static WindowPreferences? Load()
    {
        try { return JsonSerializer.Deserialize<WindowPreferences>(File.ReadAllText(PathName)); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or NotSupportedException) { return null; }
    }
    internal void Save()
    {
        string directory = Path.GetDirectoryName(PathName)!;
        Directory.CreateDirectory(directory);
        string temp = Path.Combine(directory, $"window.{Guid.NewGuid():N}.tmp");
        try { File.WriteAllText(temp, JsonSerializer.Serialize(this)); File.Move(temp, PathName, true); }
        finally { if (File.Exists(temp)) File.Delete(temp); }
    }
}
