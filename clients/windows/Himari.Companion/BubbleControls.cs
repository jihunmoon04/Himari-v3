using System.ComponentModel;
using System.IO;
using System.Net;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using Brush = System.Windows.Media.Brush;
using Button = System.Windows.Controls.Button;
using Orientation = System.Windows.Controls.Orientation;

namespace Himari.Companion;

public partial class MainWindow
{
    internal sealed class PresentedFileCardState(
        PresentedFileCardKey key,
        PresentedFileItem item,
        Border root,
        TextBlock nameText,
        TextBlock metaText,
        TextBlock stateText,
        Button saveButton,
        TextBlock saveButtonText)
    {
        internal PresentedFileCardKey Key { get; } = key;
        internal PresentedFileItem Item { get; set; } = item;
        internal Border Root { get; } = root;
        internal TextBlock NameText { get; } = nameText;
        internal TextBlock MetaText { get; } = metaText;
        internal TextBlock StateText { get; } = stateText;
        internal Button SaveButton { get; } = saveButton;
        internal TextBlock SaveButtonText { get; } = saveButtonText;
        internal bool Saving { get; set; }
        internal bool Saved { get; set; }
        internal string? ErrorMessage { get; set; }
    }

    private bool replyStopRequested;
    private BubbleStatusAction bubbleStatusAction;
    private readonly DispatcherTimer copiedTimer = new() { Interval = TimeSpan.FromSeconds(1.5) };
    private Action<string> copyText = System.Windows.Clipboard.SetText;
    private Func<string, string?>? chooseSavePath = null;
    private bool saveDialogOpen;
    private readonly Dictionary<PresentedFileCardKey, PresentedFileCardState> currentFileCards = new();
    private readonly HashSet<PresentedFileCardKey> savedFileCards = new();
    private readonly HashSet<PresentedFileCardKey> dismissedFileCards = new();
    private readonly HashSet<CancellationTokenSource> activeFileDownloads = new();
    private bool HasActiveFileCardInteraction =>
        saveDialogOpen || currentFileCards.Values.Any(c => c.Saving);
    private static readonly DependencyPropertyDescriptor StatusTextDescriptor = DependencyPropertyDescriptor.FromProperty(TextBlock.TextProperty, typeof(TextBlock));
    private static readonly DependencyPropertyDescriptor StopVisibilityDescriptor = DependencyPropertyDescriptor.FromProperty(UIElement.VisibilityProperty, typeof(System.Windows.Controls.Button));
    private EventHandler? bubblePresentationChanged;

