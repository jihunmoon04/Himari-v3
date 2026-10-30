using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using Forms = System.Windows.Forms;

namespace Himari.Companion;

internal sealed record CapturedScreen(byte[] Bytes, string ContentType, ScreenGeometry Geometry, DateTimeOffset CapturedAt);

internal static class ScreenCapture
{
    private const uint ExcludeFromCapture = 0x11;
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool SetWindowDisplayAffinity(IntPtr window, uint affinity);

    // The caller hides every companion-owned window when exclusion is unavailable,
    // waits until the compositor updates, and restores them in finally.
    internal static bool TryExcludeWindow(IntPtr window) => SetWindowDisplayAffinity(window, ExcludeFromCapture);

    internal static Rectangle SelectMonitor()
    {
        IntPtr foreground = GetForegroundWindow();
        GetWindowThreadProcessId(foreground, out uint processId);
        var screen = foreground == IntPtr.Zero || processId == (uint)Environment.ProcessId
            ? Forms.Screen.FromPoint(Forms.Cursor.Position) : Forms.Screen.FromHandle(foreground);
        return screen.Bounds;
    }

    internal static CapturedScreen Capture(Rectangle bounds)
    {
        var (width, height) = ScreenGeometry.ImageSize(bounds.Width, bounds.Height);
        using var original = new Bitmap(bounds.Width, bounds.Height, PixelFormat.Format32bppArgb);
        using (var graphics = Graphics.FromImage(original)) graphics.CopyFromScreen(bounds.Location, Point.Empty, bounds.Size, CopyPixelOperation.SourceCopy);
        using var resized = new Bitmap(width, height, PixelFormat.Format24bppRgb);
        using (var graphics = Graphics.FromImage(resized))
        {
            graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
            graphics.DrawImage(original, new Rectangle(0, 0, width, height));
        }
        using var buffer = new MemoryStream();
        resized.Save(buffer, ImageFormat.Png);
        string contentType = "image/png";
        if (buffer.Length > 1_572_864)
        {
            buffer.SetLength(0); buffer.Position = 0;
            var codec = ImageCodecInfo.GetImageEncoders().First(c => c.FormatID == ImageFormat.Jpeg.Guid);
            using var parameters = new EncoderParameters(1);
            parameters.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 85L);
            resized.Save(buffer, codec, parameters); contentType = "image/jpeg";
        }
        return new(buffer.ToArray(), contentType, new(new(bounds.Left, bounds.Top, bounds.Width, bounds.Height), width, height), DateTimeOffset.UtcNow);
    }
}
