using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

public partial class MainWindow
{
    private const int HotKeyId = 0x4849, HotKeyMessage = 0x0312;
    private HwndSource? hotKeySource;
    private bool preferencesLoaded, changingHotkey, sizingScene;
    private readonly HotkeyState hotkeyState = new();
    private bool hotKeyRegistered => hotkeyState.IsRegistered;
    private string registeredHotkey { get => hotkeyState.Current; set => hotkeyState.Current = value; }
    private readonly WorkWindowHistory workWindow = new();
    private IntPtr lastWorkWindow => workWindow.Target;
    private bool captureFromWorkWindow;
    private bool awaitingAnswer;
    private readonly DispatcherTimer saveTimer = new() { Interval = TimeSpan.FromMilliseconds(750) };
    private readonly DispatcherTimer bubbleTimer = new();
    private readonly DispatcherTimer userBubbleTimer = new();
    [DllImport("user32.dll", SetLastError = true)] private static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint key);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool UnregisterHotKey(IntPtr hwnd, int id);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

    private bool OwnWindow(IntPtr hwnd)
    {
        GetWindowThreadProcessId(hwnd, out uint process);
        return hwnd != IntPtr.Zero && process == (uint)Environment.ProcessId;
    }
    private void SetupPresentationControls()
    {
        settings.ChatHotkey.ItemsSource = DialoguePolicy.Hotkeys;
        settings.ChatHotkey.SelectedItem = "Alt+Q";
        settings.ChatHotkey.SelectionChanged += (_, _) => { if (hotKeySource is not null && !changingHotkey) RegisterSelectedHotkey(); };
        settings.ScreenSharing.Checked += (_, _) => PresentationChanged();
        settings.ScreenSharing.Unchecked += (_, _) => PresentationChanged();
        settings.FacingLeft.Checked += (_, _) => PresentationChanged();
        settings.FacingLeft.Unchecked += (_, _) => PresentationChanged();
        settings.BubbleMinimum.ValueChanged += (_, _) => PresentationChanged();
        settings.SecondsPer100.ValueChanged += (_, _) => PresentationChanged();
        UserBubble.SizeChanged += (_, _) => UpdateBubbleBounds();
        SizeChanged += (_, _) => UpdateBubbleBounds();
        DpiChanged += (_, _) => UpdateSceneBounds();
        PresentationChanged();
    }
    private void SetupWindowControls()
    {
        var saved = WindowPreferences.Load();
        if (saved is not null)
        {
            var p = WindowPreferences.Normalize(saved, SystemParameters.VirtualScreenLeft, SystemParameters.VirtualScreenTop, SystemParameters.VirtualScreenWidth, SystemParameters.VirtualScreenHeight, Width, Height);
            Left = p.Left; Top = p.Top; SizeSlider.Value = p.CharacterHeight;
            settings.MoveEnabled.IsChecked = p.MoveEnabled;
            settings.ScreenSharing.IsChecked = p.ScreenSharing; settings.FacingLeft.IsChecked = p.FacingLeft;
            settings.BubbleMinimum.Value = p.BubbleMinimum; settings.SecondsPer100.Value = p.SecondsPer100;
            settings.ChatHotkey.SelectedItem = p.ChatHotkey; registeredHotkey = p.ChatHotkey;
        }
        preferencesLoaded = true;
        saveTimer.Tick += (_, _) => { saveTimer.Stop(); SaveWindowPreferences(); };
        bubbleTimer.Tick += (_, _) =>
        {
            bubbleTimer.Stop();
            if (BubblePanel.IsMouseOver || awaitingAnswer || liveStreams.ContainsKey(currentConversation ?? "") || moveVerifications.ContainsValue(currentConversation ?? ""))
            { bubbleTimer.Interval = TimeSpan.FromSeconds(2); bubbleTimer.Start(); }
            else CloseBubble();
        };
        userBubbleTimer.Tick += (_, _) =>
        {
            userBubbleTimer.Stop();
            if (DialoguePolicy.KeepUserBubble(UserBubble.IsMouseOver, Input.IsKeyboardFocusWithin, Input.Text, sending, unresolvedScreen is not null || unknownText is not null))
            { userBubbleTimer.Interval = TimeSpan.FromSeconds(2); userBubbleTimer.Start(); }
            else { UserBubble.Visibility = Visibility.Collapsed; ComposerPanel.Visibility = Visibility.Collapsed; UpdateBubbleBounds(); }
        };
        LocationChanged += (_, _) => SchedulePreferencesSave();
        hotKeySource = HwndSource.FromHwnd(new WindowInteropHelper(this).Handle);
        if (hotKeySource is not null) { hotKeySource.AddHook(HotKeyHook); RegisterSelectedHotkey(); }
    }
    private void RegisterSelectedHotkey()
    {
        if (hotKeySource is null || settings.ChatHotkey.SelectedItem is not string selected) return;
        string prior = registeredHotkey;
        bool registered = hotkeyState.Select(selected, candidate =>
        {
            var spec = DialoguePolicy.Hotkey(candidate);
            return RegisterHotKey(hotKeySource.Handle, HotKeyId, spec.Modifiers, spec.Key);
        }, () => UnregisterHotKey(hotKeySource.Handle, HotKeyId));
        if (registered)
        {
            registeredHotkey = selected; settings.HotkeyStatus.Text = selected + " · 대화와 이전 작업 창 전환";
            tray.Text = "히마리 · " + selected; SchedulePreferencesSave(); return;
        }
        changingHotkey = true; settings.ChatHotkey.SelectedItem = prior; changingHotkey = false;
        settings.HotkeyStatus.Text = selected + "을 등록하지 못했습니다. 다른 단축키를 선택해 주세요." + (hotKeyRegistered ? " 현재: " + prior : "");
        tray.Text = hotKeyRegistered ? "히마리 · " + prior : "히마리 · 단축키 사용 불가";
    }
    private IntPtr HotKeyHook(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (msg == HotKeyMessage && wParam.ToInt32() == HotKeyId)
        {
            var foreground = GetForegroundWindow();
            if (OwnWindow(foreground)) ReturnToWork();
            else
            {
                workWindow.Remember(foreground, window => IsWindow(window) && !OwnWindow(window));
                captureFromWorkWindow = true;
                if (!IsVisible) Show(); Activate(); OpenChat();
            }
            handled = true;
        }
        return IntPtr.Zero;
    }
    private void ReturnToWork()
    {
        if (!workWindow.Return(window => IsWindow(window) && IsWindowVisible(window) && SetForegroundWindow(window)))
        { Status.Text = "이전 작업 창으로 돌아가지 못했습니다. Alt+Tab으로 전환해 주세요."; return; }
        System.Windows.Input.Keyboard.ClearFocus();
        if (!string.IsNullOrWhiteSpace(Input.Text)) UserBubble.Visibility = Visibility.Collapsed;
        else
        {
            ComposerPanel.Visibility = Visibility.Collapsed;
            UserSent.Visibility = string.IsNullOrEmpty(UserSent.Text) ? Visibility.Collapsed : Visibility.Visible;
            if (UserSent.Visibility != Visibility.Visible) UserBubble.Visibility = Visibility.Collapsed;
            ScheduleUserBubbleClose();
        }
    }
    private void CharacterSizeChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (Character is not null) Character.Height = e.NewValue;
        if (DialogueStack is not null) UpdateDialoguePosition();
        UpdateSceneBounds(); SchedulePreferencesSave();
    }
    private void PresentationChanged()
    {
        if (settings is null) return;
        bool flip = settings.FacingLeft.IsChecked == true;
        Character.RenderTransform = new ScaleTransform(flip ? -1 : 1, 1);
        UpdateDialoguePosition();
        BubblePanel.TailRight = flip; UserBubble.TailRight = !flip; FlipMenu.IsChecked = flip;
        bool sharing = settings.ScreenSharing.IsChecked == true;
        ScreenSharingIndicator.Text = sharing ? "▣" : "▢";
        string sharingDescription = sharing ? "화면 함께 전송 켜짐" : "화면 함께 전송 꺼짐";
        ScreenSharingIndicator.ToolTip = sharingDescription + " · 설정에서 변경";
        System.Windows.Automation.AutomationProperties.SetName(ScreenSharingIndicator, sharingDescription);
        UpdateBubbleBounds(); SchedulePreferencesSave();
    }
    private void UpdateDialoguePosition()
    {
        bool flip = settings.FacingLeft.IsChecked == true;
        DialogueStack.HorizontalAlignment = flip ? System.Windows.HorizontalAlignment.Left : System.Windows.HorizontalAlignment.Right;
        double bottom = Math.Max(0, Character.Height - 10);
        DialogueStack.Margin = flip ? new Thickness(50, 0, 0, bottom) : new Thickness(0, 0, 50, bottom);
        Character.Margin = flip ? new Thickness(80, 0, 0, 4) : new Thickness(0, 0, 80, 4);
    }
    private void UpdateSceneBounds()
    {
        if (!IsLoaded || sizingScene || autoMoveActive || movePreviewActive) { UpdateBubbleBounds(); return; }
        sizingScene = true;
        try
        {
            var handle = new WindowInteropHelper(this).Handle;
            var work = Forms.Screen.FromHandle(handle).WorkingArea;
            var dpi = VisualTreeHelper.GetDpi(this);
            if (!GetWindowRect(handle, out var current)) return;
            int gap = (int)Math.Ceiling(8 * dpi.DpiScaleY);
            double maximum = Math.Max(100, work.Height / dpi.DpiScaleY - 16);
            Height = Math.Clamp((current.Bottom - work.Top - gap) / dpi.DpiScaleY, Math.Min(maximum, Character.Height + 160), maximum);
            int physicalHeight = (int)Math.Ceiling(Height * dpi.DpiScaleY);
            int top = Math.Clamp(current.Bottom - physicalHeight, work.Top + gap, Math.Max(work.Top + gap, work.Bottom - physicalHeight - gap));
            int left = Math.Clamp(current.Left, work.Left, Math.Max(work.Left, work.Right - (current.Right - current.Left)));
            SetWindowPos(handle, IntPtr.Zero, left, top, 0, 0, 0x0015);
            UpdateBubbleBounds();
        }
        finally { sizingScene = false; }
    }
    private void UpdateBubbleBounds()
    {
        if (UserBubble is null || BubblePanel is null || Character is null) return;
        double available = Math.Max(100, Height - Math.Max(0, Character.Height - 10) - 16);
        UserBubble.MaxHeight = Math.Max(80, BubblePanel.Visibility == Visibility.Visible ? Math.Min(280, available * .55) : Math.Min(340, available));
        double user = UserBubble.Visibility == Visibility.Visible ? Math.Min(UserBubble.ActualHeight, UserBubble.MaxHeight) + 8 : 0;
        BubblePanel.MaxHeight = Math.Max(60, available - user);
    }
    private void SchedulePreferencesSave()
    {
        if (!preferencesLoaded || movePreviewActive || autoMoveActive) return;
        saveTimer.Stop(); saveTimer.Start();
    }
    private void OpenBubble() { bubbleTimer.Stop(); BubblePanel.Visibility = Visibility.Visible; UpdateSceneBounds(); }
    private void CloseBubble() { bubbleTimer.Stop(); BubblePanel.Visibility = Visibility.Collapsed; ReturnFromMove(); UpdateBubbleBounds(); }
    private void ShowSentMessage(string text)
    {
        UserSent.Text = string.IsNullOrEmpty(text) ? "화면을 보냈습니다." : text;
        bool hasDraft = !string.IsNullOrEmpty(Input.Text);
        UserSent.Visibility = hasDraft ? Visibility.Collapsed : Visibility.Visible;
        ComposerPanel.Visibility = hasDraft ? Visibility.Visible : Visibility.Collapsed;
        UserBubble.Visibility = Visibility.Visible; awaitingAnswer = false;
        ScheduleUserBubbleClose(); UpdateSceneBounds();
    }
    private void ScheduleUserBubbleClose()
    {
        userBubbleTimer.Stop(); userBubbleTimer.Interval = TimeSpan.FromSeconds(DialoguePolicy.Duration(UserSent.Text, settings.BubbleMinimum.Value, settings.SecondsPer100.Value)); userBubbleTimer.Start();
    }
    private void ScheduleBubbleClose(string text)
    {
        bubbleTimer.Stop(); bubbleTimer.Interval = TimeSpan.FromSeconds(DialoguePolicy.Duration(text, settings.BubbleMinimum.Value, settings.SecondsPer100.Value)); bubbleTimer.Start();
    }
    private async Task ScheduleBubbleAfterMoves(string conversation)
    {
        int version = autoMoveVersion; Task pending;
        do { pending = moveQueue; try { await pending; } catch (Exception) { } } while (!ReferenceEquals(pending, moveQueue));
        if (!lifetime.IsCancellationRequested && version == autoMoveVersion && conversation == currentConversation && !liveStreams.ContainsKey(conversation) && !moveVerifications.ContainsValue(conversation))
            ScheduleBubbleClose(Message.Text);
    }
    private void SaveWindowPreferences()
    {
        if (!preferencesLoaded || !double.IsFinite(Left) || !double.IsFinite(Top)) return;
        try { new WindowPreferences(Left, Top, SizeSlider.Value, settings.MoveEnabled.IsChecked == true, settings.ScreenSharing.IsChecked == true, settings.FacingLeft.IsChecked == true, settings.BubbleMinimum.Value, settings.SecondsPer100.Value, registeredHotkey).Save(); }
        catch (Exception ex) when (ex is System.IO.IOException or UnauthorizedAccessException) { System.Diagnostics.Trace.TraceWarning("HIMARI: could not save window preferences: {0}", ex.Message); }
    }
    private void CleanupWindowControls()
    {
        bubbleTimer.Stop(); userBubbleTimer.Stop(); saveTimer.Stop(); SaveWindowPreferences();
        if (hotKeySource is not null) { if (hotKeyRegistered) UnregisterHotKey(hotKeySource.Handle, HotKeyId); hotKeySource.RemoveHook(HotKeyHook); }
    }
}