    private void SetupBubblePresentation()
    {
        bubblePresentationChanged = (_, _) => UpdateBubblePresentation();
        StatusTextDescriptor.AddValueChanged(Status, bubblePresentationChanged);
        StopVisibilityDescriptor.AddValueChanged(StopButton, bubblePresentationChanged);
        copiedTimer.Tick += (_, _) => ResetCopyFeedback();
        UpdateBubblePresentation();
    }
    private void CleanupBubblePresentation()
    {
        CancelFileDownloads();
        ReplyProgress.Reset();
        copiedTimer.Stop();
        if (bubblePresentationChanged is null) return;
        StatusTextDescriptor.RemoveValueChanged(Status, bubblePresentationChanged);
        StopVisibilityDescriptor.RemoveValueChanged(StopButton, bubblePresentationChanged);
    }
    private void UpdateBubblePresentation()
    {
        if (UserStatus is null || SendButton is null || settings is null) return;
        var notice = connected && (unknownText is not null || unresolvedScreen is not null) ? BubbleStatus.Pending : BubbleStatus.From(Status.Text);
        UserStatus.Text = notice.Text;
        UserStatus.Visibility = UserStatusPanel.Visibility = notice.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        var brush = (System.Windows.Media.Brush)FindResource(notice.Warning ? "BubbleWarning" : "BubbleMuted");
        UserStatus.Foreground = UserStatusGlyph.Foreground = StatusActionButton.Foreground = brush;
        UserStatusGlyph.Kind = notice.Icon;
        bubbleStatusAction = notice.Action;
        StatusActionButton.Visibility = notice.Action == BubbleStatusAction.None ? Visibility.Collapsed : Visibility.Visible;
        StatusActionButton.IsEnabled = notice.Action != BubbleStatusAction.History || connected;
        StatusActionText.Text = notice.Action == BubbleStatusAction.Settings ? "연결 설정" : "확인";
        System.Windows.Automation.AutomationProperties.SetName(StatusActionButton, StatusActionText.Text);
        bool streaming = StopButton.Visibility == Visibility.Visible;
        if (ReplyPreparing) ReplyProgress.Show($"{replyProgressConversation}:{replyProgressJob}:{replyProgressRun}", replyProgressTool, connected, replyStopRequested);
        else ReplyProgress.Reset();
        bool hideEmptyMessageForFiles = !ReplyPreparing && !replyExpanded && string.IsNullOrWhiteSpace(Message.Text) && currentFileCards.Count > 0;
        Message.Visibility = ReplyPreparing || replyExpanded || hideEmptyMessageForFiles ? Visibility.Collapsed : Visibility.Visible;
        ExpandedReply.Visibility = !ReplyPreparing && replyExpanded ? Visibility.Visible : Visibility.Collapsed;
        ExpandButton.Visibility = ReplyPreparing || hideEmptyMessageForFiles ? Visibility.Collapsed : Visibility.Visible;
        if (PresentedFilesScroll is not null)
            PresentedFilesScroll.Visibility = !ReplyPreparing && currentFileCards.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        foreach (var card in currentFileCards.Values) UpdatePresentedFileCard(card);
        ReplyStatus.Text = ReplyPreparing ? "" : replyStopRequested ? "답변 중지 요청을 보냈어요." : streaming ? "답변 중이에요…" : "";
        ReplyStatus.Visibility = ReplyStatus.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        CopyReplyButton.Visibility = streaming ? Visibility.Collapsed : Visibility.Visible;
        CopyReplyButton.IsEnabled = !string.IsNullOrWhiteSpace(Message.Text);
        SendButton.Tag = string.IsNullOrWhiteSpace(Input.Text) ? "" : "ready";
        SendButton.IsEnabled = connected && !sending && (!string.IsNullOrWhiteSpace(Input.Text) || WantsScreen || unresolvedScreen is not null);
        SendButton.ToolTip = string.IsNullOrWhiteSpace(Input.Text) && WantsScreen ? "화면만 보내기 · Enter" : "보내기 · Enter";
        UpdateUnsentBubbles();
    }

    private void DismissCurrentFileCards()
    {
        foreach (var key in currentFileCards.Keys) dismissedFileCards.Add(key);
    }

    private void ClearPresentedFileCards()
    {
        currentFileCards.Clear();
        PresentedFilesPanel?.Children.Clear();
        if (PresentedFilesScroll is not null) PresentedFilesScroll.Visibility = Visibility.Collapsed;
    }

    private void CancelFileDownloads()
    {
        foreach (var cts in activeFileDownloads.ToArray())
        {
            try { cts.Cancel(); cts.Dispose(); } catch (ObjectDisposedException) { }
        }
        activeFileDownloads.Clear();
        foreach (var card in currentFileCards.Values)
        {
            if (card.Saving)
            {
                card.Saving = false;
                card.ErrorMessage = "저장이 취소되었어요.";
                UpdatePresentedFileCard(card);
            }
        }
    }

