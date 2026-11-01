namespace Himari.Companion;

// V3-P88 activity signals: what the app tells the server the user is doing. Everything here is plain
// logic (the contract tests run it anywhere); reading Windows itself is in ActivityProbe.cs.

/// <summary>What the user chose to send (V3-P88 §4.3, §5, §6.1, Q2, Q3): media and titles are off by default.</summary>
internal sealed record SignalSettings(bool Activity = true, bool Media = false, bool Titles = false, bool MicCamera = true,
    string TitleExclusions = SignalSettings.DefaultTitleExclusions, string MicExclusions = SignalSettings.DefaultMicExclusions,
    string CategoryOverrides = "")
{
    internal const string DefaultTitleExclusions = "kakaotalk\ndiscord\nslack\ntelegram\nline\nwhatsapp\nsignal\n1password\nbitwarden\nkeepass\nkeepassxc";
    internal const string DefaultMicExclusions = "obs64\nobs32\nsoundrecorder\nwindowssoundrecorder\nvoicerecorder";

    internal static IReadOnlySet<string> Names(string lines) => lines
        .Split(['\n', '\r', ','], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
        .Select(ActivityCatalog.ProgramKey).Where(n => n.Length > 0).ToHashSet(StringComparer.OrdinalIgnoreCase);

    /// <summary>"obsidian=document" lines; a category outside the list is dropped.</summary>
    internal static IReadOnlyDictionary<string, string> Overrides(string lines)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in lines.Split(['\n', '\r'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            int eq = line.IndexOf('=');
            if (eq <= 0) continue;
            string program = ActivityCatalog.ProgramKey(line[..eq]), category = line[(eq + 1)..].Trim().ToLowerInvariant();
            if (program.Length > 0 && ActivityCatalog.Categories.Contains(category)) result[program] = category;
        }
        return result;
    }
}

internal static class ActivityCatalog
{
    /// <summary>The server's fixed list (V3-P88 §4.1, <c>himari.common.ACTIVITIES</c>).</summary>
    internal static readonly string[] Categories = ["coding", "document", "browsing", "video", "game", "chat", "design", "other"];

    private static readonly Dictionary<string, string> Programs = new(StringComparer.OrdinalIgnoreCase)
    {
        // coding
        ["code"] = "coding", ["code - insiders"] = "coding", ["cursor"] = "coding", ["devenv"] = "coding", ["rider64"] = "coding",
        ["idea64"] = "coding", ["pycharm64"] = "coding", ["webstorm64"] = "coding", ["clion64"] = "coding", ["goland64"] = "coding",
        ["studio64"] = "coding", ["windowsterminal"] = "coding", ["wt"] = "coding", ["powershell"] = "coding", ["pwsh"] = "coding",
        ["cmd"] = "coding", ["conhost"] = "coding", ["sublime_text"] = "coding", ["notepad++"] = "coding", ["zed"] = "coding",
        ["windsurf"] = "coding", ["wezterm-gui"] = "coding", ["alacritty"] = "coding",
        // documents
        ["winword"] = "document", ["excel"] = "document", ["powerpnt"] = "document", ["onenote"] = "document", ["hwp"] = "document",
        ["hword"] = "document", ["notion"] = "document", ["obsidian"] = "document", ["acrobat"] = "document", ["acrord32"] = "document",
        ["notepad"] = "document", ["typora"] = "document", ["logseq"] = "document",
        // browsers (a known video site in front is "video")
        ["chrome"] = "browsing", ["msedge"] = "browsing", ["firefox"] = "browsing", ["whale"] = "browsing", ["brave"] = "browsing",
        ["opera"] = "browsing", ["vivaldi"] = "browsing", ["arc"] = "browsing", ["zen"] = "browsing",
        // video
        ["vlc"] = "video", ["potplayermini64"] = "video", ["potplayermini"] = "video", ["potplayer"] = "video", ["mpc-hc64"] = "video",
        ["mpc-be64"] = "video", ["mpv"] = "video", ["wmplayer"] = "video", ["netflix"] = "video", ["disneyplus"] = "video",
        ["video.ui"] = "video", ["daumpotplayer"] = "video",
        // chat and calls
        ["kakaotalk"] = "chat", ["discord"] = "chat", ["slack"] = "chat", ["telegram"] = "chat", ["teams"] = "chat",
        ["ms-teams"] = "chat", ["zoom"] = "chat", ["line"] = "chat", ["whatsapp"] = "chat", ["signal"] = "chat",
        // design and editing
        ["photoshop"] = "design", ["illustrator"] = "design", ["figma"] = "design", ["aseprite"] = "design", ["krita"] = "design",
        ["clipstudiopaint"] = "design", ["blender"] = "design", ["adobe premiere pro"] = "design", ["afterfx"] = "design",
        ["resolve"] = "design", ["mspaint"] = "design", ["paintdotnet"] = "design", ["inkscape"] = "design",
    };

    // Seen in a browser's window title or as the media session's source: the browser shows a video (V3-P88 §4.1).
    private static readonly string[] VideoSites =
    [
        "youtube", "netflix", "twitch", "chzzk", "치지직", "laftel", "라프텔", "tving", "티빙", "wavve", "웨이브", "coupang play",
        "쿠팡플레이", "disney+", "watcha", "왓챠", "afreecatv", "아프리카tv", "soop", "vimeo", "bilibili", "niconico", "prime video",
    ];

    // Install folders of game launchers: a program run from one is a game.
    private static readonly string[] GameFolders = [@"\steamapps\common\", @"\epic games\", @"\riot games\", @"\battle.net\",
        @"\xboxgames\", @"\nexon\", @"\ea games\", @"\ubisoft game launcher\games\"];

    /// <summary>"Code.exe", "C:\...\Code.exe" or "code" → "code".</summary>
    internal static string ProgramKey(string? program)
    {
        if (string.IsNullOrWhiteSpace(program)) return "";
        string name = program.Trim().Replace('/', '\\');
        int slash = name.LastIndexOf('\\');
        if (slash >= 0) name = name[(slash + 1)..];
        return name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) ? name[..^4] : name;
    }

    internal static bool IsVideoSite(string? text) =>
        !string.IsNullOrEmpty(text) && VideoSites.Any(site => text.Contains(site, StringComparison.OrdinalIgnoreCase));

    /// <summary>
    /// The category of the program in front: the user's own choice first, then the table (a browser on a video
    /// site is "video"), then a program from a game folder or in exclusive fullscreen is "game", else "other".
    /// </summary>
    internal static string Classify(string program, string? path, string? title, bool exclusiveFullscreen,
        IReadOnlyDictionary<string, string>? overrides = null)
    {
        string key = ProgramKey(program);
        if (overrides is not null && overrides.TryGetValue(key, out var chosen)) return chosen;
        if (Programs.TryGetValue(key, out var category))
            return category == "browsing" && IsVideoSite(title) ? "video" : category;
        string folder = (path ?? "").Replace('/', '\\');
        if (exclusiveFullscreen || GameFolders.Any(f => folder.Contains(f, StringComparison.OrdinalIgnoreCase))) return "game";
        return "other";
    }

    /// <summary>A media session's app id as a name: "Spotify.exe" → "Spotify", "SpotifyAB.SpotifyMusic_x!Spotify" → "Spotify".</summary>
    internal static string? MediaAppName(string? appId)
    {
        if (string.IsNullOrWhiteSpace(appId)) return null;
        string name = appId.Trim();
        int bang = name.LastIndexOf('!');
        if (bang >= 0 && bang < name.Length - 1) name = name[(bang + 1)..];
        name = ProgramKey(name);
        return name.Length == 0 ? null : name;
    }
}

/// <summary>What is in front now, as read: <c>Category</c> null means nothing is (the desktop).</summary>
internal sealed record ActivityObservation(string? Category, string? App = null, bool Fullscreen = false, string? Title = null);

/// <summary>An activity as sent (V3-P88 §4): <c>ForSeconds</c> is how long its category has gone on.</summary>
internal sealed record ActivityState(string Category, string? App, double ForSeconds, bool Fullscreen, string? Title);

/// <summary>
/// V3-P88 §4.1: a change of category counts only once it held for <see cref="StableSeconds"/> — a glance at a
/// messenger does not end "45 minutes of coding" — and nothing in front (null) is a change like any other (§4.4).
/// HIMARI's own window keeps what was there; a lock or a sleep starts over.
/// </summary>
internal sealed class ActivityTracker
{
    internal const double StableSeconds = 60;
    private bool started;
    private ActivityObservation? stable;  // null with started: nothing in front
    private DateTime stableSince;
    private string? candidate; private bool hasCandidate;
    private DateTime candidateSince;

    /// <summary>True when the activity sent changed: a new category (or none) took hold.</summary>
    internal bool Observe(ActivityObservation observation, DateTime now)
    {
        string? category = observation.Category;
        if (!started) { started = true; Take(observation, now); return true; }
        if (category == stable?.Category) { if (category is not null) stable = observation; hasCandidate = false; return false; }
        if (!hasCandidate || candidate != category) { candidate = category; hasCandidate = true; candidateSince = now; return false; }
        if ((now - candidateSince).TotalSeconds < StableSeconds) return false;
        Take(observation, candidateSince);
        return true;
    }

    private void Take(ActivityObservation observation, DateTime since)
    {
        stable = observation.Category is null ? null : observation;
        stableSince = since; hasCandidate = false;
    }

    /// <summary>A lock or a sleep: the next observation is taken at once.</summary>
    internal void Reset() { started = false; stable = null; hasCandidate = false; }

    internal bool Known => started;

    internal ActivityState? Current(DateTime now) => stable is null ? null
        : new ActivityState(stable.Category!, stable.App, Math.Max(0, (now - stableSince).TotalSeconds), stable.Fullscreen, stable.Title);
}

internal sealed record MediaInfo(string Title, string? Artist, string? App);

/// <summary>V3-P88 §6.1: a program outside the app (and outside the user's exclusions) uses the microphone or camera.</summary>
internal static class MicCamera
{
    /// <summary>
    /// <paramref name="entries"/>: the CapabilityAccessManager consent store's apps (a packaged app's name, or a
    /// path with '#' for '\'), with when they last started and stopped using it. In use: started, not stopped.
    /// </summary>
    internal static bool InUse(IEnumerable<(string App, long Started, long Stopped)> entries, IReadOnlySet<string> excluded, string ownProgram)
    {
        string own = ActivityCatalog.ProgramKey(ownProgram);
        foreach (var (app, started, stopped) in entries)
        {
            if (started <= 0 || stopped != 0) continue;
            string key = ActivityCatalog.ProgramKey(app.Replace('#', '\\'));
            int underscore = key.IndexOf('_');  // a packaged app: "Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe"
            string packaged = underscore > 0 ? key[..underscore] : key;
            int dot = packaged.LastIndexOf('.');
            string shortName = dot >= 0 ? packaged[(dot + 1)..] : packaged;
            if (key.Equals(own, StringComparison.OrdinalIgnoreCase)) continue;
            if (excluded.Contains(key) || excluded.Contains(packaged) || excluded.Contains(shortName)) continue;
            return true;
        }
        return false;
    }
}

/// <summary>The presence body (V3-P88 §2.1, §14): the whole snapshot; a signal the user turned off is left out.</summary>
internal static class PresenceBody
{
    internal const int NameChars = 64, TitleChars = 120;

    internal static Dictionary<string, object?> Build(int idleSeconds, string timeZone, bool serverTakesSignals, bool locked,
        string? after, ActivityState? activity, bool activityKnown, MediaInfo? media, bool mediaKnown, bool? micOrCamera,
        SignalSettings settings, IReadOnlySet<string>? titleExclusions = null)
    {
        var body = new Dictionary<string, object?> { ["idle_seconds"] = idleSeconds, ["time_zone"] = timeZone };
        if (!serverTakesSignals) return body;  // an older server refuses fields it does not know
        body["session"] = locked ? "locked" : "active";
        if (after is not null && !locked) body["after"] = after;
        if (locked) return body;  // nothing is in front of a locked screen
        if (settings.Activity && activityKnown)
        {
            body["activity"] = activity is null ? null : ActivityBody(activity, settings,
                titleExclusions ?? SignalSettings.Names(settings.TitleExclusions));
        }
        if (settings.Media && mediaKnown)
        {
            body["media"] = media is null ? null : new Dictionary<string, object?>
            {
                ["title"] = Cut(media.Title, TitleChars),
                ["artist"] = Cut(media.Artist, TitleChars),
                ["app"] = Cut(media.App, NameChars),
            }.Where(p => p.Value is not null).ToDictionary(p => p.Key, p => p.Value);
        }
        if (settings.MicCamera && micOrCamera is bool inUse) body["mic_or_camera"] = inUse;
        return body;
    }

    private static Dictionary<string, object?> ActivityBody(ActivityState activity, SignalSettings settings, IReadOnlySet<string> excluded)
    {
        var body = new Dictionary<string, object?> { ["category"] = activity.Category, ["for_seconds"] = (int)activity.ForSeconds,
            ["fullscreen"] = activity.Fullscreen };
        if (Cut(activity.App, NameChars) is { } app) body["app"] = app;
        if (settings.Titles && Cut(activity.Title, TitleChars) is { } title && !excluded.Contains(ActivityCatalog.ProgramKey(activity.App)))
            body["title"] = title;
        return body;
    }

    internal static string? Cut(string? text, int chars)
    {
        if (text is null) return null;
        string kept = string.Join(' ', text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)
            .Select(word => new string(word.Where(c => !char.IsControl(c)).ToArray())).Where(w => w.Length > 0));
        if (kept.Length == 0) return null;
        return kept.Length <= chars ? kept : kept[..chars];
    }
}

