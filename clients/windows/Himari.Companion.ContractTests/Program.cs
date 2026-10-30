using System.Net;
using System.Text;
using System.Text.Json;
using Himari.Companion;

static void Check(bool value, string message) { if (!value) throw new Exception(message); }
var reconnectServer = new ReconnectRecoveryHandler();
var previousConnection = new ServerClient("https://example.com/", "same-device", reconnectServer);
try { await previousConnection.SendAsync(HttpMethod.Post, "/api/conversations/c/messages", new { text = "hello", screen = "f1" }, CancellationToken.None); }
catch (HttpRequestException) { }
previousConnection.Dispose();
using (var reconnected = new ServerClient("https://example.com/", "same-device", new ReconnectRecoveryHandler()))
{
    Check(previousConnection.SameDevice(reconnected), "same identity survives disposed transport");
    string? recoveredJob = await ScreenHistory.RunningJobIdAsync(reconnected, "c", CancellationToken.None);
    Check(recoveredJob == "j1", "hello activity without job_id falls back to conversation detail");
    Check(await ScreenHistory.LatestReadyIdAsync(reconnected, "c", recoveredJob!, CancellationToken.None) == "f1", "screen metadata remains usable after manual reconnect");
    try { await reconnected.SendAsync(HttpMethod.Post, "/api/conversations/c/messages", new { text = "hello", screen = "f1" }, CancellationToken.None); throw new Exception("duplicate screen accepted"); }
    catch (ApiException ex) { Check(ex.Code == "screen_already_sent", "retry after reconnect uses same file id"); }
}
var lostTextConnection = new ServerClient("https://example.com/", "same-device", new ReconnectRecoveryHandler());
try { await lostTextConnection.SendAsync(HttpMethod.Post, "/api/conversations/c/messages", new { text = "hello" }, CancellationToken.None); }
catch (HttpRequestException) { }
lostTextConnection.Dispose();
using (var reconnectedText = new ServerClient("https://example.com/", "same-device", new ReconnectRecoveryHandler()))
{
    Check(lostTextConnection.SameDevice(reconnectedText), "unknown text can bind new transport of same device");
    Check(!await TextRecovery.AcceptedAsync(reconnectedText, "c", "hello", "d1", 0, CancellationToken.None), "origin-less history after reconnect still requires duplicate warning");
}
using (var otherDevice = new ServerClient("https://example.com/", "other-device", new ReconnectRecoveryHandler()))
    Check(!lostTextConnection.SameDevice(otherDevice), "device switch cannot silently claim pending text");
using (var otherServer = new ServerClient("https://other.example/", "same-device", new ReconnectRecoveryHandler()))
    Check(!lostTextConnection.SameDevice(otherServer), "server switch cannot claim pending screen");
