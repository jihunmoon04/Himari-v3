namespace Himari.Companion;

internal enum AnimationLayer { Ambient, Reaction, Directed }

internal sealed class AnimationRequest
{
    internal AnimationRequest(string clip, AnimationLayer layer, TimeSpan start, TimeSpan? end, long order) { Clip = clip; Layer = layer; Start = start; End = end; Order = order; }
    internal string Clip { get; }
    internal AnimationLayer Layer { get; }
    internal TimeSpan Start { get; }
    internal TimeSpan? End { get; set; }
    internal long Order { get; }
    // A one-shot clip requested without a duration: it ends after one full play, counted
    // from whenever it is shown (again), not from the request.
    internal bool PlaysOnce { get; set; }
}

internal sealed record CharacterPose(string Clip, int Frame, string Mood, string? Eyes, string? Mouth);

// Chooses what the character shows at a given time (docs/companion-animation-v0.1.md §5).
// Pure logic: the caller passes the time, so tests drive it without a clock or WPF.
internal sealed class CharacterAnimator
{
    internal static readonly TimeSpan TalkStep = TimeSpan.FromMilliseconds(130);
    private static readonly string?[] TalkCycle = ["small", "wide", "small", null];
    private static readonly (string Eyes, int Ms)[] Blink = [("half", 60), ("closed", 90), ("half", 60)];
    private static readonly TimeSpan DoubleBlinkGap = TimeSpan.FromMilliseconds(120);

    private readonly CharacterManifest manifest;
    private readonly Random random;
    private readonly List<AnimationRequest> requests = [];
    private readonly AnimationRequest idle;
    private AnimationRequest? sleep;
    private AnimationRequest current;
    private TimeSpan currentStart;
    private long order;
    private TimeSpan blinkAt;
    private bool doubleBlink;
    private TimeSpan? talkStart, talkStop;
    private bool sleeping;
    private string mood = "neutral";

    internal CharacterAnimator(CharacterManifest manifest, int seed, TimeSpan now)
    {
        this.manifest = manifest;
        random = new Random(seed);
        idle = current = new AnimationRequest("idle", AnimationLayer.Ambient, now, null, order++);
        currentStart = now;
        ScheduleBlink(now);
    }

    internal string Mood { get => mood; set => mood = value is not null && manifest.Faces.ContainsKey(value) ? value : "neutral"; }

    internal AnimationRequest Request(string clip, AnimationLayer layer, TimeSpan now, TimeSpan? duration = null)
    {
        if (!manifest.Clips.TryGetValue(clip, out var c)) throw new ArgumentException($"unknown clip {clip}", nameof(clip));
        // A one-shot clip without a duration ends when it has played once (see BodyFrame).
        var request = new AnimationRequest(clip, layer, now, duration is { } d ? now + d : null, order++) { PlaysOnce = duration is null && !c.Loop };
        requests.Add(request);
        return request;
    }

    internal void Release(AnimationRequest request, TimeSpan now)
    {
        if (request == idle) return;
        request.PlaysOnce = false;
        if (request.End is null || request.End > now) request.End = now;
    }

    internal void SetTalking(bool value, TimeSpan now)
    {
        if (value) { if (talkStart is null || talkStop is not null) { talkStart = now; talkStop = null; } return; }
        if (talkStart is not { } start || talkStop is not null) return;
        // Finish the step in progress, then close the mouth.
        long steps = (long)Math.Ceiling((now - start) / TalkStep);
        talkStop = start + TalkStep * Math.Max(1, steps);
    }

    // Dozing plays the sleep clip when the art has one; until then it only closes the eyes.
    internal void SetSleeping(bool value, TimeSpan now)
    {
        if (value == sleeping) return;
        sleeping = value;
        if (!manifest.Clips.ContainsKey("sleep")) return;
        if (value) sleep = Request("sleep", AnimationLayer.Ambient, now);
        else if (sleep is not null) { Release(sleep, now); sleep = null; }
    }

    internal CharacterPose Pose(TimeSpan now)
    {
        var (clip, frame) = BodyFrame(now);
        var face = manifest.Faces[Mood];
        string? eyes = frame.Eyes ?? (sleeping && !Talking(now) ? "closed" : BlinkEyes(now));
        string? mouth = frame.Mouth ?? TalkMouth(now);
        return new(current.Clip, Array.IndexOf(clip.Frames, frame), Mood,
            eyes is not null && face.Eyes?.ContainsKey(eyes) == true ? eyes : null,
            mouth is not null && face.Mouth?.ContainsKey(mouth) == true ? mouth : null);
    }

