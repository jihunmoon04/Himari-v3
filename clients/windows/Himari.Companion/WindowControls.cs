using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;

namespace Himari.Companion;

public partial class MainWindow
{
    private const int HotKeyId = 0x4849, HotKeyMessage = 0x0312;
    private HwndSource? hotKeySource;
    private bool hotKeyRegistered, preferencesLoaded;
    private readonly DispatcherTimer saveTimer = new() { Interval = TimeSpan.FromMilliseconds(750) };
    private readonly DispatcherTimer bubbleTimer = new();
    [DllImport("user32.dll", SetLastError = true)] private static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint key);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool UnregisterHotKey(IntPtr hwnd, int id);

    private void SetupWindowControls()
    {
        var saved = WindowPreferences.Load();
        if (saved is not null)
        {
            var p = WindowPreferences.Normalize(saved, SystemParameters.VirtualScreenLeft, SystemParameters.VirtualScreenTop, SystemParameters.VirtualScreenWidth, SystemParameters.VirtualScreenHeight, Width, Height);
            Left = p.Left; Top = p.Top; SizeSlider.Value = p.CharacterHeight;
            settings.MoveEnabled.IsChecked = p.MoveEnabled;
        }
        preferencesLoaded = true;
        saveTimer.Tick += (_, _) => { saveTimer.Stop(); SaveWindowPreferences(); };
        bubbleTimer.Tick += (_, _) =>
        {
            bubbleTimer.Stop();
            if (BubblePanel.IsMouseOver || Input.IsKeyboardFocusWithin) { bubbleTimer.Interval = TimeSpan.FromSeconds(3); bubbleTimer.Start(); }
            else CloseBubble();
        };
        LocationChanged += (_, _) => SchedulePreferencesSave();
        hotKeySource = HwndSource.FromHwnd(new WindowInteropHelper(this).Handle);
        if (hotKeySource is not null)
        {
            hotKeySource.AddHook(HotKeyHook);
            // Ctrl+Alt+H; failure leaves the tray menu usable.
            hotKeyRegistered = RegisterHotKey(hotKeySource.Handle, HotKeyId, 0x0001 | 0x0002 | 0x4000, 0x48);
            if (!hotKeyRegistered)
            {
                tray.Text = "히마리 · 단축키 사용 불가";
                System.Diagnostics.Trace.TraceWarning("HIMARI: Ctrl+Alt+H hotkey unavailable ({0}).", new Win32Exception(Marshal.GetLastWin32Error()).Message);
            }
        }
    }
    private IntPtr HotKeyHook(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (msg == HotKeyMessage && wParam.ToInt32() == HotKeyId)
        {
            if (!IsVisible) Show(); Activate(); OpenChat();
            handled = true;
        }
        return IntPtr.Zero;
    }
    private void CharacterSizeChanged(object sender, RoutedPropertyChangedEventArgs<double> e)
    {
        if (Character is not null) Character.Height = e.NewValue;
        if (BubblePanel is not null) BubblePanel.Margin = new Thickness(15, 0, 15, Math.Max(0, e.NewValue - 10));
        if (BubblePanel is not null) BubblePanel.MaxHeight = Math.Max(200, Height - e.NewValue - 12);
        SchedulePreferencesSave();
    }
    private void SchedulePreferencesSave()
    {
        if (!preferencesLoaded || movePreviewActive || autoMoveActive) return;
        saveTimer.Stop(); saveTimer.Start();
    }
    private void OpenBubble() { bubbleTimer.Stop(); BubblePanel.Visibility = Visibility.Visible; }
    private void CloseBubble() { bubbleTimer.Stop(); BubblePanel.Visibility = Visibility.Collapsed; ComposerPanel.Visibility = Visibility.Collapsed; ChatTools.Visibility = Visibility.Collapsed; ReturnFromMove(); }
    private void ScheduleBubbleClose(string text)
    {
        bubbleTimer.Stop();
        bubbleTimer.Interval = TimeSpan.FromSeconds(Math.Clamp(8 + text.Length / 12.0, 12, 120));
        bubbleTimer.Start();
    }
    private async Task ScheduleBubbleAfterMoves(string conversation)
    {
        int version = autoMoveVersion;
        Task pending;
        do { pending = moveQueue; try { await pending; } catch (Exception) { } }
        while (!ReferenceEquals(pending, moveQueue));
        if (!lifetime.IsCancellationRequested && version == autoMoveVersion && conversation == currentConversation && !liveStreams.ContainsKey(conversation))
            ScheduleBubbleClose(Message.Text);
    }
    private void SaveWindowPreferences()
    {
        if (!preferencesLoaded || !double.IsFinite(Left) || !double.IsFinite(Top)) return;
        try { new WindowPreferences(Left, Top, SizeSlider.Value, settings.MoveEnabled.IsChecked == true).Save(); }
        catch (Exception ex) when (ex is System.IO.IOException or UnauthorizedAccessException) { System.Diagnostics.Trace.TraceWarning("HIMARI: could not save window preferences: {0}", ex.Message); }
    }
    private void CleanupWindowControls()
    {
        bubbleTimer.Stop(); saveTimer.Stop(); SaveWindowPreferences();
        if (hotKeySource is not null)
        {
            if (hotKeyRegistered) UnregisterHotKey(hotKeySource.Handle, HotKeyId);
            hotKeySource.RemoveHook(HotKeyHook);
        }
    }
}
