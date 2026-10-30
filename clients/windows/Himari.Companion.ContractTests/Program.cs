using System.Net;
using System.Text;
using System.Text.Json;
using Himari.Companion;

static void Check(bool value, string message) { if (!value) throw new Exception(message); }
var token = CredentialStore.NewToken();
Check(token.StartsWith("hd_") && token.Length == 46, "token format");
Check(token != CredentialStore.NewToken(), "tokens must not repeat");
try { using var invalid = new ServerClient("http://example.com", token); throw new Exception("HTTP accepted"); } catch (ArgumentException) { }
try { using var invalid = new ServerClient("https://example.com/path", token); throw new Exception("path accepted"); } catch (ArgumentException) { }
using var client = new ServerClient("https://example.com/", token, new FakeHandler(token));
var paired = await client.PairAsync("ABCD-EFGH", CancellationToken.None);
Check(paired.GetProperty("device_id").GetString() == "d1", "pair response");
await client.SendAsync(HttpMethod.Post, "/api/device/presence", new { idle_seconds = 12, time_zone = "Asia/Seoul" }, CancellationToken.None);
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
Console.WriteLine("Contract transport checks passed.");

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
