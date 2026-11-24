using System.Text.Json;

namespace Himari.Companion;

// character-state v0.1 §4: the character menu's "페르소나" submenu. The server gives a device only the personas'
// ids and names and the one chosen (GET /api/device/personas), never their text; a choice goes back with the
// revision it was read at (PUT /api/device/persona-choice). The rules here need no window: the contract tests
// check them.
internal sealed record PersonaItem(int? Id, string Name, bool Chosen);

internal sealed record PersonaMenuState(IReadOnlyList<PersonaItem> Items, int Revision)
{
    /// <summary>The persona chosen now; null for none.</summary>
    internal int? Chosen => Items.FirstOrDefault(i => i.Chosen)?.Id;
}

internal static class PersonaMenu
{
    internal const string NoneName = "없음";
    internal const string Unsupported = "서버가 아직 지원하지 않아요";
    internal const string Loading = "불러오는 중…";
    internal const string Failed = "페르소나를 바꾸지 못했어요";

    /// <summary>The menu from the server's answer (either route's): the personas by id, then "없음", the chosen one
    /// marked; null when the answer is not that shape.</summary>
    internal static PersonaMenuState? Parse(JsonElement data)
    {
        if (data.ValueKind != JsonValueKind.Object || !data.TryGetProperty("personas", out var personas)
            || personas.ValueKind != JsonValueKind.Array || !data.TryGetProperty("choice", out var choice)
            || choice.ValueKind != JsonValueKind.Object || !choice.TryGetProperty("revision", out var revision)
            || !revision.TryGetInt32(out int rev)) return null;
        int? chosen = choice.TryGetProperty("persona_id", out var id) && id.ValueKind == JsonValueKind.Number && id.TryGetInt32(out int c) ? c : null;
        var items = new List<PersonaItem>();
        foreach (var persona in personas.EnumerateArray())
        {
            if (persona.ValueKind != JsonValueKind.Object || !persona.TryGetProperty("id", out var pid) || !pid.TryGetInt32(out int n)) continue;
            string name = persona.TryGetProperty("name", out var nm) && nm.ValueKind == JsonValueKind.String ? nm.GetString() ?? "" : "";
            items.Add(new PersonaItem(n, name.Length > 0 ? name : $"페르소나 {n}", chosen == n));
        }
        items.Add(new PersonaItem(null, NoneName, chosen is null || items.All(i => i.Id != chosen)));
        return new PersonaMenuState(items, rev);
    }

    /// <summary>What PUT /api/device/persona-choice takes: the persona (null: none) and the revision read.</summary>
    internal static Dictionary<string, object?> Choice(int? personaId, int revision) => new() { ["persona_id"] = personaId, ["revision"] = revision };
}