foreach (string state in new[] { "ready", "no_sandbox", "view_expired" })
{
    using var recoveryClient = new ServerClient("https://example.com/", "test", new LostResponseHandler(state));
    try { await recoveryClient.SendAsync(HttpMethod.Post, "/api/conversations/c/messages", new { text = "hello", screen = "f1" }, CancellationToken.None); }
    catch (HttpRequestException) { }
    // The job started event provides j1 after the HTTP response was lost.
    string? recoveredFile = await ScreenHistory.LatestReadyIdAsync(recoveryClient, "c", "j1", CancellationToken.None);
    Check(recoveredFile == (state == "ready" ? "f1" : null), "lost response recovery checks recorded image readiness: " + state);
    Check(await ScreenHistory.LatestReadyIdAsync(recoveryClient, "c", "other-job", CancellationToken.None) is null, "job event for another job cannot recover screen");
}
foreach (string state in new[] { "ready", "no_sandbox", "view_expired" })
{
    var file = JsonSerializer.SerializeToElement(new { image = new { state } });
    Check(ScreenSafety.Ready(file) == (state == "ready"), "only ready screen can create move context: " + state);
}
Check(!ScreenSafety.Ready(default), "missing image state is not ready");
var lifetime = new MoveJobLifetime();
lifetime.End("old");
Check(!lifetime.CanStart("old") && lifetime.CanStart("new"), "ended job cannot be restored by late HTTP response or SSE");
var withoutOrigin = JsonSerializer.SerializeToElement(new { kind = "user_message", sequence = 42, text = "hello" });
Check(!TextRecovery.Matches(withoutOrigin, "hello", "d1", 41), "same text without origin does not confirm acceptance");
var ownText = JsonSerializer.SerializeToElement(new { kind = "user_message", sequence = 42, text = "hello", origin = new { kind = "device", device_id = "d1" } });
Check(TextRecovery.Matches(ownText, "hello", "d1", 41) && !TextRecovery.Matches(ownText, "hello", "d2", 41) && !TextRecovery.Matches(ownText, "hello", "d1", 42), "text recovery checks origin and sequence boundary");
Check(MessageFailures.ReuseScreen(new ApiException(HttpStatusCode.TooManyRequests, "rate_limited")), "definite rejection preserves staged screen");
Check(!MessageFailures.ReuseScreen(new ApiException(HttpStatusCode.BadRequest, "attachment_refused")), "refused file is not reused");
Check(MessageFailures.Classify(new ApiException(HttpStatusCode.Conflict, "busy")) == MessageFailure.Rejected, "busy is rejected before recording");
Check(MessageFailures.Classify(new ApiException(HttpStatusCode.Conflict, "conversation_closed")) == MessageFailure.Closed, "closed may have been recorded");
Check(MessageFailures.Classify(new ApiException(HttpStatusCode.Conflict, "screen_already_sent")) == MessageFailure.AlreadySent, "screen retry can confirm acceptance");
Check(MessageFailures.Classify(new ApiException(HttpStatusCode.Conflict, "still_stopping")) == MessageFailure.Unknown, "unknown conflict is not assumed rejected");
var imageSize = ScreenGeometry.ImageSize(3840, 2160);
Check(imageSize == (1280, 720), "screen is scaled down to model size");
var portrait = ScreenGeometry.ImageSize(1440, 2560);
Check(portrait.Width * portrait.Height <= 1_150_000 && portrait.Height <= 1280, "portrait respects both limits");
Check(ScreenGeometry.ImageSize(640, 480) == (640, 480), "small screen is not enlarged");
var geometry = new ScreenGeometry(new PixelRect(-1920, 0, 1920, 1080), 1280, 720);
Check(geometry.Map(640, 360) == new PixelPoint(-960, 540), "negative monitor coordinates are preserved");
Check(geometry.Map(-50, 10000).X == -1920 && geometry.Map(-50, 10000).Y < 1080, "out of range coordinates are clamped");
var captured = DateTimeOffset.UtcNow;
var playback = new MovePlayback("job-1", geometry, captured);
using (var moves = JsonDocument.Parse("[{\"at\":1,\"x\":10,\"y\":20},{\"at\":2,\"x\":30,\"y\":40}]"))
{
    Check(playback.Accept("other", "run-1", false, moves.RootElement, 2, captured).Count == 0, "unrelated job ignored");
    Check(playback.Accept(null, "run-1", false, moves.RootElement, 2, captured).Count == 0, "old server without job id ignored");
    Check(playback.Accept("job-1", "run-1", false, moves.RootElement, 2, captured).Count == 2, "live moves are returned in order");
    Check(playback.Accept("job-1", "run-1", true, moves.RootElement, 2, captured).Count == 0, "reconnect snapshot does not replay moves");
    Check(playback.Accept("job-1", "run-2", true, moves.RootElement, 2, captured).Single().X == 30, "snapshot catches up with only last move");
    Check(playback.Accept("job-1", "run-3", false, moves.RootElement, 2, captured.AddMinutes(11)).Count == 0, "stale screen ignored");
}
using (var malformed = JsonDocument.Parse("[{\"at\":\"bad\",\"x\":1,\"y\":2},null]"))
    Check(playback.Accept("job-1", "run-4", false, malformed.RootElement, 2, captured).Count == 0, "malformed moves ignored");
