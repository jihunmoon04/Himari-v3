using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Threading.Channels;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Himari.Companion;

internal static class ReplyProgressChecks
{
    internal static void Run(Action<bool, string> check, string? screenshots)
    {
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var w = new MainWindow(true); w.Show();
        object? Call(string method, params object?[] args) => typeof(MainWindow).GetMethod(method, flags)!.Invoke(w, args);
        void Set(string field, object? value) => typeof(MainWindow).GetField(field, flags)!.SetValue(w, value);
        object? Get(string field) => typeof(MainWindow).GetField(field, flags)!.GetValue(w);
        void Until(Func<bool> ready)
        {
            var frame = new DispatcherFrame(); var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(10) };
            var deadline = DateTime.UtcNow.AddSeconds(8);
            timer.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) frame.Continue = false; };
            timer.Start(); Dispatcher.PushFrame(frame); timer.Stop();
            check(ready(), "preparation stream transition finishes within deadline");
        }
        var handler = new ProgressHandler();
        using var server = new ServerClient("https://example.com/", "test", handler);
        using var cancellation = new CancellationTokenSource();
        Set("client", server); Set("recoveryOwner", server); Set("connection", cancellation); Set("currentConversation", "c");
        var settings = (Window)Get("settings")!; ((CheckBox)settings.FindName("ScreenSharing")).IsChecked = false;
        var message = (MarkdownView)w.FindName("Message");
        var progress = (ReplyProgressView)w.FindName("ReplyProgress");
        var bubble = (FrameworkElement)w.FindName("BubblePanel");
        var readTimer = (DispatcherTimer)Get("bubbleTimer")!;
        var tick = (DispatcherTimer)typeof(ReplyProgressView).GetField("ticker", flags)!.GetValue(progress)!;
        var label = (TextBlock)progress.Children[1]; var elapsed = (TextBlock)progress.Children[2];
        Call("SetConnected", true, "연결됨");
        message.Text = "이전 답변";
        ((TextBox)w.FindName("Input")).Text = "새 질문"; Call("SendMessage");
        Until(() => !(bool)Get("sending")!);
        check(bubble.Visibility == Visibility.Visible && progress.Visibility == Visibility.Visible && message.Text == "" && !readTimer.IsEnabled, "accepted send shows waiting UI without stale answer or read expiry");
        check(((Button)w.FindName("CopyReplyButton")).Visibility == Visibility.Collapsed && ((Button)w.FindName("ExpandButton")).Visibility == Visibility.Collapsed && ((Button)w.FindName("StopButton")).Visibility == Visibility.Visible, "waiting offers stop and hides unavailable copy/expand");
        var refresh = (Task)Call("ReadConversation", "c", cancellation.Token)!; Until(() => refresh.IsCompleted); refresh.GetAwaiter().GetResult();
        check(message.Text == "" && progress.Visibility == Visibility.Visible, "history refresh before the first snapshot cannot populate the pending answer");
        Call("StartLive", "c", server, cancellation.Token, "j");
        handler.Live.Push("snapshot", new { job_id = "j", run_id = "r", text = "", tools = Array.Empty<string>(), thinking = true });
        Until(() => (string?)Get("replyProgressRun") == "r" && tick.IsEnabled);
        string word = label.Text;
        Until(() => elapsed.Text != "· 0s");
        check(label.Text == word && message.Text == "" && !(bool)Get("talking")!, "thinking vocabulary stays stable as elapsed time advances without talking or source text");
        if (screenshots is not null)
        {
            System.IO.Directory.CreateDirectory(screenshots);
            var host = (Window)Get("dialogueHost")!; host.UpdateLayout();
            var image = new RenderTargetBitmap((int)Math.Ceiling(host.ActualWidth), (int)Math.Ceiling(host.ActualHeight), 96, 96, PixelFormats.Pbgra32); image.Render((FrameworkElement)host.Content);
            var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(image));
            using var file = System.IO.File.Create(System.IO.Path.Combine(screenshots, "13-reply-preparing.png")); png.Save(file);
        }
        handler.Live.Push("delta", new { job_id = "j", run_id = "r", tools = new[] { "web.search" } });
        Until(() => label.Text == "Preparing web.search…");
        handler.Live.Push("snapshot", new { job_id = "j", run_id = "r2", text = "", tools = Array.Empty<string>() });
        Until(() => (string?)Get("replyProgressRun") == "r2");
        check(!label.Text.Contains("web.search"), "new run clears old tool activity");
        long order = (long)Get("assistantBubbleSequence")!;
        handler.Live.Push("delta", new { job_id = "j", run_id = "r2", text = "**답변**", moods = Array.Empty<object>() });
        Until(() => message.Text == "**답변**");
        check(progress.Visibility == Visibility.Collapsed && !tick.IsEnabled && message.Visibility == Visibility.Visible && (long)Get("assistantBubbleSequence")! == order, "first text replaces waiting in the same bubble and disposes its animation ticker");
        Call("BeginReplyProgress", "c", "j");
        check(message.Text == "**답변**" && progress.Visibility == Visibility.Collapsed, "late acceptance cannot replace streamed answer");
        handler.Live.Push("snapshot", new { job_id = "j", run_id = "r3", text = "", tools = Array.Empty<string>() });
        Until(() => progress.Visibility == Visibility.Visible);
        var cancel = (Task)Call("RequestCancel")!; Until(() => cancel.IsCompleted); cancel.GetAwaiter().GetResult();
        check(label.Text == "답변 중지 요청을 보냈어요." && !tick.IsEnabled && message.Text == "", "stop acknowledgement replaces waiting label without inventing answer text");
        handler.Live.Push("done", new { });
        Until(() => !((Dictionary<string, CancellationTokenSource>)Get("liveStreams")!).ContainsKey("c"));
        check(progress.Visibility == Visibility.Collapsed && bubble.Visibility == Visibility.Collapsed && !tick.IsEnabled, "bodyless completion leaves no orphan waiting bubble");
        Call("BeginReplyProgress", "other", "foreign");
        check(progress.Visibility == Visibility.Collapsed, "another conversation cannot show waiting in the current one");
        Call("BeginReplyProgress", "c", "new-job"); Call("CloseBubbleClick", w, new RoutedEventArgs()); Call("BeginReplyProgress", "c", "new-job");
        check(bubble.Visibility == Visibility.Collapsed && !tick.IsEnabled, "manual dismissal stays closed during the same pending job");
        Call("BeginReplyProgress", "c", "reconnect-job"); Call("CancelLive");
        check(bubble.Visibility == Visibility.Collapsed && !tick.IsEnabled, "disconnect clears waiting and its timer");
        w.Close();
    }

    private sealed class ProgressHandler : HttpMessageHandler
    {
        internal readonly ProgressStream Live = new();
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            bool live = request.RequestUri!.AbsolutePath.EndsWith("/live");
            string body = request.Method == HttpMethod.Post ? "{\"job_id\":\"j\"}" : request.RequestUri.AbsolutePath.EndsWith("/events") ? "{\"items\":[]}" : "{\"conversation\":{\"last_sequence\":1},\"job\":{\"running\":true,\"job_id\":\"j\"}}";
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = live ? new StreamContent(Live) : new StringContent(body) });
        }
    }

    private sealed class ProgressStream : System.IO.Stream
    {
        private readonly Channel<byte[]> chunks = Channel.CreateUnbounded<byte[]>();
        private ReadOnlyMemory<byte> pending;
        internal void Push(string kind, object data) => chunks.Writer.TryWrite(Encoding.UTF8.GetBytes($"event: {kind}\ndata: {JsonSerializer.Serialize(data)}\n\n"));
        public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken ct = default)
        {
            if (pending.IsEmpty) pending = await chunks.Reader.ReadAsync(ct);
            int size = Math.Min(buffer.Length, pending.Length); pending[..size].CopyTo(buffer); pending = pending[size..]; return size;
        }
        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() { }
        public override long Seek(long offset, System.IO.SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    }
}
