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
        if (pending is null || !ReferenceEquals(pending.Server, client)) return true;
        bool confirmed = false;
        try
        {
            int after = pending.After;
            while (true)
            {
                var page = await pending.Server.SendAsync(HttpMethod.Get, PathFor(pending.Conversation) + $"/events?after={after}&limit=100", null, ct);
                var items = Items(Prop(page, "items")).ToArray();
                if (items.Any(x => TextRecovery.Matches(x, pending.Text, pending.Device, pending.After))) { confirmed = true; break; }
                if (items.Length < 100) break;
                var seq = Prop(items[^1], "sequence");
                if (!seq.TryGetInt32(out int next) || next <= after) break;
                after = next;
            }
        }
        catch (Exception) when (!ct.IsCancellationRequested) { }
        if (!ReferenceEquals(pending, unknownText)) return false;
        if (confirmed)
        {
            unknownText = null;
            if (currentConversation == pending.Conversation && Input.Text.Trim() == pending.Text) Input.Clear();
            Status.Text = "기록에서 이전 메시지의 접수를 확인했습니다.";
            return false;
        }
        Status.Text = "이전 메시지의 접수 여부를 확인하지 못했습니다. 다시 보내면 중복될 수 있습니다.";
        if (!ask) return false;
        if (System.Windows.MessageBox.Show(this, "이전 메시지가 이미 기록됐을 수 있습니다. 다시 보내면 중복될 수 있어요. 현재 입력을 보내시겠어요?", "전송 여부 확인", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return false;
        unknownText = null; return true;
    }
}
