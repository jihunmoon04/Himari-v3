# HIMARI v3 — 통합 모듈 계약 기준서

> 작성: 2026-10-13 · 문서 버전: **Contract Baseline v0.1** · 구현 상태: **설계 단계 / 코드 없음**
>
> **이 문서는 지금까지의 모듈 계약을 찾고 비교하는 단일 진입점이다.** 이미 합의한 원칙은 **CONFIRMED**, 그 원칙에 따라 작성한 포트/DTO/함수 이름은 **PROPOSED**, 후속 선택이 필요한 곳은 **OPEN**으로 표시한다.
>
> 상세한 설계 근거는 [decisions.md](decisions.md), [data-contract-v0.1.md](data-contract-v0.1.md), [current-state-contract-v0.1.md](current-state-contract-v0.1.md), [prompt-composition.md](prompt-composition.md), [data-and-tools.md](data-and-tools.md), [open-questions.md](open-questions.md)에 남겨 둔다. 내용이 충돌하면 명시적으로 사용자 승인을 받은 **Confirmed Decision**을 우선하고, 충돌을 이 문서에 드러내 수정한다.

## 1. 목적과 설계 원칙

**목표:** 모듈이 자신이 소유한 공개 계약을 유지하는 한 구현, 알고리즘, 저장 방식, Provider SDK, Environment 백엔드를 교체하더라도 소비 모듈의 내부 구현을 수정할 필요가 없어야 한다.

- **6개 핵심 모듈:** Control, Data, Prompt, Provider, Tool, Environment.
- **Workspace는 독립 자원**이지만 전용 코드 모듈을 만들지 않는다. Registry/Binding은 Data가 영속화하고, 연결·실행은 Environment가 담당한다.
- Memory·Plan·Note는 Tool의 구현이며 새로운 최상위 실행 제어 모듈이 아니다.
- **Run = LLM 호출 1회**. Control이 Run의 연쇄를 다루며, Provider·Tool·Data는 Run 반복의 정책을 소유하지 않는다.
- 공개 Contract 유지와 모델 출력의 동일성은 다르다. 현재 한 프로세스의 **논리적 구현 격리**를 목표로 하고 물리 프로세스 장애 격리를 초기 필수 조건으로 만들지 않는다.
- Data의 private SQL/Repository, Provider-specific wire 타입, Environment OS 핸들은 공개 계약 밖으로 누출하지 않는다.
- 공용 거대 계약 패키지나 런타임 Contract Registry를 별도로 만들지 않는다. **계약은 Owner 모듈의 공개 Port가 정의**하고 Bootstrap이 구현체를 주입한다.

## 2. 계약 문서 표준 — **CONFIRMED (이번 결정)**

모든 공개 계약에는 최소한 다음 정보를 기록한다.

| 항목 | 정의 |
| --- | --- |
| **Contract ID / Version / Status** | 문서·테스트에서 식별하는 계약 ID, 호환성 버전, CONFIRMED/PROPOSED/OPEN |
| **Owner** | 공개 인터페이스와 의미론적 호환성을 정의·유지할 책임 모듈 |
| **Provider (Implementer)** | 해당 인터페이스를 실제 구현하고 응답을 제공하는 모듈/어댑터 |
| **Consumer** | 해당 공개 인터페이스를 호출하거나 반환 값을 소비하는 모듈 |
| **Source of Truth** | 반환 데이터의 **authoritative 원천**, 즉 실제 값을 소유/확정하는 곳. 파생값이면 원천들의 목록 |
| **Contract Location** | 계약 선언 위치. **현재는 문서 위치**, 앞으로의 코드 선언 위치는 `planned`로 별도 표기 |
| **Input / Output** | provider-neutral 불변 DTO·인자·반환 결과 및 지원 범위 |
| **Guarantees / Errors / Effects** | 불변식, 실패/취소/시간순서·원자성, 실행 부작용 여부 |
| **Dependencies / Tests** | 사용하는 하위 공개 계약, 대체 구현 및 계약 검증 범위 |

**구분 규칙**

- **Owner ≠ Provider**일 수 있다. 예: Provider 모듈이 정의한 `InvokeModel` 계약을 OpenAI Adapter가 구현한다.
- **Provider ≠ Source of Truth**일 수 있다. 예: Prompt가 현재 상태를 제공하지만 Workspace Binding의 authoritative 원본은 Data에 있다.
- **Consumer는 SQL 테이블·내부 구현 대신 공개 Port만 참조**한다.
- **Contract Location**은 지금 존재하지 않는 Python 파일을 실제 구현된 것처럼 기록하지 않는다.
- 데이터 흐름(응답 반환)은 보통 호출 방향과 반대다. `Control → Prompt.compose()`는 호출, `Prompt → Control`은 반환 데이터의 흐름이다.

아래 경로 `src/himari/<module>/api.py`는 **권고되는 장래 코드 선언 위치 예시**다. 실제 파일은 아직 생성되지 않았다. 모든 계약의 **현재 선언 위치**는 이 문서의 4절·5절이다. 인터페이스의 실제 경로/파일 분할은 OPEN이다.

### 표준 계약 템플릿 (문서 구조; YAML 파일 생성 의무 없음)

```yaml
contract_id: DATA.READ_CURRENT_STATE_BASIS
version: draft-v0.1
status: PROPOSED
owner: Data
provider: Data
consumers: [Prompt]
source_of_truth:
  - Data.EnvironmentActivation
  - Data.WorkspaceBinding
  - Data.WorkspaceRegistry
  - Data.ConversationSettings
  - Data.Artifact
contract_location:
  document: docs/contracts.md
  planned_code: src/himari/data/api.py
input: [TrustedConversationScope]
output: PersistentStateBasis
guarantees: [permission_filtered, coherent_read, immutable_projection]
errors: [not_authorized, missing_conversation, read_failure]
effects: none
dependencies: []
tests: [cross_conversation_isolation, version_consistency]
```

