using System.ComponentModel;
using System.Windows;

namespace Himari.Companion;

public partial class SettingsWindow : Window
{
    internal bool AllowClose;
    public SettingsWindow() { InitializeComponent(); }
    protected override void OnClosing(CancelEventArgs e)
    {
        if (!AllowClose) { e.Cancel = true; Hide(); }
        base.OnClosing(e);
    }
}
