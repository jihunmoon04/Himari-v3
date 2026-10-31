using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;

namespace Himari.Companion;

public partial class MainWindow
{
    private sealed record Conversation(string Id, string Title);
    private ServerClient? client;
    private ServerClient? recoveryOwner;
    private CancellationTokenSource? connection;
    private readonly Dictionary<string, CancellationTokenSource> liveStreams = new();
    private readonly Dictionary<string, (string Text, string Mood)> liveDisplays = new();
    private sealed record PendingReply(ServerClient Server, string Job, int After);
    private readonly Dictionary<string, PendingReply> pendingReplies = new();
    private readonly HashSet<string> completedReplies = new();
    private string? deviceId, currentConversation;
    private bool connected, changingSelection, sending;
    private long inputRevision;
    private DateTime? awaySince;
    private readonly ConnectionGate connectionChange = new();
    private readonly ConnectionGate messageSend = new();
    private readonly CancellationTokenSource lifetime = new();
    private readonly ICredentialVault vault = new WindowsCredentialVault();
    private bool pairingInProgress;
    private PairingCoordinator Pairing()
    {
        bool approved = false;
        return new(vault, c => new ServerClient(c.Server, c.Token), approveChange: credential =>
        {
            if (approved || recoveryOwner is null || recoveryOwner.Matches(credential)) return true;
            if (unresolvedScreen is null && unknownText is null && !sending) return true;
            if (!ConfirmDeviceSwitch()) return false;
            approved = true; return true;
        });
    }
    private bool ConfirmDeviceSwitch() => (unresolvedScreen is null && unknownText is null && !sending) ||
        System.Windows.MessageBox.Show(this, "이전 연결에서 메시지가 접수됐는지 확인하지 못했습니다. 연결을 바꾸면 이 앱에서 확인을 계속할 수 없고, 다시 보내면 중복될 수 있습니다. 이전 기록을 확인한 뒤 연결을 바꾸시겠어요?", "미확정 전송이 남아 있습니다", MessageBoxButton.YesNo, MessageBoxImage.Warning) == MessageBoxResult.Yes;

