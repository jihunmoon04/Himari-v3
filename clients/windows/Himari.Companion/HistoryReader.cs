using System.Text;
using System.Text.Json;

namespace Himari.Companion;

internal static class HistoryReader
{
    internal static async Task<string> ReadAsync(Func<int, CancellationToken, Task<JsonElement>> readPage, Action<string>? progress, CancellationToken ct)
    {
        int after = 0; var text = new StringBuilder();
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var page = await readPage(after, ct);
            if (!page.TryGetProperty("items", out var items) || items.ValueKind != JsonValueKind.Array) throw new JsonException("Invalid events page");
            int count = 0, last = after;
            foreach (var item in items.EnumerateArray())
            {
                count++;
                if (item.ValueKind != JsonValueKind.Object) continue;
                if (item.TryGetProperty("sequence", out var seq) && seq.ValueKind == JsonValueKind.Number && seq.TryGetInt32(out var n)) last = n;
                string? kind = item.TryGetProperty("kind", out var k) && k.ValueKind == JsonValueKind.String ? k.GetString() : null;
                string? content = null;
                if (kind == "user_message" && item.TryGetProperty("text", out var userText) && userText.ValueKind == JsonValueKind.String) content = userText.GetString();
                else if (kind == "assistant_final" && item.TryGetProperty("display", out var display) && display.ValueKind == JsonValueKind.Object && display.TryGetProperty("text", out var answer) && answer.ValueKind == JsonValueKind.String) content = answer.GetString();
                if (!string.IsNullOrEmpty(content)) text.Append(kind == "user_message" ? "선생님\n" : "히마리\n").Append(content).Append("\n\n");
                else if (kind == "wake") text.Append("(히마리가 먼저 말을 걸었어요)\n\n");  // V3-P87 §9: the server opened that turn
                else if (kind == "tool_result" && item.TryGetProperty("presented_files", out var files) && files.ValueKind == JsonValueKind.Array)
                {
                    foreach (var file in files.EnumerateArray())
                    {
                        if (file.ValueKind != JsonValueKind.Object) continue;
                        string rawName = file.TryGetProperty("name", out var n2) && n2.ValueKind == JsonValueKind.String ? n2.GetString() ?? "download" : "download";
                        int ver = file.TryGetProperty("version", out var v2) && v2.ValueKind == JsonValueKind.Number && v2.TryGetInt32(out int vn) ? vn : 1;
                        text.Append("히마리 (파일)\n").Append(PresentedFileNames.SanitizeSaveFileName(rawName)).Append(" · ").Append(PresentedFileNames.VersionLabel(ver)).Append("\n\n");
                    }
                }
            }
            progress?.Invoke(text.ToString());
            if (EventPages.Next(page, count, last, 100) is not { } next) return text.ToString();
            if (next <= after) throw new JsonException("Events page did not advance");
            after = next;
        }
    }
}

/// <summary>character-day second review P2 6: where the next page of a conversation's events starts — by what the server
/// says of its page, never by the rows it shows (a wake not shown yet is read but left out).</summary>
internal static class EventPages
{
    /// <summary>The next page's <c>after</c>, or null when <paramref name="page"/> was the last. The server says
    /// <c>more</c> (its page stopped at its limit) and <c>last_sequence</c> (past the events it read, shown or not). An
    /// older server says no <c>more</c>: then a page of <paramref name="limit"/> rows may have more, after its last row
    /// (<paramref name="lastRow"/>), and a shorter one was the last.</summary>
    internal static int? Next(JsonElement page, int rows, int lastRow, int limit)
    {
        var more = page.ValueKind == JsonValueKind.Object && page.TryGetProperty("more", out var told) ? told : default;
        if (more.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return rows >= limit ? lastRow : (int?)null;
        if (more.ValueKind == JsonValueKind.False) return null;
        return page.TryGetProperty("last_sequence", out var cursor) && cursor.ValueKind == JsonValueKind.Number && cursor.TryGetInt32(out int next) ? next : lastRow;
    }
}
