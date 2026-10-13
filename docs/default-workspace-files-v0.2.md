# Default Workspace, Cross-Conversation Files & Sandboxed Commands — v0.2

> 2026-10-13 · **설계 기준 업데이트 / Detailed Contract PROPOSED**
>
> **V3-032~036 명시 결정:** 기본 Workspace에서 `run_command`를 지원하고 OS Sandbox를 도입한다. **사용자별 소유 Root**에 Conversation별 디렉터리를 만들고 같은 사용자에게 허용된 다른 Conversation 폴더에도 접근한다. **Sandbox 네트워크는 기본 차단**. 초기 Approval 미도입은 유지한다. 이 문서는 [v0.1](default-workspace-files-v0.1.md)을 우선하며, Sandbox 상세는 [Sandbox Execution v0.1](sandbox-execution-v0.1.md)을 따른다.
>
> **주의:** 동일 사용자 Conversation 폴더 간 격리는 요구하지 않으나 사용자/테넌트 경계, HIMARI Data Root/호스트 파일 보호는 유지한다. 실제 Sandbox 기술·파일 보존·권한/네트워크 정책은 권고안이며 추가 검수 필요.

## 1. 핵심 결정과 범위

### Confirmed

1. HIMARI 제공 기본 Workspace를 최초 구현하여 파일 첨부, 파일 생성·수정·다운로드 및 **`inside/run_command`**의 실행 공간으로 사용.
2. `run_command`는 반드시 **OS Sandbox 안에서 실행**한다. Sandbox를 사용할 수 없을 때는 **실행을 거부하는 fail-closed** 계약을 권고(구체 기동 게이트는 Proposed).
3. 현재 사용자에게 속하는 기본 Workspace는 **Conversation별 폴더**를 가진다. 각 Conversation은 자기 폴더를 default cwd로 사용하되, **같은 사용자에게 허용된 다른 Conversation 디렉터리를 접근 가능**하게 한다.
4. v2의 Task, Worktree, Apply/Discard, Conversation별 파일 트리 복제를 재도입하지 않는다.
5. 이전 초기 기본 Workspace의 **Approval 제외는 유지**: 별도 인간 승인 요청/대기를 첫 버전의 필수 조건으로 두지 않는다. 이 선택은 더 높은 위험을 허용하므로 Sandbox·파일 액세스 정책·자원 제한·감사 기록이 중요하다.
6. **Data.Artifact의 Plan/Note 소유권은 그대로 유지**. 실제 파일 바이트·첨부 참조 및 관리 정책은 별도 파일 계약.

### Proposed first-release features

- `inside/run_command` (한 번 실행하고 결과를 반환하는 bounded synchronous command).
- `inside/list_files/read_file/write_file/edit_file/delete_file` 등 구조화된 파일 Tool.
- 채팅 파일 업로드, 생성 파일 publish 및 다운로드.
- **ManagedProcess의 장시간 프로세스 API는 첫 릴리스에서 제외 권고**: 지속 PID/출력·종료·재시작 복구는 별도 수명 계약 필요.
- 초기 Sandbox는 **사용자 간 보호 및 호스트 보호**를 목표로 하고, 같은 사용자 Conversation 간 읽기/쓰기 격리를 제공한다고 주장하지 않는다.

## 2. 파일시스템 레이아웃 (Proposed)

```text
Host (서버 실제 경로는 비공개)
  <managed-root>/<principal-id>/
      conversations/
          <opaque-conversation-id-A>/
              uploaded.pdf
              main.py
          <opaque-conversation-id-B>/
              report.md
              figures/
          <opaque-conversation-id-C>/
      shared/
          datasets/
      [internal staging/metadata bytes may be outside sandbox; OPEN]

Sandbox view (same principal only)
  /workspace/
      conversations/
          <opaque-conversation-id-A>/
          <opaque-conversation-id-B>/
          <opaque-conversation-id-C>/
      shared/

Conversation A:
  cwd = /workspace/conversations/<opaque-conversation-id-A>
```

- Conversation 폴더 경로는 **작업 정리·기본 cwd**, **보안 격리 경계가 아니다**.
- Sandbox의 실제 RW 경계는 **해당 principal에게 허용된 기본 Workspace root**. 사용자 B의 폴더를 단순 상대 경로나 mount alias로 노출하지 않는다.
- 다른 Conversation 디렉터리는 `/workspace/conversations/<other-id>/...` 등의 **명시 경로**로 읽기와 쓰기가 가능하도록 설계한다. 쓰기 기능이 필요 없을 때는 모델/사용자 선택에 따라 Read/Write 제한 정책을 추가할 수 있지만 이번 결정은 기본적인 교차 접근을 허용한다.
- `/workspace/shared/`는 공통 데이터를 정리하는 **편의 네임스페이스 제안**이며, 사용자가 명시적으로 필수 채택한 것은 아니다.
- Conversation ID는 원칙적으로 opaque identifier를 사용한다. directory reuse, 이름 충돌, 외부 경로로의 symlink/하드링크 alias, mount alias, quota는 실구현에서 차단/검증.
- 기본 Workspace를 하나의 Registry 자원(`workspace_id`)으로 기록하고 Conversation마다 `binding_id`, `working_directory`(상대 경로)을 부여하는 것이 초기 권고. 여러 Conversation 간 같은 Workspace ID가 사용됨.
- **삭제:** Conversation을 삭제해도 해당 디렉터리가 다른 Conversation에서 사용 중일 수 있다. 무조건 디렉터리 삭제 금지; tombstone/retention/reference policy는 OPEN.

