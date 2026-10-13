# Default Workspace & Chat Files — v0.1

> 2026-10-13 · **초기 제품 범위는 Confirmed, 내부 스키마·수명·보호 장치는 Proposed**.
>
> 통합 문서: [contracts.md](contracts.md). 결정 기록: [decisions.md](decisions.md). v2 Phase 4A 기능을 그대로 이식하지 않고 **v3의 기본 Workspace 파일 기반부터 설계**한다.

## 1. 명시적으로 결정된 범위

1. **HIMARI 제공 기본 Workspace부터 구현**하고 파일 첨부, 채팅에서 파일 생성/수정, 파일 제공·다운로드의 기반으로 사용한다.
2. 초기 **기본 Workspace 경로에 Approval, Workspace 격리(별도 Worktree/사본), OS Sandbox를 도입하지 않는다.**
3. **Workspace는 자원이지 독립 소프트웨어 모듈이 아니다.** Data는 식별/Binding/파일 메타데이터, Environment는 파일 I/O, Tool은 모델 노출·Dispatch를 담당한다.
4. **Plan과 Note는 기존 결정대로 Data conversation-scoped Artifacts**에 원본을 둔다. 일반 파일(PDF/코드/이미지 등)은 기본 Workspace의 파일이 원본이다.
5. 초기 범위에 임의 셸 명령 `run_command` 및 장기 ManagedProcess를 **포함하지 않는 것을 권고**한다. 명령 실행을 나중에 도입해도 'Sandbox 없음'을 강제 격리 보장으로 포장하지 않는다.

**Approval/Sandbox 미도입은 '임의 호스트 파일 접근 허용'과 동의어가 아니다.** 구조화된 파일 Tool은 지정한 기본 Workspace의 상대 경로만 받아 안전한 path-resolution, 디렉터리 탈출 거부, 크기·개수 제한, 권한 있는 호출자 검사, 덮어쓰기 충돌 규칙을 적용할 수 있다. 이는 OS 격리나 사용자 승인 체계가 아니라 공개 파일 API의 입력 계약이다.

## 2. 기본 Workspace 자원 모델

초기 권고: HIMARI가 등록·제공하는 **하나의 영속 기본 Workspace**를 사용자(또는 단일 설치 인스턴스) 범위로 둔다. 여러 Conversation이 같은 Workspace를 사용할 수 있으며, 각 Conversation에는 그 Workspace를 향하는 별도 활성 Binding을 연결한다. **각 대화마다 파일 트리 사본을 생성하거나 Worktree를 만들지 않는다.** 이 기본 스코프는 아직 사용자가 확정하지 않은 권고다.

```text
Data.WorkspaceRegistry
    ws_default (kind=managed_local, persistent root)
          ▲                     ▲
    bind_conv_A             bind_conv_B
          ▲                     ▲
    Conversation A          Conversation B
          └────────────┬────────────┘
                       ▼
Environment.Inside (structured file operations only initially)
                       ▼
      One persistent Default Workspace filesystem
```

- Workspace ID는 불변 자원 식별자, Binding ID는 Conversation/Environment와의 접근 관계 식별자. 어느 ID도 bearer 권한 토큰이 아니다.
- `Default` Workspace 선택은 현재 Binding을 해석하는 도우미다. 실행 시 trusted scope에 대한 등록/활성/권한 재검사는 필요하다.
- 공유 Workspace이므로 **다른 Conversation의 파일 변경도 현재 파일 내용에 영향을 준다**. 충돌 제어는 Tool Scheduler에서 실제 Workspace ID/파일 효과 범위를 기준으로 수행한다.
- Conversation 삭제는 공유 Workspace 전체 삭제를 뜻하지 않는다. 파일의 소유/참조/GC 의미는 별도 정책에서 결정한다.

## 3. 파일 원본 및 논리적 기록

```text
Filesystem in Default Workspace (canonical bytes)
 ├── user-uploaded files
 ├── generated/edited files
 └── managed source assets (if immutable original capture is supported)

Data (canonical metadata and conversation linkage)
 ├── Workspace + Binding
 ├── FileAssetRecord (proposed)
 ├── ConversationAttachmentRef (proposed)
 └── FilePublication / ToolExecution provenance (proposed)

Plan/Note canonical data stays in Data.Artifact.
```

**PROPOSED FileAssetRecord**: `file_id`, `workspace_id`, `relative_path`, `version/revision`, `byte_size`, `sha256`, `media_type`, `created_by`, `created_at`, `visibility/owner_scope`, `status`.

**PROPOSED ConversationAttachmentRef**: `conversation_id`, `event_id`, `file_id`, `file_version`, `role` (upload/generated), `display_name`. 한 Event에 여러 첨부 가능.

원본 bytes는 파일시스템에 있고, Data의 참조/해시/경로 메타데이터는 파일 bytes가 아니다. **파일을 수정했을 때 hash와 revision은 원자적으로 갱신해야 하며, 과거 첨부가 immutable original을 보장할 것인지 결정 전이다.**

권고: 업로드 때 exact 원본을 변경 불가 관리 영역에 게시한 뒤, 필요하면 편집 가능한 작업 복사본으로 내보낸다. 이를 위해 OS Sandbox를 요구하지는 않지만 **임의 셸이 같은 OS 권한으로 관리 영역에 접근하는 경우 불변성은 OS 수준으로 보장되지 않는다**. 초기에는 arbitrary shell을 제공하지 않는 이유다. 이 방식은 저장/수정 로직 계약만 다루며 개별 Workspace의 OS-level 격리 정책이 아니다.

## 4. End-to-end 흐름

### 4.1 사용자 첨부

