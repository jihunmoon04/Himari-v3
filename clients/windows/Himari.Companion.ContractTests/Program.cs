using System.Net;
using System.Text;
using System.Text.Json;
using Himari.Companion;

static void Check(bool value, string message) { if (!value) throw new Exception(message); }
var legacyPresentation = JsonSerializer.Deserialize<WindowPreferences>("{\"Left\":10,\"Top\":20,\"CharacterHeight\":330,\"MoveEnabled\":true}")!;
Check(legacyPresentation.ScreenSharing && !legacyPresentation.FacingLeft && legacyPresentation.ChatHotkey == "Alt+Q", "old preferences migrate to requested capture default and one-handed hotkey");
var newPresentation = legacyPresentation with { ScreenSharing = false, FacingLeft = true, BubbleMinimum = 12, SecondsPer100 = 20, ChatHotkey = "Ctrl+Space", BubblePlacement = "하 고정", LayoutVersion = 1 };
Check(JsonSerializer.Deserialize<WindowPreferences>(JsonSerializer.Serialize(newPresentation)) == newPresentation, "presentation settings persist without losing opt-out or orientation");
var safePresentation = WindowPreferences.Normalize(newPresentation with { BubbleMinimum = double.NaN, SecondsPer100 = 999, ChatHotkey = "invalid" }, 0, 0, 1920, 1080, 520, 700);
Check(safePresentation.BubbleMinimum == 8 && safePresentation.SecondsPer100 == 30 && safePresentation.ChatHotkey == "Alt+Q", "invalid presentation settings are normalized");
Check(DialoguePolicy.Duration("hello", 8, 8) < DialoguePolicy.Duration(new string('가',100), 8, 8) && DialoguePolicy.Duration("😀",8,8) == DialoguePolicy.Duration("가",8,8), "bubble duration follows Unicode character count");
Check(DialoguePolicy.Duration(new string('가',10000),8,8) == 300 && DialoguePolicy.PointAnchor(false) == .1 && DialoguePolicy.PointAnchor(true) == .9, "display duration is bounded and pointing anchor mirrors");
Check(DialoguePolicy.Hotkey("Alt+Q") == (0x4001u, 0x51u) && DialoguePolicy.Hotkey("Ctrl+Space") == (0x4002u, 0x20u), "global key registration uses expected modifiers and no-repeat");
var hotkeySelection = new HotkeyState(); int unregistered = 0;
Check(hotkeySelection.Select("Alt+Q", _ => true, () => unregistered++), "initial global shortcut registers");
var keyAttempts = new List<string>();
Check(!hotkeySelection.Select("Ctrl+Space", key => { keyAttempts.Add(key); return key == "Alt+Q"; }, () => unregistered++) && hotkeySelection.Current == "Alt+Q" && hotkeySelection.IsRegistered, "shortcut collision restores previous registration and selection");
Check(keyAttempts.SequenceEqual(new[] { "Ctrl+Space", "Alt+Q" }) && unregistered == 1, "failed change restores previous key after releasing it");
Check(!DialoguePolicy.HotkeyCloses(false, false) && !DialoguePolicy.HotkeyCloses(false, true), "shortcut enters Himari from other windows regardless of editor state");
Check(DialoguePolicy.HotkeyCloses(true, true) && DialoguePolicy.HotkeyCloses(true, false), "shortcut switches away even when the companion editor is hidden");
Check(legacyPresentation.BubblePlacement == "상하 자동", "old preferences default to automatic vertical placement");
Check(DialoguePolicy.BubblesBelow("상하 자동", 200, 540) && !DialoguePolicy.BubblesBelow("상하 자동", 700, 540), "automatic placement follows monitor midpoint");
Check(!DialoguePolicy.BubblesBelow("상 고정", 100, 540) && DialoguePolicy.BubblesBelow("하 고정", 900, 540), "fixed placement does not flip near an edge");
var workHistory = new WorkWindowHistory(); int restores = 0;
Check(!workHistory.Return(_ => { restores++; return true; }) && restores == 0, "no work window is restored before entry");
workHistory.Remember(new IntPtr(10), _ => true);
workHistory.Remember(new IntPtr(20), _ => false);
Check(workHistory.Target == new IntPtr(10) && workHistory.Return(window => { restores++; return window == new IntPtr(10); }), "own windows do not overwrite previous work target");
Check(!workHistory.Return(_ => false) && workHistory.Target == new IntPtr(10), "closed or denied work target fails safely without switching elsewhere");
Check(DialoguePolicy.KeepUserBubble(false,false,"draft",false,false) && DialoguePolicy.KeepUserBubble(false,false,"",false,true) && DialoguePolicy.KeepUserBubble(false,true,"",false,false), "auto-hide preserves draft, unresolved delivery and active input");
Check(!DialoguePolicy.KeepUserBubble(false,false,"",false,false), "sent bubble may expire when no interaction needs it");
foreach (bool textOnlyFollowUp in new[] { false, true })
foreach (string quickScenario in new[] { "completed", "already-completed", "different-job", "different-screen", "no_sandbox", "view_expired" })
{
    var verifyingJob = new MoveJobLifetime();
    Check(verifyingJob.ReserveVerification("j1"), "reserve before asynchronous screen lookup");
    using var quick = new ServerClient("https://example.com/", "test", new QuickCompletionHandler(quickScenario, () => { verifyingJob.End("j1"); verifyingJob.CompleteLive("j1"); }, textOnlyFollowUp));
    var file = await ScreenHistory.LatestReadyIdAsync(quick, "c", "j1", CancellationToken.None,
        expectedFileId: textOnlyFollowUp ? null : "f1", previousFileId: textOnlyFollowUp ? "f1" : null);
    Check(file == (quickScenario is "completed" or "already-completed" ? "f1" : null), "screen lookup after completion requires exact job, screen and readiness: " + quickScenario + ", textOnly=" + textOnlyFollowUp);
    Check(!verifyingJob.CanReceive("j1") && verifyingJob.CanBind("j1"), "in-flight verification can bind buffered moves without reopening live");
    verifyingJob.FinishVerification("j1");
    Check(!verifyingJob.CanBind("j1") && !verifyingJob.ReserveVerification("j1"), "completion cannot be resurrected after verification retires");
}
using (var missingPrevious = new ServerClient("https://example.com/", "test", new QuickCompletionHandler("completed", () => { })))
    Check(await ScreenHistory.LatestReadyIdAsync(missingPrevious, "c", "j1", CancellationToken.None) is null, "completed follow-up without local previous screen is rejected");
