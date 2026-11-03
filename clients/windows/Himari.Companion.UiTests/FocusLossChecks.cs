using System.Collections;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Himari.Companion;

internal static class FocusLossChecks
{
    private const BindingFlags Inside = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;

    private sealed class Scene : IDisposable
    {
        internal readonly MainWindow Window = new(true);
        internal bool InFront = true;
        internal TextBox Editor => (TextBox)Window.FindName("Input");
        internal FrameworkElement User => (FrameworkElement)Window.FindName("UserBubble");
        internal FrameworkElement Reply => (FrameworkElement)Window.FindName("BubblePanel");
        internal IList History => (IList)Field("history")!;
        internal Scene() => Field("chatForegroundProbe", new Func<bool>(() => InFront));
        internal object? Call(string name, params object?[] args) => typeof(MainWindow).GetMethod(name, Inside)!.Invoke(Window, args);
        internal object? Field(string name) => typeof(MainWindow).GetField(name, Inside)!.GetValue(Window);
        internal void Field(string name, object? value) => typeof(MainWindow).GetField(name, Inside)!.SetValue(Window, value);
        internal string PastText(int index) => (string)History[index]!.GetType().GetProperty("Text", Inside)!.GetValue(History[index])!;
        internal void Dismiss() => Call("DismissUserBubbleIfUnfocused");
        public void Dispose() { Window.Close(); Pump(); }
    }

    private static void Pump()
    {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.SystemIdle, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }

