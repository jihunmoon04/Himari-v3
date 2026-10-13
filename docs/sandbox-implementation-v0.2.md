# Environment Sandbox v3 — Implementation Contract Candidate v0.2

> 2026-10-13. **REVIEW CANDIDATE; NOT IMPLEMENTED, NOT FROZEN.** Binding decisions V3-030, V3-032~037; all proposed DTO names, activation mechanics, limits, component filenames and packetization below remain design proposals.
>
> References: [Sandbox v0.1](sandbox-execution-v0.1.md), [Default Workspace v0.2](default-workspace-files-v0.2.md), [unified contracts](contracts.md), [Data contract](data-contract-v0.1.md), [pytest policy](testing-policy.md).
>
> v2 baseline: [sandbox contract](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/contract.py), [protected backend](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/linux_backend.py), [Landlock helper](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/landlock_exec.py), [activation](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/activation.py), [self-test](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/sandbox/self_test.py), [Workspace admission](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/workspace/admission.py), [fd-relative filesystem](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/workspace/direct_fs.py), [run_command executor](https://github.com/jihunmoon04/HIMARI-v2/blob/main/src/himari/tools/executors/command.py).

## 1. Scope, confirmed constraints, non-goals

**Confirmed:** user/principal-owned managed root, per-Conversation working directory and same-principal cross-Conversation access; no Worktree and no initial human Approval. Built-in `inside/run_command` must execute under OS isolation; network profile is `ISOLATED_ONLY`. The command Sandbox cannot contact internet/host services; future Web Search uses a different trusted Tool. Sandbox not available ⇒ do not run command.

**Not in first packet:** ManagedProcess (`start_process` / persistent PID), connected/allowlisted/proxied network mode, Task/Work/ManagerTurn authority, Web Search implementation, user-visible approval UI, automatic rollback of Workspace changes.

**Security boundary:** same-principal Conversation folders are *not* isolated from one another. Sandbox **must** isolate unrelated principals, protected HIMARI data/credentials, trusted host runtime and ambient network/IPC capabilities.

## 2. Mapping from v2 implementation to v3

| v2 component | Reuse | v3 changes |
| --- | --- | --- |
| `sandbox/contract.py` | FilesystemAuthority/SandboxSpec validation, RO runtime roots, protected overlap checks | Remove WorkAuthority/WorkBackingIdentity coupling; UserRootAuthority and separate virtual mount/cwd |
| `sandbox/linux_backend.py` | bubblewrap process namespace + explicit mount view + enforcement handshake, backend states | Add network namespace isolation, user-root virtual bind `/workspace` and current Conversation cwd |
| `sandbox/landlock_exec.py` | trusted pre-exec helper, NNP, Landlock policy | Update writable grant location and generation. Test mount + Landlock independently |
| `sandbox/activation.py` | strict marker, immutable generation/evidence, CAPABLE_INACTIVE/ENFORCED | New **v3** activation generation; do not accept v2 marker |
| `sandbox/self_test.py` | real protected spawn startup gate, negative escape tests | Add A→B same principal access, other-principal refusal, network + socket refusal |
| `workspace/admission.py` | fd-pinned root, mountinfo/backing identity, no nested mount, external hardlink checks | Admission at **whole principal root** plus cwd identity; scan budget/performance needs proof |
| `workspace/direct_fs.py` | openat2 descriptor-relative BENEATH/NO_SYMLINKS/NO_MAGICLINKS/NO_XDEV | Resolve model paths under verified principal root; default cwd is relative |
| `tools/executors/command.py` | argv array, bounded output/time, drain and process-tree kill | Replace WorkAuthority and human Approval hooks with trusted request + Environment Port |
| `tools/process_tree.py` / `tools/process_environment.py` | process containment & sanitized environment | Adapt interfaces/limits if needed, not automatic code copy |

**Keep exact security properties where applicable; do not copy v2 Task/Work lifetime or database schema.** v2 did not implement network isolation; v3 requires entirely new networking certification.

## 3. Trusted identities and workspace lifecycle (candidate)

Proposed Data source/DTO:

~~~text
TrustedExecutionScope:
    principal_id           # from authenticated Web/CLI identity, not from model/tool arguments
    conversation_id
    environment_instance_id
    workspace_binding_id

ResolvedWorkspaceContext:
    workspace_id
    principal_id
    admitted_host_root_ref # trusted opaque identity, not exposed to Prompt
    cwd_relative           # conversations/<opaque conversation directory>
    registry_revision / binding_revision
~~~

1. Web/CLI authenticates a principal and passes a trusted scope to Control/Tool. Database must verify the Conversation belongs to that principal. The model can supply a path or binding reference but never a principal grant.
2. Data DATA-11 resolves owner → managed default Workspace ID → active Binding → Conversation directory. Each principal root persists beyond a single Conversation.
3. Root mapping is created by an authenticated administration/API path, **not by parsing a user-supplied host path or Conversation text**; root is under a trusted managed storage parent outside HIMARI DataRoot.
4. New Conversation gets its own unique opaque directory under `conversations/`, with no path reuse as IDs recycle. Initial chmod/ownership and parent identity are pinned/verified.
5. The sandbox mounts the **principal root RW**; the default cwd is inside it. Any same-user authorized other Conversation folder is intentionally RW.
6. Data Binding and scope are checked at each launch. Revocation, root swap, inaccessible cwd or auth mismatch denies spawn; a previously composed Prompt is never an authority.
7. Data owns principal→Workspace/cwd mapping, Environment owns host path validation/execution. **Do not store absolute host root paths in Provider input, model-visible ToolResults, or UI file links.**

**Important design risk:** v2's v2 WorkAuthority backing proof was fixed for one Work. A v3 shared root can change while another Conversation runs. A verified fd and mount identity must remain bound through spawn; a merely checked string pathname must not become an unchecked mount target. Mount/bind race mitigation is a pre-implementation blocking review.

## 4. Sandbox specification, isolation and startup

### 4.1 Proposed immutable internal spawn spec

~~~text
SandboxSpecV3:
    policy_generation
    principal_root_authority    # pinned root + backing identity from trusted admission
    virtual_mount_root = "/workspace"
    workspace_access = RW
    cwd_relative                # verified under principal root
    system_roots_ro             # minimal explicit allowlist
    protected_roots_absent      # HIMARI DataRoot, credentials, other principals
    private_tmp_home
    network_profile = ISOLATED_ONLY
    stdin_policy = CLOSED
    stdio_policy = only 0/1/2
    execution_limits
~~~

No dynamic network flag appears in the model-visible Tool schema. The caller cannot enlarge mounts through `argv`, `cwd`, file names, Tool payload fields or Provider instructions. The SandboxSpec comes from trusted composition and current Data scope, not text supplied by the LLM.

### 4.2 Protected spawn sequence (proposal)

~~~text
Tool.run_command(argv, timeout?, binding_ref)
  -> Tool validates schema, recorded Tool intent, Scheduler admission
  -> Environment resolves principal/Conversation/Binding and verified root/cwd
  -> Sandbox activation ENFORCED + policy generation match
  -> Fresh root/mount/alias proof and verified SandboxSpec
  -> ProtectedSpawnBackend.spawn
       bubblewrap empty root + minimal RO runtime + RW /workspace
       --unshare-user / pid / ipc / uts / net, drop capabilities
       per-process private HOME/tmpfs and private proc/dev
       trusted helper installs NNP + Landlock
       trusted handshake must report enforcement BEFORE requested argv executes
  -> bounded wait/output drain + kill/reap process tree
  -> ToolExecutionRecord raw outcome + bounded canonical ToolResult
~~~

Do not auto retry a setup refusal by launching ambient `subprocess`. Backend refusal is a typed Tool failure; an actual post-spawn crash with unknown outcome is `UNKNOWN`, never invented success.

### 4.3 Activation state and v3 policy generation

Borrow v2's state semantics (not its exact storage schema):

- `UNAVAILABLE`: host lacks trustworthy prerequisites.
- `CAPABLE_INACTIVE`: capability may exist but no valid approved generation/self-test.
- `ENFORCED`: exact new v3 generation and activation record accepted **and** full real-spawn startup test passed.
- `SETUP_FAILED`: setup/self-test failed; no command executes.

Only startup or an explicit administrative activation path can establish ENFORCED. `ready()` may demote but never auto-promote or silently run a self-test. Each spawn verifies marker/generation, trusted scope/root identity and current policy. v2 marker cannot activate v3.

Policy generation must change for any changed mount layout, runtime interpreter/helper, Landlock rights, cwd mapping, network namespace/IPC behavior, FD policy, resource constraints or self-test criteria.

**Host certification gate is separate from pytest selection**: a real target-like Linux environment must prove the actual policy, even if only Environment implementation internals changed.

### 4.4 Isolated-only networking

- Bubblewrap `--unshare-net` is the candidate process network namespace boundary; the isolated namespace must have no host-routable NIC and no usable paths to host endpoints.
- The mount view must exclude host AF_UNIX socket files, Docker socket, SSH agent, service sockets and other privileged IPC. Deny leaking inherited connected FDs.
- Independent network tests: outbound IPv4 and IPv6 socket connect fail; host localhost (127.0.0.1, ::1) not reachable; DNS and cloud metadata endpoint unreachable; protected host Unix socket cannot be opened.
- A loopback socket *inside the same isolated namespace* is a different threat than host localhost: scope must be tested rather than claiming absolutely no socket syscalls.
- The Provider host process may still call LLM APIs; the untrusted command Sandbox cannot. Future Web Search is separate trusted Tool, not a network switch on `run_command`.

## 5. First-release command contract and failures

PROPOSED `RunCommandRequest`:
- trusted invocation context supplied by Control, opaque active Binding ref, `argv: nonempty list[str]`, optional finite timeout; no arbitrary host `cwd`, no environment-variable override.
- CLI/Web may ask for a shell explicitly through argv (e.g., `["sh", "-lc", "..."]`); launcher itself never uses `shell=True`.
- all user/model-requested child processes run only through protected launcher; no detached operation.
- stdin closed, sanitized env allowlist, PRIVATE HOME/TMP, finite timeout and output acquisition bounds, forced descendant teardown.

**v2 baseline limits available as candidates, NOT v3 decisions:** argv ≤64 strings, each ≤4,096 chars; default timeout 120 s, max 600 s; stdout/stderr read acquisition bound 256 KiB and model-facing observation target around 66 KiB (see v2 `tools/limits.py`). Ensure serialized ToolResult fits the Prompt/Run budgets, and truncation metadata is exact.

PROPOSED `RunCommandResult` fields:
- `call_id`; `state = SUCCEEDED / FAILED / DENIED / TIMEOUT / CANCELLED / UNKNOWN`; `started`; `exit_code?`; `stdout`/`stderr` as bounded text; `stdout_truncated`/`stderr_truncated`; `elapsed_ms?`; `error_code?`.
- **Non-zero exit is an observed command completion, not evidence of a Sandbox failure.** Distinguish process exit code from Tool transport/setup failure.
- No full host paths, backend command-line layout, activation marker contents, credentials or raw kernel stack traces in model-visible result.

Classify failure stages: authorization/refusal (no spawn), Sandbox unavailable/setup failure (no requested argv), timeout/kill (command may have modified files), crash/unknown effect (manual or policy recovery required). Record original Tool intent before effect, avoid blind effectful retry.

## 6. File API and parallelism

- Structured file APIs: fd-relative path normalization anchored to verified root, no symlink/magiclink/mount traversal, no special device/FIFO reads, bounded listing/read/write/edit/delete.
- Command effects are **UNKNOWN footprint**, may touch any user-authorized Conversation directory: serialize with other writes/commands for the same principal root; resource key is actual Workspace root, not distinct Binding or Conversation IDs.
- Startup admission checks the **whole principal root**, not just current Conversation folder: nested mounts and outside hardlinks must not leak outside root. If bounded proof cannot complete, refuse with a typed error; never declare safe by skipping a file category.
- Workspace bytes modified by command are current filesystem state. Data FileAsset metadata can become stale; download of a previously published immutable file version cannot rely on the mutable working path.
- No automated rollback implied.

## 7. Public ownership and minimum DTO boundaries

| ID | Owner | Provider → Consumer | Source of Truth | Proposed public location |
| --- | --- | --- | --- | --- |
| DATA-11 | Data | Data → Environment/Prompt | persisted principal/Conversation/Binding/cwd mapping | `data/api.py` |
| ENV-04 | Environment | protected spawn adapter → Tool | live kernel-enforced Sandbox process + DATA-11 | `environment/api.py` |
| ENV-05 | Environment | capability/activation/self-test → Environment startup and Tool | host probe + operator activation/policy generation | `environment/api.py` |
| TOOL-05 | Tool | command dispatcher → Control/Provider | validated Tool schema + ENV-04 result | `tool/api.py` |
| FILE-01 | Environment | verified structured filesystem ops → Tool | live authorized Workspace bytes | `environment/api.py` |

Only public DTOs cross module boundaries. Helpers like `bwrap_arguments()`, root-fd wrappers, socket probes and command output pipes stay private to Environment. Tool may not import a Linux Backend private class; Data may not implement mount validation; Prompt may not use a Sandbox capability as an execution grant.

## 8. Acceptance matrix and red lines

- Verified A cwd with ability to access authorized B folder of same principal; no access to principal C.
- Malicious root path/symlink/hardlink/mount alias cannot expose host protected DataRoot or files outside bound principal.
- v3 policy refuses v2 marker; CAPABLE_INACTIVE never executes commands.
- bwrap/Landlock/NNP/capabilities and net namespace verified through real spawn and startup probe; fail closed on missing feature.
- No host TCP/UDP/IPv4/IPv6/DNS, metadata endpoint or mounted host socket access; no inherited host connected FDs.
- Exit/timeouts/truncation truthful; process descendants reaped; no background escape; no automatic retry after uncertain effect.
- Cross-Conversation commands conflict on same principal root even if they have different CallIDs/Bindings.
- No v2 Task/Work import or extra Workspace software module.
- **Test execution follows [testing policy](testing-policy.md)**: Environment-only pytest on contract-compatible internal changes; public contract change ⇒ full pytest. The host Linux confinement gate remains independently required.

## 9. Implementation sequencing (review before code)

| Packet | Internal target | Gate |
| --- | --- | --- |
| S0 | Extract v2 security invariants and identify imports to replace | no v2 Task/Work authority transitively linked |
| S1 | Data principal/root/cwd mapping contract + Environment verified root authority | same-user cross-folder works; other-user leakage fails |
| S2 | v3 SandboxSpec + bwrap/Landlock adaptor + isolated-only network mode | no unprotected spawn; no host network |
| S3 | activation generation, operator marker, self-test and real Linux probe | ENFORCED real-spawn certification; no old marker |
| S4 | Tool run_command and bounded process results + FileAPI | no implicit shell; accurate non-zero/timeout/cancel/UNKNOWN |
| S5 | Integration acceptance and contract review | no public import escapes; module-appropriate pytest + host proof |

**Before implementation:** Resolve hard blockers for trusted principal identity, root backing/admission algorithm on large mutable principal Workspace, and target-host Linux capability. Confirm first-release resource ceilings; none are implicitly frozen by copying v2 constants.

## 10. Remaining open design items

1. Authenticated principal identity Web/CLI contract, physical root provisioning/ownership and durable Data mapping.
2. Atomic root + cwd binding across spawn when directory content mutates concurrently; whole-root admission cost and alias policy.
3. v3 operator activation workflow, kernel/bwrap/Landlock host conditions, actual host smoke certification.
4. Exact command resource limits, process death/restart cleanup, output capture streams and UI projection.
5. Physical immutable file publication outside RW root; changing working files vs existing metadata.
