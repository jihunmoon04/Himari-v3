using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace Himari.Companion;

// Composes a pose at sprite size (body, mood face, eyes, mouth) into one bitmap. The
// Character image then scales that single bitmap with nearest neighbour, so the parts
// can never drift apart by a screen pixel at non-integer sizes.
internal sealed class CharacterRenderer
{
    private readonly int[] sheet;
    private readonly int sheetWidth;
    private readonly int[] buffer;
    private CharacterPose? shown;

    private CharacterRenderer(CharacterManifest manifest, BitmapSource sheetImage)
    {
        Manifest = manifest;
        var bgra = new FormatConvertedBitmap(sheetImage, PixelFormats.Bgra32, null, 0);
        sheetWidth = bgra.PixelWidth;
        sheet = new int[sheetWidth * bgra.PixelHeight];
        bgra.CopyPixels(sheet, sheetWidth * 4, 0);
        buffer = new int[manifest.Width * manifest.Height];
        Bitmap = new WriteableBitmap(manifest.Width, manifest.Height, 96, 96, PixelFormats.Bgra32, null);
    }

    internal CharacterManifest Manifest { get; }
    internal WriteableBitmap Bitmap { get; }
    internal CharacterPose? Shown => shown;

    internal static CharacterRenderer Load()
    {
        static Stream Resource(string name) => System.Windows.Application.GetResourceStream(new Uri($"pack://application:,,,/Himari.Companion;component/Assets/{name}"))?.Stream
            ?? throw new InvalidDataException($"missing Assets/{name}");
        using var json = new StreamReader(Resource("character.json"));
        var manifest = CharacterManifest.Parse(json.ReadToEnd());
        using var png = Resource(manifest.Sheet);
        var image = BitmapFrame.Create(png, BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnLoad);
        manifest.CheckSheet(image.PixelWidth, image.PixelHeight);
        return new CharacterRenderer(manifest, image);
    }

    // Returns false when the pose is already on screen.
    internal bool Render(CharacterPose pose)
    {
        if (pose == shown) return false;
        var frame = Manifest.Clips[pose.Clip].Frames[pose.Frame];
        var face = Manifest.Faces[pose.Mood];
        Array.Clear(buffer);
        Draw(frame.Src, 0, 0, replace: true);
        // A frame with its own drawn face takes no mood face.
        if (!frame.OwnFace)
        {
            int fx = Manifest.Face.Origin[0] + frame.Face[0], fy = Manifest.Face.Origin[1] + frame.Face[1];
            Draw(face.Src, fx, fy);
            if (pose.Eyes is { } eyes && face.Eyes?.GetValueOrDefault(eyes) is { } e) Draw(e.Src, fx + e.At[0], fy + e.At[1]);
            if (pose.Mouth is { } mouth && face.Mouth?.GetValueOrDefault(mouth) is { } m) Draw(m.Src, fx + m.At[0], fy + m.At[1]);
        }
        Bitmap.WritePixels(new Int32Rect(0, 0, Manifest.Width, Manifest.Height), buffer, Manifest.Width * 4, 0);
        shown = pose;
        return true;
    }

    // Overlays keep what is below their transparent pixels.
    private void Draw(int[] src, int x, int y, bool replace = false)
    {
        int width = Manifest.Width, height = Manifest.Height;
        for (int row = 0; row < src[3]; row++)
        {
            int ty = y + row;
            if (ty < 0 || ty >= height) continue;
            for (int col = 0; col < src[2]; col++)
            {
                int tx = x + col;
                if (tx < 0 || tx >= width) continue;
                int pixel = sheet[(src[1] + row) * sheetWidth + src[0] + col];
                if (replace || (pixel >>> 24) != 0) buffer[ty * width + tx] = pixel;
            }
        }
    }
}