위 DTO 이름·오류 이름·파일 경로는 **예시(PROPOSED)**이며 암묵적인 확정 스키마가 아니다.

## 3. 전체 호출 관계 / 데이터 출처 지도

### 3.1 호출 관계 (요청 방향)

```text
Bootstrap / Composition Root
     │  inject public ports (공개 계약으로만 연결)
     ▼
Control
 ├── Prompt.compose  ─────────────┬── Data.read_*      (exact history / snapshot / state / artifacts)
 │                                ├── Tool.catalog      (허용된 도구 정의)
 │                                └── Environment.observe (필요 시만; live connectivity)
 ├── Data.prepare_run / record_*   (원본 호출 및 실행 기록)
 ├── Provider.invoke               (LLM 호출 정확히 1회)
 └── Tool.execute_group ─────────┬── Internal Tools ───── Data public API
                                  └── Environment Tools ─ Environment.execute ─ Workspace

Provider Adapter ─── Provider-specific API
Environment ─────── Data.read_binding / Registry  (실행 시 다시 검증)
```

이는 **동기 함수 직접 호출을 확정한 도식이 아니다.** 순서와 의존성 계약만 나타낸다.

### 3.2 데이터별 Source of Truth

| 데이터 / 개념 | Authoritative Source of Truth | 가공·소비·표시 |
| --- | --- | --- |
| User/Assistant의 정규화된 exact Conversation Event | **Data.ConversationEvents** | Prompt 재구성, UI projection |
| LLM의 실제 request/response 원본 | **Data.RunRecords** (Provider I/O를 수신해 영속화한 값) | Provider Adapter 생성·해석, 감사/복구 |
| ToolCall 인자, 실제 실행 사실과 실행 결과 | **Data.ToolExecutionRecords** | Tool 실행 제공, Prompt history 복원 |
| 현재 Plan/Note 내용 | **Data.Artifacts** (conversation-scoped) | Internal Tool 작성, Prompt CurrentState, UI |
| 가장 최근 전체 가공 프롬프트 | **Data.LatestPromptSnapshot** (대화별 1개) | Prompt 구성, Provider 전송 전 준비 |
| Workspace 정체성/등록 | **Data.WorkspaceRegistry** | Environment 연결, Prompt 노출용 메타데이터 |
| Environment 활성화/Binding/기본 선택 | **Data.Activation/Binding/Settings** | Prompt의 CurrentState 투영, 실행 시 Environment 확인 |
| 현재 연결·가용성 관측 | **Environment의 실제 관측 결과** (시점이 있는 단기 사실) | Prompt에 표시; 저장된 Binding active와 구분 |
| CurrentStateProjection | **파생값 — Data 운영 상태 + Artifacts + 선택적 Environment 관측** | Prompt 조립. **새 authoritative 저장소 아님** |
| Tool 정의/허용된 호출 표면 | **Tool Registry 및 Environment가 제공한 capability + 실제 정책** | Prompt 노출, Provider tool schema 변환 |
| 모델 호출 옵션/Reasoning effort | **Data 설정 및 호출자 설정의 명시적 해석 결과** | Prompt → Provider; 지원 기능은 Provider가 검증 |
| Opaque Provider continuation | **Provider가 실제 반환한 자료를 Data에 영속화한 값** (이용 가능 범위 한정) | Provider Adapter의 native replay |
| Tool 실행 승인·권한 | **Environment가 실행 시 검증하는 현재 정책과 Binding** | Prompt 안의 이름/ID/상태는 **권한 증명이 아님** |

## 4. 계약 인벤토리 — 공개 Port의 Owner / Provider / Consumer / 원천

모든 `API name`은 **개념적 명칭(PROPOSED)**이다. Owner/Consumer 관계 중 **확정된 모듈 책임은 CONFIRMED**, 세부 호출 결합은 PROPOSED이다.

