using System.Text.Json;

namespace Himari.Companion;

/// <summary>
/// character-day §11, §18.13: what the app follows of HIMARI's day when the server announces
/// <c>features.day_session = 1</c>. HIMARI's sleep is the server's (<c>himari.sleep</c> in <c>GET /api/device</c> and
/// <c>hello</c>, then the <c>himari_sleep</c> event); the app's conversation is today's day session
/// (<c>recent_conversation</c>, then the <c>day_session</c> event); a closed day is read only. The rules here need no
/// window: the contract tests check them.
/// </summary>
internal static class DaySession
{
    internal const string ClosedNotice = "하루가 닫혀 보내지 않았어요. 오늘의 대화로 옮기면 다시 보내 주세요.";
    internal const string NoTodayNotice = "오늘의 대화를 열지 못했어요. 잠시 뒤 다시 보내 주세요.";
    internal const string ClosingNotice = "하루를 정리하는 중이에요. 오늘의 대화가 열리면 다시 보내 주세요.";
    internal const string MovedNotice = "오늘의 대화를 여는 사이 다른 대화로 옮겨 보내지 않았어요. 글은 그대로 있어요.";
    internal const string BusyNotice = "히마리가 답하는 중이에요.";
    internal const string SleptThroughNotice = "히마리가 자고 있어요. 메시지는 대화에 남았어요.";
    internal const string UnsentClosedNotice = "닫힌 하루에서 보내지 못한 메시지예요.";
    internal const string RetryTodayNotice = " 다시 보내면 오늘의 대화로 가요.";

    /// <summary>§5.5, §18.11: a message's 202 that HIMARI slept through (<c>asleep: "none"</c>) — recorded, and no job
    /// started: whatever job the receipt names is not this message's, so nothing is waited on. Awake (<c>null</c>), a
    /// mumble or a wake is answered as any message; an older server says nothing of it.</summary>
    internal static bool SleptThrough(JsonElement sent) =>
        sent.ValueKind == JsonValueKind.Object && sent.TryGetProperty("asleep", out var asleep)
        && asleep.ValueKind == JsonValueKind.String && asleep.GetString() == "none";

    /// <summary>§3.2: whether an unsent message's bubble may be sent again where the app is — in the conversation it was
    /// written for; or, written for a day now closed (<paramref name="writtenClosed"/>), from anywhere: it cannot go
    /// there any more, so the retry opens today's session and goes there.</summary>
    internal static bool CanRetry(string? written, string? current, bool writtenClosed) =>
        written is not null && (written == current || writtenClosed);

    /// <summary>§3.2: the conversation an unsent message belongs to once today's session is known: today's when it was
    /// written for a day now closed, the one it was written for otherwise.</summary>
    internal static string? Rebind(string? written, bool writtenClosed, string today) =>
        written is not null && writtenClosed ? today : written;

    /// <summary>§3.2 (second review P2 5): today's session as <c>GET /api/device</c> names it (<c>recent_conversation</c>)
    /// for a send leaving a closed day — none while it names a day known closed: a day fenced for its closing is still
    /// named while it closes, and refuses with <c>day_closed</c>, so it is never taken for today's again. The send waits
    /// for a different open session (the rollover).</summary>
    internal static string? Today(string? recent, bool recentClosed) => recent is not null && !recentClosed ? recent : null;

    /// <summary>§3.2 (second review P1 4): a send that left a closed day goes on only to the session it resolved, while
    /// that is still the one shown, on the client and connection it began with — never to whatever is selected now.
    /// Otherwise it stops, its draft (in the editor, or its bubble) as it was.</summary>
    internal static bool Verified(string? resolved, string? shown, object? originClient, object? client, object? originConnection, object? connection) =>
        resolved is not null && resolved == shown && client is not null && connection is not null
        && ReferenceEquals(originClient, client) && ReferenceEquals(originConnection, connection);

    /// <summary>Whether the server keeps HIMARI's day (<c>features.day_session = 1</c>); an older server does not.</summary>
    internal static bool Announced(JsonElement info) =>
        info.ValueKind == JsonValueKind.Object && info.TryGetProperty("features", out var features)
        && features.ValueKind == JsonValueKind.Object && features.TryGetProperty("day_session", out var day)
        && day.ValueKind == JsonValueKind.Number && day.TryGetInt32(out int version) && version == 1;

    /// <summary>The state in <c>himari.sleep</c> or the <c>himari_sleep</c> event's <c>sleep</c>: a string, or an object
    /// carrying it as <c>state</c>. Null for anything else.</summary>
    internal static string? Sleep(JsonElement value)
    {
        string? state = value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Object when value.TryGetProperty("state", out var s) && s.ValueKind == JsonValueKind.String => s.GetString(),
            _ => null,
        };
        return state is "awake" or "drowsy" or "asleep" or "half_asleep" ? state : null;
    }

    /// <summary>§5.1: the character sleeps while HIMARI is asleep, and half asleep too (a mumble is said in its sleep).</summary>
    internal static bool Sleeping(string? sleep) => sleep is "asleep" or "half_asleep";

    /// <summary>§18.13: drowsy, it yawns now and then.</summary>
    internal static bool Drowsy(string? sleep) => sleep == "drowsy";

    /// <summary>Whether to go to <paramref name="target"/>, today's day session: not when it is shown already. Always
    /// from none shown or from a closed day (nothing can be said there; a draft goes along). Otherwise only to a session
    /// new to the app (<paramref name="targetNew"/>: just opened, or not in the list read before) and while nothing is
    /// being typed — a conversation the user picked keeps them.</summary>
    internal static bool Follow(string? current, string? target, bool currentClosed, bool targetNew, string draft)
    {
        if (target is null || target == current) return false;
        if (current is null || currentClosed) return true;
        return targetNew && string.IsNullOrWhiteSpace(draft);
    }
}
