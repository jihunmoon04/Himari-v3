namespace Himari.Companion;

// The result of a request only belongs to the connection and conversation
// where it started. Input revisions prevent clearing a subsequently edited draft.
internal sealed record SendContext(object Client, string ConversationId, long InputRevision)
{
    internal bool IsCurrent(object? client, string? conversationId, bool connected) =>
        connected && ReferenceEquals(Client, client) && ConversationId == conversationId;
    internal bool CanClear(object? client, string? conversationId, bool connected, long inputRevision) =>
        IsCurrent(client, conversationId, connected) && InputRevision == inputRevision;
}
