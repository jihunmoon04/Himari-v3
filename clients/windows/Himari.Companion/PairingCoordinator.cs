using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Himari.Companion;

internal sealed record DeviceCredential(string Server, string Token, string? CodeFingerprint);
internal interface ICredentialVault
{
    DeviceCredential? Active();
    DeviceCredential? Pending();
    void SaveActive(DeviceCredential value);
    void SavePending(DeviceCredential value);
    void DeletePending();
}
internal sealed class WindowsCredentialVault : ICredentialVault
{
    public DeviceCredential? Active() => CredentialStore.ReadActive();
    public DeviceCredential? Pending() => CredentialStore.ReadPending();
    public void SaveActive(DeviceCredential value) => CredentialStore.SaveActive(value);
    public void SavePending(DeviceCredential value) => CredentialStore.SavePending(value);
    public void DeletePending() => CredentialStore.DeletePending();
}
internal sealed class PairingCoordinator(ICredentialVault vault, Func<DeviceCredential, ServerClient> createClient, Func<TimeSpan, CancellationToken, Task>? delay = null)
{
    private async Task<bool> Verify(DeviceCredential credential, CancellationToken ct)
    {
        using var client = createClient(credential);
        try
        {
            var device = await client.SendAsync(HttpMethod.Get, "/api/device", null, ct);
            return device.ValueKind == JsonValueKind.Object && device.TryGetProperty("device", out var obj) && obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty("device_id", out var id) && id.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(id.GetString());
        }
        catch (ApiException ex) when (ex.Status == HttpStatusCode.Unauthorized) { return false; }
    }
    private void Promote(DeviceCredential credential) { vault.SaveActive(credential); vault.DeletePending(); }
    internal async Task<DeviceCredential?> RecoverPendingAsync(CancellationToken ct)
    {
        // ReadActive first to migrate the prototype's legacy pending record.
        _ = vault.Active();
        var pending = vault.Pending();
        if (pending is null || !await Verify(pending, ct)) return null;
        Promote(pending); return pending;
    }
    internal async Task<DeviceCredential> PairAsync(string server, string code, CancellationToken ct)
    {
        var pending = vault.Pending();
        if (pending is not null && pending.Server == server && await Verify(pending, ct)) { Promote(pending); return pending; }
        var normalized = new string(code.Where(c => c != '-' && !char.IsWhiteSpace(c)).ToArray()).ToUpperInvariant();
        var fingerprint = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(normalized)));
        // A new code must not reuse a token already associated with an earlier attempt.
        if (pending is null || pending.Server != server || (pending.CodeFingerprint is { Length: > 0 } && pending.CodeFingerprint != fingerprint))
            pending = new(server, CredentialStore.NewToken(), fingerprint);
        else pending = pending with { CodeFingerprint = fingerprint };
        int conflicts = 0, busyRetries = 0;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            vault.SavePending(pending);
            using var client = createClient(pending);
            try { await client.PairAsync(normalized, ct); break; }
            catch (ApiException ex) when (ex.Status == HttpStatusCode.Conflict && ex.Code == "token_in_use" && conflicts++ == 0)
            {
                pending = pending with { Token = CredentialStore.NewToken() };
            }
            catch (ApiException ex) when (ex.Status == HttpStatusCode.TooManyRequests && ex.Code == "pairing_busy" && busyRetries++ < 2)
            {
                await (delay ?? Task.Delay)(TimeSpan.FromSeconds(2), ct);
            }
        }
        if (!await Verify(pending, ct)) throw new InvalidOperationException("짝짓기 후 기기 인증을 확인하지 못했습니다.");
        Promote(pending); return pending;
    }
}
