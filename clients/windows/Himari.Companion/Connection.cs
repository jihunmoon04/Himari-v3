using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;

namespace Himari.Companion;

public partial class MainWindow
{
    private sealed record Conversation(string Id, string Title);
    private ServerClient? client;
    private CancellationTokenSource? connection;
    private readonly Dictionary<string, CancellationTokenSource> liveStreams = new();
    private readonly Dictionary<string, (string Text, string Mood)> liveDisplays = new();
    private string? deviceId, currentConversation;
    private bool connected, changingSelection;
    private DateTime? awaySince;

    private static string? Str(JsonElement element, string name) => element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
    private static JsonElement Prop(JsonElement element, string name) => element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out var v) ? v : default;
    private static IEnumerable<JsonElement> Items(JsonElement element) => element.ValueKind == JsonValueKind.Array ? element.EnumerateArray() : [];
    private static string PathFor(string id) => "/api/conversations/" + Uri.EscapeDataString(id);

    private void SetConnected(bool value, string text)
    {
        connected = value; Status.Text = text; SendButton.IsEnabled = value; Input.IsEnabled = value;
        Character.Opacity = value ? 1 : .55;
    }
    private void StopConnection()
    {
        CancelLive();
        connection?.Cancel(); connection?.Dispose(); connection = null;
        client?.Dispose(); client = null; connected = false;
    }
    private async Task RestoreConnection()
    {
        try
        {
            var stored = CredentialStore.Read();
            if (stored is null) { SetConnected(false, "Web에서 짝짓기 코드를 발급해 주세요."); return; }
            ServerAddress.Text = stored.Value.Server;
            if (stored.Value.Pending) { SetConnected(false, "짝짓기 코드를 입력해 연결을 다시 시도해 주세요."); return; }
            await StartConnection(stored.Value.Server, stored.Value.Token);
        }
        catch (Exception ex) { ShowError(ex); }
    }
    private async void PairClick(object sender, RoutedEventArgs e)
    {
        string address = ServerAddress.Text.Trim(), code = PairCode.Text.Trim();
        if (string.IsNullOrWhiteSpace(code)) { Status.Text = "짝짓기 코드를 입력해 주세요."; return; }
        try
        {
            // Verify the URL before touching the existing credential.
            using var validator = new ServerClient(address, "unused");
            string canonical = validator.BaseUri.ToString();
            var stored = CredentialStore.Read();
            string token = stored is not null && stored.Value.Pending && stored.Value.Server == canonical ? stored.Value.Token : CredentialStore.NewToken();
            CredentialStore.Save(canonical, token, pending: true);
            using var pending = new ServerClient(canonical, token);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20));
            Status.Text = "기기 연결 중…";
            await pending.PairAsync(code, timeout.Token);
            CredentialStore.Save(canonical, token, pending: false);
            PairCode.Clear();
            await StartConnection(canonical, token);
        }
        catch (Exception ex) { ShowError(ex); }
    }
    private async Task StartConnection(string address, string token)
    {
        StopConnection();
        var local = new ServerClient(address, token); client = local;
        var cts = new CancellationTokenSource(); connection = cts;
        PairPanel.Visibility = Visibility.Collapsed; ChatPanel.Visibility = Visibility.Visible;
        SetConnected(false, "기기 확인 중…");
        int delay = 1;
        while (!cts.IsCancellationRequested)
        {
            try
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cts.Token); timeout.CancelAfter(TimeSpan.FromSeconds(20));
                var info = await local.SendAsync(HttpMethod.Get, "/api/device", null, timeout.Token);
                deviceId = Str(Prop(info, "device"), "device_id") ?? throw new InvalidOperationException("기기 응답이 올바르지 않습니다.");
                await RefreshConversations(Str(Prop(info, "recent_conversation"), "id"), timeout.Token);
                _ = EventLoop(local, cts.Token); _ = PresenceLoop(local, cts.Token); return;
            }
            catch (OperationCanceledException) when (cts.IsCancellationRequested) { return; }
            catch (ApiException ex) when (ex.Status == HttpStatusCode.Unauthorized)
            {
                StopConnection(); CredentialStore.Delete(); PairPanel.Visibility = Visibility.Visible; ChatPanel.Visibility = Visibility.Collapsed;
                SetConnected(false, "다시 짝짓기가 필요합니다."); return;
            }
            catch (Exception ex)
            {
                SetConnected(false, ex is ApiException { Code: "companion_off" } ? "서버에서 반려 기능이 꺼져 있습니다." : "기기 연결을 다시 시도합니다…");
                int wait = ex is ApiException { Code: "companion_off" } ? 300 : delay;
                delay = Math.Min(30, delay * 2);
                try { await Task.Delay(TimeSpan.FromSeconds(wait), cts.Token); } catch (OperationCanceledException) { return; }
            }
        }
    }
    private async void Reconnect()
    {
        StopConnection(); PairPanel.Visibility = Visibility.Visible; ChatPanel.Visibility = Visibility.Collapsed;
        await RestoreConnection();
    }
    private void ShowError(Exception ex)
    {
        if (ex is OperationCanceledException) { Status.Text = "연결 시간이 초과되었거나 취소되었습니다."; return; }
        Status.Text = ex is ApiException api ? api.Code switch {
            "busy" => "이 대화는 작업 중입니다. 새 대화를 열 수 있습니다.",
            "companion_off" => "서버에서 반려 기능이 꺼져 있습니다.",
            "device_scope" => "앱 권한 오류가 발생했습니다.",
            "pairing_used" => "이미 사용된 코드입니다. Web에서 새 코드를 발급해 주세요.",
            _ => "요청 실패: " + api.Code
        } : ex is ArgumentException ? ex.Message : "연결하지 못했습니다. 서버 주소와 VPN을 확인해 주세요.";
        if (ex is ApiException { Code: "device_scope" }) Trace.TraceWarning("HIMARI device_scope: client requested an unauthorized route.");
    }
    private async Task RefreshConversations(string? preferred, CancellationToken ct)
    {
        if (client is null) return;
        var data = await client.SendAsync(HttpMethod.Get, "/api/conversations", null, ct);
        var list = Items(Prop(data, "items")).Select(x => new Conversation(Str(x, "id") ?? "", Str(x, "title") ?? "새 대화")).Where(x => x.Id.Length > 0).ToList();
        string? chosen = preferred ?? currentConversation;
        changingSelection = true; Conversations.ItemsSource = list;
        Conversations.SelectedItem = list.FirstOrDefault(x => x.Id == chosen) ?? list.FirstOrDefault();
        changingSelection = false;
        currentConversation = (Conversations.SelectedItem as Conversation)?.Id;
        if (currentConversation is not null) await ReadConversation(currentConversation, ct);
    }
    private async void ConversationChanged(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        if (changingSelection) return;
        currentConversation = (Conversations.SelectedItem as Conversation)?.Id;
        if (currentConversation is not null && connection is not null)
            try { await ReadConversation(currentConversation, connection.Token); } catch (Exception ex) { ShowError(ex); }
    }
    private async Task ReadConversation(string id, CancellationToken ct)
    {
        if (client is null) return;
        var detail = await client.SendAsync(HttpMethod.Get, PathFor(id), null, ct);
        var question = Str(Prop(Prop(detail, "waiting"), "display"), "text");
        if (id != currentConversation) return;
        if (question is not null) { Message.Text = question; Input.Focus(); return; }
        if (liveDisplays.TryGetValue(id, out var display)) { Message.Text = display.Text; SetMood(display.Mood); return; }
        int after = 0; string? lastText = null;
        while (true)
        {
            var history = await client.SendAsync(HttpMethod.Get, PathFor(id) + $"/events?after={after}&limit=100", null, ct);
            var page = Items(Prop(history, "items")).ToList();
            foreach (var item in page)
                if (Str(item, "kind") == "assistant_final") lastText = Str(Prop(item, "display"), "text") ?? lastText;
            if (page.Count < 100) break;
            var seq = Prop(page[^1], "sequence");
            if (seq.ValueKind != JsonValueKind.Number || !seq.TryGetInt32(out int next) || next <= after) break;
            after = next;
        }
        if (id != currentConversation || liveStreams.ContainsKey(id)) return;
        Message.Text = lastText ?? "대화를 시작해 주세요.";
    }
    private async void NewConversationClick(object sender, RoutedEventArgs e)
    {
        if (!connected || client is null || connection is null) return;
        try { var created = await client.SendAsync(HttpMethod.Post, "/api/conversations", new { }, connection.Token); await RefreshConversations(Str(created, "id"), connection.Token); }
        catch (Exception ex) { ShowError(ex); }
    }
    private void InputKeyDown(object sender, System.Windows.Input.KeyEventArgs e) { if (e.Key == Key.Enter) { e.Handled = true; SendMessage(); } }
    private void SendClick(object sender, RoutedEventArgs e) => SendMessage();
    private async void SendMessage()
    {
        if (!connected || client is null || connection is null || currentConversation is null) return;
        string text = Input.Text.Trim(); int length = text.EnumerateRunes().Count();
        if (length is < 1 or > 32000) { Status.Text = "메시지는 1~32,000자로 입력해 주세요."; return; }
        SendButton.IsEnabled = false;
        try
        {
            await client.SendAsync(HttpMethod.Post, PathFor(currentConversation) + "/messages", new { text }, connection.Token);
            if (Input.Text.Trim() == text) Input.Clear();
            Status.Text = "답변을 기다리는 중…"; SetMood("thinking");
        }
        catch (Exception ex) { ShowError(ex); }
        finally { SendButton.IsEnabled = connected; }
    }
    private async void CancelClick(object sender, RoutedEventArgs e)
    {
        if (!connected || client is null || connection is null || currentConversation is null) return;
        try { await client.SendAsync(HttpMethod.Post, PathFor(currentConversation) + "/cancel", new { }, connection.Token); }
        catch (Exception ex) { ShowError(ex); }
    }
    private void OpenWebClick(object sender, RoutedEventArgs e) => OpenWeb();
    private void OpenWeb() { if (client is not null) Process.Start(new ProcessStartInfo(client.BaseUri.ToString()) { UseShellExecute = true }); }

    [StructLayout(LayoutKind.Sequential)] private struct LastInput { internal uint Size, Time; }
    [DllImport("user32.dll", SetLastError = true)] private static extern bool GetLastInputInfo(ref LastInput info);
    private static int IdleSeconds()
    {
        var info = new LastInput { Size = (uint)Marshal.SizeOf<LastInput>() };
        if (!GetLastInputInfo(ref info)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        return (int)Math.Min(unchecked((uint)Environment.TickCount - info.Time) / 1000, 2592000u);
    }
    private async Task PresenceLoop(ServerClient local, CancellationToken ct)
    {
        DateTime last = DateTime.MinValue; bool wasIdle = false;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                int idle = IdleSeconds();
                if ((DateTime.UtcNow - last).TotalSeconds >= 60 || (wasIdle && idle < 300))
                {
                    string zone = TimeZoneInfo.TryConvertWindowsIdToIanaId(TimeZoneInfo.Local.Id, out var iana) ? iana : TimeZoneInfo.Local.Id;
                    await local.SendAsync(HttpMethod.Post, "/api/device/presence", new { idle_seconds = idle, time_zone = zone }, ct); last = DateTime.UtcNow;
                }
                wasIdle = idle >= 300;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (Exception) { /* Event stream owns connection status and retries. */ }
            try { await Task.Delay(TimeSpan.FromSeconds(2), ct); } catch (OperationCanceledException) { return; }
        }
    }
}
