using System.Text.Json;

namespace Himari.Companion;

/// <summary>Whose a job is (V3-P83 §8.4, V3-P87 §10).</summary>
internal static class JobOrigin
{
    /// <summary>A job this device speaks: one its own message started ("device"), or one the
    /// server opened to speak first on it ("wake"). Either names this device.</summary>
    internal static bool IsOwn(JsonElement origin, string? deviceId) =>
        deviceId is not null && Kind(origin) is "device" or "wake" && Text(origin, "device_id") == deviceId;

    /// <summary>The server opened it: HIMARI speaks first (V3-P87 §10).</summary>
    internal static bool IsWake(JsonElement origin) => Kind(origin) == "wake";

    /// <summary>Whether to move to the conversation a wake speaks in: not when it is shown
    /// already, and not while the user is typing (the text stays where it is written).</summary>
    internal static bool Follow(string? current, string conversation, string draft) =>
        current != conversation && string.IsNullOrWhiteSpace(draft);

    /// <summary>Still follow once the conversation list is read (PR #159 review P1): the same
    /// conversation shown, nothing typed or switched since (the input revision moves on every
    /// edit and switch), and the editor empty.</summary>
    internal static bool StillFollow(string? before, string? now, long revisionBefore, long revisionNow, string draft) =>
        before == now && revisionBefore == revisionNow && string.IsNullOrWhiteSpace(draft);

    private static string? Kind(JsonElement origin) => Text(origin, "kind");

    private static string? Text(JsonElement element, string name) =>
        element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() : null;
}
