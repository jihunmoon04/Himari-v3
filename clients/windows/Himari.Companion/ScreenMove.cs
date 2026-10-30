using System.Text.Json;

namespace Himari.Companion;

// Coordinates are physical pixels, including negative virtual-screen origins.
internal readonly record struct PixelRect(int Left, int Top, int Width, int Height);
internal readonly record struct PixelPoint(double X, double Y);
internal readonly record struct ScreenGeometry(PixelRect Monitor, int ImageWidth, int ImageHeight)
{
    internal static (int Width, int Height) ImageSize(int width, int height)
    {
        if (width <= 0 || height <= 0) throw new ArgumentOutOfRangeException(nameof(width));
        double scale = Math.Min(1, Math.Min(1280.0 / Math.Max(width, height), Math.Sqrt(1_150_000.0 / ((double)width * height))));
        return (Math.Max(1, (int)Math.Floor(width * scale)), Math.Max(1, (int)Math.Floor(height * scale)));
    }

    internal PixelPoint Map(int x, int y)
    {
        if (ImageWidth <= 0 || ImageHeight <= 0 || Monitor.Width <= 0 || Monitor.Height <= 0) throw new InvalidOperationException("Invalid screen geometry.");
        return new(Monitor.Left + Math.Clamp(x, 0, ImageWidth - 1) * (double)Monitor.Width / ImageWidth,
                   Monitor.Top + Math.Clamp(y, 0, ImageHeight - 1) * (double)Monitor.Height / ImageHeight);
    }
}

internal readonly record struct ScreenMove(int At, int X, int Y);

// One instance belongs to a single job and its locally captured screen.
internal sealed class MovePlayback(string jobId, ScreenGeometry geometry, DateTimeOffset capturedAt)
{
    private readonly Dictionary<string, int> played = new();
    internal ScreenGeometry Geometry => geometry;
    internal bool IsFresh(DateTimeOffset now) => now >= capturedAt && now - capturedAt <= TimeSpan.FromMinutes(10);

    internal IReadOnlyList<ScreenMove> Accept(string? eventJobId, string? runId, bool snapshot, JsonElement moves, int textLength, DateTimeOffset now)
    {
        if (string.IsNullOrEmpty(jobId) || eventJobId != jobId || string.IsNullOrEmpty(runId) || !IsFresh(now) || moves.ValueKind != JsonValueKind.Array) return [];
        var eligible = new List<ScreenMove>();
        foreach (var item in moves.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object || !item.TryGetProperty("at", out var atValue) || atValue.ValueKind != JsonValueKind.Number || !atValue.TryGetInt32(out int at) || at < 0 || at > textLength ||
                !item.TryGetProperty("x", out var xValue) || xValue.ValueKind != JsonValueKind.Number || !xValue.TryGetInt32(out int x) ||
                !item.TryGetProperty("y", out var yValue) || yValue.ValueKind != JsonValueKind.Number || !yValue.TryGetInt32(out int y)) continue;
            eligible.Add(new(at, x, y));
        }
        int previous = played.GetValueOrDefault(runId);
        if (snapshot)
        {
            if (eligible.Count <= previous) return [];
            played[runId] = eligible.Count;
            return [eligible[^1]];
        }
        played[runId] = previous + eligible.Count;
        return eligible;
    }
}
