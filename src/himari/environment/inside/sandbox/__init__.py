"""OS Sandbox for ``inside/run_command`` (ENV-04/05, V3-032, V3-036/037, V3-050).

bubblewrap (user/pid/ipc/uts/cgroup/**net** namespaces, empty tmpfs root,
read-only ``/usr`` view, the principal's Workspace bound from a verified fd)
plus an in-sandbox helper that installs Landlock (filesystem + TCP) and
no_new_privs before the command runs. ENFORCED only after a real-spawn
self-test at startup (V3-050); otherwise commands are refused — never run
unsandboxed.
"""

from himari.environment.inside.sandbox.backend import (
    CommandResult,
    SandboxBackend,
    SandboxSetupError,
    SandboxState,
    SandboxUnavailableError,
)

__all__ = ["CommandResult", "SandboxBackend", "SandboxSetupError", "SandboxState", "SandboxUnavailableError"]
