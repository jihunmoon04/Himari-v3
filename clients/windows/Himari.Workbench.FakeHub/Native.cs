using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

/// <summary>
/// The process handle and Job Object parts of H15, as the hub does them on Windows: a process is opened once, its
/// creation time is read from that handle, and it is ended through that same handle (a pid cannot be reused while the
/// handle is held). The runner puts itself into a Job with KILL_ON_JOB_CLOSE before it starts anything.
/// </summary>
internal static partial class Native
{
    private const uint ProcessTerminate = 0x0001;
    private const uint ProcessQueryLimitedInformation = 0x1000;
    private const uint Synchronize = 0x0010_0000;
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x2000;

    /// <summary>Opens a process to check and possibly end it; null when it cannot be opened (gone, or not ours).</summary>
    public static SafeProcessHandle? Open(int pid)
    {
        var handle = OpenProcess(ProcessTerminate | ProcessQueryLimitedInformation | Synchronize, false, pid);
        if (!handle.IsInvalid) return handle;
        handle.Dispose();
        return null;
    }

    /// <summary>The creation time read from this handle, in the form the state file's <c>started_at</c> uses.</summary>
    public static string? StartedAt(SafeProcessHandle handle) =>
        GetProcessTimes(handle, out long created, out _, out _, out _) ? DateTime.FromFileTimeUtc(created).ToString("o") : null;

    public static string? StartedAt(int pid)
    {
        using var handle = Open(pid);
        return handle is null ? null : StartedAt(handle);
    }

    /// <summary>Ends the process through this handle and waits for it; false when that could not be confirmed.</summary>
    public static bool TerminateAndWait(SafeProcessHandle handle, int waitMs) =>
        TerminateProcess(handle, 1) && WaitForSingleObject(handle, (uint)waitMs) == 0;

    /// <summary>
    /// Puts the current process into a new Job that kills everything in it when its last handle closes. The handle is
    /// returned to be held for the process's whole life; the OS closes it when the process ends, however it ends.
    /// </summary>
    public static SafeFileHandle JoinNewKillOnCloseJob()
    {
        var job = CreateJobObjectW(IntPtr.Zero, null);
        if (job.IsInvalid) throw new Win32Exception(Marshal.GetLastPInvokeError());
        var info = new ExtendedLimitInformation { BasicLimitInformation = new BasicLimitInformation { LimitFlags = JobObjectLimitKillOnJobClose } };
        if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, ref info, (uint)Marshal.SizeOf<ExtendedLimitInformation>())
            || !AssignProcessToJobObject(job, GetCurrentProcess()))
        {
            int error = Marshal.GetLastPInvokeError();
            job.Dispose();
            throw new Win32Exception(error);
        }
        return job;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [LibraryImport("kernel32.dll", SetLastError = true)]
    private static partial SafeProcessHandle OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, int pid);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetProcessTimes(SafeProcessHandle process, out long creation, out long exit, out long kernel, out long user);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool TerminateProcess(SafeProcessHandle process, uint exitCode);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    private static partial uint WaitForSingleObject(SafeProcessHandle handle, uint milliseconds);

    [LibraryImport("kernel32.dll", SetLastError = true, StringMarshalling = StringMarshalling.Utf16)]
    private static partial SafeFileHandle CreateJobObjectW(IntPtr attributes, string? name);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool SetInformationJobObject(SafeFileHandle job, int infoClass, ref ExtendedLimitInformation info, uint length);

    [LibraryImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool AssignProcessToJobObject(SafeFileHandle job, IntPtr process);

    [LibraryImport("kernel32.dll")]
    private static partial IntPtr GetCurrentProcess();
}
