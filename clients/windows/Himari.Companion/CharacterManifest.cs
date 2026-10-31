using System.IO;
using System.Text.Json;

namespace Himari.Companion;

// Assets/character.json, written by animation-preview/pixel/tools/export.lua
// (docs/companion-animation-v0.1.md §3.4). Coordinates are sprite pixels.
internal sealed record CharacterManifest(int Version, int[] Canvas, string Sheet, FaceArea Face,
    Dictionary<string, CharacterClip> Clips, Dictionary<string, CharacterFace> Faces)
{
    internal const int SupportedVersion = 1;
    private static readonly JsonSerializerOptions Options = new() { PropertyNameCaseInsensitive = true };

    // Assets ship with the code, so anything unexpected is a build mistake: fail at startup.
    internal static CharacterManifest Parse(string json)
    {
        var manifest = JsonSerializer.Deserialize<CharacterManifest>(json, Options) ?? throw new InvalidDataException("empty character manifest");
        if (manifest.Version != SupportedVersion) throw new InvalidDataException($"character manifest version {manifest.Version}, expected {SupportedVersion}");
        if (manifest.Canvas is not [> 0, > 0]) throw new InvalidDataException("character canvas");
        if (!manifest.Clips.TryGetValue("idle", out var idle) || !idle.Loop) throw new InvalidDataException("character manifest needs a looping idle clip");
        foreach (var (name, clip) in manifest.Clips)
            if (clip.Frames.Length == 0 || clip.Frames.Any(f => f.Ms <= 0 || f.Src.Length != 4 || f.Face.Length != 2)) throw new InvalidDataException($"clip {name}");
        if (!manifest.Faces.ContainsKey("neutral")) throw new InvalidDataException("character manifest needs a neutral face");
        return manifest;
    }

    internal int Width => Canvas[0];
    internal int Height => Canvas[1];
}

internal sealed record FaceArea(int[] Origin, int[] Size);
internal sealed record CharacterClip(bool Loop, ClipFrame[] Frames)
{
    internal TimeSpan Length => TimeSpan.FromMilliseconds(Frames.Sum(f => f.Ms));
}
// Eyes and Mouth, when present, force that variant for the frame (for example closed eyes while dozing).
internal sealed record ClipFrame(int[] Src, int Ms, int[] Face, string? Eyes = null, string? Mouth = null);
internal sealed record CharacterFace(int[] Src, Dictionary<string, FaceVariant>? Eyes = null, Dictionary<string, FaceVariant>? Mouth = null);
internal sealed record FaceVariant(int[] Src, int[] At);
