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
        check(answer.ActualHeight <= 100, "short answer keeps one text row plus a compact footer toolbar");
        var toolbar = (FrameworkElement)w.FindName("ReplyToolbar");
        var replyContent = (FrameworkElement)w.FindName("ReplyContent");
        check(toolbar.TranslatePoint(new Point(), answer).Y >= replyContent.TranslatePoint(new Point(0, replyContent.ActualHeight), answer).Y, "reply toolbar occupies a separate row below text");
        foreach (string name in new[] { "CopyReplyButton", "StopButton", "ExpandButton", "SendButton", "ScreenSharingButton" })
        {
            var button = (Button)w.FindName(name);
            check(button.Width == 24 && button.Height == 24 && button.Content is BubbleGlyph { Width: 14, Height: 14 }, "all bubble actions share 24-DIP targets and 14-DIP glyphs");
        }
        string copied = ""; typeof(MainWindow).GetField("copyText", Private)!.SetValue(w, (Action<string>)(value => copied = value));
        message.Text = "**복사할 원문**"; ((Button)w.FindName("CopyReplyButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        check(copied == "**복사할 원문**" && ((BubbleGlyph)w.FindName("CopyReplyGlyph")).Kind == BubbleGlyphKind.Check, "footer copy preserves full Markdown source and shows confirmation glyph");
        typeof(MainWindow).GetField("copyText", Private)!.SetValue(w, (Action<string>)(_ => throw new ExternalException("clipboard busy")));
        ((Button)w.FindName("CopyReplyButton")).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        check(((TextBlock)w.FindName("Status")).Text == "복사하지 못했어요. 다시 시도해 주세요.", "clipboard contention shows an error without closing the app");
        var status = (TextBlock)w.FindName("Status"); var userStatus = (TextBlock)w.FindName("UserStatus");
        var stop = (Button)w.FindName("StopButton"); var copy = (Button)w.FindName("CopyReplyButton"); var replyStatus = (TextBlock)w.FindName("ReplyStatus");
        stop.Visibility = Visibility.Visible; status.Text = "답변 중…"; Pump();
        check(copy.Visibility == Visibility.Collapsed && replyStatus.Text == "답변 중이에요…" && userStatus.Visibility == Visibility.Collapsed, "streaming replaces copy with stop and shows progress only in assistant bubble");
        typeof(MainWindow).GetField("replyStopRequested", Private)!.SetValue(w, true); stop.Visibility = Visibility.Collapsed; Pump();
        check(copy.Visibility == Visibility.Visible && replyStatus.Text == "답변 중지 요청을 보냈어요.", "cancel acknowledgement retains answer and reports only the request");
        Call(w, "OpenReplyBubble", "c", "new-footer-reply"); status.Text = "연결됨"; Pump();
        check(replyStatus.Visibility == Visibility.Collapsed && ((BubbleGlyph)w.FindName("CopyReplyGlyph")).Kind == BubbleGlyphKind.Copy, "new reply clears old stop and copy feedback");
        status.Text = "화면을 보내지 못해 글만 보냈습니다."; Pump();
        check(userStatus.Text == "화면 없이 글만 보냈어요." && (string)((FrameworkElement)w.FindName("UserStatusPanel")).ToolTip == status.Text, "screen warning uses short copy and retains full diagnostic tooltip");
        Call(w, "SetConnected", false, "연결이 끊겼습니다. 다시 연결 중…"); Pump();
        check(userStatus.Text == "다시 연결하는 중이에요…" && ((TextBlock)w.FindName("StatusActionText")).Text == "연결 설정", "connection failure exposes consistent reconnect copy and settings action");
        status.Text = "이전 메시지의 접수 여부를 확인하지 못했습니다. 다시 보내면 중복될 수 있습니다."; Pump();
        check(userStatus.Text == "보냈는지 확인이 필요해요." && ((TextBlock)w.FindName("StatusActionText")).Text == "확인", "uncertain delivery points to confirmation rather than resubmitting automatically");
        Call(w, "SetConnected", false, "연결되지 않음");
        bool sharing = ((CheckBox)settings.FindName("ScreenSharing")).IsChecked == true;
        Call(w, "ScreenSharingClick", w, new RoutedEventArgs());
        check(((CheckBox)settings.FindName("ScreenSharing")).IsChecked != sharing && ((BubbleGlyph)w.FindName("ScreenSharingIndicator")).Kind == BubbleGlyphKind.MonitorOff, "attachment action changes the next message setting and matching glyph");
        Call(w, "ScreenSharingClick", w, new RoutedEventArgs());
        Call(w, "SetConnected", true, "연결됨"); ((CheckBox)settings.FindName("ScreenSharing")).IsChecked = false; input.Clear();
        check(!((Button)w.FindName("SendButton")).IsEnabled, "blank text with no attachment has no send action");
        var attachmentButton = (Button)w.FindName("ScreenSharingButton"); attachmentButton.Focus();
        attachmentButton.RaiseEvent(new KeyEventArgs(Keyboard.PrimaryDevice, source, Environment.TickCount, Key.Space) { RoutedEvent = Keyboard.PreviewKeyDownEvent });
        check(attachmentButton.IsKeyboardFocused, "Space on a toolbar button is reserved for its action rather than opening composer");
        attachmentButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        check(((Button)w.FindName("SendButton")).IsEnabled && ((Button)w.FindName("SendButton")).ToolTip.ToString()!.StartsWith("화면만 보내기"), "one-line composer retains screen-only sending when sharing is enabled");
        Call(w, "SetConnected", false, "연결되지 않음");
        Call(w, "CloseBubble");
        Call(w, "OpenChat"); Pump();
        input.Text = "남길 초안"; input.Select(2, 2);
        foreach (string value in new[] { "상 고정", "하 고정", "상하 자동" })
        {
            mode.SelectedItem = value; Pump();
            check(input.Text == "남길 초안" && input.SelectedText == " 초" && input.IsKeyboardFocused, "placement changes preserve draft, selection and editor focus");
            check(user.TailTop == (value != "상 고정"), "fixed placement updates frame tail without flipping text");
            check(w.Left == left && w.Top == top, "changing bubble side keeps character anchored");
        }
        var area = SystemParameters.WorkArea;
        // Earlier steps left a sent message in the stack, which would expire first.
        Call(w, "ClearHistory"); mode.SelectedItem = "하 고정"; Call(w, "OpenReplyBubble", "c", "vertical-order"); Call(w, "OpenComposer"); Pump();
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
        var composer = (FrameworkElement)w.FindName("ComposerPanel"); var sentText = (FrameworkElement)w.FindName("UserSent");
        // The keystroke above moved the first message into the stack; these checks start without it.
        var past = (StackPanel)w.FindName("History");
        Call(w, "ClearHistory"); Call(w, "ShowSentMessage", "질문"); Pump();
        check(input.IsKeyboardFocused && composer.DesiredSize.Height == 0 && sentText.Visibility == Visibility.Visible,
            "a sent bubble shows only the sent text while its hidden editor keeps focus for the next IME keystroke");
        Call(w, "AssistantBubbleExpired"); Call(w, "UserBubbleExpired"); Pump();
        check(input.IsKeyboardFocused && user.Visibility == Visibility.Visible && user.DesiredSize.Height == 0 && user.Opacity == 0,
            "an expired sent bubble disappears but still holds editor focus");
        input.Text = "다음"; Pump();
        check(input.IsKeyboardFocused && composer.DesiredSize.Height > 0 && sentText.Visibility == Visibility.Collapsed && user.DesiredSize.Height > 0 && user.Opacity == 1 && past.Children.Count == 0,
            "typing after a send opens a new editor bubble and never brings back an expired one");
        input.Clear(); Call(w, "CloseBubble"); Call(w, "ShowSentMessage", "둘째 질문"); message.Text = "둘째 답변"; Call(w, "OpenReplyBubble", "c", "stack-1"); Pump();
        input.Text = "셋째"; Pump();
        check(past.Children.Count == 1 && sentText.Visibility == Visibility.Collapsed && answer.Visibility == Visibility.Visible && Grid.GetRow(past) < Grid.GetRow(answer) && Grid.GetRow(answer) < Grid.GetRow(user),
            "typing moves the sent message up into the stack above the reply, like a text conversation");
        input.Clear(); Call(w, "ShowSentMessage", "셋째"); Call(w, "OpenReplyBubble", "c", "stack-2"); message.Text = "셋째 답변"; Pump();
        check(past.Children.Count == 2 && ((PixelBubble)past.Children[0]).IsUser && !((PixelBubble)past.Children[1]).IsUser && sentText.Visibility == Visibility.Visible && Grid.GetRow(user) < Grid.GetRow(answer),
            "a new reply moves the earlier one into the stack while the newest message stays live above it");
        for (int i = 0; i < 4; i++)
        {
            input.Text = "더"; Pump(); input.Clear(); Call(w, "ShowSentMessage", $"질문 {i}"); Call(w, "OpenReplyBubble", "c", $"stack-more-{i}"); message.Text = $"답변 {i}"; Pump();
        }
        check(past.Children.Count == 4 && user.Visibility == Visibility.Visible && answer.Visibility == Visibility.Visible, "at most six bubbles are shown; the oldest leave the stack first");
        if (screenshots is not null) { System.IO.Directory.CreateDirectory(screenshots); Save(host, System.IO.Path.Combine(screenshots, "06-stacked-conversation.png")); }
        var oldestLeft = past.Children[1];
        Call(w, "PastBubbleExpired", 0); Pump();
        check(past.Children.Count == 3 && past.Children[0] == oldestLeft, "each stacked bubble expires on its own time, oldest first");
        Call(w, "ClearHistory"); input.Clear(); answer.Visibility = Visibility.Visible; Pump();
        input.Text = "IME 초안"; input.Select(0, 3);
        var ime = new TextComposition(InputManager.Current, input, "가", TextCompositionAutoComplete.Off);
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, ime) { RoutedEvent = TextCompositionManager.PreviewTextInputStartEvent });
        mode.SelectedItem = "상 고정"; Pump();
        var enter = new KeyEventArgs(Keyboard.PrimaryDevice, source, Environment.TickCount, Key.ImeProcessed) { RoutedEvent = Keyboard.PreviewKeyDownEvent }; input.RaiseEvent(enter);
        check(!enter.Handled && input.SelectedText == "IME", "moving the detached bubble retains active IME composition and selection");
        message.Text = "## 오늘 할 일\n\n**선생님**, 바로 이어서 이야기할 수 있어요.\n\n- 입력창 자동 열기\n- 말풍선 위치 선택\n\n| 기능 | 상태 |\n|---|---|\n| 대화 | 준비 |";
        if (screenshots is not null)
        {
            System.IO.Directory.CreateDirectory(screenshots);
            w.Left = area.Left + 160; w.Top = area.Top + 80; mode.SelectedItem = "하 고정"; Pump(); Save(host, System.IO.Path.Combine(screenshots, "04-detached-below.png"));
            w.Top = area.Bottom - w.Height - 60; mode.SelectedItem = "상 고정"; Pump(); Save(host, System.IO.Path.Combine(screenshots, "05-detached-above.png"));
            string draftBefore = input.Text, replyBefore = message.Text;
            Call(w, "SetConnected", true, "연결됨"); input.Clear(); message.Text = "이어서 이야기해요."; Pump(); Save(host, System.IO.Path.Combine(screenshots, "07-footer-idle.png"));
            input.Text = "이 부분을 더 설명해줘."; Pump(); Save(host, System.IO.Path.Combine(screenshots, "08-footer-writing.png"));
            stop.Visibility = Visibility.Visible; status.Text = "답변 중…"; message.Text = "Alt+Q로 입력창을 열고, 한 번 더 누르면 이전 작업으로 돌아가요."; Pump(); Save(host, System.IO.Path.Combine(screenshots, "09-footer-streaming.png"));
            stop.Visibility = Visibility.Collapsed; status.Text = "화면을 보내지 못해 글만 보냈습니다."; Pump(); Save(host, System.IO.Path.Combine(screenshots, "10-footer-screen-warning.png"));
            Call(w, "SetConnected", false, "연결이 끊겼습니다. 다시 연결 중…"); Pump(); Save(host, System.IO.Path.Combine(screenshots, "11-footer-offline.png"));
            Call(w, "SetConnected", true, "이전 메시지의 접수 여부를 확인하지 못했습니다. 다시 보내면 중복될 수 있습니다."); Pump(); Save(host, System.IO.Path.Combine(screenshots, "12-footer-uncertain.png"));
            Call(w, "SetConnected", false, "연결되지 않음"); input.Text = draftBefore; message.Text = replyBefore; Pump();
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
        double userX = user.PointToScreen(new Point()).X;
        Call(w, "ExpandReplyClick", w, new RoutedEventArgs()); Pump();
        var inlineReader = (ReplyReader)typeof(MainWindow).GetField("replyReader", Private)!.GetValue(w)!;
        Until(() => (bool)typeof(ReplyReader).GetField("ready", Private)!.GetValue(inlineReader)!);
        var inlineBrowser = (WebView2CompositionControl)typeof(ReplyReader).GetField("browser", Private)!.GetValue(inlineReader)!;
        var rendered = inlineBrowser.ExecuteScriptAsync("document.getElementById('answer').textContent"); Until(() => rendered.IsCompleted);
        Until(() => { var style = inlineBrowser.ExecuteScriptAsync("document.querySelector('h2') ? getComputedStyle(document.querySelector('h2')).fontSize : ''"); Until(() => style.IsCompleted); return style.Result == "\"16px\""; });
        Pump();
        check(System.Windows.Application.Current.Windows.Count == windowCount && answer.Width > compactWidth && Window.GetWindow(inlineReader) == host, "expansion enlarges existing pixel bubble without creating another window");
        check(w.Left == anchoredLeft && w.Top == anchoredTop, "expanding content keeps character anchored");
        check(user.Visibility == Visibility.Visible && Math.Abs(user.PointToScreen(new Point()).X - userX) <= 2, "expanding the reply leaves the user bubble at its own anchor");
        check(toolbar.TranslatePoint(new Point(), answer).Y >= replyContent.TranslatePoint(new Point(0, replyContent.ActualHeight), answer).Y, "expanded reader also keeps the toolbar outside its scrolling content");
        if (screenshots is not null) Save(host, System.IO.Path.Combine(screenshots, "06-expanded-inline.png"));
        Call(w, "ExpandReplyClick", w, new RoutedEventArgs()); Pump();
        check(answer.Width == compactWidth && input.Text == "IME 초안", "collapsing restores compact bubble and preserves draft");
        check(Math.Abs(user.PointToScreen(new Point()).X - userX) <= 2, "collapsing keeps the user bubble where it was");
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
        {
            // Stand in for a fullscreen foreground window; Windows may refuse this process the real one.
            var probe = typeof(MainWindow).GetField("fullscreenWorkProbe", Private)!;
            var monitor = System.Windows.Forms.Screen.FromHandle(new WindowInteropHelper(w).Handle);
            bool Covers() => host.IsVisible && host.Width * VisualTreeHelper.GetDpi(host).DpiScaleX >= monitor.Bounds.Width - 1
                && host.Height * VisualTreeHelper.GetDpi(host).DpiScaleY >= monitor.Bounds.Height - 1;
            input.Clear(); Call(w, "ClearHistory"); Call(w, "CloseBubble"); Call(w, "HideUserBubble"); Pump();
            check(!host.IsVisible, "with no bubbles and no fullscreen window the dialogue host is hidden");
            probe.SetValue(w, (Func<System.Windows.Forms.Screen?>)(() => monitor));
            Call(w, "UpdateSceneBounds"); Pump();
            check(Covers() && user.Visibility == Visibility.Collapsed && answer.Visibility == Visibility.Collapsed,
                "over fullscreen the empty host already spans the monitor before Alt+Tab activates the character");
            int activations = 0; void Count(object? s, EventArgs e) => activations++;
            w.Activated += Count;
            w.Hide(); Pump();
            check(!host.IsVisible, "hiding the character from the tray also hides the cover");
            Call(w, "HotKeyHook", IntPtr.Zero, 0x0312, new IntPtr(0x4849), IntPtr.Zero, false); Pump();
            w.Activated -= Count;
            check(w.IsVisible && activations == 0 && Covers() && ((FrameworkElement)w.FindName("ComposerPanel")).Visibility == Visibility.Visible,
                "Alt+Q from the tray over fullscreen shows the character without activating it and opens chat in the cover");
            probe.SetValue(w, null); Call(w, "UpdateSceneBounds"); Pump();
            check(host.IsVisible && !Covers() && host.Width <= 320, "leaving fullscreen returns the host to the bubble's own size");
            check(settings.Owner is null && !settings.Topmost, "settings is an ordinary window that other windows can cover");
        }
        w.Close(); Pump();
        foreach (bool newerReply in new[] { false, true })
        {
            var race = new MainWindow(true); var transport = new ReplyReconnectHandler { DeferCancel = true };
            var server = new ServerClient("https://example.com/", "test", transport); using var token = new CancellationTokenSource();
            void Field(string name, object? value) => typeof(MainWindow).GetField(name, Private)!.SetValue(race, value);
            Field("client", server); Field("connection", token); Field("currentConversation", "c");
            Call(race, "SetConnected", true, "연결됨"); Call(race, "OpenReplyBubble", "c", "j1");
            var raceStop = (Button)race.FindName("StopButton"); raceStop.Visibility = Visibility.Visible;
            var cancellation = (Task)Call(race, "RequestCancel")!;
            // The stream completes normally before the HTTP cancellation acknowledgement.
            raceStop.Visibility = Visibility.Collapsed;
            if (newerReply) Call(race, "OpenReplyBubble", "c", "j2");
            transport.ReleaseCancel();
            Until(() => cancellation.IsCompleted); cancellation.GetAwaiter().GetResult();
            Pump();
            var text = ((TextBlock)race.FindName("ReplyStatus")).Text;
            check(newerReply ? text == "" : text == "답변 중지 요청을 보냈어요.", "late cancel response never claims completion or affects a newer reply");
            race.Close(); server.Dispose();
        }
        {
            // A fresh process: the first composer above a character at the bottom of the screen.
            var fresh = new MainWindow(true);
            fresh.Show(); Pump();
            fresh.Left = area.Left + 60; fresh.Top = area.Bottom - fresh.Height; Pump();
            var freshHost = (Window)typeof(MainWindow).GetField("dialogueHost", Private)!.GetValue(fresh)!;
            Call(fresh, "OpenChat"); Pump();
            var freshUser = (PixelBubble)fresh.FindName("UserBubble");
            double gap = fresh.Top - (freshUser.TranslatePoint(new Point(0, freshUser.ActualHeight), freshHost).Y + freshHost.Top);
            check(gap >= 0 && gap <= 16, "first composer after start sits directly above a bottom character");
            fresh.Close(); Pump();
        }
        foreach (bool flipped in new[] { false, true })
        {
            var legacy =new MainWindow(true) { Left = 120, Top = 100, Width = 520, Height = 700 };
            var legacySettings = (Window)typeof(MainWindow).GetField("settings", Private)!.GetValue(legacy)!;
            ((CheckBox)legacySettings.FindName("FacingLeft")).IsChecked = flipped;
            typeof(MainWindow).GetField("loadedOnce", Private)!.SetValue(legacy, true);
            legacy.Show(); Pump();
            var character = (FrameworkElement)legacy.FindName("Character");
            var a = character.PointToScreen(new Point(0, 0)); var b = character.PointToScreen(new Point(character.ActualWidth, character.ActualHeight));
            var before = new Point(Math.Min(a.X, b.X), Math.Min(a.Y, b.Y));
            typeof(MainWindow).GetField("legacyWindowPosition", Private)!.SetValue(legacy, true);
            Call(legacy, "InitializeDialogueHost"); legacy.UpdateLayout();
            a = character.PointToScreen(new Point(0, 0)); b = character.PointToScreen(new Point(character.ActualWidth, character.ActualHeight));
            check(Math.Abs(Math.Min(a.X, b.X) - before.X) <= 2 && Math.Abs(Math.Min(a.Y, b.Y) - before.Y) <= 2, "legacy window migration preserves the visible character position in both orientations");
            legacy.Close();
        }
        Console.WriteLine("Detached windows, four screen corners, vertical modes, type-to-chat and native Markdown checks passed.");
    }
    private static void Save(Window window, string path)
    {
        window.UpdateLayout(); var element = (FrameworkElement)window.Content;
        var bitmap = new RenderTargetBitmap((int)Math.Ceiling(window.ActualWidth), (int)Math.Ceiling(window.ActualHeight), 96, 96, PixelFormats.Pbgra32); bitmap.Render(element);
        var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(bitmap)); using var file = System.IO.File.Create(path); png.Save(file);
    }
}
