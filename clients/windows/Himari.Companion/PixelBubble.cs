using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Point = System.Windows.Point;
using Size = System.Windows.Size;

namespace Himari.Companion;

// Code-native pixel frame: all corners and the tail share a four-DIP grid.
// Corners do not stretch when the text grows; text remains an ordinary WPF control.
public sealed class PixelBubble : Decorator
{
    public static readonly DependencyProperty IsUserProperty = DependencyProperty.Register(nameof(IsUser), typeof(bool), typeof(PixelBubble), new FrameworkPropertyMetadata(false, FrameworkPropertyMetadataOptions.AffectsRender));
    public static readonly DependencyProperty TailRightProperty = DependencyProperty.Register(nameof(TailRight), typeof(bool), typeof(PixelBubble), new FrameworkPropertyMetadata(true, FrameworkPropertyMetadataOptions.AffectsRender));
    public static readonly DependencyProperty TailTopProperty = DependencyProperty.Register(nameof(TailTop), typeof(bool), typeof(PixelBubble), new FrameworkPropertyMetadata(false, FrameworkPropertyMetadataOptions.AffectsRender | FrameworkPropertyMetadataOptions.AffectsArrange));
    public bool TailTop { get => (bool)GetValue(TailTopProperty); set => SetValue(TailTopProperty, value); }
    public bool IsUser { get => (bool)GetValue(IsUserProperty); set => SetValue(IsUserProperty, value); }
    public bool TailRight { get => (bool)GetValue(TailRightProperty); set => SetValue(TailRightProperty, value); }
    public PixelBubble() { SnapsToDevicePixels = true; UseLayoutRounding = true; RenderOptions.SetEdgeMode(this, EdgeMode.Aliased); }
    protected override Size MeasureOverride(Size constraint)
    {
        if (Child is null) return new Size(64, 48);
        Child.Measure(new Size(Math.Max(0, constraint.Width - 24), Math.Max(0, constraint.Height - 32)));
        return new Size(Child.DesiredSize.Width + 24, Child.DesiredSize.Height + 32);
    }
    protected override Size ArrangeOverride(Size size)
    {
        Child?.Arrange(new Rect(12, TailTop ? 24 : 8, Math.Max(0, size.Width - 24), Math.Max(0, size.Height - 32)));
        return size;
    }
    protected override void OnRender(DrawingContext dc)
    {
        double w = Math.Floor(ActualWidth / 4) * 4, h = Math.Floor((ActualHeight - 16) / 4) * 4;
        if (w < 48 || h < 24) return;
        // Flip the frame only; text and input retain their normal orientation.
        if (TailTop) dc.PushTransform(new ScaleTransform(1, -1, 0, (h + 16) / 2));
        var edge = Brush(IsUser ? "#625972" : "#60538F");
        var fill = Brush(IsUser ? "#FFFDF4" : "#F0EBFF");
        double x = TailRight ? w - 52 : 28;
        if (TailRight) dc.PushTransform(new ScaleTransform(-1, 1, x + 12, 0));
        dc.DrawGeometry(edge, null, Polygon(new Point(x, h - 4), new Point(x + 24, h - 4), new Point(x + 24, h + 4), new Point(x + 16, h + 4), new Point(x + 16, h + 8), new Point(x + 8, h + 8), new Point(x + 8, h + 12), new Point(x, h + 12)));
        dc.DrawRectangle(fill, null, new Rect(x + 4, h - 4, 12, 8));
        dc.DrawRectangle(fill, null, new Rect(x + 4, h + 4, 4, 4));
        if (TailRight) dc.Pop();
        dc.DrawGeometry(edge, null, Frame(0, 0, w, h));
        dc.DrawGeometry(fill, null, Frame(4, 4, w - 8, h - 8));
        dc.DrawRectangle(Brush("#FFFFFF"), null, new Rect(12, 4, Math.Max(0, w - 24), 4));
        dc.DrawRectangle(Brush(IsUser ? "#E4DDCF" : "#DCD2F0"), null, new Rect(12, h - 8, Math.Max(0, w - 24), 4));
        if (TailTop) dc.Pop();
    }
    private static SolidColorBrush Brush(string color) { var brush = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(color)); brush.Freeze(); return brush; }
    private static Geometry Frame(double x, double y, double w, double h) => Polygon(new Point(x + 12,y), new Point(x + w - 12,y), new Point(x + w - 12,y + 4), new Point(x + w - 4,y + 4), new Point(x + w - 4,y + 12), new Point(x + w,y + 12), new Point(x + w,y + h - 12), new Point(x + w - 4,y + h - 12), new Point(x + w - 4,y + h - 4), new Point(x + w - 12,y + h - 4), new Point(x + w - 12,y + h), new Point(x + 12,y + h), new Point(x + 12,y + h - 4), new Point(x + 4,y + h - 4), new Point(x + 4,y + h - 12), new Point(x,y + h - 12), new Point(x,y + 12), new Point(x + 4,y + 12), new Point(x + 4,y + 4), new Point(x + 12,y + 4));
    private static Geometry Polygon(params Point[] points)
    {
        var shape = new StreamGeometry(); using (var ctx = shape.Open()) { ctx.BeginFigure(points[0], true, true); ctx.PolyLineTo(points.Skip(1).ToArray(), true, false); }
        shape.Freeze(); return shape;
    }
}

public sealed class SizeGlyph : FrameworkElement
{
    public static readonly DependencyProperty ExpandedProperty = DependencyProperty.Register(nameof(Expanded), typeof(bool), typeof(SizeGlyph), new FrameworkPropertyMetadata(false, FrameworkPropertyMetadataOptions.AffectsRender));
    public bool Expanded { get => (bool)GetValue(ExpandedProperty); set => SetValue(ExpandedProperty, value); }
    protected override void OnRender(DrawingContext dc)
    {
        var pen = new System.Windows.Media.Pen(new SolidColorBrush(System.Windows.Media.Color.FromRgb(77, 65, 112)), 1.5);
        if (Expanded)
        {
            dc.DrawLine(pen, new Point(0, 4), new Point(4, 4)); dc.DrawLine(pen, new Point(4, 0), new Point(4, 4));
            dc.DrawLine(pen, new Point(8, 8), new Point(12, 8)); dc.DrawLine(pen, new Point(8, 8), new Point(8, 12));
        }
        else
        {
            dc.DrawLine(pen, new Point(0, 0), new Point(4, 0)); dc.DrawLine(pen, new Point(0, 0), new Point(0, 4));
            dc.DrawLine(pen, new Point(8, 12), new Point(12, 12)); dc.DrawLine(pen, new Point(12, 8), new Point(12, 12));
        }
    }
}

public sealed class PixelGlyph : FrameworkElement
{
    public bool Stop { get; set; }
    protected override void OnRender(DrawingContext dc)
    {
        RenderOptions.SetEdgeMode(this, EdgeMode.Aliased);
        var ink = new SolidColorBrush(System.Windows.Media.Color.FromRgb(77, 65, 112));
        if (Stop) { dc.DrawRectangle(ink, null, new Rect(2, 2, 12, 12)); return; }
        dc.DrawRectangle(ink, null, new Rect(7, 0, 2, 2));
        dc.DrawRectangle(ink, null, new Rect(5, 2, 6, 2));
        dc.DrawRectangle(ink, null, new Rect(3, 4, 10, 2));
        dc.DrawRectangle(ink, null, new Rect(1, 6, 14, 2));
        dc.DrawRectangle(ink, null, new Rect(6, 8, 4, 8));
    }
}
