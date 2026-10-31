using System.Windows;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Windows.Data;
using System.Windows.Media;
using System.IO;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

public partial class MainWindow : Window
{
    private static readonly string[] Moods = ["neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried"];
    // Rescheduled after every frame to wake exactly when the pose next changes.
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromMilliseconds(30) };
    private readonly System.Diagnostics.Stopwatch clock = System.Diagnostics.Stopwatch.StartNew();
    private readonly CharacterRenderer character = CharacterRenderer.Load();
    private readonly CharacterAnimator animator;
    private readonly Forms.NotifyIcon tray;
    private readonly SettingsWindow settings;
    private System.Windows.Controls.Panel PairPanel => settings.PairPanel;
    private System.Windows.Controls.TextBox ServerAddress => settings.ServerAddress;
    private System.Windows.Controls.TextBox PairCode => settings.PairCode;
    private System.Windows.Controls.Button PairButton => settings.PairButton;
    private System.Windows.Controls.Slider SizeSlider => settings.SizeSlider;
    private System.Windows.Controls.ComboBox MoodSelector => settings.MoodSelector;
    private System.Windows.Point dragStart;
    private bool dragInProgress;
    private bool loadedOnce;
    private string mood = "neutral";
    private bool talking;
    private bool sleeping;
    private DateTime lastIdleCheck = DateTime.MinValue;
    private int demoVersion;
    private int chatFocusVersion;

    public MainWindow() : this(false) { }
    internal MainWindow(bool offline)
    {
        InitializeComponent();
        animator = new CharacterAnimator(character.Manifest, Environment.TickCount, TimeSpan.Zero);
        animationClockRead = clock.Elapsed;
        Character.Source = character.Bitmap;
        _ = new ComposerInput(Input, SendMessage);
        SetupTypeToChat(this);
        Message.SourceChanged += (_, _) => { replyReader?.UpdateText(Message.Text); UpdateBubblePresentation(); };
        settings = new SettingsWindow();
        settings.ConnectionStatus.SetBinding(System.Windows.Controls.TextBlock.TextProperty, new System.Windows.Data.Binding("Text") { Source = Status });
        settings.PairButton.Click += PairClick;
        settings.ReconnectButton.Click += (_, _) => Reconnect();
        settings.ChangeServerButton.Click += (_, _) => { PairPanel.Visibility = Visibility.Visible; ServerAddress.Focus(); };
        settings.WebButton.Click += OpenWebClick;
        settings.SizeSlider.ValueChanged += CharacterSizeChanged;
        settings.MoodSelector.SelectionChanged += MoodChanged;
        settings.DemoButton.Click += DemoClick;
        settings.MoveDemoButton.Click += MoveDemoClick;
        settings.MoveEnabled.Unchecked += (_, _) => { if (autoMoveActive) ReturnFromMove(); else CancelAutomaticMove(); SchedulePreferencesSave(); };
        settings.MoveEnabled.Checked += (_, _) => SchedulePreferencesSave();
        SetupAnimationPreview();
        SetupPresentationControls();
        SetupBubblePresentation();
        Character.MouseRightButtonDown += (_, _) => captureFromWorkWindow = false;
        ApplyCharacterTheme();
        MoodSelector.ItemsSource = Moods;
        MoodSelector.SelectedIndex = 0;
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("보이기 / 숨기기", null, (_, _) => ToggleVisible());
        menu.Items.Add("설정", null, (_, _) => ShowSettings());
        menu.Items.Add("다시 연결", null, (_, _) => Reconnect());
        menu.Items.Add("Web 열기", null, (_, _) => OpenWeb());
        menu.Items.Add("끝내기", null, (_, _) => Close());
        tray = new Forms.NotifyIcon { Icon = System.Drawing.SystemIcons.Application, Text = "히마리 · Alt+Q", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ToggleVisible();
        timer.Tick += (_, _) => UpdateFrame();
        timer.Start();
        SourceInitialized += (_, _) => { if (!offline) { SetupWindowControls(); SetupForegroundTracking(); } };
        Loaded += async (_, _) => { if (loadedOnce) return; loadedOnce = true; InitializeDialogueHost(); UpdateSceneBounds(); if (!offline) { if (!preferencesLoaded) SetupWindowControls(); SetupChatHotkey(); await RestoreConnection(); } };
        Closing += (_, _) => { settings.AllowClose = true; };
        Closing += (_, _) => RestoreMoveHomeOnClose();
        Closed += (_, _) => { CleanupBubblePresentation(); replyReader?.Dispose(); CleanupForegroundTracking(); dialogueHost?.Close(); CancelMovePreview(); lifetime.Cancel(); StopConnection(); timer.Stop(); CleanupWindowControls(); tray.Visible = false; tray.Dispose(); menu.Dispose(); };
        IsVisibleChanged += (_, _) => UpdateSceneBounds();
        Activated += (_, _) =>
        {
            UpdateFullscreenPresentation();
            // Alt+Tab selects the character window; over fullscreen hand focus to the dialogue host.
            if (CoverScreen() is not null && ComposerPanel.Visibility != Visibility.Visible) Dispatcher.BeginInvoke(new Action(OpenChat));
            else FocusComposerIfOpen();
        };
        UpdateFrame();
    }

    private void ToggleVisible() { if (IsVisible) Hide(); else { Show(); Activate(); } }
    private void DragWindow(object sender, MouseButtonEventArgs e) { if (e.ChangedButton == MouseButton.Left) DragMove(); }
    private void CharacterMouseDown(object sender, MouseButtonEventArgs e)
    {
        captureFromWorkWindow = false;
        dragStart = e.GetPosition(this); dragInProgress = false; Character.CaptureMouse(); e.Handled = true;
    }
    private void CharacterMouseMove(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (!Character.IsMouseCaptured || e.LeftButton != MouseButtonState.Pressed) return;
        var p = e.GetPosition(this);
        if (Math.Abs(p.X - dragStart.X) < SystemParameters.MinimumHorizontalDragDistance && Math.Abs(p.Y - dragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;
        dragInProgress = true; CancelMovePreview(); Character.ReleaseMouseCapture(); DragMove();
    }
    private void CharacterMouseUp(object sender, MouseButtonEventArgs e)
    {
        if (Character.IsMouseCaptured) Character.ReleaseMouseCapture();
        if (!dragInProgress) OpenChat(); e.Handled = true;
    }
    private void OpenChat()
    {
        int version = ++chatFocusVersion;
        if (awaitingAnswer && !string.IsNullOrWhiteSpace(Message.Text)) ShowAssistantBubble(assistantBubbleKey);
        OpenComposer();
        if (dialogueHost is not null && IsVisible) dialogueHost.Activate();
        // The first time the composer appears its editor is not laid out yet and refuses focus.
        if (!Input.Focus()) Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() =>
        {
            if (version == chatFocusVersion && ComposerPanel.Visibility == Visibility.Visible && IsChatWindow(GetForegroundWindow())) Input.Focus();
        }));
    }
    private void FocusComposerIfOpen()
    {
        int version = chatFocusVersion;
        if (ComposerPanel.Visibility == Visibility.Visible)
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => { if (version == chatFocusVersion && ComposerPanel.Visibility == Visibility.Visible && IsChatWindow(GetForegroundWindow())) { dialogueHost?.Activate(); Input.Focus(); } }));
    }
    private void OpenComposer()
    {
        if (UserBubble.Visibility != Visibility.Visible && string.IsNullOrWhiteSpace(Input.Text)) { UserSent.Text = ""; UserSent.Visibility = Visibility.Collapsed; }
        UserSent.Visibility = Visibility.Collapsed;
        PresentUserBubble(); UserBubble.Visibility = Visibility.Visible; ComposerPanel.Visibility = Visibility.Visible;
        ArrangeBubbles(); UpdateSceneBounds(); ExpireBubbles();
    }
    private void ConversationMenuClick(object sender, RoutedEventArgs e) { OpenComposer(); ChatTools.Visibility = Visibility.Visible; ChatTools.IsExpanded = true; }
    private void CloseBubbleClick(object sender, RoutedEventArgs e) { CloseBubble(); HideUserBubble(); }
    private void FlipClick(object sender, RoutedEventArgs e) => settings.FacingLeft.IsChecked = FlipMenu.IsChecked;
    private void ChatClick(object sender, RoutedEventArgs e) => OpenChat();
    private void SettingsClick(object sender, RoutedEventArgs e) => ShowSettings();
    private void HideClick(object sender, RoutedEventArgs e) => Hide();
    private void ExitClick(object sender, RoutedEventArgs e) => Close();
    private void ShowSettings() { if (!IsVisible) Show(); if (!settings.IsVisible) settings.Show(); settings.Activate(); }
    private void ApplyCharacterTheme()
    {
        CharacterTheme theme = CharacterTheme.Default;
        try
        {
            var resource = System.Windows.Application.GetResourceStream(new Uri("pack://application:,,,/Himari.Companion;component/Assets/himari.json"));
            if (resource is not null) { using var reader = new StreamReader(resource.Stream); theme = CharacterTheme.Parse(reader.ReadToEnd()); }
        }
        catch (IOException) { }
        settings.BubbleDescription.Text = "말풍선은 2단위 도트 테두리와 한 줄 입력창을 사용합니다. 답변 아래 도구줄에서 복사·중지·펼치기를 사용할 수 있어요.";
    }
    private void MoodChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (MoodSelector.SelectedItem is string value) { demoVersion++; talking = false; mood = value; UpdateFrame(); }
    }
    private void SetMood(string value) { mood = Moods.Contains(value) ? value : "neutral"; MoodSelector.SelectedItem = mood; }
    private void SetTalking(bool value) { talking = value; UpdateFrame(); }
    private void UpdateFrame()
    {
        if ((DateTime.UtcNow - lastIdleCheck).TotalSeconds >= 2)
        {
            lastIdleCheck = DateTime.UtcNow;
            try { sleeping = IdleSeconds() >= 300; }
            catch (System.ComponentModel.Win32Exception) { sleeping = false; }
        }
        var now = AnimationNow();
        animator.Mood = mood;
        animator.SetTalking(talking || settings.TalkPreview.IsChecked == true, now);
        animator.SetSleeping(settings.SleepPreview.IsChecked == true || sleeping && !Input.IsKeyboardFocusWithin, now);
        character.Render(PreviewPose(animator.Pose(now)));
        // Wake for the next pose, and at least every 2 s for the idle check.
        double wait = settings.PausePreview.IsChecked == true ? 2000 : (animator.NextChange(now) - now).TotalMilliseconds / animationSpeed;
        timer.Interval = TimeSpan.FromMilliseconds(Math.Clamp(wait, 10, 2000));
    }
    private async void DemoClick(object sender, RoutedEventArgs e)
    {
        if (sending || liveStreams.ContainsKey(currentConversation ?? "")) { Status.Text = "답변이 끝난 뒤 미리보기를 실행해 주세요."; return; }
        PresentUserBubble(); ShowSentMessage("설정은 어디에서 바꿀 수 있어?");
        OpenBubble(); SetMood("thinking"); int version = demoVersion; Message.Text = "잠시만 기다려 주세요…";
        await Task.Delay(1500); if (version != demoVersion) return;
        SetMood("happy"); version = demoVersion; Message.Text = "다 됐답니다, 선생님.";
        await Task.Delay(3500); if (version != demoVersion) return;
        SetMood("neutral"); ScheduleBubbleClose(Message.Text);
    }
}