| ID | Contract / Owner | Provider → Consumer | Source of Truth / 입력 | 반환 또는 효과 | 상태 |
| --- | --- | --- | --- | --- | --- |
| **CTRL-01** | DriveConversation / **Control** | Control → 외부 UI/CLI 진입점 | 사용자 입력 + 신뢰된 conversation scope | Run 실행, Tool 실행·종료 제어 | 방향 확정, Port 미정 |
| **CTRL-02** | ResumeConversation / **Control** | Control → 외부 UI/CLI | Data 실행 상태·신규 사용자 응답 | 중단·question 이후 재개 | PROPOSED |
| **DATA-01** | AppendReadEvents / **Data** | Data → Control, Prompt | Data.ConversationEvents | immutable event + ordered sequence | 원칙 확정, API 미정 |
| **DATA-02** | PrepareRecordRun / **Data** | Data → Control | Data.RunRecords | raw request 영속화, raw response/실패 기록 | 원칙 확정, API 미정 |
| **DATA-03** | RecordToolExecution / **Data** | Data → Control, Tool, Prompt | Data.ToolExecutionRecords | run_id/call_id별 실행 상태·원본/모델 결과 | 원칙 확정, API 미정 |
| **DATA-04** | ReadWriteArtifact / **Data** | Data → Internal Tool, Prompt | Data.Artifacts | Plan/Note current content+revision | 원본 위치 확정, API 미정 |
| **DATA-05** | ReadReplacePromptSnapshot / **Data** | Data → Prompt, Control | Data.LatestPromptSnapshot | 최신 전체 가공 Prompt 1개 | 원칙 확정, API 미정 |
| **DATA-06** | ReadCurrentStateBasis / **Data** | Data → Prompt | Data Registry+Activation+Binding+Settings+Artifacts | 일관된 PersistentStateBasis | 원본 소유권 확정, DTO 제안 |
| **DATA-07** | ReadWorkspaceBinding / **Data** | Data → Environment, Tool | Data.WorkspaceRegistry/Binding | Binding/등록·활성 관계 | 방향 확정, API 미정 |
| **DATA-08** | CommitInternalMutation / **Data** | Data → Internal Tool | Artifact + ToolExecution + optional ConversationEvent | revision CAS 및 원자 커밋 | PROPOSED |
| **PROMPT-01** | ComposeModelInput / **Prompt** | Prompt → Control | DATA-01/03/05/06 + Tool catalog | provider-neutral model input+prompt basis | 순서 확정, DTO 미정 |
| **PROMPT-02** | ProjectCurrentState / **Prompt** | Prompt → Prompt composition | DATA-06 + optional ENV-01 | CurrentStateProjection | PROPOSED |
| **PROMPT-03** | ReplayHistory / **Prompt** | Prompt → Prompt composition | exact Events + non-Conversation Execution Records | 완전한 정규화 Tool 그룹, coverage | 분담 확정, 포맷 미정 |
| **PROMPT-04** | CompactAndSnapshot / **Prompt** | Prompt → Control/유지관리 경로 | complete history / DATA-05 | 압축된 부분·exact tail·Snapshot 교체 요청 | 방향 확정, 알고리즘 미정 |
| **PROVIDER-01** | InvokeModel / **Provider** | 각 Provider Adapter → Control | PROMPT-01 + 호출 모델 설정 | 실제 LLM 호출 1회, raw response + normalized output | Run 원칙 확정, Port 미정 |
| **PROVIDER-02** | RenderNativeRequest / **Provider** | 각 Provider Adapter → Provider.invoke 경계 | Prompt 중립 입력 + Tool 표면 + continuation | provider-native messages/tools/role/reasoning | 변환 책임 확정, 형식 미정 |
| **TOOL-01** | CatalogTools / **Tool** | Tool Registry → Prompt, Control | Tool Registry + Environment capabilities/Policy | 모델 노출 가능한 도구 정의 | 방향 확정, API 미정 |
| **TOOL-02** | DispatchToolCalls / **Tool** | Tool Dispatcher → Control | Run의 호출 그룹 + 각 Tool 정의/Binding | `call_id`에 연결된 Model ToolResult | 방향 확정, API 미정 |
| **TOOL-03** | ScheduleConflicts / **Tool** | Tool Scheduler → Tool Dispatcher | verified resource identity & effect metadata | 독립 실행 병렬·충돌 직렬화 | 병렬 원칙 확정, 정책 미정 |
| **ENV-01** | ObserveBindings / **Environment** | Environment 구현 → Prompt | 실제 연결·가용성 관측, Data Binding | online/offline/unknown 등 시점 표시 | PROPOSED |
| **ENV-02** | AuthorizeExecute / **Environment** | Inside 등 Environment 구현 → Tool Dispatcher | Data Binding + 현재 실행 정책 + Workspace | 실행 결과/실패/거부 | 방향 확정, API·Sandbox 미정 |
| **ENV-03** | ManageActivation / **Environment** | Environment/Control 구현 → 외부 활성화 요청자 | Data Activation/Binding (영속 원본) | 활성화·연결 관계의 생성/변경/비활성화 | 책임 세부 OPEN |
| **CTRL-03** | RequestPause / **Control** | Control → UI/CLI | Data.ConversationControlState / active Run | durable pause intent, eventual paused boundary | PROPOSED |
| **CTRL-04** | ResumeExecution / **Control** | Control → UI/CLI | ControlState + completed/partial Run and Tool records | continue pending ToolCall or next Run, without blind replay | PROPOSED |
| **DATA-09** | ReadWriteControlState / **Data** | Data → Control | Data control cursor/phase/revision | serialized state transition / resumption checkpoint | PROPOSED |
| **DATA-10** | ClaimControlLease / **Data** | Data → Control | Data owner generation / fence | concurrent resume exclusion | PROPOSED |
| **TOOL-04** | ResumePendingGroup / **Tool** | Tool → Control | Data ToolExecutionRecords | run only unstarted pending calls, preserve original call IDs | PROPOSED |
| **FILE-01** | FileOperations / **Environment** | Inside → Tool, file ingress | Default Workspace real file bytes | bounded read/write/inspect with relative-path validation | initial scope CONFIRMED, API PROPOSED |
| **FILE-02** | FileMetadata / **Data** | Data → Tool, UI, Prompt | Data FileAssetRecord and AttachmentRefs | durable file_id/path/version/digest and conversation linkage | PROPOSED |
| **FILE-03** | StructuredFileTools / **Tool** | Inside File Tool → Control/Provider | FILE-01 + FILE-02 | create/edit/list/read/delete/publish tool results | PROPOSED |
| **FILE-04** | UploadDownload / **Interface** | Web/CLI surface → User/Control | FILE-01 bytes + FILE-02 authorized metadata | upload and download bytes without host path disclosure | PROPOSED |
| **FILE-05** | AttachmentManifest / **Prompt** | Prompt → Control | Data attachment references | model-visible bounded manifest, no unbounded bytes | PROPOSED |
| **ENV-04** | SandboxedCommandExecution / **Environment** | Inside Sandbox Backend → Tool | authorized principal root + current Workspace Binding + execution policy | bounded command exit, output, failure; **no unsandboxed fallback** | sandbox requirement CONFIRMED, DTO PROPOSED |
| **DATA-11** | ResolveConversationWorkingDirectory / **Data** | Data → Environment, Prompt | Data Conversation ↔ user Workspace mapping | per-conversation default cwd, user-authorized cross-folder scope | directory behavior CONFIRMED, mapping PROPOSED |
| **TOOL-05** | RunCommandTool / **Tool** | Tool registration/dispatch → Control/Provider | ENV-04 execution result, DATA-11 cwd mapping | command request, bounded canonical ToolResult | initial support CONFIRMED, schema PROPOSED |
| **ENV-05** | SandboxCapabilityActivation / **Environment** | Sandbox validation/self-test → Environment startup/Tool | host capability + validated sandbox policy generation | enforcement status incl network-deny; refusal if unavailable | network deny CONFIRMED, implementation PROPOSED |
| **TOOL-06** | WebSearch (future) / **Tool** | trusted Web Search adapter → Control/Prompt | external retrieved content, explicitly untrusted | bounded Web Search observations without Sandbox egress grant | OPTIONAL FUTURE |
| **INTERNAL-01** | MemoryTool / **Tool** | Internal Memory Tool → Tool Dispatcher | Data Memory 저장/검색 계약 | 모델용 검색 결과, Conversation 기록 | 방향 확정, 기능 미정 |
| **INTERNAL-02** | PlanNoteTool / **Tool** | Internal Plan/Note Tool → Tool Dispatcher | Data conversation-scoped Artifacts | 간결한 complete 결과 + Artifact 갱신 | 저장 위치 확정, 수정 API 미정 |

