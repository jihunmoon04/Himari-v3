using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using Brushes = System.Windows.Media.Brushes;
using Size = System.Windows.Size;
using Panel = System.Windows.Controls.Panel;
using ComboBox = System.Windows.Controls.ComboBox;
using TextBoxBase = System.Windows.Controls.Primitives.TextBoxBase;

namespace Himari.Companion;

public partial class MainWindow
{
    private Window? dialogueHost;
    private bool legacyWindowPosition;
    private bool bubblesBelow;

    private void InitializeDialogueHost()
    {
        if (dialogueHost is not null) return;
        var oldPosition = Character.PointToScreen(new System.Windows.Point(0, 0));
        var oldOpposite = Character.PointToScreen(new System.Windows.Point(Character.ActualWidth, Character.ActualHeight));
        oldPosition = new System.Windows.Point(Math.Min(oldPosition.X, oldOpposite.X), Math.Min(oldPosition.Y, oldOpposite.Y));
        ((Panel)Content).Children.Remove(DialogueStack);
        DialogueStack.Margin = new Thickness(0);
        DialogueStack.HorizontalAlignment = System.Windows.HorizontalAlignment.Stretch;
        DialogueStack.VerticalAlignment = VerticalAlignment.Top;
        dialogueHost = new Window
        {
            Title = "히마리 대화", Owner = this, WindowStyle = WindowStyle.None,
            AllowsTransparency = true, Background = Brushes.Transparent, ResizeMode = ResizeMode.NoResize,
            ShowInTaskbar = false, ShowActivated = false, Topmost = true, Width = 340, Height = 1,
            FontFamily = FontFamily, FontSize = FontSize, UseLayoutRounding = true, Content = DialogueStack
        };
        // Moving between windows never reparents the editor again, including during IME input.
        SetupTypeToChat(dialogueHost);
        dialogueHost.Activated += (_, _) => UpdateFullscreenPresentation();
        dialogueHost.Deactivated += (_, _) => Dispatcher.BeginInvoke(new Action(UpdateFullscreenPresentation));
        dialogueHost.DpiChanged += (_, _) => Dispatcher.BeginInvoke(new Action(UpdateSceneBounds));
        Character.Margin = new Thickness(0);
        ConnectionNotice.HorizontalAlignment = System.Windows.HorizontalAlignment.Right;
        ConnectionNotice.Margin = new Thickness(0, 0, 8, 16);
        ResizeCharacterWindow();
        UpdateLayout();
        if (legacyWindowPosition)
            SetWindowPos(new WindowInteropHelper(this).Handle, IntPtr.Zero, (int)oldPosition.X, (int)oldPosition.Y, 0, 0, 0x0015);
        // Track all moves, including native drags and server-driven animation, in offline UI tests too.
        LocationChanged += (_, _) => UpdateSceneBounds();
    }

    private void ResizeCharacterWindow()
    {
        double ratio = Character.Source is { Height: > 0 } source ? source.Width / source.Height : 1;
        Width = Character.Height * ratio;
        Height = Character.Height;
    }