    // The next time the pose may change, so the caller can sleep until then.
    internal TimeSpan NextChange(TimeSpan now)
    {
        BodyFrame(now);
        var next = new List<TimeSpan> { BodyBoundary(now), BlinkBoundary(now) };
        foreach (var r in requests) if (r.End is { } end && end > now) next.Add(end);
        if (talkStart is { } start && (talkStop is null || talkStop > now))
            next.Add(start + TalkStep * ((long)Math.Floor((now - start) / TalkStep) + 1));
        return next.Where(t => t > now).DefaultIfEmpty(now + TimeSpan.FromSeconds(1)).Min();
    }

    private (CharacterClip Clip, ClipFrame Frame) BodyFrame(TimeSpan now)
    {
        requests.RemoveAll(r => r.End is { } end && end <= now && r != current);
        var active = requests.Where(r => r.Start <= now && (r.End is null || r.End > now))
            .OrderByDescending(r => r.Layer).ThenByDescending(r => r.Order).FirstOrDefault() ?? idle;
        if (active != current)
        {
            // A newer request starts when it was made; a lower layer shown again after the
            // current one ended restarts from its first frame at that moment (§5.3).
            bool ended = current.End is { } e && e <= now;
            currentStart = ended && current.End > active.Start ? current.End.Value : active.Start;
            if (ended) requests.Remove(current);
            // An interrupted one-shot plays in full again when it comes back.
            else if (current.PlaysOnce) current.End = null;
            current = active;
            if (current.PlaysOnce) current.End = currentStart + manifest.Clips[current.Clip].Length;
        }
        var clip = manifest.Clips[current.Clip];
        double t = (now - currentStart).TotalMilliseconds, total = clip.Length.TotalMilliseconds;
        t = clip.Loop ? t % total : Math.Min(t, total - 1);
        foreach (var f in clip.Frames) { if (t < f.Ms) return (clip, f); t -= f.Ms; }
        return (clip, clip.Frames[^1]);
    }

    private TimeSpan BodyBoundary(TimeSpan now)
    {
        var clip = manifest.Clips[current.Clip];
        if (clip.Frames.Length == 1) return now + TimeSpan.FromSeconds(1);
        double elapsed = (now - currentStart).TotalMilliseconds, total = clip.Length.TotalMilliseconds;
        double cycle = clip.Loop ? Math.Floor(elapsed / total) * total : 0, edge = cycle;
        foreach (var f in clip.Frames) { edge += f.Ms; if (edge > elapsed) return currentStart + TimeSpan.FromMilliseconds(edge); }
        return now + TimeSpan.FromSeconds(1);
    }

    private bool Talking(TimeSpan now) => talkStart is not null && (talkStop is null || talkStop > now);

    private string? TalkMouth(TimeSpan now)
    {
        if (talkStart is not { } start || !Talking(now)) return null;
        return TalkCycle[(int)(Math.Floor((now - start) / TalkStep) % TalkCycle.Length)];
    }

    private TimeSpan BlinkLength => TimeSpan.FromMilliseconds(Blink.Sum(b => b.Ms) * (doubleBlink ? 2 : 1)) + (doubleBlink ? DoubleBlinkGap : TimeSpan.Zero);

    // Every 2.5-6 s after the last blink; one blink in five is a double blink.
    private void ScheduleBlink(TimeSpan after)
    {
        blinkAt = after + TimeSpan.FromMilliseconds(random.Next(2500, 6001));
        doubleBlink = random.Next(5) == 0;
    }

    private string? BlinkEyes(TimeSpan now)
    {
        // A long pause (the PC slept) restarts the schedule instead of replaying missed blinks.
        if (now >= blinkAt + BlinkLength) ScheduleBlink(now - blinkAt - BlinkLength > TimeSpan.FromSeconds(6) ? now : blinkAt + BlinkLength);
        if (now < blinkAt) return null;
        double t = (now - blinkAt).TotalMilliseconds, single = Blink.Sum(b => b.Ms);
        if (doubleBlink && t >= single) { t -= single + DoubleBlinkGap.TotalMilliseconds; if (t < 0) return null; }
        foreach (var (eyes, ms) in Blink) { if (t < ms) return eyes; t -= ms; }
        return null;
    }

    private TimeSpan BlinkBoundary(TimeSpan now)
    {
        BlinkEyes(now);
        if (now < blinkAt) return blinkAt;
        double t = (now - blinkAt).TotalMilliseconds, edge = 0;
        var steps = Blink.Select(b => (double)b.Ms).ToList();
        if (doubleBlink) { steps.Add(DoubleBlinkGap.TotalMilliseconds); steps.AddRange(Blink.Select(b => (double)b.Ms)); }
        foreach (var ms in steps) { edge += ms; if (edge > t) return blinkAt + TimeSpan.FromMilliseconds(edge); }
        return blinkAt + BlinkLength;
    }
}
