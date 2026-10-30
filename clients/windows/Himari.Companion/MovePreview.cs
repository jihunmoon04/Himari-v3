using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

public partial class MainWindow
{
    private int movePreviewVersion;
    private bool movePreviewActive;

    private void CancelMovePreview()
    {
        movePreviewVersion++;
        movePreviewActive = false;
        CancelAutomaticMove();
    }

    private async void MoveDemoClick(object sender, RoutedEventArgs e)
    {
        if (movePreviewActive || !IsVisible) return;
        var source = PresentationSource.FromVisual(this);
        if (source?.CompositionTarget is null) return;
        var screen = Forms.Screen.FromHandle(new WindowInteropHelper(this).Handle).WorkingArea;
        Matrix fromDevice = source.CompositionTarget.TransformFromDevice;
        System.Windows.Point areaStart = fromDevice.Transform(new System.Windows.Point(screen.Left, screen.Top));
        System.Windows.Point areaEnd = fromDevice.Transform(new System.Windows.Point(screen.Right, screen.Bottom));
        double originalLeft = Left, originalTop = Top;
        double leftLimit = areaStart.X, rightLimit = Math.Max(leftLimit, areaEnd.X - ActualWidth);
        double target = Math.Clamp(originalLeft + (originalLeft + 100 <= rightLimit ? 100 : -100), leftLimit, rightLimit);
        int version = ++movePreviewVersion;
        movePreviewActive = true;
        saveTimer.Stop();
        settings.MoveDemoButton.IsEnabled = false;
        try
        {
            if (!await AnimatePreview(originalLeft, target, version)) return;
            await Task.Delay(500);
            if (version != movePreviewVersion) return;
            await AnimatePreview(target, originalLeft, version);
        }
        finally
        {
            if (version == movePreviewVersion) { movePreviewActive = false; Left = originalLeft; Top = originalTop; }
            settings.MoveDemoButton.IsEnabled = true;
        }
    }

    private async Task<bool> AnimatePreview(double from, double to, int version)
    {
        var watch = System.Diagnostics.Stopwatch.StartNew();
        while (version == movePreviewVersion && watch.ElapsedMilliseconds < 600)
        {
            double t = Math.Clamp(watch.Elapsed.TotalMilliseconds / 600, 0, 1);
            double eased = t * t * (3 - 2 * t);
            Left = from + (to - from) * eased;
            await Task.Delay(16);
        }
        if (version != movePreviewVersion) return false;
        Left = to; return true;
    }
}
