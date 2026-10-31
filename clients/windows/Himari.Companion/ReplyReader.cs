using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace Himari.Companion;

public partial class MainWindow
{
    private ReplyReader? replyReader;
    private void ExpandReplyClick(object sender, RoutedEventArgs e)
    {
        if (replyReader is null)
        {
            replyReader = new ReplyReader(this, Message.Text);
            replyReader.Closed += (_, _) => replyReader = null;
        }
        replyReader.UpdateText(Message.Text); replyReader.Show(); replyReader.Activate();
    }
}

internal sealed class ReplyReader : Window
{
    private const string Host = "himari-reader.invalid";
    private readonly WebView2 browser = new();
    private string text;
    private bool ready;
    private MarkdownView? fallbackView;
    internal void UpdateText(string value)
    {
        text = value;
        if (ready) browser.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new { text }));
        if (fallbackView is not null) fallbackView.Text = value;
    }
    internal ReplyReader(Window owner, string text, string? dataFolder = null)
    {
        this.text = text; Owner = owner; Title = "히마리 · 답변 확장 보기";
        Width = 900; Height = 720; MinWidth = 360; MinHeight = 300; WindowStartupLocation = WindowStartupLocation.CenterScreen;
        var root = new DockPanel(); var copy = new System.Windows.Controls.Button { Content = "원문 복사", Margin = new Thickness(12, 8, 12, 8), HorizontalAlignment = System.Windows.HorizontalAlignment.Right };
        copy.Click += (_, _) => { try { System.Windows.Clipboard.SetText(this.text); } catch (System.Runtime.InteropServices.COMException) { } };
        DockPanel.SetDock(copy, Dock.Top); root.Children.Add(copy); root.Children.Add(browser); Content = root;
        Loaded += async (_, _) => await InitializeReader(root, dataFolder);
        Closed += (_, _) => { ready = false; browser.Dispose(); };
    }
    private async Task InitializeReader(DockPanel root, string? dataFolder)
    {
        try
        {
            var data = dataFolder ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HIMARI", "reader");
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: data);
            if (!IsVisible) return;
            await browser.EnsureCoreWebView2Async(environment);
            var core = browser.CoreWebView2;
            core.Settings.AreDevToolsEnabled = false; core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.AreHostObjectsAllowed = false; core.Settings.IsPasswordAutosaveEnabled = false; core.Settings.IsGeneralAutofillEnabled = false;
            core.PermissionRequested += (_, e) => e.State = CoreWebView2PermissionState.Deny;
            core.DownloadStarting += (_, e) => e.Cancel = true;
            core.SetVirtualHostNameToFolderMapping(Host, Path.Combine(AppContext.BaseDirectory, "Reader"), CoreWebView2HostResourceAccessKind.DenyCors);
            core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
            core.WebResourceRequested += (_, e) =>
            {
                if (!Uri.TryCreate(e.Request.Uri, UriKind.Absolute, out var uri) || uri.Scheme != "https" || uri.Host != Host)
                    e.Response = environment.CreateWebResourceResponse(null, 403, "Blocked", "");
            };
            core.NavigationStarting += (_, e) =>
            {
                if (e.Uri == $"https://{Host}/index.html") return;
                e.Cancel = true; MarkdownView.OpenLink(e.Uri);
            };
            core.NewWindowRequested += (_, e) => { e.Handled = true; MarkdownView.OpenLink(e.Uri); };
            core.WebMessageReceived += (_, e) =>
            {
                if (e.Source == $"https://{Host}/index.html" && e.TryGetWebMessageAsString() == "ready")
                { ready = true; core.PostWebMessageAsJson(JsonSerializer.Serialize(new { text })); }
            };
            browser.Source = new Uri($"https://{Host}/index.html");
        }
        catch (Exception ex) when (ex is System.Runtime.InteropServices.COMException or WebView2RuntimeNotFoundException or InvalidOperationException or IOException)
        {
            root.Children.Remove(browser);
            var fallback = new StackPanel();
            fallback.Children.Add(new TextBlock { Text = "수식과 넓은 표를 표시하려면 Microsoft Edge WebView2 Runtime이 필요합니다.", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(16) });
            fallbackView = new MarkdownView { Text = text, Margin = new Thickness(16) };
            fallback.Children.Add(fallbackView);
            root.Children.Add(new ScrollViewer { Content = fallback, VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        }
    }
}
