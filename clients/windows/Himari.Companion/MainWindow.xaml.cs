using System.Windows;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

public partial class MainWindow : Window
{
    private static readonly string[] Moods = ["neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried"];
    private readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromMilliseconds(100) };
    private readonly Dictionary<string, BitmapImage> frames = new();
    private readonly Forms.NotifyIcon tray;
    private DateTime started = DateTime.UtcNow;
    private string mood = "neutral";
    private bool talking;
    private int demoVersion;

    public MainWindow()
    {
        InitializeComponent();
        MoodSelector.ItemsSource = Moods;
        MoodSelector.SelectedIndex = 0;
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("보이기 / 숨기기", null, (_, _) => ToggleVisible());
        menu.Items.Add("다시 연결", null, (_, _) => Reconnect());
        menu.Items.Add("Web 열기", null, (_, _) => OpenWeb());
        menu.Items.Add("끝내기", null, (_, _) => Close());
        tray = new Forms.NotifyIcon { Icon = System.Drawing.SystemIcons.Application, Text = "히마리 (로컬 시연)", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ToggleVisible();
        timer.Tick += (_, _) => UpdateFrame();
        timer.Start();
        Loaded += async (_, _) => await RestoreConnection();
        Closed += (_, _) => { StopConnection(); timer.Stop(); tray.Visible = false; tray.Dispose(); menu.Dispose(); };
        UpdateFrame();
    }

    private void ToggleVisible() { if (IsVisible) Hide(); else { Show(); Activate(); } }
    private void DragWindow(object sender, MouseButtonEventArgs e) { if (e.ChangedButton == MouseButton.Left) DragMove(); }
    private void CharacterMouseDown(object sender, MouseButtonEventArgs e) { if (e.LeftButton == MouseButtonState.Pressed) { DragMove(); if (connected) Input.Focus(); } }
    private void MoodChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (MoodSelector.SelectedItem is string value) { demoVersion++; talking = false; mood = value; started = DateTime.UtcNow; UpdateFrame(); }
    }
    private void SetMood(string value) { mood = Moods.Contains(value) ? value : "neutral"; MoodSelector.SelectedItem = mood; }
    private void UpdateFrame()
    {
        var elapsed = (DateTime.UtcNow - started).TotalMilliseconds;
        var frame = mood;
        if (mood == "neutral" && talking && (int)(elapsed / 260) % 2 == 1) frame = "talk";
        else if (mood == "neutral" && !talking && elapsed % 4000 > 3820) frame = "blink";
        if (!frames.TryGetValue(frame, out var image))
            frames[frame] = image = new BitmapImage(new Uri($"pack://application:,,,/Assets/{frame}.png"));
        Character.Source = image;
    }
    private async void DemoClick(object sender, RoutedEventArgs e)
    {
        SetMood("thinking"); int version = demoVersion; Message.Text = "잠시만 기다려 주세요…";
        await Task.Delay(1500); if (version != demoVersion) return;
        SetMood("happy"); version = demoVersion; Message.Text = "다 됐답니다, 선생님.";
        await Task.Delay(3500); if (version != demoVersion) return;
        SetMood("neutral");
    }
}