using (var newScreenWins = new ServerClient("https://example.com/", "test", new QuickCompletionHandler("completed", () => { })))
    Check(await ScreenHistory.LatestReadyIdAsync(newScreenWins, "c", "j1", CancellationToken.None, "new-f2", "f1") is null, "new attachment takes priority; do not fall back to older screen on mismatch");
// Both streams can end while the first animation is still blocked.
var orderedLifetime = new MoveJobLifetime();
var firstAnimation = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
var animationOrder = new List<int>();
Task orderedMoves = Task.CompletedTask;
orderedMoves = MoveQueue.RunAfterAsync(orderedMoves, async () => { animationOrder.Add(1); await firstAnimation.Task; });
orderedMoves = MoveQueue.RunAfterAsync(orderedMoves, () => { animationOrder.Add(2); return Task.CompletedTask; });
orderedLifetime.End("job");
Check(!orderedLifetime.CanStart("job") && orderedLifetime.CanReceive("job"), "job ended blocks new contexts while existing live accepts final delta");
if (orderedLifetime.CanReceive("job")) orderedMoves = MoveQueue.RunAfterAsync(orderedMoves, () => { animationOrder.Add(3); return Task.CompletedTask; });
orderedLifetime.CompleteLive("job");
Check(!orderedLifetime.CanReceive("job") && !orderedLifetime.CanStart("job"), "done closes reception without reopening ended context");
Check(animationOrder.SequenceEqual(new[] { 1 }), "following moves wait for first animation");
firstAnimation.SetResult(); await orderedMoves;
Check(animationOrder.SequenceEqual(new[] { 1, 2, 3 }), "all accepted moves drain after job ended and live done");
var protocol129 = new OriginRecoveryHandler();
using (var protocolClient = new ServerClient("https://example.com/", "test", protocol129))
{
    Check(await ScreenHistory.ResolveJobIdAsync(protocolClient, "c", "activity-job", CancellationToken.None) == "activity-job" && protocol129.Reads == 0, "activity job id is used without detail request");
    Check(await ScreenHistory.ResolveJobIdAsync(protocolClient, "c", null, CancellationToken.None) == "detail-job" && protocol129.Reads == 1, "legacy activity retains detail fallback");
    Check(await TextRecovery.AcceptedAsync(protocolClient, "c", "hello", "d1", 41, CancellationToken.None), "PR 129 origin and text confirm unknown delivery");
    Check(!await TextRecovery.AcceptedAsync(protocolClient, "c", "hello", "other", 41, CancellationToken.None), "another device's identical text is not confirmation");
}
var invalidScreen = new ApiException(HttpStatusCode.BadRequest, "not_a_screen");
Check(MessageFailures.Classify(invalidScreen) == MessageFailure.Rejected && !MessageFailures.ReuseScreen(invalidScreen), "not_a_screen is rejected and cannot reuse staged screen");
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

var declinedVault = new FakeVault { ActiveValue = new DeviceCredential("https://old.example/", "old-token", null) };
var declinedServer = new PairHandler();
var originalCredential = declinedVault.ActiveValue;
int switchPrompts = 0;
var declinedPairing = new PairingCoordinator(declinedVault, c => new ServerClient(c.Server, c.Token, new SharedHandler(declinedServer)), approveChange: _ =>
{
    switchPrompts++;
    Check(declinedVault.ActiveValue == originalCredential && declinedVault.PendingValue is null, "approval requested before any credential write");
    return false;
});
try { await declinedPairing.PairAsync("https://example.com/", "NEW-CODE", CancellationToken.None); throw new Exception("declined switch continued"); }
catch (DeviceSwitchDeclinedException) { }
Check(switchPrompts == 1 && declinedServer.AttemptedTokens.Count == 0 && declinedVault.ActiveValue == originalCredential && declinedVault.PendingValue is null, "declined switch preserves active and pending credentials without pairing request");
using (var oldRecoveryOwner = new ServerClient("https://old.example/", "old-token", new SharedHandler(declinedServer)))
    Check(oldRecoveryOwner.Matches(declinedVault.Active()!), "reconnect after declined pairing selects original recovery identity");
// A prior successful-but-unpromoted pairing must also ask before changing Active.
declinedVault.PendingValue = new DeviceCredential("https://example.com/", "new-token", null);
var pendingBeforeDecline = declinedVault.PendingValue;
var declinedRecovery = new PairingCoordinator(declinedVault, c => new ServerClient(c.Server, c.Token, new SharedHandler(declinedServer)), approveChange: _ => false);
try { await declinedRecovery.RecoverPendingAsync(CancellationToken.None); throw new Exception("declined pending promotion continued"); }
catch (DeviceSwitchDeclinedException) { }
Check(declinedVault.ActiveValue == originalCredential && declinedVault.PendingValue == pendingBeforeDecline, "declined pending recovery preserves both credentials");

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
// V3-P87 §9, §10: a wake in the history, and whose a job is
var wakeHistory = JsonSerializer.SerializeToElement(new { items = new object[] {
    new { sequence = 1, kind = "wake", text = "HIMARI reached out · a try" },
    new { sequence = 2, kind = "assistant_final", display = new { text = "아직 거기 있어?" } } } });
