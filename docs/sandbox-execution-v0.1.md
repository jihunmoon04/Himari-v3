# Sandbox Execution Contract v0.1 — v2 selective reuse for v3

> 2026-10-13 · REVIEW DRAFT. Binding user decisions: V3-032~036. All concrete backend DTOs, platform versions and resource limits remain PROPOSED. v3 code not yet implemented.
>
> Links: [unified contracts](contracts.md) · [default workspace](default-workspace-files-v0.2.md) · [decisions](decisions.md) · [open questions](open-questions.md).

## 1. Explicit decisions (CONFIRMED)

- **One default Workspace root per trusted user/principal**; each Conversation has a separate default folder/cwd. Other Conversation directories accessible to that same authorized user (V3-033, V3-035).
- **run_command supported inside an OS Sandbox**, with no unprotected execution fallback (V3-032).
- **Network access within the Sandbox denied by default**; future Web Search is an independently authorized Tool outside the command Sandbox and does not grant arbitrary shell network access (V3-036).
- **Initial no human Approval and no Worktree/Workspace-copy isolation** decisions retained (V3-034). Sandbox protects host/other users, not the files of other permitted same-user Conversations.
- Plan/Note remain Data conversation-scoped Artifacts. Default Workspace files are distinct.

Not guaranteed: isolation among same-user Conversation folders, automatic backup/undo of arbitrary command side effects, long-lived ManagedProcess, unrestricted internet, sandbox suitability on OS without certified backend.

## 2. Evidence from v2 implementation