## 3. Sandbox ExecutionProfile (Proposed)

```text
ToolCall inside/run_command(argv, binding_ref)
    │
    ▼
Tool Gateway: schema + concurrency + allowed invocation
    │
    ▼
Environment: read current Binding/principal/cwd; authorize
    │
    ▼
Sandbox Backend (fail-closed)
    ├── RW mount: /workspace (only authorized principal)
    ├── RO system runtime tools (minimum)
    ├── No mount: Data Root / Secrets / Other principals
    ├── no privileged elevation, bounded resources, sanitized env
    └── optional network policy (default disabled is recommended, not decided)
    │
    ▼
Bounded process: exit code/stdout/stderr + truncation metadata
    │
    ▼
ToolExecutionRecord + canonical ToolResult
```

**Sandbox는 `run_command` 및 임의 프로그램 실행에 대한 필수 실행 경계다.** 구조화된 파일 API가 신뢰된 서버 프로세스 안에서 동작하는 경우에도 path/ACL/파일 descriptor 검증이 필요하다. 구조화 API가 별도 자식 프로세스를 실행한다면 동일 Sandbox 조건이 적용된다.

- **Linux 구현 권고:** v2의 Bubblewrap mount/user/PID namespaces + Landlock 보강·NoNewPrivileges·제한된 mount view·fd-safe structured file access를 참조해 재설계한다. 기존 v2 정책은 네트워크 격리가 없고 WorkAuthority·실제 Workspace Root cwd에 결합되어 있어 그대로 옮기지 않는다. [상세 재사용 검토](sandbox-execution-v0.1.md).
- 샌드박스 실행 전에 지원/활성 상태를 검사하며, 미지원 플랫폼에서 **unsandboxed fallback 금지**. Windows 또는 기타 OS는 동일 계약을 만족하는 구현이 없으면 실행을 지원하지 않는다.
- 설치된 툴체인/라이브러리는 **필요 최소한 Read-only**로 매핑한다. 홈 디렉터리, 임의 임시 디렉터리, Docker/호스트 소켓, 인증서·SSH key, 앱 내부 DB는 자동 노출하지 않는다.
- 프로세스 환경 변수는 allowlist 사용; 호스트의 토큰/secret을 기본적으로 전달하지 않는다.
- `argv`, cwd, 자원 범위, 최대 실행 시간, 출력 상한, 프로세스 하위 트리 종료, 시스템 콜 제한 등의 구체 정책은 OPEN.
- **네트워크 기본 차단은 확정(V3-036)**이다. bwrap 네트워크 namespace 등 별도 커널 집행과 네트워크 우회 테스트가 필요하다. 향후 별도 Web Search Tool은 trusted network service 경계에서 구현하며, 이를 이유로 `run_command`에 네트워크를 허용하지 않는다.
- Sandbox 안에서 `rm -rf /workspace/conversations`를 실행할 수 있다면 **같은 사용자의 파일은 삭제될 수 있다**. Approval을 사용하지 않는 만큼 구조적 보호·버전/스냅샷·사용자 경고는 별도 결정 필요.

### Process scope and concurrency

- 일반 `run_command`는 **한 호출에서 시작해 완료된 후 결과를 반환**하는 bounded process가 우선. 프로세스가 호출 뒤에도 지속되는 `start_process`는 별도 계약.
- 하나의 사용자 root를 공유하고 `run_command`의 파일 효과 범위가 불명확하므로, 기본 Scheduler에서는 **같은 Workspace의 arbitrary `run_command`와 겹치는 파일 작업을 직렬화**한다. 서로 다른 Conversation도 충돌 후보.
- 서로 다른 검증된 파일에만 쓰는 구조화 API는 필요하다면 병렬 허용 가능. 모델이 보고한 수정 범위만으로 명령 실행의 안전한 병렬성을 인정하지 않는다.

## 4. File Asset와 Attachment는 working files와 구분 (Proposed)

```text
Mutable Working Files (canonical current bytes)
  Default Workspace /workspace/conversations/<id>/...
          │
          ├── command can edit/delete bytes
          └── explicit publish/attach operation
                      ▼
Immutable published attachment/version (recommended)
    Data-managed blob or separate protected store, NOT writable by run_command
                      +
    FileAssetRecord / ConversationAttachmentRef metadata
                      ▼
            Web / CLI download, Prompt manifest
```

