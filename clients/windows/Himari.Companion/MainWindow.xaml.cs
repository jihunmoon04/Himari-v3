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
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private readonly Dictionary<string, BitmapImage> frames = new();
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
    private DateTime started = DateTime.UtcNow;
    private string mood = "neutral";
    private bool talking;
    private bool sleeping;
    private DateTime lastIdleCheck = DateTime.MinValue;
    private int demoVersion;

    public MainWindow()
    {
        InitializeComponent();
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
        ApplyCharacterTheme();
        MoodSelector.ItemsSource = Moods;
        MoodSelector.SelectedIndex = 0;
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("보이기 / 숨기기", null, (_, _) => ToggleVisible());
        menu.Items.Add("설정", null, (_, _) => ShowSettings());
        menu.Items.Add("다시 연결", null, (_, _) => Reconnect());
        var share = new Forms.ToolStripMenuItem("화면 같이 보기") { CheckOnClick = true };
        share.CheckedChanged += (_, _) => { shareEveryMessage = share.Checked; ScreenOnce.IsChecked = share.Checked; ScreenSharingIndicator.Visibility = share.Checked ? Visibility.Visible : Visibility.Collapsed; if (tray is not null) tray.Text = share.Checked ? "히마리 · 화면 같이 보기 켜짐" : "히마리 · Ctrl+Alt+H"; };
        menu.Items.Add(share);
        menu.Items.Add("Web 열기", null, (_, _) => OpenWeb());
        menu.Items.Add("끝내기", null, (_, _) => Close());
        tray = new Forms.NotifyIcon { Icon = System.Drawing.SystemIcons.Application, Text = "히마리 · Ctrl+Alt+H", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ToggleVisible();
        timer.Tick += (_, _) => UpdateFrame();
        timer.Start();
        SourceInitialized += (_, _) => SetupWindowControls();
        Loaded += async (_, _) => { if (loadedOnce) return; loadedOnce = true; settings.Owner = this; await RestoreConnection(); };
        Closing += (_, _) => { settings.AllowClose = true; };
        Closing += (_, _) => RestoreMoveHomeOnClose();
        Closed += (_, _) => { CancelMovePreview(); lifetime.Cancel(); StopConnection(); timer.Stop(); CleanupWindowControls(); tray.Visible = false; tray.Dispose(); menu.Dispose(); };
        UpdateFrame();
    }

    private void ToggleVisible() { if (IsVisible) Hide(); else { Show(); Activate(); } }
    private void DragWindow(object sender, MouseButtonEventArgs e) { if (e.ChangedButton == MouseButton.Left) DragMove(); }
    private void CharacterMouseDown(object sender, MouseButtonEventArgs e)
    {
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
        OpenComposer();
        if (connected) Input.Focus(); else MoreButton.Focus();
    }
    private void OpenComposer() { OpenBubble(); ComposerPanel.Visibility = Visibility.Visible; }
    private void MoreClick(object sender, RoutedEventArgs e) { MoreButton.ContextMenu.PlacementTarget = MoreButton; MoreButton.ContextMenu.IsOpen = true; }
    private void ConversationMenuClick(object sender, RoutedEventArgs e) { OpenComposer(); ChatTools.Visibility = Visibility.Visible; ChatTools.IsExpanded = true; }
    private void CloseBubbleClick(object sender, RoutedEventArgs e) => CloseBubble();
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
            var resource = System.Windows.Application.GetResourceStream(new Uri("pack://application:,,,/Assets/himari.json"));
            if (resource is not null) { using var reader = new StreamReader(resource.Stream); theme = CharacterTheme.Parse(reader.ReadToEnd()); }
        }
        catch (IOException) { }
        var fill = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(theme.BubbleTemplate == "pixel" ? "#FFFDF8" : "#FFFFFF"));
        var outline = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(theme.BubbleTemplate == "pixel" ? "#6C678C" : "#52627B"));
        BubbleSurface.Background = BubbleTail.Fill = fill; BubbleSurface.BorderBrush = BubbleTail.Stroke = outline;
        settings.BubbleDescription.Text = theme.BubbleTemplate == "pixel" ? "히마리 도트 말풍선이 선택되어 있습니다." : "기본 말풍선을 사용 중입니다.";
    }
    private void MoodChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (MoodSelector.SelectedItem is string value) { demoVersion++; talking = false; mood = value; started = DateTime.UtcNow; UpdateFrame(); }
    }
    private void SetMood(string value) { mood = Moods.Contains(value) ? value : "neutral"; MoodSelector.SelectedItem = mood; }
    private void UpdateFrame()
    {
        var elapsed = (DateTime.UtcNow - started).TotalMilliseconds;
        if ((DateTime.UtcNow - lastIdleCheck).TotalSeconds >= 2)
        {
            lastIdleCheck = DateTime.UtcNow;
            try { sleeping = IdleSeconds() >= 300; }
            catch (System.ComponentModel.Win32Exception) { sleeping = false; }
        }
        var frame = mood;
        if (mood == "neutral" && sleeping && !talking && !Input.IsKeyboardFocusWithin) frame = "blink";
        else if (mood == "neutral" && talking && (int)(elapsed / 260) % 2 == 1) frame = "talk";
        else if (mood == "neutral" && !talking && elapsed % 4000 > 3820) frame = "blink";
        if (!frames.TryGetValue(frame, out var image))
            frames[frame] = image = new BitmapImage(new Uri($"pack://application:,,,/Assets/{frame}.png"));
        Character.Source = image;
    }
    private async void DemoClick(object sender, RoutedEventArgs e)
    {
        OpenBubble(); SetMood("thinking"); int version = demoVersion; Message.Text = "잠시만 기다려 주세요…";
        await Task.Delay(1500); if (version != demoVersion) return;
        SetMood("happy"); version = demoVersion; Message.Text = "다 됐답니다, 선생님.";
        await Task.Delay(3500); if (version != demoVersion) return;
        SetMood("neutral"); ScheduleBubbleClose(Message.Text);
    }
}
