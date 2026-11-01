using System.Net;
using System.Text;
using System.Text.Json;
using System.Windows;

namespace Himari.Companion;

public partial class MainWindow
{
    private async Task EventLoop(ServerClient local, CancellationToken ct)
    {
        int delay = 1;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await foreach (var (type, data) in local.StreamAsync("/api/events", ct))
                {
                    if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                    if (type == "hello")
                    {
                        delay = 1; SetConnected(true, "연결됨");
                        deviceId = Str(Prop(data, "device"), "device_id") ?? deviceId;
                        ServerFeatures(data);
                        bool returned = UpdateCurrent(Str(data, "current_device"));
                        await HelloReconciliation.ApplyAsync(
                            async () => { if (currentConversation is null || (returned && string.IsNullOrWhiteSpace(Input.Text))) await RefreshConversations(Str(Prop(data, "recent_conversation"), "id"), ct); },
                            () => StartOwnJobs(Prop(data, "activity"), local, ct),
                            async () => { if (currentConversation is not null) await ReadConversation(currentConversation, ct); }, ct);
                        if (unknownText is not null) await CheckUnknownText(false, ct);
                    }
                    else if (type == "activity")
                    {
                        StartOwnJobs(data, local, ct);
                        if (currentConversation is not null && !liveStreams.ContainsKey(currentConversation)) await ReadConversation(currentConversation, ct);
                    }
                    else if (type == "job")
                    {
                        string? id = Str(data, "conversation"), state = Str(data, "state");
                        if (state == "started" && IsOwn(Prop(data, "origin")) && id is not null)
                        {
                            // V3-P87 §10: HIMARI speaks first in that conversation (FollowWake).
                            if (JobOrigin.IsWake(Prop(data, "origin")) && id != currentConversation) await FollowWake(id, local, ct);
                            await RecoverMoveJob(id, Str(data, "job_id"), local, ct);
                            StartLive(id, local, ct, Str(data, "job_id"));
                        }
                        if (state == "ended" && id is not null)
                        {
                            if (id == wakeElsewhere) wakeElsewhere = null;  // HIMARI finished speaking there
                            EndMoveJob(id, Str(data, "job_id"));
                            if (id == currentConversation && Str(data, "outcome") is null && Prop(data, "error").ValueKind is not (JsonValueKind.Null or JsonValueKind.Undefined)) Status.Text = "작업 중 문제가 생겼습니다. Web에서 확인해 주세요.";
                        }
                    }
                    else if (type == "conversation" && Str(data, "conversation") == currentConversation && currentConversation is not null && !liveStreams.ContainsKey(currentConversation))
                        await ReadConversation(currentConversation, ct);
                    else if (type == "current")
                    {
                        if (UpdateCurrent(Str(data, "device_id")) && string.IsNullOrWhiteSpace(Input.Text))
                        {
                            var info = await local.SendAsync(System.Net.Http.HttpMethod.Get, "/api/device", null, ct);
                            await RefreshConversations(Str(Prop(info, "recent_conversation"), "id"), ct);
                        }
                    }
                    else if (type == "quiet") QuietFromServer(data, "until");  // V3-P88 §6.2: set on any device or the Web
                    else if (type == "revoked")
                    {
                        DeleteActiveIfCurrent(local); StopConnection(); PairPanel.Visibility = Visibility.Visible;
                        SetConnected(false, "기기 연결이 해제되었습니다. 다시 짝짓기해 주세요."); return;
                    }
                    else if (type == "replaced") { Status.Text = "다른 히마리 앱이 연결되어 이 앱을 종료합니다."; Close(); return; }
                }
                if (!ct.IsCancellationRequested) { CancelLive(); SetConnected(false, "다시 연결 중…"); }
                // A normal five-minute stream end reconnects immediately.
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (ApiException ex) when (ex.Status == HttpStatusCode.Unauthorized)
            {
                if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                DeleteActiveIfCurrent(local); StopConnection(); PairPanel.Visibility = Visibility.Visible;
                SetConnected(false, "인증이 만료되었습니다. 다시 짝짓기해 주세요."); return;
            }
            catch (Exception ex)
            {
                if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                CancelLive();
                if (ex is ApiException { Code: "device_scope" }) System.Diagnostics.Trace.TraceWarning("HIMARI device_scope: event stream denied.");
                SetConnected(false, ex is ApiException { Code: "companion_off" } ? "서버에서 반려 기능이 꺼져 있습니다." : "연결이 끊겼습니다. 다시 연결 중…");
                int wait = ex is ApiException { Code: "companion_off" } ? 300 : ex is ApiException { Code: "too_many_streams" } ? 30 : delay;
                delay = Math.Min(delay * 2, 30);
                try { await Task.Delay(TimeSpan.FromSeconds(wait), ct); } catch (OperationCanceledException) { return; }
            }
        }
    }
    private bool UpdateCurrent(string? current)
    {
        if (current != deviceId) { awaySince ??= DateTime.UtcNow; return false; }
        bool returned = awaySince is DateTime since && DateTime.UtcNow - since > TimeSpan.FromMinutes(10);
        awaySince = null; return returned;
    }
    private bool IsOwn(JsonElement origin) => JobOrigin.IsOwn(origin, deviceId);
    private void CancelLive()
    {
        foreach (var stream in liveStreams.Values) { stream.Cancel(); stream.Dispose(); }
        liveStreams.Clear(); liveDisplays.Clear();
        EndReplyProgress(closeEmpty: true);
        StopButton.Visibility = Visibility.Collapsed;
    }
    private void StartOwnJobs(JsonElement activity, ServerClient local, CancellationToken ct)
    {
        foreach (var job in Items(Prop(activity, "working")))
        {
            string? id = Str(job, "id");
            if (id is not null && IsOwn(Prop(job, "origin")))
                _ = RecoverAndStartLive(id, Str(job, "job_id"), local, ct, JobOrigin.IsWake(Prop(job, "origin")));
        }
    }
    private async Task RecoverAndStartLive(string id, string? job, ServerClient local, CancellationToken ct, bool wake = false)
    {
        try
        {
            // PR #159 review P2-1: a wake found running after a reconnect is followed like a new one
            if (wake && id != currentConversation) await FollowWake(id, local, ct);
            job = await ScreenHistory.ResolveJobIdAsync(local, id, job, ct);
            await RecoverMoveJob(id, job, local, ct);
            if (!ct.IsCancellationRequested && ReferenceEquals(local, client)) StartLive(id, local, ct, job);
        }
        catch (OperationCanceledException) { }
        catch (Exception) { if (!ct.IsCancellationRequested && ReferenceEquals(local, client)) StartLive(id, local, ct); }
    }
    private void StartLive(string id, ServerClient local, CancellationToken ct, string? expectedJob = null)
    {
        if (liveStreams.ContainsKey(id)) return;
        var source = CancellationTokenSource.CreateLinkedTokenSource(ct);
        liveStreams[id] = source;
        BeginReplyProgress(id, expectedJob);
        _ = LiveLoop(id, local, source.Token, expectedJob);
    }
    private async Task LiveLoop(string id, ServerClient local, CancellationToken ct, string? expectedJob)
    {
        var display = new LiveDisplayState(); int delay = 1; bool opened = false; string? liveJob = expectedJob;
        // Cache identity even before the first SSE frame, so returning to this
        // conversation can restore preparation without opening another stream.
        if (liveJob is not null) liveDisplays[id] = (liveJob, display);
        try
        {
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    await foreach (var (type, data) in local.StreamAsync(PathFor(id) + "/live", ct))
                    {
                        if (ct.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                        delay = 1;
                        if (type is "snapshot" or "delta")
                        {
                            liveJob = Str(data, "job_id");
                            display.Apply(type, Str(data, "run_id"), Str(data, "text") ?? "", Prop(data, "moods"), Prop(data, "tools"));
                            ReceiveMoves(id, Str(data, "job_id"), Str(data, "run_id"), type == "snapshot", Prop(data, "moves"), display.Text.EnumerateRunes().Count());
                            liveDisplays[id] = (liveJob, display);
                            if (id == currentConversation)
                            {
                                UpdateReplyProgress(id, liveJob, display);
                                if (!opened && display.Text.Length > 0) { OpenReplyBubble(id, liveJob); opened = true; }
                                Message.Text = display.Text;
                                SetMood(display.Text.Length == 0 ? "thinking" : display.Mood);
                                SetTalking(display.Text.Length > 0);
                                StopButton.Visibility = Visibility.Visible;
                                Status.Text = "답변 중…";
                            }
                        }
                        else if (type == "done")
                        {
                            // A reconnect may return done without any display.
                            // Only a reply actually shown through Live is read;
                            // otherwise ReadConversation must recover its final.
                            if (opened && display.Text.Length > 0) CompleteReply(id, liveJob);
                            CompleteLiveMoves(id, liveJob); return;
                        }
                    }
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
                catch (ApiException ex) when (ex.Status == HttpStatusCode.Unauthorized) { return; }
                catch (ApiException ex) when (ex.Code == "too_many_streams")
                {
                    try { await Task.Delay(TimeSpan.FromSeconds(30), ct); } catch (OperationCanceledException) { return; }
                }
                catch (Exception)
                {
                    try { await Task.Delay(TimeSpan.FromSeconds(delay), ct); } catch (OperationCanceledException) { return; }
                    delay = Math.Min(30, delay * 2);
                }
            }
        }
        finally
        {
            if (liveStreams.TryGetValue(id, out var source) && source.Token == ct && !ct.IsCancellationRequested)
            {
                liveStreams.Remove(id); liveDisplays.Remove(id); source.Dispose();
                if (currentConversation == id)
                {
                    bool hadPreparation = ReplyPreparing;
                    EndReplyProgress(closeEmpty: true); SetTalking(false); StopButton.Visibility = Visibility.Collapsed; SetMood("neutral");
                    if (!hadPreparation || display.Text.Length > 0) _ = ScheduleBubbleAfterMoves(id);
                }
                if (connected && currentConversation == id) Status.Text = JobOrigin.Status(wakeElsewhere, currentConversation, "연결됨");
                if (currentConversation == id && connection is not null)
                    try { await ReadConversation(id, connection.Token); } catch (Exception ex) { ShowError(ex); }
            }
        }
    }
}