var token = CredentialStore.NewToken();
Check(token.StartsWith("hd_") && token.Length == 46, "token format");
Check(token != CredentialStore.NewToken(), "tokens must not repeat");
try { using var invalid = new ServerClient("http://example.com", token); throw new Exception("HTTP accepted"); } catch (ArgumentException) { }
try { using var invalid = new ServerClient("https://example.com/path", token); throw new Exception("path accepted"); } catch (ArgumentException) { }
using var client = new ServerClient("https://example.com/", token, new FakeHandler(token));
var paired = await client.PairAsync("ABCD-EFGH", CancellationToken.None);
Check(paired.GetProperty("device_id").GetString() == "d1", "pair response");
await client.SendAsync(HttpMethod.Post, "/api/device/presence", new { idle_seconds = 12, time_zone = "Asia/Seoul" }, CancellationToken.None);
var uploaded = await client.UploadScreenAsync("c1", [1, 2, 3], "image/png", CancellationToken.None);
Check(uploaded.GetProperty("file_id").GetString() == "f1", "screen upload response");
var events = new List<(string Event, string Text)>();
await foreach (var item in client.StreamAsync("/api/events", CancellationToken.None)) events.Add((item.Event, item.Data.GetProperty("text").GetString()!));
Check(events.Count == 2 && events[0] == ("hello", "안녕😀") && events[1] == ("current", "끝"), "SSE parsing");
try { await client.SendAsync(HttpMethod.Get, "/unauthorized", null, CancellationToken.None); throw new Exception("error accepted"); }
catch (ApiException ex) { Check(ex.Status == HttpStatusCode.Forbidden && ex.Code == "device_scope", "error envelope"); }
using (var detail = JsonDocument.Parse("{\"waiting\":{\"question\":{\"display\":{\"text\":\"어느 쪽?\"}}},\"job\":{\"running\":true}}"))
{
    Check(ConversationView.Question(detail.RootElement) == "어느 쪽?", "nested question");
    Check(ConversationView.IsRunning(detail.RootElement), "busy status");
}
using (var detail = JsonDocument.Parse("{\"waiting\":{\"question\":\"raw\",\"display\":{\"text\":\"clean\"}},\"job\":{\"running\":false}}"))
    Check(ConversationView.Question(detail.RootElement) == "clean" && !ConversationView.IsRunning(detail.RootElement), "display wins and question completion");

