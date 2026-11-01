using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace Himari.Companion;

public partial class MainWindow
{
    private bool replyStopRequested;
    private BubbleStatusAction bubbleStatusAction;
    private readonly DispatcherTimer copiedTimer = new() { Interval = TimeSpan.FromSeconds(1.5) };
    private Action<string> copyText = System.Windows.Clipboard.SetText;
    private static readonly DependencyPropertyDescriptor StatusTextDescriptor = DependencyPropertyDescriptor.FromProperty(TextBlock.TextProperty, typeof(TextBlock));
    private static readonly DependencyPropertyDescriptor StopVisibilityDescriptor = DependencyPropertyDescriptor.FromProperty(UIElement.VisibilityProperty, typeof(System.Windows.Controls.Button));
    private EventHandler? bubblePresentationChanged;

    private void SetupBubblePresentation()
    {
        bubblePresentationChanged = (_, _) => UpdateBubblePresentation();
        StatusTextDescriptor.AddValueChanged(Status, bubblePresentationChanged);
        StopVisibilityDescriptor.AddValueChanged(StopButton, bubblePresentationChanged);
        copiedTimer.Tick += (_, _) => ResetCopyFeedback();
        UpdateBubblePresentation();
    }
    private void CleanupBubblePresentation()
    {
        copiedTimer.Stop();
        if (bubblePresentationChanged is null) return;
        StatusTextDescriptor.RemoveValueChanged(Status, bubblePresentationChanged);
        StopVisibilityDescriptor.RemoveValueChanged(StopButton, bubblePresentationChanged);
    }
    private void UpdateBubblePresentation()
    {
        if (UserStatus is null || SendButton is null || settings is null) return;
        var notice = connected && (unknownText is not null || unresolvedScreen is not null) ? BubbleStatus.Pending : BubbleStatus.From(Status.Text);
        UserStatus.Text = notice.Text;
        UserStatus.Visibility = UserStatusPanel.Visibility = notice.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        var brush = (System.Windows.Media.Brush)FindResource(notice.Warning ? "BubbleWarning" : "BubbleMuted");
        UserStatus.Foreground = UserStatusGlyph.Foreground = StatusActionButton.Foreground = brush;
        UserStatusGlyph.Kind = notice.Icon;
        bubbleStatusAction = notice.Action;
        StatusActionButton.Visibility = notice.Action == BubbleStatusAction.None ? Visibility.Collapsed : Visibility.Visible;
        StatusActionButton.IsEnabled = notice.Action != BubbleStatusAction.History || connected;
        StatusActionText.Text = notice.Action == BubbleStatusAction.Settings ? "연결 설정" : "확인";
        System.Windows.Automation.AutomationProperties.SetName(StatusActionButton, StatusActionText.Text);
        bool streaming = StopButton.Visibility == Visibility.Visible;
        ReplyStatus.Text = replyStopRequested ? "답변 중지 요청을 보냈어요." : streaming ? "답변 중이에요…" : "";
        ReplyStatus.Visibility = ReplyStatus.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        CopyReplyButton.Visibility = streaming ? Visibility.Collapsed : Visibility.Visible;
        CopyReplyButton.IsEnabled = !string.IsNullOrWhiteSpace(Message.Text);
        SendButton.Tag = string.IsNullOrWhiteSpace(Input.Text) ? "" : "ready";
        SendButton.IsEnabled = connected && !sending && (!string.IsNullOrWhiteSpace(Input.Text) || settings.ScreenSharing.IsChecked == true || unresolvedScreen is not null);
        SendButton.ToolTip = string.IsNullOrWhiteSpace(Input.Text) && settings.ScreenSharing.IsChecked == true ? "화면만 보내기 · Enter" : "보내기 · Enter";
        UpdateUnsentBubbles();
    }
    private void ScreenSharingClick(object sender, RoutedEventArgs e)
    {
        settings.ScreenSharing.IsChecked = settings.ScreenSharing.IsChecked != true;
        FocusComposerIfOpen();
    }
    private void CopyReplyClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(Message.Text)) return;
        try
        {
            copyText(Message.Text);
            CopyReplyGlyph.Kind = BubbleGlyphKind.Check;
            CopyReplyButton.ToolTip = "복사했어요.";
            copiedTimer.Stop(); copiedTimer.Start();
        }
        catch (System.Runtime.InteropServices.ExternalException) { Status.Text = "복사하지 못했어요. 다시 시도해 주세요."; }
    }
    private void ResetCopyFeedback()
    {
        copiedTimer.Stop(); CopyReplyGlyph.Kind = BubbleGlyphKind.Copy; CopyReplyButton.ToolTip = "답변 복사";
    }
    private void StatusActionClick(object sender, RoutedEventArgs e)
    {
        if (bubbleStatusAction == BubbleStatusAction.Settings) ShowSettings();
        else if (bubbleStatusAction == BubbleStatusAction.History)
        {
            if (!connected || client is null || connection is null) return;
            if (unknownText is not null && !unknownText.Server.SameDevice(client)) { ShowSettings(); return; }
            string? conversation = unknownText?.Conversation ?? unresolvedScreen?.Conversation ?? currentConversation;
            if (conversation is not null) new HistoryWindow(this, client, conversation, "전송 기록", connection.Token).Show();
        }
    }
}