**Contract Location (예정, 아직 없는 코드):**

| Owner | 현재 계약 문서 | 장래 코드 선언 위치 (예시, PROPOSED) |
| --- | --- | --- |
| Control | 본 문서 §4·§5, [data-contract-v0.1](data-contract-v0.1.md) | `src/himari/control/api.py` |
| Data | 본 문서 §4·§6, [data-contract-v0.1](data-contract-v0.1.md) | `src/himari/data/api.py` |
| Prompt | 본 문서 §4·§7, [prompt-composition](prompt-composition.md) | `src/himari/prompt/api.py` |
| Provider | 본 문서 §4·§8, [prompt-composition](prompt-composition.md) | `src/himari/provider/api.py` |
| Tool | 본 문서 §4·§9, [data-and-tools](data-and-tools.md) | `src/himari/tool/api.py` |
| Environment | 본 문서 §4·§10, [current-state-contract-v0.1](current-state-contract-v0.1.md) | `src/himari/environment/api.py` |
| Bootstrap | 본 문서 §11 | `src/himari/bootstrap/` (구체 경로 미정) |

## 5. Control Contract

**Owner:** Control · **Provider:** Control 구현 · **Consumers:** UI/CLI·애플리케이션 진입점 · **Data Sources:** DATA-01/02/03, PROMPT-01, PROVIDER-01, TOOL-02 · **Location:** 본 문서 §5 / planned `control/api.py`.

**CONFIRMED**

- 동일 Conversation에서는 **Control 실행 흐름과 Run을 직렬화**한다. 다른 Conversation은 병렬 가능하며, 하나의 Run 결과로 발생한 독립 ToolCalls는 병렬 실행할 수 있다.
- Run은 모델 호출 **1회**. final response 또는 question에서 반복 종료/사용자에게 제어 반환.
- Tool 실패는 **ToolResult로 기록해 다음 모델 판단에 전달**한다. Tool 의미를 가정한 Runtime의 숨은 자동 복구는 기본 정책이 아니다.
- Run trigger는 이미 존재하는 Conversation Event 또는 대응 Tool 실행 기록의 참조이며 원본을 복사/이동하지 않는다.

**PROPOSED**

- `drive(scope, initiating_refs)`는 단일 Conversation 흐름 권한을 확보 → Prompt 구성 → raw request 저장 → Provider 1회 호출 → raw response 저장 → 정규화/Tool 분기 순서.
- 하나의 Run에서 여러 ToolCalls를 받으면 `call_id`별 모든 결과/실패/거부/취소가 종결 상태가 된 **완전한 호출 그룹**을 다음 Run의 근거로 사용한다.
- Resume는 저장된 원본 실행 상태에서 복원하되, 네트워크/외부 Tool의 실행 여부가 불명확한 상태를 임의로 재실행하지 않는다.

**OPEN:** question과 혼합 ToolCall 허용 여부, 출력 정규화 우선순위, lease/queue/timeout, cancel, 재개, in-flight tool group 배리어·동시 사용자 입력.

## 6. Data Contract

**Owner/Provider:** Data · **Consumers:** Control, Prompt, Internal Tool, Environment (공개 Port만) · **Source of Truth:** Data에 영속된 각 record/registry · **Location:** 본 문서 §6 + [data-contract-v0.1](data-contract-v0.1.md) / planned `data/api.py`.

### 6.1 논리 모델

| 객체 | 원본/불변 규칙 | 소비자 |
| --- | --- | --- |
| `ConversationEvent` | per-conversation monotonic sequence, kind별 immutable canonical payload. Tool 이벤트는 기록 정책에 따라 선택 | Prompt, Control, UI |
| `RunRecord` | **각 Run의 실제 Provider request/수신한 response raw 전체**. Provider 실패/미수신 응답은 상태로 기록 | Control, Prompt 복구·감사 |
| `ToolExecutionRecord` | run_id, call_id, ordinal, 호출 인자, 실행 결과(실제/모델용), status 및 Artifact provenance | Control, Tool, Prompt |
| `PromptSnapshot` | **마지막으로 구성된 전체 가공 Prompt 1개** (stable/history/current state + coverage). 이전 Snapshot 복제 보존 없음 | Prompt |
| `Artifact` | **Plan/Note 원본은 Conversation-scoped Data Artifact**. Revision/Schema 등의 세부 포맷은 제안 | Internal Tool, Prompt, UI |
| `OperationalState` | Workspace Registry, EnvironmentActivation, WorkspaceBinding, default-selection settings. Artifact와 구분 | Environment, Prompt |

**CONFIRMED**: Execution / Conversation / Artifact 분리, exact 원본 보존, Snapshot 최신 1개, Plan/Note 원본은 Data, 활성 Workspace/Environment는 운영 상태.

**PROPOSED**: `append_event/read_events`, `prepare_run/record_run_outcome`, `commit_model_output`, `record_tool_outcome`, `read_current_state_basis`, `get/replace_latest_snapshot`, `read/write_artifact`, `read_binding`.