var vault = new FakeVault { ActiveValue = new DeviceCredential("https://old.example/", "old-token", null) };
var pairingServer = new PairHandler();
var coordinator = new PairingCoordinator(vault, c => new ServerClient(c.Server, c.Token, new SharedHandler(pairingServer)));
pairingServer.FailPair = true;
try { await coordinator.PairAsync("https://example.com/", "ABCD-EFGH", CancellationToken.None); throw new Exception("expected pair failure"); } catch (ApiException) { }
Check(vault.ActiveValue!.Token == "old-token" && vault.PendingValue is not null, "failed pairing preserves active token");
string firstToken = vault.PendingValue!.Token;
pairingServer.FailPair = false; pairingServer.LoseResponse = true;
try { await coordinator.PairAsync("https://example.com/", "ABCD-EFGH", CancellationToken.None); throw new Exception("expected lost response"); } catch (HttpRequestException) { }
Check(vault.ActiveValue!.Token == "old-token" && vault.PendingValue!.Token == firstToken, "lost response preserves both credentials");
var recovered = await coordinator.RecoverPendingAsync(CancellationToken.None);
Check(recovered?.Token == firstToken && vault.ActiveValue!.Token == firstToken && vault.PendingValue is null, "registered pending token recovered");
pairingServer.RegisteredToken = null; pairingServer.FailPair = true;
try { await coordinator.PairAsync("https://example.com/", "AAAA-BBBB", CancellationToken.None); } catch (ApiException) { }
string oldPending = vault.PendingValue!.Token;
try { await coordinator.PairAsync("https://example.com/", "CCCC-DDDD", CancellationToken.None); } catch (ApiException) { }
Check(vault.PendingValue!.Token != oldPending && vault.ActiveValue!.Token == firstToken, "new code generates new pending token without losing active");
var retryVault = new FakeVault { ActiveValue = new("https://old.example/", "old-token", null) };
var retryServer = new PairHandler { TokenConflicts = 1, BusyResponses = 2 };
int delays = 0;
var retryCoordinator = new PairingCoordinator(retryVault, c => new ServerClient(c.Server, c.Token, new SharedHandler(retryServer)), (duration, ct) => { ct.ThrowIfCancellationRequested(); Check(duration == TimeSpan.FromSeconds(2), "busy retry delay"); delays++; return Task.CompletedTask; });
var newDevice = await retryCoordinator.PairAsync("https://example.com/", "ABCD-EFGH", CancellationToken.None);
Check(retryServer.AttemptedTokens.Count == 4 && retryServer.AttemptedTokens[0] != retryServer.AttemptedTokens[1] && retryServer.AttemptedTokens[1] == retryServer.AttemptedTokens[2] && retryServer.AttemptedTokens[2] == retryServer.AttemptedTokens[3], "token collision regenerates once; busy retries same token");
Check(retryServer.AttemptedCodes.All(c => c == "ABCDEFGH") && delays == 2 && retryVault.ActiveValue == newDevice && retryVault.PendingValue is null, "retry preserves code and promotes only after verify");
var fullVault = new FakeVault { ActiveValue = new("https://old.example/", "old-token", null) };
var fullServer = new PairHandler { BusyResponses = 10 };
var fullCoordinator = new PairingCoordinator(fullVault, c => new ServerClient(c.Server, c.Token, new SharedHandler(fullServer)), (_, ct) => Task.CompletedTask);
try { await fullCoordinator.PairAsync("https://example.com/", "ABCD-EFGH", CancellationToken.None); throw new Exception("busy should fail"); }
catch (ApiException ex) { Check(ex.Code == "pairing_busy", "busy error surfaced"); }
Check(fullServer.AttemptedTokens.Count == 3 && fullVault.ActiveValue!.Token == "old-token" && fullVault.PendingValue is not null, "busy retries bounded and credentials preserved");
var conflictVault = new FakeVault { ActiveValue = new("https://old.example/", "old-token", null) };
var conflictServer = new PairHandler { TokenConflicts = 3 };
var conflictCoordinator = new PairingCoordinator(conflictVault, c => new ServerClient(c.Server, c.Token, new SharedHandler(conflictServer)), (_, ct) => Task.CompletedTask);
try { await conflictCoordinator.PairAsync("https://example.com/", "ABCD-EFGH", CancellationToken.None); throw new Exception("conflict should fail"); }
catch (ApiException ex) { Check(ex.Code == "token_in_use", "repeated collision surfaced"); }
Check(conflictServer.AttemptedTokens.Count == 2 && conflictVault.ActiveValue!.Token == "old-token" && conflictVault.PendingValue!.Token == conflictServer.AttemptedTokens[1], "collision retry bounded and latest pending preserved");
var cancelVault = new FakeVault { ActiveValue = new("https://old.example/", "old-token", null) };
var cancelServer = new PairHandler { BusyResponses = 2 };
using (var cancelRetry = new CancellationTokenSource())
{
    var cancelCoordinator = new PairingCoordinator(cancelVault, c => new ServerClient(c.Server, c.Token, new SharedHandler(cancelServer)), (_, ct) => { cancelRetry.Cancel(); return Task.Delay(1, ct); });
    try { await cancelCoordinator.PairAsync("https://example.com/", "ABCD-EFGH", cancelRetry.Token); throw new Exception("retry cancellation ignored"); } catch (OperationCanceledException) { }
    Check(cancelServer.AttemptedTokens.Count == 1 && cancelVault.ActiveValue!.Token == "old-token", "cancel stops retry and preserves active credential");
}
// Reconnect after the server finished a job while the stream was down.
string displayed = "중간 결과"; bool resumed = false; string serverFinal = "최종 답변";
await HelloReconciliation.ApplyAsync(() => Task.CompletedTask, () => resumed = false, () => { displayed = serverFinal; return Task.CompletedTask; }, CancellationToken.None);
Check(!resumed && displayed == "최종 답변", "hello restores final event after disconnected completion");
string order = "";
await HelloReconciliation.ApplyAsync(() => { order += "select "; return Task.CompletedTask; }, () => order += "resume ", () => { order += "read"; return Task.CompletedTask; }, CancellationToken.None);
Check(order == "select resume read", "hello resumes live before reading conversation");
using (var cancelled = new CancellationTokenSource())
{
    cancelled.Cancel();
    try { await HelloReconciliation.ApplyAsync(() => throw new Exception("unexpected stale callback"), () => { }, () => Task.CompletedTask, cancelled.Token); throw new Exception("cancel ignored"); }
    catch (OperationCanceledException) { }
}
var scenario = new ScenarioHandler();
using var scenarioClient = new ServerClient("https://example.com/", token, scenario);
var seen = new List<string>();
await foreach (var item in scenarioClient.StreamAsync("/api/events", CancellationToken.None)) seen.Add(item.Event);
Check(seen.SequenceEqual(new[] { "hello", "job" }), "initial SSE job");
scenario.FinishWhileOffline(); seen.Clear();
await foreach (var item in scenarioClient.StreamAsync("/api/events", CancellationToken.None))
{
    if (item.Event != "hello") continue;
    await HelloReconciliation.ApplyAsync(() => Task.CompletedTask, () => { }, async () =>
    {
        var history = await scenarioClient.SendAsync(HttpMethod.Get, "/api/conversations/c1/events", null, CancellationToken.None);
        displayed = history.GetProperty("items")[0].GetProperty("display").GetProperty("text").GetString()!;
    }, CancellationToken.None);
    seen.Add(item.Event);
}
Check(seen.SequenceEqual(new[] { "hello" }) && displayed == "연결 중단 중 완료", "SSE reconnect reads authoritative final display");
var waiting = await scenarioClient.SendAsync(HttpMethod.Get, "/api/conversations/c1", null, CancellationToken.None);
Check(ConversationView.Question(waiting) == "어떻게 할까요?", "ask_user question received");
await scenarioClient.SendAsync(HttpMethod.Post, "/api/conversations/c1/messages", new { text = "계속해" }, CancellationToken.None);
waiting = await scenarioClient.SendAsync(HttpMethod.Get, "/api/conversations/c1", null, CancellationToken.None);
Check(scenario.Answer == "계속해" && ConversationView.Question(waiting) is null, "question answer and completion");
using (var marks = JsonDocument.Parse("[{\"at\":0,\"mood\":\"happy\"},{\"at\":2,\"mood\":\"worried\"}]"))
using (var emptyMarks = JsonDocument.Parse("[]"))
using (var unknown = JsonDocument.Parse("[{\"at\":0,\"mood\":\"unknown\"}]"))
{
    var live = new LiveDisplayState();
    live.Apply("snapshot", "r1", "😀", marks.RootElement);
    Check(live.Mood == "happy", "mood offsets use Unicode code points");
    live.Apply("delta", "r1", "가", marks.RootElement);
    Check(live.Mood == "worried", "later mood after delta");
    live.Apply("snapshot", "r2", "새 답", emptyMarks.RootElement);
    Check(live.Text == "새 답" && live.Mood == "neutral", "new run resets mood");
    live.Apply("delta", "r3", "다음", emptyMarks.RootElement);
    Check(live.Text == "다음" && live.Mood == "neutral", "run change without snapshot resets state");
    Check(DisplayState.Mood(marks.RootElement, 2) == "worried", "saved final restores last mood");
    Check(DisplayState.Mood(emptyMarks.RootElement, 5) == "neutral", "missing mood resets to neutral");
    Check(DisplayState.Mood(unknown.RootElement, 1) == "neutral", "unknown mood fallback");
}
var gate = new ConnectionGate();
Check(gate.TryEnter(), "first connection operation enters");
Check(!gate.TryEnter(), "concurrent pairing is rejected");
using (var gateCancel = new CancellationTokenSource())
{
    var waitingForGate = gate.EnterAsync(gateCancel.Token);
    Check(!waitingForGate.IsCompleted, "restore waits for pairing");
    gateCancel.Cancel();
    try { await waitingForGate; throw new Exception("gate cancellation ignored"); } catch (OperationCanceledException) { }
}
gate.Exit(); await gate.EnterAsync(CancellationToken.None); gate.Exit();
var sendGate = new ConnectionGate(); var releaseSend = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously); int posts = 0;
async Task PressEnter()
{
    if (!sendGate.TryEnter()) return;
    try { posts++; await releaseSend.Task; } finally { sendGate.Exit(); }
}
var firstPress = PressEnter(); await PressEnter(); await PressEnter();
Check(posts == 1, "repeated Enter while sending creates only one request");
releaseSend.SetResult(); await firstPress; await PressEnter();
Check(posts == 2, "sending can resume after request finishes");
var connectionA = new object(); var connectionB = new object();
var sendContext = new SendContext(connectionA, "conversation-A", 7);
Check(sendContext.CanClear(connectionA, "conversation-A", true, 7), "unchanged original draft may clear");
Check(!sendContext.CanClear(connectionA, "conversation-B", true, 7), "old result cannot clear another conversation");
Check(!sendContext.CanClear(connectionA, "conversation-A", true, 9), "edited then restored draft is preserved");
Check(!sendContext.IsCurrent(connectionB, "conversation-A", true), "old connection cannot update new connection");
Check(!sendContext.IsCurrent(connectionA, "conversation-A", false), "offline state is not overwritten");
var pendingResponse = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
object currentClient = connectionA; string currentId = "conversation-A"; long revision = 7;
string draft = "같은 내용", uiStatus = "B 상태"; bool showError = false;
async Task CompleteOldSend(bool fail)
{
    await pendingResponse.Task;
    if (fail) { if (sendContext.IsCurrent(currentClient, currentId, true)) showError = true; return; }
    if (sendContext.CanClear(currentClient, currentId, true, revision)) draft = "";
    if (sendContext.IsCurrent(currentClient, currentId, true)) uiStatus = "답변 대기";
}
var delayedSuccess = CompleteOldSend(false); var delayedFailure = CompleteOldSend(true);
currentId = "conversation-B"; revision++; pendingResponse.SetResult(); await Task.WhenAll(delayedSuccess, delayedFailure);
Check(draft == "같은 내용" && uiStatus == "B 상태" && !showError, "late success and error do not change another conversation");
var restoredWindow = WindowPreferences.Normalize(new(2400, -900, 900), 0, 0, 1920, 1080, 420, 700);
Check(CharacterTheme.Parse("{\"characterId\":\"himari\",\"bubbleTemplate\":\"pixel\"}").BubbleTemplate == "pixel", "character bubble template");
Check(CharacterTheme.Parse("{\"characterId\":\"himari\",\"bubbleTemplate\":\"unknown\"}").BubbleTemplate == "default", "unknown template fallback");
Check(CharacterTheme.Parse("bad json") == CharacterTheme.Default, "invalid character manifest fallback");
Check(restoredWindow == new WindowPreferences(1500, 0, 400), "window and size clamped to visible bounds");
var safeWindow = WindowPreferences.Normalize(new(double.NaN, double.PositiveInfinity, double.NaN), -1920, 0, 3840, 1080, 420, 700);
Check(safeWindow == new WindowPreferences(-1920, 0, 330), "invalid preferences use safe defaults");
var firstHistory = Enumerable.Range(1, 100).Select(n => new { sequence = n, kind = n == 1 ? "user_message" : "tool_result", text = n == 1 ? "<hello>" : "private tool result" });
var pageOne = JsonSerializer.SerializeToElement(new { items = firstHistory });
var pageTwo = JsonSerializer.SerializeToElement(new { items = new[] { new { sequence = 101, kind = "assistant_final", text = "[[mood:happy]]raw", display = new { text = "안녕하세요" } } } });
var requestedPages = new List<int>();
string historyText = await HistoryReader.ReadAsync((after, ct) => { requestedPages.Add(after); return Task.FromResult(after == 0 ? pageOne : pageTwo); }, null, CancellationToken.None);
Check(requestedPages.SequenceEqual(new[] { 0, 100 }) && historyText.Contains("<hello>") && historyText.Contains("안녕하세요") && !historyText.Contains("private tool result") && !historyText.Contains("[[mood:"), "history pages and display text");
using (var cancelledHistory = new CancellationTokenSource())
{
    cancelledHistory.Cancel();
    try { await HistoryReader.ReadAsync((_, _) => throw new Exception("unexpected page"), null, cancelledHistory.Token); throw new Exception("history cancellation ignored"); }
    catch (OperationCanceledException) { }
}
Console.WriteLine("Contract transport checks passed.");