string wakeText = await HistoryReader.ReadAsync((_, _) => Task.FromResult(wakeHistory), null, CancellationToken.None);
Check(wakeText == "(히마리가 먼저 말을 걸었어요)\n\n히마리\n아직 거기 있어?\n\n" && !wakeText.Contains("reached out"), "a wake shows as one line before HIMARI's words");
JsonElement Origin(object value) => JsonSerializer.SerializeToElement(value);
Check(JobOrigin.IsOwn(Origin(new { kind = "device", device_id = "d1" }), "d1") && JobOrigin.IsOwn(Origin(new { kind = "wake", device_id = "d1" }), "d1"), "a job this device speaks: its message's or a wake on it");
Check(!JobOrigin.IsOwn(Origin(new { kind = "wake", device_id = "d2" }), "d1") && !JobOrigin.IsOwn(Origin(new { kind = "web" }), "d1") && !JobOrigin.IsOwn(Origin(new { kind = "wake", device_id = "d1" }), null), "another device's wake, the Web, or no device id are not");
Check(JobOrigin.IsWake(Origin(new { kind = "wake", device_id = "d1" })) && !JobOrigin.IsWake(Origin(new { kind = "device", device_id = "d1" })) && !JobOrigin.IsWake(default), "a wake is told apart");
Check(JobOrigin.Follow("c1", "c2", "") && JobOrigin.Follow(null, "c2", "  ") && !JobOrigin.Follow("c2", "c2", "") && !JobOrigin.Follow("c1", "c2", "쓰는 중"), "follow a wake to its conversation unless shown already or the user is typing");
Check(JobOrigin.Status("c2", "c1", "연결됨") == JobOrigin.WakeNotice && JobOrigin.Status("c2", "c2", "연결됨") == "연결됨" && JobOrigin.Status(null, "c1", "연결됨") == "연결됨", "the wake notice stays until that conversation is opened");
Check(JobOrigin.StillFollow("c1", "c1", 4, 4, "") && !JobOrigin.StillFollow("c1", "c1", 4, 5, "") && !JobOrigin.StillFollow("c1", "c3", 4, 4, "") && !JobOrigin.StillFollow("c1", "c1", 4, 4, "a"), "once the list is read: not when the user typed, picked another conversation or the editor holds text");
using (var cancelledHistory = new CancellationTokenSource())
{
    cancelledHistory.Cancel();
    try { await HistoryReader.ReadAsync((_, _) => throw new Exception("unexpected page"), null, cancelledHistory.Token); throw new Exception("history cancellation ignored"); }
    catch (OperationCanceledException) { }
}

// Character animation (docs/companion-animation-v0.1.md §5), driven with explicit times.
var shipped = CharacterManifest.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "character.json")));
Check(shipped.Width == 205 && shipped.Height == 206 && new[] { "neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried" }.All(shipped.Faces.ContainsKey),
    "shipped manifest covers the server's eight moods on the 205x206 canvas");
Check(shipped.Faces["neutral"].Eyes!.Keys.Order().SequenceEqual(["closed", "half", "left", "right"]) && shipped.Faces["neutral"].Mouth!.Keys.Order().SequenceEqual(["small", "wide"]), "neutral has blink and talk variants");
Check(shipped.Faces.All(f => f.Value.Mouth?.Keys.Order().SequenceEqual(["small", "wide"]) == true)
    && shipped.Faces.Where(f => f.Key != "happy").All(f => f.Value.Eyes?.Keys.Order().SequenceEqual(["closed", "half", "left", "right"]) == true)
    && shipped.Faces["happy"].Eyes is null,
    "every mood talks; every mood but happy (eyes already closed in a smile) blinks");
var shippedSleep = shipped.Clips["sleep"];
Check(shippedSleep is { Loop: true, LoopFrom: > 0 } && shippedSleep.Frames[0].Src.SequenceEqual(shipped.Clips["idle"].Frames[0].Src)
    && shippedSleep.Frames.Take(shippedSleep.LoopFrom).Any(f => f.Eyes == "half") && shippedSleep.Frames.Skip(shippedSleep.LoopFrom).All(f => f.Eyes == "closed")
    && Math.Abs(shippedSleep.Length.TotalMilliseconds - shippedSleep.IntroMs - 6000) < 1 && shippedSleep.Frames.Skip(shippedSleep.LoopFrom).All(f => f.Face[1] > 0),
    "the shipped sleep clip nods off from the rest pose, then breathes every 6 s with the head lowered and the eyes closed");
