using System.Net;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;

namespace Himari.Companion;

internal sealed record PresentedFileItem(
    string FileId,
    int Version,
    string Name,
    string MediaType,
    long Size,
    string Sha256,
    string? CreatedAt,
    bool Expired);

internal sealed record PresentedFileCardKey(
    string Server,
    string Token,
    string ConversationId,
    string FileId,
    int Version);

internal static class PresentedFileNames
{
    private static readonly HashSet<string> ReservedDeviceNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "COM\u00B9", "COM\u00B2", "COM\u00B3",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9", "LPT\u00B9", "LPT\u00B2", "LPT\u00B3",
    };

    private static readonly HashSet<char> InvalidChars =
        Path.GetInvalidFileNameChars().Concat(['<', '>', ':', '"', '/', '\\', '|', '?', '*']).ToHashSet();

    /// <summary>V3-P93 §6.3: sanitize a server-supplied filename for Windows local save dialogs.</summary>
    internal static string SanitizeSaveFileName(string? rawName)
    {
        if (string.IsNullOrWhiteSpace(rawName)) return "download";
        string segment = rawName.Split(['/', '\\'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .LastOrDefault(part => part is not ("." or "..")) ?? "";
        var builder = new StringBuilder(segment.Length);
        foreach (char ch in segment)
        {
            if (char.IsControl(ch)) continue;
            builder.Append(InvalidChars.Contains(ch) ? '_' : ch);
        }
        string cleaned = builder.ToString().Trim().TrimEnd('.', ' ');
        if (cleaned.Length == 0 || cleaned is "." or "..") return "download";
        int dot = cleaned.IndexOf('.');
        string stem = (dot >= 0 ? cleaned[..dot] : cleaned).TrimEnd(' ');
        if (ReservedDeviceNames.Contains(stem)) cleaned = "_" + cleaned;
        if (cleaned.Length > 180)
        {
            string ext = Path.GetExtension(cleaned);
            if (ext.Length > 0 && ext.Length <= 32)
                cleaned = cleaned[..(180 - ext.Length)].TrimEnd('.', ' ') + ext;
            else
                cleaned = cleaned[..180].TrimEnd('.', ' ');
        }
        return cleaned.Length == 0 ? "download" : cleaned;
    }

    internal static string FormatSize(long bytes)
    {
        if (bytes < 1024) return $"{Math.Max(0, bytes)} B";
        if (bytes < 1024 * 1024) return $"{bytes / 1024.0:0.#} KB";
        return $"{bytes / (1024.0 * 1024.0):0.#} MB";
    }

    internal static string VersionLabel(int version) => $"v{Math.Max(1, version)}";
}

internal sealed class ApiException(HttpStatusCode status, string code) : Exception(code)
{
    internal HttpStatusCode Status { get; } = status;
    internal string Code { get; } = code;
}

internal sealed class SaveTargetChangedException : IOException
{
    internal SaveTargetChangedException(string message) : base(message) { }
    internal SaveTargetChangedException(string message, Exception innerException) : base(message, innerException) { }
}

internal readonly record struct FileHandleIdentity(
    uint VolumeSerialNumber,
    uint FileIndexHigh,
    uint FileIndexLow,
    ulong CreationTimeTicks,
    ulong LastWriteTimeTicks,
    ulong FileSize);

/// <summary>V3-P93 §6.3 (PR #181 review P1): state of the user-selected save path at dialog confirmation time.
/// New paths commit with overwrite:false; approved existing files verify handle identity/timestamps/content on a
/// FileShare.None handle, stage the original via handle rename, and atomically move the verified .part into place
/// (restoring the original on any failure without ever truncating in-place or mutating other hard links).</summary>
internal sealed record ApprovedSaveTarget(
    string FullPath,
    bool ExistedAtSelection,
    FileHandleIdentity ExistingIdentity,
    string? ExistingSha256)
{
    internal Action? OnAfterVerifyBeforeReplace { get; init; }
    internal Action? OnBeforeCommitTempMove { get; init; }

    private const uint GenericRead = 0x80000000;
    private const uint GenericWrite = 0x40000000;
    private const uint DeleteAccess = 0x00010000;
    private const uint FileShareNone = 0;
    private const uint FileShareDelete = 0x00000004;
    private const uint OpenExisting = 3;
    private const uint FileAttributeNormal = 0x00000080;
    private const int ErrorFileNotFound = 2;
    private const int ErrorPathNotFound = 3;
    private const int ErrorAccessDenied = 5;
    private const int FileRenameInfoEx = 22;
    private const int FileRenameReplaceIfExists = 0x00000001;
    private const int FileRenamePosixSemantics = 0x00000002;

    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME
    {
        public uint DateTimeLow;
        public uint DateTimeHigh;
        public readonly ulong Ticks => ((ulong)DateTimeHigh << 32) | DateTimeLow;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BY_HANDLE_FILE_INFORMATION
    {
        public uint FileAttributes;
        public FILETIME CreationTime;
        public FILETIME LastAccessTime;
        public FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
        public readonly ulong FileSize => ((ulong)FileSizeHigh << 32) | FileSizeLow;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle hFile, out BY_HANDLE_FILE_INFORMATION lpFileInformation);

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern SafeFileHandle CreateFile(
        string lpFileName,
        uint dwDesiredAccess,
        uint dwShareMode,
        IntPtr lpSecurityAttributes,
        uint dwCreationDisposition,
        uint dwFlagsAndAttributes,
        IntPtr hTemplateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetFileInformationByHandle(
        SafeFileHandle hFile,
        int FileInformationClass,
        IntPtr lpFileInformation,
        uint dwBufferSize);

    private static void AtomicReplaceOverOpenTarget(string tempPath, string targetPath)
    {
        using SafeFileHandle tempHandle = CreateFile(
            tempPath,
            GenericRead | DeleteAccess,
            FileShareNone,
            IntPtr.Zero,
            OpenExisting,
            FileAttributeNormal,
            IntPtr.Zero);
        if (tempHandle.IsInvalid)
            throw new IOException("검증된 임시 파일을 열 수 없습니다.", Marshal.GetExceptionForHR(Marshal.GetHRForLastWin32Error()));
        byte[] nameBytes = Encoding.Unicode.GetBytes(targetPath + "\0");
        int headerSize = IntPtr.Size * 2 + 4;
        int totalSize = headerSize + nameBytes.Length;
        IntPtr buffer = Marshal.AllocHGlobal(totalSize);
        try
        {
            for (int i = 0; i < headerSize; i++) Marshal.WriteByte(buffer, i, 0);
            Marshal.WriteInt32(buffer, 0, FileRenameReplaceIfExists | FileRenamePosixSemantics);
            Marshal.WriteIntPtr(buffer, IntPtr.Size, IntPtr.Zero);
            Marshal.WriteInt32(buffer, IntPtr.Size * 2, targetPath.Length * sizeof(char));
            Marshal.Copy(nameBytes, 0, IntPtr.Add(buffer, headerSize), nameBytes.Length);
            if (!SetFileInformationByHandle(tempHandle, FileRenameInfoEx, buffer, (uint)totalSize))
                throw new IOException("검증된 임시 파일로 대상 파일을 원자적으로 교체할 수 없습니다.", Marshal.GetExceptionForHR(Marshal.GetHRForLastWin32Error()));
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static FileHandleIdentity ReadHandleIdentity(SafeFileHandle handle)
    {
        if (!GetFileInformationByHandle(handle, out var info))
            throw new IOException("대상 파일의 핸들 정보를 확인할 수 없습니다.", Marshal.GetExceptionForHR(Marshal.GetHRForLastWin32Error()));
        return new FileHandleIdentity(
            info.VolumeSerialNumber,
            info.FileIndexHigh,
            info.FileIndexLow,
            info.CreationTime.Ticks,
            info.LastWriteTime.Ticks,
            info.FileSize);
    }

    internal static ApprovedSaveTarget Capture(string destinationPath)
    {
        if (string.IsNullOrWhiteSpace(destinationPath))
            throw new ArgumentException("저장 경로가 올바르지 않습니다.", nameof(destinationPath));
        string fullPath = Path.GetFullPath(destinationPath);
        if (Directory.Exists(fullPath))
            throw new IOException("선택한 저장 경로가 폴더입니다.");
        if (!File.Exists(fullPath))
            return new ApprovedSaveTarget(fullPath, false, default, null);
        try
        {
            using var stream = new FileStream(fullPath, FileMode.Open, FileAccess.Read, FileShare.Read);
            var identityBefore = ReadHandleIdentity(stream.SafeFileHandle);
            string sha256 = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
            var identityAfter = ReadHandleIdentity(stream.SafeFileHandle);
            if (identityBefore != identityAfter)
                throw new SaveTargetChangedException("저장 위치를 확인하는 중 대상 파일이 변경되었습니다.");
            return new ApprovedSaveTarget(fullPath, true, identityAfter, sha256);
        }
        catch (FileNotFoundException)
        {
            return new ApprovedSaveTarget(fullPath, false, default, null);
        }
    }

    internal void CommitMove(string tempPath)
    {
        if (!ExistedAtSelection)
        {
            try
            {
                File.Move(tempPath, FullPath, overwrite: false);
            }
            catch (IOException ex) when (File.Exists(FullPath) || Directory.Exists(FullPath))
            {
                throw new SaveTargetChangedException("다운로드 중 같은 경로에 파일이 생성되어 덮어쓰기를 중단했습니다.", ex);
            }
            return;
        }
        if (Directory.Exists(FullPath))
            throw new SaveTargetChangedException("다운로드 중 대상 경로가 폴더로 바뀌어 덮어쓰기를 중단했습니다.");
        SafeFileHandle handle = CreateFile(
            FullPath,
            GenericRead | GenericWrite,
            FileShareDelete,
            IntPtr.Zero,
            OpenExisting,
            FileAttributeNormal,
            IntPtr.Zero);
        if (handle.IsInvalid)
        {
            int err = Marshal.GetLastWin32Error();
            handle.Dispose();
            if (err is ErrorFileNotFound or ErrorPathNotFound)
                throw new SaveTargetChangedException("다운로드 중 대상 파일이 사라져 덮어쓰기를 중단했습니다.");
            if (err == ErrorAccessDenied && Directory.Exists(FullPath))
                throw new SaveTargetChangedException("다운로드 중 대상 경로가 폴더로 바뀌어 덮어쓰기를 중단했습니다.");
            throw new IOException("대상 파일을 잠글 수 없습니다.", Marshal.GetExceptionForHR(Marshal.GetHRForLastWin32Error()));
        }
        using var existingStream = new FileStream(handle, FileAccess.ReadWrite);
        var currentIdentity = ReadHandleIdentity(handle);
        if (currentIdentity != ExistingIdentity || (ulong)existingStream.Length != ExistingIdentity.FileSize)
            throw new SaveTargetChangedException("다운로드 중 대상 파일이 변경되어 덮어쓰기를 중단했습니다.");
        existingStream.Position = 0;
        string currentSha256 = Convert.ToHexString(SHA256.HashData(existingStream)).ToLowerInvariant();
        if (!string.Equals(currentSha256, ExistingSha256, StringComparison.OrdinalIgnoreCase))
            throw new SaveTargetChangedException("다운로드 중 대상 파일이 변경되어 덮어쓰기를 중단했습니다.");
        // While existingStream is held open with GenericRead|GenericWrite and FileShareDelete (no read/write sharing),
        // external writes and standard Win32 moves/overwrites are rejected by the kernel, while FileRenameInfoEx with
        // FILE_RENAME_REPLACE_IF_EXISTS | FILE_RENAME_POSIX_SEMANTICS atomically replaces the directory entry in a single call.
        OnAfterVerifyBeforeReplace?.Invoke();
        OnBeforeCommitTempMove?.Invoke();
        AtomicReplaceOverOpenTarget(tempPath, FullPath);
    }
}
internal sealed class ServerClient : IDisposable
{
    internal const string GenerationHeader = "X-Himari-Connection-Generation";
    internal const long MaxPresentedFileBytes = 20 * 1024 * 1024;
    private readonly HttpClient http;
    private readonly string token;
    internal Uri BaseUri { get; }
    internal bool SameDevice(ServerClient other) => BaseUri == other.BaseUri && token == other.token;
    internal bool Matches(DeviceCredential credential) => credential.Server == BaseUri.ToString() && credential.Token == token;
    internal PresentedFileCardKey CardKey(string conversationId, string fileId, int version) =>
        new(BaseUri.ToString(), token, conversationId, fileId, version);
    internal bool MatchesCard(PresentedFileCardKey key) => key.Server == BaseUri.ToString() && key.Token == token;
    internal ServerClient(string address, string token) : this(address, token, new HttpClientHandler { UseCookies = false, AllowAutoRedirect = false }) { }
    internal ServerClient(string address, string token, HttpMessageHandler handler)
    {
        if (!Uri.TryCreate(address, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps || !string.IsNullOrEmpty(uri.UserInfo) || uri.AbsolutePath != "/" || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
            throw new ArgumentException("서버 주소는 경로가 없는 HTTPS 주소여야 합니다.");
        BaseUri = uri; this.token = token;
        http = new HttpClient(handler) { BaseAddress = uri, Timeout = Timeout.InfiniteTimeSpan };
    }
    internal async Task<JsonElement> PairAsync(string code, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/devices/pair");
        request.Headers.Authorization = new AuthenticationHeaderValue("Pairing", code.Replace("-", "").Replace(" ", ""));
        request.Content = JsonContent.Create(new { token });
        using var response = await http.SendAsync(request, ct);
        return await ReadAsync(response, ct);
    }
    private HttpRequestMessage Request(HttpMethod method, string path, object? body = null, string? connectionGeneration = null)
    {
        var request = new HttpRequestMessage(method, path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        if (!string.IsNullOrEmpty(connectionGeneration))
            request.Headers.TryAddWithoutValidation(GenerationHeader, connectionGeneration);
        if (body != null) request.Content = JsonContent.Create(body);
        return request;
    }
    internal async Task<JsonElement> SendAsync(HttpMethod method, string path, object? body, CancellationToken ct, string? connectionGeneration = null)
    {
        using var request = Request(method, path, body, connectionGeneration);
        using var response = await http.SendAsync(request, ct);
        return await ReadAsync(response, ct);
    }
    internal Task<JsonElement> GetPresentedFilesAsync(string conversationId, int limit, int offset, CancellationToken ct) =>
        SendAsync(HttpMethod.Get, "/api/device/conversations/" + Uri.EscapeDataString(conversationId) + $"/presented-files?limit={limit}&offset={offset}", null, ct);

    internal async Task<List<PresentedFileItem>> ReadAllPresentedFilesAsync(string conversationId, CancellationToken ct)
    {
        var items = new List<PresentedFileItem>();
        int offset = 0;
        while (true)
        {
            var page = await GetPresentedFilesAsync(conversationId, 50, offset, ct);
            if (page.ValueKind == JsonValueKind.Object && page.TryGetProperty("items", out var array) && array.ValueKind == JsonValueKind.Array)
            {
                foreach (var entry in array.EnumerateArray())
                {
                    if (entry.ValueKind != JsonValueKind.Object) continue;
                    string? fileId = entry.TryGetProperty("file_id", out var fid) && fid.ValueKind == JsonValueKind.String ? fid.GetString() : null;
                    int version = entry.TryGetProperty("version", out var ver) && ver.ValueKind == JsonValueKind.Number && ver.TryGetInt32(out int v) ? v : 0;
                    if (string.IsNullOrEmpty(fileId) || version < 1) continue;
                    string name = entry.TryGetProperty("name", out var n) && n.ValueKind == JsonValueKind.String ? n.GetString() ?? "download" : "download";
                    string mediaType = entry.TryGetProperty("media_type", out var mt) && mt.ValueKind == JsonValueKind.String ? mt.GetString() ?? "application/octet-stream" : "application/octet-stream";
                    long size = entry.TryGetProperty("size", out var sz) && sz.ValueKind == JsonValueKind.Number && sz.TryGetInt64(out long s) ? s : 0;
                    string sha256 = entry.TryGetProperty("sha256", out var sh) && sh.ValueKind == JsonValueKind.String ? sh.GetString() ?? "" : "";
                    string? createdAt = entry.TryGetProperty("created_at", out var ca) && ca.ValueKind == JsonValueKind.String ? ca.GetString() : null;
                    bool expired = entry.TryGetProperty("expired", out var ex) && ex.ValueKind is JsonValueKind.True or JsonValueKind.False && ex.GetBoolean();
                    items.Add(new PresentedFileItem(fileId, version, name, mediaType, size, sha256, createdAt, expired));
                }
            }
            if (!page.TryGetProperty("next_offset", out var nextProp) || nextProp.ValueKind != JsonValueKind.Number || !nextProp.TryGetInt32(out int next) || next <= offset)
                break;
            offset = next;
        }
        return items;
    }

    internal Task DownloadPresentedFileAsync(
        string conversationId,
        string fileId,
        int version,
        string destinationPath,
        long expectedSize,
        string expectedSha256,
        CancellationToken ct) =>
        DownloadPresentedFileAsync(
            conversationId,
            fileId,
            version,
            ApprovedSaveTarget.Capture(destinationPath),
            expectedSize,
            expectedSha256,
            ct);

    internal async Task DownloadPresentedFileAsync(
        string conversationId,
        string fileId,
        int version,
        ApprovedSaveTarget destinationTarget,
        long expectedSize,
        string expectedSha256,
        CancellationToken ct)
    {
        ArgumentNullException.ThrowIfNull(destinationTarget);
        if (expectedSize < 0 || expectedSize > MaxPresentedFileBytes)
            throw new InvalidDataException("파일 크기가 허용 범위를 벗어났습니다.");
        if (string.IsNullOrWhiteSpace(expectedSha256) || expectedSha256.Trim().Length != 64)
            throw new InvalidDataException("파일 해시 정보가 올바르지 않습니다.");
        string fullDestination = destinationTarget.FullPath;
        string directory = Path.GetDirectoryName(fullDestination)
            ?? throw new ArgumentException("저장 경로가 올바르지 않습니다.", nameof(destinationTarget));
        Directory.CreateDirectory(directory);
        string tempPath = Path.Combine(directory, $".himari-download-{Guid.NewGuid():N}.part");
        try
        {
            string url = "/api/device/conversations/" + Uri.EscapeDataString(conversationId)
                + "/presented-files/" + Uri.EscapeDataString(fileId) + "/" + version;
            using var request = Request(HttpMethod.Get, url);
            using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
            if (!response.IsSuccessStatusCode)
            {
                await ReadAsync(response, ct);
                return;
            }
            long totalBytes = 0;
            string actualSha256;
            using (var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256))
            {
                await using var source = await response.Content.ReadAsStreamAsync(ct);
                await using (var target = new FileStream(tempPath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920, useAsync: true))
                {
                    byte[] buffer = new byte[81920];
                    while (true)
                    {
                        int read = await source.ReadAsync(buffer.AsMemory(), ct);
                        if (read <= 0) break;
                        totalBytes += read;
                        if (totalBytes > expectedSize || totalBytes > MaxPresentedFileBytes)
                            throw new InvalidDataException("파일 크기가 예상과 다릅니다.");
                        hash.AppendData(buffer, 0, read);
                        await target.WriteAsync(buffer.AsMemory(0, read), ct);
                    }
                    await target.FlushAsync(ct);
                }
                actualSha256 = Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
            }
            if (totalBytes != expectedSize)
                throw new InvalidDataException("파일 크기가 예상과 다릅니다.");
            if (!string.Equals(actualSha256, expectedSha256.Trim(), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("파일 무결성(SHA-256) 검증에 실패했습니다.");
            ct.ThrowIfCancellationRequested();
            destinationTarget.CommitMove(tempPath);
        }
        finally
        {
            if (File.Exists(tempPath))
            {
                try { File.Delete(tempPath); }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
            }
        }
    }
    internal async Task<JsonElement> UploadScreenAsync(string conversationId, byte[] bytes, string contentType, CancellationToken ct)
    {
        if (contentType is not ("image/png" or "image/jpeg")) throw new ArgumentException("Unsupported screen image format.", nameof(contentType));
        if (bytes.Length == 0 || bytes.Length > 8 * 1024 * 1024) throw new ArgumentException("Invalid screen image size.", nameof(bytes));
        string name = contentType == "image/png" ? "screen.png" : "screen.jpg";
        using var request = Request(HttpMethod.Post, "/api/conversations/" + Uri.EscapeDataString(conversationId) + "/uploads?name=" + name);
        request.Content = new ByteArrayContent(bytes);
        request.Content.Headers.ContentType = new MediaTypeHeaderValue(contentType);
        using var response = await http.SendAsync(request, ct);
        return await ReadAsync(response, ct);
    }
    /// <summary>V3-P91 §4.2: the screen a request asked for. 410 (request_ended) when it ended meanwhile.</summary>
    internal async Task AnswerScreenAsync(string requestId, byte[] bytes, string contentType, CancellationToken ct)
    {
        if (contentType is not ("image/png" or "image/jpeg")) throw new ArgumentException("Unsupported screen image format.", nameof(contentType));
        if (bytes.Length == 0 || bytes.Length > 8 * 1024 * 1024) throw new ArgumentException("Invalid screen image size.", nameof(bytes));
        using var request = Request(HttpMethod.Post, "/api/device/screens/" + Uri.EscapeDataString(requestId));
        request.Content = new ByteArrayContent(bytes);
        request.Content.Headers.ContentType = new MediaTypeHeaderValue(contentType);
        using var response = await http.SendAsync(request, ct);
        await ReadAsync(response, ct);
    }
    /// <summary>V3-P91 §4.2: no screen for this request, and why (capture_failed, excluded, turned_off, asleep).</summary>
    internal Task RefuseScreenAsync(string requestId, string reason, CancellationToken ct) =>
        SendAsync(HttpMethod.Post, "/api/device/screens/" + Uri.EscapeDataString(requestId) + "/refuse", new { reason }, ct);
    private static async Task<JsonElement> ReadAsync(HttpResponseMessage response, CancellationToken ct)
    {
        if (response.StatusCode == HttpStatusCode.NoContent) return default;
        var body = await response.Content.ReadAsStringAsync(ct);
        if (!response.IsSuccessStatusCode)
        {
            string code = "http_error";
            try { using var doc = JsonDocument.Parse(body); code = doc.RootElement.GetProperty("error").GetProperty("code").GetString() ?? code; } catch (JsonException) { } catch (KeyNotFoundException) { }
            throw new ApiException(response.StatusCode, code);
        }
        using var parsed = JsonDocument.Parse(body); return parsed.RootElement.Clone();
    }
    internal async IAsyncEnumerable<(string Event, JsonElement Data)> StreamAsync(string path, [EnumeratorCancellation] CancellationToken ct)
    {
        using var request = Request(HttpMethod.Get, path);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("text/event-stream"));
        using var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
        if (!response.IsSuccessStatusCode) { await ReadAsync(response, ct); yield break; }
        using var reader = new StreamReader(await response.Content.ReadAsStreamAsync(ct), Encoding.UTF8);
        string type = "message"; var data = new StringBuilder();
        while (await reader.ReadLineAsync(ct) is { } line)
        {
            if (line.Length == 0)
            {
                if (data.Length > 0) { using var json = JsonDocument.Parse(data.ToString()); yield return (type, json.RootElement.Clone()); }
                type = "message"; data.Clear(); continue;
            }
            if (line.StartsWith(':')) continue;
            int colon = line.IndexOf(':'); string field = colon < 0 ? line : line[..colon];
            string value = colon < 0 ? "" : line[(colon + 1)..]; if (value.StartsWith(' ')) value = value[1..];
            if (field == "event") type = value;
            else if (field == "data") { if (data.Length > 0) data.Append('\n'); data.Append(value); }
        }
    }
    public void Dispose() => http.Dispose();
}