    /// <summary>V3-P93 §6.3: the reply bubble shows only the files presented in the
    /// latest reply turn (<paramref name="turnFiles"/>); earlier ones stay in history.</summary>
    private async Task<bool> RefreshPresentedFilesAsync(ServerClient local, string conversationId,
        IReadOnlySet<(string FileId, int Version)> turnFiles, CancellationToken ct)
    {
        if (turnFiles.Count == 0)
        {
            if (currentFileCards.Count > 0) { ClearPresentedFileCards(); UpdateBubblePresentation(); UpdateSceneBounds(); }
            return false;
        }
        if (!generationState.ServerTakesFileDownload)
        {
            if (currentFileCards.Count > 0) { ClearPresentedFileCards(); UpdateBubblePresentation(); UpdateSceneBounds(); }
            return false;
        }
        List<PresentedFileItem> items;
        try
        {
            items = await local.ReadAllPresentedFilesAsync(conversationId, ct);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch (Exception) { return false; }
        if (ct.IsCancellationRequested || !ReferenceEquals(local, client) || conversationId != currentConversation) return false;
        return ReconcilePresentedFiles(local, conversationId, items.Where(i => turnFiles.Contains((i.FileId, i.Version))).ToList());
    }

    private bool ReconcilePresentedFiles(ServerClient local, string conversationId, IReadOnlyList<PresentedFileItem> items)
    {
        if (PresentedFilesPanel is null || PresentedFilesScroll is null) return false;
        if (items.Count == 0)
        {
            bool hadCards = currentFileCards.Count > 0;
            ClearPresentedFileCards();
            if (hadCards) { UpdateBubblePresentation(); UpdateSceneBounds(); }
            return false;
        }
        bool hasUndismissed = false;
        var orderedStates = new List<PresentedFileCardState>(items.Count);
        var seenKeys = new HashSet<PresentedFileCardKey>();
        foreach (var item in items)
        {
            var key = local.CardKey(conversationId, item.FileId, item.Version);
            if (!seenKeys.Add(key)) continue;
            if (!dismissedFileCards.Contains(key)) hasUndismissed = true;
            if (currentFileCards.TryGetValue(key, out var existing))
            {
                existing.Item = item;
                if (savedFileCards.Contains(key)) existing.Saved = true;
                UpdatePresentedFileCard(existing);
                orderedStates.Add(existing);
            }
            else
            {
                var created = CreatePresentedFileCard(key, item);
                if (savedFileCards.Contains(key)) created.Saved = true;
                UpdatePresentedFileCard(created);
                currentFileCards[key] = created;
                orderedStates.Add(created);
            }
        }
        foreach (var staleKey in currentFileCards.Keys.Where(k => !seenKeys.Contains(k)).ToList())
            currentFileCards.Remove(staleKey);
        if (!PresentedFilesPanel.Children.Cast<UIElement>().SequenceEqual(orderedStates.Select(s => s.Root)))
        {
            PresentedFilesPanel.Children.Clear();
            foreach (var state in orderedStates) PresentedFilesPanel.Children.Add(state.Root);
        }
        UpdateBubblePresentation();
        UpdateSceneBounds();
        return hasUndismissed;
    }

    private PresentedFileCardState CreatePresentedFileCard(PresentedFileCardKey key, PresentedFileItem item)
    {
        var nameText = new TextBlock
        {
            FontSize = 13,
            FontWeight = FontWeights.SemiBold,
            Foreground = (Brush)FindResource("BubbleInk"),
            TextTrimming = TextTrimming.CharacterEllipsis
        };
        var metaText = new TextBlock
        {
            FontSize = 11,
            Foreground = (Brush)FindResource("BubbleMuted"),
            Margin = new Thickness(0, 1, 0, 0)
        };
        var stateText = new TextBlock
        {
            FontSize = 11,
            Margin = new Thickness(0, 2, 0, 0),
            TextWrapping = TextWrapping.Wrap,
            Visibility = Visibility.Collapsed
        };
        var saveButtonText = new TextBlock { Text = "저장", TextDecorations = TextDecorations.Underline };
        var saveButton = new Button
        {
            Style = (Style)FindResource("BubbleTextButton"),
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(8, 0, 0, 0),
            Content = saveButtonText
        };
        var leftStack = new StackPanel { Children = { nameText, metaText, stateText } };
        var grid = new Grid();
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        Grid.SetColumn(leftStack, 0);
        Grid.SetColumn(saveButton, 1);
        grid.Children.Add(leftStack);
        grid.Children.Add(saveButton);
        var border = new Border
        {
            BorderBrush = (Brush)FindResource("BubbleSeparator"),
            BorderThickness = new Thickness(1),
            Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(250, 247, 255)),
            CornerRadius = new CornerRadius(2),
            Padding = new Thickness(8, 6, 8, 6),
            Margin = new Thickness(0, 4, 0, 0),
            Child = grid
        };
        var card = new PresentedFileCardState(key, item, border, nameText, metaText, stateText, saveButton, saveButtonText);
        saveButton.Click += (_, _) => _ = SavePresentedFileAsync(card);
        return card;
    }