**핵심:** `run_command`가 파일을 수정할 수 있으므로 파일 경로 + 과거 sha256 metadata만으로 'exact uploaded attachment'의 동일성을 영원히 주장할 수 없다. 파일의 **현재 작업 사본**과 **한 시점에 게시된 immutable 첨부 버전**을 별도로 식별하는 전략을 권고한다.

- 파일 원본이 어떤 의미인지 나눈다: **작업 파일의 진실은 Workspace filesystem**. 업로드 시점/게시된 파일 버전의 진실은 **불변 snapshot bytes**(물리 저장 방법 미정).
- 업로드 사용자 파일은 workspace에 사본을 노출하여 `run_command`가 사용하게 한다. 업로드의 원본 바이트를 보존할지/어디에 둘지는 다음 결정.
- 생성 파일 publish시 verified bytes에서 immutable version + checksum + file_id/revision을 생성해 UI가 race-free 다운로드 가능하게 하는 것이 권고.
- DB + filesystem을 하나의 트랜잭션으로 묶을 수 없으므로 staging → publish → metadata association 및 orphan cleanup/refcount GC 계약 필요.
- **Plan/Note Artifact는 Data 영역**, 일반 업로드/생성 파일은 별도 FileAsset/Attachment 계약. 이름만 같다고 Plan/Note JSON을 파일로 자동 동기화하지 않는다.

## 5. Source of Truth / Contracts

| ID | Owner | Provider → Consumer | Source of Truth | Contract Location |
| --- | --- | --- | --- | --- |
| FILE-01 | Environment | Inside File API → Tool / Upload | current working bytes: default Workspace | 본 문서 §4; planned `environment/api.py` |
| FILE-02 | Data | File metadata Port → Prompt/UI/Tool | Data FileAssetRecord + attachment association | 본 문서 §4; planned `data/api.py` |
| FILE-03 | Tool | Structured file tools → Control/Provider | FILE-01 + FILE-02 | 본 문서 §3–4; planned `tool/api.py` |
| FILE-04 | Interface | Web/CLI upload/download → User/Control | approved immutable published bytes + Data metadata (proposed) | 본 문서 §4; planned interfaces |
| FILE-05 | Prompt | AttachmentManifest → Control | Data references + included bytes/version | 본 문서 §4; planned `prompt/api.py` |
| **ENV-04** | Environment | SandboxCommandExecutor → Tool | validated Workspace binding + Sandbox profile + actual process | 본 문서 §3; planned `environment/api.py` |
| **DATA-11** | Data | ConversationWorkspacePath Port → Environment/Prompt | Data conversation-to-workspace relative cwd assignment | 본 문서 §2; planned `data/api.py` |

These new Port identifiers extend [contracts.md](contracts.md); they do not imply that code exists.

## 6. Minimal acceptance gates

1. Conversation A and B owned by the same user have distinct default working directories and the **same authorized Workspace root**.
2. `pwd` for A shows `/workspace/conversations/A`; explicit cross-folder access to `B` works (read, and authorized write).
3. Another user's Conversation directory is **not present/readable/writable** in A's Sandbox.
4. Data Root/host secrets/system protected roots unavailable; bypass attempts via symlink/hardlink/mount alias fail closed.
5. Sandbox unavailable or activation self-test failed → `inside/run_command` **not executed** and explicit ToolResult failure.
6. `run_command` returns bounded exit/status/output/error/timeout; unknown result is never reported success.
7. Named `file_id` download returns published version bytes, not the latest mutable path contents.
8. Cross-conversation file mutations are serialized against unsafe command execution, even with distinct `binding_id`.
9. Conversation delete doesn't remove bytes referenced by another Conversation without liveness proof.
10. No outside scope shell access even when the model claims an arbitrary host path.

## 7. OPEN

- Shared root scope: **user/principal별 기본 Root는 확정(V3-035)**. Web/CLI에서 신뢰된 principal을 획득·검증하고 물리 root에 매핑하는 방식은 미결정.
- Whether default cross-Conversation access includes all owned directories or only explicitly enabled folders. User wants access; permission filtering is still required for multi-user installations.
- Sandbox 구현/활성화·실제 호스트 기동 검증, 프로세스 출력/시간/메모리 정책. **네트워크 차단 여부는 확정**, 방법과 DNS/loopback/Unix socket 예외 검증은 미결정.
- How/where to store immutable published file versions outside the writable Sandbox root.
- Filesystem operations' file-size and edit/version bounds; how to reconcile out-of-band shell writes.
- Conversation deletion, directory retention, cross-conversation references, shared/ retention policy.
- ManagedProcess later, explicit Approval later (not automatically introduced).