using (var screenClient = new ServerClient("https://example.com/", "test", new ScreenProtocolHandler()))
{
    var staged = await screenClient.UploadScreenAsync("c1", [1, 2, 3], "image/png", CancellationToken.None);
    var job = await screenClient.SendAsync(HttpMethod.Post, "/api/conversations/c1/messages", new { text = "봐줘", screen = staged.GetProperty("file_id").GetString() }, CancellationToken.None);
    var replay = new MovePlayback(job.GetProperty("job_id").GetString()!, geometry, captured);
    var collected = new List<ScreenMove>();
    await foreach (var (kind, data) in screenClient.StreamAsync("/api/conversations/c1/live", CancellationToken.None))
        if (kind == "snapshot") collected.AddRange(replay.Accept(data.GetProperty("job_id").GetString(), data.GetProperty("run_id").GetString(), true, data.GetProperty("moves"), 1, captured));
    Check(collected.Count == 1 && collected[0].X == 42, "PR 126 upload, message job id and reconnect snapshot");
}

sealed class ReconnectRecoveryHandler : HttpMessageHandler
{
    private static bool screenRecorded;
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        if (request.Method == HttpMethod.Post)
        {
            using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
            if (body.RootElement.TryGetProperty("screen", out var file))
            {
                if (file.GetString() != "f1") throw new Exception("screen was recaptured instead of reused");
                if (screenRecorded) return new(HttpStatusCode.Conflict) { Content = new StringContent("{\"error\":{\"code\":\"screen_already_sent\"}}") };
                screenRecorded = true;
            }
            throw new HttpRequestException("accepted, response lost");
        }
        string value = request.RequestUri!.AbsolutePath.EndsWith("/events")
            ? "{\"items\":[{\"kind\":\"user_message\",\"sequence\":1,\"text\":\"hello\",\"attachments\":[{\"file_id\":\"f1\",\"screen\":true,\"image\":{\"state\":\"ready\"}}]}]}"
            : "{\"job\":{\"job_id\":\"j1\",\"running\":true},\"conversation\":{\"last_sequence\":1}}";
        return new(HttpStatusCode.OK) { Content = new StringContent(value, Encoding.UTF8, "application/json") };
    }
}