```text
User / Web / CLI
  → Upload ingress (bounded streaming; trusted caller identity)
  → Environment default file staging (temporary, non-published)
  → file size/type/hash verification + safe canonical destination
  → publish bytes atomically in Default Workspace
  → Data FileAssetRecord + Conversation Attachment Event reference
  → Prompt receives bounded file manifest (not unbounded file bytes)
  → model reads via structured inside/read_file if exposed
```

- 파일 전체를 UserMessage 문자열에 인라인으로 넣지 않는다. provider-native 이미지/파일 입력 지원은 향후 Provider 옵션이며 불가할 경우 Tool을 통해 안전하게 접근한다.
- 실제 Web 요청/CLI는 별도 interface 경계가 필요하다. 화면에 호스트 절대 경로나 내부 저장 경로를 공개하지 않는다.
- 파일 작업과 DB 트랜잭션은 하나의 원자 트랜잭션이 아니다. staged/unpublished/orphan 상태와 복구·GC를 정의한다.

### 4.2 채팅에서 생성·수정

```text
Assistant ToolCall inside/write_file or inside/edit_file
   → Tool: arguments, effect_profile and collision guard
   → Environment: validate scoped relative path
   → Default Workspace: write temp then atomic replace/rename
   → Data: publish FileAssetRecord version + ToolExecution provenance
   → ToolResult: file_id, version, relative link/ref, byte_size
   → UI: guarded download or file preview through file_id
```

- 초기 권고 Tool: `inside/list_files`, `inside/read_file`, `inside/write_file`, `inside/edit_file`, `inside/delete_file`, `inside/publish_file`.
- 기존 문서의 `inside/run_command`는 **향후 별도 실행 기능**. 승인·Sandbox가 없는 초기 기본 Workspace에 임의 프로세스 실행 능력을 자동으로 부여하지 않는다.
- 같은 파일 수정은 충돌 직렬화. 확인된 서로 다른 파일은 조건부 병렬. 다른 Conversation의 동시 편집도 같은 파일 충돌 규칙을 적용.
- 사용자 생성 파일도 실행 결과의 원본, Conversation의 ToolResult, UI 결과, 파일 bytes는 각각 다른 계약이다.

### 4.3 다운로드 및 삭제

- `file_id` 또는 attachment reference를 통해 승인된 Web/CLI surface가 바이트 스트림을 읽는다. raw `file://` 경로나 서버 실제 경로를 외부로 반환하지 않는다.
- Delete Conversation은 Attachment **논리 참조의 삭제**를 포함하되, 공유 Workspace 전체 삭제로 연결하지 않는다. 물리 파일 삭제는 owner/reference/retention을 확인한 GC 정책으로 결정한다.
- 사용자 제공 파일 vs 생성 파일의 retention·공유·수정 이력은 아직 결정해야 한다.

## 5. 계약 출처(Owner / Provider / Consumer / Source)

| ID | Owner | Provider → Consumer | Source of Truth | Contract / Location |
| --- | --- | --- | --- | --- |
| FILE-01 | Environment | Inside File Executor → Tool / Upload ingress | 기본 Workspace 파일시스템 | `Read/Write/InspectFile`; 문서 본절 / planned `environment/api.py` |
| FILE-02 | Data | File Metadata Port → Prompt/Tool/UI | Data FileAssetRecord + AttachmentRef | `RegisterFile/LinkAttachment/GetFileRef`; planned `data/api.py` |
| FILE-03 | Tool | Inside File Tools → Control/Provider | FILE-01/02를 통한 현재 파일 상태 | `list/read/write/edit/delete/publish`; planned `tool/api.py` |
| FILE-04 | Interface (surface, 핵심 새 모듈 아님) | Web/CLI Upload/Download → User | FILE-01 bytes + FILE-02 authorized metadata | ingress/egress Port는 구체화 전 |
| FILE-05 | Prompt | Attachment Manifest Projector → Control | DATA File refs + workspace metadata | `BuildAttachmentManifest`; planned `prompt/api.py` |

각 Port의 DTO와 실제 선언 파일은 **제안**이며, Owner와 원천 구분은 기존 모듈 계약 표준을 따른다.

## 6. 최소 안전/정합성 테스트

1. Default Workspace 등록 및 여러 Conversation Binding에서 동일 Workspace ID 보존.
2. 허용된 상대 파일 경로만 처리, traversal/symlink/broken link/별칭 경유 탈출 차단 (structured API 수준).
3. 업로드 limit·스트리밍·중단 후 partial 파일 미노출.
4. 파일 publish + 메타데이터 작성 사이 crash 재조정; 거짓 완료 Result 생성 금지.
5. User Event에 첨부 참조만 포함; 모델에게 파일 전량을 무제한 주입 금지.
6. 에이전트 생성 파일을 authorized download로 읽을 수 있음.
7. 다른 Conversation의 파일 수정 충돌 및 revision 검사.
8. Plan/Note는 Workspace 없이 Data Artifact에 남음.
9. Conversation purge가 Workspace 공유 원본을 무조건 삭제하지 않음.
10. `run_command`가 초기 파일-only surface에 노출되지 않음.

## 7. OPEN

- 첫 Default Workspace가 **한 설치 단위 공유**인지, 사용자별로 나눌지, 대화별 폴더 네임스페이스만 만들지.
- 업로드 immutable exact original/작업 파일 관계, 원본 보존·버전 모델.
- FileAssetRecord schema 및 직접 파일 변경 감지, 내부 관리 영역의 보호 수준.
- 생성 파일의 자동 publish 여부와 UI 노출 조건, 파일형식 preview/rendering.
- 크기·총량·파일 수·MIME 정책, 유효성 검증, GC, 소유권/삭제.
- `inside/run_command`의 권한·실행 모델은 별도 향후 계약. 초기 no-sandbox/no-approval을 고위험 범용 도구로 무조건 확장하지 않음.
