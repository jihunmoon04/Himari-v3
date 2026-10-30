using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Threading;
using System.Reflection;
using System.Windows.Media;
using Himari.Companion;

internal static class Program
{
    [STAThread] private static void Main()
    {
        var app = new App(); app.InitializeComponent();
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
        ((TextBlock)companion.FindName("Message")).Text = new string('가', 1500);
        ((TextBox)companion.FindName("Input")).Text = "작성 중인 글\n둘째 줄";
        foreach (bool flipped in new[] { false, true })
        {
            ((CheckBox)settings.FindName("FacingLeft")).IsChecked = flipped;
            Check(((ScaleTransform)character.RenderTransform).ScaleX == (flipped ? -1 : 1), "character mirrors");
            Check(assistant.HorizontalAlignment == HorizontalAlignment.Center && user.HorizontalAlignment == HorizontalAlignment.Center && assistant.Width == user.Width, "speakers share a column and bubble width without mirroring text");
            Check(((PixelBubble)assistant).TailRight == flipped && ((PixelBubble)user).TailRight != flipped, "assistant tail points toward character and user tail points away");
            foreach (int size in new[] { 200, 400 })
            {
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
                Check(Math.Abs(assistantBounds.Left + assistantBounds.Width / 2 - userBounds.Left - userBounds.Width / 2) < 1 && assistantBounds.Bottom <= userBounds.Top, "assistant stays above user on the same center line");
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
        Check(userStatus.Visibility == Visibility.Visible && userStatus.Text == status.Text, "screen delivery warnings remain visible inside user bubble");
        companion.Close(); app.Shutdown();
        Console.WriteLine("WPF keyboard, synthetic IME and dual-bubble layout checks passed.");
    }
}
