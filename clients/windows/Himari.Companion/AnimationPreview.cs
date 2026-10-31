namespace Himari.Companion;

// Settings → 미리보기 → 애니메이션 점검: play any clip, hold eyes or mouth on one variant,
// slow down or pause the animation clock and step one pose at a time.
public partial class MainWindow
{
    private static readonly (string Label, string? Variant)[] EyesChoices = [("자동", null), ("뜬 눈", ""), ("반쯤 감음", "half"), ("감음", "closed")];
    private static readonly (string Label, string? Variant)[] MouthChoices = [("자동", null), ("닫힘", ""), ("작게", "small"), ("크게", "wide")];
    private static readonly (string Label, double Speed)[] SpeedChoices = [("1배", 1), ("0.5배", .5), ("0.25배", .25), ("0.1배", .1)];
    // The animator runs on this clock, which the preview can slow down or stop.
    private TimeSpan animationTime;
    private TimeSpan animationClockRead;
    private double animationSpeed = 1;
    private AnimationRequest? previewRequest;

    private void SetupAnimationPreview()
    {
        settings.ClipSelector.ItemsSource = character.Manifest.Clips.Keys.ToList();
        settings.ClipSelector.SelectedItem = "idle";
        settings.EyesSelector.ItemsSource = EyesChoices.Select(c => c.Label).ToList();
        settings.MouthSelector.ItemsSource = MouthChoices.Select(c => c.Label).ToList();
        settings.SpeedSelector.ItemsSource = SpeedChoices.Select(c => c.Label).ToList();
        settings.EyesSelector.SelectedIndex = settings.MouthSelector.SelectedIndex = settings.SpeedSelector.SelectedIndex = 0;
        settings.ClipSelector.SelectionChanged += (_, _) => PlayPreviewClip(restart: false);
        settings.RestartButton.Click += (_, _) => PlayPreviewClip(restart: true);
        settings.SpeedSelector.SelectionChanged += (_, _) => { AnimationNow(); animationSpeed = SpeedChoices[Math.Max(0, settings.SpeedSelector.SelectedIndex)].Speed; UpdateFrame(); };
        settings.StepButton.Click += (_, _) =>
        {
            settings.PausePreview.IsChecked = true;
            // Skip changes the held eyes or mouth hide, so every click shows a new picture.
            var shown = character.Shown;
            for (int i = 0; i < 100 && PreviewPose(animator.Pose(animationTime)) == shown; i++) animationTime = animator.NextChange(AnimationNow());
            UpdateFrame();
        };
        foreach (var box in new[] { settings.EyesSelector, settings.MouthSelector }) box.SelectionChanged += (_, _) => UpdateFrame();
        foreach (var check in new[] { settings.TalkPreview, settings.SleepPreview, settings.PausePreview })
        {
            check.Checked += (_, _) => UpdateFrame();
            check.Unchecked += (_, _) => UpdateFrame();
        }
    }

    private TimeSpan AnimationNow()
    {
        var real = clock.Elapsed;
        if (settings.PausePreview.IsChecked != true) animationTime += (real - animationClockRead) * animationSpeed;
        animationClockRead = real;
        return animationTime;
    }

    // Plays the chosen clip from its first frame above everything else. Choosing idle clears
    // the preview; 처음부터 restarts whichever clip is chosen, idle included.
    private void PlayPreviewClip(bool restart)
    {
        var now = AnimationNow();
        if (previewRequest is not null) animator.Release(previewRequest, now);
        previewRequest = settings.ClipSelector.SelectedItem is string clip && (clip != "idle" || restart)
            ? animator.Request(clip, AnimationLayer.Directed, now) : null;
        UpdateFrame();
    }

    private CharacterPose PreviewPose(CharacterPose pose)
    {
        var face = character.Manifest.Faces[pose.Mood];
        var eyes = EyesChoices[Math.Max(0, settings.EyesSelector.SelectedIndex)].Variant;
        var mouth = MouthChoices[Math.Max(0, settings.MouthSelector.SelectedIndex)].Variant;
        if (eyes is not null) pose = pose with { Eyes = eyes != "" && face.Eyes?.ContainsKey(eyes) == true ? eyes : null };
        if (mouth is not null) pose = pose with { Mouth = mouth != "" && face.Mouth?.ContainsKey(mouth) == true ? mouth : null };
        var frames = character.Manifest.Clips[pose.Clip].Frames;
        string missing = string.Join(", ", new[] { (eyes, face.Eyes), (mouth, face.Mouth) }
            .Where(c => c.Item1 is { Length: > 0 } v && c.Item2?.ContainsKey(v) != true).Select(c => c.Item1));
        settings.PoseText.Text = $"지금: {pose.Clip} {pose.Frame + 1}/{frames.Length} ({frames[pose.Frame].Ms}ms) · {pose.Mood} · 눈 {pose.Eyes ?? "뜬 눈"} · 입 {pose.Mouth ?? "닫힘"}"
            + (missing.Length > 0 ? $" · {pose.Mood}에는 {missing} 그림이 없어 기본 얼굴을 보여 줍니다" : "");
        return pose;
    }
}
