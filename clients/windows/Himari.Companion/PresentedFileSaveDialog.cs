using System.IO;
using System.Windows;

namespace Himari.Companion;

internal static class PresentedFileSaveDialog
{
    internal static string? ChoosePath(Window owner, string suggestedName)
    {
        var dialog = new Microsoft.Win32.SaveFileDialog
        {
            Title = "새 이름으로 파일 저장",
            FileName = suggestedName,
            OverwritePrompt = false,
            AddExtension = false,
            Filter = "모든 파일 (*.*)|*.*"
        };
        dialog.FileOk += (_, args) =>
        {
            if (!File.Exists(dialog.FileName) && !Directory.Exists(dialog.FileName)) return;
            args.Cancel = true;
            System.Windows.MessageBox.Show(owner, ApprovedSaveTarget.PathConflictMessage, "파일 저장",
                MessageBoxButton.OK, MessageBoxImage.Information);
        };
        return dialog.ShowDialog(owner) == true ? dialog.FileName : null;
    }
}
