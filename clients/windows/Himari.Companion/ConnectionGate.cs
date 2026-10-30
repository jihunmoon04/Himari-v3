namespace Himari.Companion;

internal sealed class ConnectionGate
{
    private readonly SemaphoreSlim semaphore = new(1, 1);
    internal bool TryEnter() => semaphore.Wait(0);
    internal Task EnterAsync(CancellationToken ct) => semaphore.WaitAsync(ct);
    internal void Exit() => semaphore.Release();
}