    private static string? Str(JsonElement element, string name) => element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
    private static JsonElement Prop(JsonElement element, string name) => element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var v) ? v : default;
    private static IEnumerable<JsonElement> Items(JsonElement element) => element.ValueKind == JsonValueKind.Array ? element.EnumerateArray() : [];
    private static string PathFor(string id) => "/api/conversations/" + Uri.EscapeDataString(id);

    private void SetConnected(bool value, string text)
    {
        connected = value; Status.Text = text; Input.IsEnabled = true;
        StopButton.IsEnabled = value;
        Character.Opacity = value ? 1 : .55;
        ConnectionNotice.Visibility = value || text == "기기 확인 중…" ? Visibility.Collapsed : Visibility.Visible;
        UpdateBubblePresentation();
    }
    private void StopConnection()
    {
        // Transport shutdown must not erase delivery evidence or replay counters.
        ReturnFromMove();
        CancelLive();
        connection?.Cancel(); connection?.Dispose(); connection = null;
        client?.Dispose(); client = null; connected = false;
    }
    private async Task RestoreConnection()
    {
        try { await connectionChange.EnterAsync(lifetime.Token); } catch (OperationCanceledException) { return; }
        pairingInProgress = true;
        try
        {
            var stored = vault.Active();
            var pending = vault.Pending();
            bool switchApproved = false;
            if (pending is not null)
            {
                try
                {
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token); timeout.CancelAfter(TimeSpan.FromSeconds(10));
                    var recovered = await Pairing().RecoverPendingAsync(timeout.Token);
                    if (recovered is not null) { stored = recovered; switchApproved = true; }
                }
                catch (Exception) { /* Preserve both records; an offline pending attempt can be retried later. */ }
            }
            if (lifetime.IsCancellationRequested) return;
            if (stored is null)
            {
                if (pending is not null) ServerAddress.Text = pending.Server;
                SetConnected(false, pending is null ? "Web에서 짝짓기 코드를 발급해 주세요." : "짝짓기 코드를 입력해 연결을 다시 시도해 주세요."); return;
            }
            ServerAddress.Text = stored.Server;
            StartConnection(stored.Server, stored.Token, switchApproved);
        }
        catch (Exception ex) { ShowError(ex); }
        finally { pairingInProgress = false; connectionChange.Exit(); }
    }
    private async void PairClick(object sender, RoutedEventArgs e)
    {
        string address = ServerAddress.Text.Trim(), code = PairCode.Text.Trim();
        if (string.IsNullOrWhiteSpace(code)) { Status.Text = "짝짓기 코드를 입력해 주세요."; return; }
        if (!connectionChange.TryEnter()) { Status.Text = "다른 연결 작업이 진행 중입니다."; return; }
        PairButton.IsEnabled = false;
        pairingInProgress = true;
        try
        {
            // Verify the URL before touching the existing credential.
            using var validator = new ServerClient(address, "unused");
            string canonical = validator.BaseUri.ToString();
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token); timeout.CancelAfter(TimeSpan.FromSeconds(20));
            Status.Text = "기기 연결 중…";
            var paired = await Pairing().PairAsync(canonical, code, timeout.Token);
            if (lifetime.IsCancellationRequested) return;
            PairCode.Clear();
            StartConnection(paired.Server, paired.Token, switchApproved: true);
        }
        catch (DeviceSwitchDeclinedException) { Status.Text = "기기 전환을 취소했습니다. 기존 연결과 미확정 전송을 유지합니다."; }
        catch (Exception ex) { ShowError(ex); }
        finally { pairingInProgress = false; PairButton.IsEnabled = true; connectionChange.Exit(); }
    }
    private void StartConnection(string address, string token, bool switchApproved = false)
    {
        var local = new ServerClient(address, token);
        if (recoveryOwner is not null && !recoveryOwner.SameDevice(local))
        {
            if (!switchApproved && !ConfirmDeviceSwitch())
            { local.Dispose(); return; }
            // A different identity is discarded only after an explicit decision.
            moveJobs.Clear(); localScreens.Clear(); previousScreenIds.Clear(); earlyMoves.Clear(); moveVerifications.Clear(); moveLifetime.Reset();
            submittedScreen = null; submittedConversation = null; unresolvedScreen = null; unknownText = null;
            pendingReplies.Clear(); completedReplies.Clear();
        }
        recoveryOwner = local;
        if (unknownText is { } pending && pending.Server.SameDevice(local)) unknownText = pending with { Server = local };
        StopConnection();
        client = local;
        var cts = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token); connection = cts;
        PairPanel.Visibility = Visibility.Collapsed;
        SetConnected(false, "기기 확인 중…");
        _ = ConnectLoop(local, cts);
    }
    private async Task ConnectLoop(ServerClient local, CancellationTokenSource cts)
    {
        int delay = 1;
        while (!cts.IsCancellationRequested)
        {
            try
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cts.Token); timeout.CancelAfter(TimeSpan.FromSeconds(20));
                var info = await local.SendAsync(HttpMethod.Get, "/api/device", null, timeout.Token);
                if (cts.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                deviceId = Str(Prop(info, "device"), "device_id") ?? throw new InvalidOperationException("기기 응답이 올바르지 않습니다.");
                await RefreshConversations(Str(Prop(info, "recent_conversation"), "id"), timeout.Token);
                if (cts.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                _ = EventLoop(local, cts.Token); _ = PresenceLoop(local, cts.Token); return;
            }
            catch (OperationCanceledException) when (cts.IsCancellationRequested) { return; }
            catch (ApiException ex) when (ex.Status == HttpStatusCode.Unauthorized)
            {
                if (cts.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                DeleteActiveIfCurrent(local); StopConnection(); PairPanel.Visibility = Visibility.Visible;
                SetConnected(false, "다시 짝짓기가 필요합니다."); return;
            }
            catch (Exception ex)
            {
                if (cts.IsCancellationRequested || !ReferenceEquals(local, client)) return;
                SetConnected(false, ex is ApiException { Code: "companion_off" } ? "서버에서 반려 기능이 꺼져 있습니다." : "기기 연결을 다시 시도합니다…");
                int wait = ex is ApiException { Code: "companion_off" } ? 300 : delay;
                delay = Math.Min(30, delay * 2);
                try { await Task.Delay(TimeSpan.FromSeconds(wait), cts.Token); } catch (OperationCanceledException) { return; }
            }
        }
    }
    private async void Reconnect()
    {
        await RestoreConnection();
    }
    private void ShowError(Exception ex)
    {
        if (ex is OperationCanceledException) { Status.Text = "연결 시간이 초과되었거나 취소되었습니다."; return; }
        Status.Text = ex is ApiException api ? api.Code switch {
            "busy" => "이 대화는 작업 중입니다. 새 대화를 열 수 있습니다.",
            "companion_off" => "서버에서 반려 기능이 꺼져 있습니다.",
            "device_scope" => "앱 권한 오류가 발생했습니다.",
            "pairing_used" => "이미 사용된 코드입니다. Web에서 새 코드를 발급해 주세요.",
            "pairing_busy" => "서버가 혼잡합니다. 잠시 후 다시 시도해 주세요.",
            "token_in_use" => "기기 토큰이 충돌했습니다. 다시 시도해 주세요.",
            _ => "요청 실패: " + api.Code
        } : ex is ArgumentException ? ex.Message : "연결하지 못했습니다. 서버 주소와 VPN을 확인해 주세요.";
        if (ex is ApiException { Code: "device_scope" }) Trace.TraceWarning("HIMARI device_scope: client requested an unauthorized route.");
    }
    private async Task RefreshConversations(string? preferred, CancellationToken ct)
    {
        if (client is null) return;
        var list = new List<Conversation>(); int offset = 0;
        while (true)
        {
            var data = await client.SendAsync(HttpMethod.Get, $"/api/conversations?limit=50&offset={offset}", null, ct);
            list.AddRange(Items(Prop(data, "items")).Select(x => new Conversation(Str(x, "id") ?? "", Str(x, "title") ?? "새 대화")).Where(x => x.Id.Length > 0));
            var nextValue = Prop(data, "next_offset");
            if (nextValue.ValueKind != JsonValueKind.Number || !nextValue.TryGetInt32(out var next) || next <= offset) break;
            offset = next;
        }
        string? chosen = preferred ?? currentConversation;
        changingSelection = true; Conversations.ItemsSource = list;
        Conversations.SelectedItem = list.FirstOrDefault(x => x.Id == chosen) ?? list.FirstOrDefault();
        changingSelection = false;
        SetCurrentConversation((Conversations.SelectedItem as Conversation)?.Id);
        if (currentConversation is not null) await ReadConversation(currentConversation, ct);
    }
    private async void ConversationChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (changingSelection) return;
        SetCurrentConversation((Conversations.SelectedItem as Conversation)?.Id);
        if (currentConversation is not null && connection is not null)
            try { await ReadConversation(currentConversation, connection.Token); } catch (Exception ex) { ShowError(ex); }
    }
    private Task ReadConversation(string id, CancellationToken ct) => ReadConversationState(id, ct, completedReplyOnly: false);
    private async Task ReadConversationState(string id, CancellationToken ct, bool completedReplyOnly)
    {
        var local = client;
        if (local is null) return;
        var detail = await local.SendAsync(HttpMethod.Get, PathFor(id), null, ct);
        var question = ConversationView.Question(detail);
        if (ct.IsCancellationRequested || !ReferenceEquals(local, client) || id != currentConversation) return;
        // Acceptance can arrive after job ended and a bodyless Live done. Probe
        // again once the waiter exists, leaving a still-running display alone.
        if (completedReplyOnly && (ConversationView.IsRunning(detail) ||
            !pendingReplies.TryGetValue(id, out var awaited) || !awaited.Server.SameDevice(local) ||
            Str(Prop(detail, "job"), "job_id") != awaited.Job)) return;
        StopButton.Visibility = ConversationView.IsRunning(detail) ? Visibility.Visible : Visibility.Collapsed;
        if (connected) Status.Text = ConversationView.IsRunning(detail) ? "이 대화는 작업 중입니다. 새 대화를 열 수 있습니다." : "연결됨";
        if (question is not null) { awaitingAnswer = true; Message.Text = question; OpenReplyBubble(id, Str(Prop(detail, "job"), "job_id")); OpenComposer(); if (IsChatWindow(GetForegroundWindow())) { dialogueHost?.Activate(); Input.Focus(); } return; }
        awaitingAnswer = false;
        if (liveDisplays.TryGetValue(id, out var display)) { Message.Text = display.Text; SetMood(display.Mood); return; }
        var sequence = Prop(Prop(detail, "conversation"), "last_sequence");
        int lastSequence = sequence.ValueKind == JsonValueKind.Number && sequence.TryGetInt32(out int n) ? n : 0;
        int after = Math.Max(0, lastSequence - 100), finalSequence = 0; string? lastText = null; string lastMood = "neutral";
        while (true)
        {
            var history = await local.SendAsync(HttpMethod.Get, PathFor(id) + $"/events?after={after}&limit=100", null, ct);
            var page = Items(Prop(history, "items")).ToList();
            foreach (var item in page)
                if (Str(item, "kind") == "assistant_final" && Prop(item, "sequence").ValueKind == JsonValueKind.Number && Prop(item, "sequence").TryGetInt32(out int itemSequence) && itemSequence <= lastSequence)
                {
                    var finalDisplay = Prop(item, "display"); var value = Str(finalDisplay, "text");
                    if (value is not null) { lastText = value; finalSequence = itemSequence; lastMood = DisplayState.Mood(Prop(finalDisplay, "moods"), value.EnumerateRunes().Count()); }
                }
            if (lastText is not null || after == 0) break;
            after = Math.Max(0, after - 100);
        }
        if (ct.IsCancellationRequested || !ReferenceEquals(local, client) || id != currentConversation || liveStreams.ContainsKey(id)) return;
        bool recoveredReply = false;
        if (!string.IsNullOrWhiteSpace(lastText) && pendingReplies.TryGetValue(id, out var pending) &&
            pending.Server.SameDevice(local) && finalSequence > pending.After &&
            !ConversationView.IsRunning(detail) && Str(Prop(detail, "job"), "job_id") == pending.Job)
        {
            // A new job may have started while history was being read. Do not
            // label another answer as the one this app was waiting for.
            var latest = await local.SendAsync(HttpMethod.Get, PathFor(id), null, ct);
            if (ct.IsCancellationRequested || !ReferenceEquals(local, client) || id != currentConversation || liveStreams.ContainsKey(id)) return;
            if (!ConversationView.IsRunning(latest) && Str(Prop(latest, "job"), "job_id") == pending.Job &&
                pendingReplies.TryGetValue(id, out var current) && ReferenceEquals(current, pending))
            { CompleteReply(id, pending.Job); recoveredReply = true; }
        }
        Message.Text = lastText ?? "대화를 시작해 주세요.";
        SetMood(lastText is null ? "neutral" : lastMood);
        if (recoveredReply) { SetTalking(false); OpenReplyBubble(id, Str(Prop(detail, "job"), "job_id")); _ = ScheduleBubbleAfterMoves(id); }
    }
    private void WaitForReply(ServerClient server, string conversation, string? job, int after)
    {
        if (job is null || completedReplies.Contains(job)) return;
        if (pendingReplies.TryGetValue(conversation, out var pending) && pending.Job == job && pending.Server.SameDevice(server)) return;
        pendingReplies[conversation] = new(server, job, after);
    }
    private void CompleteReply(string conversation, string? job)
    {
        if (job is null) return;
        completedReplies.Add(job);
        if (pendingReplies.TryGetValue(conversation, out var pending) && pending.Job == job) pendingReplies.Remove(conversation);
    }
    private async void NewConversationClick(object sender, RoutedEventArgs e)
    {
        if (!connected || client is null || connection is null) return;
        try { var created = await client.SendAsync(HttpMethod.Post, "/api/conversations", new { }, connection.Token); await RefreshConversations(Str(created, "id"), connection.Token); }
        catch (Exception ex) { ShowError(ex); }
    }
    private void InputChanged(object sender, System.Windows.Controls.TextChangedEventArgs e)
    {
        inputRevision++;
        UpdateBubblePresentation();
        if (!string.IsNullOrEmpty(Input.Text) && ComposerPanel.Visibility == Visibility.Visible) PresentUserBubble();
    }
    private void SetCurrentConversation(string? id)
    {
        if (currentConversation != id)
        {
            inputRevision++; CloseBubble(); awaitingAnswer = false;
            UserSent.Text = ""; UserSent.Visibility = Visibility.Collapsed;
        }
        currentConversation = id;
    }
    private void SendClick(object sender, RoutedEventArgs e) => SendMessage();
    private async void SendMessage()
    {
        if (pairingInProgress) { Status.Text = "기기 연결 작업이 끝난 뒤 메시지를 보내 주세요."; return; }
        if (!connected || client is null || connection is null || currentConversation is null) return;
        string text = Input.Text.Trim(); int length = text.EnumerateRunes().Count();
        bool wantsScreen = settings.ScreenSharing.IsChecked == true;
        if (length > 32000 || (length == 0 && !wantsScreen && unresolvedScreen is null)) { Status.Text = "메시지를 입력하거나 화면 첨부를 선택해 주세요 (최대 32,000자)."; return; }
        if (unresolvedScreen is { } unresolved && (unresolved.Conversation != currentConversation || unresolved.Text != text)) { Status.Text = "이전 화면 메시지의 전송 여부를 먼저 확인해 주세요. 원래 대화와 입력으로 돌아가 다시 보내기를 누르세요."; return; }
        if (!messageSend.TryEnter()) return;
        var local = client; var ct = connection.Token;
        var context = new SendContext(local, currentConversation, inputRevision);
        sending = true;
        PresentUserBubble();
        SendButton.IsEnabled = false;
        LocalScreen? screen = null;
        bool screenFailed = false;
        int beforeSequence = 0;
        try
        {
            if (unknownText is not null && !await CheckUnknownText(true, ct)) return;
            if (unresolvedScreen is { } retry)
            {
                if (DateTimeOffset.UtcNow - retry.Screen.CapturedAt > TimeSpan.FromMinutes(10)) { unresolvedScreen = null; Status.Text = "이전 화면이 만료되었습니다. 전송 여부를 기록에서 확인한 뒤 다시 보내세요. 중복될 수 있습니다."; return; }
                screen = retry.Screen;
            }
            else if (wantsScreen)
            {
                try
                {
                    Status.Text = "화면을 캡처하고 있습니다…";
                    var captured = await CaptureScreenAsync(ct);
                    Status.Text = "화면을 캡처해 업로드 중입니다…";
                    var upload = await local.UploadScreenAsync(context.ConversationId, captured.Bytes, captured.ContentType, ct);
                    if (recoveryOwner?.SameDevice(local) != true) return;
                    string fileId = Str(upload, "file_id") ?? throw new InvalidOperationException("화면 업로드 응답이 올바르지 않습니다.");
                    screen = new(fileId, captured.Geometry, captured.CapturedAt, ScreenSafety.Ready(upload));
                    localScreens[fileId] = screen;
                }
                catch (OperationCanceledException) { throw; }
                catch (Exception) { screenFailed = true; if (length == 0) { Status.Text = "화면을 보내지 못했습니다. 다시 시도해 주세요."; return; } }
            }
            if (screen is not null || localScreens.Count > 0) pendingScreenConversation = context.ConversationId;
            submittedScreen = screen; submittedConversation = context.ConversationId;
            // Both screen and text requests need a history boundary so an old
            // answer is never mistaken for a newly completed reply after reconnect.
            var before = await local.SendAsync(HttpMethod.Get, PathFor(context.ConversationId), null, ct);
            var seq = Prop(Prop(before, "conversation"), "last_sequence");
            if (seq.ValueKind == JsonValueKind.Number) seq.TryGetInt32(out beforeSequence);
            JsonElement response;
            try
            {
                response = await local.SendAsync(HttpMethod.Post, PathFor(context.ConversationId) + "/messages", screen is null ? new { text } : new { text, screen = screen.FileId }, ct);
                if (recoveryOwner?.SameDevice(local) != true) return;
                unresolvedScreen = null;
                if (screen is { Ready: true }) previousScreenIds[context.ConversationId] = screen.FileId;
            }
            catch (ApiException ex) when (screen is not null && ex.Code == "screen_already_sent")
            {
                if (recoveryOwner?.SameDevice(local) != true) return;
                unresolvedScreen = null;
                if (screen.Ready) previousScreenIds[context.ConversationId] = screen.FileId;
                var detail = await local.SendAsync(HttpMethod.Get, PathFor(context.ConversationId), null, ct);
                await RecoverMoveJob(context.ConversationId, Str(Prop(detail, "job"), "job_id"), local, ct);
                if (context.IsCurrent(client, currentConversation, connected)) { if (context.CanClear(client, currentConversation, connected, inputRevision)) Input.Clear(); ShowSentMessage(text); await ReadConversation(context.ConversationId, ct); Status.Text = "이전에 보낸 화면 메시지를 확인했습니다."; }
                return;
            }
            catch (ApiException ex) when (screen is not null && MessageFailures.Classify(ex) == MessageFailure.Closed)
            {
                if (recoveryOwner?.SameDevice(local) != true) return;
                unresolvedScreen = null;
                try { await ReadConversation(context.ConversationId, ct); } catch (Exception) { }
                Status.Text = "이 대화는 닫혔습니다. 메시지가 기록됐을 수 있으니 기록을 확인해 주세요.";
                return;
            }
            catch (Exception ex) when (screen is not null && MessageFailures.Classify(ex) == MessageFailure.Unknown)
            {
                if (recoveryOwner?.SameDevice(local) != true) return;
                unresolvedScreen = (context.ConversationId, text, screen);
                Status.Text = "화면 메시지가 전송됐는지 확인하지 못했습니다. 같은 내용으로 다시 보내면 확인합니다.";
                return;
            }
            catch (ApiException ex) when (screen is not null && MessageFailures.ReuseScreen(ex))
            {
                if (recoveryOwner?.SameDevice(local) != true) return;
                unresolvedScreen = (context.ConversationId, text, screen);
                Status.Text = "전송이 거절됐습니다. 원인을 해결한 뒤 10분 안에는 같은 화면으로 다시 보낼 수 있습니다.";
                return;
            }
            catch (ApiException ex) when (screen is not null && ex.Code is "not_a_screen" or "attachment_refused" or "not_found")
            {
                if (recoveryOwner?.SameDevice(local) != true) return;
                unresolvedScreen = null; localScreens.Remove(screen.FileId);
                submittedScreen = null; submittedConversation = null;
                if (context.IsCurrent(client, currentConversation, connected)) Status.Text = "화면 첨부가 거절됐습니다. 다음 전송에서는 화면을 새로 캡처해 주세요.";
                return;
            }
            catch (Exception ex) when (screen is null && MessageFailures.Classify(ex) is MessageFailure.Unknown or MessageFailure.Closed)
            {
                if (recoveryOwner?.SameDevice(local) != true) return;
                unknownText = new(local, context.ConversationId, text, deviceId ?? "", beforeSequence);
                await CheckUnknownText(false, ct);
                if (MessageFailures.Classify(ex) == MessageFailure.Closed) Status.Text = "이 대화는 닫혔습니다. 메시지가 기록됐을 수 있으니 기록을 확인해 주세요.";
                return;
            }
            string? jobId = Str(response, "job_id");
            WaitForReply(local, context.ConversationId, jobId, beforeSequence);
            if (jobId is not null && localScreens.Count > 0) await RecoverMoveJob(context.ConversationId, jobId, local, ct, screen);
            if (context.CanClear(client, currentConversation, connected, inputRevision)) Input.Clear();
            if (context.IsCurrent(client, currentConversation, connected)) ShowSentMessage(text);
            if (context.IsCurrent(client, currentConversation, connected) && !liveDisplays.ContainsKey(context.ConversationId) && (jobId is null || moveLifetime.CanStart(jobId)))
            { Status.Text = screenFailed ? "화면을 보내지 못해 글만 보냈습니다." : "답변을 기다리는 중…"; SetMood("thinking"); }
            if (context.IsCurrent(client, currentConversation, connected) && jobId is not null &&
                pendingReplies.TryGetValue(context.ConversationId, out var pendingReply) && pendingReply.Job == jobId)
            {
                await ReadConversationState(context.ConversationId, ct, completedReplyOnly: true);
                if (screenFailed && context.IsCurrent(client, currentConversation, connected)) Status.Text = "화면을 보내지 못해 글만 보냈습니다.";
            }
        }
        catch (Exception ex)
        {
            if (context.IsCurrent(client, currentConversation, connected)) ShowError(ex);
            else if (ex is ApiException { Code: "device_scope" }) Trace.TraceWarning("HIMARI device_scope: message request denied.");
        }
        finally
        {
            pendingScreenConversation = null;
            // Another stream may still be validating this request's screen.
            if (earlyMoves.TryGetValue(context.ConversationId, out var buffered)) buffered.RemoveAll(x => !moveLifetime.IsVerifying(x.Job));
            sending = false; messageSend.Exit(); UpdateBubblePresentation();
        }
    }
    private async void CancelClick(object sender, RoutedEventArgs e) => await RequestCancel();
    private async Task RequestCancel()
    {
        if (!connected || client is null || connection is null || currentConversation is null) return;
        var local = client; var conversation = currentConversation; var reply = assistantBubbleKey;
        try
        {
            await local.SendAsync(HttpMethod.Post, PathFor(conversation) + "/cancel", new { }, connection.Token);
            if (ReferenceEquals(local, client) && conversation == currentConversation && reply == assistantBubbleKey)
            { replyStopRequested = true; UpdateBubblePresentation(); }
        }
        catch (Exception ex) { ShowError(ex); }
    }
    private void OpenWebClick(object sender, RoutedEventArgs e) => OpenWeb();
    private void HistoryClick(object sender, RoutedEventArgs e)
    {
        if (!connected || client is null || connection is null || currentConversation is null) return;
        var title = (Conversations.SelectedItem as Conversation)?.Title ?? "대화";
        new HistoryWindow(this, client, currentConversation, title, connection.Token).Show();
    }
    private void DeleteActiveIfCurrent(ServerClient source)
    {
        var stored = vault.Active();
        if (stored is not null && source.Matches(stored)) CredentialStore.DeleteActive();
    }
    private void OpenWeb() { if (client is not null) Process.Start(new ProcessStartInfo(client.BaseUri.ToString()) { UseShellExecute = true }); }

    [StructLayout(LayoutKind.Sequential)] private struct LastInput { internal uint Size, Time; }
    [DllImport("user32.dll", SetLastError = true)] private static extern bool GetLastInputInfo(ref LastInput info);
    private static int IdleSeconds()
    {
        var info = new LastInput { Size = (uint)Marshal.SizeOf<LastInput>() };
        if (!GetLastInputInfo(ref info)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        return (int)Math.Min(unchecked((uint)Environment.TickCount - info.Time) / 1000, 2592000u);
    }
    private async Task PresenceLoop(ServerClient local, CancellationToken ct)
    {
        DateTime last = DateTime.MinValue; bool wasIdle = false;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                int idle = IdleSeconds();
                if ((DateTime.UtcNow - last).TotalSeconds >= 60 || (wasIdle && idle < 300))
                {
                    string zone = TimeZoneInfo.TryConvertWindowsIdToIanaId(TimeZoneInfo.Local.Id, out var iana) ? iana : TimeZoneInfo.Local.Id;
                    await local.SendAsync(HttpMethod.Post, "/api/device/presence", new { idle_seconds = idle, time_zone = zone }, ct); last = DateTime.UtcNow;
                }
                wasIdle = idle >= 300;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (Exception) { /* Event stream owns connection status and retries. */ }
            try { await Task.Delay(TimeSpan.FromSeconds(2), ct); } catch (OperationCanceledException) { return; }
        }
    }
}
