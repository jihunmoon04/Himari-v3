using System.Text.Json;

namespace Himari.Companion;

internal static class ScreenSafety
{
    internal static bool Ready(JsonElement file) => file.ValueKind == JsonValueKind.Object &&
        file.TryGetProperty("image", out var image) && image.ValueKind == JsonValueKind.Object &&
        image.TryGetProperty("state", out var state) && state.ValueKind == JsonValueKind.String && state.GetString() == "ready";
}

internal sealed class MoveJobLifetime
{
    private readonly HashSet<string> ended = new();
    internal bool CanStart(string job) => !ended.Contains(job);
    internal void End(string job) => ended.Add(job);
    internal void Reset() => ended.Clear();
}
