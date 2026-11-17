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
    private HubCommand? checkedCommand;
    private string? checkedHome;

    internal SetupWizard(SettingsStore store, Func<bool> holdsRunner)
    {
        this.store = store; this.holdsRunner = holdsRunner;
        InitializeComponent();
        WorkbenchSettings settings = store.Current;
        if (settings.Server is { Length: > 0 } server) ServerAddress.Text = server;
        DeveloperMode.IsChecked = settings.DeveloperMode;
        Hub.Load(settings.Hub, settings.EffectiveHubHome);
        Linked.IsChecked = settings.Linked;
        StartAtLogin.IsChecked = LoginStartup.IsEnabled;
        if (Hub.Executable.Text.Length == 0) HubResult.Text = "PATH에서 uv를 찾지 못했습니다. 실행 파일을 직접 고르세요.";
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
        CheckHubButton.IsEnabled = false;
        Say(HubResult, "확인하는 중… (처음에는 몇 분 걸릴 수 있습니다)", null);
        try
        {
            Directory.CreateDirectory(home!);
            HubResult result = await new ProcessHubCommandRunner(command!, home!).RunAsync(["runner", "status", "--json"], HubSetup.FirstCheckTimeout);
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

    private void FinishClick(object sender, RoutedEventArgs e)
    {
        bool developer = DeveloperMode.IsChecked == true;
        if (!OriginPolicy.TryServerUri(ServerAddress.Text, developer, out var server, out string error)) { Say(ServerResult, error, false); return; }
        var (command, home, problem) = Hub.Read();
        if (problem is not null || checkedCommand is null || !command!.SameAs(checkedCommand) || home != checkedHome)
        { Say(HubResult, "hub 명령을 바꿨습니다. 다시 \"hub 확인\"을 누르세요.", false); FinishButton.IsEnabled = false; return; }
        if (SettingsChanges.Check(store.Current, command, home!, holdsRunner(), this) is { } refusal) { Say(HubResult, refusal, false); return; }
        try
        {
            store.Update(s => s with { Server = server.AbsoluteUri, DeveloperMode = developer, Hub = command, HubHome = home, Linked = Linked.IsChecked == true });
            if (LoginStartup.IsEnabled != (StartAtLogin.IsChecked == true)) LoginStartup.Set(StartAtLogin.IsChecked == true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Security.SecurityException) { Say(HubResult, $"설정을 저장하지 못했습니다: {ex.Message}", false); return; }
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
