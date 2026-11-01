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
            }
            progress?.Invoke(text.ToString());
            if (count < 100) return text.ToString();
            if (last <= after) throw new JsonException("Events page did not advance");
            after = last;
        }
    }
}
