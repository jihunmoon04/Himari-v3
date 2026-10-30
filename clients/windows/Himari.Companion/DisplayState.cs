using System.Text;
using System.Text.Json;

namespace Himari.Companion;

internal static class DisplayState
{
    private static readonly HashSet<string> Known = ["neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried"];
    internal static string Mood(JsonElement marks, int characterCount, string initial = "neutral")
    {
        var mood = Known.Contains(initial) ? initial : "neutral";
        if (marks.ValueKind != JsonValueKind.Array) return mood;
        foreach (var mark in marks.EnumerateArray())
        {
            if (mark.ValueKind != JsonValueKind.Object || !mark.TryGetProperty("at", out var offset) || offset.ValueKind != JsonValueKind.Number || !offset.TryGetInt32(out var at) || at < 0 || at > characterCount) continue;
            string? value = mark.TryGetProperty("mood", out var item) && item.ValueKind == JsonValueKind.String ? item.GetString() : null;
            mood = value is not null && Known.Contains(value) ? value : "neutral";
        }
        return mood;
    }
}

internal sealed class LiveDisplayState
{
    private readonly StringBuilder text = new();
    private string? run;
    internal string Text => text.ToString();
    internal string Mood { get; private set; } = "neutral";
    internal void Apply(string type, string? runId, string chunk, JsonElement marks)
    {
        if (type == "snapshot" || runId != run) { text.Clear(); run = runId; Mood = "neutral"; }
        text.Append(chunk);
        Mood = DisplayState.Mood(marks, Text.EnumerateRunes().Count(), Mood);
    }
}
