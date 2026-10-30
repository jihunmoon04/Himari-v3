using System.Text.Json;

namespace Himari.Companion;

internal static class TextRecovery
{
    internal static bool Matches(JsonElement item, string text, string deviceId, int after)
    {
        static JsonElement Get(JsonElement e, string n) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(n, out var v) ? v : default;
        static string? Str(JsonElement e) => e.ValueKind == JsonValueKind.String ? e.GetString() : null;
        var seq = Get(item, "sequence"); var origin = Get(item, "origin");
        return seq.ValueKind == JsonValueKind.Number && seq.TryGetInt32(out int n) && n > after &&
            Str(Get(item, "kind")) == "user_message" && Str(Get(item, "text")) == text &&
            Str(Get(origin, "kind")) == "device" && Str(Get(origin, "device_id")) == deviceId;
    }
}