Check(shipped.Clips["idle"].Frames.Length == 6 && shipped.Clips["idle"].Frames.All(f => f.Eyes is null), "idle keeps its own six frames, none of the sleep frames");
foreach (var bad in new[] { "{\"version\":2,\"canvas\":[1,1],\"sheet\":\"s\",\"face\":{\"origin\":[0,0],\"size\":[1,1]},\"clips\":{},\"faces\":{}}",
                            "{\"version\":1,\"canvas\":[1,1],\"sheet\":\"s\",\"face\":{\"origin\":[0,0],\"size\":[1,1]},\"clips\":{},\"faces\":{\"neutral\":{\"src\":[0,0,1,1]}}}",
                            "{\"version\":1,\"canvas\":[1,1],\"sheet\":\"s\",\"face\":{\"origin\":[0,0],\"size\":[0,1]},\"clips\":{\"idle\":{\"loop\":true,\"frames\":[{\"src\":[0,0,1,1],\"ms\":1,\"face\":[0,0]}]}},\"faces\":{\"neutral\":{\"src\":[0,0,1,1]}}}",
                            "{\"version\":1,\"canvas\":[1,1],\"sheet\":\"s\",\"clips\":{\"idle\":{\"loop\":true,\"frames\":[{\"src\":[0,0,1,1],\"ms\":1,\"face\":[0,0]}]}},\"faces\":{\"neutral\":{\"src\":[0,0,1,1]}}}",
                            "{\"version\":1,\"canvas\":[1,1],\"sheet\":\"s\",\"face\":{\"origin\":[0,0],\"size\":[1,1]},\"clips\":{\"idle\":{\"loop\":true,\"frames\":[{\"src\":[0,0,-1,1],\"ms\":1,\"face\":[0,0]}]}},\"faces\":{\"neutral\":{\"src\":[0,0,1,1]}}}",
                            "{\"version\":1,\"canvas\":[1,1],\"sheet\":\"s\",\"face\":{\"origin\":[0,0],\"size\":[1,1]},\"clips\":{\"idle\":{\"loop\":true,\"frames\":[{\"src\":[0,0,1,1],\"ms\":1,\"face\":[0,0]}]}},\"faces\":{\"neutral\":{\"src\":[0,0,1,1],\"eyes\":{\"half\":{\"src\":[0,0,1,1],\"at\":[0]}}}}}",
                            "{\"version\":1,\"canvas\":[1,1],\"sheet\":\"s\",\"face\":{\"origin\":[0,0],\"size\":[1,1]},\"clips\":{\"idle\":{\"loop\":true,\"loopFrom\":1,\"frames\":[{\"src\":[0,0,1,1],\"ms\":1,\"face\":[0,0]}]}},\"faces\":{\"neutral\":{\"src\":[0,0,1,1]}}}",
                            "{\"version\":1,\"canvas\":[1,1],\"sheet\":\"s\",\"face\":{\"origin\":[0,0],\"size\":[1,1]},\"clips\":{\"idle\":{\"loop\":true,\"frames\":[{\"src\":[0,0,1,1],\"ms\":1,\"face\":[0,0]}]},\"wave\":{\"loop\":false,\"loopFrom\":1,\"frames\":[{\"src\":[0,0,1,1],\"ms\":1,\"face\":[0,0]},{\"src\":[0,0,1,1],\"ms\":1,\"face\":[0,0]}]}},\"faces\":{\"neutral\":{\"src\":[0,0,1,1]}}}" })
{
    try { CharacterManifest.Parse(bad); Check(false, "invalid character manifest is rejected"); }
    catch (InvalidDataException) { }
}
var shippedRects = shipped.Clips.Values.SelectMany(c => c.Frames.Select(f => f.Src)).Concat(shipped.Faces.Values.Select(f => f.Src))
    .Concat(shipped.Faces.Values.SelectMany(f => (f.Eyes ?? []).Concat(f.Mouth ?? []).Select(v => v.Value.Src))).ToList();
int sheetRight = shippedRects.Max(r => r[0] + r[2]), sheetBottom = shippedRects.Max(r => r[1] + r[3]);
shipped.CheckSheet(sheetRight, sheetBottom);
try { shipped.CheckSheet(sheetRight, sheetBottom - 1); Check(false, "a src outside the sheet is rejected"); }
catch (InvalidDataException) { }
static TimeSpan Ms(double value) => TimeSpan.FromMilliseconds(value);
var frame = new ClipFrame([0, 0, 1, 1], 100, [0, 0]);
// "plain" is a mood face without blink or talk art.
var animationManifest = shipped with
{
    Faces = new(shipped.Faces) { ["plain"] = new CharacterFace([0, 0, 1, 1]) },
    Clips = new()
    {
        ["idle"] = new(true, [frame, frame with { Src = [1, 0, 1, 1], Ms = 300 }]),
        ["wave"] = new(false, [frame, frame with { Src = [2, 0, 1, 1] }]),
        ["roll"] = new(true, [frame, frame with { Src = [3, 0, 1, 1] }]),
        ["sleep"] = new(true, [frame with { Eyes = "closed" }]),
        ["doze"] = new(true, [frame with { Eyes = "half" }, frame with { Src = [4, 0, 1, 1], Eyes = "closed" }, frame with { Src = [5, 0, 1, 1], Eyes = "closed" }], LoopFrom: 1),
    }
};
// The first blink comes 2.5 s at the earliest, so the checks before 1.4 s see open eyes.
var animator = new CharacterAnimator(animationManifest, 7, TimeSpan.Zero);
Check(animator.Pose(Ms(50)) == new CharacterPose("idle", 0, "neutral", null, null) && animator.Pose(Ms(150)).Frame == 1 && animator.Pose(Ms(450)).Frame == 0,
    "idle loops through its frames");
Check(animator.NextChange(Ms(150)) == Ms(400), "the animator sleeps until the next frame boundary");
var wave = animator.Request("wave", AnimationLayer.Reaction, Ms(500));
var roll = animator.Request("roll", AnimationLayer.Directed, Ms(520));
Check(animator.Pose(Ms(530)).Clip == "roll" && animator.Pose(Ms(530)).Frame == 0, "a higher layer interrupts at once from its first frame");
animator.Release(roll, Ms(560));
Check(animator.Pose(Ms(570)).Clip == "wave" && animator.Pose(Ms(570)).Frame == 0, "after the higher request ends, the lower one restarts from its first frame");
Check(animator.Pose(Ms(680)).Clip == "wave" && animator.Pose(Ms(680)).Frame == 1 && animator.Pose(Ms(780)).Clip == "idle" && animator.Pose(Ms(780)).Frame == 0,
    "a one-shot clip plays once and hands back to idle from its first frame");
// A one-shot covered almost to its end still plays in full when it comes back (PR #142 review).
var hidden = new CharacterAnimator(animationManifest, 7, TimeSpan.Zero);
hidden.Request("wave", AnimationLayer.Reaction, Ms(0));
Check(hidden.Pose(Ms(10)) is { Clip: "wave", Frame: 0 }, "the one-shot starts when requested");
var cover = hidden.Request("roll", AnimationLayer.Directed, Ms(50));
Check(hidden.Pose(Ms(60)).Clip == "roll", "a higher layer covers the one-shot");
hidden.Release(cover, Ms(190));
Check(hidden.Pose(Ms(195)) is { Clip: "wave", Frame: 0 } && hidden.Pose(Ms(295)) is { Clip: "wave", Frame: 1 } && hidden.Pose(Ms(385)).Clip == "wave"
    && hidden.NextChange(Ms(385)) == Ms(390) && hidden.Pose(Ms(395)).Clip == "idle",
    "a covered one-shot replays from its first frame for its full 200 ms");
