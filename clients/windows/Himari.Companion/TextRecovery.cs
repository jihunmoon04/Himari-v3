using System.Text.Json;

namespace Himari.Companion;

internal static class TextRecovery
{
    internal static async Task<bool> AcceptedAsync(ServerClient server, string conversation, string text, string deviceId, int boundary, CancellationToken ct)
    {
        int after = boundary;
        while (true)
        {
            var page = await server.SendAsync(System.Net.Http.HttpMethod.Get, "/api/conversations/" + Uri.EscapeDataString(conversation) + $"/events?after={after}&limit=100", null, ct);
            var items = page.GetProperty("items").EnumerateArray().ToArray();
            if (items.Any(x => Matches(x, text, deviceId, boundary))) return true;
            if (items.Length < 100) return false;
            var seq = items[^1].GetProperty("sequence");
            if (seq.ValueKind != JsonValueKind.Number || !seq.TryGetInt32(out int next) || next <= after) return false;
            after = next;
        }
    }
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
