using System.Windows;

namespace Himari.Workbench;

/// <summary>
/// Start-up order: single instance (§3) → migration guard (§8) → settings or the first-run wizard (§5.6, §7) → the
/// window and the runner (§5.3). Exit and Windows logoff write stop requests directly (§5.5).
/// </summary>
public partial class App : System.Windows.Application
{
    private const string Title = "HIMARI 작업 앱";
    private SingleInstance? instance;
    private RunnerSupervisor? supervisor;
    private MainWindow? window;
    private bool exiting;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        instance = SingleInstance.TryAcquire();
        if (instance is null)
        {
            SecondLaunch answer = await Task.Run(SingleInstance.SignalFirst);
            if (answer == SecondLaunch.OtherSession) Tell("HIMARI 작업 앱이 다른 Windows 세션에서 이미 켜져 있습니다.");
            else if (answer == SecondLaunch.NotResponding) Tell("HIMARI 작업 앱이 이미 켜져 있지만 답하지 않습니다.");
            Shutdown();
            return;
        }

        var store = new SettingsStore(SettingsStore.DefaultDirectory);
        SettingsLoad load = store.Load();
        switch (load.Kind)
        {
            case SettingsLoadKind.Migrated:
                // §8: the merged HIMARI app manages the runner now; never open the wizard or touch the runner.
                Tell("HIMARI 앱으로 옮겨졌습니다. 이 작업 앱 대신 HIMARI 앱을 쓰세요.");
                Quit();
                return;
            case SettingsLoadKind.Unreadable:
                Tell($"설정 파일을 읽지 못했습니다: {load.Error}\n{store.PathName}");
                Quit();
                return;
            case SettingsLoadKind.Broken:
                Tell($"설정 파일이 깨져서 덮어쓰지 않고 옮겨 두었습니다:\n{load.MovedTo}\n\n처음 설정을 다시 합니다.");
                break;
        }
        if (!store.Current.IsComplete && !RunWizard(store, null)) { Quit(); return; }

        var log = WorkbenchLog.Default();
        log.Write($"app start (pid {Environment.ProcessId})");
        supervisor = new RunnerSupervisor(store, s => s.Hub is { } hub ? new ProcessHubCommandRunner(hub, s.EffectiveHubHome) : null,
            new WindowsSystemInfo(), log);
        window = new MainWindow(store, supervisor, log, ExitFromTray, RunWizard);
        MainWindow = window;
        instance.Listen(() => Dispatcher.BeginInvoke(window.ShowFromOutside));
        SessionEnding += (_, _) => StopForSessionEnd();
        Microsoft.Win32.SystemEvents.SessionEnding += (_, _) => StopForSessionEnd();
        window.Show();
        await supervisor.InitializeAsync();
        window.AfterFirstCheck();
    }

    private bool RunWizard(SettingsStore store, Window? owner)
    {
        var wizard = new SetupWizard(store, () => supervisor?.HoldsRunner ?? false) { Owner = owner };
        if (owner is null) wizard.WindowStartupLocation = WindowStartupLocation.CenterScreen;
        return wizard.ShowDialog() == true;
    }

    /// <summary>Tray "종료": stop requests for the owned and pending instances, at most 5 s for the acknowledgement.</summary>
    private async void ExitFromTray()
    {
        if (exiting || supervisor is null) return;
        exiting = true;
        window?.PrepareExit();
        await Task.Run(() => supervisor.RequestStopForExit(RunnerSupervisor.ExitAckWait));
        Quit();
    }

    /// <summary>Logoff or shutdown: no hub command, just the stop request files (§5.5). Best effort.</summary>
    private void StopForSessionEnd()
    {
        if (supervisor is null) return;
        exiting = true;
        window?.PrepareExit();
        supervisor.RequestStopForExit(RunnerSupervisor.ExitAckWait);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        window?.DisposeTray();
        instance?.Dispose();
        base.OnExit(e);
    }

    private void Quit()
    {
        Shutdown();
    }

    private static void Tell(string message) =>
        System.Windows.MessageBox.Show(message, Title, MessageBoxButton.OK, MessageBoxImage.Information);
}
