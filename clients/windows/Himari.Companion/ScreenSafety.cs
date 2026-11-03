using System.Text.Json;

namespace Himari.Companion;

internal static class ScreenSafety
{
    internal static bool Ready(JsonElement file) => file.ValueKind == JsonValueKind.Object &&
        file.TryGetProperty("image", out var image) && image.ValueKind == JsonValueKind.Object &&
        image.TryGetProperty("state", out var state) && state.ValueKind == JsonValueKind.String && state.GetString() == "ready";
}

internal sealed class MoveJobLifetime
{
    private readonly HashSet<string> ended = new();
    private readonly HashSet<string> liveClosed = new();
    private readonly HashSet<string> verifying = new();
    internal bool CanStart(string job) => !ended.Contains(job);
    internal void End(string job) => ended.Add(job);
    internal bool ReserveVerification(string job)
    {
        if (!CanStart(job)) return false;
        return verifying.Add(job);
    }
    internal bool IsVerifying(string job) => verifying.Contains(job);
    internal bool CanBind(string job) => CanStart(job) || IsVerifying(job);
    internal void FinishVerification(string job) => verifying.Remove(job);
    internal bool CanReceive(string job) => !liveClosed.Contains(job);
    internal void CompleteLive(string job) { ended.Add(job); liveClosed.Add(job); }
    internal void Reset() { ended.Clear(); liveClosed.Clear(); verifying.Clear(); }
}

/// <summary>A window that shows: its program (null when unknown) and its visible frame, in physical pixels.</summary>
internal readonly record struct VisibleWindow(string? Program, PixelRect Frame);

internal static class ScreenWindows
{
    /// <summary>V3-P91 §14 Q10: the programs with a window on the monitor that is captured — any window that shows
    /// there, in front or not, so what is checked is what the image can hold.</summary>
    internal static IEnumerable<string?> On(IEnumerable<VisibleWindow> windows, PixelRect monitor) =>
        windows.Where(w => w.Frame.Width > 0 && w.Frame.Height > 0
            && w.Frame.Left < monitor.Left + monitor.Width && monitor.Left < w.Frame.Left + w.Frame.Width
            && w.Frame.Top < monitor.Top + monitor.Height && monitor.Top < w.Frame.Top + w.Frame.Height).Select(w => w.Program);
}

/// <summary>One capture at a time (V3-P84: the companion is never in the image). Without the capture exclusion the
/// windows are hidden and shown again; two captures at once would show them in the middle of the other.</summary>
internal sealed class CaptureGate
{
    private readonly SemaphoreSlim gate = new(1, 1);

    /// <summary>Runs <paramref name="work"/> alone; false when the gate did not open within <paramref name="wait"/>.</summary>
    internal async Task<(bool Ran, T? Value)> RunAsync<T>(Func<Task<T>> work, TimeSpan wait, CancellationToken ct)
    {
        if (!await gate.WaitAsync(wait, ct)) return (false, default);
        try { return (true, await work()); }
        finally { gate.Release(); }
    }
}
