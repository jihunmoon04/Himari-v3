using System.IO;
using System.Windows;

namespace Himari.Workbench;

/// <summary>Server address, hub command and home, linkage, start at login (§3, §5.1, §5.5).</summary>
public partial class SettingsWindow : Window
{
    private readonly SettingsStore store;
    private readonly RunnerSupervisor supervisor;
    private readonly Func<bool> wizard;

    internal SettingsWindow(SettingsStore store, RunnerSupervisor supervisor, Func<bool> wizard)
    {
        this.store = store; this.supervisor = supervisor; this.wizard = wizard;
        InitializeComponent();
        Fill(store.Current);
    }

    private void Fill(WorkbenchSettings settings)
    {
        ServerAddress.Text = settings.Server ?? "https://";
        DeveloperMode.IsChecked = settings.DeveloperMode;
        Hub.Load(settings.Hub, settings.EffectiveHubHome);
        Linked.IsChecked = settings.Linked;
        StartAtLogin.IsChecked = LoginStartup.IsEnabled;
    }

    private void SaveClick(object sender, RoutedEventArgs e)
    {
        Problem.Visibility = Visibility.Collapsed;
        bool developer = DeveloperMode.IsChecked == true;
        if (!OriginPolicy.TryServerUri(ServerAddress.Text, developer, out var server, out string error)) { Show(error); return; }
        var (command, home, problem) = Hub.Read();
        if (problem is not null) { Show(problem); return; }
        WorkbenchSettings before = store.Current;
        if (SettingsChanges.Check(before, command!, home!, supervisor.HoldsRunner, this) is { } refusal) { Show(refusal); return; }
        try
        {
            store.Update(s => s with { Server = server.AbsoluteUri, DeveloperMode = developer, Hub = command, HubHome = home, Linked = Linked.IsChecked == true });
            if (LoginStartup.IsEnabled != (StartAtLogin.IsChecked == true)) LoginStartup.Set(StartAtLogin.IsChecked == true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException) { Show($"설정을 저장하지 못했습니다: {ex.Message}"); return; }
        DialogResult = true;
    }

    private void WizardClick(object sender, RoutedEventArgs e)
    {
        if (wizard()) DialogResult = true;
    }

    private void Show(string text) { Problem.Text = text; Problem.Visibility = Visibility.Visible; }
}

/// <summary>The rules for changing the hub command or home (§5.1).</summary>
internal static class SettingsChanges
{
    /// <summary>Null to go ahead; otherwise why not. Asks for confirmation when the executable or the home changes.</summary>
    public static string? Check(WorkbenchSettings before, HubCommand command, string home, bool holdsRunner, Window owner)
    {
        bool commandChanged = !command.SameAs(before.Hub);
        bool homeChanged = !string.Equals(Path.GetFullPath(home), Path.GetFullPath(before.EffectiveHubHome), StringComparison.OrdinalIgnoreCase);
        if ((commandChanged || homeChanged) && holdsRunner && before.Hub is not null)
            return "앱이 켠 실행기가 있는 동안에는 hub 명령이나 hub 홈을 바꿀 수 없습니다. 먼저 상태 줄에서 실행기를 끄세요(다른 홈의 실행기를 잃지 않도록).";
        bool executableChanged = !string.Equals(command.Executable, before.Hub?.Executable, StringComparison.OrdinalIgnoreCase);
        if (before.Hub is not null && (executableChanged || homeChanged)
            && System.Windows.MessageBox.Show(owner, $"이 앱은 아래 실행 파일을 그대로 실행합니다.\n\n실행 파일: {command.Executable}\n인자: {string.Join(" ", command.Arguments.Select(a => $"[{a}]"))}\n작업 폴더: {command.WorkingDirectory}\nhub 홈: {home}\n\n바꿀까요?",
                "hub 명령 바꾸기", MessageBoxButton.OKCancel, MessageBoxImage.Warning) != MessageBoxResult.OK)
            return "바꾸지 않았습니다";
        return null;
    }
}
