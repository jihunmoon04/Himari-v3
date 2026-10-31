using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using Point = System.Windows.Point;
using Size = System.Windows.Size;

namespace Himari.Companion;

// Two-DIP step frame with a shared 10-DIP inset and an eight-DIP tail.
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
        Child.Measure(new Size(Math.Max(0, constraint.Width - 20), Math.Max(0, constraint.Height - 28)));
        return new Size(Math.Ceiling((Child.DesiredSize.Width + 20) / 2) * 2, Math.Ceiling((Child.DesiredSize.Height + 28) / 2) * 2);
    }
    protected override Size ArrangeOverride(Size size)
    {
        Child?.Arrange(new Rect(10, TailTop ? 18 : 10, Math.Max(0, size.Width - 20), Math.Max(0, size.Height - 28)));
        return size;
    }
    protected override void OnRender(DrawingContext dc)
    {
        double w = Math.Floor(ActualWidth / 2) * 2, h = Math.Floor((ActualHeight - 8) / 2) * 2;
        if (w < 48 || h < 24) return;
        // Flip the frame only; text and input retain their normal orientation.
        if (TailTop) dc.PushTransform(new ScaleTransform(1, -1, 0, (h + 8) / 2));
        var edge = Brush("#766385");
        var fill = Brush(IsUser ? "#FFFDF3" : "#F2EDFF");
        double x = TailRight ? w - 48 : 24;
        if (TailRight) dc.PushTransform(new ScaleTransform(-1, 1, x + 8, 0));
        dc.DrawGeometry(edge, null, Polygon(new Point(x, h - 2), new Point(x + 16, h - 2), new Point(x + 16, h + 2), new Point(x + 12, h + 2), new Point(x + 12, h + 4), new Point(x + 8, h + 4), new Point(x + 8, h + 8), new Point(x, h + 8)));
        dc.DrawGeometry(fill, null, Polygon(new Point(x + 2, h - 2), new Point(x + 14, h - 2), new Point(x + 14, h), new Point(x + 10, h), new Point(x + 10, h + 2), new Point(x + 6, h + 2), new Point(x + 6, h + 6), new Point(x + 2, h + 6)));
        if (TailRight) dc.Pop();
        dc.DrawGeometry(edge, null, Frame(0, 0, w, h, 8));
        dc.DrawGeometry(fill, null, Frame(2, 2, w - 4, h - 4, 6));
        if (TailTop) dc.Pop();
    }
    private static SolidColorBrush Brush(string color) { var brush = new SolidColorBrush((System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(color)); brush.Freeze(); return brush; }
    private static Geometry Frame(double x, double y, double w, double h, double corner) => Polygon(new Point(x + corner,y), new Point(x + w - corner,y), new Point(x + w - corner,y + 2), new Point(x + w - 2,y + 2), new Point(x + w - 2,y + corner), new Point(x + w,y + corner), new Point(x + w,y + h - corner), new Point(x + w - 2,y + h - corner), new Point(x + w - 2,y + h - 2), new Point(x + w - corner,y + h - 2), new Point(x + w - corner,y + h), new Point(x + corner,y + h), new Point(x + corner,y + h - 2), new Point(x + 2,y + h - 2), new Point(x + 2,y + h - corner), new Point(x,y + h - corner), new Point(x,y + corner), new Point(x + 2,y + corner), new Point(x + 2,y + 2), new Point(x + corner,y + 2));
    private static Geometry Polygon(params Point[] points)
    {
        var shape = new StreamGeometry(); using (var ctx = shape.Open()) { ctx.BeginFigure(points[0], true, true); ctx.PolyLineTo(points.Skip(1).ToArray(), true, false); }
        shape.Freeze(); return shape;
    }
}
