using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace Himari.Companion;

// A pending token is saved before pairing so a lost response can be retried.
internal static class CredentialStore
{
    private const string Target = "Himari.Companion.Device";
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
    internal static void Save(string server, string token, bool pending)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(token);
        IntPtr blob = Marshal.AllocHGlobal(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var credential = new Credential { Type = Generic, TargetName = Target, UserName = (pending ? "pending|" : "paired|") + server, CredentialBlob = blob, CredentialBlobSize = bytes.Length, Persist = LocalMachine };
            if (!CredWrite(ref credential, 0)) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        finally { CryptographicOperations.ZeroMemory(bytes); Marshal.Copy(bytes, 0, blob, bytes.Length); Marshal.FreeHGlobal(blob); }
    }
    internal static (string Server, string Token, bool Pending)? Read()
    {
        if (!CredRead(Target, Generic, 0, out var ptr))
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
                var username = credential.UserName;
                bool pending = username.StartsWith("pending|", StringComparison.Ordinal);
                string server = pending ? username[8..] : username.StartsWith("paired|", StringComparison.Ordinal) ? username[7..] : username;
                return (server, Encoding.UTF8.GetString(bytes), pending);
            }
            finally { CryptographicOperations.ZeroMemory(bytes); }
        }
        finally { CredFree(ptr); }
    }
    internal static void Delete()
    {
        if (!CredDelete(Target, Generic, 0) && Marshal.GetLastWin32Error() != NotFound) throw new Win32Exception(Marshal.GetLastWin32Error());
    }
}