    private void UpdatePresentedFileCard(PresentedFileCardState card)
    {
        string safeName = PresentedFileNames.SanitizeSaveFileName(card.Item.Name);
        string versionLabel = PresentedFileNames.VersionLabel(card.Item.Version);
        card.NameText.Text = safeName;
        card.NameText.ToolTip = safeName;
        card.MetaText.Text = $"{PresentedFileNames.FormatSize(card.Item.Size)} · {versionLabel}";
        card.SaveButtonText.Text = card.Saving ? "저장 중…" : card.Saved ? "다시 저장" : "저장";
        card.SaveButton.IsEnabled = connected && !card.Saving && !card.Item.Expired;
        System.Windows.Automation.AutomationProperties.SetName(card.SaveButton, $"파일 저장: {safeName} ({versionLabel})");
        if (card.Saving)
        {
            card.StateText.Text = "저장 중…";
            card.StateText.Foreground = (Brush)FindResource("BubbleMuted");
            card.StateText.Visibility = Visibility.Visible;
        }
        else if (!string.IsNullOrEmpty(card.ErrorMessage))
        {
            card.StateText.Text = card.ErrorMessage;
            card.StateText.Foreground = (Brush)FindResource("BubbleWarning");
            card.StateText.Visibility = Visibility.Visible;
        }
        else if (card.Saved)
        {
            card.StateText.Text = "저장됨";
            card.StateText.Foreground = (Brush)FindResource("BubbleAccent");
            card.StateText.Visibility = Visibility.Visible;
        }
        else if (card.Item.Expired)
        {
            card.StateText.Text = "만료됨";
            card.StateText.Foreground = (Brush)FindResource("BubbleWarning");
            card.StateText.Visibility = Visibility.Visible;
        }
        else
        {
            card.StateText.Text = "";
            card.StateText.Visibility = Visibility.Collapsed;
        }
    }

    private string? ChooseSavePathDialog(string suggestedFileName)
    {
        Window owner = dialogueHost is { IsVisible: true } ? dialogueHost : this;
        return PresentedFileSaveDialog.ChoosePath(owner, suggestedFileName);
    }

