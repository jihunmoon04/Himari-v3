using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media;
using Markdig;
using Markdig.Syntax;
using Markdig.Syntax.Inlines;
using WpfParagraph = System.Windows.Documents.Paragraph;
using Brushes = System.Windows.Media.Brushes;
using FontFamily = System.Windows.Media.FontFamily;
using Color = System.Windows.Media.Color;

namespace Himari.Companion;

// Native, selectable prose in the small bubble. No HTML, image fetches or scripts.
public sealed class MarkdownView : FlowDocumentScrollViewer
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder().UsePipeTables().UseEmphasisExtras().UseMathematics().Build();
    public static readonly DependencyProperty TextProperty = DependencyProperty.Register(nameof(Text), typeof(string), typeof(MarkdownView),
        new FrameworkPropertyMetadata(""));
    private bool renderScheduled;
    public event EventHandler? SourceChanged;
    public string Text { get => (string)GetValue(TextProperty); set => SetValue(TextProperty, value ?? ""); }
    public MarkdownView()
    {
        IsToolBarVisible = false; VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled; Background = Brushes.Transparent;
        BorderThickness = new Thickness(0); Padding = new Thickness(0);
    }
    protected override void OnPropertyChanged(DependencyPropertyChangedEventArgs e)
    {
        base.OnPropertyChanged(e);
        if (e.Property == TextProperty)
        {
            SourceChanged?.Invoke(this, EventArgs.Empty);
            if (!IsLoaded) Render();
            else if (!renderScheduled)
            {
                renderScheduled = true;
                Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.Background, new Action(() => { renderScheduled = false; Render(); }));
            }
        }
    }
    private void Render()
    {
        var scroll = FindScrollViewer(this);
        double offset = scroll?.VerticalOffset ?? 0;
        var doc = new FlowDocument { PagePadding = new Thickness(0), FontFamily = FontFamily, FontSize = FontSize, Foreground = Foreground, ColumnWidth = double.PositiveInfinity };
        foreach (var block in Markdown.Parse(Text, Pipeline)) AddBlock(doc.Blocks, block);
        if (doc.Blocks.LastBlock is { } last) last.Margin = new Thickness(last.Margin.Left, last.Margin.Top, last.Margin.Right, 0);
        Document = doc;
        if (offset > 0) Dispatcher.BeginInvoke(new Action(() => FindScrollViewer(this)?.ScrollToVerticalOffset(offset)));
    }
    private static ScrollViewer? FindScrollViewer(DependencyObject parent)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        { var child = VisualTreeHelper.GetChild(parent, i); if (child is ScrollViewer scroll) return scroll; if (FindScrollViewer(child) is { } nested) return nested; }
        return null;
    }
    private static void AddBlock(BlockCollection target, Markdig.Syntax.Block block)
    {
        if (block is Markdig.Extensions.Mathematics.MathBlock) target.Add(new WpfParagraph(new Run("∑ 수식 · 확장 보기에서 확인")) { Margin = new Thickness(0, 4, 0, 8) });
        else if (block is HeadingBlock heading)
        {
            var p = Paragraph(heading.Inline); p.FontWeight = FontWeights.Medium; p.FontSize = 16; p.LineHeight = 24; target.Add(p);
        }
        else if (block is ParagraphBlock paragraph) target.Add(Paragraph(paragraph.Inline));
        else if (block is CodeBlock code)
        {
            var lines = code.Lines.ToString().Split('\n');
            var p = new WpfParagraph(new Run(string.Join("\n", lines.Take(5)) + (lines.Length > 5 ? "\n… 확장 보기에서 계속" : "")))
            { FontFamily = new FontFamily("Consolas, Malgun Gothic"), FontSize = 12, LineHeight = 19, Background = new SolidColorBrush(Color.FromRgb(233, 226, 245)), Padding = new Thickness(9), Margin = new Thickness(0, 4, 0, 8) };
            target.Add(p);
        }
        else if (block is Markdig.Extensions.Tables.Table) target.Add(new WpfParagraph(new Run("▦ 표 · 확장 보기에서 확인")) { Margin = new Thickness(0, 4, 0, 8), FontStyle = FontStyles.Italic });
        else if (block is ListBlock list)
        {
            var items = new System.Windows.Documents.List { MarkerStyle = list.IsOrdered ? TextMarkerStyle.Decimal : TextMarkerStyle.Disc, Padding = new Thickness(18, 0, 0, 0), Margin = new Thickness(0, 0, 0, 8) };
            if (list.IsOrdered && int.TryParse(list.OrderedStart, out int start)) items.StartIndex = Math.Max(1, start);
            foreach (var child in list.OfType<ListItemBlock>()) { var item = new ListItem(); foreach (var nested in child) AddBlock(item.Blocks, nested); items.ListItems.Add(item); }
            target.Add(items);
        }
        else if (block is QuoteBlock quote)
        {
            var section = new Section { BorderBrush = new SolidColorBrush(Color.FromRgb(118, 99, 133)), BorderThickness = new Thickness(2, 0, 0, 0), Padding = new Thickness(10, 0, 0, 0), Margin = new Thickness(0, 4, 0, 8) };
            foreach (var child in quote) AddBlock(section.Blocks, child); target.Add(section);
        }
        else if (block is ThematicBreakBlock) target.Add(new WpfParagraph(new Run("────────")) { Foreground = Brushes.SlateGray });
        else if (block is LeafBlock leaf) target.Add(new WpfParagraph(new Run(leaf.Lines.ToString())) { Margin = new Thickness(0, 0, 0, 8) });
        else if (block is ContainerBlock container) foreach (var child in container) AddBlock(target, child);
    }
    private static WpfParagraph Paragraph(ContainerInline? inline)
    {
        var p = new WpfParagraph { Margin = new Thickness(0, 0, 0, 8), LineHeight = 22 };
        if (inline is not null) Inlines(p.Inlines, inline); return p;
    }
    private static void Inlines(InlineCollection target, ContainerInline container)
    {
        foreach (var inline in container)
        {
            if (inline is LiteralInline literal) target.Add(new Run(literal.Content.ToString()));
            else if (inline is HtmlEntityInline entity) target.Add(new Run(entity.Transcoded.ToString()));
            else if (inline is Markdig.Extensions.Mathematics.MathInline) target.Add(new Run("∑ 수식 (확장 보기)"));
            else if (inline is AutolinkInline auto)
            {
                string url = auto.IsEmail ? "mailto:" + auto.Url : auto.Url;
                if (SafeLink(url) is Uri uri)
                {
                    var hyperlink = new Hyperlink(new Run(auto.Url)) { NavigateUri = uri };
                    hyperlink.RequestNavigate += (_, e) => { OpenLink(e.Uri.AbsoluteUri); e.Handled = true; }; target.Add(hyperlink);
                }
                else target.Add(new Run(auto.Url));
            }
            else if (inline is CodeInline code) target.Add(new Run(code.Content) { FontFamily = new FontFamily("Consolas"), FontSize = 12, Background = new SolidColorBrush(Color.FromRgb(233, 226, 245)) });
            else if (inline is LineBreakInline) target.Add(new LineBreak());
            else if (inline is EmphasisInline emphasis)
            {
                Span span = emphasis.DelimiterChar == '~' ? new Span { TextDecorations = TextDecorations.Strikethrough } : emphasis.DelimiterCount >= 2 ? new Bold { FontWeight = FontWeights.Medium } : new Italic();
                Inlines(span.Inlines, emphasis); target.Add(span);
            }
            else if (inline is LinkInline link)
            {
                if (link.IsImage) { target.Add(new Run("[이미지: ")); Inlines(target, link); target.Add(new Run("]")); }
                else if (SafeLink(link.Url) is Uri uri)
                {
                    var hyperlink = new Hyperlink { NavigateUri = uri }; Inlines(hyperlink.Inlines, link);
                    hyperlink.RequestNavigate += (_, e) => { OpenLink(e.Uri.AbsoluteUri); e.Handled = true; }; target.Add(hyperlink);
                }
                else Inlines(target, link);
            }
            else if (inline is HtmlInline html) target.Add(new Run(html.Tag));
            else if (inline is ContainerInline nested) Inlines(target, nested);
        }
    }
    internal static Uri? SafeLink(string? value) => Uri.TryCreate(value, UriKind.Absolute, out var uri) && uri.Scheme is "https" or "http" or "mailto" ? uri : null;
    internal static void OpenLink(string value)
    {
        if (SafeLink(value) is not Uri uri) return;
        try { Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception ex) when (ex is System.ComponentModel.Win32Exception or InvalidOperationException) { }
    }
}
