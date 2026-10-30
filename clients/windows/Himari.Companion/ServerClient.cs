using System.Net;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;

namespace Himari.Companion;

internal sealed class ApiException(HttpStatusCode status, string code) : Exception(code)
{
    internal HttpStatusCode Status { get; } = status;
    internal string Code { get; } = code;
}
internal sealed class ServerClient : IDisposable
{
    private readonly HttpClient http;
    private readonly string token;
    internal Uri BaseUri { get; }
    internal bool Matches(DeviceCredential credential) => credential.Server == BaseUri.ToString() && credential.Token == token;
    internal ServerClient(string address, string token) : this(address, token, new HttpClientHandler { UseCookies = false, AllowAutoRedirect = false }) { }
    internal ServerClient(string address, string token, HttpMessageHandler handler)
    {
        if (!Uri.TryCreate(address, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps || !string.IsNullOrEmpty(uri.UserInfo) || uri.AbsolutePath != "/" || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
            throw new ArgumentException("서버 주소는 경로가 없는 HTTPS 주소여야 합니다.");
        BaseUri = uri; this.token = token;
        http = new HttpClient(handler) { BaseAddress = uri, Timeout = Timeout.InfiniteTimeSpan };
    }
    internal async Task<JsonElement> PairAsync(string code, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/devices/pair");
        request.Headers.Authorization = new AuthenticationHeaderValue("Pairing", code.Replace("-", "").Replace(" ", ""));
        request.Content = JsonContent.Create(new { token });
        using var response = await http.SendAsync(request, ct);
        return await ReadAsync(response, ct);
    }
    private HttpRequestMessage Request(HttpMethod method, string path, object? body = null)
    {
        var request = new HttpRequestMessage(method, path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        if (body != null) request.Content = JsonContent.Create(body);
        return request;
    }
    internal async Task<JsonElement> SendAsync(HttpMethod method, string path, object? body, CancellationToken ct)
    {
        using var request = Request(method, path, body);
        using var response = await http.SendAsync(request, ct);
        return await ReadAsync(response, ct);
    }
    private static async Task<JsonElement> ReadAsync(HttpResponseMessage response, CancellationToken ct)
    {
        if (response.StatusCode == HttpStatusCode.NoContent) return default;
        var body = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
        {
            string code = "http_error";
            try { using var doc = JsonDocument.Parse(body); code = doc.RootElement.GetProperty("error").GetProperty("code").GetString() ?? code; } catch (JsonException) { } catch (KeyNotFoundException) { }
            throw new ApiException(response.StatusCode, code);
        }
        using var parsed = JsonDocument.Parse(body); return parsed.RootElement.Clone();
    }
    internal async IAsyncEnumerable<(string Event, JsonElement Data)> StreamAsync(string path, [EnumeratorCancellation] CancellationToken ct)
    {
        using var request = Request(HttpMethod.Get, path);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("text/event-stream"));
        using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
        if (!response.IsSuccessStatusCode) { await ReadAsync(response, ct); yield break; }
        using var reader = new StreamReader(await response.Content.ReadAsStreamAsync(ct), Encoding.UTF8);
        string type = "message"; var data = new StringBuilder();
        while (await reader.ReadLineAsync(ct) is { } line)
        {
            if (line.Length == 0)
            {
                if (data.Length > 0) { using var json = JsonDocument.Parse(data.ToString()); yield return (type, json.RootElement.Clone()); }
                type = "message"; data.Clear(); continue;
            }
            if (line.StartsWith(':')) continue;
            int colon = line.IndexOf(':'); string field = colon < 0 ? line : line[..colon];
            string value = colon < 0 ? "" : line[(colon + 1)..]; if (value.StartsWith(' ')) value = value[1..];
            if (field == "event") type = value;
            else if (field == "data") { if (data.Length > 0) data.Append('\n'); data.Append(value); }
        }
    }
    public void Dispose() => http.Dispose();
}
