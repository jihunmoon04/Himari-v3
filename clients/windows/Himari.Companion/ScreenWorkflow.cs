using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Interop;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

public partial class MainWindow
{
    private bool shareEveryMessage;
    private sealed record LocalScreen(string FileId, ScreenGeometry Geometry, DateTimeOffset CapturedAt, bool Ready);
    private sealed record MoveJob(string ConversationId, MovePlayback Playback);
    private readonly Dictionary<string, LocalScreen> localScreens = new();
    private readonly Dictionary<string, string> previousScreenIds = new();
    private readonly Dictionary<string, MoveJob> moveJobs = new();
    private readonly MoveJobLifetime moveLifetime = new();
    private readonly Dictionary<string, string> moveVerifications = new();
    private LocalScreen? submittedScreen;
    private string? submittedConversation;
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
        bool excluded = OperatingSystem.IsWindowsVersionAtLeast(10, 0, 19041) &&
            visible.All(w => ScreenCapture.TryExcludeWindow(new WindowInteropHelper(w).Handle));
        try
        {
            if (!excluded) foreach (var window in visible.Reverse()) window.Hide();
            await Task.Delay(200, ct); // let the desktop compositor remove our windows
            ct.ThrowIfCancellationRequested();
            return ScreenCapture.Capture(bounds);
        }
        finally
        {
            if (!excluded) foreach (var window in visible) window.Show();
        }
    }

    private async Task<LocalScreen?> FindLatestLocalScreen(ServerClient server, string conversation, string jobId, CancellationToken ct, LocalScreen? expected = null)
    {
        // A text-only follow-up has no new attachment. Pin the last locally
        // accepted screen for this conversation before awaiting server reads.
        string? previous = previousScreenIds.GetValueOrDefault(conversation);
        string? id = await ScreenHistory.LatestReadyIdAsync(server, conversation, jobId, ct, expected?.FileId, previous);
        return id is not null && localScreens.TryGetValue(id, out var screen) && screen.Ready ? screen : null;
    }

    private void BeginMoveJob(string conversation, string? jobId, LocalScreen? screen)
    {
        if (string.IsNullOrEmpty(jobId) || screen is null || !screen.Ready || !moveLifetime.CanBind(jobId)) return;
        previousScreenIds[conversation] = screen.FileId;
        if (!moveJobs.ContainsKey(jobId)) moveJobs[jobId] = new(conversation, new(jobId, screen.Geometry, screen.CapturedAt));
        if (earlyMoves.TryGetValue(conversation, out var buffered))
        {
            var matching = buffered.Where(x => x.Job == jobId).ToArray();
            buffered.RemoveAll(x => x.Job == jobId);
            foreach (var item in matching) ApplyMoves(conversation, item.Job, item.Run, item.Snapshot, item.Moves, item.Length);
        }
        if (!moveLifetime.CanReceive(jobId))
        {
            moveJobs.Remove(jobId);
            if (conversation == currentConversation) _ = ScheduleBubbleAfterMoves(conversation);
        }
    }

    private async Task RecoverMoveJob(string conversation, string? jobId, ServerClient server, CancellationToken ct, LocalScreen? acceptedScreen = null)
    {
        if (jobId is null || moveJobs.ContainsKey(jobId) || !moveLifetime.ReserveVerification(jobId)) return;
        moveVerifications[jobId] = conversation;
        try
        {
            var expected = acceptedScreen ?? (submittedConversation == conversation ? submittedScreen : null);
            var screen = await FindLatestLocalScreen(server, conversation, jobId, ct, expected);
            // The recorded screen, rather than event arrival order, binds the request.
            if (screen is null || !ReferenceEquals(server, client)) return;
            if (submittedConversation == conversation && submittedScreen is { } pending && pending.FileId != screen.FileId) return;
            BeginMoveJob(conversation, jobId, screen);
        }
        catch (Exception) when (!ct.IsCancellationRequested) { /* No verified context: do not move. */ }
        finally
        {
            moveLifetime.FinishVerification(jobId);
            moveVerifications.Remove(jobId);
            if (earlyMoves.TryGetValue(conversation, out var remaining)) remaining.RemoveAll(x => x.Job == jobId);
            if (!moveLifetime.CanReceive(jobId))
            {
                moveJobs.Remove(jobId);
                if (earlyMoves.TryGetValue(conversation, out var buffered)) buffered.RemoveAll(x => x.Job == jobId);
                if (conversation == currentConversation) _ = ScheduleBubbleAfterMoves(conversation);
            }
        }
    }

    private void EndMoveJob(string conversation, string? jobId)
    {
        if (jobId is null) return;
        // The event stream can end a job before live delivers its last delta.
        // Block new contexts, keeping existing live playback open until done.
        moveLifetime.End(jobId);
    }

    private void CompleteLiveMoves(string conversation, string? jobId)
    {
        if (jobId is null) return;
        moveLifetime.CompleteLive(jobId); moveJobs.Remove(jobId);
        if (!moveLifetime.IsVerifying(jobId) && earlyMoves.TryGetValue(conversation, out var buffer)) buffer.RemoveAll(x => x.Job == jobId);
        // Accepted animations own their playback data and finish after done.
    }

    private void ReceiveMoves(string conversation, string? job, string? run, bool snapshot, JsonElement moves, int length)
    {
        if (string.IsNullOrEmpty(job) || string.IsNullOrEmpty(run) || !moveLifetime.CanReceive(job) || moves.ValueKind != JsonValueKind.Array) return;
        if (moveJobs.ContainsKey(job)) { ApplyMoves(conversation, job, run, snapshot, moves, length); return; }
        if (!moveLifetime.CanStart(job) && !moveLifetime.IsVerifying(job)) return;
        if (pendingScreenConversation != conversation && !moveLifetime.IsVerifying(job)) return;
        if (!earlyMoves.TryGetValue(conversation, out var pending)) earlyMoves[conversation] = pending = [];
        if (pending.Count < 256) pending.Add((job, run, snapshot, moves.Clone(), length));
    }

    private void ApplyMoves(string conversation, string job, string run, bool snapshot, JsonElement moves, int length)
    {
        if (!moveJobs.TryGetValue(job, out var context) || context.ConversationId != conversation) return;
        var accepted = context.Playback.Accept(job, run, snapshot, moves, length, DateTimeOffset.UtcNow);
        if (settings.MoveEnabled.IsChecked != true || conversation != currentConversation || !IsVisible) return;
        if (accepted.Count > 0) bubbleTimer.Stop();
        foreach (var move in accepted)
        {
            int version = autoMoveVersion;
            moveQueue = MoveQueue.RunAfterAsync(moveQueue, () => AnimateMoveAsync(conversation, context.Playback, move, version));
        }
    }

    private async Task AnimateMoveAsync(string conversation, MovePlayback playback, ScreenMove move, int version)
    {
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
