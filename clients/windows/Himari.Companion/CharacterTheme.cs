using System.Text.Json;

namespace Himari.Companion;

// Only local, known presentation templates are accepted; no executable skin content.
internal sealed record CharacterTheme(string CharacterId, string BubbleTemplate)
{
    internal static CharacterTheme Default => new("himari", "default");
    internal static CharacterTheme Parse(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return Default;
            string? character = root.TryGetProperty("characterId", out var id) && id.ValueKind == JsonValueKind.String ? id.GetString() : null;
            string? template = root.TryGetProperty("bubbleTemplate", out var bubble) && bubble.ValueKind == JsonValueKind.String ? bubble.GetString() : null;
            if (character != "himari") return Default;
            return new(character, template is "pixel" or "default" ? template : "default");
        }
        catch (JsonException) { return Default; }
    }
}