/// <summary>
/// When a presence signal goes out (V3-P88 §14): every 60 s, at once on a change of session, activity, microphone
/// or camera, or the user's settings, and for media at most every 10 s. The <c>after</c> mark rides on every send
/// until one is accepted.
/// </summary>
internal sealed class SignalSchedule
{
    internal const double PeriodSeconds = 60, MediaSeconds = 10;
    private DateTime lastSent = DateTime.MinValue, lastMedia = DateTime.MinValue;
    private bool urgent, mediaChanged;
    private MediaInfo? media; private bool mediaKnown;
    private bool? micOrCamera;
    internal string? After { get; private set; }

    internal void Urgent() => urgent = true;
    internal void Mark(string after) { After = after; urgent = true; }

    internal void SeeMedia(MediaInfo? value, bool known)
    {
        if (known == mediaKnown && Equals(value, media)) return;
        media = value; mediaKnown = known; mediaChanged = true;
    }

    internal void SeeMicCamera(bool? value) { if (value != micOrCamera) { micOrCamera = value; urgent = true; } }

    internal bool Due(DateTime now) => urgent || (now - lastSent).TotalSeconds >= PeriodSeconds
        || (mediaChanged && (now - lastMedia).TotalSeconds >= MediaSeconds);

    internal void Sent(DateTime now)
    {
        lastSent = now; urgent = false; After = null;
        if (mediaChanged) { mediaChanged = false; lastMedia = now; }
    }
}

/// <summary>V3-P88 §6.2: the "quiet" choices the character's menu offers.</summary>
internal static class QuietTimes
{
    internal static DateTimeOffset OneHour(DateTimeOffset now) => now.AddHours(1);

    /// <summary>The next 08:00 on this computer's clock.</summary>
    internal static DateTimeOffset NextMorning(DateTimeOffset now)
    {
        var morning = new DateTimeOffset(now.Year, now.Month, now.Day, 8, 0, 0, now.Offset);
        return morning > now ? morning : morning.AddDays(1);
    }

    internal static string Describe(DateTimeOffset? until, DateTimeOffset now) => until is not { } end || end <= now
        ? "히마리가 먼저 말을 걸 수 있어요"
        : $"조용히: {end.ToLocalTime():M월 d일 HH:mm}까지";
}