    private async Task SavePresentedFileAsync(PresentedFileCardState card)
    {
        if (!connected || client is not { } local || connection is null || card.Saving || card.Item.Expired) return;
        if (card.Key.ConversationId != currentConversation || !local.MatchesCard(card.Key)) return;
        string suggested = PresentedFileNames.SanitizeSaveFileName(card.Item.Name);
        ApprovedSaveTarget? destinationTarget = null;
        saveDialogOpen = true;
        try
        {
            string? destination = (chooseSavePath ?? ChooseSavePathDialog)(suggested);
            if (!string.IsNullOrWhiteSpace(destination))
                destinationTarget = ApprovedSaveTarget.Capture(destination);
        }
        catch (SaveTargetChangedException ex)
        {
            card.ErrorMessage = ex.Message;
            UpdatePresentedFileCard(card);
            DismissUserBubbleIfUnfocused();
            return;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException)
        {
            card.ErrorMessage = "저장 위치를 확인하지 못했어요. 다시 시도해 주세요.";
            UpdatePresentedFileCard(card);
            DismissUserBubbleIfUnfocused();
            return;
        }
        finally
        {
            saveDialogOpen = false;
        }
        if (destinationTarget is null)
        {
            DismissUserBubbleIfUnfocused();
            return;
        }
        if (!connected || !ReferenceEquals(local, client) || connection is null || !local.MatchesCard(card.Key))
        {
            DismissUserBubbleIfUnfocused();
            return;
        }
        using var downloadCts = CancellationTokenSource.CreateLinkedTokenSource(connection.Token);
        activeFileDownloads.Add(downloadCts);
        card.Saving = true;
        card.Saved = false;
        card.ErrorMessage = null;
        UpdatePresentedFileCard(card);
        UpdateSceneBounds();
        try
        {
            await local.DownloadPresentedFileAsync(
                card.Key.ConversationId,
                card.Item.FileId,
                card.Item.Version,
                destinationTarget,
                card.Item.Size,
                card.Item.Sha256,
                downloadCts.Token);
            if (!ReferenceEquals(local, client) || !local.MatchesCard(card.Key)) return;
            card.Saving = false;
            card.Saved = true;
            card.ErrorMessage = null;
            savedFileCards.Add(card.Key);
            UpdatePresentedFileCard(card);
        }
        catch (Exception ex)
        {
            card.Saving = false;
            card.Saved = false;
            if (ex is ApiException { Code: "file_expired" })
            {
                card.Item = card.Item with { Expired = true };
                card.ErrorMessage = "만료되어 저장할 수 없어요.";
            }
            else if (ex is ApiException { Code: "file_missing" or "not_found" })
                card.ErrorMessage = "서버에서 파일을 찾을 수 없어요.";
            else if (ex is ApiException { Code: "device_scope" } || ex is ApiException { Status: HttpStatusCode.Unauthorized })
                card.ErrorMessage = "저장 권한이 없습니다.";
            else if (ex is SaveTargetChangedException)
                card.ErrorMessage = ex.Message;
            else if (ex is InvalidDataException)
                card.ErrorMessage = "파일 검증에 실패해 저장하지 않았어요.";
            else if (ex is OperationCanceledException)
                card.ErrorMessage = "저장이 취소되었어요.";
            else
                card.ErrorMessage = "저장하지 못했어요. 다시 시도해 주세요.";
            UpdatePresentedFileCard(card);
        }
        finally
        {
            activeFileDownloads.Remove(downloadCts);
            if (assistantBubbleCloseDue && BubblePanel.Visibility == Visibility.Visible) RetryExpiry(bubbleTimer);
            UpdateSceneBounds();
            DismissUserBubbleIfUnfocused();
        }
    }
    /// <summary>V3-P91 §7.3: the next message, or every one (that is set in the settings only).</summary>
    private bool WantsScreen => settings.ScreenSharing.IsChecked == true || screenOnce;
    private void ScreenSharingClick(object sender, RoutedEventArgs e)
    {
        if (settings.ScreenSharing.IsChecked == true) Status.Text = "메시지마다 화면을 붙이는 것은 설정의 대화 화면 탭에서 끕니다.";
        else { screenOnce = !screenOnce; UpdateScreenButton(); UpdateBubblePresentation(); }
        FocusComposerIfOpen();
    }
    private void CopyReplyClick(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(Message.Text)) return;
        try
        {
            copyText(Message.Text);
            CopyReplyGlyph.Kind = BubbleGlyphKind.Check;
            CopyReplyButton.ToolTip = "복사했어요.";
            copiedTimer.Stop(); copiedTimer.Start();
        }
        catch (System.Runtime.InteropServices.ExternalException) { Status.Text = "복사하지 못했어요. 다시 시도해 주세요."; }
    }
    private void ResetCopyFeedback()
    {
        copiedTimer.Stop(); CopyReplyGlyph.Kind = BubbleGlyphKind.Copy; CopyReplyButton.ToolTip = "답변 복사";
    }
    private void StatusActionClick(object sender, RoutedEventArgs e)
    {
        if (bubbleStatusAction == BubbleStatusAction.Settings) ShowSettings();
        else if (bubbleStatusAction == BubbleStatusAction.History)
        {
            if (!connected || client is null || connection is null) return;
            if (unknownText is not null && !unknownText.Server.SameDevice(client)) { ShowSettings(); return; }
            string? conversation = unknownText?.Conversation ?? unresolvedScreen?.Conversation ?? currentConversation;
            if (conversation is not null) new HistoryWindow(this, client, conversation, "전송 기록", connection.Token).Show();
        }
    }
}
