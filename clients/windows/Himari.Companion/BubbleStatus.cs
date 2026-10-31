namespace Himari.Companion;

internal enum BubbleStatusAction { None, Settings, History }
internal sealed record BubbleNotice(string Text, BubbleGlyphKind Icon = BubbleGlyphKind.Warning, bool Warning = false, BubbleStatusAction Action = BubbleStatusAction.None);

// Short bubble copy is separate from the full diagnostic text shown in settings/tooltips.
internal static class BubbleStatus
{
    internal static BubbleNotice From(string text) => text switch
    {
        "" or "연결됨" or "연결되지 않음" or "답변 중…" or "답변을 기다리는 중…" => new(""),
        "화면을 보내지 못해 글만 보냈습니다." => new("화면 없이 글만 보냈어요.", BubbleGlyphKind.MonitorOff, true),
        "화면을 캡처하고 있습니다…" => new("화면을 준비하고 있어요…", BubbleGlyphKind.Monitor),
        "화면을 캡처해 업로드 중입니다…" => new("화면을 보내고 있어요…", BubbleGlyphKind.Monitor),
        "화면을 보내지 못했습니다. 다시 시도해 주세요." => new("화면을 보내지 못했어요. 다시 시도해 주세요.", BubbleGlyphKind.MonitorOff, true),
        "기록에서 이전 메시지의 접수를 확인했습니다." or "이전에 보낸 화면 메시지를 확인했습니다." => new("보낸 메시지를 확인했어요.", BubbleGlyphKind.Check),
        _ when text.Contains("다시 연결 중", StringComparison.Ordinal) => new("다시 연결하는 중이에요…", BubbleGlyphKind.Refresh, false, BubbleStatusAction.Settings),
        _ when text.Contains("미확정", StringComparison.Ordinal) || text.Contains("접수 여부를 확인하지 못", StringComparison.Ordinal) || text.StartsWith("화면 메시지가 전송됐는지 확인하지 못", StringComparison.Ordinal)
            => Pending,
        _ when text.Contains("다시 짝짓기", StringComparison.Ordinal) || text.Contains("다시 짝을", StringComparison.Ordinal) => new("기기 연결을 다시 확인해 주세요.", BubbleGlyphKind.Warning, true, BubbleStatusAction.Settings),
        _ => new(text, BubbleGlyphKind.Warning, true)
    };
    internal static BubbleNotice Pending => new("보냈는지 확인이 필요해요.", BubbleGlyphKind.Help, true, BubbleStatusAction.History);
}