var longHidden = new CharacterAnimator(animationManifest, 7, TimeSpan.Zero);
longHidden.Request("wave", AnimationLayer.Reaction, Ms(0));
longHidden.Pose(Ms(10));
var longCover = longHidden.Request("roll", AnimationLayer.Directed, Ms(50));
longHidden.Pose(Ms(60));
longHidden.Release(longCover, Ms(300));
Check(longHidden.Pose(Ms(305)) is { Clip: "wave", Frame: 0 } && longHidden.Pose(Ms(495)).Clip == "wave" && longHidden.Pose(Ms(505)).Clip == "idle",
    "a one-shot covered past its original end still comes back and plays in full");
var dropped = hidden.Request("wave", AnimationLayer.Reaction, Ms(400));
hidden.Release(dropped, Ms(450));
Check(hidden.Pose(Ms(460)).Clip == "idle", "releasing a one-shot ends it at once");
animator.Mood = "happy";
Check(animator.Pose(Ms(800)).Mood == "happy", "mood comes from the server");
animator.Mood = "unknown";
Check(animator.Pose(Ms(800)).Mood == "neutral", "an unknown mood falls back to neutral");
animator.SetTalking(true, Ms(1000));
Check(animator.Pose(Ms(1010)).Mouth == "small" && animator.Pose(Ms(1140)).Mouth == "wide" && animator.Pose(Ms(1270)).Mouth == "small" && animator.Pose(Ms(1400)).Mouth is null,
    "talking cycles small, wide, small, closed in 130 ms steps");
animator.SetTalking(false, Ms(1150));
Check(animator.Pose(Ms(1200)).Mouth == "wide" && animator.Pose(Ms(1260)).Mouth is null, "when talking stops the current step finishes, then the mouth closes");
animator.Mood = "plain";
animator.SetTalking(true, Ms(1300));
Check(animator.Pose(Ms(1310)).Mouth is null && animator.Pose(Ms(1310)).Eyes is null, "a mood without the variant keeps its own face");
animator.SetTalking(false, Ms(1310)); animator.Mood = "neutral";
var blinks = new List<(double Start, string Eyes)>();
string? previous = null;
for (double t = 1400; t < 31400; t += 10)
{
    var eyes = animator.Pose(Ms(t)).Eyes;
    if (eyes != previous) { blinks.Add((t, eyes ?? "open")); previous = eyes; }
}
var opens = blinks.Where((b, i) => b.Eyes == "half" && (i == 0 || blinks[i - 1].Eyes == "open")).Select(b => b.Start).ToList();
// The second half of a double blink follows 330 ms after the first; count it with the first.
var starts = opens.Where((t, i) => i == 0 || t - opens[i - 1] > 1000).ToList();
var gaps = starts.Zip(starts.Skip(1), (a, b) => b - a).ToList();
Check(starts.Count >= 5 && gaps.All(g => g >= 2500 + 210 - 10 && g <= 6000 + 540 + 10), "blinks come every 2.5-6 s after the previous one");
Check(blinks.Select(b => b.Eyes).Where(e => e != "open").Take(3).SequenceEqual(["half", "closed", "half"]), "a blink is half, closed, half");
Check(opens.Count > starts.Count && opens.Zip(opens.Skip(1), (a, b) => b - a).Any(g => Math.Abs(g - 330) <= 10), "some blinks are double, 120 ms apart");
var sleepy = new CharacterAnimator(animationManifest, 7, TimeSpan.Zero);
sleepy.SetSleeping(true, Ms(100));
Check(sleepy.Pose(Ms(200)) is { Clip: "sleep", Eyes: "closed" }, "dozing plays the sleep clip, which forces closed eyes");
sleepy.SetSleeping(false, Ms(300));
Check(sleepy.Pose(Ms(310)).Clip == "idle", "waking returns to idle");
sleepy.SetSleeping(true, Ms(400));
sleepy.SetTalking(true, Ms(500));
Check(sleepy.Pose(Ms(510)) is { Clip: "sleep", Eyes: not "closed", Mouth: "small" }, "talking opens the eyes even in a sleep frame");
var dozer = new CharacterAnimator(animationManifest, 7, TimeSpan.Zero);
dozer.Request("doze", AnimationLayer.Directed, Ms(0));
Check(new[] { 50, 150, 250, 350, 450, 550 }.Select(t => dozer.Pose(Ms(t)).Frame).SequenceEqual([0, 1, 2, 1, 2, 1]), "a clip with an intro plays it once, then loops the rest");
Check(dozer.Pose(Ms(50)).Eyes == "half" && dozer.Pose(Ms(350)).Eyes == "closed", "frames force their eyes");
Check(dozer.NextChange(Ms(320)) == Ms(400) && dozer.NextChange(Ms(50)) == Ms(100), "the next change follows the loop after the intro");
var noSleepArt = new CharacterAnimator(shipped with { Clips = new() { ["idle"] = shipped.Clips["idle"] } }, 7, TimeSpan.Zero);
noSleepArt.SetSleeping(true, Ms(100));
Check(noSleepArt.Pose(Ms(200)) is { Clip: "idle", Eyes: "closed" }, "without sleep art, dozing only closes the eyes");
noSleepArt.SetTalking(true, Ms(300));
Check(noSleepArt.Pose(Ms(310)) is { Eyes: not "closed", Mouth: "small" }, "talking wakes the eyes");
var shippedClips = shipped.Clips;
Check(new[] { "yawn", "glance", "ahoge", "twinkle" }.All(c => shippedClips.TryGetValue(c, out var clip) && !clip.Loop && clip.Frames[0].Src.SequenceEqual(shippedClips["idle"].Frames[0].Src))
    && shippedClips["yawn"].Frames.Any(f => f.OwnFace)
    && shippedClips["glance"].Frames.Any(f => f.Eyes == "left") && shippedClips["glance"].Frames.Any(f => f.Eyes == "right"),
    "the shipped extras are one-shots from the rest pose; the yawn has its own drawn face, the glance looks both ways");
