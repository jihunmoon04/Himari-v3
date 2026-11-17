using Himari.Workbench;

namespace Himari.Workbench.Tests;

public sealed class OriginPolicyTests
{
    private static readonly Uri Server = new("https://himari.example/console");

    [Theory]
    [InlineData("https://himari.example/app", true)]
    [InlineData("https://HIMARI.Example:443/x?y=1", true)]
    [InlineData("https://himari.example:8443/", false)]
    [InlineData("http://himari.example/", false)]
    [InlineData("https://himari.example.evil.test/", false)]
    [InlineData("https://evil.test/https://himari.example/", false)]
    [InlineData("https://himari.example@evil.test/", false)]
    [InlineData("file:///C:/Windows/win.ini", false)]
    [InlineData("not a uri", false)]
    public void SameOriginComparesParsedParts(string candidate, bool expected) => Assert.Equal(expected, OriginPolicy.SameOrigin(Server, candidate));

    [Fact]
    public void IdnHostsCompareAsPunycode()
    {
        var idn = new Uri("https://히마리.example/");
        string ascii = new System.Globalization.IdnMapping().GetAscii("히마리.example");
        Assert.StartsWith("xn--", ascii);
        Assert.True(OriginPolicy.SameOrigin(idn, $"https://{ascii}/x"));
        Assert.True(OriginPolicy.SameOrigin(idn, "https://히마리.EXAMPLE/x"));
    }

    [Fact]
    public void TopLevelNavigation()
    {
        Assert.Equal(NavigationDecision.Allow, OriginPolicy.TopLevel(Server, "https://himari.example/x", false, true));
        Assert.Equal(NavigationDecision.OpenExternal, OriginPolicy.TopLevel(Server, "https://docs.example/", true, false));
        Assert.Equal(NavigationDecision.Block, OriginPolicy.TopLevel(Server, "https://docs.example/", false, false));
        Assert.Equal(NavigationDecision.Block, OriginPolicy.TopLevel(Server, "https://docs.example/", true, true));
        Assert.Equal(NavigationDecision.Block, OriginPolicy.TopLevel(Server, "http://docs.example/", true, false));
        Assert.Equal(NavigationDecision.Block, OriginPolicy.TopLevel(Server, "ms-settings:privacy", true, false));
    }

    [Fact]
    public void Frames()
    {
        Assert.Equal(NavigationDecision.Allow, OriginPolicy.Frame(Server, "https://himari.example/frame"));
        Assert.Equal(NavigationDecision.Allow, OriginPolicy.Frame(Server, "about:blank"));
        Assert.Equal(NavigationDecision.Block, OriginPolicy.Frame(Server, "https://ads.example/frame"));
        Assert.Equal(NavigationDecision.Block, OriginPolicy.Frame(Server, "data:text/html,hi"));
    }

    [Fact]
    public void NewWindows()
    {
        Assert.Equal(NewWindowDecision.NavigateHere, OriginPolicy.NewWindow(Server, "https://himari.example/other", false));
        Assert.Equal(NewWindowDecision.OpenExternal, OriginPolicy.NewWindow(Server, "https://docs.example/", true));
        Assert.Equal(NewWindowDecision.Block, OriginPolicy.NewWindow(Server, "https://docs.example/", false));
        Assert.Equal(NewWindowDecision.Block, OriginPolicy.NewWindow(Server, "http://docs.example/", true));
        Assert.Equal(NewWindowDecision.Block, OriginPolicy.NewWindow(Server, "file:///C:/x", true));
        Assert.Equal(NewWindowDecision.Block, OriginPolicy.NewWindow(Server, "himari-custom:run", true));
        Assert.Equal(NewWindowDecision.Block, OriginPolicy.NewWindow(Server, "https://user:pw@docs.example/", true));
    }

    [Theory]
    [InlineData("https://himari.example", false, true)]
    [InlineData("https://himari.example/path?q=1#f", false, true)]
    [InlineData("http://himari.example", true, false)]
    [InlineData("http://127.0.0.1:8000", false, false)]
    [InlineData("http://127.0.0.1:8000", true, true)]
    [InlineData("http://localhost:8000", true, true)]
    [InlineData("http://[::1]:8000", true, true)]
    [InlineData("ftp://himari.example", true, false)]
    [InlineData("https://u:p@himari.example", false, false)]
    [InlineData("himari.example", false, false)]
    public void ServerAddresses(string text, bool developerMode, bool ok)
    {
        Assert.Equal(ok, OriginPolicy.TryServerUri(text, developerMode, out var uri, out string error));
        if (ok) { Assert.Empty(uri.Query); Assert.Empty(uri.Fragment); }
        else Assert.NotEmpty(error);
    }

    [Fact]
    public void SchemeNames()
    {
        Assert.Equal("file", OriginPolicy.SchemeOf("file:///C:/x"));
        Assert.Equal("ms-settings", OriginPolicy.SchemeOf("ms-settings:privacy"));
        Assert.Equal("알 수 없는 주소", OriginPolicy.SchemeOf("???"));
    }

    [Fact]
    public void ExternalOpensAreThrottledToOnePerTwoSeconds()
    {
        long now = 0;
        var throttle = new ExternalOpenThrottle(() => now);
        Assert.True(throttle.TryAcquire());
        now = 1999;
        Assert.False(throttle.TryAcquire());
        now = 2000;
        Assert.True(throttle.TryAcquire());
    }
}
