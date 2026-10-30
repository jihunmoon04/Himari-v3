using System.Net;
using System.Text;
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
