namespace Himari.Companion;

/// <summary>The task that goes on (V3-P90 §6.1): its group and how long it has gone on.</summary>
internal sealed record TaskState(string Group, double ForSeconds);

/// <summary>A game or a video that clearly ended (V3-P90 §5.3): how long it lasted, how long ago it ended.</summary>
internal sealed record TaskEndedState(string Group, double LastedSeconds, double EndedSecondsAgo);

/// <summary>A media session as Windows lists it: the app's key ("chrome", like a program's key) and whether it plays.</summary>
internal readonly record struct MediaSession(string App, bool Playing);

/// <summary>
/// V3-P90 §6.1, §5.3: the main task over the last <see cref="Window"/> and the games and videos that clearly end.
/// Pure: the caller passes the time and what it read, so tests drive it without Windows.
/// <list type="bullet">
/// <item>Each look counts the time since the last one to the group in front: work (coding, documents, design),
/// video, game; browsing, chat, other and nothing in front are neutral. HIMARI's own window counts as what was
/// there before it.</item>
/// <item>The main task: of the window's non-neutral time — at least <see cref="MinTask"/> of it — the group with more
/// than half. Neutral time does not break it; neutral alone long enough leaves none. It goes on while it stays the
/// same; sleep ends it (<see cref="Reset"/>).</item>
/// <item>A game ends when its program ends; a video when its media sessions are gone, or once paused and the main task
/// moved off it (a pause alone is no end). A video's sessions are those that played from the program in front while
/// it was the task, so music playing elsewhere is not it (PR #166 review P2); with none matched, any session counts.
/// The end is carried for <see cref="Carried"/>; the end of work is never told.</item>
/// </list>
/// </summary>
internal sealed class TaskTracker
{
    internal static readonly TimeSpan Window = TimeSpan.FromMinutes(5);
    internal static readonly TimeSpan MinTask = TimeSpan.FromMinutes(2);
    internal static readonly TimeSpan Carried = TimeSpan.FromMinutes(3);
    private static readonly TimeSpan MaxStep = TimeSpan.FromSeconds(10);  // a longer gap between looks is not all counted
    private static readonly TimeSpan OpenAfterMain = TimeSpan.FromMinutes(5);  // a game or video left this long is no longer watched

    private readonly Queue<(DateTime At, string? Group, double Seconds)> samples = new();
    private DateTime? lastAt;
    private string? lastGroup;
    private string? main;
    private DateTime mainSince;
    private OpenTask? open;  // the game or video whose end is watched
    private (string Group, double Lasted, DateTime At)? ended;

    private sealed class OpenTask
    {
        internal OpenTask(string group, DateTime start, DateTime now) { Group = group; Start = start; LastMain = now; }
        internal string Group { get; }
        internal DateTime Start { get; }
        internal DateTime LastMain { get; set; }
        internal DateTime? LastPlaying { get; set; }
        internal bool SawSession { get; set; }
        internal HashSet<string> Sources { get; } = new(StringComparer.OrdinalIgnoreCase);  // a video's own sessions
    }

    internal static string? GroupOf(string? category) => category switch
    {
        "coding" or "document" or "design" => "work",
        "video" => "video",
        "game" => "game",
        _ => null,
    };

    /// <summary>
    /// One look: the category in front (<paramref name="own"/>: HIMARI's window) and its program's key, whether the
    /// last game's program still runs, and the media sessions. True when the main task changed or a task's end is new.
    /// </summary>
    internal bool Observe(DateTime now, string? category, bool own, bool gameAlive, IReadOnlyCollection<MediaSession> sessions,
        string? program = null)
    {
        string? group = own ? lastGroup : GroupOf(category);
        if (!own) lastGroup = group;
        double step = lastAt is { } before ? Math.Clamp((now - before).TotalSeconds, 0, MaxStep.TotalSeconds) : 0;
        lastAt = now;
        samples.Enqueue((now, group, step));
        while (samples.Count > 0 && now - samples.Peek().At > Window) samples.Dequeue();

        var totals = samples.Where(s => s.Group is not null).GroupBy(s => s.Group!).ToDictionary(g => g.Key, g => g.Sum(s => s.Seconds));
        double counted = totals.Values.Sum();
        string? next = counted >= MinTask.TotalSeconds && totals.MaxBy(t => t.Value) is { } top && top.Value > counted / 2 ? top.Key : null;
        bool changed = next != main;
        if (changed) { main = next; mainSince = now; }

        if (main is "game" or "video")
        {
            if (open is null || open.Group != main) open = new OpenTask(main, mainSince, now);
            open.LastMain = now;
        }
        if (open is not null)
        {
            if (open.Group == "video" && main == "video" && !own && !string.IsNullOrEmpty(program))
                foreach (var session in sessions) if (session.Playing && session.App.Equals(program, StringComparison.OrdinalIgnoreCase)) open.Sources.Add(session.App);
            var mine = open.Sources.Count > 0 ? sessions.Where(s => open.Sources.Contains(s.App)).ToList() : sessions.ToList();
            bool playing = mine.Any(s => s.Playing);
            if (playing) open.LastPlaying = now;
            if (mine.Count > 0) open.SawSession = true;
            bool over = open.Group == "game"
                ? !gameAlive
                : (open.SawSession && mine.Count == 0) || (!playing && main != "video");
            if (over)
            {
                DateTime lastOn = open.Group == "video" && open.LastPlaying is { } played ? played : open.LastMain;
                ended = (open.Group, Math.Max(0, (lastOn - open.Start).TotalSeconds), now);
                open = null;
                return true;
            }
            if (main != open.Group && now - open.LastMain > OpenAfterMain) open = null;  // left long ago, not ended
        }
        return changed;
    }

    internal TaskState? Current(DateTime now) => main is null ? null : new TaskState(main, Math.Max(0, (now - mainSince).TotalSeconds));

    internal TaskEndedState? Ended(DateTime now) => ended is { } e && now - e.At <= Carried
        ? new TaskEndedState(e.Group, e.Lasted, Math.Max(0, (now - e.At).TotalSeconds))
        : null;

    /// <summary>Asleep or locked (§6.1): the task ends, and no end is told (nobody is there to hear it) — nor one
    /// told just before, which would come back stale on waking (PR #166 review P1).</summary>
    internal void Reset()
    {
        samples.Clear(); lastAt = null; lastGroup = null; main = null; open = null; ended = null;
    }
}
