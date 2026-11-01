using System.Windows;

namespace Himari.Companion;

public partial class MainWindow
{
    private string? replyProgressConversation, replyProgressJob, replyProgressRun, replyProgressTool;
    private string? dismissedReplyProgress;
    private long replyPreparationVersion;
    private bool ReplyPreparing => replyProgressConversation == currentConversation && replyProgressConversation is not null;

    private void BeginReplyProgress(string conversation, string? job)
    {
        if (conversation != currentConversation || job is null || completedReplies.Contains(job) || !moveLifetime.CanStart(job)) return;
        string key = $"{conversation}:{job}";
        if (dismissedReplyProgress == key) return;
        // A late HTTP acknowledgement must not erase text already received through Live.
        if (assistantBubbleKey == key && !string.IsNullOrEmpty(Message.Text)) return;
        if (replyProgressConversation == conversation && replyProgressJob == job) return;
        replyPreparationVersion++;
        OpenReplyBubble(conversation, job);
        replyProgressConversation = conversation; replyProgressJob = job;
        replyProgressRun = replyProgressTool = null;
        Message.Text = "";
        StopButton.Visibility = Visibility.Visible;
        SetTalking(false); SetMood("thinking");
        UpdateBubblePresentation(); UpdateSceneBounds();
    }

    private void UpdateReplyProgress(string conversation, string? job, LiveDisplayState display)
    {
        if (conversation != currentConversation) return;
        if (display.Text.Length > 0) { EndReplyProgress(); return; }
        // An empty snapshot for a new model run supersedes the previous run's text.
        if (assistantBubbleKey == $"{conversation}:{job}") Message.Text = "";
        BeginReplyProgress(conversation, job);
        if (!ReplyPreparing || replyProgressJob != job) return;
        replyProgressRun = display.RunId; replyProgressTool = display.LastTool;
        UpdateBubblePresentation(); UpdateSceneBounds();
    }

    private void EndReplyProgress(bool closeEmpty = false)
    {
        if (replyProgressConversation is null) return;
        bool empty = ReplyPreparing && Message.Text.Length == 0;
        replyProgressConversation = replyProgressJob = replyProgressRun = replyProgressTool = null;
        ReplyProgress.Reset();
        UpdateBubblePresentation();
        if (closeEmpty && empty) CloseBubble();
    }
}
