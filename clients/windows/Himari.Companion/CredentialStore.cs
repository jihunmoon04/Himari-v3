using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace Himari.Companion;

// A pending token is saved before pairing so a lost response can be retried.
internal static class CredentialStore
{
    private const string Target = "Himari.Companion.Device", PendingTarget = "Himari.Companion.Pending";
    private const int Generic = 1, LocalMachine = 2, NotFound = 1168;
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct Credential
    {
        public int Flags, Type;
        public string TargetName, Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public int CredentialBlobSize;
        public IntPtr CredentialBlob;
        public int Persist, AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias, UserName;
    }
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CredWriteW")]
    private static extern bool CredWrite(ref Credential credential, int flags);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CredReadW")]
    private static extern bool CredRead(string target, int type, int flags, out IntPtr credential);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true, EntryPoint = "CredDeleteW")]
    private static extern bool CredDelete(string target, int type, int flags);
    [DllImport("advapi32.dll")] private static extern void CredFree(IntPtr credential);

    internal static string NewToken() => "hd_" + Convert.ToBase64String(RandomNumberGenerator.GetBytes(32)).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    private static void Write(string target, string username, string token)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(token);
        IntPtr blob = Marshal.AllocHGlobal(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var credential = new Credential { Type = Generic, TargetName = target, UserName = username, CredentialBlob = blob, CredentialBlobSize = bytes.Length, Persist = LocalMachine };
            if (!CredWrite(ref credential, 0)) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally { CryptographicOperations.ZeroMemory(bytes); Marshal.Copy(bytes, 0, blob, bytes.Length); Marshal.FreeHGlobal(blob); }
    }
    private static (string UserName, string Token)? ReadTarget(string target)
    {
        if (!CredRead(target, Generic, 0, out var ptr))
        {
            var error = Marshal.GetLastWin32Error();
            if (error == NotFound) return null;
            throw new Win32Exception(error);
        }
        try
        {
            var credential = Marshal.PtrToStructure<Credential>(ptr);
            var bytes = new byte[credential.CredentialBlobSize];
            try
            {
                Marshal.Copy(credential.CredentialBlob, bytes, 0, bytes.Length);
                return (credential.UserName, Encoding.UTF8.GetString(bytes));
            }
            finally { CryptographicOperations.ZeroMemory(bytes); }
        }
        finally { CredFree(ptr); }
    }
    private static void DeleteTarget(string target)
    {
        if (!CredDelete(target, Generic, 0) && Marshal.GetLastWin32Error() != NotFound) throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    internal static DeviceCredential? ReadActive()
    {
        var item = ReadTarget(Target);
        if (item is null) return null;
        if (item.Value.UserName.StartsWith("pending|", StringComparison.Ordinal))
        {
            // Migrate credentials written by the first prototype without losing a pending token.
            if (ReadTarget(PendingTarget) is null) Write(PendingTarget, item.Value.UserName[8..] + "|", item.Value.Token);
            DeleteTarget(Target); return null;
        }
        var server = item.Value.UserName.StartsWith("paired|", StringComparison.Ordinal) ? item.Value.UserName[7..] : item.Value.UserName;
        return new(server, item.Value.Token, null);
    }
    internal static DeviceCredential? ReadPending()
    {
        var item = ReadTarget(PendingTarget); if (item is null) return null;
        int separator = item.Value.UserName.LastIndexOf('|');
        return separator < 0 ? new(item.Value.UserName, item.Value.Token, null) : new(item.Value.UserName[..separator], item.Value.Token, item.Value.UserName[(separator + 1)..]);
    }
    internal static void SaveActive(DeviceCredential item) => Write(Target, "paired|" + item.Server, item.Token);
    internal static void SavePending(DeviceCredential item) => Write(PendingTarget, item.Server + "|" + item.CodeFingerprint, item.Token);
    internal static void DeleteActive() => DeleteTarget(Target);
    internal static void DeletePending() => DeleteTarget(PendingTarget);
}
