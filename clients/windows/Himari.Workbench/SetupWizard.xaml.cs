using System.IO;
using System.Net.Http;
using System.Windows;
using System.Windows.Media;
using Color = System.Windows.Media.Color;

namespace Himari.Workbench;

/// <summary>The first-run wizard (§5.6): server address, hub command, the status check, registration, linkage.</summary>
public partial class SetupWizard : Window
{
    private readonly SettingsStore store;
    private readonly Func<bool> holdsRunner;
    private readonly Func<Func<WorkbenchSettings, WorkbenchSettings>, Task<string?>> save;
    private readonly Func<HubCommand, string, Task<HubResult?>> probe;
    private HubCommand? checkedCommand;
    private string? checkedHome;
    private bool saving;

    /// <param name="save">Saves the settings: through the supervisor's queue once it exists (§5.1); returns null or why not.</param>
    /// <param name="probe">Runs <c>runner status --json</c> with a candidate command and home: through the same queue once
    /// the supervisor exists (§5.3); null when the app is exiting.</param>
    /// <param name="problem">Why the wizard opened again: the configured hub command does not work (§5.6 step 3).</param>
    internal SetupWizard(SettingsStore store, Func<bool> holdsRunner, Func<Func<WorkbenchSettings, WorkbenchSettings>, Task<string?>> save,
        Func<HubCommand, string, Task<HubResult?>> probe, string? problem = null)
    {
        this.store = store; this.holdsRunner = holdsRunner; this.save = save; this.probe = probe;
        InitializeComponent();
        Closing += (_, e) => e.Cancel = saving;
        WorkbenchSettings settings = store.Current;
        if (settings.Server is { Length: > 0 } server) ServerAddress.Text = server;
        DeveloperMode.IsChecked = settings.DeveloperMode;
        Hub.Load(settings.Hub, settings.EffectiveHubHome);
        Linked.IsChecked = settings.Linked;
        StartAtLogin.IsChecked = LoginStartup.IsEnabled;
        if (problem is not null) Say(HubResult, $"설정된 hub 명령이 동작하지 않습니다. {problem}", false);
        else if (Hub.Executable.Text.Length == 0) HubResult.Text = "PATH에서 uv를 찾지 못했습니다. 실행 파일을 직접 고르세요.";
    }

    private async void CheckServerClick(object sender, RoutedEventArgs e)
    {
        if (!OriginPolicy.TryServerUri(ServerAddress.Text, DeveloperMode.IsChecked == true, out var server, out string error)) { Say(ServerResult, error, false); return; }
        Say(ServerResult, "여는 중…", null);
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            using var response = await http.GetAsync(server, HttpCompletionOption.ResponseHeadersRead);
            Say(ServerResult, $"열립니다(HTTP {(int)response.StatusCode}).", true);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException)
        {
            Say(ServerResult, $"열리지 않습니다: {ex.Message} 주소가 맞다면 나중에 서버가 열려도 됩니다.", false);
        }
    }

    private async void CheckHubClick(object sender, RoutedEventArgs e)
    {
        FinishButton.IsEnabled = false;
        checkedCommand = null;
        RegistrationResult.Text = "";
        var (command, home, problem) = Hub.Read();
        if (problem is not null) { Say(HubResult, problem, false); return; }
        // §5.1: a changed executable or home is confirmed (and refused while the app holds a runner) before the wizard
        // runs it at all, not only when it is saved. Finish does not ask again for the command checked here.
        if (SettingsChanges.Check(store.Current, command!, home!, holdsRunner(), this) is { } refusal) { Say(HubResult, refusal, false); return; }
        CheckHubButton.IsEnabled = false;
        Say(HubResult, "확인하는 중… (처음에는 몇 분 걸릴 수 있습니다)", null);
        try
        {
            Directory.CreateDirectory(home!);
            if (await probe(command!, home!) is not { } result) { Say(HubResult, "앱을 끝내는 중입니다", false); return; }
            HubCheck check = HubSetup.Classify(command!, result);
            Say(HubResult, check.Message, check.Stage == HubCheckStage.Ok);
            if (check.Status is { } status)
            {
                string? registration = HubSetup.RegistrationMessage(status);
                Say(RegistrationResult, registration ?? "이 기기는 실행기로 등록되어 있습니다. 서버 연결은 켠 뒤 상태 줄에서 봅니다.", registration is null);
                checkedCommand = command; checkedHome = home;
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { Say(HubResult, $"hub 홈을 만들지 못했습니다: {ex.Message}", false); }
        finally { CheckHubButton.IsEnabled = true; UpdateFinish(); }
    }

    private void UpdateFinish() => FinishButton.IsEnabled = checkedCommand is not null;

    private async void FinishClick(object sender, RoutedEventArgs e)
    {
        if (saving) return;
        bool developer = DeveloperMode.IsChecked == true;
        if (!OriginPolicy.TryServerUri(ServerAddress.Text, developer, out var server, out string error)) { Say(ServerResult, error, false); return; }
        var (command, home, problem) = Hub.Read();
        if (problem is not null || checkedCommand is null || !command!.SameAs(checkedCommand) || home != checkedHome)
        { Say(HubResult, "hub 명령을 바꿨습니다. 다시 \"hub 확인\"을 누르세요.", false); FinishButton.IsEnabled = false; return; }
        // Confirmed at "hub 확인" for exactly this command and home; the queue still refuses it while a runner is held.
        bool linked = Linked.IsChecked == true, atLogin = StartAtLogin.IsChecked == true;
        saving = true; IsEnabled = false;
        try
        {
            if (await save(s => s with { Server = server.AbsoluteUri, DeveloperMode = developer, Hub = command, HubHome = home, Linked = linked }) is { } failed)
            { Say(HubResult, failed, false); return; }
            try { if (LoginStartup.IsEnabled != atLogin) LoginStartup.Set(atLogin); }
            // The settings are saved already: finish as saved and say what did not change.
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException)
            { System.Windows.MessageBox.Show(this, $"설정은 저장했지만 로그인 시 시작을 바꾸지 못했습니다: {ex.Message}", "HIMARI 작업 앱 처음 설정", MessageBoxButton.OK, MessageBoxImage.Warning); }
        }
        finally { saving = false; IsEnabled = true; }
        DialogResult = true;
    }

    private static void Say(System.Windows.Controls.TextBlock target, string text, bool? good)
    {
        target.Text = text;
        target.Foreground = new SolidColorBrush(good switch
        {
            true => Color.FromRgb(0x2E, 0x7D, 0x4F),
            false => Color.FromRgb(0xB3, 0x26, 0x1E),
            null => Color.FromRgb(0x6B, 0x64, 0x85),
        });
    }
}
