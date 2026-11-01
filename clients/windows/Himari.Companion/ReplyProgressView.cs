using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;

namespace Himari.Companion;

// Presentation only: waiting labels never enter the Markdown source or history.
public sealed class ReplyProgressView : Grid
{
    // Same vocabulary as the web's sun theme (static/live.js, THINKING_WORDS.sun).
    private static readonly string[] Words = ["Turning to the sun", "Blooming", "Basking", "Unfurling", "Gathering light", "Following the sun", "Soaking up sunlight", "Opening petals"];
    private static readonly string[] Glyphs = ["·", "✢", "✣", "❋", "✾", "❀", "✾", "❋", "✣", "✢"];
    private readonly TextBlock glyph = new() { Width = 20, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock label = new() { TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock elapsed = new() { Margin = new Thickness(4, 0, 0, 0), VerticalAlignment = VerticalAlignment.Center };
    private readonly Stopwatch clock = new();
    private readonly DispatcherTimer ticker = new() { Interval = TimeSpan.FromMilliseconds(120) };
    private string? identity;
    private string word = "";
    private int frame;

    public ReplyProgressView()
    {
        ColumnDefinitions.Add(new() { Width = GridLength.Auto });
        ColumnDefinitions.Add(new() { Width = new GridLength(1, GridUnitType.Star) });
        ColumnDefinitions.Add(new() { Width = GridLength.Auto });
        SetColumn(label, 1); SetColumn(elapsed, 2);
        Children.Add(glyph); Children.Add(label); Children.Add(elapsed);
        Margin = new Thickness(0, 3, 0, 3);
        ticker.Tick += (_, _) => Paint();
        IsVisibleChanged += (_, _) => { if (IsVisible && clock.IsRunning) ticker.Start(); else ticker.Stop(); };
        Unloaded += (_, _) => ticker.Stop();
    }

    internal void Show(string key, string? tool, bool connected, bool stopping)
    {
        if (identity != key)
        {
            identity = key; word = Words[Random.Shared.Next(Words.Length)]; frame = 0; clock.Restart();
        }
        bool active = connected && !stopping;
        label.Text = stopping ? "답변 중지 요청을 보냈어요." : !connected ? "다시 연결하는 중이에요…" : tool is null ? word + "…" : $"Preparing {tool}…";
        System.Windows.Automation.AutomationProperties.SetName(this, stopping || !connected ? label.Text : tool is null ? "답변을 준비하고 있어요." : label.Text);
        if (active) clock.Start(); else clock.Stop();
        Visibility = Visibility.Visible;
        if (active && IsVisible) ticker.Start(); else ticker.Stop();
        Paint();
    }

    private void Paint()
    {
        glyph.Text = Glyphs[SystemParameters.ClientAreaAnimation && clock.IsRunning ? frame++ % Glyphs.Length : 4];
        int seconds = (int)clock.Elapsed.TotalSeconds;
        elapsed.Text = seconds < 60 ? $"· {seconds}s" : $"· {seconds / 60}m {seconds % 60:00}s";
    }

    internal void Reset()
    {
        ticker.Stop(); clock.Reset(); identity = null; Visibility = Visibility.Collapsed;
    }
}
