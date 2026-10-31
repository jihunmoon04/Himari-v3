using System.Windows;
using System.Windows.Documents;
using System.Windows.Media;

namespace Himari.Companion;

public enum BubbleGlyphKind { Send, Stop, Expand, Collapse, Copy, Check, Monitor, MonitorOff, Refresh, Warning, Help }

// Every bubble action shares one 14-DIP coordinate space and one stroke weight.
public sealed class BubbleGlyph : FrameworkElement
{
    public static readonly DependencyProperty KindProperty = DependencyProperty.Register(nameof(Kind), typeof(BubbleGlyphKind), typeof(BubbleGlyph), new FrameworkPropertyMetadata(BubbleGlyphKind.Send, FrameworkPropertyMetadataOptions.AffectsRender));
    public static readonly DependencyProperty ForegroundProperty = TextElement.ForegroundProperty.AddOwner(typeof(BubbleGlyph), new FrameworkPropertyMetadata(System.Windows.Media.Brushes.Black, FrameworkPropertyMetadataOptions.Inherits | FrameworkPropertyMetadataOptions.AffectsRender));
    public BubbleGlyphKind Kind { get => (BubbleGlyphKind)GetValue(KindProperty); set => SetValue(KindProperty, value); }
    public System.Windows.Media.Brush Foreground { get => (System.Windows.Media.Brush)GetValue(ForegroundProperty); set => SetValue(ForegroundProperty, value); }
    protected override void OnRender(DrawingContext dc)
    {
        var pen = new System.Windows.Media.Pen(Foreground, 1.5) { StartLineCap = PenLineCap.Square, EndLineCap = PenLineCap.Square, LineJoin = PenLineJoin.Miter };
        string path = Kind switch
        {
            BubbleGlyphKind.Stop => "M3,3 L11,3 11,11 3,11 Z",
            BubbleGlyphKind.Expand => "M2,5 L2,2 5,2 M2,2 L5,5 M9,12 L12,12 12,9 M9,9 L12,12",
            BubbleGlyphKind.Collapse => "M2,5 L5,5 5,2 M2,2 L5,5 M9,12 L9,9 12,9 M9,9 L12,12",
            BubbleGlyphKind.Copy => "M5,5 L12,5 12,12 5,12 Z M2,9 L2,2 9,2",
            BubbleGlyphKind.Check => "M2,7 L5.5,10.5 12,3.5",
            BubbleGlyphKind.Monitor => "M1.5,2.5 L12.5,2.5 12.5,10 1.5,10 Z M7,10 L7,12.5 M4.5,12.5 L9.5,12.5",
            BubbleGlyphKind.MonitorOff => "M4,2.5 L12.5,2.5 12.5,8 M1.5,5 L1.5,10 9,10 M7,10 L7,12.5 M4.5,12.5 L9.5,12.5 M1,1 L13,13",
            BubbleGlyphKind.Refresh => "M11.5,5 A5,5 0 1 0 12,9 M9,5 L12,5 12,2",
            BubbleGlyphKind.Warning => "M7,1.5 L13,12.5 1,12.5 Z M7,5 L7,8 M7,10 L7,10.5",
            BubbleGlyphKind.Help => "M7,1 A6,6 0 1 1 6.99,1 M5,5 A2,2 0 1 1 8,7 L7,8 M7,10 L7,10.5",
            _ => "M7,12 L7,2 M3,6 L7,2 11,6"
        };
        dc.PushTransform(new ScaleTransform(ActualWidth / 14, ActualHeight / 14));
        dc.DrawGeometry(null, pen, Geometry.Parse(path)); dc.Pop();
    }
}
