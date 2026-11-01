using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace Himari.Companion;

public partial class MainWindow
{
    private long bubbleSequence, userBubbleSequence, assistantBubbleSequence;
    private string? assistantBubbleKey;
    private bool userBubbleCloseDue, assistantBubbleCloseDue;
    private DateTime userBubbleDueAt, assistantBubbleDueAt;

    // Earlier messages and replies stay in a stack beside the live bubbles, like a text
    // conversation, until each one's own time runs out (oldest first) or more than
    // MaxBubbles are shown. The live bubbles keep the editor, streaming and reply tools.
    private const int MaxBubbles = 6;
    private sealed class PastBubble(PixelBubble bubble, string text, bool user)
    {
        internal PixelBubble Bubble { get; } = bubble;
        internal string Text { get; } = text;
        internal bool User { get; } = user;
        internal DispatcherTimer Timer { get; } = new();
        internal bool Due { get; set; }
    }
    private readonly List<PastBubble> history = [];
    // The message being sent, once it moved into the stack before the server answered.
    private PastBubble? frozenSent;

    // A sent bubble shows only what was sent, like a text message. Its editor keeps keyboard
    // focus at zero height (a keystroke typed into an unfocused window loses its IME
    // composition), and typing shows it again as the newest bubble. If the sent bubble expires
    // meanwhile, it stays in the tree as an invisible ghost that only holds that focus.
    private bool composerStashed, userBubbleGhost;
    private bool UserBubbleShown => UserBubble.Visibility == Visibility.Visible && !userBubbleGhost;
    private void ShowSentOnly(bool hasDraft)
    {
        if (hasDraft) { UnstashComposer(); ComposerPanel.Visibility = Visibility.Visible; }
        else if (Input.IsKeyboardFocusWithin) { UnstashComposer(); composerStashed = true; ComposerPanel.Height = 0; ComposerPanel.Visibility = Visibility.Visible; }
        else { UnstashComposer(); ComposerPanel.Visibility = Visibility.Collapsed; }
    }
    private void UnstashComposer()
    {
        composerStashed = userBubbleGhost = false;
        ComposerPanel.ClearValue(HeightProperty);
        UserBubble.ClearValue(HeightProperty); UserBubble.ClearValue(OpacityProperty); UserBubble.ClearValue(IsHitTestVisibleProperty);
    }
    // Focus also drops for a moment while activation moves between our two windows; only a
    // focus that is still gone once input settles closes the hidden editor.
    private void StashedComposerLostFocus() => Dispatcher.BeginInvoke(DispatcherPriority.ApplicationIdle, new Action(() =>
    {
        if (!composerStashed || Input.IsKeyboardFocusWithin || !string.IsNullOrEmpty(Input.Text)) return;
        if (userBubbleGhost) HideUserBubble();
        else { UnstashComposer(); ComposerPanel.Visibility = Visibility.Collapsed; ArrangeBubbles(); UpdateSceneBounds(); }
    }));
    private void GhostUserBubble()
    {
        userBubbleTimer.Stop(); userBubbleCloseDue = false; userBubbleGhost = true;
        UserBubble.Height = 0; UserBubble.Opacity = 0; UserBubble.IsHitTestVisible = false;
        ArrangeBubbles(); UpdateBubbleBounds();
        if (assistantBubbleCloseDue && BubblePanel.Visibility == Visibility.Visible) RetryExpiry(bubbleTimer);
    }

