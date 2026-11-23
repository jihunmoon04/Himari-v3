"""OS Sandbox (ENV-04/05, V3-032, V3-036/037, V3-050, V3-P59 §3).

bubblewrap (user/pid/ipc/uts/cgroup/**net** namespaces, empty tmpfs root,
read-only ``/usr`` view, the profile's folders bound from verified fds)
plus an in-sandbox helper that installs Landlock (filesystem + TCP) and
no_new_privs before the command runs. ENFORCED only after a real-spawn
self-test at startup (V3-050); otherwise launches are refused — never run
unsandboxed.

A module of its own above ``common`` (it imports nothing else of HIMARI).
Consumers use :mod:`himari.sandbox.api`; Bootstrap builds the server
sandboxes.
"""
