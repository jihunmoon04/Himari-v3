using System.IO;
using System.Windows;

namespace Himari.Workbench;

/// <summary>
/// The hub command and home, always shown in full (§5.1): the executable, every argument, the working folder, the home.
/// Shared by the settings window and the first-run wizard.
/// </summary>
public partial class HubFields : System.Windows.Controls.UserControl
{
    public HubFields()
    {
        InitializeComponent();
        // A typed or pasted hub folder sets up the default command too, not only the "찾아보기" button (§5.6).
        HubFolder.LostFocus += (_, _) => ApplyHubFolder();
    }

    public void Load(HubCommand? command, string home)
    {
        Executable.Text = command?.Executable ?? HubSetup.FindOnPath() ?? "";
        Arguments.Text = command is null ? "" : string.Join(Environment.NewLine, command.Arguments);
        WorkingDirectory.Text = command?.WorkingDirectory ?? "";
        HubFolder.Text = command is not null && HubSetup.UsesUv(command) ? command.Arguments[2] : "";
        HubHome.Text = home;
    }

    /// <summary>The command as entered, or why it cannot be used.</summary>
    public (HubCommand? Command, string? Home, string? Problem) Read()
    {
        ApplyHubFolder();
        string executable = Executable.Text.Trim().Trim('"');
        string working = WorkingDirectory.Text.Trim().Trim('"');
        string home = HubHome.Text.Trim().Trim('"');
        if (executable.Length == 0 || !Path.IsPathFullyQualified(executable)) return (null, null, "hub 실행 파일을 절대 경로로 넣으세요");
        if (working.Length == 0 || !Path.IsPathFullyQualified(working)) return (null, null, "작업 폴더를 절대 경로로 넣으세요");
        if (home.Length == 0 || !Path.IsPathFullyQualified(home)) return (null, null, "hub 홈을 절대 경로로 넣으세요");
        string[] arguments = Arguments.Text.Split('\n').Select(line => line.TrimEnd('\r')).Where(line => line.Length > 0).ToArray();
        return (new HubCommand(executable, arguments, working), home, null);
    }

    private void BrowseExecutable(object sender, RoutedEventArgs e)
    {
        var dialog = new Microsoft.Win32.OpenFileDialog { Filter = "실행 파일 (*.exe)|*.exe", Title = "hub 명령의 실행 파일" };
        if (dialog.ShowDialog(Window.GetWindow(this)) == true) Executable.Text = dialog.FileName;
    }

    private void BrowseHubFolder(object sender, RoutedEventArgs e)
    {
        if (PickFolder("hub 폴더(pyproject.toml이 있는 곳)") is not { } folder) return;
        HubFolder.Text = folder;
        ApplyHubFolder();
    }

    /// <summary>
    /// The hub folder field, when it holds an absolute path, makes the command <c>uv run --project &lt;folder&gt; himari-hub</c>
    /// working in that folder, unless the command is already exactly that. A command that is not a <c>uv</c> one (another
    /// executable typed in by hand) is left alone.
    /// </summary>
    private void ApplyHubFolder()
    {
        string folder = HubFolder.Text.Trim().Trim('"');
        if (folder.Length == 0 || !Path.IsPathFullyQualified(folder)) return;
        string executable = Executable.Text.Trim().Trim('"');
        string uv = executable.Length > 0 ? executable : HubSetup.FindOnPath() ?? "";
        if (uv.Length > 0 && !string.Equals(Path.GetFileName(uv), "uv.exe", StringComparison.OrdinalIgnoreCase)) return;
        var command = HubSetup.UvCommand(uv, folder);
        Executable.Text = command.Executable;
        Arguments.Text = string.Join(Environment.NewLine, command.Arguments);
        WorkingDirectory.Text = command.WorkingDirectory;
    }

    private void BrowseWorkingDirectory(object sender, RoutedEventArgs e) { if (PickFolder("작업 폴더") is { } folder) WorkingDirectory.Text = folder; }
    private void BrowseHubHome(object sender, RoutedEventArgs e) { if (PickFolder("hub 홈") is { } folder) HubHome.Text = folder; }

    private string? PickFolder(string title)
    {
        var dialog = new Microsoft.Win32.OpenFolderDialog { Title = title };
        return dialog.ShowDialog(Window.GetWindow(this)) == true ? dialog.FolderName : null;
    }
}