**원자성 권고:**

1. Provider 요청(raw)을 **송신 전** Data에 보존.
2. 실제 응답을 받으면 **정규화 이전에 raw response** 저장.
3. Assistant ToolCall 그룹과 각 pending ToolExecution을 누락 없는 관계로 연결.
4. Internal Tool에서 Artifact 변경 + Tool 완료 결과 + 필요한 Conversation Event를 **하나의 Data 트랜잭션**으로 저장; revision CAS와 call_id 멱등성 고려.
5. 외부 효과가 있는 Tool은 DB 트랜잭션으로 파일 변경을 되돌릴 수 없음. 실행 여부가 불명확하면 **UNKNOWN**, 임의 재실행 금지.
6. Conversation 밖에 저장된 ToolResult를 다음 Prompt에 쓸 때는 `call_id`·원래 그룹·순서·완결성 검증. 불완전 그룹 재생 거부.

**OPEN:** 실제 필드/테이블, Provider raw 직렬화와 비밀/스트리밍 정책, Run 상태 전이, 원자적 commit API 형태, snapshot cursor/coverage, Artifact Revision/삭제 이력.

### 6.2 CurrentStateBasis

**DATA-06 (PROPOSED)**: `read_current_state_basis(trusted_scope) -> PersistedStateBasis`.

- **Owner/Provider:** Data. **Consumer:** Prompt (또는 공개 Data Port를 이용하는 상위 조립자).
- **Source of Truth:** Activation/Binding/Default Selection/Registry/Artifact. **별도 CurrentState 테이블은 원본이 아님**.
- **Output guarantee:** 허용된 Conversation/Environment 범위만 표시, single coherent Data read, revision/version 정보를 함께 반환. 연결 online 여부는 Data의 Binding active에서 추론하지 않는다.
- **Error examples (미확정 이름):** unauthorized scope, missing conversation, inconsistent state/read failure.

## 7. Prompt Contract

**Owner/Provider:** Prompt · **Consumer:** Control · **Sources:** DATA-01/03/05/06, TOOL-01, optionally ENV-01 · **Location:** 본 문서 §7 + [prompt-composition](prompt-composition.md) / planned `prompt/api.py`.

**CONFIRMED — 순서**

```text
[ Stable Prefix: instructions + stable tool definitions ]
[ Conversation: compressed old context + exact chronological history ]
[ Current State: activated environments + bindings + Plan/Note + optional observation ]

Provider model/reasoning settings & opaque continuation = message tail text가 아닌 전용 API 필드
```

- Trigger는 원본 Event 또는 그룹의 **참조**. 다음 Run에서 원본을 이전 위치로 이동하거나 중복 삽입하지 않는다.
- ToolCall/ToolResult는 **완전한 네이티브 그룹**으로 복원. Context에 없는 Tool 결과가 필요하면 Prompt가 Execution Records에서 선택·검증하고 Provider가 native 직렬화한다.
- Previous Snapshot은 가공된 문맥 캐시일 뿐이다. 다음 Run의 CurrentState는 **권한 있는 최신 원본**에서 다시 읽는다.
- 캐시를 위한 안정적인 Prefix 재사용은 우선하되, **잘못된 Provider-native Tool 프로토콜**로 요청을 보내거나 변경된 권한/State를 숨기는 일은 허용하지 않는다.

**PROPOSED — CurrentStateProjection**

| 반환 데이터 | Producer | Source of Truth |
| --- | --- | --- |
| 활성 Environment Instance | Prompt가 포맷팅 | Data Activation |
| 기본 Workspace 및 Binding 표시 | Prompt가 포맷팅 | Data Binding + default-selection + Registry |
| Plan/Note 선택된 상태 | Prompt가 포맷팅 | Data Artifact |
| 연결 상태 관측값 | Prompt가 결합 | Environment observation; unknown 허용 |
| StateBasis revisions | Data가 발급, Prompt가 전달 | Data read versions |

Prompt가 연결 상태를 임의로 `online`으로 채우지 않는다. CurrentStateProjection은 immutable view이고 실행 권한의 authoritative 원본이 아니다. Provider가 이를 허용하는 role/블록으로 보내지 못하면 **요청을 거부**한다.

**OPEN:** state 읽기 버전 포맷 및 prepare_run 검증, current-state renderer, 모델별 도구 schema prefix 안정화, 압축/예산, 일부 Artifact의 포함 범위 및 trust role.

## 8. Provider Contract

**Owner:** Provider · **Provider:** OpenAI/기타 Adapter · **Consumers:** Control(호출), Prompt(지원 capability 계약 소비) · **Sources:** PROMPT-01의 중립 입력, 현재 모델 설정, 실제 외부 Provider API 결과 · **Location:** 본 문서 §8 / planned `provider/api.py`.

**CONFIRMED**

- Run의 모델 호출은 **정확히 1회**이며, Provider의 암묵적 재시도가 호출 횟수를 숨겨서는 안 된다.
- Adapter가 Provider native 형식, Tool schema/role, function call/result adjacency, reasoning/continuation 표현을 담당한다.
- reasoning **설정**, reported token **usage**, opaque **continuation**은 서로 다른 데이터. 비공개 reasoning 원문을 얻는다고 가정하지 않는다.
- Run별 실제 요청·응답 원본은 Data에 저장; 정규화 응답은 원본을 대체하지 않는다.

**PROPOSED**: `invoke(model_input, config, compatible_continuation?) -> ProviderInvocationResult` (raw + normalized + usage + continuation metadata), `describe_capabilities(model)`.

**OPEN:** 실제 API별 Provider 캐싱/continuation, 스트리밍 및 원본 chunk 저장 방식, model option 미지원시 거부·변환 규칙, 에러 taxonomy.

## 9. Tool Contract