    private TimeSpan Remaining(DispatcherTimer timer, bool due, DateTime dueAt, string text) =>
        due ? TimeSpan.FromSeconds(2)
        : timer.IsEnabled ? TimeSpan.FromSeconds(Math.Max(1, (dueAt - DateTime.UtcNow).TotalSeconds))
        : TimeSpan.FromSeconds(DialoguePolicy.Duration(text, settings.BubbleMinimum.Value, settings.SecondsPer100.Value));
    private PastBubble AddHistory(string text, bool user, TimeSpan remaining)
    {
        FrameworkElement content = user
            ? new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, Content = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, FontSize = 14, LineHeight = 22 } }
            // It renders when Text is set, before joining the tree, so nothing is inherited yet.
            : new MarkdownView { FontFamily = Message.FontFamily, FontSize = 14, Foreground = Message.Foreground, Text = text };
        var bubble = new PixelBubble { IsUser = user, TailRight = user, TailTop = bubblesBelow, Width = UserBubble.Width, MaxHeight = user ? 260 : 320, HorizontalAlignment = System.Windows.HorizontalAlignment.Left, Child = content };
        var past = new PastBubble(bubble, text, user);
        past.Timer.Interval = remaining;
        past.Timer.Tick += (_, _) => { past.Timer.Stop(); past.Due = true; ExpireBubbles(); };
        history.Add(past); past.Timer.Start();
        return past;
    }
    private bool FreezeSent()
    {
        if (!UserBubbleShown || UserSent.Visibility != Visibility.Visible || string.IsNullOrEmpty(UserSent.Text)) return false;
        var past = AddHistory(UserSent.Text, true, Remaining(userBubbleTimer, userBubbleCloseDue, userBubbleDueAt, UserSent.Text));
        if (sending) frozenSent = past;
        UserSent.Visibility = Visibility.Collapsed; userBubbleTimer.Stop(); userBubbleCloseDue = false;
        return true;
    }
    private bool FreezeReply()
    {
        // A reply still being written stays live with its stop button.
        if (BubblePanel.Visibility != Visibility.Visible || StopButton.Visibility == Visibility.Visible || string.IsNullOrWhiteSpace(Message.Text)) return false;
        AddHistory(Message.Text, false, Remaining(bubbleTimer, assistantBubbleCloseDue, assistantBubbleDueAt, Message.Text));
        // A question moved into the stack is never shown again as a live bubble.
        bubbleTimer.Stop(); assistantBubbleCloseDue = false; assistantBubbleKey = null; awaitingAnswer = false;
        BubblePanel.Visibility = Visibility.Collapsed;
        return true;
    }
    // Moves the live bubbles up to this sequence into the stack, oldest first, so the stack
    // keeps conversation order.
    private bool FreezeThrough(long sequence)
    {
        bool userOlder = userBubbleSequence <= assistantBubbleSequence, changed = false;
        foreach (bool user in new[] { userOlder, !userOlder })
            if ((user ? userBubbleSequence : assistantBubbleSequence) <= sequence) changed |= user ? FreezeSent() : FreezeReply();
        if (!changed) return false;
        DropEmptyUserBubble(); ArrangeBubbles(); UpdateSceneBounds();
        return true;
    }
    // Typing or opening the editor after a send moves the sent bubble into the stack;
    // the sent one never turns editable.
    private void FreezeSentBubble()
    {
        if (userBubbleGhost) UserSent.Visibility = Visibility.Collapsed;   // it already expired
        bool sent = UserBubbleShown && UserSent.Visibility == Visibility.Visible;
        UnstashComposer();
        if (sent) FreezeThrough(userBubbleSequence);
    }
    private void DropEmptyUserBubble()
    {
        if (!UserBubbleShown || UserSent.Visibility == Visibility.Visible || UserStatusPanel.Visibility == Visibility.Visible
            || ComposerPanel.Visibility == Visibility.Visible && !composerStashed) return;
        if (composerStashed && Input.IsKeyboardFocusWithin) GhostUserBubble(); else HideUserBubble();
    }
    private void RemoveHistory(PastBubble past, bool relayout = true)
    {
        past.Timer.Stop(); history.Remove(past); History.Children.Remove(past.Bubble);
        if (relayout) { ArrangeBubbles(); UpdateSceneBounds(); }
    }
    private void ClearHistory()
    {
        if (history.Count == 0) return;
        foreach (var past in history.ToList()) RemoveHistory(past, false);
        ArrangeBubbles(); UpdateSceneBounds();
    }
    private void PlaceHistory()
    {
        int live = (UserBubbleShown ? 1 : 0) + (BubblePanel.Visibility == Visibility.Visible ? 1 : 0);
        while (history.Count > 0 && history.Count + live > MaxBubbles) RemoveHistory(history[0], false);
        // The newest bubble is always the one next to the character.
        var order = bubblesBelow ? Enumerable.Reverse(history).ToList() : history;
        if (!History.Children.Cast<UIElement>().SequenceEqual(order.Select(past => past.Bubble)))
        {
            History.Children.Clear();
            foreach (var past in order) History.Children.Add(past.Bubble);
        }
        for (int i = 0; i < order.Count; i++) { order[i].Bubble.Margin = new Thickness(0, i > 0 ? 8 : 0, 0, 0); order[i].Bubble.TailTop = bubblesBelow; }
        History.Visibility = history.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        History.HorizontalAlignment = UserBubble.HorizontalAlignment;
        History.Margin = new Thickness(UserBubble.HorizontalAlignment == System.Windows.HorizontalAlignment.Left ? UserBubble.Margin.Left : 0, bubblesBelow && live > 0 ? 8 : 0, 0, 0);
    }

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
        // selection, its draft and any IME composition. The editor always comes last.
        bool userFirst = UserSent.Visibility == Visibility.Visible && userBubbleSequence <= assistantBubbleSequence;
        if (bubblesBelow) userFirst = !userFirst;
        int live = bubblesBelow ? 0 : 1;
        Grid.SetRow(History, bubblesBelow ? 2 : 0);
        Grid.SetRow(UserBubble, live + (userFirst ? 0 : 1));
        Grid.SetRow(BubblePanel, live + (userFirst ? 1 : 0));
        bool gap = history.Count > 0 && !bubblesBelow;
        foreach (var bubble in userFirst ? new FrameworkElement[] { UserBubble, BubblePanel } : [BubblePanel, UserBubble])
        {
            bool shown = bubble == UserBubble ? UserBubbleShown : bubble.Visibility == Visibility.Visible;
            bubble.Margin = new Thickness(bubble.Margin.Left, shown && gap ? 8 : 0, 0, 0);
            gap |= shown;
        }
        PlaceHistory();
    }
    private void OpenReplyBubble(string conversation, string? job) => ShowAssistantBubble($"{conversation}:{job}");
    private void ShowAssistantBubble(string? key)
    {
        if (key is null || key != assistantBubbleKey)
        {
            // A new reply moves the one still shown, and anything older, into the stack.
            if (BubblePanel.Visibility == Visibility.Visible) FreezeThrough(assistantBubbleSequence);
            assistantBubbleSequence = ++bubbleSequence; replyStopRequested = false; ResetCopyFeedback();
        }
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
    private void PastBubbleExpired(int index)
    {
        var past = history[index]; past.Timer.Stop(); past.Due = true; ExpireBubbles();
    }
    private FrameworkElement? OldestVisibleBubble() => new[] { UserBubble, BubblePanel }
        .Where(bubble => bubble == UserBubble ? UserBubbleShown : bubble.Visibility == Visibility.Visible)
        .OrderBy(bubble => bubble == UserBubble ? userBubbleSequence : assistantBubbleSequence).FirstOrDefault();
    private void ExpireBubbles()
    {
        // The stack is older than the live bubbles and empties first.
        if (history.Count > 0)
        {
            var past = history[0];
            if (!past.Due) return;
            if (past.Bubble.IsMouseOver || past.User && sending) { RetryExpiry(past.Timer); return; }
            RemoveHistory(past);
            if (history.Count > 0) { if (history[0].Due) RetryExpiry(history[0].Timer); return; }
            if (userBubbleCloseDue && UserBubble.Visibility == Visibility.Visible) RetryExpiry(userBubbleTimer);
            if (assistantBubbleCloseDue && BubblePanel.Visibility == Visibility.Visible) RetryExpiry(bubbleTimer);
            return;
        }
        var oldest = OldestVisibleBubble();
        if (oldest == UserBubble)
        {
            if (!userBubbleCloseDue) return;
            if (DialoguePolicy.KeepUserBubble(UserBubble.IsMouseOver, Input.IsKeyboardFocusWithin && !composerStashed, Input.Text, sending, unresolvedScreen is not null || unknownText is not null))
            { RetryExpiry(userBubbleTimer); return; }
            if (composerStashed && Input.IsKeyboardFocusWithin && string.IsNullOrEmpty(Input.Text)) { GhostUserBubble(); return; }
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
        UnstashComposer();
        UserBubble.Visibility = Visibility.Collapsed; ComposerPanel.Visibility = Visibility.Collapsed;
        ArrangeBubbles(); UpdateBubbleBounds();
        if (assistantBubbleCloseDue && BubblePanel.Visibility == Visibility.Visible) RetryExpiry(bubbleTimer);
    }
}
