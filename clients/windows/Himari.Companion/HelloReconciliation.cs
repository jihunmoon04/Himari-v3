namespace Himari.Companion;

// Every hello is a fresh snapshot; live subscriptions are restored before the
// selected conversation is re-read, so a completed job comes from saved events.
internal static class HelloReconciliation
{
    internal static async Task ApplyAsync(Func<Task> selectConversation, Action resumeOwnJobs, Func<Task> refreshSelected, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        await selectConversation();
        ct.ThrowIfCancellationRequested();
        resumeOwnJobs();
        ct.ThrowIfCancellationRequested();
        await refreshSelected();
    }
}
