using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Threading;
using System.Reflection;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using Himari.Companion;

internal static class Program
{
    [STAThread] private static void Main(string[] args)
    {
        if (args.Contains("--fullscreen-fixture")) { FullscreenChecks.Fixture(); return; }
        var app = new App(); app.InitializeComponent(); app.ShutdownMode = ShutdownMode.OnExplicitShutdown;
        using var source = new HwndSource(new HwndSourceParameters("HIMARI keyboard regression") { Width = 1, Height = 1, WindowStyle = unchecked((int)0x80000000) });
        var root = new Grid(); var input = new TextBox { AcceptsReturn = true }; root.Children.Add(input); source.RootVisual = root;
        int sent = 0, routed = 0; ModifierKeys modifiers = ModifierKeys.None;
        _ = new ComposerInput(input, () => sent++, () => modifiers);
        root.AddHandler(Keyboard.PreviewKeyDownEvent, new KeyEventHandler((_, _) => routed++), true);
        KeyEventArgs Key(Key key)
        {
            var e = new KeyEventArgs(Keyboard.PrimaryDevice, source, Environment.TickCount, key) { RoutedEvent = Keyboard.PreviewKeyDownEvent };
            input.RaiseEvent(e); return e;
        }
        void Check(bool value, string message) { if (!value) throw new Exception(message); }
        Check(Key(System.Windows.Input.Key.Enter).Handled && sent == 1, "preview Enter sends before multiline textbox handling");
        modifiers = ModifierKeys.Shift;
        Check(!Key(System.Windows.Input.Key.Enter).Handled && sent == 1, "Shift+Enter is left to multiline textbox");
        modifiers = ModifierKeys.None;
        var composition = new TextComposition(InputManager.Current, input, "가", TextCompositionAutoComplete.Off);
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, composition) { RoutedEvent = TextCompositionManager.PreviewTextInputStartEvent });
        Check(!Key(System.Windows.Input.Key.Enter).Handled && sent == 1, "Enter during IME composition does not send");
        Check(!Key(System.Windows.Input.Key.ImeProcessed).Handled && sent == 1, "IME-processed key is left to IME");
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, composition) { RoutedEvent = TextCompositionManager.PreviewTextInputEvent });
        Check(!Key(System.Windows.Input.Key.Enter).Handled && sent == 1, "commit Enter is guarded until input dispatch finishes");
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
        Check(Key(System.Windows.Input.Key.Enter).Handled && sent == 2 && routed == 6, "next Enter sends after IME commit on real routed event path");
        var normal = new TextComposition(InputManager.Current, input, "a", TextCompositionAutoComplete.On);
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, normal) { RoutedEvent = TextCompositionManager.PreviewTextInputStartEvent });
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, normal) { RoutedEvent = TextCompositionManager.PreviewTextInputEvent });
        Check(Key(System.Windows.Input.Key.Enter).Handled && sent == 3, "ordinary text followed immediately by Enter is not mistaken for IME");
        Color Fill(bool isUser)
        {
            var bubble = new PixelBubble { IsUser = isUser, Child = new Border() };
            bubble.Measure(new Size(160, 100)); bubble.Arrange(new Rect(0, 0, 160, 100)); bubble.UpdateLayout();
            var bitmap = new RenderTargetBitmap(160, 100, 96, 96, PixelFormats.Pbgra32); bitmap.Render(bubble);
            var pixel = new byte[4]; bitmap.CopyPixels(new Int32Rect(20, 20, 1, 1), pixel, 4, 0);
            return Color.FromArgb(pixel[3], pixel[2], pixel[1], pixel[0]);
        }
        Check(Fill(true) == Color.FromRgb(255, 253, 243) && Fill(false) == Color.FromRgb(242, 237, 255), "rendered user bubble is cream and Himari bubble is lavender");
        var timeline = new MainWindow();
        object? Timeline(string method, params object?[] args) => typeof(MainWindow).GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(timeline, args);
        var oldUser = (FrameworkElement)timeline.FindName("UserBubble");
        var reply = (FrameworkElement)timeline.FindName("BubblePanel");
        var draftInput = (TextBox)timeline.FindName("Input");
        var replyTimer = (DispatcherTimer)typeof(MainWindow).GetField("bubbleTimer", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(timeline)!;
        Timeline("OpenComposer"); draftInput.Text = "첫 질문"; draftInput.Clear(); Timeline("ShowSentMessage", "첫 질문");
        Timeline("OpenReplyBubble", "c", "j1");
        Check(Grid.GetRow(oldUser) == 0 && Grid.GetRow(reply) == 1, "reply arrives beneath the earlier user message");
        Timeline("ShowSentMessage", "첫 질문");
        Check(Grid.GetRow(oldUser) == 0 && Grid.GetRow(reply) == 1, "late send acknowledgement preserves chronological order");
        var originalParent = oldUser.Parent;
        Timeline("OpenComposer"); draftInput.Text = "다음 초안"; draftInput.Select(1, 3);
        Timeline("OpenReplyBubble", "c", "j1");
        Check(Grid.GetRow(reply) == 0 && Grid.GetRow(oldUser) == 1 && oldUser.Parent == originalParent && draftInput.SelectedText == "음 초", "new draft moves below the prior reply and reconnecting that same reply neither reorders nor reparents its editor");
        Timeline("OpenReplyBubble", "c", "j2");
        Check(Grid.GetRow(oldUser) == 0 && Grid.GetRow(reply) == 1, "a new reply moves to the bottom even if a previous reply remained visible");
        draftInput.Clear(); Timeline("ShowSentMessage", new string('가', 100)); Timeline("ScheduleBubbleClose", "짧은 답변");
        Timeline("AssistantBubbleExpired");
        Check(oldUser.Visibility == Visibility.Visible && reply.Visibility == Visibility.Visible && !replyTimer.IsEnabled, "a newer short answer cannot disappear ahead of an older long user message");
        Timeline("UserBubbleExpired");
        Check(oldUser.Visibility == Visibility.Collapsed && reply.Visibility == Visibility.Visible && replyTimer.IsEnabled && replyTimer.Interval == TimeSpan.FromSeconds(2), "oldest expires first and the overdue newer answer remains for another tick");
        Timeline("AssistantBubbleExpired");
        Check(reply.Visibility == Visibility.Collapsed, "the newer answer then expires");
        Timeline("OpenComposer"); draftInput.Text = "보존할 초안"; Timeline("OpenReplyBubble", "c", "j3");
        Timeline("UserBubbleExpired"); Timeline("AssistantBubbleExpired");
        Check(oldUser.Visibility == Visibility.Visible && reply.Visibility == Visibility.Visible && draftInput.Text == "보존할 초안", "an older protected draft also holds an expired newer answer");
        draftInput.Text += " 계속";
        Check(reply.Visibility == Visibility.Collapsed && oldUser.Visibility == Visibility.Visible, "continued drafting moves the user last and lets an expired older answer close");
        Timeline("OpenReplyBubble", "c", "j4"); Timeline("ScheduleBubbleClose", "새 답변"); draftInput.Clear(); Timeline("ShowSentMessage", "보낸 질문");
        Timeline("UserBubbleExpired");
        Check(oldUser.Visibility == Visibility.Collapsed && reply.Visibility == Visibility.Visible, "replacing an expired answer gives the new answer its own reading lifetime");
        Timeline("CloseBubble");
        Timeline("OpenReplyBubble", "c", "j5"); Timeline("OpenComposer"); draftInput.Clear();
        typeof(MainWindow).GetField("awaitingAnswer", BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(timeline, true);
        Timeline("AssistantBubbleExpired"); Timeline("UserBubbleExpired");
        Check(reply.Visibility == Visibility.Visible && oldUser.Visibility == Visibility.Visible, "an older pending question blocks the newer bubble's expiry");
        typeof(MainWindow).GetField("awaitingAnswer", BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(timeline, false);
        Timeline("AssistantBubbleExpired");
        Check(reply.Visibility == Visibility.Collapsed && oldUser.Visibility == Visibility.Visible, "assistant-first order also closes from the top");
        Timeline("UserBubbleExpired");
        Check(oldUser.Visibility == Visibility.Collapsed, "the newer user bubble closes after the older answer");
        Timeline("OpenComposer"); Timeline("ShowSentMessage", "전송 중인 글"); Timeline("OpenReplyBubble", "c", "j6");
        typeof(MainWindow).GetField("sending", BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(timeline, true);
        Timeline("UserBubbleExpired"); Timeline("AssistantBubbleExpired");
        Check(oldUser.Visibility == Visibility.Visible && reply.Visibility == Visibility.Visible, "an older unconfirmed send holds both bubbles");
        typeof(MainWindow).GetField("sending", BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(timeline, false);
        Timeline("UserBubbleExpired"); Timeline("AssistantBubbleExpired");
        Timeline("OpenReplyBubble", "c", "j7"); Timeline("OpenComposer");
        var streams = (Dictionary<string, CancellationTokenSource>)typeof(MainWindow).GetField("liveStreams", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(timeline)!;
        using var live = new CancellationTokenSource(); streams[""] = live;
        Timeline("AssistantBubbleExpired"); Timeline("UserBubbleExpired");
        Check(reply.Visibility == Visibility.Visible && oldUser.Visibility == Visibility.Visible, "an older streaming answer holds a newer expired user bubble");
        streams.Clear(); Timeline("AssistantBubbleExpired"); Timeline("UserBubbleExpired");
        Timeline("OpenComposer"); draftInput.Text = "숨겨도 남는 초안"; Timeline("OpenReplyBubble", "c", "j8");
        Timeline("AssistantBubbleExpired"); Timeline("HideUserBubble");
        Check(replyTimer.IsEnabled && draftInput.Text == "숨겨도 남는 초안", "manually hiding an older draft resumes the overdue reply timer without losing the draft");
        Timeline("AssistantBubbleExpired");
        Check(reply.Visibility == Visibility.Collapsed, "the overdue reply can expire after the older draft is hidden");
        var composerPanel = (FrameworkElement)timeline.FindName("ComposerPanel");
        Timeline("OpenChat"); draftInput.Text = "단축키로 숨긴 초안"; Timeline("CloseChat");
        Check(oldUser.Visibility == Visibility.Visible && composerPanel.Visibility == Visibility.Visible && draftInput.Text == "단축키로 숨긴 초안", "switching away preserves composer and draft like Alt+Tab");
        Timeline("OpenChat");
        Check(oldUser.Visibility == Visibility.Visible && composerPanel.Visibility == Visibility.Visible && draftInput.Text == "단축키로 숨긴 초안", "shortcut reopens the hidden draft ready to type");
        draftInput.Clear(); Timeline("CloseChat");
        Check(composerPanel.Visibility == Visibility.Visible, "switching focus leaves an empty composer available until ordinary expiry");
        timeline.Close();
        var companion = new MainWindow();
        var settings = (Window)typeof(MainWindow).GetField("settings", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(companion)!;
        var assistant = (FrameworkElement)companion.FindName("BubblePanel");
        var user = (FrameworkElement)companion.FindName("UserBubble");
        var character = (FrameworkElement)companion.FindName("Character");
        Check(assistant.Visibility == Visibility.Collapsed && user.Visibility == Visibility.Collapsed, "idle starts without either bubble");
        Check(((CheckBox)settings.FindName("ScreenSharing")).IsChecked == true && (string)((ComboBox)settings.FindName("ChatHotkey")).SelectedItem == "Alt+Q", "default capture and shortcut are reflected in settings");
        assistant.Visibility = user.Visibility = Visibility.Visible;
        ((FrameworkElement)companion.FindName("ComposerPanel")).Visibility = Visibility.Visible;
        ((FrameworkElement)companion.FindName("StopButton")).Visibility = Visibility.Visible;
        ((MarkdownView)companion.FindName("Message")).Text = new string('가', 1500);
        ((TextBox)companion.FindName("Input")).Text = "작성 중인 글\n둘째 줄";
        typeof(MainWindow).GetMethod("OpenBubble", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(companion, null);
        foreach (bool flipped in new[] { false, true })
        {
            ((CheckBox)settings.FindName("FacingLeft")).IsChecked = flipped;
            Check(((ScaleTransform)character.RenderTransform).ScaleX == (flipped ? -1 : 1), "character mirrors");
            Check(assistant.HorizontalAlignment == HorizontalAlignment.Center && user.HorizontalAlignment == HorizontalAlignment.Center && assistant.Width == user.Width, "speakers share a column and bubble width without mirroring text");
            Check(((PixelBubble)assistant).TailRight == flipped && ((PixelBubble)user).TailRight != flipped, "assistant tail points toward character and user tail points away");
            foreach (bool assistantLatest in new[] { true, false })
            foreach (int size in new[] { 200, 400 })
            {
                typeof(MainWindow).GetMethod(assistantLatest ? "OpenBubble" : "OpenComposer", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(companion, null);
                ((Slider)settings.FindName("SizeSlider")).Value = size;
                companion.Width = 520; companion.Height = 700;
                var content = (FrameworkElement)companion.Content;
                for (int i=0;i<3;i++)
                {
                    typeof(MainWindow).GetMethod("UpdateBubbleBounds", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(companion,null);
                    content.Measure(new Size(520,700)); content.Arrange(new Rect(0,0,520,700)); content.UpdateLayout();
                }
                var assistantBounds = assistant.TransformToAncestor(content).TransformBounds(new Rect(assistant.RenderSize));
                var userBounds = user.TransformToAncestor(content).TransformBounds(new Rect(user.RenderSize));
                Check(assistantBounds.Top >= 0 && userBounds.Bottom <= 700 - size + 14, "both bubbles fit above character at supported sizes");
                Check(Math.Abs(assistantBounds.Left + assistantBounds.Width / 2 - userBounds.Left - userBounds.Width / 2) < 1 &&
                    (assistantLatest ? userBounds.Bottom <= assistantBounds.Top : assistantBounds.Bottom <= userBounds.Top), "the newer bubble stays below the older on the same center line in either speaker order");
                var characterBounds = character.TransformToAncestor(content).TransformBounds(new Rect(character.RenderSize));
                double gazeOffset = userBounds.Left + userBounds.Width / 2 - characterBounds.Left - characterBounds.Width / 2;
                Check(flipped ? gazeOffset < 0 : gazeOffset > 0, "shared conversation center follows the character gaze in both directions");
                Check(assistantBounds.Left >= 0 && assistantBounds.Right <= 520 && userBounds.Left >= 0 && userBounds.Right <= 520, "conversation column fits at either window edge");
                var stop = (FrameworkElement)companion.FindName("StopButton");
                var stopBounds = stop.TransformToAncestor(assistant).TransformBounds(new Rect(stop.RenderSize));
                Check(stopBounds.Top >= 0 && stopBounds.Bottom <= assistant.ActualHeight, "stop button remains visible with a long answer");
            }
        }
        ((Slider)settings.FindName("BubbleMinimum")).Value = 12;
        ((Slider)settings.FindName("SecondsPer100")).Value = 20;
        typeof(MainWindow).GetMethod("ScheduleBubbleClose", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(companion, new object[] { new string('가',100) });
        var assistantTimer = (DispatcherTimer)typeof(MainWindow).GetField("bubbleTimer", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(companion)!;
        Check(assistantTimer.Interval == TimeSpan.FromSeconds(32), "assistant display timer uses saved reading settings");
        typeof(MainWindow).GetMethod("ShowSentMessage", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(companion, new object[] { new string('가',100) });
        var userTimer = (DispatcherTimer)typeof(MainWindow).GetField("userBubbleTimer", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(companion)!;
        Check(userTimer.Interval == TimeSpan.FromSeconds(32) && !ReferenceEquals(userTimer, assistantTimer), "speaker bubbles use independent timers with same configurable reading rule");
        var draft = (TextBox)companion.FindName("Input");
        var composer = (FrameworkElement)companion.FindName("ComposerPanel");
        var sentText = (TextBlock)companion.FindName("UserSent");
        Check(composer.Visibility == Visibility.Visible && sentText.Visibility == Visibility.Collapsed && draft.Text.Contains("작성 중인 글"), "late send confirmation keeps a new draft without duplicating previous text");
        draft.Clear();
        typeof(MainWindow).GetMethod("ShowSentMessage", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(companion, new object[] { "보낸 글" });
        Check(composer.Visibility == Visibility.Collapsed && sentText.Visibility == Visibility.Visible && sentText.Text == "보낸 글", "confirmed send displays one compact user message");
        typeof(MainWindow).GetMethod("OpenComposer", BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(companion, null);
        Check(composer.Visibility == Visibility.Visible && sentText.Visibility == Visibility.Collapsed, "reopening composer replaces sent text with the draft editor");
        var status = (TextBlock)companion.FindName("Status");
        var userStatus = (TextBlock)companion.FindName("UserStatus");
        status.Text = "연결됨";
        Check(userStatus.Visibility == Visibility.Collapsed, "routine connected status does not add a chat row");
        status.Text = "화면을 보내지 못해 글만 보냈습니다.";
        Check(userStatus.Visibility == Visibility.Visible && userStatus.Text == "화면 없이 글만 보냈어요.", "screen delivery warning uses concise bubble copy");
        Check((string)((FrameworkElement)companion.FindName("UserStatusPanel")).ToolTip == status.Text, "warning tooltip retains the full diagnostic text");
        companion.Close();
        foreach (bool partialLive in new[] { false, true })
        {
            var recovery = new MainWindow();
            object? Call(string method, params object?[] args) => typeof(MainWindow).GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(recovery, args);
            void Field(string field, object? value) => typeof(MainWindow).GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(recovery, value);
            void Finish(Task task)
            {
                Until(() => task.IsCompleted);
                task.GetAwaiter().GetResult();
            }
            void Until(Func<bool> ready)
            {
                var untilFrame = new DispatcherFrame(); var poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(10) };
                var deadline = DateTime.UtcNow.AddSeconds(5);
                poll.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) untilFrame.Continue = false; };
                if (!ready()) { poll.Start(); Dispatcher.PushFrame(untilFrame); poll.Stop(); }
                Check(ready(), "asynchronous UI operation completed within test deadline");
            }
            var transport = new ReplyReconnectHandler();
            var first = new ServerClient("https://example.com/", "device-one", transport);
            using var connectionToken = new CancellationTokenSource();
            Field("client", first); Field("recoveryOwner", first); Field("connection", connectionToken); Field("currentConversation", "c"); Field("deviceId", "d");
            var recoverySettings = (Window)typeof(MainWindow).GetField("settings", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(recovery)!;
            ((CheckBox)recoverySettings.FindName("ScreenSharing")).IsChecked = false;
            var answer = (FrameworkElement)recovery.FindName("BubblePanel");
            var editor = (TextBox)recovery.FindName("Input");
            var notice = (FrameworkElement)recovery.FindName("ConnectionNotice");
            Call("SetConnected", true, "연결됨");
            Finish((Task)Call("ReadConversation", "c", CancellationToken.None)!);
            Check(answer.Visibility == Visibility.Collapsed, "browsing an old completed answer does not open a bubble");
            editor.Text = "새 질문";
            Call("SendMessage");
            Until(() => !(bool)typeof(MainWindow).GetField("sending", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(recovery)!);
            Check(transport.Posts == 1 && editor.Text == "", "real send path records accepted request and clears submitted draft");
            if (partialLive)
            {
                transport.Live = "event: snapshot\ndata: {\"job_id\":\"j\",\"run_id\":\"r\",\"text\":\"답변 앞부분\",\"moods\":[]}\n\n";
                Call("StartLive", "c", first, connectionToken.Token, "j");
                Until(() => ((MarkdownView)recovery.FindName("Message")).Text == "답변 앞부분");
                Call("OpenComposer"); editor.Text = "연결이 끊겨도 남길 새 초안";
            }
            Call("StopConnection"); Call("CloseBubble");
            Call("SetConnected", false, "연결이 끊겼습니다. 다시 연결 중…");
            Check(notice.Visibility == Visibility.Visible && answer.Visibility == Visibility.Collapsed, "disconnect warning is visible even when chat is hidden");
            transport.Complete = true;
            var restored = new ServerClient("https://example.com/", "device-one", transport);
            using var restoredConnection = new CancellationTokenSource();
            Field("client", restored); Field("recoveryOwner", restored); Field("connection", restoredConnection);
            Call("SetConnected", true, "연결됨");
            Finish((Task)Call("ReadConversation", "c", CancellationToken.None)!);
            var readingTimer = (DispatcherTimer)typeof(MainWindow).GetField("bubbleTimer", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(recovery)!;
            Check(answer.Visibility == Visibility.Visible && ((MarkdownView)recovery.FindName("Message")).Text == "복구된 최종 답변" && readingTimer.IsEnabled, "accepted send, disconnect, server completion and reconnect opens final answer with reading timer");
            Check(!(bool)typeof(MainWindow).GetField("talking", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(recovery)!, "completed recovery stops partial Live talking animation");
            Check(notice.Visibility == Visibility.Collapsed, "successful reconnect clears independent warning");
            if (partialLive)
            {
                Check(editor.Text == "연결이 끊겨도 남길 새 초안" && Grid.GetRow(answer) < Grid.GetRow((FrameworkElement)recovery.FindName("UserBubble")), "recovered final keeps a newer draft below the same job's earlier streamed reply");
                editor.Clear();
            }
            Call("CloseBubble"); Finish((Task)Call("ReadConversation", "c", CancellationToken.None)!);
            Check(answer.Visibility == Visibility.Collapsed && !readingTimer.IsEnabled, "repeated refresh does not reopen or restart an already recovered answer");
            Call("WaitForReply", restored, "c", "j2", 3);
            transport.Job = "j2"; transport.Live = "event: snapshot\ndata: {\"job_id\":\"j2\",\"run_id\":\"r2\",\"text\":\"정상 Live 답변\",\"moods\":[]}\n\nevent: done\ndata: {}\n\n";
            Finish((Task)Call("LiveLoop", "c", restored, CancellationToken.None, "j2")!);
            Call("CloseBubble");
            Call("WaitForReply", restored, "c", "j2", 3); // A late HTTP acknowledgement must not re-arm done.
            transport.Sequence = 5;
            Finish((Task)Call("ReadConversation", "c", CancellationToken.None)!);
            Check(answer.Visibility == Visibility.Collapsed, "normally completed Live and late acceptance cannot resurrect a hidden answer");
            Call("WaitForReply", restored, "c", "j3", 5);
            transport.Job = "j4"; transport.Sequence = 7;
            Finish((Task)Call("ReadConversation", "c", CancellationToken.None)!);
            Check(answer.Visibility == Visibility.Collapsed, "another job's final answer cannot satisfy the pending reply");
            Call("WaitForReply", restored, "c", "j4", 7);
            Finish((Task)Call("ReadConversation", "c", CancellationToken.None)!);
            Check(answer.Visibility == Visibility.Collapsed, "a final at or before the send boundary is not new evidence");
            transport.Job = "j5"; transport.Sequence = 9;
            Call("WaitForReply", restored, "c", "j5", 7);
            transport.Live = "event: done\ndata: {}\n\n";
            Call("StartLive", "c", restored, CancellationToken.None, "j5");
            Until(() => answer.Visibility == Visibility.Visible && readingTimer.IsEnabled);
            Check(((MarkdownView)recovery.FindName("Message")).Text == "복구된 최종 답변", "done without a Live snapshot still recovers and opens saved final");
            Call("CloseBubble");
            var foreign = new ServerClient("https://example.com/", "device-two", transport);
            Call("WaitForReply", foreign, "c", "j6", 9);
            transport.Job = "j6"; transport.Sequence = 11;
            Finish((Task)Call("ReadConversation", "c", CancellationToken.None)!);
            Check(answer.Visibility == Visibility.Collapsed, "pending replies from a different device do not open this device's bubble");
            foreign.Dispose();
            recovery.Close();
        }
        foreach (bool doneBeforeAcceptance in new[] { true, false })
        foreach (bool hasLiveBody in new[] { false, true })
        {
            var ordering = new MainWindow();
            object? Call(string method, params object?[] args) => typeof(MainWindow).GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(ordering, args);
            void Field(string field, object? value) => typeof(MainWindow).GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(ordering, value);
            void Until(Func<bool> ready)
            {
                var pendingFrame = new DispatcherFrame(); var poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(10) };
                var deadline = DateTime.UtcNow.AddSeconds(5);
                poll.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) pendingFrame.Continue = false; };
                if (!ready()) { poll.Start(); Dispatcher.PushFrame(pendingFrame); poll.Stop(); }
                Check(ready(), "ordered delivery operation completes within test deadline");
            }
            bool Sending() => (bool)typeof(MainWindow).GetField("sending", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(ordering)!;
            bool LiveActive() => ((Dictionary<string, CancellationTokenSource>)typeof(MainWindow).GetField("liveStreams", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(ordering)!).ContainsKey("c");
            var transport = new ReplyReconnectHandler { DeferAcceptance = true };
            var server = new ServerClient("https://example.com/", "device-one", transport);
            using var connectionToken = new CancellationTokenSource();
            Field("client", server); Field("recoveryOwner", server); Field("connection", connectionToken); Field("currentConversation", "c"); Field("deviceId", "d");
            var orderingSettings = (Window)typeof(MainWindow).GetField("settings", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(ordering)!;
            ((CheckBox)orderingSettings.FindName("ScreenSharing")).IsChecked = false;
            var answer = (FrameworkElement)ordering.FindName("BubblePanel");
            var editor = (TextBox)ordering.FindName("Input");
            var timer = (DispatcherTimer)typeof(MainWindow).GetField("bubbleTimer", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(ordering)!;
            Call("SetConnected", true, "연결됨"); editor.Text = "빠른 답변을 기다리는 질문";
            Call("SendMessage"); Until(() => transport.Posts == 1);
            Check(Sending() && answer.Visibility == Visibility.Collapsed, "POST is accepted on server while its HTTP response is deliberately held");
            if (!doneBeforeAcceptance)
            {
                transport.ReleaseAcceptance(); Until(() => !Sending());
                Check(answer.Visibility == Visibility.Collapsed && !timer.IsEnabled, "202 for a running job does not open historical text or start reading timer");
            }
            transport.Complete = true;
            Call("EndMoveJob", "c", "j");
            transport.Live = (hasLiveBody ? "event: snapshot\ndata: {\"job_id\":\"j\",\"run_id\":\"r\",\"text\":\"복구된 최종 답변\",\"moods\":[]}\n\n" : "") + "event: done\ndata: {}\n\n";
            Call("StartLive", "c", server, connectionToken.Token, "j"); Until(() => !LiveActive());
            if (doneBeforeAcceptance)
            {
                Check(Sending() && (answer.Visibility == Visibility.Visible) == hasLiveBody, "job ended and Live done finish before HTTP 202 is released");
                if (hasLiveBody) Call("CloseBubble");
                transport.ReleaseAcceptance(); Until(() => !Sending());
            }
            bool shouldShow = !doneBeforeAcceptance || !hasLiveBody;
            Check((answer.Visibility == Visibility.Visible) == shouldShow && timer.IsEnabled == shouldShow, "both delivery orders recover unread finals once and preserve closure of already displayed Live");
            Check(((MarkdownView)ordering.FindName("Message")).Text == "복구된 최종 답변", "ordered delivery leaves the correct final text");
            if (shouldShow)
                Check(Grid.GetRow((FrameworkElement)ordering.FindName("UserBubble")) < Grid.GetRow(answer), "reply stays below its request regardless of HTTP acceptance and Live completion order");
            Call("CloseBubble");
            var refresh = (Task)Call("ReadConversation", "c", connectionToken.Token)!;
            Until(() => refresh.IsCompleted); refresh.GetAwaiter().GetResult();
            Check(answer.Visibility == Visibility.Collapsed && !timer.IsEnabled, "later refresh does not reopen the ordered delivery answer");
            ordering.Close();
        }
        PresentationChecks.Run(Check, args.Length == 2 && args[0] == "--screenshots" ? args[1] : null);
        if (args.Contains("--native-presentation")) FullscreenChecks.Run(Check);
        if (args.Length == 2 && args[0] == "--screenshots")
        {
            // Render actual controls without opening a connected app, using credentials,
            // saving preferences, registering hotkeys, or capturing the desktop.
            System.IO.Directory.CreateDirectory(args[1]);
            var preview = new MainWindow();
            object? Preview(string method, params object?[] values) => typeof(MainWindow).GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(preview, values);
            var previewSettings = (Window)typeof(MainWindow).GetField("settings", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(preview)!;
            ((CheckBox)previewSettings.FindName("ScreenSharing")).IsChecked = false;
            ((Slider)previewSettings.FindName("SizeSlider")).Value = 280;
            ((TextBlock)preview.FindName("Status")).Text = "연결됨";
            void Snapshot(string name)
            {
                var content = (FrameworkElement)preview.Content;
                for (int pass = 0; pass < 3; pass++)
                {
                    Preview("UpdateBubbleBounds");
                    content.Measure(new Size(520, 700)); content.Arrange(new Rect(0, 0, 520, 700)); content.UpdateLayout();
                }
                var drawing = new DrawingVisual();
                using (var dc = drawing.RenderOpen())
                {
                    dc.DrawRectangle(Brushes.WhiteSmoke, null, new Rect(0, 0, 520, 700));
                    var brush = new VisualBrush(content) { ViewboxUnits = BrushMappingMode.Absolute, Viewbox = new Rect(0, 0, 520, 700) };
                    dc.DrawRectangle(brush, null, new Rect(0, 0, 520, 700));
                }
                var image = new RenderTargetBitmap(520, 700, 96, 96, PixelFormats.Pbgra32); image.Render(drawing);
                var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(image));
                using var file = System.IO.File.Create(System.IO.Path.Combine(args[1], name + ".png")); png.Save(file);
            }
            Preview("PresentUserBubble"); Preview("ShowSentMessage", "안녕?"); Preview("OpenReplyBubble", "preview", "reply");
            ((MarkdownView)preview.FindName("Message")).Text = "안녕하세요, 선생님! 오늘도 잘 부탁드려요.";
            Snapshot("01-user-then-himari");
            Preview("UserBubbleExpired"); Snapshot("02-oldest-hidden");
            Preview("OpenComposer"); ((TextBox)preview.FindName("Input")).Text = "다음 이야기도 해 볼까?";
            Snapshot("03-new-draft-below-reply");
            preview.Close();
        }
        app.Shutdown();
        Console.WriteLine("WPF keyboard, IME, bubble colors/order/expiry, layout and reply reconnect checks passed.");
    }
}

internal sealed class ReplyReconnectHandler : HttpMessageHandler
{
    internal bool Complete;
    internal int Posts, Sequence = 1;
    internal string Job = "old", Live = "";
    internal bool DeferAcceptance;
    internal bool DeferCancel;
    private readonly TaskCompletionSource<HttpResponseMessage> cancellation = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal void ReleaseCancel() => cancellation.SetResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("{}") });
    private readonly TaskCompletionSource<HttpResponseMessage> acceptance = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal void ReleaseAcceptance() => acceptance.SetResult(new HttpResponseMessage(HttpStatusCode.Accepted) { Content = new StringContent("{\"job_id\":\"j\"}", Encoding.UTF8, "application/json") });
    private bool liveSent;
    // Reconnection replaces HttpClient but the fake server's state survives it.
    protected override void Dispose(bool disposing) { }
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        string body; var status = HttpStatusCode.OK;
        string path = request.RequestUri!.AbsolutePath;
        if (request.Method == HttpMethod.Post && path.EndsWith("/cancel") && DeferCancel) return cancellation.Task.WaitAsync(ct);
        if (request.Method == HttpMethod.Post && path.EndsWith("/messages"))
        {
            Posts++; Job = "j"; Sequence = 2;
            if (DeferAcceptance) return acceptance.Task.WaitAsync(ct);
            body = "{\"job_id\":\"j\"}"; status = HttpStatusCode.Accepted;
        }
        else if (path.EndsWith("/live"))
        {
            if (liveSent && !Live.Contains("event: done")) return UntilCancelled(ct);
            liveSent = true;
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(Live, Encoding.UTF8, "text/event-stream") });
        }
        else if (path.EndsWith("/events"))
            body = JsonSerializer.Serialize(new { items = new[] { new { sequence = Complete ? Math.Max(3, Sequence) : 1, kind = "assistant_final", display = new { text = Complete ? "복구된 최종 답변" : "이전 답변", moods = Array.Empty<object>() } } } });
        else
            body = JsonSerializer.Serialize(new { conversation = new { last_sequence = Complete ? Math.Max(3, Sequence) : Sequence }, job = new { running = Posts > 0 && !Complete, job_id = Job } });
        return Task.FromResult(new HttpResponseMessage(status) { Content = new StringContent(body, Encoding.UTF8, "application/json") });
    }
    private static async Task<HttpResponseMessage> UntilCancelled(CancellationToken ct)
    { await Task.Delay(Timeout.Infinite, ct); throw new OperationCanceledException(ct); }
}
