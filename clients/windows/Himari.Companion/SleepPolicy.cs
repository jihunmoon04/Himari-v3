namespace Himari.Companion;

/// <summary>
/// V3-P90 §2: when the character sleeps and wakes. The app decides it and tells the server (<c>sleep</c>).
/// Pure: the caller passes the time and what it read, so tests drive it without Windows.
/// <list type="bullet">
/// <item>Asleep after <see cref="IdleSeconds"/> without input — unless something quiet goes on (media playing,
/// a call, a fullscreen window, §2.2) — at once when the screen turns off or locks, and when the app starts.</item>
/// <item>Awake again once an input is followed by another <see cref="ConfirmAfter"/> to <see cref="ConfirmWithin"/>
/// later (a bumped desk or mouse is one input, §2.3); at once on a key, a click on HIMARI, or an unlock.</item>
/// </list>
/// </summary>
internal sealed class SleepPolicy
{
    internal static readonly TimeSpan IdleSeconds = TimeSpan.FromSeconds(600);
    internal static readonly TimeSpan ConfirmAfter = TimeSpan.FromSeconds(5);
    internal static readonly TimeSpan ConfirmWithin = TimeSpan.FromSeconds(15);

    private DateTime? lastInput;  // the latest input seen
    private DateTime? firstInput;  // asleep: the input that may wake it, waiting for its confirmation
    private bool displayOff;

    /// <summary>What the body's <c>sleep</c> says: null awake, else "idle" or "display_off".
    /// It starts asleep (§2.3): the first use after the computer starts is a wake.</summary>
    internal string? Reason { get; private set; } = "idle";
    internal bool Locked { get; private set; }
    internal bool Asleep => Reason is not null || Locked;

    /// <summary>
    /// What the app read now: the moment of the latest input, whether the screen is on, locked, and whether
    /// something quiet goes on. True when it fell asleep or woke.
    /// </summary>
    internal bool Observe(DateTime now, DateTime latestInput, bool displayOn, bool locked, bool quiet)
    {
        bool was = Asleep; string? wasReason = Reason;
        bool fresh = lastInput is not { } seen || latestInput > seen;
        lastInput = latestInput;
        displayOff = !displayOn;
        if (locked) { Locked = true; Reason ??= "idle"; firstInput = null; return was != Asleep; }
        if (Locked) Locked = false;  // unlocked: still asleep until Unlocked() or an input confirms
        if (!Asleep)
        {
            if (displayOff) Reason = "display_off";
            else if (!quiet && now - latestInput >= IdleSeconds) Reason = "idle";
            return Asleep;
        }
        Reason = displayOff ? "display_off" : "idle";
        if (firstInput is { } first && now - first > ConfirmWithin) firstInput = null;  // nothing followed it
        if (fresh && !displayOff && latestInput > now - ConfirmWithin)
        {
            if (firstInput is not { } start) firstInput = latestInput;
            else if (latestInput - start >= ConfirmAfter) Wake();
        }
        return was != Asleep || wasReason != Reason;
    }

    /// <summary>A key, a click on HIMARI, an unlock: awake at once.</summary>
    internal bool WakeNow()
    {
        if (!Asleep) return false;
        Locked = false; Wake();
        return true;
    }

    /// <summary>The computer woke from sleep: the app goes on asleep (§2.1).</summary>
    internal bool FallAsleep()
    {
        if (Asleep) return false;
        Reason = displayOff ? "display_off" : "idle"; firstInput = null;
        return true;
    }

    private void Wake() { Reason = null; firstInput = null; }
}
