using System.Text;

namespace Himari.Companion;

internal static class DialoguePolicy
{
    internal static readonly string[] Hotkeys = ["Alt+Q", "Alt+Shift+Q", "Ctrl+Space", "Ctrl+Alt+H"];
    internal static readonly string[] BubblePlacements = ["상하 자동", "상 고정", "하 고정"];
    internal static (uint Modifiers, uint Key) Hotkey(string value) => value switch
    {
        "Alt+Shift+Q" => (0x0001 | 0x0004 | 0x4000, 0x51),
        "Ctrl+Space" => (0x0002 | 0x4000, 0x20),
        "Ctrl+Alt+H" => (0x0001 | 0x0002 | 0x4000, 0x48),
        _ => (0x0001 | 0x4000, 0x51)
    };
    internal static double Finite(double value, double fallback, double minimum, double maximum) => double.IsFinite(value) ? Math.Clamp(value, minimum, maximum) : fallback;
    internal static double Duration(string text, double minimum, double per100) => Math.Min(300,
        Finite(minimum, 8, 3, 30) + text.EnumerateRunes().Count() / 100.0 * Finite(per100, 8, 3, 30));
    internal static double PointAnchor(bool facingLeft) => facingLeft ? .9 : .1;
    // Switching away does not depend on the editor being visible.
    internal static bool HotkeyCloses(bool companionInFront, bool composerOpen) => companionInFront;
    internal static bool BubblesBelow(string placement, double characterCenter, double monitorCenter) =>
        placement == "하 고정" || (placement != "상 고정" && characterCenter < monitorCenter);
    internal static bool KeepUserBubble(bool hovered, bool focused, string draft, bool sending, bool unresolved) =>
        hovered || focused || !string.IsNullOrWhiteSpace(draft) || sending || unresolved;
}

internal sealed class HotkeyState
{
    internal string Current { get; set; } = "Alt+Q";
    internal bool IsRegistered { get; private set; }
    internal bool Select(string selected, Func<string, bool> register, Action unregister)
    {
        if (!DialoguePolicy.Hotkeys.Contains(selected)) return false;
        if (selected == Current && IsRegistered) return true;
        string prior = Current; bool hadPrior = IsRegistered;
        if (hadPrior) unregister();
        IsRegistered = register(selected);
        if (IsRegistered) { Current = selected; return true; }
        if (hadPrior) IsRegistered = register(prior);
        return false;
    }
}

internal sealed class WorkWindowHistory
{
    internal IntPtr Target { get; private set; }
    internal void Remember(IntPtr window, Func<IntPtr, bool> eligible) { if (window != IntPtr.Zero && eligible(window)) Target = window; }
    internal bool Return(Func<IntPtr, bool> restore) => Target != IntPtr.Zero && restore(Target);
}