    private static void Until(Func<bool> ready, Action<bool, string> check)
    {
        var frame = new DispatcherFrame();
        var poll = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(10) };
        var deadline = DateTime.UtcNow.AddSeconds(5);
        poll.Tick += (_, _) => { if (ready() || DateTime.UtcNow >= deadline) frame.Continue = false; };
        try { if (!ready()) { poll.Start(); Dispatcher.PushFrame(frame); } }
        finally { poll.Stop(); }
        check(ready(), "focus-loss operation completes within the test deadline");
    }

    private sealed class QuestionHandler : HttpMessageHandler
    {
        private readonly TaskCompletionSource release = new(TaskCreationOptions.RunContinuationsAsynchronously);
        internal bool Requested;
        internal void Release() => release.SetResult();
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Requested = true;
            await release.Task.WaitAsync(ct);
            return new(HttpStatusCode.OK) { Content = new StringContent(
                "{\"waiting\":{\"question\":{\"display\":{\"text\":\"어느 쪽으로 갈까요?\"}}},\"job\":{\"running\":false,\"job_id\":\"question\"}}",
                Encoding.UTF8, "application/json") };
        }
    }

    internal static void Run(Action<bool, string> check)
    {
        using (var scene = new Scene())
        {
            scene.Call("OpenComposer"); scene.Editor.Text = "다른 창에 가도 남길 초안"; scene.Editor.Select(3, 4);
            scene.Dismiss(); Pump();
            check(scene.User.Visibility == Visibility.Visible, "moving between character and chat windows keeps the draft open");
            scene.InFront = false; scene.Dismiss(); scene.InFront = true; Pump();
            check(scene.User.Visibility == Visibility.Visible, "focus regained before the idle callback keeps the draft open");
            scene.InFront = false; scene.Dismiss(); scene.Dismiss(); Pump();
            check(scene.User.Visibility == Visibility.Collapsed && scene.Editor.Text == "다른 창에 가도 남길 초안"
                && scene.Editor.SelectionStart == 3 && scene.Editor.SelectionLength == 4,
                "external focus hides the editor while preserving the draft and selection");
            scene.InFront = true; scene.Call("OpenComposer");
            check(scene.User.Visibility == Visibility.Visible && scene.Editor.Text == "다른 창에 가도 남길 초안",
                "reopening chat restores the hidden draft");
        }

        foreach (bool olderReply in new[] { true, false })
        using (var scene = new Scene())
        {
            void Reply()
            {
                scene.Call("OpenReplyBubble", "c", "reply");
                ((MarkdownView)scene.Window.FindName("Message")).Text = "답변";
                ((FrameworkElement)scene.Window.FindName("StopButton")).Visibility = Visibility.Collapsed;
            }
            if (olderReply) Reply();
            scene.Call("OpenComposer"); scene.Call("ShowSentMessage", "보낸 글");
            if (!olderReply) Reply();
            scene.InFront = false; scene.Dismiss(); scene.Dismiss(); Pump();
            check(scene.User.Visibility == Visibility.Collapsed && scene.History.Count == (olderReply ? 2 : 1),
                "a confirmed send moves to history once despite duplicate focus notifications");
            check(scene.PastText(olderReply ? 1 : 0) == "보낸 글"
                && (olderReply ? scene.PastText(0) == "답변" : scene.Reply.Visibility == Visibility.Visible),
                "focus dismissal keeps the reply and sent message in conversation order");
        }

        foreach (bool question in new[] { true, false })
        using (var scene = new Scene())
        {
            scene.Call("OpenReplyBubble", "c", "reply");
            ((MarkdownView)scene.Window.FindName("Message")).Text = "먼저 온 답변";
            scene.Call("OpenComposer"); scene.Call("ShowSentMessage", "나중에 보낸 글");
            scene.Field("awaitingAnswer", question);
            ((FrameworkElement)scene.Window.FindName("StopButton")).Visibility = question ? Visibility.Collapsed : Visibility.Visible;
            scene.InFront = false; scene.Dismiss(); Pump();
            check(scene.History.Count == 0 && scene.User.Visibility == Visibility.Visible && scene.Reply.Visibility == Visibility.Visible,
                "an older question or streaming reply keeps its controls and order on focus loss");
            check(!question || (bool)scene.Field("awaitingAnswer")!, "focus loss never clears a pending question");
        }

        foreach (bool screen in new[] { false, true })
        using (var scene = new Scene())
        using (var server = new ServerClient("https://example.com/", "test", new ReplyReconnectHandler()))
        {
            scene.Call("OpenComposer"); scene.Editor.Text = "접수 여부를 확인할 글";
            object unresolved;
            string field;
            if (screen)
            {
                var type = typeof(MainWindow).GetNestedType("LocalScreen", BindingFlags.NonPublic)!;
                var local = Activator.CreateInstance(type, Inside, null,
                    ["screen-id", new ScreenGeometry(new PixelRect(0, 0, 100, 100), 100, 100), DateTimeOffset.UtcNow, true], null)!;
                field = "unresolvedScreen";
                unresolved = Activator.CreateInstance(typeof(MainWindow).GetField(field, Inside)!.FieldType.GenericTypeArguments[0], "c", "text", local)!;
            }
            else
            {
                field = "unknownText";
                unresolved = Activator.CreateInstance(typeof(MainWindow).GetNestedType("UnknownText", BindingFlags.NonPublic)!, Inside,
                    null, [server, "c", "text", "d", 1, null, null], null)!;
            }
            scene.Field(field, unresolved);
            var status = (FrameworkElement)scene.Window.FindName("UserStatusPanel"); status.Visibility = Visibility.Visible;
            scene.InFront = false; scene.Dismiss(); Pump();
            check(scene.User.Visibility == Visibility.Visible && status.Visibility == Visibility.Visible && Equals(scene.Field(field), unresolved),
                "unknown text/screen delivery keeps its confirmation UI and recovery state");
        }

        using (var token = new CancellationTokenSource())
        using (var scene = new Scene())
        {
            var transport = new ReplyReconnectHandler { DeferAcceptance = true };
            using var server = new ServerClient("https://example.com/", "test", transport);
            scene.Field("client", server); scene.Field("recoveryOwner", server); scene.Field("connection", token);
            scene.Field("currentConversation", "c"); scene.Field("deviceId", "d");
            var settings = (Window)scene.Field("settings")!;
            ((CheckBox)settings.FindName("ScreenSharing")).IsChecked = false;
            scene.Call("SetConnected", true, "연결됨"); scene.Call("OpenComposer"); scene.Editor.Text = "보낼 글";
            scene.Call("SendMessage"); Until(() => transport.Posts == 1, check);
            scene.Editor.Text = "전송 중에 쓴 새 초안"; scene.InFront = false; scene.Dismiss(); Pump();
            check((bool)scene.Field("sending")! && scene.User.Visibility == Visibility.Visible, "an in-flight POST protects the user bubble");
            transport.ReleaseAcceptance(); Until(() => !(bool)scene.Field("sending")!, check); Pump();
            check(scene.User.Visibility == Visibility.Collapsed && scene.Editor.Text == "전송 중에 쓴 새 초안",
                "POST completion rechecks focus and hides a newer draft without losing it");
        }

        foreach (bool before in new[] { true, false })
        foreach (bool after in new[] { false, true })
        using (var scene = new Scene())
        {
            var transport = new QuestionHandler();
            using var server = new ServerClient("https://example.com/", "test", transport);
            scene.Field("client", server); scene.Field("currentConversation", "c");
            scene.Call("OpenReplyBubble", "c", "earlier");
            ((MarkdownView)scene.Window.FindName("Message")).Text = "이전 답변";
            scene.Call("OpenComposer"); scene.Editor.Text = "질문이 와도 남길 초안"; scene.Editor.Select(2, 3);
            scene.InFront = before;
            if (!before) { scene.Dismiss(); Pump(); }
            var read = (Task)scene.Call("ReadConversation", "c", CancellationToken.None)!;
            Until(() => transport.Requested, check);
            // Change foreground while the HTTP detail is in flight. No focus event
            // follows its completion, so the question path itself must obey the policy.
            scene.InFront = after;
            if (!after) { scene.Dismiss(); Pump(); }
            transport.Release(); Until(() => read.IsCompleted, check); read.GetAwaiter().GetResult(); Pump();
            check(scene.Reply.Visibility == Visibility.Visible && (bool)scene.Field("awaitingAnswer")!
                && ((MarkdownView)scene.Window.FindName("Message")).Text == "어느 쪽으로 갈까요?"
                && scene.History.Count == 1 && scene.PastText(0) == "이전 답변",
                "an asynchronous question remains visible and pending even outside the chat");
            var composer = (FrameworkElement)scene.Window.FindName("ComposerPanel");
            check(scene.User.Visibility == (after ? Visibility.Visible : Visibility.Collapsed)
                && composer.Visibility == (after ? Visibility.Visible : Visibility.Collapsed)
                && scene.Editor.Text == "질문이 와도 남길 초안" && scene.Editor.SelectionStart == 2 && scene.Editor.SelectionLength == 3,
                "question receipt opens the editor only when chat is foreground at completion, preserving its draft");
            if (!after)
            {
                var again = (Task)scene.Call("ReadConversation", "c", CancellationToken.None)!;
                Until(() => again.IsCompleted, check); again.GetAwaiter().GetResult(); Pump();
                check(scene.User.Visibility == Visibility.Collapsed && (bool)scene.Field("awaitingAnswer")!,
                    "repeated background question refreshes do not reopen the editor");
                scene.InFront = true; scene.Call("OpenChat"); Pump();
                check(scene.User.Visibility == Visibility.Visible && composer.Visibility == Visibility.Visible
                    && scene.Editor.Text == "질문이 와도 남길 초안" && (bool)scene.Field("awaitingAnswer")!,
                    "returning to chat opens the preserved draft for the pending question");
            }
        }

        using (var scene = new Scene())
        {
            scene.Call("OpenComposer"); scene.InFront = false; scene.Dismiss(); scene.Window.Close(); Pump();
            check(scene.User.Visibility == Visibility.Visible, "a queued dismissal does nothing after the window closes");
        }
    }
}