var one = new ClipFrame([0, 0, 1, 1], 100, [0, 0]);
var ownFaced = animationManifest with { Clips = new(animationManifest.Clips) { ["hand"] = new(false, [one with { OwnFace = true, Eyes = "closed" }, one]) } };
var handAnimator = new CharacterAnimator(ownFaced, 7, TimeSpan.Zero);
handAnimator.Request("hand", AnimationLayer.Directed, Ms(0));
handAnimator.SetTalking(true, Ms(10));
Check(handAnimator.Pose(Ms(20)) is { Clip: "hand", Eyes: null, Mouth: null } && handAnimator.Pose(Ms(150)) is { Mouth: not null }, "a frame with its own face shows no eye or mouth variants");
var sequenced = animationManifest with
{
    Clips = new(animationManifest.Clips)
    {
        ["yawn"] = new(false, [one with { Eyes = "closed", Mouth = "wide" }, one]),
        ["stretch"] = new(false, [one with { Eyes = "closed" }, one]),
    }
};
var dozing = new CharacterAnimator(sequenced, 7, TimeSpan.Zero);
dozing.SetSleeping(true, Ms(100));
Check(dozing.Pose(Ms(150)) is { Clip: "yawn", Eyes: "closed", Mouth: "wide" }, "dozing yawns first");
dozing.SetTalking(true, Ms(160));
Check(dozing.Pose(Ms(170)) is { Clip: "yawn", Mouth: "small" }, "talking moves the mouth even in a frame that forces it");
dozing.SetTalking(false, Ms(170));
Check(dozing.Pose(Ms(350)).Clip == "sleep", "the sleep follows the yawn");
dozing.SetSleeping(false, Ms(400));
Check(dozing.Pose(Ms(410)).Clip == "stretch" && dozing.Pose(Ms(610)).Clip == "idle", "waking stretches once, then idles");
dozing.SetSleeping(true, Ms(1000));
dozing.SetSleeping(false, Ms(1050));
Check(dozing.Pose(Ms(1060)).Clip == "stretch" && dozing.Pose(Ms(1400)).Clip == "idle", "waking during the yawn drops the yawn and the sleep still to come");
var extrasManifest = animationManifest with
{
    Clips = new(animationManifest.Clips) { ["glance"] = new(false, [one]), ["ahoge"] = new(false, [one]), ["twinkle"] = new(false, [one]) }
};
var fidget = new CharacterAnimator(extrasManifest, 7, TimeSpan.Zero);
var extraStarts = new List<(double At, string Clip)>();
string? shownClip = null;
for (double t = 0; t <= 600_000; t += 50)
{
    if (t == 300_000) fidget.SetSleeping(true, Ms(t));
    if (t == 400_000) fidget.SetSleeping(false, Ms(t));
    var clip = fidget.Pose(Ms(t)).Clip;
    if (clip != shownClip && clip is "glance" or "ahoge" or "twinkle") extraStarts.Add((t, clip));
    shownClip = clip;
}
var extraGaps = extraStarts.Zip(extraStarts.Skip(1), (a, b) => b.At - a.At).Where((g, i) => extraStarts[i + 1].At < 300_000 || extraStarts[i].At > 400_000).ToList();
Check(extraStarts.Count >= 6 && extraGaps.All(g => g >= 30_000 && g <= 90_100)
    && extraStarts.Zip(extraStarts.Skip(1)).All(p => p.First.Clip != p.Second.Clip)
    && !extraStarts.Any(e => e.At >= 300_000 && e.At < 400_000)
    && extraStarts.Select(e => e.Clip).Distinct().Count() == 3,
    "extras play every 30-90 s over idle, never the same twice in a row and never while dozing");