Verified source: [v2 Phase 3.5 architecture](https://github.com/jihunmoon04/HIMARI-v2/blob/main/docs/stages/64-sandboxed-workspace-execution/architecture.md), [contract.py](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/contract.py), [linux_backend.py](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/linux_backend.py), [self_test.py](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/self_test.py), [admission.py](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/workspace/admission.py), [direct_fs.py](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/workspace/direct_fs.py), [command.py](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/tools/executors/command.py).

| v2 capability | v3 disposition |
| --- | --- |
| Explicit Bubblewrap mount namespace view, Landlock helper, NoNewPrivileges, dropped capabilities | **Reuse security mechanics**, update grant mapping and policy generation |
| SANDBOX_POLICY_GENERATION / activation marker / startup self-test | **Reuse enforcement pattern**; new v3 profile must be certified independently |
| WorkspaceAdmission, pinned backing identity, mountinfo, no unsafe aliases, hardlink scan | **Adapt** from one Direct Work root to one principal-owned shared root + separate Conversation cwd |
| fd-safe openat2 beneath verified root for structured file tools | **Reuse principle**, adapt path resolution to shared principal root |
| ProtectedProcessSpawn, bounded output, process tree teardown, no implicit shell | **Reuse mechanics**, remove Task/Work/Approval dependencies |
| Direct WorkAuthority, Task/Work lifecycle and associated schema | **Do not copy**; Data Binding + trusted principal + working-directory mapping replace authority source |
| Network containment | **Not implemented in v2**; v2 explicitly disclaims network isolation and does not pass bwrap --unshare-net. v3 must implement and verify it |
| v2 ALWAYS ASK approval | **Not copied into initial v3 Workspace** per user decision |

This is selective reuse rather than a wholesale file copy: v2 policy builder, command executor and process launcher are coupled to WorkAuthority and a single Workspace root/cwd.

## 3. Trust boundary and filesystem view (PROPOSED)

Host storage (paths illustrative):
- managed-root/principal-A/conversations/conv-1
- managed-root/principal-A/conversations/conv-2
- managed-root/principal-A/shared
- managed-root/principal-B/conversations/conv-3
- data-root (database, auth, credentials) — never granted

Sandbox for principal A, Conversation 1:
- /workspace is the **only RW root**, mounted from principal-A workspace
- cwd = /workspace/conversations/conv-1
- /workspace/conversations/conv-2 is accessible on purpose (including writes) if authorized for that same principal
- minimal system executables/libraries RO; private tmpfs HOME/TMP, sandbox-local proc/dev
- no principal-B directory, DataRoot, host HOME, credentials, Docker socket or unrelated mounts

Authorization derivation:
1. Trusted Web/CLI identity resolves principal_id (never from model-provided path).
2. Data maps Conversation to owner principal, Workspace Registry ID, Binding ID and relative default cwd.
3. Environment rechecks current owner/Binding, root/cwd identity and authorized scope per call.
4. Immutable SandboxSpec maps **principal root to sandbox-visible /workspace** and sets **current Conversation cwd**.
5. Process launch revalidates actual path/backing/mount layout and activation/profile generation. Unsafe mismatch fails closed.
6. The Folder is an organizational boundary, **not a Conversation-level security boundary**; any inside command can mutate permitted files in other Conversations.

Important adaptation: v2 bind-mounts exactly one Work Workspace at the physical root and uses that root as cwd. v3 binds the broader principal root into a virtual /workspace and uses an inner Conversation cwd. The old SandboxSpec and bwrap_arguments are not byte-compatible with this policy.

## 4. Command execution backend (PROPOSED)

Flow:

    Tool.run_command(argv, call_id, binding_ref)
      -> Tool schema, command scope, duration/output budgets, Scheduler
      -> Environment principal + Binding + cwd + backing verification
      -> v3 ProtectedSpawnBackend (no unsandboxed fallback)
           -> Bubblewrap empty-root/explicit allowlist mount
           -> isolated user, PID, IPC, UTS and **network** namespaces
           -> Landlock read/write policy + NoNewPrivileges + no capabilities
           -> verified startup handshake before actual argv executes
      -> bounded stdout/stderr and exit status, entire process tree teardown
      -> durable ToolExecutionRecord + canonical ToolResult

Recommended mechanical rules:
- argv array is exact, shell=False (no hidden shell wrapping). Explicit shell argv allowed, but still isolated.
- CWD is fixed by trusted Conversation mapping, not arbitrary model-provided host cwd.
- Stdin closed by default, no detached/background process initially.
- Mandatory finite timeout, CPU/memory/PID/fd/output limits; output limited **during acquisition**, not only model projection.
- No ambient environment variable, host credential, extra inherited descriptor, or host socket.
- Host protected roots are absent from mount view, not merely RO. Dynamic libraries/toolchain require explicit RO allowlist.
- Structured filesystem tools use descriptor-relative fd safe methods and own path validation, even if they do not spawn processes.
- Fail-closed Sandbox unavailable/setup failed, unsupported host, changed mounts/backing/ownership, or policy marker mismatch.

## 5. Network DENY (v3-specific)

- Use **kernel-enforced egress isolation**. Candidate: Bubblewrap --unshare-net (plus host-socket exclusion and appropriate loopback/IPC policy); validate with real-host tests. Merely omitting proxy variables or instructing the LLM to avoid networking is insufficient.
- Reject Sandbox execution if network isolation cannot be enforced. Sandbox activation must include network controls in version/fingerprint and self-test.
- Deny external TCP/UDP/DNS access by commands; specify Unix-domain and loopback cases at backend freeze. Separate sockets/mounts must not defeat network policy.
- Provider's own network API access runs in trusted Provider/host service and is **not in the command Sandbox**.
- Later separate Web Search Tool (TOOL-06 provisional) can call a trusted network service with independent policy. Search results are untrusted content, not authority to execute commands or widen sandbox mounts.
- A future explicit network-enabled Environment or Tool would need new policy and user decision. The first default Workspace does not silently gain network access.

## 6. Admission, activation and certification (PROPOSED)

Reuse v2 activation classes/behavior conceptually: UNAVAILABLE / CAPABLE_INACTIVE / ENFORCED / SETUP_FAILED.

- Capability discovery: bwrap present and safe, Linux namespace and Landlock support, trusted interpreter/helper, policy prerequisites.
- v3 policy generation and activation marker are **new**: a v2 marker must not activate v3 by accident.
- Startup self-test through the actual protected spawn path covers: command execution, A cwd, allowed A-to-B same principal read/write, denied access to another principal, DataRoot/secret absence, symlink/hardlink/mount alias denial, private HOME/TMP, NoNewPrivileges, capability zero, process tree cleanup, fd hygiene and **network denied**.
- Recheck Sandbox readiness, marker/profile version and current binding/backing **at every spawn**; no automatic de-escalation to ambient subprocess.
- A WorkspaceAdmission walk analogous to v2 can prevent preexisting outside hardlink aliases and hidden mount access. New multiple-Conversation root is potentially larger and mutable; **bounded and efficient re-admission/alias proof strategy must be designed**. The sandbox claim cannot be certified merely by reusing a v2 test at a different root.
- Linux production-like host gate required. Windows/macOS without an implemented and proven equivalent backend refuse commands rather than run unsandboxed.

## 7. Durability, Pause and concurrency (PROPOSED)

- run_command is a Tool execution, **not** a Run. Run is one LLM Provider call.
- Before launch, record intent; after settlement, record execution outcome. On uncertain result after crash, do not blindly rerun command. ToolCall ID preserved.
- Soft Pause: already-running bounded command may finish while pause is requested. No new command starts after pause gate; pending calls remain pending and Resume must check current authority.
- With same principal root, different Conversations have overlapping filesystem authority. Resource-conflict key must be the **real shared principal root identity**, not Conversation ID nor distinct Binding ID. Arbitrary run_command vs another command or file write in that root: initially serialize.
- Same-user cross-Conversation edits are intentionally possible and potentially destructive. No Approval implies no per-action user confirmation; no automatic undo is promised.

## 8. Public contract ownership

| ID | Owner / provider → consumer | Source of Truth | Proposed location |
| --- | --- | --- | --- |
| ENV-04 SandboxedCommandExecution | Environment protected process backend → Tool | Kernel enforcement + current Binding/principal and process output | docs/contracts.md + planned environment/api.py |
| ENV-05 SandboxCapabilityActivation | Environment activation/probe → Environment startup/Tool | observed host capability + current validated profile marker | docs/contracts.md + planned environment/api.py |
| DATA-11 ConversationWorkingDirectory | Data → Environment/Prompt | Data Principal/Conversation/Workspace Binding mapping | docs/contracts.md + planned data/api.py |
| TOOL-05 RunCommandTool | Tool → Control | Tool registry + ENV-04 outcome | docs/contracts.md + planned tool/api.py |
| TOOL-06 WebSearch (FUTURE) | Tool / trusted Network Adapter → Control | third-party web content, explicitly untrusted | docs/contracts.md + planned tool/api.py |

File metadata and published file bytes remain separate from mutable workspace bytes; command execution can change current files, not immutable already published file versions (if implemented as proposed outside RW sandbox mounts).

## 9. Review tests / gates

1. A and B same principal have distinct cwd and allowed A↔B file access within the same RW root.
2. A cannot read/write another principal's root, nor DataRoot/credentials/host HOME even with symlinks, mount aliases, hardlink tricks.
3. Startup certification and per-spawn profile/version+identity checks must pass.
4. Outbound network TCP/UDP and external DNS denied; mounted Unix socket bypass denied.
5. Requested argv cannot start if Sandbox enforcement or network isolation is missing. No unsandboxed subprocess fallback.
6. Stdin, inherited fds, private HOME/TMP, capability/NNP and child-tree cleanup pass.
7. Finite runtime and captured stdout/stderr limits; non-zero exit is observation, not fabricated success.
8. Concurrent A/B commands conflicting on shared user root serialize even with distinct Binding IDs.
9. Process crash after uncertain command effect never blindly retries; corresponding ToolExecution remains auditable.
10. Future Web Search remains network-enabled in its own trusted boundary **without** opening network to the Sandbox.

## 10. OPEN (not yet ratified)

- Principal identity/auth in Web+CLI and physical root naming, quota; whether cross-conversation writes ever require optional user warning.
- Exact Linux bwrap/Landlock executable packages, network namespace policy and host certification; localhost vs socket behavior.
- Preexisting hardlink scan / protected backing overlap checks on large principal root, TOCTOU-resistant cwd pinning after user edits.
- CPU/memory/PID/time/output, supported commands/toolchains and private writable cache behavior (pip install may not work offline).
- Sandboxed command changes to working files vs Data FileAsset revisions; published immutable file storage outside RW mount.
- Any future separate Web Search API, result policy and UI contract.
