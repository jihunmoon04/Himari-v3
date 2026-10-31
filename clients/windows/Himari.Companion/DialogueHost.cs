using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
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

    private bool sceneRepositionPending;
    private string? hostCoversScreen;

    // The character's monitor, when a fullscreen work window there is in front or under the chat.
    private string? CoverScreen()
    {
        var work = FullscreenWorkScreen();
        var mine = System.Windows.Forms.Screen.FromHandle(new WindowInteropHelper(this).Handle);
        return work is not null && work.DeviceName == mine.DeviceName ? work.DeviceName : null;
    }

    private void PositionDialogueHost()
    {
        if (dialogueHost is null) return;
        // Bubble sizes settle during this pass (first show, templates); never drop that change.
        if (sizingScene) { sceneRepositionPending = true; return; }
        if (!IsVisible || (UserBubble.Visibility != Visibility.Visible && BubblePanel.Visibility != Visibility.Visible))
        { dialogueHost.Hide(); hostCoversScreen = null; UpdateFullscreenPresentation(); return; }
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
            // Keep the bubbles against the character even if the host is momentarily taller.
            DialogueStack.VerticalAlignment = below ? VerticalAlignment.Top : VerticalAlignment.Bottom;
            // Measure without a previous cover's offset; it is reapplied below when still covering.
            DialogueStack.Margin = new Thickness(0);
            double sideSpace = below ? area.Bottom - character.Bottom - 8 : character.Top - area.Top - 8;
            // Fixed-side modes retain their direction at an edge; only the bubbles may overlap
            // the character when that side cannot fit even the minimum usable editor.
            double available = Math.Min(Math.Max(240, sideSpace / dpi.DpiScaleY), Math.Max(240, area.Height / dpi.DpiScaleY - 16));
            double stackWidth = Math.Min(replyExpanded ? 644 : 304, area.Width / dpi.DpiScaleX);
            DialogueStack.Width = stackWidth;
            BubblePanel.Width = Math.Min(replyExpanded ? 620 : 280, stackWidth);
            UserBubble.Width = Math.Min(280, stackWidth);
            UserBubble.MaxHeight = BubblePanel.Visibility == Visibility.Visible ? Math.Min(280, available * .5) : Math.Min(340, available);
            UserBubble.Measure(new Size(stackWidth, available));
            double user = UserBubble.Visibility == Visibility.Visible ? UserBubble.DesiredSize.Height + 8 : 0;
            BubblePanel.MaxHeight = Math.Max(60, available - user);
            if (replyReader is not null) replyReader.Height = Math.Clamp(replyReader.ContentHeight, 22, Math.Max(22, BubblePanel.MaxHeight - 28 - ReplyToolbar.DesiredSize.Height - (ReplyStatus.Visibility == Visibility.Visible ? 26 : 0)));
            DialogueStack.Measure(new Size(stackWidth, available));
            double stackHeight = Math.Max(1, Math.Min(available, DialogueStack.DesiredSize.Height));
            double width = stackWidth * dpi.DpiScaleX, height = stackHeight * dpi.DpiScaleY;
            bool flip = settings.FacingLeft.IsChecked == true;
            double center = (character.Left + character.Right) / 2.0 + (flip ? -48 : 48) * dpi.DpiScaleX;
            int x = (int)Math.Round(Math.Clamp(center - width / 2, area.Left, Math.Max(area.Left, area.Right - width)));
            // The user bubble keeps its compact anchor; only the reply widens around the character.
            double compact = Math.Min(304, area.Width / dpi.DpiScaleX) * dpi.DpiScaleX;
            double compactX = Math.Clamp(center - compact / 2, area.Left, Math.Max(area.Left, area.Right - compact));
            double userLeft = (compactX - x) / dpi.DpiScaleX + (compact / dpi.DpiScaleX - UserBubble.Width) / 2;
            UserBubble.HorizontalAlignment = System.Windows.HorizontalAlignment.Left;
            UserBubble.Margin = new Thickness(Math.Clamp(userLeft, 0, Math.Max(0, stackWidth - UserBubble.Width)), UserBubble.Margin.Top, 0, 0);
            double y = below ? character.Bottom + 8 : character.Top - height - 8;
            int top = (int)Math.Round(Math.Clamp(y, area.Top, Math.Max(area.Top, area.Bottom - height)));
            var hostHandle = new WindowInteropHelper(dialogueHost).EnsureHandle();
            // Over a fullscreen window the host spans that monitor, transparent and click-through
            // outside the bubbles, so Windows treats it as fullscreen and keeps the taskbar down.
            hostCoversScreen = CoverScreen();
            if (hostCoversScreen is not null)
            {
                var bounds = screen.Bounds;
                dialogueHost.Width = bounds.Width / dpi.DpiScaleX; dialogueHost.Height = bounds.Height / dpi.DpiScaleY;
                DialogueStack.HorizontalAlignment = System.Windows.HorizontalAlignment.Left;
                DialogueStack.Margin = new Thickness((x - bounds.Left) / dpi.DpiScaleX, below ? (top - bounds.Top) / dpi.DpiScaleY : 0,
                    0, below ? 0 : (bounds.Bottom - top - height) / dpi.DpiScaleY);
                x = bounds.Left; top = bounds.Top;
            }
            else
            {
                dialogueHost.Width = stackWidth; dialogueHost.Height = stackHeight;
                DialogueStack.HorizontalAlignment = System.Windows.HorizontalAlignment.Stretch;
            }
            SetWindowPos(hostHandle, IntPtr.Zero, x, top, 0, 0, 0x0015);
            if (!dialogueHost.IsVisible) { dialogueHost.Show(); shellFullscreen?.Forget(hostHandle); }
            UpdateFullscreenPresentation();
        }
        finally { sizingScene = false; }
        if (sceneRepositionPending)
        {
            sceneRepositionPending = false;
            Dispatcher.BeginInvoke(DispatcherPriority.Loaded, new Action(PositionDialogueHost));
        }
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