sealed class LostResponseHandler(string state) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        if (request.Method == HttpMethod.Post) throw new HttpRequestException("202 was lost after server accepted screen");
        string value = request.RequestUri!.AbsolutePath.EndsWith("/events")
            ? JsonSerializer.Serialize(new { items = new[] { new { kind = "user_message", attachments = new[] { new { file_id = "f1", screen = true, image = new { state } } } } } })
            : "{\"job\":{\"job_id\":\"j1\",\"running\":true},\"conversation\":{\"last_sequence\":1}}";
        return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(value, Encoding.UTF8, "application/json") });
    }
}

sealed class ScreenProtocolHandler : HttpMessageHandler
{
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        if (request.RequestUri!.AbsolutePath.EndsWith("/uploads")) return Json("{\"file_id\":\"f1\"}");
        if (request.RequestUri.AbsolutePath.EndsWith("/messages"))
        {
            using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
            if (body.RootElement.GetProperty("screen").GetString() != "f1") throw new Exception("screen id missing");
            return Json("{\"job_id\":\"j1\"}");
        }
        const string snapshot = "event: snapshot\ndata: {\"job_id\":\"j1\",\"run_id\":\"r1\",\"text\":\"가\",\"moves\":[{\"at\":1,\"x\":42,\"y\":5}]}\n\n";
        return new(HttpStatusCode.OK) { Content = new StringContent(snapshot + snapshot, Encoding.UTF8, "text/event-stream") };
    }
    private static HttpResponseMessage Json(string value) => new(HttpStatusCode.OK) { Content = new StringContent(value, Encoding.UTF8, "application/json") };
}

