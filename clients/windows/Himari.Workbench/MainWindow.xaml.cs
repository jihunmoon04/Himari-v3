using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Media;
using Color = System.Windows.Media.Color;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace Himari.Workbench;

/// <summary>The status bar, the tray and the window life (§3, §5.7). The work console itself is in ConsoleBrowser.cs.</summary>
public partial class MainWindow : Window
{
    private static readonly TimeSpan NotifyEvery = TimeSpan.FromMinutes(5);
    private readonly SettingsStore store;
    private readonly RunnerSupervisor supervisor;
    private readonly WorkbenchLog log;
    private readonly Action exit;
    private readonly Func<SettingsStore, Window?, string?, bool> wizard;
    private readonly Forms.NotifyIcon tray;
    private readonly Forms.ToolStripMenuItem runnerItem;
    private readonly DispatcherTimer poll = new();
    private readonly DispatcherTimer noticeTimer = new() { Interval = TimeSpan.FromSeconds(4) };
    private RunnerView view = new(RunnerPhase.Checking);
    private bool wasConnected;
    private DateTime lastNotified = DateTime.MinValue;
    private bool exiting;
    private bool busy;

    internal MainWindow(SettingsStore store, RunnerSupervisor supervisor, WorkbenchLog log, Action exit, Func<SettingsStore, Window?, string?, bool> wizard)
    {
        this.store = store; this.supervisor = supervisor; this.log = log; this.exit = exit; this.wizard = wizard;
        InitializeComponent();
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("작업 콘솔 열기", null, (_, _) => ShowFromOutside());
        runnerItem = new Forms.ToolStripMenuItem("실행기 켜기", null, (_, _) => RunnerClick(this, new RoutedEventArgs()));
        menu.Items.Add(runnerItem);
        menu.Items.Add("설정", null, (_, _) => { ShowFromOutside(); SettingsClick(this, new RoutedEventArgs()); });
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("종료", null, (_, _) => exit());
        tray = new Forms.NotifyIcon { Icon = System.Drawing.SystemIcons.Application, Text = "HIMARI 작업 앱", ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += (_, _) => ShowFromOutside();

        supervisor.Changed += next => Dispatcher.BeginInvoke(() => Render(next));
        noticeTimer.Tick += (_, _) => { noticeTimer.Stop(); NoticeBar.Visibility = Visibility.Collapsed; };
        poll.Tick += (_, _) => supervisor.PollStateFile();
        IsVisibleChanged += (_, _) => SchedulePoll();
        SchedulePoll();
        poll.Start();
        Closing += HideInsteadOfClosing;
        Loaded += async (_, _) => await InitializeBrowserAsync();
        Render(supervisor.View);
    }

    /// <summary>From the tray, or from a second launch through the pipe.</summary>
    internal void ShowFromOutside()
    {
        if (exiting) return;
        Show();
        if (WindowState == WindowState.Minimized) WindowState = WindowState.Normal;
        Activate();
        Topmost = true; Topmost = false;
    }

    /// <summary>
    /// After the first status at start-up: a configured hub command that does not work (missing executable, not a hub
    /// folder, old hub, failed dependencies, timeout, any other failure) reopens the wizard with what failed (§5.6).
    /// </summary>
    internal void AfterFirstCheck()
    {
        if (exiting || supervisor.View.Phase != RunnerPhase.Unverifiable || store.Current.Hub is not { } hub || supervisor.LastStatusResult is not { } result) return;
        HubCheck check = HubSetup.Classify(hub, result);
        // A busy hub (a runner starting or stopping just then) is a working command: the next check settles it.
        if (check.Stage is HubCheckStage.Ok or HubCheckStage.Busy) return;
        ShowFromOutside();
        if (!wizard(store, this, check.Message)) return;
        _ = ReloadBrowserAsync();
        // The command may be the same one, now working (dependencies fixed): ask again, and start with the app if linked.
        _ = supervisor.RefreshAsync();
    }

    internal void PrepareExit()
    {
        exiting = true;
        poll.Stop();
        tray.Text = "HIMARI 작업 앱 · 끝내는 중";
    }

    internal void DisposeTray()
    {
        tray.Visible = false;
        tray.Dispose();
    }

    // §3: the window's X hides it; the app keeps running in the tray.
    private void HideInsteadOfClosing(object? sender, CancelEventArgs e)
    {
        if (exiting) return;
        e.Cancel = true;
        Hide();
        if (!store.Current.TrayNoticeShown)
        {
            tray.ShowBalloonTip(5000, "HIMARI 작업 앱", "트레이에서 계속 돕니다. 끝내려면 트레이 아이콘의 \"종료\"를 누르세요.", Forms.ToolTipIcon.Info);
            TrySave(s => s with { TrayNoticeShown = true });
        }
    }

    private void SchedulePoll() => poll.Interval = TimeSpan.FromSeconds(IsVisible ? 2 : 5);

    private void Render(RunnerView next)
    {
        view = next;
        StatusText.Text = next.Text;
        StatusText.ToolTip = next.Instance is null ? next.Text : $"{next.Text}\n인스턴스 {next.Instance}" + (next.Pid is { } pid ? $" · pid {pid}" : "");
        StatusText.Foreground = next.IsProblem ? new SolidColorBrush(Color.FromRgb(0xB3, 0x26, 0x1E)) : new SolidColorBrush(Color.FromRgb(0x34, 0x30, 0x4A));
        StatusDot.Fill = new SolidColorBrush(next switch
        {
            { IsProblem: true } => Color.FromRgb(0xD3, 0x3B, 0x2F),
            { Phase: RunnerPhase.Running, Connection: RunnerConnection.Connected } => Color.FromRgb(0x2E, 0x9E, 0x5B),
            { Phase: RunnerPhase.Running } => Color.FromRgb(0xE0, 0x9A, 0x22),
            _ => Color.FromRgb(0xA7, 0xA1, 0xB8),
        });
        bool stop = next.CanStop;
        RunnerButton.Content = stop ? "끄기" : "켜기";
        RunnerButton.IsEnabled = !busy && (stop || next.CanStart);
        runnerItem.Text = stop ? "실행기 끄기" : "실행기 켜기";
        runnerItem.Enabled = RunnerButton.IsEnabled;
        tray.Text = next.Tooltip;
        // §5.7: one notification when "connected" turns into something else, at most every 5 minutes.
        bool connected = next.Phase == RunnerPhase.Running && next.Connection == RunnerConnection.Connected;
        if (wasConnected && !connected && next.Phase != RunnerPhase.Exiting && DateTime.UtcNow - lastNotified >= NotifyEvery)
        {
            lastNotified = DateTime.UtcNow;
            tray.ShowBalloonTip(5000, "HIMARI 작업 앱", next.Text, next.IsProblem ? Forms.ToolTipIcon.Warning : Forms.ToolTipIcon.Info);
        }
        wasConnected = connected;
    }

    private async void RunnerClick(object sender, RoutedEventArgs e)
    {
        if (busy || exiting) return;
        busy = true; Render(view);
        try
        {
            if (view.CanStop) await StopWithConfirmation();
            else if (await supervisor.StartAsync() is { } problem) ShowNotice(problem, sticky: true);
        }
        finally { busy = false; Render(supervisor.View); }
    }

    /// <summary>§5.4: show the target, confirm, stop exactly that instance; if it changed meanwhile, show and ask again.</summary>
    private async Task StopWithConfirmation()
    {
        for (int round = 0; round < 3; round++)
        {
            var (target, problem) = await supervisor.GetStopTargetAsync();
            if (target is null) { ShowNotice(problem ?? "끌 실행기가 없습니다", sticky: true); return; }
            string who = target.Owned ? "앱이 켠 것" : "다른 곳에서 켠 것";
            string pid = target.Pid is { } p ? $", pid {p}" : "";
            string question = $"실행기 {target.Name ?? ""}({who}{pid})을 끕니다. 진행 중인 작업은 실패할 수 있습니다.".Replace("실행기 (", "실행기(");
            if (System.Windows.MessageBox.Show(this, question, "실행기 끄기", MessageBoxButton.OKCancel, MessageBoxImage.Warning) != MessageBoxResult.OK) return;
            var (result, message) = await supervisor.StopAsync(target.Instance);
            if (result != StopResult.OtherInstance) { if (message is not null) ShowNotice(message, sticky: true); return; }
            System.Windows.MessageBox.Show(this, message ?? "그 사이 다른 실행기로 바뀌었습니다.", "실행기 끄기", MessageBoxButton.OK, MessageBoxImage.Information);
        }
    }

    // §5.1: runner.log is the one hub file opened, in the default editor, only when the person asks.
    private void LogClick(object sender, RoutedEventArgs e)
    {
        string path = Path.Combine(store.Current.EffectiveHubHome, "runner.log");
        if (!File.Exists(path)) { ShowNotice("실행기 기록(runner.log)이 아직 없습니다"); return; }
        try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true })?.Dispose(); }
        catch (Win32Exception)
        {
            var notepad = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "notepad.exe")) { UseShellExecute = false };
            notepad.ArgumentList.Add(path);
            try { Process.Start(notepad)?.Dispose(); } catch (Win32Exception) { ShowNotice("기록을 열지 못했습니다"); }
        }
    }

    private async void SettingsClick(object sender, RoutedEventArgs e)
    {
        var before = store.Current;
        var dialog = new SettingsWindow(store, supervisor, () => wizard(store, this, null)) { Owner = this };
        if (dialog.ShowDialog() != true) return;
        // The supervisor has already checked the runner again if its command or home changed (ChangeSettingsAsync).
        var after = store.Current;
        if (before.Server != after.Server || before.DeveloperMode != after.DeveloperMode) await ReloadBrowserAsync();
    }

    private void ShowNotice(string text, bool sticky = false)
    {
        NoticeText.Text = text;
        NoticeBar.Visibility = Visibility.Visible;
        noticeTimer.Stop();
        noticeTimer.Interval = TimeSpan.FromSeconds(sticky ? 10 : 4);
        noticeTimer.Start();
    }

    private void TrySave(Func<WorkbenchSettings, WorkbenchSettings> change)
    {
        try { store.Update(change); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
    }
}
