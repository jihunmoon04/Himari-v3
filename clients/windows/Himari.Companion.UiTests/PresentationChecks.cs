using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Himari.Companion;
using Microsoft.Web.WebView2.Wpf;
using System.Runtime.InteropServices;

internal static class PresentationChecks
{
    private const BindingFlags Private = BindingFlags.NonPublic | BindingFlags.Instance;
    [DllImport("user32.dll")] private static extern bool PostMessage(IntPtr window, int message, IntPtr wParam, IntPtr lParam);
    private static object? Call(MainWindow window, string method, params object?[] values) => typeof(MainWindow).GetMethod(method, Private)!.Invoke(window, values);
    private static void Pump()
    {
        var frame = new DispatcherFrame(); Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, new Action(() => frame.Continue = false)); Dispatcher.PushFrame(frame);
    }
    private static void Until(Func<bool> ready)
    {
        var frame = new DispatcherFrame(); var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(20) }; var deadline = DateTime.UtcNow.AddSeconds(20);
        timer.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) frame.Continue = false; };
        if (!ready()) { timer.Start(); Dispatcher.PushFrame(frame); timer.Stop(); }
        if (!ready()) throw new Exception("presentation operation timed out");
    }
    internal static void Run(Action<bool, string> check, string? screenshots)
    {
        var markdown = new MarkdownView { Text = "## 제목\n\n**굵게**와 *기울임*, `코드`\n\n- 첫째\n- 둘째\n\n[링크](https://example.com)\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n<script>alert(1)</script>" };
        string visible = new TextRange(markdown.Document.ContentStart, markdown.Document.ContentEnd).Text;
        check(visible.Contains("제목") && !visible.Contains("##") && !visible.Contains("**") && visible.Contains("표 · 확장 보기"), "native bubble renders headings/emphasis/lists and directs wide tables to reader");
        check(markdown.Document.Blocks.OfType<Paragraph>().SelectMany(p => p.Inlines.Cast<Inline>()).Any(i => i is Bold), "strong emphasis has actual WPF formatting");
        check(MarkdownView.SafeLink("javascript:alert(1)") is null && MarkdownView.SafeLink("file:///C:/private") is null && MarkdownView.SafeLink("https://example.com") is not null, "reader links permit only explicit web/mail schemes");
        using var source = new HwndSource(new HwndSourceParameters("HIMARI type-to-chat check") { Width = 1, Height = 1, WindowStyle = unchecked((int)0x80000000) });
        var w = new MainWindow(true) { Left = SystemParameters.WorkArea.Left + 60, Top = SystemParameters.WorkArea.Top + 20 };
        w.Show(); Pump();
        var host = (Window)typeof(MainWindow).GetField("dialogueHost", Private)!.GetValue(w)!;
        var settings = (Window)typeof(MainWindow).GetField("settings", Private)!.GetValue(w)!;
        var mode = (ComboBox)settings.FindName("BubblePlacement");
        var input = (TextBox)w.FindName("Input"); var user = (PixelBubble)w.FindName("UserBubble"); var answer = (PixelBubble)w.FindName("BubblePanel");
        var message = (MarkdownView)w.FindName("Message");
        double left = w.Left, top = w.Top;
        Call(w, "OpenChat"); Pump();
        check(host.IsVisible && input.IsKeyboardFocused, "opening real detached composer focuses its editor");
        check(user.ActualHeight <= 60 && input.ActualHeight <= 26, "empty composer fits one compact line including send button");
        var send = (FrameworkElement)w.FindName("SendButton");
        check(Math.Abs(input.TranslatePoint(new Point(0, 0), user).Y - send.TranslatePoint(new Point(0, 0), user).Y) < 4, "send action and empty editor share the same row");
        check(w.Left == left && w.Top == top, "opening a bubble never moves the character window");
        check(answer.TailTop && user.TailTop && host.Top >= w.Top + w.Height - 1, "automatic mode puts both bubbles below a character in the top half");
        Call(w, "OpenReplyBubble", "c", "short"); message.Text = "한 줄 답변"; Pump();
        check(answer.ActualHeight <= 70, "short answer occupies one row with small expansion icon");
        Call(w, "CloseBubble");
        input.Text = "남길 초안"; input.Select(2, 2);
        foreach (string value in new[] { "상 고정", "하 고정", "상하 자동" })
        {
            mode.SelectedItem = value; Pump();
            check(input.Text == "남길 초안" && input.SelectedText == " 초" && input.IsKeyboardFocused, "placement changes preserve draft, selection and editor focus");
            check(user.TailTop == (value != "상 고정"), "fixed placement updates frame tail without flipping text");
            check(w.Left == left && w.Top == top, "changing bubble side keeps character anchored");
        }
        var area = SystemParameters.WorkArea;
        mode.SelectedItem = "하 고정"; Call(w, "OpenReplyBubble", "c", "vertical-order"); Call(w, "OpenComposer"); Pump();
        check(Grid.GetRow(user) < Grid.GetRow(answer), "below mode mirrors stack order so new draft stays closest to character");
        Call(w, "AssistantBubbleExpired"); Pump();
        check(answer.Visibility == Visibility.Collapsed && user.Visibility == Visibility.Visible, "below mode expires oldest reply first even when it occupies bottom row");
        mode.SelectedItem = "상하 자동";
        foreach (var location in new[] { new Point(area.Left, area.Top), new Point(area.Right - w.Width, area.Top), new Point(area.Left, area.Bottom - w.Height), new Point(area.Right - w.Width, area.Bottom - w.Height) })
        {
            w.Left = location.X; w.Top = location.Y; Pump(); Call(w, "OpenChat"); Pump();
            check(Math.Abs(w.Left - location.X) < 1 && Math.Abs(w.Top - location.Y) < 1, "all four character corners remain stable when chatting");
            check(host.Left >= area.Left - 1 && host.Top >= area.Top - 1 && host.Left + host.Width <= area.Right + 1 && host.Top + host.Height <= area.Bottom + 1, "detached bubble remains inside monitor work area at all corners");
        }
        input.Clear(); Call(w, "ShowSentMessage", "질문"); Call(w, "OpenReplyBubble", "c", "j"); message.Text = "**답변**이 끝났어요."; Pump();
        w.Activate(); Keyboard.Focus(w); Pump();
        var composition = new TextComposition(InputManager.Current, w, "가");
        w.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, composition) { RoutedEvent = TextCompositionManager.PreviewTextInputEvent }); Pump();
        check(input.Text == "가" && input.IsKeyboardFocused && user.Visibility == Visibility.Visible, "first character after a completed reply opens composer and is retained exactly once");
        input.Clear(); Call(w, "ShowSentMessage", "질문"); w.Activate(); Keyboard.Focus(w); Pump();
        w.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, source, Environment.TickCount, Key.A) { RoutedEvent = Keyboard.PreviewKeyDownEvent }); Pump();
        check(input.IsKeyboardFocused && ((FrameworkElement)w.FindName("ComposerPanel")).Visibility == Visibility.Visible, "printable key focuses editor before text/IME composition arrives");
        input.Text = "IME 초안"; input.Select(0, 3);
        var ime = new TextComposition(InputManager.Current, input, "가", TextCompositionAutoComplete.Off);
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, ime) { RoutedEvent = TextCompositionManager.PreviewTextInputStartEvent });
        mode.SelectedItem = "상 고정"; Pump();
        var enter = new KeyEventArgs(Keyboard.PrimaryDevice, source, Environment.TickCount, Key.Enter) { RoutedEvent = Keyboard.PreviewKeyDownEvent }; input.RaiseEvent(enter);
        check(!enter.Handled && input.SelectedText == "IME", "moving the detached bubble retains active IME composition and selection");
        message.Text = "## 오늘 할 일\n\n**선생님**, 바로 이어서 이야기할 수 있어요.\n\n- 입력창 자동 열기\n- 말풍선 위치 선택\n\n| 기능 | 상태 |\n|---|---|\n| 대화 | 준비 |";
        if (screenshots is not null)
        {
            System.IO.Directory.CreateDirectory(screenshots);
            w.Left = area.Left + 160; w.Top = area.Top + 80; mode.SelectedItem = "하 고정"; Pump(); Save(host, System.IO.Path.Combine(screenshots, "04-detached-below.png"));
            w.Top = area.Bottom - w.Height - 60; mode.SelectedItem = "상 고정"; Pump(); Save(host, System.IO.Path.Combine(screenshots, "05-detached-above.png"));
        }
        w.Hide(); Pump(); check(!host.IsVisible, "hiding character also hides detached bubbles");
        w.Show(); Pump(); check(host.IsVisible && input.Text == "IME 초안", "show restores bubble without losing draft");
        var reader = new ReplyReader("# 확장 보기\n\n| 기능 | 상태 |\n|---|---|\n| 수식 | 준비 |\n\n$$x^2+\\frac{1}{2}$$\n\n```csharp\nvar text = \"한 줄의 긴 코드도 가로 스크롤로 읽습니다.\";\n```\n\n<script>window.pwned=true</script>\n\n![private](https://example.com/private.png)", System.IO.Path.Combine(System.IO.Path.GetTempPath(), "himari-reader-test-" + Guid.NewGuid().ToString("N")));
        ((ContentControl)w.FindName("ExpandedReply")).Content = reader;
        ((ContentControl)w.FindName("ExpandedReply")).Visibility = Visibility.Visible;
        reader.Height = 300;
        var browser = (WebView2CompositionControl)typeof(ReplyReader).GetField("browser", Private)!.GetValue(reader)!;
        Until(() => (bool)typeof(ReplyReader).GetField("ready", Private)!.GetValue(reader)!);
        string Eval(string js) { var task = browser.ExecuteScriptAsync(js); Until(() => task.IsCompleted); return task.GetAwaiter().GetResult(); }
        Until(() => Eval("document.querySelectorAll('table').length") == "1");
        Until(() => Eval("document.querySelectorAll('.katex').length") == "1");
        check(Eval("document.querySelectorAll('#answer script,#answer img').length") == "0" && Eval("!!window.pwned") == "false", "expanded reader renders tables/math but never creates model scripts or remote images");
        check(Eval("getComputedStyle(document.querySelector('pre')).overflowX") == "\"auto\"", "long code has horizontal scrolling in expanded reader");
        reader.UpdateText("**새 답변**"); Until(() => Eval("document.getElementById('answer').textContent") == "\"새 답변\"");
        check(Eval("document.querySelectorAll('table').length") == "0", "reopening reader with a new answer replaces old content");
        reader.Dispose();
        ((ContentControl)w.FindName("ExpandedReply")).Content = null;
        ((ContentControl)w.FindName("ExpandedReply")).Visibility = Visibility.Collapsed;
        int windowCount = System.Windows.Application.Current.Windows.Count;
        double compactWidth = answer.Width;
        double anchoredLeft = w.Left, anchoredTop = w.Top;
        Call(w, "ExpandReplyClick", w, new RoutedEventArgs()); Pump();
        var inlineReader = (ReplyReader)typeof(MainWindow).GetField("replyReader", Private)!.GetValue(w)!;
        Until(() => (bool)typeof(ReplyReader).GetField("ready", Private)!.GetValue(inlineReader)!);
        var inlineBrowser = (WebView2CompositionControl)typeof(ReplyReader).GetField("browser", Private)!.GetValue(inlineReader)!;
        var rendered = inlineBrowser.ExecuteScriptAsync("document.getElementById('answer').textContent"); Until(() => rendered.IsCompleted);
        Pump();
        check(System.Windows.Application.Current.Windows.Count == windowCount && answer.Width > compactWidth && Window.GetWindow(inlineReader) == host, "expansion enlarges existing pixel bubble without creating another window");
        check(w.Left == anchoredLeft && w.Top == anchoredTop, "expanding content keeps character anchored");
        if (screenshots is not null) Save(host, System.IO.Path.Combine(screenshots, "06-expanded-inline.png"));
        Call(w, "ExpandReplyClick", w, new RoutedEventArgs()); Pump();
        check(answer.Width == compactWidth && input.Text == "IME 초안", "collapsing restores compact bubble and preserves draft");
        ((ComboBox)settings.FindName("ChatHotkey")).SelectedItem = "Alt+Shift+Q";
        Call(w, "SetupChatHotkey");
        check((bool)typeof(MainWindow).GetProperty("hotKeyRegistered", Private)!.GetValue(w)!, "OS accepted the real global hotkey registration");
        var hotkeySource = (HwndSource)typeof(MainWindow).GetField("hotKeySource", Private)!.GetValue(w)!;
        var register = typeof(MainWindow).GetMethod("RegisterHotKey", BindingFlags.Static | BindingFlags.NonPublic)!;
        bool Available() => (bool)register.Invoke(null, new object[] { IntPtr.Zero, 0x7123, 0x4005u, 0x51u })!;
        check(!Available(), "native global shortcut is reserved by dedicated message window");
        Call(w, "CloseBubble"); Call(w, "HideUserBubble"); w.Hide(); w.Show(); Pump();
        check(!Available(), "hiding and showing presentation windows keeps native hotkey registered");
        check(PostMessage(hotkeySource.Handle, 0x0312, new IntPtr(0x4849), IntPtr.Zero), "native shortcut message can be posted to stable receiver");
        Pump();
        // If the app was already in front the first press switches away; the second reopens it.
        if (!input.IsKeyboardFocused) { PostMessage(hotkeySource.Handle, 0x0312, new IntPtr(0x4849), IntPtr.Zero); Pump(); }
        check(host.IsVisible && ((FrameworkElement)w.FindName("ComposerPanel")).Visibility == Visibility.Visible && input.IsKeyboardFocused, "actual WM_HOTKEY routing reopens detached composer and focuses input");
        Console.WriteLine("WebView2 expanded reader: tables, KaTeX, scrollable code and hostile-content checks passed.");
        w.Activate(); Call(w, "CloseChat"); Pump();
        var foreground = (IntPtr)typeof(MainWindow).GetMethod("GetForegroundWindow", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, null)!;
        check(!(bool)Call(w, "IsChatWindow", foreground)! && !input.IsKeyboardFocused, "shortcut with no previous work window releases foreground and keyboard input");
        w.Close(); Pump();
        Console.WriteLine("Detached windows, four screen corners, vertical modes, type-to-chat and native Markdown checks passed.");
    }
    private static void Save(Window window, string path)
    {
        window.UpdateLayout(); var element = (FrameworkElement)window.Content;
        var bitmap = new RenderTargetBitmap((int)Math.Ceiling(window.ActualWidth), (int)Math.Ceiling(window.ActualHeight), 96, 96, PixelFormats.Pbgra32); bitmap.Render(element);
        var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(bitmap)); using var file = System.IO.File.Create(path); png.Save(file);
    }
}
