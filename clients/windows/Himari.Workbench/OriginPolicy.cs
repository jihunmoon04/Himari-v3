using System.Net;

namespace Himari.Workbench;

public enum NavigationDecision { Allow, Block, OpenExternal }

public enum NewWindowDecision { NavigateHere, OpenExternal, Block }

/// <summary>
/// Which navigations the work console window allows (§4). Origins are compared from parsed URIs: scheme, host (lower
/// case, IDN as punycode) and port (default filled in), never by string prefix.
/// </summary>
public static class OriginPolicy
{
    /// <summary>
    /// A server address the app will open: absolute <c>https</c>, or loopback <c>http</c> in developer mode only.
    /// No user info, query or fragment.
    /// </summary>
    public static bool TryServerUri(string? text, bool developerMode, out Uri uri, out string error)
    {
        uri = null!;
        error = "";
        if (!Uri.TryCreate(text?.Trim(), UriKind.Absolute, out var parsed) || string.IsNullOrEmpty(parsed.Host))
        { error = "주소를 읽을 수 없습니다. https://로 시작하는 주소를 넣으세요"; return false; }
        if (parsed.UserInfo.Length > 0) { error = "주소에 사용자 이름이나 비밀번호를 넣지 마세요"; return false; }
        if (parsed.Scheme == Uri.UriSchemeHttp)
        {
            if (!IsLoopback(parsed)) { error = "https 주소만 쓸 수 있습니다"; return false; }
            if (!developerMode) { error = "http://127.0.0.1·localhost는 개발자 모드에서만 쓸 수 있습니다"; return false; }
        }
        else if (parsed.Scheme != Uri.UriSchemeHttps) { error = "https 주소만 쓸 수 있습니다"; return false; }
        uri = new UriBuilder(parsed) { Query = "", Fragment = "" }.Uri;
        return true;
    }

    public static bool IsLoopback(Uri uri) =>
        uri.IsLoopback || string.Equals(uri.IdnHost, "localhost", StringComparison.OrdinalIgnoreCase)
        || IPAddress.TryParse(uri.IdnHost.Trim('[', ']'), out var address) && IPAddress.IsLoopback(address);

    public static bool SameOrigin(Uri server, string? candidate) =>
        Uri.TryCreate(candidate, UriKind.Absolute, out var uri) && SameOrigin(server, uri);

    public static bool SameOrigin(Uri server, Uri uri) =>
        uri.IsAbsoluteUri && !string.IsNullOrEmpty(uri.Host)
        && string.Equals(server.Scheme, uri.Scheme, StringComparison.OrdinalIgnoreCase)
        && string.Equals(server.IdnHost, uri.IdnHost, StringComparison.OrdinalIgnoreCase)
        && server.Port == uri.Port;

    /// <summary>
    /// Top-level navigation, redirects included: the server's origin only. A cancelled cross-origin <c>https</c> link the
    /// person clicked goes to the default browser; a redirect never does.
    /// </summary>
    public static NavigationDecision TopLevel(Uri server, string? uri, bool userInitiated, bool redirected)
    {
        if (SameOrigin(server, uri)) return NavigationDecision.Allow;
        return userInitiated && !redirected && ExternalHttps(uri) is not null ? NavigationDecision.OpenExternal : NavigationDecision.Block;
    }

    /// <summary>Frames: the server's origin only (§4), with no exception for <c>about:</c> documents (the console has no frames).</summary>
    public static NavigationDecision Frame(Uri server, string? uri) =>
        SameOrigin(server, uri) ? NavigationDecision.Allow : NavigationDecision.Block;

    /// <summary>
    /// <c>target=_blank</c> and <c>window.open</c>: never a new window in the app. Same origin opens here; a cross-origin
    /// <c>https</c> request from a person's action goes to the default browser; anything else is blocked.
    /// </summary>
    public static NewWindowDecision NewWindow(Uri server, string? uri, bool userInitiated)
    {
        if (SameOrigin(server, uri)) return NewWindowDecision.NavigateHere;
        return userInitiated && ExternalHttps(uri) is not null ? NewWindowDecision.OpenExternal : NewWindowDecision.Block;
    }

    /// <summary>The URI re-parsed as an absolute <c>https</c> URI with a host, or null.</summary>
    public static Uri? ExternalHttps(string? uri) =>
        Uri.TryCreate(uri, UriKind.Absolute, out var parsed) && parsed.Scheme == Uri.UriSchemeHttps && !string.IsNullOrEmpty(parsed.Host)
        && parsed.UserInfo.Length == 0 ? parsed : null;

    /// <summary>The scheme to name in "열지 않음: …".</summary>
    public static string SchemeOf(string? uri)
    {
        if (Uri.TryCreate(uri, UriKind.Absolute, out var parsed)) return parsed.Scheme;
        int colon = uri?.IndexOf(':') ?? -1;
        return colon > 0 && colon <= 32 ? uri![..colon].ToLowerInvariant() : "알 수 없는 주소";
    }
}

/// <summary>At most one hand-off to the default browser every two seconds (§4: repeated pop-ups).</summary>
public sealed class ExternalOpenThrottle(Func<long> uptimeMs)
{
    public static readonly TimeSpan Interval = TimeSpan.FromSeconds(2);
    private long last = long.MinValue;

    public bool TryAcquire()
    {
        long now = uptimeMs();
        if (last != long.MinValue && now - last < (long)Interval.TotalMilliseconds) return false;
        last = now;
        return true;
    }
}