**Owner/Provider:** Tool 모듈(Dispatcher, Scheduler) 및 등록된 Tool 구현 · **Consumers:** Control(실행), Prompt(정의), Provider(스키마 변환) · **Source of Truth:** Tool Registry/허용 정책, Run ToolCall, 실제 Environment 또는 Internal Tool 결과 · **Location:** 본 문서 §9 / planned `tool/api.py`.

**CONFIRMED**

- 복수 ToolCall은 **독립성 및 자원 충돌을 검증할 수 있으면 병렬** 실행.
- Tool 실패는 결과에 보존되어 다음 모델 판단에 제공.
- Workspace가 필요한 Tool과 필요 없는 Tool을 지원. Memory는 Data API를 통해 접근 가능하며 Outside/Inside 강제 경유 불필요.
- Model-visible ToolResult / Conversation에 기록하는 내용 / UI 표시 / Artifact 원본은 서로 다른 계약.
- 독립 ToolCall은 `call_id`에 연결하며 원래 호출 순서와 실제 완료 순서를 구분.

**PROPOSED**

| Tool 정의 속성 | 이유 |
| --- | --- |
| `tool_id / input_schema / output_schema` | 노출 및 인자/응답 검증 |
| `execution_kind` = Internal / Environment | 실제 실행 Port의 선택 |
| `workspace_requirement` = None / Required / Default | 인자 또는 유효한 기본 Binding 해석 |
| `effect_profile` = Read / Scoped Write / Unknown | 동시 실행의 검증 가능한 충돌 판단 |
| `conversation_record` / `presentation` | 원본 기록/모델 결과/UI 표시 분리 |

- 같은 Workspace의 범위 불명 `run_command`는 기본 직렬 후보; 확인된 서로 다른 파일 범위 수정은 공유 부작용이 없으면 병렬 후보.
- **Binding ID가 달라도 실제 Workspace·공유 자원이 같으면 충돌** 가능. 다른 Conversation도 Scheduler 충돌 영역에 포함.
- 모델이 주장한 수정 파일명/문자열 동일 여부는 안전성 증명이 아니다.
- Plan/Note 변경 + Tool 완료 결과 원자 커밋은 DATA-08 복합 API로 실현하는 방향.

**OPEN:** 동시성 limit/locking/resource alias, Internal/Environment dispatcher 세부 구현, 전체 Tool group 완료 배리어, Connector 확장 유형.

## 10. Environment Contract (Workspace 자원 접근)

**Owner:** Environment · **Providers:** Inside 등 Environment 구현 · **Consumers:** Tool(실행), Prompt(관측) · **Sources:** Data Registry/Binding, 실제 연결/실행 대상, 실행 정책 · **Location:** 본 문서 §10 + [current-state-contract-v0.1](current-state-contract-v0.1.md) + [sandbox-execution-v0.1](sandbox-execution-v0.1.md) / planned `environment/api.py`.

**CONFIRMED**

- **Workspace는 독립 자원**이며 Environment 종료/Binding 해제가 Workspace 삭제를 의미하지 않는다.
- 동일 Workspace는 여러 Conversation 및 Environment에서 참조할 수 있고, 각 Environment의 실행·권한 정책은 달라질 수 있다.
- Workspace 전용 소프트웨어 모듈은 **없다**.
- 현재 온라인 상태는 Binding active에서 자동 추론하지 않는다. 권한은 Tool 실행 시점에 재검증한다.

**PROPOSED**

- `observe_bindings(environment_instance_id, allowed_binding_ids)` → online/offline/unknown 및 관측 시각. **Best effort**, 무부작용, 타임아웃/한도 필요.
- `authorize_and_execute(binding_id, tool_request, trusted_scope)` → 권한·연결 재검증 후 결과 또는 명시적 DENIED/UNAVAILABLE.
- 기본 Binding 선택은 `(conversation, environment_instance)`당 0~1개로 두는 방향.
- Data에는 Activation/Binding/Registry가 영속되고, Environment는 실시간 연결 가능 상태를 관측. **Prompt CurrentState는 둘의 읽기 투영**, 새로운 원본이 아님.

**OPEN:** Instance/Binding 생성·철회 주체, default scope, Sandbox의 기술/자원/네트워크 정책, remote/local 종류별 실행, 안전한 상태 관측 API, Environment 권한 계약. **기본 Workspace의 run_command에 OS Sandbox를 적용하는 요구사항은 확정(V3-032)**이며, 초기 사용자 Approval은 요구하지 않는다(V3-034).

## 11. 종단 간 하나의 Run 예시 (출처/호출/데이터 구분)

```text
0. UI → Control.drive(trusted conversation scope, user event ref)
1. Control → Data.append_event            [user input exact, if not already recorded]
2. Control → Prompt.compose               [request construction]
      Prompt → Data.read_events/snapshot   [source: Data]
      Prompt → Data.read_current_state_basis
      Prompt → Tool.catalog_tools          [source: Tool registry]
      Prompt → Environment.observe         [optional, live status]
   Prompt → Control                        [neutral input + source basis]
3. Control → Provider request preparation  [provider-specific wire normalization]
4. Control → Data.prepare_run              [persist actual raw provider request BEFORE send]
5. Control → Provider.invoke               [LLM invocation exactly once]
6. Control → Data.record_run_outcome        [actual raw response; received or failure]
7. Control → Data.commit_model_output      [assistant final/question/call group]
   ├── final / question: stop loop
   └── tool calls: Data pending records → Tool.execute_group
         ├── Internal Tool → Data public API (Memory/Plan/Note)
         └── Environment Tool → Environment.authorize_execute
8. Tool results → Data commit outcomes       [results and provenance]
9. Control repeats from Prompt              [next Run trigger = existing Event/group refs]
```

