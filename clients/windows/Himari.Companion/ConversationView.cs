using System.Text.Json;
namespace Himari.Companion;

internal static class ConversationView
{
    private static JsonElement Get(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object && value.TryGetProperty(name, out var result) ? result : default;
    private static string? Text(JsonElement value) => value.ValueKind == JsonValueKind.String ? value.GetString() : null;
    internal static string? Question(JsonElement detail)
    {
        var waiting = Get(detail, "waiting"); var question = Get(waiting, "question");
        // rev. 5 §9.2, plus Appendix A.6 and the current legacy response shape.
        return Text(Get(Get(question, "display"), "text")) ?? Text(Get(Get(waiting, "display"), "text")) ?? Text(question);
    }
    internal static bool IsRunning(JsonElement detail) => Get(Get(detail, "job"), "running").ValueKind == JsonValueKind.True;
}

/// <summary>V3-P93 §6.3: the files presented in the latest reply turn only.
/// A turn starts at a user message, or at the first event after an assistant
/// final (a wake reply has no user message). Files from earlier turns stay in
/// the history window, not in the current reply bubble.</summary>
internal sealed class ReplyTurn
{
    /// <summary>True once the events read so far contain the turn's start, so
    /// reading earlier pages cannot change <see cref="Files"/>.</summary>
    internal bool Bounded { get; private init; }
    internal IReadOnlySet<(string FileId, int Version)> Files { get; private init; } = new HashSet<(string, int)>();

    private static JsonElement Get(JsonElement value, string name) => value.ValueKind == JsonValueKind.Object && value.TryGetProperty(name, out var result) ? result : default;

    internal static ReplyTurn Find(IEnumerable<JsonElement> events)
    {
        var ordered = new List<(int Sequence, string Kind, JsonElement Item)>();
        foreach (var item in events)
        {
            var sequence = Get(item, "sequence");
            var kind = Get(item, "kind");
            if (sequence.ValueKind != JsonValueKind.Number || !sequence.TryGetInt32(out int n) || kind.ValueKind != JsonValueKind.String) continue;
            ordered.Add((n, kind.GetString() ?? "", item));
        }
        ordered.Sort((a, b) => a.Sequence.CompareTo(b.Sequence));
        var files = new HashSet<(string, int)>();
        bool bounded = false, closed = false;
        foreach (var (_, kind, item) in ordered)
        {
            if (kind is not ("user_message" or "assistant_final" or "tool_result")) continue;
            if (closed || kind == "user_message") { files.Clear(); bounded = true; closed = false; }
            if (kind == "assistant_final") closed = true;
            else if (kind == "tool_result" && Get(item, "presented_files") is { ValueKind: JsonValueKind.Array } presented)
            {
                foreach (var pf in presented.EnumerateArray())
                {
                    var id = Get(pf, "file_id"); var version = Get(pf, "version");
                    if (id.ValueKind == JsonValueKind.String && id.GetString() is { Length: > 0 } fileId &&
                        version.ValueKind == JsonValueKind.Number && version.TryGetInt32(out int v) && v >= 1)
                        files.Add((fileId, v));
                }
            }
        }
        return new ReplyTurn { Bounded = bounded, Files = files };
    }
}