sealed class FakeHandler(string token) : HttpMessageHandler
{
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        string path = request.RequestUri!.AbsolutePath;
        if (path == "/api/devices/pair")
        {
            if (request.Headers.Authorization?.ToString() != "Pairing ABCDEFGH" || !(await request.Content!.ReadAsStringAsync(ct)).Contains(token)) throw new Exception("pair request");
            return Json("{\"device_id\":\"d1\",\"name\":\"PC\"}");
        }
        if (request.Headers.Authorization?.ToString() != "Bearer " + token) throw new Exception("bearer header");
        if (path == "/api/conversations/c1/uploads")
        {
            if (request.Method != HttpMethod.Post || request.RequestUri!.Query != "?name=screen.png" || request.Content?.Headers.ContentType?.MediaType != "image/png" || !(await request.Content.ReadAsByteArrayAsync(ct)).SequenceEqual(new byte[] { 1, 2, 3 })) throw new Exception("screen upload request");
            return Json("{\"file_id\":\"f1\"}");
        }
        if (path == "/api/device/presence") return new HttpResponseMessage(HttpStatusCode.NoContent);
        if (path == "/api/events") return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(": keep-alive\r\nevent: hello\r\ndata: {\"text\":\"안녕😀\"}\r\n\r\nevent: current\ndata: {\"text\":\ndata: \"끝\"}\n\n", Encoding.UTF8, "text/event-stream") };
        return new HttpResponseMessage(HttpStatusCode.Forbidden) { Content = new StringContent("{\"error\":{\"code\":\"device_scope\"}}", Encoding.UTF8, "application/json") };
    }
    private static HttpResponseMessage Json(string value) => new(HttpStatusCode.OK) { Content = new StringContent(value, Encoding.UTF8, "application/json") };
}