이 절차는 **책임 경계의 대표적인 예시(PROPOSED)**다. 제어의 실제 트랜잭션 분할, Provider의 raw wire capture, tool execution barrier는 아직 확정하지 않았다. 특히 raw 요청을 최종 바이트로 만드는 책임과 송신 API 경계는 독립 검토에서 일치시켜야 한다. `Data.prepare_run`와 외부 네트워크 송신 사이에 DB 원자성을 가정하지 않는다.

### 예외 흐름

- Provider 응답이 정규화 실패: raw response는 남으며 정상 assistant output을 날조하지 않는다.
- 다중 Tool 실행 결과 순서가 뒤바뀜: `(run_id, call_id, ordinal)`과 완료 시각을 구분. native replay는 호출 그룹을 완결시킨 후.
- Plan/Note 결과: Artifact 원본은 Data; Conversation에는 완결된 간결한 result; UI는 독자적 표시가 가능하다.
- ToolResult가 Conversation에 없다면: Prompt가 Execution Record에서 해당 결과를 찾아 정합성을 검증; 빠진 결과를 가정하지 않는다.
- Default Binding이 철회되거나 오프라인: Prompt에 보인 ID 또는 stale observation으로 실행 허용하지 않음.

## 12. 계약 관리·변경 영향 및 테스트

- 모듈이 자신이 소유한 `public Port`의 코드/테스트/문서 계약을 관리한다. 타 모듈의 `private` 모듈/DB/SDK 타입을 가져오면 dependency gate 실패.
- 계약 변경 시 **Owner가 version·status·consumer 목록·Source of Truth·테스트 영향**을 먼저 갱신한다. 구현 변경은 같은 공개 의미를 만족하면 다른 소비 모듈 구현의 수정을 요구하지 않아야 한다.
- Provider Adapter·Data Storage·Environment Adapter를 Fake로 교체해도 Control/Prompt/Tool이 공개 계약만으로 실행되는지 검증한다.
- **정적 검사:** 금지 import/cyclic dependency, 소비자별 허용 Port만 참조.
- **행동 테스트:** append-only exact Event, raw Run I/O, Snapshot 최신 1개, Plan/Note 원자 커밋, CurrentState scope filtering, binding revoke 시 재검증, Tool group replay, 동일 Workspace conflict, final/question 종료.
- **호환성 계약:** 데이터 필드 추가/삭제, Error type·retryability, 호출 순서, cancellation semantics, 권한 범위까지 포함. 'signature만 동일'은 충분하지 않다.

### 참조 구현 영향 예시

| 변경 사항 | 변경해도 되는 내부 구현 | 반드시 유지할 계약/테스트 |
| --- | --- | --- |
| SQLite → 다른 DB | Data persistence internals | DATA-01~08 및 Event/Run/Snapshot exact 의미 |
| OpenAI → 다른 LLM | Provider Adapter | PROVIDER-01/02, native tool history 합법성 |
| Inside sandbox 교체 | Environment internal executor | ENV-01/02의 정책/오류·결과 의미 |
| Prompt 압축 알고리즘 교체 | Prompt internals | PROMPT-01/03/04, Event coverage 및 no duplication |
| Tool Scheduler 알고리즘 교체 | Tool internals | TOOL-02/03, call_id mapping 및 안전한 동시성 |
| Plan/Note schema 확장 | Internal Tool + Artifact schema version | DATA-04/08, 다른 모듈의 API 경계 불변 |

## 13. 남은 결정 — 이 문서로 확정하지 않는다

**초기 구현(P0) 전에 결정할 것**

1. `ConversationEvent`, `RunRecord`, `ToolExecutionRecord`, `PromptSnapshot`의 **정확한 불변 DTO와 오류 스키마**.
2. Provider-neutral `ModelInput / ModelOutput` 및 native Tool 그룹 재생 안전성, CurrentState 메시지의 Provider별 합법성.
3. Raw request·response 비밀 정보/스트리밍 캡처, Run 기록 영속 순서, 재시도/unknown 상태와 Data transaction 경계.
4. Conversation별 Control 직렬화를 실현할 lease/queue, final/question/mixed-output의 정확한 경계.
5. **최신 전체 Prompt Snapshot**의 cursor, exact-tail coverage 및 source revision 일치 검사.
6. 최소 Port 선언 코드의 실제 위치, Contract test 및 의존성 검사 도입 방식.

**Inside/Workspace 연결(P1) 전에 결정할 것**

7. Environment 인스턴스·Binding·기본 Workspace 선택 스코프 및 활성/철회 권한.
8. Workspace online observation/timeout/freshness의 책임과 Provider 투영 방법.
9. Scheduler read/write footprint, 같은 Workspace의 unsafe command 직렬화, cross-conversation alias conflict.
10. **초기 기본 Workspace에 OS Sandbox + run_command 및 Sandbox 네트워크 기본 차단 적용은 확정**. Approval/Worktree 사본 격리는 요구하지 않는다. v2 Sandbox를 선택적으로 재사용하되 변경된 사용자 Root·Conversation cwd·네트워크 정책을 재검증한다.

**확장 단계(P2)에서 결정할 것**

11. Memory 세부 수명, Plan/Note revision·삭제·outside export, Artifact 변경 이력.
12. Prompt 압축 시점/품질/예산, opaque Provider continuation/캐시 최적화/streaming.
13. 데이터 삭제 cascade/retention, v2 데이터 이관, 외부 서비스 Connector 형태.

상세 추적은 [open-questions.md](open-questions.md), [Data Contract](data-contract-v0.1.md)의 DC 목록과 [CurrentState Contract](current-state-contract-v0.1.md)의 CS 목록을 따른다.

## 14. 문서의 권위와 앞으로의 업데이트

- **이 문서**: 모듈 간 관계·Contract ownership·source map·공개 Port 인벤토리를 한눈에 조회하는 통합 기준선.
- **decisions.md**: 사용자가 실제로 승인한 **확정 결정**의 원본 이력. 이번에 채택한 계약 메타데이터 형식도 여기 기록한다.
- **상세 v0.1 초안**: Data, CurrentState, Prompt/Tool의 구현 전 설계 근거. 변경 사항은 통합 기준서에도 반영해 표가 어긋나지 않도록 한다.
- **현재 저장소는 설계 문서만 존재**한다. `defined_at`/planned code path가 적혔다고 해당 파일/테스트/구현이 생성됐다는 뜻은 아니다.

