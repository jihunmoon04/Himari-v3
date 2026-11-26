using System.Text.Json;
using System.Windows;

namespace Himari.Companion;

// character-day §11, §18.13: with a server that announces features.day_session = 1, the character sleeps by HIMARI's
// own sleep (himari.sleep, the himari_sleep event) — the input policy (SleepPolicy) is then only the user's being away,
// still sent as before — a click on it asleep is a poke, and the app's conversation is today's day session: no "새 대화",
// it follows recent_conversation and the day_session event, and a message to a closed day goes to today's instead. An
// older server keeps the old behaviour. The rules are in DaySession.cs.
public partial class MainWindow
{
    private bool serverDaySession;  // GET /api/device or hello said features.day_session = 1
    private string? himariSleep;  // the server's: awake, drowsy, asleep, half_asleep (null: not told)
    private readonly HashSet<string> closedDays = new();  // day sessions known closed (read only, §3.2)

    /// <summary>What GET /api/device and hello say of the day: whether the server keeps it, and HIMARI's sleep now.</summary>
    private void DayFeature(JsonElement info)
    {
        serverDaySession = DaySession.Announced(info);
        himariSleep = serverDaySession ? DaySession.Sleep(Prop(Prop(info, "himari"), "sleep")) : null;
        if (!serverDaySession) closedDays.Clear();
        ApplyDayControls();
    }

    private void ForgetDay()
    {
        serverDaySession = false; himariSleep = null; closedDays.Clear();  // learnt again from the new server
        ApplyDayControls();
    }

    /// <summary>§11: the conversation is today's day session, so "새 대화" goes; the character's sleep is redrawn.</summary>
    private void ApplyDayControls()
    {
        NewConversationButton.Visibility = NewConversationMenu.Visibility = serverDaySession ? Visibility.Collapsed : Visibility.Visible;
        UpdateFrame();
    }

    /// <summary>The character sleeps by the server's sleep; with an older server, by the input policy as before (a wake
    /// while asleep wakes it for its words only, V3-P90 §2.4).</summary>
    private bool CharacterAsleep(bool policyAsleep) => serverDaySession ? DaySession.Sleeping(himariSleep) : policyAsleep && !wakeAwake;

    /// <summary>The <c>himari_sleep</c> event: HIMARI fell asleep, woke, got drowsy, half woke for a mumble.</summary>
    private void HimariSleepChanged(JsonElement sleep)
    {
        if (!serverDaySession) return;
        bool was = DaySession.Sleeping(himariSleep);
        himariSleep = DaySession.Sleep(sleep);
        // waking stretches; words HIMARI says first wait for the stretch, as V3-P90 §5.2
        if (was && !DaySession.Sleeping(himariSleep) && character.Manifest.Clips.TryGetValue("stretch", out var stretch))
            stretchUntil = DateTime.UtcNow + TimeSpan.FromTicks((long)(stretch.Length.Ticks / animationSpeed));
        UpdateFrame();
    }

    /// <summary>The <c>day_session</c> event: a day closed (read only from now), or today's opened — the app goes there
    /// unless the user stays in a conversation they picked (DaySession.Follow); the list is read again either way.</summary>
    private async Task DaySessionChanged(JsonElement data, ServerClient local, CancellationToken ct)
    {
        if (!serverDaySession || Str(data, "conversation") is not { } id) return;
        if (Prop(data, "closed").ValueKind == JsonValueKind.True) { closedDays.Add(id); UpdateUnsentBubbles(); return; }
        if (closedDays.Contains(id)) return;  // second review P2 5: a day known closed is never today's again
        RebindClosedUnsent(id);  // today's: what a closed day did not take goes there
        string? before = currentConversation;
        bool follow = DaySession.Follow(before, id, before is not null && closedDays.Contains(before), targetNew: true, Input.Text);
        var list = await LoadConversations(local, ct);
        if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return;
        if (currentConversation != before) follow = false;  // the user picked another meanwhile
        ShowConversations(list, follow ? id : currentConversation);
        if (follow && currentConversation is not null) await ReadConversation(currentConversation, ct);
    }

    /// <summary>hello: whether to go to <paramref name="recent"/> (today's day session) — from a closed day, or to one
    /// the app has not listed yet (it opened while the app was away).</summary>
    private bool FollowDayOnHello(string? recent)
    {
        if (!serverDaySession || recent is null || closedDays.Contains(recent)) return false;  // a closing day: wait (P2 5)
        bool listed = (Conversations.ItemsSource as IEnumerable<Conversation>)?.Any(x => x.Id == recent) == true;
        string? current = currentConversation;
        return DaySession.Follow(current, recent, current is not null && closedDays.Contains(current), targetNew: !listed, Input.Text);
    }

    /// <summary>Whether <paramref name="id"/> is a day known closed (a message cannot go there).</summary>
    private bool ClosedDay(string? id) => serverDaySession && id is not null && closedDays.Contains(id);

