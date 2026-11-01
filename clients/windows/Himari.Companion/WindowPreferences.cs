using System.IO;
using System.Text.Json;

namespace Himari.Companion;

internal sealed record WindowPreferences(double Left, double Top, double CharacterHeight, bool MoveEnabled = true,
    bool ScreenSharing = true, bool FacingLeft = false, double BubbleMinimum = 8, double SecondsPer100 = 8, string ChatHotkey = "Alt+Q",
    string BubblePlacement = "상하 자동", int LayoutVersion = 0, SignalSettings? Signals = null)
{
    internal static WindowPreferences Normalize(WindowPreferences value, double left, double top, double width, double height, double windowWidth, double windowHeight)
    {
        double x = double.IsFinite(value.Left) ? value.Left : left;
        double y = double.IsFinite(value.Top) ? value.Top : top;
        double size = double.IsFinite(value.CharacterHeight) ? Math.Clamp(value.CharacterHeight, 200, 400) : 330;
        // Keep the title and controls reachable after a monitor is removed.
        x = Math.Clamp(x, left, Math.Max(left, left + width - windowWidth));
        y = Math.Clamp(y, top, Math.Max(top, top + height - windowHeight));
        return value with { Left = x, Top = y, CharacterHeight = size,
            BubbleMinimum = DialoguePolicy.Finite(value.BubbleMinimum, 8, 3, 30), SecondsPer100 = DialoguePolicy.Finite(value.SecondsPer100, 8, 3, 30),
            ChatHotkey = DialoguePolicy.Hotkeys.Contains(value.ChatHotkey) ? value.ChatHotkey : "Alt+Q",
            BubblePlacement = DialoguePolicy.BubblePlacements.Contains(value.BubblePlacement) ? value.BubblePlacement : "상하 자동" };
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
