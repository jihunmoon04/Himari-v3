using System.Net;
using System.Text.Json;
using System.Windows;

namespace Himari.Companion;

// V3-P91 §7: HIMARI asks for the screen (its screen tool). The app captures by the same rules as a message's
// screen, shows that it looked, and posts the image to the request — or says why not.
public partial class MainWindow
{
    private int screenLookNoticeVersion;

    private async Task AnswerScreenRequest(ServerClient local, JsonElement data, CancellationToken ct)
    {
        string? requestId = Str(data, "request_id");
        if (string.IsNullOrEmpty(requestId)) return;
        try
        {
            // §7.2: what is in front decides; HIMARI's own window keeps what was there before it (§14 Q10).
            string? program = ReadForeground() is { } seen ? seen.Program : activityTracker.Current(DateTime.UtcNow)?.Program;
            if (ScreenRequestPolicy.Refusal(signalSettings.ScreenLook, sleepPolicy.Asleep || sessionLocked, program, titleExclusions) is { } reason)
            {
                await local.RefuseScreenAsync(requestId, reason, ct);
                return;
            }
            ShowScreenLookNotice();
            CapturedScreen captured;
            try { captured = await CaptureScreenAsync(ct); }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                await local.RefuseScreenAsync(requestId, "capture_failed", ct);
                return;
            }
            await local.AnswerScreenAsync(requestId, captured.Bytes, captured.ContentType, ct);
            // §6: the job's moves are on the screen it last saw.
            if (Str(data, "job_id") is { Length: > 0 } jobId && Str(data, "conversation") is { } conversation && moveLifetime.CanBind(jobId))
                moveJobs[jobId] = new(conversation, new(jobId, captured.Geometry, captured.CapturedAt));
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { }
        catch (ApiException ex) when (ex.Status is HttpStatusCode.Gone or HttpStatusCode.NotFound) { /* §4.3: it ended; nothing to show */ }
        catch (Exception ex) when (ex is ApiException or System.Net.Http.HttpRequestException or OperationCanceledException)
        {
            System.Diagnostics.Trace.TraceWarning("HIMARI: could not answer a screen request: {0}", ex.Message);
        }
    }

    /// <summary>§7.2: a small "looking at the screen" for two seconds, shown while it captures (the window is left out of it).</summary>
    private async void ShowScreenLookNotice()
    {
        int version = ++screenLookNoticeVersion;
        ScreenLookNotice.Visibility = Visibility.Visible;
        await Task.Delay(2000);
        if (version == screenLookNoticeVersion) ScreenLookNotice.Visibility = Visibility.Collapsed;
    }

    /// <summary>§7.3, §14 Q9: the first time the server says it can ask for the screen, the saved "attach to every
    /// message" goes off once, with a note under it; it is not changed again.</summary>
    private void ScreenSharingOffOnce()
    {
        if (screenSharingReset) return;
        screenSharingReset = true;
        if (settings.ScreenSharing.IsChecked == true)
        {
            settings.ScreenSharing.IsChecked = false;
            settings.ScreenSharingNotice.Visibility = Visibility.Visible;
        }
        SchedulePreferencesSave();
    }
}