    /// <summary>§3.2: the day shown closed. Today's session opens at its first need — GET /api/device opens it and names
    /// it (recent_conversation) — and the app goes there; the draft stays in the editor, and the unsent bubbles of closed
    /// days move to today's (RebindClosedUnsent). <c>Today</c>: the session resolved as today's (null: none, a reconnect,
    /// or it was found closed); the caller sends only while it is the one shown, on the same client and connection
    /// (DaySession.Verified, second review P1 4). Another conversation picked while this reads keeps the user there: the
    /// app does not move, and the send stops. <c>Closing</c>: the server still names a day known closed — fenced while it
    /// closes (P2 5): it stays closed, nothing moves, and the rollover (a different open session) is waited for.</summary>
    private async Task<(string? Today, bool Closing)> OpenTodaySession(ServerClient local, CancellationToken ct)
    {
        string? before = currentConversation;
        var info = await local.SendAsync(System.Net.Http.HttpMethod.Get, "/api/device", null, ct);
        if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return (null, false);
        string? recent = Str(Prop(info, "recent_conversation"), "id");
        if (DaySession.Today(recent, ClosedDay(recent)) is not { } today) return (null, recent is not null);
        RebindClosedUnsent(today);
        if (today == currentConversation || currentConversation != before) return (today, false);
        var list = await LoadConversations(local, ct);
        if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return (null, false);
        if (ClosedDay(today)) return (null, true);  // the list read now says it closed meanwhile (P2 5)
        if (currentConversation != before) return (today, false);  // the user picked another meanwhile (P1 4)
        ShowConversations(list, today);
        if (currentConversation == today) await ReadConversation(today, ct);
        return (today, false);
    }

    /// <summary>§3.2 (review 8): a message a closed day did not take can never go there — its unsent bubble belongs to
    /// <paramref name="today"/>'s session from now on, and is sent again from there. Its unknown outcome, if any, is still
    /// checked in the day it was sent to (UnknownText keeps that conversation).</summary>
    private void RebindClosedUnsent(string today)
    {
        bool moved = false;
        foreach (var past in history)
        {
            string? to = DaySession.Rebind(past.UnsentConversation, ClosedDay(past.UnsentConversation), today);
            if (to == past.UnsentConversation) continue;
            past.UnsentConversation = to; moved = true;
        }
        if (moved) UpdateUnsentBubbles();
    }

    /// <summary>§3.2, §18.11: a closed day refused this send — its message, or the screen uploaded for it (409
    /// <c>day_closed</c>, second review P2 8) — and recorded nothing. A screen taken for that day is dropped, the day is
    /// marked closed for good (its unsent bubbles move to today's once known), and the app goes to today's when the server
    /// names a different open session. The caller keeps the draft: the text (SendMessageCore's finally), the screen button.</summary>
    private void ClosedDayRefused(SendContext context, ServerClient local, LocalScreen? screen)
    {
        if (screen is not null) { unresolvedScreen = null; localScreens.Remove(screen.FileId); submittedScreen = null; submittedConversation = null; }
        closedDays.Add(context.ConversationId);
        UpdateUnsentBubbles();
        if (context.IsCurrent(client, currentConversation, connected)) Status.Text = DaySession.ClosedNotice;
        _ = FollowToday(local);
    }

    /// <summary>A message refused for a closed day (409 <c>day_closed</c>): go to today's session, not waited on.</summary>
    private async Task FollowToday(ServerClient local)
    {
        if (connection is not { } live) return;
        try { await OpenTodaySession(local, live.Token); }
        catch (Exception ex) when (ex is ApiException or System.Net.Http.HttpRequestException or OperationCanceledException or JsonException)
        {
            System.Diagnostics.Trace.TraceWarning($"HIMARI day session: {ex.Message}");
        }
    }

    /// <summary>§5.5, §18.13: a click on HIMARI asleep pokes it (one count of waking it). Not woken, it only stirs; a
    /// mumble or a wake comes from the server as any reply and sleep change does.</summary>
    private void PokeIfAsleep()
    {
        if (!serverDaySession || !DaySession.Sleeping(himariSleep) || !connected || client is not { } local) return;
        _ = Poke(local);
    }

    private async Task Poke(ServerClient local)
    {
        try
        {
            var answer = await local.SendAsync(System.Net.Http.HttpMethod.Post, "/api/device/poke", new { }, lifetime.Token);
            if (!ReferenceEquals(local, client)) return;
            if (Str(answer, "answer") == "none") { animator.Stir(AnimationNow()); UpdateFrame(); }
        }
        catch (Exception ex) when (ex is ApiException or System.Net.Http.HttpRequestException or OperationCanceledException or JsonException)
        {
            System.Diagnostics.Trace.TraceWarning($"HIMARI poke: {ex.Message}");
        }
    }
}
