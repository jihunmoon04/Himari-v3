using System.Net.Http;
using System.Text.Json;
using System.Windows;

namespace Himari.Companion;

public partial class MainWindow
{
    private sealed record UnknownText(ServerClient Server, string Conversation, string Text, string Device, int After);
    private UnknownText? unknownText;

    private async Task<bool> CheckUnknownText(bool ask, CancellationToken ct)
    {
        var pending = unknownText;
        if (pending is null) return true;
        var server = client;
        if (server is null || !pending.Server.SameDevice(server))
        { Status.Text = "이전 서버·기기의 전송 여부가 미확정입니다. 원래 연결에서 기록을 확인해 주세요."; return false; }
        bool confirmed = false;
        try
        {
            confirmed = await TextRecovery.AcceptedAsync(server, pending.Conversation, pending.Text, pending.Device, pending.After, ct);
        }
        catch (Exception) when (!ct.IsCancellationRequested) { }
        if (!ReferenceEquals(server, client) || !ReferenceEquals(pending, unknownText)) return false;
        if (confirmed)
        {
            unknownText = null;
            if (currentConversation == pending.Conversation && Input.Text.Trim() == pending.Text) Input.Clear();
            if (currentConversation == pending.Conversation) ShowSentMessage(pending.Text);
            Status.Text = "기록에서 이전 메시지의 접수를 확인했습니다.";
            return false;
        }
        Status.Text = "이전 메시지의 접수 여부를 확인하지 못했습니다. 다시 보내면 중복될 수 있습니다.";
        if (!ask) return false;
        if (System.Windows.MessageBox.Show(this, "이전 메시지가 이미 기록됐을 수 있습니다. 다시 보내면 중복될 수 있어요. 현재 입력을 보내시겠어요?", "전송 여부 확인", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return false;
        unknownText = null; return true;
    }
}
