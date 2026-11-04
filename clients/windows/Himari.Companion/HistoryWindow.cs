using System.IO;
using System.Net;
using System.Net.Http;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Himari.Companion;

internal sealed class HistoryWindow : Window
{
    private readonly System.Windows.Controls.TextBox output = new() { IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, BorderThickness = new Thickness(0), Padding = new Thickness(16), FontSize = 14 };
    private readonly StackPanel filesPanel = new() { Margin = new Thickness(16, 8, 16, 12) };
    private readonly Border filesSection;
    private readonly CancellationTokenSource close = new();
    internal HistoryWindow(Window owner, ServerClient client, string conversationId, string title, CancellationToken parent)
    {
        Owner = owner; Title = $"대화 기록 · {title}"; Width = 580; Height = 650; MinWidth = 320; MinHeight = 300;
        WindowStartupLocation = WindowStartupLocation.CenterOwner; Background = System.Windows.Media.Brushes.White;
        filesSection = new Border
        {
            BorderBrush = new SolidColorBrush(System.Windows.Media.Color.FromRgb(220, 220, 228)),
            BorderThickness = new Thickness(0, 1, 0, 0),
            Background = new SolidColorBrush(System.Windows.Media.Color.FromRgb(250, 249, 253)),
            Visibility = Visibility.Collapsed,
            Child = new ScrollViewer
            {
                MaxHeight = 160,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                Content = filesPanel
            }
        };
        var root = new Grid();
        root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        Grid.SetRow(output, 0);
        Grid.SetRow(filesSection, 1);
        root.Children.Add(output);
        root.Children.Add(filesSection);
        Content = root;
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
            await LoadPresentedFilesAsync(client, conversationId, linked.Token);
        }
        catch (OperationCanceledException) when (linked.IsCancellationRequested) { }
        catch (Exception) { output.Text = loaded + "\n대화 기록을 불러오지 못했습니다. 연결 상태를 확인해 주세요."; }
    }
    private async Task LoadPresentedFilesAsync(ServerClient client, string conversationId, CancellationToken ct)
    {
        List<PresentedFileItem> items;
        try
        {
            items = await client.ReadAllPresentedFilesAsync(conversationId, ct);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch (Exception) { return; }
        filesPanel.Children.Clear();
        if (items.Count == 0)
        {
            filesSection.Visibility = Visibility.Collapsed;
            return;
        }
        foreach (var item in items)
        {
            string safeName = PresentedFileNames.SanitizeSaveFileName(item.Name);
            string meta = $"{PresentedFileNames.FormatSize(item.Size)} · {PresentedFileNames.VersionLabel(item.Version)}";
            var statusLabel = new TextBlock { FontSize = 11, Margin = new Thickness(0, 2, 0, 0), Visibility = item.Expired ? Visibility.Visible : Visibility.Collapsed, Text = item.Expired ? "만료됨" : "" };
            var button = new System.Windows.Controls.Button
            {
                Content = item.Expired ? "만료됨" : "저장",
                IsEnabled = !item.Expired,
                Padding = new Thickness(10, 4, 10, 4),
                VerticalAlignment = VerticalAlignment.Center
            };
            button.Click += async (_, _) => await SaveFromHistoryAsync(client, conversationId, item, safeName, button, statusLabel, ct);
            var row = new Grid { Margin = new Thickness(0, 4, 0, 4) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var infoStack = new StackPanel
            {
                Children =
                {
                    new TextBlock { Text = safeName, FontWeight = FontWeights.SemiBold, FontSize = 13, TextTrimming = TextTrimming.CharacterEllipsis },
                    new TextBlock { Text = meta, FontSize = 11, Foreground = System.Windows.Media.Brushes.DimGray },
                    statusLabel
                }
            };
            Grid.SetColumn(infoStack, 0);
            Grid.SetColumn(button, 1);
            row.Children.Add(infoStack);
            row.Children.Add(button);
            filesPanel.Children.Add(row);
        }
        filesSection.Visibility = Visibility.Visible;
    }
    private async Task SaveFromHistoryAsync(ServerClient client, string conversationId, PresentedFileItem item, string suggestedName, System.Windows.Controls.Button button, TextBlock statusLabel, CancellationToken ct)
    {
        string? destination = PresentedFileSaveDialog.ChoosePath(this, suggestedName);
        if (string.IsNullOrWhiteSpace(destination)) return;
        ApprovedSaveTarget target;
        try
        {
            target = ApprovedSaveTarget.Capture(destination);
        }
        catch (SaveTargetChangedException ex)
        {
            statusLabel.Text = ex.Message;
            statusLabel.Visibility = Visibility.Visible;
            return;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException)
        {
            statusLabel.Text = "저장 위치를 확인하지 못했어요.";
            statusLabel.Visibility = Visibility.Visible;
            return;
        }
        button.IsEnabled = false;
        button.Content = "저장 중…";
        statusLabel.Visibility = Visibility.Collapsed;
        try
        {
            await client.DownloadPresentedFileAsync(conversationId, item.FileId, item.Version, target, item.Size, item.Sha256, ct);
            button.Content = "다시 저장";
            button.IsEnabled = true;
            statusLabel.Text = "저장됨";
            statusLabel.Visibility = Visibility.Visible;
        }
        catch (Exception ex)
        {
            button.Content = "저장";
            button.IsEnabled = true;
            statusLabel.Text = ex is SaveTargetChangedException
                ? ex.Message
                : ex is InvalidDataException
                    ? "파일 검증에 실패해 저장하지 않았어요."
                    : ex is ApiException { Code: "file_expired" }
                        ? "만료되어 저장할 수 없어요."
                        : "저장하지 못했어요. 다시 시도해 주세요.";
            statusLabel.Visibility = Visibility.Visible;
        }
    }
}
