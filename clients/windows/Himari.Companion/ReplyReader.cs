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
    private bool replyExpanded;
    private void ExpandReplyClick(object sender, RoutedEventArgs e)
    {
        replyExpanded = !replyExpanded;
        if (replyExpanded && replyReader is null)
        {
            replyReader = new ReplyReader(Message.Text);
            replyReader.ContentHeightChanged += (_, _) => UpdateSceneBounds();
            ExpandedReply.Content = replyReader;
        }
        Message.Visibility = replyExpanded ? Visibility.Collapsed : Visibility.Visible;
        ExpandedReply.Visibility = replyExpanded ? Visibility.Visible : Visibility.Collapsed;
        ExpandGlyph.Expanded = replyExpanded;
        ExpandButton.ToolTip = replyExpanded ? "말풍선 작게" : "말풍선 크게";
        replyReader?.UpdateText(Message.Text);
        UpdateSceneBounds();
    }
}
internal sealed class ReplyReader : System.Windows.Controls.UserControl, IDisposable
{
    private const string Host = "himari-reader.invalid";
    private readonly WebView2CompositionControl browser = new();
    private string text;
    private bool ready;
    private MarkdownView? fallbackView;
    internal void UpdateText(string value)
    {
        text = value;
        if (ready) browser.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(new { text }));
        if (fallbackView is not null) fallbackView.Text = value;
    }
    internal ReplyReader(string text, string? dataFolder = null)
    {
        this.text = text;
        var root = new DockPanel(); root.Children.Add(browser); Content = root;
        browser.DefaultBackgroundColor = System.Drawing.Color.Transparent;
        Loaded += async (_, _) => { if (initialized) return; initialized = true; await InitializeReader(root, dataFolder); };
    }
    private bool initialized;
    public double ContentHeight { get; private set; } = 120;
    public event EventHandler? ContentHeightChanged;
    public void Dispose() { ready = false; browser.Dispose(); }
    private async Task InitializeReader(DockPanel root, string? dataFolder)
    {
        try
        {
            var data = dataFolder ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HIMARI", "reader");
            var environment = await CoreWebView2Environment.CreateAsync(userDataFolder: data);
            if (!IsLoaded) return;
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
                if (e.Source != $"https://{Host}/index.html") return;
                if (e.WebMessageAsJson == "\"ready\"")
                { ready = true; core.PostWebMessageAsJson(JsonSerializer.Serialize(new { text })); }
                else
                {
                    using var message = JsonDocument.Parse(e.WebMessageAsJson);
                    if (message.RootElement.TryGetProperty("height", out var height) && height.TryGetDouble(out double value) && double.IsFinite(value))
                    { ContentHeight = Math.Clamp(value, 22, 4000); ContentHeightChanged?.Invoke(this, EventArgs.Empty); }
                }
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