    private void PositionDialogueHost()
    {
        if (dialogueHost is null || sizingScene) return;
        if (!IsVisible || (UserBubble.Visibility != Visibility.Visible && BubblePanel.Visibility != Visibility.Visible))
        { dialogueHost.Hide(); UpdateFullscreenPresentation(); return; }
        sizingScene = true;
        try
        {
            var handle = new WindowInteropHelper(this).Handle;
            if (!GetWindowRect(handle, out var character)) return;
            var screen = System.Windows.Forms.Screen.FromHandle(handle);
            var area = screen.WorkingArea;
            var dpi = VisualTreeHelper.GetDpi(dialogueHost.IsLoaded ? dialogueHost : this);
            bool below = DialoguePolicy.BubblesBelow(settings.BubblePlacement.SelectedItem as string ?? "상하 자동",
                (character.Top + character.Bottom) / 2.0, screen.Bounds.Top + screen.Bounds.Height / 2.0);
            if (bubblesBelow != below) { bubblesBelow = below; ArrangeBubbles(); }
            BubblePanel.TailTop = UserBubble.TailTop = below;
            double sideSpace = below ? area.Bottom - character.Bottom - 8 : character.Top - area.Top - 8;
            // Fixed-side modes retain their direction at an edge; only the bubbles may overlap
            // the character when that side cannot fit even the minimum usable editor.
            double available = Math.Min(Math.Max(240, sideSpace / dpi.DpiScaleY), Math.Max(240, area.Height / dpi.DpiScaleY - 16));
            dialogueHost.Width = Math.Min(replyExpanded ? 644 : 304, area.Width / dpi.DpiScaleX);
            DialogueStack.Width = dialogueHost.Width;
            BubblePanel.Width = Math.Min(replyExpanded ? 620 : 280, dialogueHost.Width);
            UserBubble.Width = Math.Min(280, dialogueHost.Width);
            UserBubble.MaxHeight = BubblePanel.Visibility == Visibility.Visible ? Math.Min(280, available * .5) : Math.Min(340, available);
            UserBubble.Measure(new Size(dialogueHost.Width, available));
            double user = UserBubble.Visibility == Visibility.Visible ? UserBubble.DesiredSize.Height + 8 : 0;
            BubblePanel.MaxHeight = Math.Max(60, available - user);
            if (replyReader is not null) replyReader.Height = Math.Clamp(replyReader.ContentHeight, 22, Math.Max(22, BubblePanel.MaxHeight - 28 - ReplyToolbar.DesiredSize.Height - (ReplyStatus.Visibility == Visibility.Visible ? 26 : 0)));
            DialogueStack.Measure(new Size(dialogueHost.Width, available));
            dialogueHost.Height = Math.Max(1, Math.Min(available, DialogueStack.DesiredSize.Height));
            double width = dialogueHost.Width * dpi.DpiScaleX, height = dialogueHost.Height * dpi.DpiScaleY;
            bool flip = settings.FacingLeft.IsChecked == true;
            double center = (character.Left + character.Right) / 2.0 + (flip ? -48 : 48) * dpi.DpiScaleX;
            int x = (int)Math.Round(Math.Clamp(center - width / 2, area.Left, Math.Max(area.Left, area.Right - width)));
            double y = below ? character.Bottom + 8 : character.Top - height - 8;
            int top = (int)Math.Round(Math.Clamp(y, area.Top, Math.Max(area.Top, area.Bottom - height)));
            var hostHandle = new WindowInteropHelper(dialogueHost).EnsureHandle();
            SetWindowPos(hostHandle, IntPtr.Zero, x, top, 0, 0, 0x0015);
            if (!dialogueHost.IsVisible) { dialogueHost.Show(); shellFullscreen?.Forget(hostHandle); }
            UpdateFullscreenPresentation();
        }
        finally { sizingScene = false; }
    }

    private void SetupTypeToChat(Window window)
    {
        window.PreviewKeyDown += (_, e) =>
        {
            if (Input.IsKeyboardFocusWithin || Keyboard.FocusedElement is TextBoxBase or ComboBox) return;
            if (Keyboard.FocusedElement is System.Windows.Controls.Primitives.ButtonBase && (e.Key is Key.Space or Key.Enter)) return;
            var mods = Keyboard.Modifiers;
            if ((mods & (ModifierKeys.Alt | ModifierKeys.Windows)) != 0) return;
            bool paste = mods == ModifierKeys.Control && e.Key == Key.V;
            if (mods.HasFlag(ModifierKeys.Control) && !paste) return;
            Key key = e.Key == Key.ImeProcessed ? e.ImeProcessedKey : e.Key;
            bool printable = key is >= Key.A and <= Key.Z or >= Key.D0 and <= Key.D9 or >= Key.NumPad0 and <= Key.Divide
                or >= Key.Oem1 and <= Key.OemBackslash or Key.Space;
            if (!paste && !printable) return;
            OpenChat();
            if (paste) { ApplicationCommands.Paste.Execute(null, Input); e.Handled = true; }
        };
        window.PreviewTextInput += (_, e) =>
        {
            if (Input.IsKeyboardFocusWithin || Keyboard.FocusedElement is TextBoxBase || string.IsNullOrEmpty(e.Text)
                || Keyboard.Modifiers.HasFlag(ModifierKeys.Alt) || Keyboard.Modifiers.HasFlag(ModifierKeys.Control)) return;
            // Text already targeted at the character window must not lose its first character.
            OpenChat(); int next = Input.SelectionStart + e.Text.Length; Input.SelectedText = e.Text; Input.CaretIndex = next; e.Handled = true;
        };
    }
}