sealed class FakeVault : ICredentialVault
{
    public DeviceCredential? ActiveValue, PendingValue;
    public DeviceCredential? Active() => ActiveValue;
    public DeviceCredential? Pending() => PendingValue;
    public void SaveActive(DeviceCredential value) => ActiveValue = value;
    public void SavePending(DeviceCredential value) => PendingValue = value;
    public void DeletePending() => PendingValue = null;
}
sealed class SharedHandler(PairHandler inner) : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct) => inner.Handle(request, ct);
}
sealed class PairHandler
{
    public bool FailPair, LoseResponse;
    public int TokenConflicts, BusyResponses;
    public readonly List<string> AttemptedTokens = [], AttemptedCodes = [];
    public string? RegisteredToken;
    public async Task<HttpResponseMessage> Handle(HttpRequestMessage request, CancellationToken ct)
    {
        if (request.Method == HttpMethod.Get)
            return request.Headers.Authorization?.Parameter == RegisteredToken && RegisteredToken is not null
                ? Json(HttpStatusCode.OK, "{\"device\":{\"device_id\":\"d1\"}}")
                : Json(HttpStatusCode.Unauthorized, "{\"error\":{\"code\":\"unauthorized\"}}");
        using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
        var attempted = body.RootElement.GetProperty("token").GetString()!;
        AttemptedTokens.Add(attempted); AttemptedCodes.Add(request.Headers.Authorization?.Parameter ?? "");
        if (FailPair) return Json(HttpStatusCode.Unauthorized, "{\"error\":{\"code\":\"pairing_invalid\"}}");
        if (TokenConflicts-- > 0) return Json(HttpStatusCode.Conflict, "{\"error\":{\"code\":\"token_in_use\"}}");
        if (BusyResponses-- > 0) return Json(HttpStatusCode.TooManyRequests, "{\"error\":{\"code\":\"pairing_busy\"}}");
        RegisteredToken = attempted;
        if (LoseResponse) { LoseResponse = false; throw new HttpRequestException("simulated lost response"); }
        return Json(HttpStatusCode.OK, "{\"device_id\":\"d1\",\"name\":\"PC\"}");
    }
    private static HttpResponseMessage Json(HttpStatusCode status, string value) => new(status) { Content = new StringContent(value, Encoding.UTF8, "application/json") };
}
sealed class ScenarioHandler : HttpMessageHandler
{
    private bool completed, answered;
    public string? Answer;
    public void FinishWhileOffline() => completed = true;
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        var path = request.RequestUri!.AbsolutePath;
        if (path == "/api/events")
        {
            string sse = "event: hello\ndata: {\"activity\":{\"working\":" + (completed ? "[]" : "[{\"id\":\"c1\"}]") + "}}\n\n";
            if (!completed) sse += "event: job\ndata: {\"conversation\":\"c1\",\"state\":\"started\"}\n\n";
            return new(HttpStatusCode.OK) { Content = new StringContent(sse, Encoding.UTF8, "text/event-stream") };
        }
        if (path.EndsWith("/events")) return Json("{\"items\":[{\"kind\":\"assistant_final\",\"display\":{\"text\":\"연결 중단 중 완료\"}}]}");
        if (path.EndsWith("/messages"))
        {
            using var body = JsonDocument.Parse(await request.Content!.ReadAsStringAsync(ct));
            Answer = body.RootElement.GetProperty("text").GetString(); answered = true; return Json("{}");
        }
        return Json(answered ? "{\"waiting\":null}" : "{\"waiting\":{\"question\":{\"display\":{\"text\":\"어떻게 할까요?\"}}}}" );
    }
    private static HttpResponseMessage Json(string value) => new(HttpStatusCode.OK) { Content = new StringContent(value, Encoding.UTF8, "application/json") };
}
