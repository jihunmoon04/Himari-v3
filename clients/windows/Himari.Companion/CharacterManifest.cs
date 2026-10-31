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
        if (string.IsNullOrWhiteSpace(manifest.Sheet)) throw new InvalidDataException("character sheet");
        if (manifest.Face?.Origin is not [>= 0, >= 0] || manifest.Face.Size is not [> 0, > 0]) throw new InvalidDataException("character face area");
        if (manifest.Clips is null || manifest.Faces is null) throw new InvalidDataException("character manifest needs clips and faces");
        if (!manifest.Clips.TryGetValue("idle", out var idle) || idle is null || !idle.Loop) throw new InvalidDataException("character manifest needs a looping idle clip");
        foreach (var (name, clip) in manifest.Clips)
            if (clip?.Frames is not { Length: > 0 } || clip.Frames.Any(f => f is null || f.Ms <= 0 || !IsRect(f.Src) || f.Face is not [_, _])
                || clip.LoopFrom < 0 || clip.LoopFrom >= clip.Frames.Length || clip.LoopFrom > 0 && !clip.Loop)
                throw new InvalidDataException($"clip {name}");
        if (!manifest.Faces.ContainsKey("neutral")) throw new InvalidDataException("character manifest needs a neutral face");
        foreach (var (name, face) in manifest.Faces)
        {
            if (face is null || !IsRect(face.Src)) throw new InvalidDataException($"face {name}");
            foreach (var variant in (face.Eyes ?? []).Concat(face.Mouth ?? []))
                if (variant.Value is null || !IsRect(variant.Value.Src) || variant.Value.At is not [_, _]) throw new InvalidDataException($"face {name} variant {variant.Key}");
        }
        return manifest;
    }

    // Every src must lie inside the loaded sheet; checked once at startup so a bad export
    // fails there instead of while drawing.
    internal void CheckSheet(int width, int height)
    {
        var rects = Clips.SelectMany(c => c.Value.Frames.Select(f => (c.Key, f.Src)))
            .Concat(Faces.SelectMany(f => new[] { (f.Key, f.Value.Src) }
                .Concat((f.Value.Eyes ?? []).Concat(f.Value.Mouth ?? []).Select(v => ($"{f.Key}/{v.Key}", v.Value.Src)))));
        foreach (var (name, src) in rects)
            if (src[0] + src[2] > width || src[1] + src[3] > height) throw new InvalidDataException($"{name} lies outside the {width}x{height} sheet");
    }

    private static bool IsRect(int[]? src) => src is [>= 0, >= 0, > 0, > 0];

    internal int Width => Canvas[0];
    internal int Height => Canvas[1];
}

internal sealed record FaceArea(int[] Origin, int[] Size);
// A loop with LoopFrom > 0 plays its first LoopFrom frames once (falling asleep), then
// repeats the rest.
internal sealed record CharacterClip(bool Loop, ClipFrame[] Frames, int LoopFrom = 0)
{
    internal TimeSpan Length => TimeSpan.FromMilliseconds(Frames.Sum(f => f.Ms));
    internal double IntroMs => Frames.Take(LoopFrom).Sum(f => f.Ms);

    // Time into the clip, in ms, for the frame shown after elapsed ms of play.
    internal double Position(double elapsed)
    {
        double total = Length.TotalMilliseconds, intro = IntroMs;
        if (!Loop) return Math.Min(elapsed, total - 1);
        return elapsed < intro ? elapsed : intro + (elapsed - intro) % (total - intro);
    }
}
// Eyes and Mouth, when present, force that variant for the frame (for example closed eyes while dozing).
// Forced eyes open while the character talks. OwnFace: the frame is a drawing with its own
// face (a hand over the yawning mouth), so no mood face, eyes or mouth go over it.
internal sealed record ClipFrame(int[] Src, int Ms, int[] Face, string? Eyes = null, string? Mouth = null, bool OwnFace = false);
internal sealed record CharacterFace(int[] Src, Dictionary<string, FaceVariant>? Eyes = null, Dictionary<string, FaceVariant>? Mouth = null);
internal sealed record FaceVariant(int[] Src, int[] At);