var glanceManifest = extrasManifest with { Clips = new(extrasManifest.Clips) { ["glance"] = new(false, [one with { Eyes = "left" }]) } };
var cheerful = new CharacterAnimator(glanceManifest, 7, TimeSpan.Zero) { Mood = "happy" };
var cheerfulClips = new HashSet<string>();
for (double t = 0; t <= 600_000; t += 50) cheerfulClips.Add(cheerful.Pose(Ms(t)).Clip);
var plainGlance = new CharacterAnimator(glanceManifest, 7, TimeSpan.Zero);
var plainClips = new HashSet<string>();
for (double t = 0; t <= 600_000; t += 50) plainClips.Add(plainGlance.Pose(Ms(t)).Clip);
Check(!cheerfulClips.Contains("glance") && cheerfulClips.Contains("ahoge") && plainClips.Contains("glance"), "a mood without glance eyes never glances");
var busy = new CharacterAnimator(extrasManifest, 7, TimeSpan.Zero);
busy.Request("roll", AnimationLayer.Directed, Ms(0));
Check(Enumerable.Range(0, 2000).All(i => busy.Pose(Ms(i * 100)).Clip == "roll"), "extras wait while another clip plays");
// --- V3-P88 activity signals ---------------------------------------------------------------------
Check(ActivityCatalog.ProgramKey(@"C:\Program Files\Microsoft VS Code\Code.exe") == "Code" && ActivityCatalog.ProgramKey("code") == "code", "a program is named by its file without .exe");
Check(ActivityCatalog.Classify("Code", null, "main.py - himari", false) == "coding", "known programs have their category");
Check(ActivityCatalog.Classify("chrome", null, "고양이 영상 - YouTube - Chrome", false) == "video" && ActivityCatalog.Classify("chrome", null, "GitHub - Chrome", false) == "browsing", "a browser on a video site is video");
Check(ActivityCatalog.Classify("SomeGame", @"D:\Steam\steamapps\common\Some Game\SomeGame.exe", "Some Game", false) == "game" && ActivityCatalog.Classify("unknown", null, "", true) == "game", "a launcher's folder or exclusive fullscreen is a game");
Check(ActivityCatalog.Classify("unknown", null, "", false) == "other", "anything else is other");
var overrides = SignalSettings.Overrides("obsidian=design\nbad\nzoom=knitting\n=coding");
Check(ActivityCatalog.Classify("Obsidian", null, "", false, overrides) == "design" && overrides.Count == 1, "the user's own category wins; a category outside the list is dropped");
Check(ActivityCatalog.MediaAppName("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "Spotify" && ActivityCatalog.MediaAppName("chrome.exe") == "chrome" && ActivityCatalog.MediaAppName(" ") is null, "a media session's app id becomes a name");

var t0 = new DateTime(2026, 11, 1, 9, 0, 0, DateTimeKind.Utc);
var tracker = new ActivityTracker();
Check(!tracker.Known && tracker.Current(t0) is null, "nothing is known before the first look");
Check(tracker.Observe(new("coding", "Visual Studio Code", Program: "Code"), t0) && tracker.Current(t0.AddMinutes(45))!.ForSeconds == 2700
    && tracker.Current(t0)!.Program == "Code", "the first look is taken at once, counts on and keeps its executable");
Check(!tracker.Observe(new("chat", "KakaoTalk"), t0.AddMinutes(45)) && !tracker.Observe(new("coding", "Visual Studio Code"), t0.AddMinutes(45.5))
    && tracker.Current(t0.AddMinutes(46))!.Category == "coding" && tracker.Current(t0.AddMinutes(46))!.ForSeconds == 2760, "a glance at a messenger does not end the coding");
Check(!tracker.Observe(new("game", "Some Game", true), t0.AddMinutes(50)) && !tracker.Observe(new("game", "Some Game", true), t0.AddMinutes(50.5)), "a new category waits for 60 seconds");
Check(tracker.Observe(new("game", "Some Game", true), t0.AddMinutes(51)) && tracker.Current(t0.AddMinutes(51))!.ForSeconds == 60 && tracker.Current(t0.AddMinutes(51))!.Fullscreen, "after 60 seconds it takes over, counted from when it began");
tracker.Observe(new(null), t0.AddMinutes(60));
Check(tracker.Observe(new(null), t0.AddMinutes(61)) && tracker.Current(t0.AddMinutes(61)) is null && tracker.Known, "nothing in front for 60 seconds is a change too (the game ended)");
tracker.Reset();
Check(!tracker.Known && tracker.Observe(new("document", "Word"), t0.AddMinutes(70)) && tracker.Current(t0.AddMinutes(70))!.Category == "document", "after a lock the next look is taken at once");

var plain = new SignalSettings();
Check(!plain.Media && !plain.Titles && plain.Activity && plain.MicCamera, "media and titles are off by default (owner Q2, Q3)");
var coding = new ActivityState("coding", "Visual Studio Code", 2700.4, false, "secret.docx - Word", "Code");
var oldServer = PresenceBody.Build(3, "Asia/Seoul", false, false, "unlock", coding, true, null, true, true, plain);
Check(oldServer.Keys.OrderBy(k => k).SequenceEqual(new[] { "idle_seconds", "time_zone" }), "an older server gets only what it knows");
var locked = PresenceBody.Build(3, "Asia/Seoul", true, true, "unlock", coding, true, new MediaInfo("Song", null, null), true, true, plain with { Media = true });
Check(locked.Keys.OrderBy(k => k).SequenceEqual(new[] { "idle_seconds", "session", "time_zone" }) && (string)locked["session"]! == "locked", "a locked screen sends its session only");
var full = PresenceBody.Build(3, "Asia/Seoul", true, false, "unlock", coding, true, new MediaInfo(" Song\u0000 ", "Singer", "Spotify"), true, false, plain with { Media = true, Titles = true });
var activityBody = (Dictionary<string, object?>)full["activity"]!;
Check((string)full["after"]! == "unlock" && (int)activityBody["for_seconds"]! == 2700 && (string)activityBody["app"]! == "Visual Studio Code" && (string)activityBody["title"]! == "secret.docx - Word", "an unlocked signal carries the after mark, the activity and, when chosen, its title");
Check((string)((Dictionary<string, object?>)full["media"]!)["title"]! == "Song" && (bool)full["mic_or_camera"]! == false, "media text is cleaned; mic and camera say false when known");
var vsCode = coding with { Program = "Code" };  // shown as "Visual Studio Code", listed by its executable
var listed = PresenceBody.Build(3, "z", true, false, null, vsCode, true, null, true, null, plain with { Titles = true, TitleExclusions = "code.exe" });
Check(!((Dictionary<string, object?>)listed["activity"]!).ContainsKey("title") && (string)((Dictionary<string, object?>)listed["activity"]!)["app"]! == "Visual Studio Code",
    "a title is left out by the executable's name, whatever the program calls itself (PR #150 review B1)");
var unknownProgram = PresenceBody.Build(3, "z", true, false, null, coding with { Program = null }, true, null, true, null, plain with { Titles = true });
Check(!((Dictionary<string, object?>)unknownProgram["activity"]!).ContainsKey("title"), "with no executable known, no title goes");
var noTitle = PresenceBody.Build(3, "z", true, false, null, coding with { App = "KakaoTalk", Program = "KakaoTalk" }, true, null, true, null, plain with { Titles = true });
Check(!((Dictionary<string, object?>)noTitle["activity"]!).ContainsKey("title") && !noTitle.ContainsKey("after") && !noTitle.ContainsKey("mic_or_camera"), "an excluded program's title, an absent mark and an unknown mic are left out");
var off = PresenceBody.Build(3, "z", true, false, null, coding, true, null, true, true, plain with { Activity = false, MicCamera = false });
Check(!off.ContainsKey("activity") && !off.ContainsKey("media") && !off.ContainsKey("mic_or_camera"), "a signal turned off is absent (the server drops what it had)");
var nothing = PresenceBody.Build(3, "z", true, false, null, null, true, null, true, null, plain with { Media = true });
string wire = JsonSerializer.Serialize(nothing, JsonSerializerOptions.Web);
Check(wire.Contains("\"activity\":null") && wire.Contains("\"media\":null"), "nothing in front and nothing playing go as null, not absent");
Check(PresenceBody.Cut(new string('가', 200), PresenceBody.TitleChars)!.Length == 120 && PresenceBody.Cut(" \n ", 10) is null, "text is cut to its limit and blank is none");

var excluded = SignalSettings.Names(SignalSettings.DefaultMicExclusions);
Check(MicCamera.InUse([(@"C:#Program Files#Zoom#bin#Zoom.exe", 100, 0)], excluded, @"C:\Apps\Himari.Companion.exe"), "another program using the microphone counts");
Check(!MicCamera.InUse([(@"C:#Program Files#Zoom#bin#Zoom.exe", 100, 200), (@"C:#obs#bin#64bit#obs64.exe", 100, 0),
    (@"C:#Apps#Himari.Companion.exe", 100, 0), ("Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe", 100, 0), ("never", 0, 0)], excluded, @"C:\Apps\Himari.Companion.exe"),
    "stopped, excluded, this app's own and never-used entries do not");

var schedule = new SignalSchedule();
void SendNow(DateTime at) { var ticket = schedule.Begin(); schedule.Sent(ticket, ticket.After is not null, at); }
Check(schedule.Due(t0), "the first signal goes at once");
SendNow(t0);
Check(!schedule.Due(t0.AddSeconds(30)) && schedule.Due(t0.AddSeconds(60)), "then every 60 seconds");
schedule.Mark("resume");
Check(schedule.Due(t0.AddSeconds(31)) && schedule.After == "resume", "an unlock or a wake goes at once with its mark");
SendNow(t0.AddSeconds(31));
Check(schedule.After is null && !schedule.Due(t0.AddSeconds(31)), "the mark goes once a body carrying it was accepted");
schedule.SeeMedia(new MediaInfo("A", null, null), true);
Check(schedule.Due(t0.AddSeconds(32)), "a media change goes");
SendNow(t0.AddSeconds(32));
schedule.SeeMedia(new MediaInfo("B", null, null), true);
Check(!schedule.Due(t0.AddSeconds(35)) && schedule.Due(t0.AddSeconds(42)), "but at most every 10 seconds");
SendNow(t0.AddSeconds(42));
schedule.SeeMicCamera(true);
Check(schedule.Due(t0.AddSeconds(43)), "the microphone starting goes at once");
SendNow(t0.AddSeconds(43));
// PR #150 review B2: what happens while a request is on its way is not lost when it is accepted
var onTheWay = schedule.Begin();
schedule.Urgent();  // the screen locked meanwhile
schedule.Sent(onTheWay, false, t0.AddSeconds(44));
Check(schedule.Due(t0.AddSeconds(44)), "a lock while a request is on its way is still sent");
SendNow(t0.AddSeconds(45));
onTheWay = schedule.Begin();
schedule.Mark("unlock");  // unlocked meanwhile: the body on its way has no mark
schedule.Sent(onTheWay, false, t0.AddSeconds(46));
Check(schedule.After == "unlock" && schedule.Due(t0.AddSeconds(46)), "an unlock while a request is on its way keeps its mark");
var carrying = schedule.Begin();
schedule.Sent(carrying, false, t0.AddSeconds(47));  // built while locked: the mark was not in the body
Check(schedule.After == "unlock", "a mark the body did not carry is kept");
carrying = schedule.Begin();
schedule.Mark("resume");  // a newer mark while the older one is on its way
schedule.Sent(carrying, true, t0.AddSeconds(48));
Check(schedule.After == "resume" && schedule.Due(t0.AddSeconds(48)), "a newer mark is kept when an older one is accepted");

var evening = new DateTimeOffset(2026, 11, 1, 22, 30, 0, TimeSpan.FromHours(9));
Check(QuietTimes.NextMorning(evening) == new DateTimeOffset(2026, 11, 2, 8, 0, 0, TimeSpan.FromHours(9))
    && QuietTimes.NextMorning(evening.AddHours(-16)) == new DateTimeOffset(2026, 11, 1, 8, 0, 0, TimeSpan.FromHours(9)), "until morning is the next 08:00");
Check(QuietTimes.Describe(null, evening).Contains("먼저 말을 걸 수") && QuietTimes.Describe(evening.AddHours(1), evening).StartsWith("조용히"), "the menu says whether quiet is on");
var withSignals = new WindowPreferences(1, 2, 330, Signals: new SignalSettings(Media: true, TitleExclusions: "a"));
Check(JsonSerializer.Deserialize<WindowPreferences>(JsonSerializer.Serialize(withSignals)) is { Signals: { Media: true, TitleExclusions: "a", Activity: true } }, "signal settings persist");
Check(JsonSerializer.Deserialize<WindowPreferences>("{\"Left\":1,\"Top\":2,\"CharacterHeight\":330}")!.Signals is null, "old preferences have no signals (the defaults are used)");

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

sealed class QuickCompletionHandler(string scenario, Action complete, bool textOnlyFollowUp = false) : HttpMessageHandler
{
    private int detailReads;
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        string value;
        if (request.RequestUri!.AbsolutePath.EndsWith("/events"))
        {
            complete();
            var priorScreen = new { kind = "user_message", attachments = new[] { new { file_id = scenario == "different-screen" ? "f2" : "f1", screen = true, image = new { state = scenario is "no_sandbox" or "view_expired" ? scenario : "ready" } } } };
            object[] history = textOnlyFollowUp ? [priorScreen, new { kind = "user_message", text = "follow-up without screen", attachments = Array.Empty<object>() }] : [priorScreen];
            value = JsonSerializer.Serialize(new { items = history });
        }
        else
        {
            detailReads++;
            value = JsonSerializer.Serialize(new { conversation = new { last_sequence = textOnlyFollowUp ? 2 : 1 }, job = new { job_id = detailReads > 1 && scenario == "different-job" ? "j2" : "j1", running = detailReads == 1 && scenario != "already-completed" } });
        }
        return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(value, Encoding.UTF8, "application/json") });
    }
}

sealed class OriginRecoveryHandler : HttpMessageHandler
{
    public int Reads;
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        Reads++;
        string value = request.RequestUri!.AbsolutePath.EndsWith("/events")
            ? "{\"items\":[{\"sequence\":42,\"kind\":\"user_message\",\"text\":\"hello\",\"origin\":{\"kind\":\"device\",\"device_id\":\"d1\"}}]}"
            : "{\"job\":{\"running\":true,\"job_id\":\"detail-job\"}}";
        return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(value, Encoding.UTF8, "application/json") });
    }
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
