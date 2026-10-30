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
