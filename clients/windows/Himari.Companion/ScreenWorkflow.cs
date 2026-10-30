using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Interop;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

public partial class MainWindow
{
    private bool shareEveryMessage;
    private sealed record LocalScreen(string FileId, ScreenGeometry Geometry, DateTimeOffset CapturedAt);
    private sealed record MoveJob(string ConversationId, MovePlayback Playback);
    private readonly Dictionary<string, LocalScreen> localScreens = new();
    private readonly Dictionary<string, MoveJob> moveJobs = new();
    private readonly Dictionary<string, List<(string Job, string Run, bool Snapshot, JsonElement Moves, int Length)>> earlyMoves = new();
    private string? pendingScreenConversation;
    private (string Conversation, string Text, LocalScreen Screen)? unresolvedScreen;
    private bool autoMoveActive;
    private int autoMoveVersion;
    private NativeRect? moveHome;
    private Task moveQueue = Task.CompletedTask;

    [StructLayout(LayoutKind.Sequential)] private struct NativeRect { internal int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out NativeRect rect);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);

    private async Task<CapturedScreen> CaptureScreenAsync(CancellationToken ct)
    {
        // Choose before hiding: hiding the composer changes the foreground window.
        var bounds = ScreenCapture.SelectMonitor();
        var visible = System.Windows.Application.Current.Windows.Cast<Window>().Where(w => w.IsVisible).ToArray();
        try
        {
            foreach (var window in visible.Reverse()) window.Hide();
            await Task.Delay(200, ct); // let the desktop compositor remove our windows
            ct.ThrowIfCancellationRequested();
            return ScreenCapture.Capture(bounds);
        }
        finally
        {
            foreach (var window in visible) window.Show();
        }
    }

    private async Task<LocalScreen?> FindLatestLocalScreen(ServerClient server, string conversation, string jobId, CancellationToken ct)
    {
        async Task<JsonElement> Detail() => await server.SendAsync(System.Net.Http.HttpMethod.Get, PathFor(conversation), null, ct);
        bool SameRunningJob(JsonElement detail) => Str(Prop(detail, "job"), "job_id") == jobId && ConversationView.IsRunning(detail);
        var detail = await Detail();
        if (!SameRunningJob(detail)) return null;
        var sequence = Prop(Prop(detail, "conversation"), "last_sequence");
        if (sequence.ValueKind != JsonValueKind.Number || !sequence.TryGetInt32(out int last)) return null;
        int after = Math.Max(0, last - 100);
        string? found = null;
        while (true)
        {
            var page = await server.SendAsync(System.Net.Http.HttpMethod.Get, PathFor(conversation) + $"/events?after={after}&limit=100", null, ct);
            foreach (var item in Items(Prop(page, "items")).Reverse())
            {
                if (Str(item, "kind") != "user_message") continue;
                foreach (var attachment in Items(Prop(item, "attachments")).Reverse())
                    if (Prop(attachment, "screen").ValueKind == JsonValueKind.True) { found = Str(attachment, "file_id"); break; }
                if (found is not null) break;
            }
            if (found is not null || after == 0) break;
            after = Math.Max(0, after - 100);
        }
        if (found is null || !localScreens.TryGetValue(found, out var screen) || !SameRunningJob(await Detail())) return null;
        return screen;
    }

    private void BeginMoveJob(string conversation, string? jobId, LocalScreen? screen)
    {
        if (string.IsNullOrEmpty(jobId) || screen is null) return;
        if (!moveJobs.ContainsKey(jobId)) moveJobs[jobId] = new(conversation, new(jobId, screen.Geometry, screen.CapturedAt));
        if (earlyMoves.Remove(conversation, out var buffered))
            foreach (var item in buffered.Where(x => x.Job == jobId)) ApplyMoves(conversation, item.Job, item.Run, item.Snapshot, item.Moves, item.Length);
    }

    private void ReceiveMoves(string conversation, string? job, string? run, bool snapshot, JsonElement moves, int length)
    {
        if (string.IsNullOrEmpty(job) || string.IsNullOrEmpty(run) || moves.ValueKind != JsonValueKind.Array) return;
        if (moveJobs.ContainsKey(job)) { ApplyMoves(conversation, job, run, snapshot, moves, length); return; }
        if (pendingScreenConversation != conversation) return;
        if (!earlyMoves.TryGetValue(conversation, out var pending)) earlyMoves[conversation] = pending = [];
        if (pending.Count < 256) pending.Add((job, run, snapshot, moves.Clone(), length));
    }

    private void ApplyMoves(string conversation, string job, string run, bool snapshot, JsonElement moves, int length)
    {
        if (!moveJobs.TryGetValue(job, out var context) || context.ConversationId != conversation) return;
        var accepted = context.Playback.Accept(job, run, snapshot, moves, length, DateTimeOffset.UtcNow);
        if (settings.MoveEnabled.IsChecked != true || conversation != currentConversation || !IsVisible) return;
        foreach (var move in accepted)
        {
            int version = autoMoveVersion;
            moveQueue = AnimateMoveAfterAsync(moveQueue, conversation, context.Playback, move, version);
        }
    }

    private async Task AnimateMoveAfterAsync(Task previous, string conversation, MovePlayback playback, ScreenMove move, int version)
    {
        try { await previous; } catch (Exception) { }
        if (version != autoMoveVersion || conversation != currentConversation || settings.MoveEnabled.IsChecked != true || !IsVisible || !playback.IsFresh(DateTimeOffset.UtcNow)) return;
        var handle = new WindowInteropHelper(this).Handle;
        if (!GetWindowRect(handle, out var start)) return;
        if (!autoMoveActive) { moveHome = start; autoMoveActive = true; saveTimer.Stop(); }
        var point = playback.Geometry.Map(move.X, move.Y);
        var anchor = Character.PointToScreen(new System.Windows.Point(Character.ActualWidth * 0.1, Character.ActualHeight * 0.75));
        var work = Forms.Screen.FromPoint(new System.Drawing.Point((int)point.X, (int)point.Y)).WorkingArea;
        int width = start.Right - start.Left, height = start.Bottom - start.Top;
        int x = Math.Clamp((int)Math.Round(point.X - (anchor.X - start.Left)), work.Left, Math.Max(work.Left, work.Right - width));
        int y = Math.Clamp((int)Math.Round(point.Y - (anchor.Y - start.Top)), work.Top, Math.Max(work.Top, work.Bottom - height));
        await AnimateNative(handle, start.Left, start.Top, x, y, version);
    }

    private async Task AnimateNative(IntPtr handle, int fromX, int fromY, int toX, int toY, int version)
    {
        var watch = System.Diagnostics.Stopwatch.StartNew();
        while (version == autoMoveVersion && watch.ElapsedMilliseconds < 600)
        {
            double t = Math.Clamp(watch.Elapsed.TotalMilliseconds / 600, 0, 1); t = t * t * (3 - 2 * t);
            SetWindowPos(handle, IntPtr.Zero, (int)Math.Round(fromX + (toX - fromX) * t), (int)Math.Round(fromY + (toY - fromY) * t), 0, 0, 0x0015);
            await Task.Delay(16);
        }
        if (version == autoMoveVersion) SetWindowPos(handle, IntPtr.Zero, toX, toY, 0, 0, 0x0015);
    }

    private void CancelAutomaticMove() { autoMoveVersion++; autoMoveActive = false; moveHome = null; }

    private void RestoreMoveHomeOnClose()
    {
        if (autoMoveActive && moveHome is NativeRect home)
            SetWindowPos(new WindowInteropHelper(this).Handle, IntPtr.Zero, home.Left, home.Top, 0, 0, 0x0015);
        CancelAutomaticMove();
    }

    private async void ReturnFromMove()
    {
        if (!autoMoveActive || moveHome is not NativeRect home) { autoMoveVersion++; return; }
        int version = ++autoMoveVersion;
        var handle = new WindowInteropHelper(this).Handle;
        if (GetWindowRect(handle, out var start)) await AnimateNative(handle, start.Left, start.Top, home.Left, home.Top, version);
        if (version == autoMoveVersion) { autoMoveActive = false; moveHome = null; }
    }
}
