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
