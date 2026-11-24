using System.Text.Json;
using System.Windows;
using MenuItem = System.Windows.Controls.MenuItem;
using Separator = System.Windows.Controls.Separator;

namespace Himari.Companion;

// character-state v0.1 §4: the "페르소나" submenu of the character menu, and §2: the mood the character starts with.
// The menu is read when the stream says hello and again on each `persona` event (a change here, on another device
// or on the Web); choosing goes to the server, and a refusal is said in a small notice over the character. The
// rules (what the answer means, what a choice sends) are in PersonaMenu.cs.
public partial class MainWindow
{
    private bool serverTakesPersonas;  // GET /api/device or hello said features.persona_choice = 1
    private PersonaMenuState? personaState;
    private int personaNoticeVersion;

    private void PersonaFeature(JsonElement info)
    {
        serverTakesPersonas = Prop(Prop(info, "features"), "persona_choice") is { ValueKind: JsonValueKind.Number } v && v.GetInt32() == 1;
        if (!serverTakesPersonas) personaState = null;
        BuildPersonaMenu();
    }

    private void ForgetPersonas()
    {
        serverTakesPersonas = false; personaState = null; BuildPersonaMenu();  // learnt again from the new server
    }

    private async Task RefreshPersonas(ServerClient local, CancellationToken ct)
    {
        if (!serverTakesPersonas) return;
        try
        {
            var answer = await local.SendAsync(System.Net.Http.HttpMethod.Get, "/api/device/personas", null, ct);
            if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return;
            personaState = PersonaMenu.Parse(answer) ?? personaState;
            BuildPersonaMenu();
        }
        catch (Exception ex) when (ex is ApiException or System.Net.Http.HttpRequestException or OperationCanceledException or JsonException)
        {
            System.Diagnostics.Trace.TraceWarning($"HIMARI personas: {ex.Message}");
        }
    }

    private void BuildPersonaMenu()
    {
        PersonaSubmenu.Items.Clear();
        if (!serverTakesPersonas || personaState is null)
        {
            PersonaSubmenu.Items.Add(new MenuItem { Header = serverTakesPersonas ? PersonaMenu.Loading : PersonaMenu.Unsupported, IsEnabled = false });
            return;
        }
        foreach (var item in personaState.Items)
        {
            if (item.Id is null && personaState.Items.Count > 1) PersonaSubmenu.Items.Add(new Separator());
            var entry = new MenuItem { Header = item.Name, IsCheckable = true, IsChecked = item.Chosen, IsEnabled = client is not null };
            int? id = item.Id;
            entry.Click += (_, _) => _ = ChoosePersona(id);
            PersonaSubmenu.Items.Add(entry);
        }
    }

    private async Task ChoosePersona(int? personaId)
    {
        if (client is not { } local || personaState is not { } state) return;
        if (state.Chosen == personaId) { BuildPersonaMenu(); return; }  // already this one: keep the mark where it was
        try
        {
            var answer = await local.SendAsync(System.Net.Http.HttpMethod.Put, "/api/device/persona-choice",
                PersonaMenu.Choice(personaId, state.Revision), lifetime.Token);
            if (!ReferenceEquals(local, client)) return;
            personaState = PersonaMenu.Parse(answer) ?? personaState;
            BuildPersonaMenu();
        }
        catch (Exception ex) when (ex is ApiException or System.Net.Http.HttpRequestException or OperationCanceledException or JsonException)
        {
            ShowPersonaNotice(PersonaMenu.Failed);
            BuildPersonaMenu();  // the mark goes back to the one chosen
            if (connection is { } live && ReferenceEquals(local, client)) _ = RefreshPersonas(local, live.Token);  // e.g. changed elsewhere
        }
    }

    /// <summary>§4: a refusal said briefly over the character, then gone.</summary>
    private async void ShowPersonaNotice(string text)
    {
        int version = ++personaNoticeVersion;
        PersonaNoticeText.Text = text;
        PersonaNotice.Visibility = Visibility.Visible;
        await Task.Delay(3000);
        if (version == personaNoticeVersion) PersonaNotice.Visibility = Visibility.Collapsed;
    }

    /// <summary>§2: the mood the character starts with, as the server sends it once after hello; a reply being
    /// prepared or spoken keeps its own face.</summary>
    private void StartingMood(string? value)
    {
        if (value is null || ReplyPreparing || (currentConversation is not null && liveStreams.ContainsKey(currentConversation))) return;
        SetMood(value);
    }
}
