using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

public partial class MainWindow
{
    private const int HotKeyId = 0x4849, HotKeyMessage = 0x0312, RestoreCommand = 9;
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
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

    private bool OwnWindow(IntPtr hwnd)
    {
        GetWindowThreadProcessId(hwnd, out uint process);
        return hwnd != IntPtr.Zero && process == (uint)Environment.ProcessId;
    }
    private void SetupPresentationControls()
    {
        settings.ChatHotkey.ItemsSource = DialoguePolicy.Hotkeys;
        settings.BubblePlacement.ItemsSource = DialoguePolicy.BubblePlacements;
        settings.BubblePlacement.SelectedIndex = 0;
        settings.BubblePlacement.SelectionChanged += (_, _) => { UpdateSceneBounds(); SchedulePreferencesSave(); };
        settings.ChatHotkey.SelectedItem = "Alt+Q";
        settings.ChatHotkey.SelectionChanged += (_, _) => { if (hotKeySource is not null && !changingHotkey) RegisterSelectedHotkey(); };
        settings.ScreenSharing.Checked += (_, _) => PresentationChanged();
        settings.ScreenSharing.Unchecked += (_, _) => PresentationChanged();
        settings.FacingLeft.Checked += (_, _) => PresentationChanged();
        settings.FacingLeft.Unchecked += (_, _) => PresentationChanged();
        settings.BubbleMinimum.ValueChanged += (_, _) => PresentationChanged();
        settings.SecondsPer100.ValueChanged += (_, _) => PresentationChanged();
        UserBubble.SizeChanged += (_, _) => UpdateSceneBounds();
        BubblePanel.SizeChanged += (_, _) => UpdateSceneBounds();
        SizeChanged += (_, _) => UpdateBubbleBounds();
        DpiChanged += (_, _) => UpdateSceneBounds();
        PresentationChanged();
    }
    private void SetupWindowControls()
    {
        var saved = WindowPreferences.Load();
        if (saved is not null)
        {
            double height = saved.LayoutVersion >= 1 ? DialoguePolicy.Finite(saved.CharacterHeight, 330, 200, 400) : Height;
            double width = saved.LayoutVersion >= 1 && Character.Source is { Height: > 0 } image ? height * image.Width / image.Height : Width;
            var p = WindowPreferences.Normalize(saved, SystemParameters.VirtualScreenLeft, SystemParameters.VirtualScreenTop, SystemParameters.VirtualScreenWidth, SystemParameters.VirtualScreenHeight, width, height);
            Left = p.Left; Top = p.Top; SizeSlider.Value = p.CharacterHeight;
            legacyWindowPosition = p.LayoutVersion < 1;
            settings.MoveEnabled.IsChecked = p.MoveEnabled;
            settings.ScreenSharing.IsChecked = p.ScreenSharing; settings.FacingLeft.IsChecked = p.FacingLeft;
            settings.BubbleMinimum.Value = p.BubbleMinimum; settings.SecondsPer100.Value = p.SecondsPer100;
            settings.ChatHotkey.SelectedItem = p.ChatHotkey; registeredHotkey = p.ChatHotkey;
            settings.BubblePlacement.SelectedItem = p.BubblePlacement;
            LoadSignalControls(p.Signals ?? new SignalSettings());
        }
        else LoadSignalControls(new SignalSettings());
        preferencesLoaded = true;
        saveTimer.Tick += (_, _) => { saveTimer.Stop(); SaveWindowPreferences(); };
        bubbleTimer.Tick += (_, _) => AssistantBubbleExpired();
        userBubbleTimer.Tick += (_, _) => UserBubbleExpired();
        LocationChanged += (_, _) => { UpdateSceneBounds(); SchedulePreferencesSave(); };
        SetupChatHotkey();
    }
    private void SetupChatHotkey()
    {
        if (hotKeySource is null)
        {
            // Keep the registration alive independently of WPF's presentation HWNDs.
            hotKeySource = new HwndSource(new HwndSourceParameters("HIMARI shortcuts")
            { ParentWindow = new IntPtr(-3), Width = 0, Height = 0, WindowStyle = 0 });
            hotKeySource.AddHook(HotKeyHook);
        }
        RegisterSelectedHotkey();
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
            if (DialoguePolicy.HotkeyCloses(IsChatWindow(foreground), ComposerPanel.Visibility == Visibility.Visible)) CloseChat();
            else
            {
                // Only a window that was in front at this press may pick the capture monitor;
                // opening from Himari itself must not reuse an older remembered window.
                bool fromWork = EligibleWorkWindow(foreground);
                workWindow.Remember(foreground, _ => fromWork);
                captureFromWorkWindow = fromWork;
                // Over fullscreen, activating the small character window raises the taskbar;
                // OpenChat activates the monitor-sized dialogue host directly instead. Show()
                // activates by default, so a hidden character is shown without activation there.
                bool cover = CoverScreen() is not null;
                if (!IsVisible) { ShowActivated = !cover; Show(); ShowActivated = true; }
                if (!cover) Activate();
                OpenChat();
            }
            handled = true;
        }
        return IntPtr.Zero;
    }
    // Without a surviving work window, deactivate without selecting an unrelated app.
    private void CloseChat()
    {
        chatFocusVersion++;
        System.Windows.Input.Keyboard.ClearFocus();
        // Like Alt+Tab, switching focus does not dismiss or replace the editor.
        if (string.IsNullOrEmpty(Input.Text)) ScheduleUserBubbleClose();
        if (!workWindow.Return(window => IsWindow(window) && IsWindowVisible(window) && RestoreWindow(window)))
        {
            // SetActiveWindow(NULL) alone can reactivate an owned WPF window. Give
            // the desktop focus, leaving other applications and our visuals in place.
            if (!SetForegroundWindow(GetShellWindow())) SetActiveWindow(IntPtr.Zero);
        }
        UpdateFullscreenPresentation();
    }
    private static bool RestoreWindow(IntPtr window)
    {
        if (IsIconic(window)) ShowWindow(window, RestoreCommand);
        return SetForegroundWindow(window);
    }
    private void CharacterSizeChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (Character is not null) Character.Height = e.NewValue;
        if (dialogueHost is not null) ResizeCharacterWindow();
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
        ScreenSharingIndicator.Kind = sharing ? BubbleGlyphKind.Monitor : BubbleGlyphKind.MonitorOff;
        string sharingDescription = sharing ? "화면 함께 보내기 켜짐" : "화면 함께 보내기 꺼짐";
        ScreenSharingButton.ToolTip = sharingDescription;
        ScreenSharingButton.Foreground = (System.Windows.Media.Brush)FindResource(sharing ? "BubbleAccent" : "BubbleMuted");
        System.Windows.Automation.AutomationProperties.SetName(ScreenSharingButton, sharingDescription);
        UpdateBubblePresentation();
        UpdateBubbleBounds(); SchedulePreferencesSave();
    }
    private void UpdateDialoguePosition()
    {
        if (dialogueHost is not null) { UpdateSceneBounds(); return; }
        bool flip = settings.FacingLeft.IsChecked == true;
        DialogueStack.HorizontalAlignment = flip ? System.Windows.HorizontalAlignment.Left : System.Windows.HorizontalAlignment.Right;
        double bottom = Math.Max(0, Character.Height - 10);
        DialogueStack.Margin = flip ? new Thickness(50, 0, 0, bottom) : new Thickness(0, 0, 50, bottom);
        Character.Margin = flip ? new Thickness(80, 0, 0, 4) : new Thickness(0, 0, 80, 4);
        ConnectionNotice.Margin = flip ? new Thickness(0, 0, 220, 24) : new Thickness(220, 0, 0, 24);
    }
    private void UpdateSceneBounds()
    {
        if (dialogueHost is not null) { PositionDialogueHost(); return; }
        UpdateBubbleBounds();
    }
    private void UpdateBubbleBounds()
    {
        if (UserBubble is null || BubblePanel is null || Character is null) return;
        if (dialogueHost is not null) { PositionDialogueHost(); return; }
        double available = Math.Max(100, Height - Math.Max(0, Character.Height - 10) - 16);
        UserBubble.MaxHeight = Math.Max(80, BubblePanel.Visibility == Visibility.Visible ? Math.Min(280, available * .55) : Math.Min(340, available));
        double user = UserBubbleShown ? Math.Min(UserBubble.ActualHeight, UserBubble.MaxHeight) + 8 : 0;
        BubblePanel.MaxHeight = Math.Max(60, available - user);
    }
    private void SchedulePreferencesSave()
    {
        if (!preferencesLoaded || movePreviewActive || autoMoveActive) return;
        saveTimer.Stop(); saveTimer.Start();
    }
    private void OpenBubble() => ShowAssistantBubble(null);
    private void CloseBubble()
    {
        bubbleTimer.Stop(); assistantBubbleCloseDue = false; BubblePanel.Visibility = Visibility.Collapsed;
        ReturnFromMove(); ArrangeBubbles(); UpdateBubbleBounds();
        if (userBubbleCloseDue && UserBubble.Visibility == Visibility.Visible) RetryExpiry(userBubbleTimer);
    }
    private void ShowSentMessage(string text)
    {
        // Typing or a reply already moved it into the stack while it was on its way.
        if (frozenSent is not null && frozenSent.Text == text) { awaitingAnswer = false; return; }
        UserSent.Text = string.IsNullOrEmpty(text) ? "화면을 보냈습니다." : text;
        bool hasDraft = !string.IsNullOrEmpty(Input.Text);
        UserSent.Visibility = hasDraft ? Visibility.Collapsed : Visibility.Visible;
        ShowSentOnly(hasDraft);
        // A late 202 must not move this request below a reply already received.
        if (userBubbleSequence == 0) PresentUserBubble();
        UserBubble.Visibility = Visibility.Visible; awaitingAnswer = false;
        ScheduleUserBubbleClose(); ArrangeBubbles(); UpdateSceneBounds(); ExpireBubbles();
    }
    private void ScheduleUserBubbleClose()
    {
        userBubbleCloseDue = false;
        userBubbleTimer.Stop(); userBubbleTimer.Interval = TimeSpan.FromSeconds(DialoguePolicy.Duration(UserSent.Text, settings.BubbleMinimum.Value, settings.SecondsPer100.Value)); userBubbleTimer.Start();
        userBubbleDueAt = DateTime.UtcNow + userBubbleTimer.Interval;
    }
    private void ScheduleBubbleClose(string text)
    {
        assistantBubbleCloseDue = false;
        bubbleTimer.Stop(); bubbleTimer.Interval = TimeSpan.FromSeconds(DialoguePolicy.Duration(text, settings.BubbleMinimum.Value, settings.SecondsPer100.Value)); bubbleTimer.Start();
        assistantBubbleDueAt = DateTime.UtcNow + bubbleTimer.Interval;
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
        try { new WindowPreferences(Left, Top, SizeSlider.Value, settings.MoveEnabled.IsChecked == true, settings.ScreenSharing.IsChecked == true, settings.FacingLeft.IsChecked == true, settings.BubbleMinimum.Value, settings.SecondsPer100.Value, registeredHotkey, settings.BubblePlacement.SelectedItem as string ?? "상하 자동", 1, ReadSignalControls()).Save(); }
        catch (Exception ex) when (ex is System.IO.IOException or UnauthorizedAccessException) { System.Diagnostics.Trace.TraceWarning("HIMARI: could not save window preferences: {0}", ex.Message); }
    }
    private void CleanupWindowControls()
    {
        bubbleTimer.Stop(); userBubbleTimer.Stop(); saveTimer.Stop(); SaveWindowPreferences();
        if (hotKeySource is not null) { if (hotKeyRegistered) UnregisterHotKey(hotKeySource.Handle, HotKeyId); hotKeySource.RemoveHook(HotKeyHook); hotKeySource.Dispose(); hotKeySource = null; }
    }
}
