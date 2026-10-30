using System.Net;

namespace Himari.Companion;

internal enum MessageFailure { Rejected, Unknown, Closed, AlreadySent }

internal static class MessageFailures
{
    internal static bool ReuseScreen(Exception ex) => Classify(ex) == MessageFailure.Rejected && ex is ApiException api && api.Code is not ("attachment_refused" or "not_found" or "not_a_screen");
    internal static MessageFailure Classify(Exception ex)
    {
        if (ex is not ApiException api) return MessageFailure.Unknown;
        if (api.Code == "screen_already_sent") return MessageFailure.AlreadySent;
        if (api.Code == "conversation_closed") return MessageFailure.Closed;
        if (api.Status is HttpStatusCode.RequestEntityTooLarge or HttpStatusCode.TooManyRequests ||
            api.Code is "invalid_argument" or "attachment_refused" or "device_scope" or "companion_off" or "not_found" or "busy" or "not_a_screen") return MessageFailure.Rejected;
        return MessageFailure.Unknown;
    }
}
