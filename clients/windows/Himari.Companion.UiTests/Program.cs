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
        if (args.Contains("--reply-progress-only"))
        {
            ReplyProgressChecks.Run((ok, message) => { if (!ok) throw new Exception(message); }, args.Length == 3 && args[1] == "--screenshots" ? args[2] : null);
            Console.WriteLine("Reply preparation UI checks passed."); app.Shutdown(); return;
        }
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
        void Compose(RoutedEvent stage) => input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, composition) { RoutedEvent = stage });
        void Pump()
        {
            var frame = new DispatcherFrame();
            Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, new Action(() => frame.Continue = false));
            Dispatcher.PushFrame(frame);
        }
        Compose(TextCompositionManager.PreviewTextInputStartEvent);
        Check(Key(System.Windows.Input.Key.Enter).Handled && sent == 1, "Enter during IME composition never reaches the multiline textbox");
        Check(!Key(System.Windows.Input.Key.ImeProcessed).Handled && sent == 1, "IME-processed key is left to IME");
        Compose(TextCompositionManager.PreviewTextInputEvent);
        Pump();
        Check(sent == 2, "Enter during a composition sends once the commit has landed");
        // Korean IME commits the last syllable and passes the same Enter on.
        Compose(TextCompositionManager.PreviewTextInputStartEvent);
        Compose(TextCompositionManager.PreviewTextInputEvent);
        Check(Key(System.Windows.Input.Key.Enter).Handled && sent == 2, "Korean commit Enter is not left to the textbox as a newline");
        Pump();
        Check(sent == 3, "Korean commit Enter sends after the commit");
        Compose(TextCompositionManager.PreviewTextInputStartEvent);
        modifiers = ModifierKeys.Shift;
        Check(!Key(System.Windows.Input.Key.Enter).Handled && sent == 3, "Shift+Enter during a composition is left to multiline textbox");
        modifiers = ModifierKeys.None;
        Compose(TextCompositionManager.PreviewTextInputEvent);
        Pump();
        Check(Key(System.Windows.Input.Key.Enter).Handled && sent == 4 && routed == 7, "Enter after the commit settles sends at once on real routed event path");
        var normal = new TextComposition(InputManager.Current, input, "a", TextCompositionAutoComplete.On);
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, normal) { RoutedEvent = TextCompositionManager.PreviewTextInputStartEvent });
        input.RaiseEvent(new TextCompositionEventArgs(Keyboard.PrimaryDevice, normal) { RoutedEvent = TextCompositionManager.PreviewTextInputEvent });
        Check(Key(System.Windows.Input.Key.Enter).Handled && sent == 5, "ordinary text followed immediately by Enter is not mistaken for IME");
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
        // Scope reply-flow verification independently of artwork and foreground-input checks.
        if (!args.Contains("--reply-flow-only"))
        {
            // Character art (docs/companion-animation-v0.1.md §7): every piece lies inside the sheet,
            // every frame and mood renders, and composed poses match export.lua's references pixel for pixel.
            var renderer = (CharacterRenderer)typeof(MainWindow).GetField("character", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(timeline)!;
            var manifest = renderer.Manifest;
            var sheetImage = BitmapFrame.Create(Application.GetResourceStream(new Uri("pack://application:,,,/Himari.Companion;component/Assets/" + manifest.Sheet))!.Stream);
            bool InSheet(int[] src) => src[0] >= 0 && src[1] >= 0 && src[2] > 0 && src[3] > 0 && src[0] + src[2] <= sheetImage.PixelWidth && src[1] + src[3] <= sheetImage.PixelHeight;
            var pieces = manifest.Clips.Values.SelectMany(c => c.Frames).Select(f => f.Src)
                .Concat(manifest.Faces.Values.SelectMany(f => new[] { f.Src }.Concat((f.Eyes?.Values ?? Enumerable.Empty<FaceVariant>()).Concat(f.Mouth?.Values ?? Enumerable.Empty<FaceVariant>()).Select(v => v.Src)))).ToList();
            Check(pieces.All(InSheet) && manifest.Clips.Values.SelectMany(c => c.Frames).All(f => f.Src[2] == manifest.Width && f.Src[3] == manifest.Height),
                "every character piece lies inside the sheet and body frames fill the canvas");
            foreach (var (clipName, clip) in manifest.Clips)
                for (int i = 0; i < clip.Frames.Length; i++)
                    foreach (var (moodName, face) in manifest.Faces)
                        foreach (var eyes in (face.Eyes?.Keys ?? Enumerable.Empty<string>()).Prepend(null))
                            foreach (var mouth in (face.Mouth?.Keys ?? Enumerable.Empty<string>()).Prepend(null))
                                renderer.Render(new CharacterPose(clipName, i, moodName, eyes, mouth));
            int[] Pixels(BitmapSource source)
            {
                var bgra = new FormatConvertedBitmap(source, PixelFormats.Bgra32, null, 0);
                var pixels = new int[bgra.PixelWidth * bgra.PixelHeight];
                bgra.CopyPixels(pixels, bgra.PixelWidth * 4, 0);
                return pixels;
            }
            foreach (var (file, pose) in new[] {
                ("idle-1-neutral", new CharacterPose("idle", 0, "neutral", null, null)),
                ("idle-2-happy", new CharacterPose("idle", 1, "happy", null, null)),
                ("idle-1-neutral-closed-wide", new CharacterPose("idle", 0, "neutral", "closed", "wide")),
                ("sleep-4-neutral-closed", new CharacterPose("sleep", 3, "neutral", "closed", null)),
                ("yawn-2-happy", new CharacterPose("yawn", 1, "happy", null, null)),
                ("glance-2-sad-left", new CharacterPose("glance", 1, "sad", "left", null)),
                ("ahoge-2-neutral", new CharacterPose("ahoge", 1, "neutral", null, null)),
                ("twinkle-3-neutral", new CharacterPose("twinkle", 2, "neutral", null, null)),
                ("stretch-4-sad-closed", new CharacterPose("stretch", 3, "sad", "closed", null)),
                ("stretch-5-worried", new CharacterPose("stretch", 4, "worried", null, null)),
                ("stretch-8-happy", new CharacterPose("stretch", 7, "happy", null, null)),
                ("stretch-9-neutral", new CharacterPose("stretch", 8, "neutral", null, null)) })
            {
                renderer.Render(pose);
                using var reference = System.IO.File.OpenRead(System.IO.Path.Combine(AppContext.BaseDirectory, "Fixtures", "character", file + ".png"));
                Check(Pixels(renderer.Bitmap).SequenceEqual(Pixels(BitmapFrame.Create(reference, BitmapCreateOptions.None, BitmapCacheOption.OnLoad))), $"{file} matches the exported reference pixel for pixel");
            }
            var characterImage = (Image)timeline.FindName("Character");
            Check(characterImage.Source == renderer.Bitmap && renderer.Bitmap.PixelWidth == 205 && renderer.Bitmap.PixelHeight == 206, "the character image shows the composed 205x206 bitmap");
            Timeline("SetMood", "happy"); Timeline("SetTalking", true);
            Check(renderer.Shown is { Mood: "happy", Mouth: "small" }, "mood reaches the character and every mood talks");
            Timeline("SetMood", "neutral"); Timeline("SetTalking", true);
            Check(renderer.Shown is { Mood: "neutral", Mouth: "small" }, "talking starts the mouth at once");
            Timeline("SetTalking", false);
            var inspect = (SettingsWindow)typeof(MainWindow).GetField("settings", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(timeline)!;
            inspect.EyesSelector.SelectedIndex = 3; inspect.MouthSelector.SelectedIndex = 3;
            Check(renderer.Shown is { Eyes: "closed", Mouth: "wide" } && inspect.PoseText.Text.Contains("closed"), "the animation preview holds eyes and mouth on a chosen variant");
            Timeline("SetMood", "happy");
            Check(renderer.Shown is { Mood: "happy", Eyes: null, Mouth: "wide" } && inspect.PoseText.Text.Contains("그림이 없어"), "the preview says when a mood lacks the chosen variant (happy has no blink art)");
            Timeline("SetMood", "neutral");
            inspect.PausePreview.IsChecked = true;
            var paused = renderer.Shown!;
            inspect.StepButton.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent));
            Check(renderer.Shown != paused && renderer.Shown!.Frame != paused.Frame, "stepping a paused preview shows the next body frame");
            inspect.PausePreview.IsChecked = false; inspect.EyesSelector.SelectedIndex = 0; inspect.MouthSelector.SelectedIndex = 0;
        }
        var oldUser = (FrameworkElement)timeline.FindName("UserBubble");
        var reply = (FrameworkElement)timeline.FindName("BubblePanel");
        var draftInput = (TextBox)timeline.FindName("Input");
        var replyTimer = (DispatcherTimer)typeof(MainWindow).GetField("bubbleTimer", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(timeline)!;
        Timeline("OpenComposer"); draftInput.Text = "첫 질문"; draftInput.Clear(); Timeline("ShowSentMessage", "첫 질문");
        Timeline("OpenReplyBubble", "c", "j1");
        Check(Grid.GetRow(oldUser) < Grid.GetRow(reply), "reply arrives beneath the earlier user message");
        Timeline("ShowSentMessage", "첫 질문");
        Check(Grid.GetRow(oldUser) < Grid.GetRow(reply), "late send acknowledgement preserves chronological order");
        var originalParent = oldUser.Parent;
        Timeline("OpenComposer"); draftInput.Text = "다음 초안"; draftInput.Select(1, 3);
        Timeline("OpenReplyBubble", "c", "j1");
        Check(Grid.GetRow(reply) < Grid.GetRow(oldUser) && oldUser.Parent == originalParent && draftInput.SelectedText == "음 초", "new draft moves below the prior reply and reconnecting that same reply neither reorders nor reparents its editor");
        Timeline("OpenReplyBubble", "c", "j2");
        Check(Grid.GetRow(reply) < Grid.GetRow(oldUser), "a new reply comes in above the open editor, which always stays last");
        // The first message went into the stack when the editor reopened; the single-bubble checks below start without it.
        Timeline("ClearHistory");
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
                    assistantBounds.Bottom <= userBounds.Top, "the open editor stays below the reply on the same center line whichever came last");
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
                Check(answer.Visibility == Visibility.Visible && ((FrameworkElement)ordering.FindName("ReplyProgress")).Visibility == Visibility.Visible && ((MarkdownView)ordering.FindName("Message")).Text == "" && !timer.IsEnabled, "202 opens a preparation bubble without historical text or a reading timer");
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
        {
            // A message the server did not take is never lost or shown as sent, and one it did take
            // is never offered again (PR #154 review).
            var unsentWindow = new MainWindow();
            object? Call(string method, params object?[] args) => typeof(MainWindow).GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(unsentWindow, args);
            void Field(string field, object? value) => typeof(MainWindow).GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(unsentWindow, value);
            object? Get(string field) => typeof(MainWindow).GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(unsentWindow);
            void Until(Func<bool> ready)
            {
                var unsentFrame = new DispatcherFrame(); var poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(10) };
                var deadline = DateTime.UtcNow.AddSeconds(5);
                poll.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) unsentFrame.Continue = false; };
                if (!ready()) { poll.Start(); Dispatcher.PushFrame(unsentFrame); poll.Stop(); }
                Check(ready(), "unsent message operation completes within test deadline");
            }
            bool Sending() => (bool)Get("sending")!;
            var transport = new ReplyReconnectHandler { DeferAcceptance = true };
            var server = new ServerClient("https://example.com/", "device-one", transport);
            using var connectionToken = new CancellationTokenSource();
            Field("client", server); Field("recoveryOwner", server); Field("connection", connectionToken); Field("currentConversation", "c"); Field("deviceId", "d");
            ((CheckBox)((Window)Get("settings")!).FindName("ScreenSharing")).IsChecked = false;
            var editor = (TextBox)unsentWindow.FindName("Input"); var sentLine = (TextBlock)unsentWindow.FindName("UserSent");
            var history = (System.Collections.IList)Get("history")!;
            object? Past(int index, string property) => history[index]!.GetType().GetProperty(property, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(history[index]);
            Call("SetConnected", true, "연결됨");
            editor.Text = "A 질문"; Call("SendMessage"); Until(() => transport.Posts == 1);
            Call("OpenComposer"); editor.Text = "B 초안";
            transport.ReleaseFailure(HttpStatusCode.TooManyRequests, "busy"); Until(() => !Sending());
            Check(editor.Text == "B 초안" && history.Count == 1 && (string?)Past(0, "Text") == "A 질문" && (string?)Past(0, "UnsentConversation") == "c" && sentLine.Visibility == Visibility.Collapsed,
                "A sent, B typed, A refused: B stays in the editor and A stays as an unsent bubble, not a sent one");
            if (args.Length == 2 && args[0] == "--screenshots")
            {
                var unsentStack = (FrameworkElement)unsentWindow.FindName("History");
                unsentStack.Measure(new Size(304, double.PositiveInfinity)); unsentStack.Arrange(new Rect(unsentStack.DesiredSize)); unsentStack.UpdateLayout();
                var image = new RenderTargetBitmap((int)Math.Ceiling(unsentStack.ActualWidth), (int)Math.Ceiling(unsentStack.ActualHeight), 96, 96, PixelFormats.Pbgra32); image.Render(unsentStack);
                var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(image)); System.IO.Directory.CreateDirectory(args[1]);
                using var file = System.IO.File.Create(System.IO.Path.Combine(args[1], "13-unsent-message.png")); png.Save(file);
            }
            Call("PastBubbleExpired", 0);
            Check(history.Count == 1, "an unsent message does not expire");
            var retry = (Button)Past(0, "RetryButton")!;
            Check(retry.IsEnabled, "an unsent message can be sent again in its own conversation");
            transport.DeferAcceptance = false;
            retry.RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Until(() => transport.Posts == 2 && !Sending());
            Check(editor.Text == "B 초안" && history.Count == 1 && Past(0, "UnsentConversation") is null, "sending it again from its bubble leaves the newer draft in place and shows it as sent");
            editor.Clear(); Call("ClearHistory");
            transport.Fail = (HttpStatusCode.BadGateway, "http_error");
            editor.Text = "C 질문"; Call("SendMessage"); Until(() => transport.Posts == 3 && !Sending());
            Check(editor.Text == "C 질문" && Get("unknownText") is not null, "a send whose outcome is unknown returns to an empty editor");
            transport.Fail = null; transport.AcceptedText = "C 질문";
            Call("SendMessage"); Until(() => !Sending());
            Check(transport.Posts == 3 && editor.Text == "" && Get("unknownText") is null && sentLine.Visibility == Visibility.Visible && sentLine.Text == "C 질문",
                "a resend that finds the earlier send recorded neither posts again nor puts it back in the editor");
            transport.AcceptedText = null; transport.ResetAcceptance(); transport.DeferAcceptance = true;
            editor.Text = "D 질문"; Call("SendMessage"); Until(() => transport.Posts == 4);
            Call("OpenComposer"); editor.Text = "E 초안";
            Check(Get("frozenSent") is not null, "typing during a send moves the message into the stack");
            Call("SetCurrentConversation", "other"); Call("SetCurrentConversation", "c");
            Check(Get("frozenSent") is null && history.Count == 0, "switching conversations drops the stack and its reference to the message being sent");
            editor.Clear(); transport.ReleaseAcceptance(); Until(() => !Sending());
            Check(sentLine.Visibility == Visibility.Visible && sentLine.Text == "D 질문", "a late acceptance after switching back shows the message instead of trusting a removed bubble");
            // Same text twice: the first is refused, the second's outcome is unknown and later found recorded.
            Call("HideUserBubble"); Call("ClearHistory"); editor.Clear();
            transport.ResetAcceptance(); editor.Text = "같은 질문"; Call("SendMessage"); Until(() => transport.Posts == 5);
            Call("OpenComposer"); editor.Text = "초안 1";
            transport.ReleaseFailure(HttpStatusCode.TooManyRequests, "busy"); Until(() => !Sending());
            editor.Clear(); transport.ResetAcceptance(); editor.Text = "같은 질문"; Call("SendMessage"); Until(() => transport.Posts == 6);
            Call("OpenComposer"); editor.Text = "초안 2";
            transport.ReleaseFailure(HttpStatusCode.BadGateway, "http_error"); Until(() => !Sending());
            Check(history.Count == 2 && (string?)Past(0, "UnsentConversation") == "c" && (string?)Past(1, "UnsentConversation") == "c", "both attempts with the same text stay unsent");
            transport.AcceptedText = "같은 질문";
            var check = (Task)Call("CheckUnknownText", false, CancellationToken.None)!; Until(() => check.IsCompleted);
            Check((string?)Past(0, "UnsentConversation") == "c" && Past(1, "UnsentConversation") is null,
                "confirming the unknown attempt marks only its own bubble sent, never the refused one with the same text");
            transport.AcceptedText = null;
            // More unsent messages than the stack may show: none is lost, at most six bubbles are shown.
            // The fake server leaves accepted jobs running; dismiss their preparation
            // bubble to isolate the unsent-message capacity scenario.
            Call("CloseBubble");
            Call("ClearHistory"); editor.Text = "남은 초안";
            for (int i = 0; i < 8; i++) Call("KeepUnsent", $"못 보낸 {i}", "c", null, false);
            var stack = (StackPanel)unsentWindow.FindName("History");
            int Shown() => stack.Children.Cast<UIElement>().Count(child => child.Visibility == Visibility.Visible)
                + (((FrameworkElement)unsentWindow.FindName("UserBubble")).Visibility == Visibility.Visible ? 1 : 0)
                + (((FrameworkElement)unsentWindow.FindName("BubblePanel")).Visibility == Visibility.Visible ? 1 : 0);
            Check(history.Count == 8 && Shown() == 6 && ((UIElement)Past(0, "Bubble")!).Visibility == Visibility.Collapsed && ((UIElement)Past(7, "Bubble")!).Visibility == Visibility.Visible,
                "unsent messages past the six-bubble limit are kept hidden, oldest first");
            Check(((TextBlock)Past(3, "UnsentNote")!).Text.Contains("3개 더"), "the oldest shown unsent message says how many wait hidden");
            ((Button)((StackPanel)((Button)Past(7, "RetryButton")!).Parent).Children[2]).RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Check(history.Count == 7 && Shown() == 6 && ((UIElement)Past(2, "Bubble")!).Visibility == Visibility.Visible, "removing one brings a hidden unsent message back");
            // Unknown A, sent again from its bubble, the resend declined, then A found recorded.
            Call("ClearHistory"); Call("HideUserBubble"); editor.Clear();
            transport.ResetAcceptance(); transport.DeferAcceptance = true; editor.Text = "확인 안 된 질문"; Call("SendMessage"); Until(() => Sending() && transport.Posts == 7);
            Call("OpenComposer"); editor.Text = "다음 초안";
            transport.ReleaseFailure(HttpStatusCode.BadGateway, "http_error"); Until(() => !Sending());
            var attempt = history[0];
            Field("confirmResend", (Func<bool>)(() => false));
            ((Button)Past(0, "RetryButton")!).RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Until(() => !Sending());
            Check(transport.Posts == 7 && history.Count == 1 && ReferenceEquals(history[0], attempt) && (string?)Past(0, "UnsentConversation") == "c" && editor.Text == "다음 초안",
                "a declined resend leaves the same bubble unsent and the draft untouched");
            transport.AcceptedText = "확인 안 된 질문";
            var found = (Task)Call("CheckUnknownText", false, CancellationToken.None)!; Until(() => found.IsCompleted);
            Check(history.Count == 1 && Past(0, "UnsentConversation") is null && ((UIElement)Past(0, "UnsentPanel")!).Visibility == Visibility.Collapsed && sentLine.Visibility != Visibility.Visible,
                "when the attempt is later found recorded, that same bubble becomes sent and loses its resend action, with no second copy");
            // Unknown A back in the editor, then typed again by the user as a new draft before A is found recorded.
            Field("confirmResend", null); transport.AcceptedText = null; Call("ClearHistory"); Call("HideUserBubble"); editor.Clear();
            transport.DeferAcceptance = false; transport.Fail = (HttpStatusCode.BadGateway, "http_error");
            editor.Text = "다시 쓴 질문"; Call("SendMessage"); Until(() => !Sending());
            Check(editor.Text == "다시 쓴 질문" && Get("unknownText") is not null, "the unknown attempt's text returns to the empty editor");
            editor.Clear(); editor.Text = "다시 쓴 질문"; transport.Fail = null; transport.AcceptedText = "다시 쓴 질문";
            found = (Task)Call("CheckUnknownText", false, CancellationToken.None)!; Until(() => found.IsCompleted);
            Check(editor.Text == "다시 쓴 질문" && Get("unknownText") is null, "confirming the earlier attempt never clears a new draft that merely has the same text");
            // Six unsent messages and an open editor fill the stack; one is sent again and falls due on its way.
            Call("ClearHistory"); Call("HideUserBubble"); Call("OpenComposer"); editor.Text = "열린 초안";
            for (int i = 0; i < 6; i++) Call("KeepUnsent", $"묶음 {i}", "c", null, false);
            var resent = history[5]; int posts = transport.Posts;
            transport.ResetAcceptance(); transport.DeferAcceptance = true;
            ((Button)Past(5, "RetryButton")!).RaiseEvent(new RoutedEventArgs(Button.ClickEvent)); Until(() => Sending() && transport.Posts == posts + 1);
            Check(history.Contains(resent) && ((UIElement)Past(history.IndexOf(resent), "Bubble")!).Visibility == Visibility.Visible && history.Count == 6 && Shown() == 6,
                "the six-bubble limit never trims the message being sent again, and hides an older unsent one instead");
            Call("PastBubbleExpired", history.IndexOf(resent));
            Check(history.Contains(resent), "a bubble falling due on its way stays until the send ends");
            transport.ReleaseAcceptance(); Until(() => !Sending());
            Check(!history.Contains(resent) && history.Count == 5 && Enumerable.Range(0, 5).All(i => (string?)Past(i, "UnsentConversation") == "c") && editor.Text == "열린 초안",
                "once accepted, the overdue bubble expires at once and every other unsent message is kept");
            unsentWindow.Close();
        }
        ReplyProgressChecks.Run(Check, args.Length == 2 && args[0] == "--screenshots" ? args[1] : null);
        if (!args.Contains("--reply-flow-only")) PresentationChecks.Run(Check, args.Length == 2 && args[0] == "--screenshots" ? args[1] : null);
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
        // V3-P87 §10 (PR #159 review P1): following a wake checks again once the conversation list is read.
        foreach (string meanwhile in new[] { "nothing", "typed", "picked" })
        {
            var follow = new MainWindow();
            object? Call(string method, params object?[] arguments) => typeof(MainWindow).GetMethod(method, BindingFlags.NonPublic | BindingFlags.Instance)!.Invoke(follow, arguments);
            void Field(string field, object? value) => typeof(MainWindow).GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(follow, value);
            object? Value(string field) => typeof(MainWindow).GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(follow);
            void Until(Func<bool> ready)
            {
                var untilFrame = new DispatcherFrame(); var poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(10) };
                var deadline = DateTime.UtcNow.AddSeconds(5);
                poll.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) untilFrame.Continue = false; };
                if (!ready()) { poll.Start(); Dispatcher.PushFrame(untilFrame); poll.Stop(); }
                Check(ready(), "asynchronous UI operation completed within test deadline");
            }
            var slow = new SlowListHandler();
            var local = new ServerClient("https://example.com/", "device-one", slow);
            using var followToken = new CancellationTokenSource();
            Field("client", local); Field("connection", followToken); Field("currentConversation", "a"); Field("deviceId", "d");
            var editor = (TextBox)follow.FindName("Input");
            var following = (Task)Call("FollowWake", "b", local, followToken.Token)!;
            Until(() => slow.ListAsked);  // the list is on its way
            if (meanwhile == "typed") editor.Text = "A에 보낼 말";
            if (meanwhile == "picked") Call("SetCurrentConversation", "c");
            slow.ReleaseList();
            Until(() => following.IsCompleted); following.GetAwaiter().GetResult();
            var shown = (string?)Value("currentConversation");
            if (meanwhile == "nothing") Check(shown == "b", "a wake moves the app to its conversation when nothing changed meanwhile");
            else
            {
                Check(shown == (meanwhile == "picked" ? "c" : "a"), "typing or picking a conversation while the list is read keeps the user where they are");
                Check(((TextBlock)follow.FindName("Status")).Text.Contains("다른 대화에서"), "the status line tells where HIMARI spoke instead");
            }
            if (meanwhile == "typed")
            {
                Check(editor.Text == "A에 보낼 말", "the draft stays with the conversation it was written in");
                // PR #159 re-review P2: reading A afterwards (as hello and activity do) keeps the notice
                Field("connected", true);  // connected, the read writes the status line
                var reading = (Task)Call("ReadConversation", "a", followToken.Token)!;
                Until(() => reading.IsCompleted); reading.GetAwaiter().GetResult();
                Check(((TextBlock)follow.FindName("Status")).Text == JobOrigin.WakeNotice, "reading the shown conversation does not wipe the wake notice");
                Call("SetCurrentConversation", "b");
                Check((string?)Value("wakeElsewhere") is null, "opening that conversation ends the notice");
            }
            follow.Close();
        }
        app.Shutdown();
        Console.WriteLine("WPF keyboard, IME, bubble colors/order/expiry, layout, reply reconnect and wake follow checks passed.");
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
    private TaskCompletionSource<HttpResponseMessage> acceptance = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal void ReleaseAcceptance() => acceptance.SetResult(new HttpResponseMessage(HttpStatusCode.Accepted) { Content = new StringContent("{\"job_id\":\"j\"}", Encoding.UTF8, "application/json") });
    internal void ReleaseFailure(HttpStatusCode status, string code) => acceptance.SetResult(Error(status, code));
    internal void ResetAcceptance() => acceptance = new(TaskCreationOptions.RunContinuationsAsynchronously);
    // A POST that fails at once, and a user message the server's history shows as recorded.
    internal (HttpStatusCode Status, string Code)? Fail;
    internal string? AcceptedText;
    private static HttpResponseMessage Error(HttpStatusCode status, string code) => new(status) { Content = new StringContent(JsonSerializer.Serialize(new { error = new { code } }), Encoding.UTF8, "application/json") };
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
            if (Fail is { } fail) return Task.FromResult(Error(fail.Status, fail.Code));
            body = "{\"job_id\":\"j\"}"; status = HttpStatusCode.Accepted;
        }
        else if (path.EndsWith("/live"))
        {
            if (liveSent && !Live.Contains("event: done")) return UntilCancelled(ct);
            liveSent = true;
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(Live, Encoding.UTF8, "text/event-stream") });
        }
        else if (path.EndsWith("/events"))
        {
            var items = new List<object> { new { sequence = Complete ? Math.Max(3, Sequence) : 1, kind = "assistant_final", display = new { text = Complete ? "복구된 최종 답변" : "이전 답변", moods = Array.Empty<object>() } } };
            if (AcceptedText is not null) items.Add(new { sequence = 10, kind = "user_message", text = AcceptedText, origin = new { kind = "device", device_id = "d" } });
            body = JsonSerializer.Serialize(new { items });
        }
        else
            body = JsonSerializer.Serialize(new { conversation = new { last_sequence = Complete ? Math.Max(3, Sequence) : Sequence }, job = new { running = Posts > 0 && !Complete, job_id = Job } });
        return Task.FromResult(new HttpResponseMessage(status) { Content = new StringContent(body, Encoding.UTF8, "application/json") });
    }
    private static async Task<HttpResponseMessage> UntilCancelled(CancellationToken ct)
    { await Task.Delay(Timeout.Infinite, ct); throw new OperationCanceledException(ct); }
}

/// <summary>V3-P87 §10: a server whose conversation list comes only when released.</summary>
internal sealed class SlowListHandler : HttpMessageHandler
{
    private readonly TaskCompletionSource list = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal volatile bool ListAsked;
    internal void ReleaseList() => list.SetResult();
    protected override void Dispose(bool disposing) { }
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        string path = request.RequestUri!.AbsolutePath, body;
        if (path == "/api/conversations")
        {
            ListAsked = true;
            await list.Task.WaitAsync(ct);
            body = JsonSerializer.Serialize(new { items = new[] { new { id = "a", title = "A" }, new { id = "b", title = "B" }, new { id = "c", title = "C" } } });
        }
        else if (path.EndsWith("/events")) body = JsonSerializer.Serialize(new { items = Array.Empty<object>() });
        else body = JsonSerializer.Serialize(new { conversation = new { last_sequence = 1 }, job = new { running = false, job_id = "w" } });
        return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(body, Encoding.UTF8, "application/json") };
    }
}
