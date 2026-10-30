using System.Net.Http;
using System.Text.Json;

namespace Himari.Companion;

internal static class ScreenHistory
{
    private static JsonElement Prop(JsonElement e, string n) => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(n, out var v) ? v : default;
    private static string? Str(JsonElement e, string n) => Prop(e, n).ValueKind == JsonValueKind.String ? Prop(e, n).GetString() : null;
    private static IEnumerable<JsonElement> Items(JsonElement e) => e.ValueKind == JsonValueKind.Array ? e.EnumerateArray() : [];
    private static string PathFor(string id) => "/api/conversations/" + Uri.EscapeDataString(id);
    internal static async Task<string?> ResolveJobIdAsync(ServerClient server, string conversation, string? activityJobId, CancellationToken ct) =>
        activityJobId ?? await RunningJobIdAsync(server, conversation, ct);
    internal static async Task<string?> RunningJobIdAsync(ServerClient server, string conversation, CancellationToken ct)
    {
        var detail = await server.SendAsync(HttpMethod.Get, PathFor(conversation), null, ct);
        return ConversationView.IsRunning(detail) ? Str(Prop(detail, "job"), "job_id") : null;
    }
    internal static async Task<string?> LatestReadyIdAsync(ServerClient server, string conversation, string jobId, CancellationToken ct, string? expectedFileId = null, string? previousFileId = null)
    {
        expectedFileId ??= previousFileId;
        async Task<JsonElement> Detail() => await server.SendAsync(System.Net.Http.HttpMethod.Get, PathFor(conversation), null, ct);
        bool SameJob(JsonElement detail) => Str(Prop(detail, "job"), "job_id") == jobId && (ConversationView.IsRunning(detail) || expectedFileId is not null);
        var detail = await Detail();
        if (!SameJob(detail)) return null;
        var sequence = Prop(Prop(detail, "conversation"), "last_sequence");
        if (sequence.ValueKind != JsonValueKind.Number || !sequence.TryGetInt32(out int last)) return null;
        int after = Math.Max(0, last - 100);
        string? found = null;
        bool ready = false;
        while (true)
        {
            var page = await server.SendAsync(System.Net.Http.HttpMethod.Get, PathFor(conversation) + $"/events?after={after}&limit=100", null, ct);
            foreach (var item in Items(Prop(page, "items")).Reverse())
            {
                if (Str(item, "kind") != "user_message") continue;
                foreach (var attachment in Items(Prop(item, "attachments")).Reverse())
                    if (Prop(attachment, "screen").ValueKind == JsonValueKind.True) { found = Str(attachment, "file_id"); ready = ScreenSafety.Ready(attachment); break; }
                if (found is not null) break;
            }
            if (found is not null || after == 0) break;
            after = Math.Max(0, after - 100);
        }
        if (!ready || found is null || (expectedFileId is not null && found != expectedFileId) || !SameJob(await Detail())) return null;
        return found;
    }
}
