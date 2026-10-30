using System.Net.Http;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Himari.Companion;

internal sealed class HistoryWindow : Window
{
    private readonly System.Windows.Controls.TextBox output = new() { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, BorderThickness = new Thickness(0), Padding = new Thickness(16), FontSize = 14 };
    private readonly CancellationTokenSource close = new();
    internal HistoryWindow(Window owner, ServerClient client, string conversationId, string title, CancellationToken parent)
    {
        Owner = owner; Title = $"대화 기록 · {title}"; Width = 580; Height = 650; MinWidth = 320; MinHeight = 300;
        WindowStartupLocation = WindowStartupLocation.CenterOwner; Background = System.Windows.Media.Brushes.White; Content = output;
        Loaded += async (_, _) => await LoadHistory(client, conversationId, parent);
        Closed += (_, _) => close.Cancel();
    }
    private async Task LoadHistory(ServerClient client, string conversationId, CancellationToken parent)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(parent, close.Token);
        string loaded = "";
        try
        {
            output.Text = "대화 기록을 불러오는 중…";
            loaded = await HistoryReader.ReadAsync((after, ct) => client.SendAsync(HttpMethod.Get, "/api/conversations/" + Uri.EscapeDataString(conversationId) + $"/events?after={after}&limit=100", null, ct), value => { loaded = value; output.Text = value.Length == 0 ? "표시할 대화가 없습니다." : value; }, linked.Token);
            output.ScrollToEnd();
        }
        catch (OperationCanceledException) when (linked.IsCancellationRequested) { }
        catch (Exception) { output.Text = loaded + "\n대화 기록을 불러오지 못했습니다. 연결 상태를 확인해 주세요."; }
    }
}
