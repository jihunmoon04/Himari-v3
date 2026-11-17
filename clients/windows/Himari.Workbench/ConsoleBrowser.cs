using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Himari.Workbench;

/// <summary>
/// The work console in WebView2 (§4): the server's origin only, no new windows, external <c>https</c> only from a
/// person's action, no permissions, no certificate bypass, no developer tools outside developer mode, and no bridge
/// from the page to the app (no host objects, no web messages).
/// </summary>
public partial class MainWindow
{
    private const string RuntimeGuide = "https://developer.microsoft.com/microsoft-edge/webview2/";
    private readonly ExternalOpenThrottle externalThrottle = new(() => Environment.TickCount64);
    private WebView2? browser;
    private CoreWebView2Environment? environment;
    private Uri? server;
    private bool runtimeMissing;

    private static string DataFolder => Path.Combine(SettingsStore.DefaultDirectory, "workbench", "webview");

    private async Task InitializeBrowserAsync()
    {
        try { CoreWebView2Environment.GetAvailableBrowserVersionString(); }
        catch (WebView2RuntimeNotFoundException) { ShowRuntimeMissing(); return; }
        await ReloadBrowserAsync();
    }

    /// <summary>(Re)opens the console at the configured server address.</summary>
    private async Task ReloadBrowserAsync()
    {
        if (runtimeMissing) return;
        WorkbenchSettings settings = store.Current;
        if (!OriginPolicy.TryServerUri(settings.Server, settings.DeveloperMode, out var address, out string error))
        {
            ShowFallback("서버 주소를 고쳐 주세요", error, "설정 열기", () => SettingsClick(this, new RoutedEventArgs()));
            return;
        }
        server = address;
        Fallback.Visibility = Visibility.Collapsed;
        try
        {
            if (browser is null)
            {
                environment ??= await CoreWebView2Environment.CreateAsync(null, DataFolder);
                browser = new WebView2 { DefaultBackgroundColor = System.Drawing.Color.FromArgb(0xF7, 0xF5, 0xFA) };
                BrowserHost.Children.Insert(0, browser);
                await browser.EnsureCoreWebView2Async(environment);
                Configure(browser.CoreWebView2);
            }
            browser.Visibility = Visibility.Visible;
            browser.CoreWebView2.Settings.AreDevToolsEnabled = settings.DeveloperMode;
            browser.CoreWebView2.Navigate(server.AbsoluteUri);
        }
        catch (WebView2RuntimeNotFoundException) { ShowRuntimeMissing(); }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or InvalidOperationException or IOException or UnauthorizedAccessException)
        {
            // §5.1 keeps the log to exit codes and masked lines: the kind of failure only, never its message.
            log.Write($"webview failed: {ex.GetType().Name}");
            ShowFallback("작업 콘솔을 열지 못했습니다", ex.Message, "다시 시도", () => _ = ReloadBrowserAsync());
        }
    }

    private void Configure(CoreWebView2 core)
    {
        var options = core.Settings;
        options.AreHostObjectsAllowed = false;
        options.IsWebMessageEnabled = false;
        options.AreDevToolsEnabled = store.Current.DeveloperMode;
        options.AreDefaultContextMenusEnabled = true;
        options.IsPasswordAutosaveEnabled = false;
        options.IsGeneralAutofillEnabled = false;
        core.ContextMenuRequested += (_, e) =>
        {
            if (store.Current.DeveloperMode) return;
            foreach (var item in e.MenuItems.Where(item => item.Name is "inspectElement" or "openDevTools").ToList()) e.MenuItems.Remove(item);
        };
        core.PermissionRequested += (_, e) => { e.State = CoreWebView2PermissionState.Deny; e.Handled = true; };
        core.ServerCertificateErrorDetected += (_, e) => e.Action = CoreWebView2ServerCertificateErrorAction.Cancel;
        core.NavigationStarting += (_, e) =>
        {
            if (server is null) { e.Cancel = true; return; }
            switch (OriginPolicy.TopLevel(server, e.Uri, e.IsUserInitiated, e.IsRedirected))
            {
                case NavigationDecision.Allow: return;
                case NavigationDecision.OpenExternal: e.Cancel = true; OpenExternal(e.Uri); return;
                default: e.Cancel = true; ShowBlocked(e.Uri); return;
            }
        };
        core.FrameNavigationStarting += (_, e) =>
        {
            if (server is null || OriginPolicy.Frame(server, e.Uri) != NavigationDecision.Allow) { e.Cancel = true; ShowBlocked(e.Uri); }
        };
        core.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            if (server is null) return;
            switch (OriginPolicy.NewWindow(server, e.Uri, e.IsUserInitiated))
            {
                case NewWindowDecision.NavigateHere: core.Navigate(e.Uri); break;
                case NewWindowDecision.OpenExternal: OpenExternal(e.Uri); break;
                default: ShowBlocked(e.Uri); break;
            }
        };
        core.ProcessFailed += (_, e) =>
        {
            log.Write($"webview process failed: {e.ProcessFailedKind}");
            if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
                Dispatcher.BeginInvoke(() => { BrowserHost.Children.Remove(browser); browser?.Dispose(); browser = null; environment = null; _ = ReloadBrowserAsync(); });
            else ShowNotice("작업 콘솔 화면이 멈췄습니다. 다시 엽니다");
        };
    }

    /// <summary>The default browser, for an absolute <c>https</c> URI re-parsed here, at most once every 2 s.</summary>
    private void OpenExternal(string? uri)
    {
        if (OriginPolicy.ExternalHttps(uri) is not { } target) { ShowBlocked(uri); return; }
        if (!externalThrottle.TryAcquire()) { ShowNotice("링크를 너무 자주 열려고 해서 막았습니다"); return; }
        try { Process.Start(new ProcessStartInfo(target.AbsoluteUri) { UseShellExecute = true })?.Dispose(); }
        catch (Win32Exception) { ShowNotice("기본 브라우저를 열지 못했습니다"); }
    }

    private void ShowBlocked(string? uri) => ShowNotice($"열지 않음: {OriginPolicy.SchemeOf(uri)}");

    private void ShowRuntimeMissing()
    {
        runtimeMissing = true;
        ShowFallback("WebView2 Runtime이 필요합니다",
            "작업 콘솔 창을 보이려면 Microsoft Edge WebView2 Runtime을 설치하세요. 실행기 켜기·끄기(위 상태 줄과 트레이)는 그대로 쓸 수 있습니다.",
            "설치 안내 열기", () => OpenExternal(RuntimeGuide));
    }

    private Action? fallbackAction;

    private void ShowFallback(string title, string detail, string button, Action action)
    {
        if (browser is not null) browser.Visibility = Visibility.Collapsed;
        FallbackTitle.Text = title;
        FallbackDetail.Text = detail;
        FallbackButton.Content = button;
        fallbackAction = action;
        Fallback.Visibility = Visibility.Visible;
    }

    private void FallbackClick(object sender, RoutedEventArgs e) => fallbackAction?.Invoke();
}