> **채택한 새 원칙:** 각 공개 계약의 **정의 책임(Owner), 실제 제공자(Provider), 사용처(Consumer), 데이터 원천(Source of Truth), 선언 위치(Contract Location)**를 추적한다. 이는 모듈 교체 시 영향 범위와 테스트 책임을 확인하기 위한 최소 메타데이터다.

## 15. Default Workspace & Files / Sandboxed Commands — 2026-10-13 current decision

**CONFIRMED: V3-030 + V3-032~036.** 첫 Workspace를 **사용자/principal별** 공유 기본 파일 공간으로 구현한다. **Conversation마다 별도 폴더를 기본 cwd로 사용하고, 같은 사용자에게 허용된 다른 Conversation 폴더도 읽고 쓸 수 있다.** 명령 실행 `inside/run_command`를 첫 범위에 포함하고 **OS Sandbox가 반드시 적용**되도록 설계한다. **인간 Approval 및 Git Worktree/복제 Workspace 격리**는 첫 릴리스에서 요구하지 않는다.

```text
Default Workspace for authorized principal  (one workspace_id proposed)
  /workspace/
      conversations/<conversation-A>/    ← Conversation A default cwd
      conversations/<conversation-B>/    ← A can access B if same authorized scope
      shared/                            ← optional convenience directory

Inside Environment
  ├── structured file read/write Tools
  └── inside/run_command → OS sandbox → only principal-scoped /workspace RW
                               no host Data Root / other user directories
```

- **Conversation별 폴더는 사용 편의성 경계일 뿐 OS 보안 경계가 아니다.** 호스트/다른 사용자의 데이터는 Sandbox mount/view 및 사용자 권한 정책으로 별도 보호한다.
- 원본 소유권: 사용자 현재 파일 bytes는 기본 Workspace filesystem; Data에는 Conversation-folder mapping, Registry, Binding, File metadata 및 attachment refs. Plan/Note는 **Data.Artifact** 원본.
- 명령이 파일을 임의 변경할 수 있으므로 FileAsset metadata hash를 작업 파일의 영구 immutable 증거로 쓰지 않는다. **working file과 published immutable attachment version 분리는 권고안**이며, 해당 immutable snapshot의 물리적 저장 위치는 OPEN.
- Model이 제공한 path는 권한이 아니다. 실제 대상 principal/Binding/cwd를 검증하고, 외부 탈출 path/symlink/hardlink/mount alias를 거부한다.
- Sandbox 미지원 시 **unsandboxed `run_command` fallback을 두지 않는다**. **네트워크 기본 차단은 확정**했으며, 구체 Linux 격리 기술·기동 Probe·DNS/loopback/socket 검증 및 시간·메모리·프로세스·출력 한도는 상세 설계 대상이다. 별도 Web Search Tool은 향후 가능하지만 명령 Sandbox의 네트워크 권한을 확대하지 않는다.
- 같은 공유 Workspace root를 읽고 쓸 수 있는 임의 명령은 다른 Conversation의 파일도 손상시킬 수 있다. UNKNOWN file effects를 가진 run_command는 같은 Workspace 파일 작업과 기본 충돌하며, 완료/결과 불명은 ToolExecution으로 보존한다.
- Conversation 삭제가 공유 filesystem 경로와 published files를 어떻게 정리하는지는 OPEN; 다른 Conversation이 사용 중이면 자동 삭제하지 않는다.

**Contract metadata:** FILE-01~05, ENV-04~05, DATA-11, TOOL-05; future TOOL-06 (§4). 세부: [Default Workspace v0.2](default-workspace-files-v0.2.md), [Sandbox Execution v0.1](sandbox-execution-v0.1.md). 이전 [Default Workspace v0.1](default-workspace-files-v0.1.md)은 Superseded.

## 16. Conversation Pause/Resume — 2026-10-13 신규 설계

**CONFIRMED:** Run은 Provider 호출 1회, Run 원본·ToolExecution은 Data에 보존, Conversation별 Control/Run은 직렬화.

**PROPOSED:** Pause는 진행 중 Run을 토큰 단위로 일시 정지하는 기능이 아니라 **새로운 Run/Tool 작업을 시작하지 않고 안전한 경계에서 멈추는 요청**이다. Conversation-scoped `ControlExecutionState`(새 Task 계층 아님)는 `RUNNING/PAUSE_REQUESTED/PAUSED/WAITING_FOR_USER/RECOVERY_REQUIRED` 등 실행 위치/의도를 저장한다.

- Provider 응답 대기 중 Pause: 수신 가능한 실제 결과를 저장하고 새 Run을 시작하지 않음.
- Provider 응답이 ToolCall 그룹인데 아직 실행 전: `PENDING` 호출을 보존하고 PAUSED; **Resume 시 이 호출들을 먼저 완료**해야 하며 그 전에는 새로운 Provider Run 금지.
- 일부 Tool만 완료: 완료한 호출은 재실행하지 않고 나머지 `PENDING`만 Resume. 결과 불명인 호출은 자동 재실행하지 않음.
- final/question terminal의 의미는 Pause 요청으로 덮어쓰지 않음.
- Resume 시 기존 Run의 Provider 요청을 다시 전송하는 것이 아니라 **완결된 기록에서 다음 작업 지점을 복원**함.

**Contract metadata:** CTRL-03/04, DATA-09/10, TOOL-04 (§4, 상세 [control-pause-v0.1.md](control-pause-v0.1.md)). 실제 status enum, DB lease, Pause timeout 및 Cancel 의미는 검토 후 결정.
