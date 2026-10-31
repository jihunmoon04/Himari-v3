using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace Himari.Companion;

public partial class MainWindow
{
    private long bubbleSequence, userBubbleSequence, assistantBubbleSequence;
    private string? assistantBubbleKey;
    private bool userBubbleCloseDue, assistantBubbleCloseDue;

    private void PresentUserBubble()
    {
        userBubbleSequence = ++bubbleSequence;
        userBubbleCloseDue = false;
        userBubbleTimer.Stop();
        ArrangeBubbles();
        ExpireBubbles();
    }
    private void ArrangeBubbles()
    {
        // Change grid rows, not parents: moving a live TextBox must retain focus,
        // selection, its draft and any IME composition.
        bool userFirst = userBubbleSequence <= assistantBubbleSequence;
        if (bubblesBelow) userFirst = !userFirst;
        Grid.SetRow(UserBubble, userFirst ? 0 : 1);
        Grid.SetRow(BubblePanel, userFirst ? 1 : 0);
        bool both = UserBubble.Visibility == Visibility.Visible && BubblePanel.Visibility == Visibility.Visible;
        UserBubble.Margin = new Thickness(0, both && !userFirst ? 8 : 0, 0, 0);
        BubblePanel.Margin = new Thickness(0, both && userFirst ? 8 : 0, 0, 0);
    }
    private void OpenReplyBubble(string conversation, string? job) => ShowAssistantBubble($"{conversation}:{job}");
    private void ShowAssistantBubble(string? key)
    {
        if (key is null || key != assistantBubbleKey) { assistantBubbleSequence = ++bubbleSequence; replyStopRequested = false; ResetCopyFeedback(); }
        assistantBubbleKey = key;
        assistantBubbleCloseDue = false;
        bubbleTimer.Stop();
        BubblePanel.Visibility = Visibility.Visible;
        UpdateBubblePresentation();
        ArrangeBubbles(); UpdateSceneBounds(); ExpireBubbles();
    }
    private void AssistantBubbleExpired()
    {
        bubbleTimer.Stop(); assistantBubbleCloseDue = true; ExpireBubbles();
    }
    private void UserBubbleExpired()
    {
        userBubbleTimer.Stop(); userBubbleCloseDue = true; ExpireBubbles();
    }
    private FrameworkElement? OldestVisibleBubble() => new[] { UserBubble, BubblePanel }
        .Where(bubble => bubble.Visibility == Visibility.Visible)
        .OrderBy(bubble => bubble == UserBubble ? userBubbleSequence : assistantBubbleSequence).FirstOrDefault();
    private void ExpireBubbles()
    {
        var oldest = OldestVisibleBubble();
        if (oldest == UserBubble)
        {
            if (!userBubbleCloseDue) return;
            if (DialoguePolicy.KeepUserBubble(UserBubble.IsMouseOver, Input.IsKeyboardFocusWithin, Input.Text, sending, unresolvedScreen is not null || unknownText is not null))
            { RetryExpiry(userBubbleTimer); return; }
            HideUserBubble();
        }
        else if (oldest == BubblePanel)
        {
            if (!assistantBubbleCloseDue) return;
            if (BubblePanel.IsMouseOver || awaitingAnswer || liveStreams.ContainsKey(currentConversation ?? "") || moveVerifications.ContainsValue(currentConversation ?? ""))
            { RetryExpiry(bubbleTimer); return; }
            CloseBubble();
        }
        else return;
    }
    // Used after the older bubble closes as well as when reading protects it:
    // an overdue newer bubble never disappears in the same tick as the older.
    private static void RetryExpiry(DispatcherTimer timer)
    { timer.Interval = TimeSpan.FromSeconds(2); timer.Start(); }
    private void HideUserBubble()
    {
        userBubbleTimer.Stop(); userBubbleCloseDue = false;
        UserBubble.Visibility = Visibility.Collapsed; ComposerPanel.Visibility = Visibility.Collapsed;
        ArrangeBubbles(); UpdateBubbleBounds();
        if (assistantBubbleCloseDue && BubblePanel.Visibility == Visibility.Visible) RetryExpiry(bubbleTimer);
    }
}
