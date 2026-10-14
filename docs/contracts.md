# HIMARI v3 — 통합 모듈 계약 기준서

> 작성: 2026-10-13 · 문서 버전: **Contract Baseline v0.1** · 구현 상태(2026-10-14): **P0 계약·실제 Provider·Inside 파일 Tool·Sandbox `run_command`·SQLite 영속 Data·Web 서버 구현됨 / CLI·자원 한도 미구현**
>
> **이 문서는 지금까지의 모듈 계약을 찾고 비교하는 단일 진입점이다.** 이미 합의한 원칙은 **CONFIRMED**, 그 원칙에 따라 작성한 포트/DTO/함수 이름은 **PROPOSED**, 후속 선택이 필요한 곳은 **OPEN**으로 표시한다.
>
> 상세한 설계 근거는 [decisions.md](decisions.md), [data-contract-v0.2.md](data-contract-v0.2.md), [current-state-contract-v0.1.md](current-state-contract-v0.1.md), [prompt-composition.md](prompt-composition.md), [data-and-tools.md](data-and-tools.md), [open-questions.md](open-questions.md)에 남겨 둔다. 내용이 충돌하면 명시적으로 사용자 승인을 받은 **Confirmed Decision**을 우선하고, 충돌을 이 문서에 드러내 수정한다.

## 1. 목적과 설계 원칙

**목표:** 모듈이 자신이 소유한 공개 계약을 유지하는 한 구현, 알고리즘, 저장 방식, Provider SDK, Environment 백엔드를 교체하더라도 소비 모듈의 내부 구현을 수정할 필요가 없어야 한다.

- **6개 핵심 모듈:** Control, Data, Prompt, Provider, Tool, Environment.
- **Workspace는 독립 자원**이지만 전용 코드 모듈을 만들지 않는다. Registry/Binding은 Data가 영속화하고, 연결·실행은 Environment가 담당한다.
- Memory·Plan·Note는 Tool의 구현이며 새로운 최상위 실행 제어 모듈이 아니다.
- **Run = LLM 호출 1회**. Control이 Run의 연쇄를 다루며, Provider·Tool·Data는 Run 반복의 정책을 소유하지 않는다.
- 공개 Contract 유지와 모델 출력의 동일성은 다르다. 현재 한 프로세스의 **논리적 구현 격리**를 목표로 하고 물리 프로세스 장애 격리를 초기 필수 조건으로 만들지 않는다.
- Data의 private SQL/Repository, Provider-specific wire 타입, Environment OS 핸들은 공개 계약 밖으로 누출하지 않는다.
- 공용 거대 계약 패키지나 런타임 Contract Registry를 별도로 만들지 않는다. **계약은 Owner 모듈의 공개 Port가 정의**하고 Bootstrap이 구현체를 주입한다.
- **Data 공개 Port는 Consumer별 sub-port로 나눈다(V3-039):** `ConversationLog` · `ArtifactStore` · `WorkspaceState` · `StateQuery`(읽기 전용 합성 조회) · `ControlState`(P2). 백엔드는 공유해도 계약 변경 단위는 sub-port다.
- **확장 SPI가 가장 먼저, 가장 작게 확정된다(V3-040):** `ToolHandler` · `ProviderAdapter` · `EnvironmentPlugin`. 새 Tool/Provider/Environment는 패키지 하나로 추가한다.
- **Environment가 자기 Tool을 제공(V3-041)**하고, **Tool 구현은 실행 원장을 모른다(V3-042)** — 원자 커밋은 Tool Dispatcher 책임.
- **Prompt 핵심은 순수 함수(V3-043)**, **question은 suspending Tool `ask_user`(V3-044)**, **P0는 단일 프로세스 규모이되 송신·부작용 직전 영속 표시와 크래시 후 UNKNOWN 처리는 유지(V3-045)**, **계약은 코드(`api.py`)로 선언(V3-046)**.

## 2. 계약 문서 표준 — **CONFIRMED (이번 결정)**

모든 공개 계약에는 최소한 다음 정보를 기록한다.

| 항목 | 정의 |
| --- | --- |
| **Contract ID / Version / Status** | 문서·테스트에서 식별하는 계약 ID, 호환성 버전, CONFIRMED/PROPOSED/OPEN |
| **Owner** | 공개 인터페이스와 의미론적 호환성을 정의·유지할 책임 모듈 |
| **Provider (Implementer)** | 해당 인터페이스를 실제 구현하고 응답을 제공하는 모듈/어댑터 |
| **Consumer** | 해당 공개 인터페이스를 호출하거나 반환 값을 소비하는 모듈 |
| **Source of Truth** | 반환 데이터의 **authoritative 원천**, 즉 실제 값을 소유/확정하는 곳. 파생값이면 원천들의 목록 |
| **Contract Location** | 구현된 공개 계약은 Owner 모듈 `api.py`가 원본(V3-046). 미구현 Port는 `planned`로 구분하고 상세 설계는 근거로 유지한다 |
| **Input / Output** | provider-neutral 불변 DTO·인자·반환 결과 및 지원 범위 |
| **Guarantees / Errors / Effects** | 불변식, 실패/취소/시간순서·원자성, 실행 부작용 여부 |
| **Dependencies / Tests** | 사용하는 하위 공개 계약, 대체 구현 및 계약 검증 범위 |

**구분 규칙**

- **Owner ≠ Provider**일 수 있다. 예: Provider 모듈이 정의한 `InvokeModel` 계약을 OpenAI Adapter가 구현한다.
- **Provider ≠ Source of Truth**일 수 있다. 예: Prompt가 현재 상태를 제공하지만 Workspace Binding의 authoritative 원본은 Data에 있다.
- **Consumer는 SQL 테이블·내부 구현 대신 공개 Port만 참조**한다.
- **Contract Location**은 구현된 `api.py`와 미구현 설계상 Port를 구분한다.
- 데이터 흐름(응답 반환)은 보통 호출 방향과 반대다. `Control → Prompt.compose()`는 호출, `Prompt → Control`은 반환 데이터의 흐름이다.

**코드 원본:** `src/himari/{control,data,prompt,provider,tool,environment}/api.py`와 `src/himari/common.py`. 실제 DTO·시그니처·상태값은 해당 코드가 우선한다(V3-046). 아래 계약 ID/메서드명은 논리적 분류이며 소스 이름과 1:1일 필요가 없다. Sandbox·Scheduler·Streaming·정식 Pause 등 미구현 항목은 제안 상태다.

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
     │  inject public ports (공개 계약으로만 연결) + 플러그인 등록(Provider/Environment/Internal Tool)
     ▼
Control
 ├── Prompt.load_context ──────────┬── Data.ConversationLog / StateQuery (exact history / snapshot / state)
 │                                 ├── Tool.catalog       (Environment·Internal ToolHandler 취합)
 │                                 └── Environment.observe (필요 시만; live connectivity)
 ├── Prompt.compose(PromptInputs)   (순수 함수, I/O 없음)
 ├── Provider.prepare_native_request → Data.ConversationLog.prepare_run → Provider.invoke_prepared
 ├── Provider.normalize → Data.ConversationLog.project_model_output
 └── Tool.dispatch_group ─────────┬── ToolHandler.run() → ToolOutcome  (Internal / Environment 제공 / ask_user)
                                   └── Dispatcher → Data.ConversationLog(+ArtifactStore) 원자 커밋

Provider Adapter ─── Provider-specific API
EnvironmentPlugin ── tools() 제공 · execute 시 Data.WorkspaceState 재검증
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

아래 `API name`은 개념적 계약 분류다. 현재 구현된 Port의 실제 시그니처는 `api.py`를 따른다. 초기 작성 당시의 상태 문구는 구현 상태와 다를 수 있으므로 다음 표의 상태를 병행 확인한다.

| ID | Contract / Owner | Provider → Consumer | Source of Truth / 입력 | 반환 또는 효과 | 상태 |
| --- | --- | --- | --- | --- | --- |
| **CTRL-01** | DriveConversation / **Control** | Control → 외부 UI/CLI 진입점 | 사용자 입력 + 신뢰된 conversation scope | Run 실행, Tool 실행·종료 제어 | P0 코드 구현 |
| **CTRL-02** | ResumeConversation / **Control** | Control → 외부 UI/CLI | Data 실행 상태·신규 사용자 응답 | 중단·question 이후 재개 | resume 구현; 정식 Pause 미구현 |
| **DATA-01** | AppendReadEvents / **Data·ConversationLog** | Data → Control, Prompt | Data.ConversationEvents | immutable event + ordered sequence | P0 인메모리 구현 |
| **DATA-02** | PrepareRecordRun / **Data·ConversationLog** | Data → Control | Data.RunRecords | raw request 영속화, **송신 직전 `DISPATCH_CLAIMED` 표시(P0)**, raw response/실패 기록 | P0 인메모리 구현 |
| **DATA-03** | RecordToolExecution / **Data·ConversationLog** | Data → Control, Tool Dispatcher, Prompt | Data.ToolExecutionRecords | run_id/call_id별 실행 상태·원본/모델 결과, **부작용 Tool 실행 직전 `EXECUTION_CLAIMED`(P0)**, ask_user 대기 판별·답변 커밋(T7a), 재시작 정리(T-R) | P0 인메모리 구현 |
| **DATA-04** | ReadWriteArtifact / **Data·ArtifactStore** | Data → Prompt, Tool Dispatcher (읽기는 Internal ToolHandler 가능) | Data.Artifacts | Plan/Note current content+revision | P0 인메모리 구현 |
| **DATA-05** | ReadReplacePromptSnapshot / **Data·ConversationLog** | Data → Prompt, Control | Data.LatestPromptSnapshot | 최신 전체 가공 Prompt 1개 | Snapshot 저장 Port 구현; 압축 요약 + coverage 저장(T9c `commit_compaction`, V3-P48) |
| **DATA-06** | ReadCurrentStateBasis / **Data·StateQuery** | Data → Prompt | Data Registry+Activation+Binding+Settings+Artifacts | 일관된 PersistentStateBasis | P0 인메모리 구현 |
| **DATA-07** | ReadWorkspaceBinding / **Data·WorkspaceState** | Data → Environment, Tool | Data.WorkspaceRegistry/Binding | Binding/등록·활성 관계 | P0 인메모리 구현 |
| **DATA-08** | CommitToolOutcome / **Data·ConversationLog (+ArtifactStore 트랜잭션)** | Data → **Tool Dispatcher** | ToolOutcome의 Artifact 변경 + ToolExecution 종결 + optional ConversationEvent | revision CAS 및 원자 커밋. Tool 구현은 호출하지 않음(V3-042) | P0 인메모리 원자 커밋 |
| **PROMPT-01** | ComposeModelInput / **Prompt** | Prompt → Control | `PromptInputs` (loader가 DATA-01/03/05/06 + Tool catalog에서 수집) | provider-neutral model input+prompt basis. **순수 함수(V3-043)** | P0 순수 Composer 구현 |
| **PROMPT-02** | ProjectCurrentState / **Prompt** | Prompt → Prompt composition | DATA-06 + optional ENV-01 | CurrentStateProjection | PROPOSED |
| **PROMPT-03** | ValidateNativeToolGroup (구 ReplayHistory) / **Prompt** | Prompt → Prompt composition | exact Events + non-Conversation Execution Records | 완전한 정규화 Tool 그룹, coverage | P0 Tool 그룹 검증 구현 |
| **PROMPT-04** | CompactAndSnapshot / **Prompt** | Prompt → Control/유지관리 경로 | complete history / DATA-05 | 압축된 부분·exact tail·Snapshot 교체 요청 | 기본 구현(V3-P48, Proposed): 토큰 기준 80% 자동 압축 |
| **PROVIDER-01** | InvokePreparedRequest / **Provider** | 각 Provider Adapter → Control | PROVIDER-02가 만들고 DATA-02가 보존한 native request | 실제 LLM 호출 1회, raw response 또는 typed 실패 | Scripted·OpenRouter 구현 |
| **PROVIDER-02** | PrepareNativeRequest / **Provider** | 각 Provider Adapter → Provider.invoke 경계 | Prompt 중립 입력 + Tool 표면 + continuation | provider-native messages/tools/role/reasoning | Scripted·OpenRouter wire-exact 구현 |
| **PROVIDER-03** | NormalizeNativeResponse / **Provider** | Provider Adapter → Control/Data projection | 실제 raw native response | **FINAL** / **TOOL_CALLS**(질문은 `ask_user`) 또는 typed error | P0 구현(`provider/api.py`, `provider/openrouter.py`) |
| **TOOL-01** | CatalogTools / **Tool** | Tool Registry → Prompt, Control | 등록된 ToolHandler (Internal + `EnvironmentPlugin.tools()` + External + `ask_user`) + Policy(활성 Environment 종류, `ExternalAccess`가 켠 External 출처) | 모델 노출 가능한 도구 정의 | P0 Registry 구현 |
| **TOOL-02** | DispatchToolCalls / **Tool** | Tool Dispatcher → Control | Run의 호출 그룹 + 각 Tool 정의/Binding | `call_id`별 ToolOutcome을 DATA-08로 커밋, Model ToolResult | P0 Dispatcher 구현 |
| **TOOL-03** | ScheduleConflicts / **Tool** | Tool Scheduler → Tool Dispatcher | verified resource identity & effect metadata | 독립 실행 병렬·충돌 직렬화 | 병렬 원칙 확정, 정책 미정 |
| **ENV-01** | ObserveBindings / **Environment** | Environment 구현 → Prompt | 실제 연결·가용성 관측, Data Binding | online/offline/unknown 등 시점 표시 | PROPOSED |
| **ENV-02** | AuthorizeExecute / **Environment** | Inside 등 Environment 구현 → Tool Dispatcher | Data Binding + 현재 실행 정책 + Workspace | 실행 결과/실패/거부 | 방향 확정, API·Sandbox 미정 |
| **ENV-03** | ManageActivation / **Environment** | Environment/Control 구현 → 외부 활성화 요청자 | Data Activation/Binding (영속 원본) | 활성화·연결 관계의 생성/변경/비활성화 | 책임 세부 OPEN |
| **CTRL-03** | RequestPause / **Control** | Control → UI/CLI | Data.ConversationControlState / active Run | durable pause intent, eventual paused boundary | PROPOSED |
| **CTRL-04** | ResumeExecution / **Control** | Control → UI/CLI | ControlState + completed/partial Run and Tool records | continue pending ToolCall or next Run, without blind replay | PROPOSED |
| **DATA-09** | ReadWriteControlState / **Data·ControlState** | Data → Control | Data control cursor/phase/revision | serialized state transition / resumption checkpoint | PROPOSED · **P2 (V3-045)** |
| **DATA-10** | ClaimControlLease / **Data·ControlState** | Data → Control | Data owner generation / fence | concurrent resume exclusion | PROPOSED · **P2 (V3-045)**, P0는 프로세스 내 lock |
| **TOOL-04** | ResumePendingGroup / **Tool** | Tool → Control | Data ToolExecutionRecords | run only unstarted pending calls, preserve original call IDs | PROPOSED |
| **FILE-01** | FileOperations / **Environment** | Inside → Tool, file ingress | Default Workspace real file bytes | bounded read/write/inspect with relative-path validation | initial scope CONFIRMED, API PROPOSED |
| **FILE-02** | FileMetadata / **Data·WorkspaceState** | Data → Tool, UI, Prompt | Data FileAssetRecord and AttachmentRefs | durable file_id/path/version/digest and conversation linkage | PROPOSED |
| **FILE-03** | StructuredFileTools / **Environment(Inside)** | Inside가 제공하는 ToolHandler → Tool Dispatcher | FILE-01 + FILE-02 | create/edit/list/read/delete/publish tool results | PROPOSED |
| **FILE-04** | UploadDownload / **Interface** | Web/CLI surface → User/Control | FILE-01 bytes + FILE-02 authorized metadata | upload and download bytes without host path disclosure | PROPOSED |
| **FILE-05** | AttachmentManifest / **Prompt** | Prompt → Control | Data attachment references | model-visible bounded manifest, no unbounded bytes | PROPOSED |
| **ENV-04** | SandboxedCommandExecution / **Environment** | Inside Sandbox Backend → Tool | authorized principal root + current Workspace Binding + execution policy | bounded command exit, output, failure; **no unsandboxed fallback** | sandbox requirement CONFIRMED, DTO PROPOSED |
| **DATA-11** | ResolveConversationWorkingDirectory / **Data·WorkspaceState** | Data → Environment, Prompt | Data Conversation ↔ user Workspace mapping | per-conversation default cwd, user-authorized cross-folder scope | directory behavior CONFIRMED, mapping PROPOSED |
| **DATA-12** | ConversationCatalog / **Data·ConversationCatalog** | Data → Web/CLI (조립된 Application 경유) | Data Conversation 소유·제목·생성 시각 + Event 요약 | T0 대화 생성(초기 Environment 활성화와 원자적), 본인 대화 목록(최근 활동순)·조회·제목 변경 | P0 코드 구현 (V3-P43) |
| **DATA-13** | ExternalSourceSettings / **Data·ExternalSourceSettings** | Data → `ExternalAccess`(Tool), Web/CLI (Application 경유) | Data 대화별 External Tool 출처 선택(출처 → 켬/끔) | 운영자 기본값과 다른 선택만 저장·조회·삭제. 기본값은 모름 | P0 코드 구현 (V3-P53, SQLite v4) |
| **TOOL-05** | RunCommandTool / **Environment(Inside)** | Inside가 제공하는 ToolHandler → Tool Dispatcher | ENV-04 execution result, DATA-11 cwd mapping | command request, bounded canonical ToolResult | initial support CONFIRMED, schema PROPOSED |
| **ENV-05** | SandboxCapabilityActivation / **Environment** | Sandbox validation/self-test → Environment startup/Tool | host capability + validated sandbox policy generation | **isolated-only network enforcement** status; refuse if unavailable | isolated-only profile CONFIRMED (V3-037); spec in [Sandbox Implementation v0.2](sandbox-implementation-v0.2.md), implementation PROPOSED |
| **TOOL-06** | WebSearch (future) / **Tool** | trusted Web Search adapter → Control/Prompt | external retrieved content, explicitly untrusted | bounded Web Search observations without Sandbox egress grant | OPTIONAL FUTURE |
| **INTERNAL-01** | MemoryTool / **Tool** | Internal Memory ToolHandler → Tool Dispatcher | Data Memory 저장/검색 계약 (읽기) | ToolOutcome: 모델용 검색 결과, Conversation 기록 | 방향 확정, 기능 미정 |
| **INTERNAL-02** | PlanNoteTool / **Tool** | Internal Plan/Note ToolHandler → Tool Dispatcher | Data conversation-scoped Artifacts | ToolOutcome: 간결한 complete 결과 + `artifact_mutations` (커밋은 DATA-08) | 저장 위치 확정, 수정 API 미정 |
| **TOOL-07** | ToolHandler SPI / **Tool** | 모든 Tool 구현(Internal·Environment 제공·Control) → Tool Dispatcher | ToolDefinition + `run(call, ctx) -> ToolOutcome` | 확장 지점. Tool 구현은 ledger·`run_id` 트랜잭션을 모름 | P0 ToolHandler SPI 구현 |
| **ENV-06** | EnvironmentPlugin SPI / **Environment** | 각 Environment 구현 → Bootstrap, Tool Registry, Prompt | `kind`, `tools() -> [ToolHandler]`, `observe()` | kind당 1개 등록. Conversation 카탈로그에는 활성 Instance가 있는 kind의 Tool만. 대상 Instance/Workspace는 `workspace_binding_id`로 해석(V3-P19) | P0 SPI 구현; Inside 미구현 |
| **PROVIDER-SPI** | ProviderAdapter SPI / **Provider** | 각 Provider Adapter → Control | PROVIDER-01~03 + `describe_capabilities` | 새 Provider = Adapter 하나 | P0 ProviderAdapter SPI 구현 |
| **CTRL-05** | AskUserTool / **Control** | Control이 등록한 suspending ToolHandler → Tool Dispatcher | 모델의 `ask_user` 호출 + 이후 사용자 응답 Event | 그룹 커밋 후 `WAITING_FOR_USER`·lock 해제. 답변 시 원래 `call_id`의 ToolResult를 커밋(T7a)한 뒤 새 Run. 대기 판별은 영속 기록에서 파생 | P0 ask_user 구현 |
| **CTRL-06** | ConversationRunner / **Control** | Control → Web/CLI | ConversationDriver + 사용자 입력 | `drive`/`resume`을 백그라운드로 실행, (사용자, 대화)별 상태·완료 대기·종료 처리. 진행 내용은 Data Event로 조회 | P0 코드 구현 (V3-P44) |
| **CTRL-07** | ContextInspector / **Control** | Control → Web/CLI(Application 경유) | Prompt loader·composer·planner, ContextBudget | 다음 요청의 예상 토큰(측정/추정), 예산·압축 시작점·거부 한도, 최신 요약과 그 범위. 표시 전용, lock 없음 | P0 코드 구현 (V3-P50) |
| **CTRL-08** | ManualCompaction / **Control** | Control → Web/CLI(Runner 경유) | Prompt planner `plan_now`, Data T9c/T9d | 크기와 상관없이 지금 압축(요약 + 최근 tail). Run을 시작하지 않음. 백그라운드 작업이며 BUSY 규칙은 다른 작업과 같음 | P0 코드 구현 (V3-P51) |
| **UI-01** | WebInterface / **Interface** | Web 서버 → 브라우저·CLI | Application(DATA-12, CTRL-06, ConversationLog 읽기) | 운영자 인증, 대화 목록·생성·이름 변경, 메시지·재개 제출, Event·작업 상태 폴링 | P0 코드 구현 (V3-P46) |
| **UI-02** | TerminalInterface / **Interface** | CLI → 로컬 Web 서버(UI-01) | UI-01 JSON API + 운영자 비밀값 파생 bearer | 대화형·명령형 대화, 질문 답변, 이어서 실행, 목록·조회 | P0 코드 구현 (V3-P47) |

**Contract Location (PR #1~#12 반영):** Control·Data·Tool·Environment·Prompt·Provider 공개 계약은 각각 `api.py`로 선언됐다. Control 실행은 `control/driver.py`·`control/runner.py`, Bootstrap 조립은 `bootstrap/`(`Application`), 공용 DTO는 `src/himari/common.py`에 있다. Inside 파일 Tool·Sandbox `run_command`는 `environment/inside/`(PR #8~#9), SQLite Data는 `data/sqlite.py`(PR #10), Web은 `interfaces/web/`(PR #12)에 있다. `scripts/check_imports.py`가 모듈 경계를 검사한다.

| Owner | 현재 계약 문서 | 코드 선언 위치 |
| --- | --- | --- |
| Control | 본 문서 §4·§5, [control-prompt-provider-v0.1](control-prompt-provider-v0.1.md) | `src/himari/control/api.py` |
| Data | 본 문서 §4·§6, [data-contract-v0.2](data-contract-v0.2.md) | `src/himari/data/api.py` (sub-port Protocol: `ConversationLog`, `ArtifactStore`, `WorkspaceState`, `StateQuery`; P2 `ControlState`) |
| Prompt | 본 문서 §4·§7, [prompt-composition](prompt-composition.md) | `src/himari/prompt/api.py` |
| Provider | 본 문서 §4·§8, [prompt-composition](prompt-composition.md) | `src/himari/provider/api.py` |
| Tool | 본 문서 §4·§9, [data-and-tools](data-and-tools.md) | `src/himari/tool/api.py` (`ToolHandler` SPI 포함) |
| Environment | 본 문서 §4·§10, [current-state-contract-v0.1](current-state-contract-v0.1.md) | `src/himari/environment/api.py` (`EnvironmentPlugin` SPI), 구현은 `environment/inside/` |
| Bootstrap | 본 문서 §11 | `src/himari/bootstrap/` (구체 경로 미정) |

## 5. Control Contract

**Owner:** Control · **Provider:** Control 구현 · **Consumers:** UI/CLI·애플리케이션 진입점 · **Data Sources:** DATA-01/02/03, PROMPT-01, PROVIDER-01, TOOL-02 · **Location:** 본 문서 §5 / planned `control/api.py`.

**CONFIRMED**

- 동일 Conversation에서는 **Control 실행 흐름과 Run을 직렬화**한다. 다른 Conversation은 병렬 가능하며, 하나의 Run 결과로 발생한 독립 ToolCalls는 병렬 실행할 수 있다.
- Run은 모델 호출 **1회**. final response 또는 question에서 반복 종료/사용자에게 제어 반환.
- Tool 실패는 **ToolResult로 기록해 다음 모델 판단에 전달**한다. Tool 의미를 가정한 Runtime의 숨은 자동 복구는 기본 정책이 아니다.
- Run trigger는 이미 존재하는 Conversation Event 또는 대응 Tool 실행 기록의 참조이며 원본을 복사/이동하지 않는다.
- **question은 suspending Tool `ask_user` 호출(V3-044)**이다. 정규화 출력은 `FINAL | TOOL_CALLS`다. Control은 `ask_user` 그룹을 커밋한 뒤 `WAITING_FOR_USER`로 멈추고 **Conversation lock을 해제**한다. 사용자 응답이 오면 원래 `call_id`의 ToolResult를 커밋한 뒤 새 Run을 시작한다. 재시작 후 대기 판별은 영속 기록만 사용한다(상세 [CPP §5.1](control-prompt-provider-v0.1.md)).
- **P0 직렬화는 프로세스 내 Conversation별 lock(V3-045).** 이 lock은 **살아 있는 프로세스 안의 동시 실행만** 막는다. 크래시 후 중복 송신·중복 부작용은 Data의 송신 직전 `DISPATCH_CLAIMED`, 부작용 Tool 실행 직전 `EXECUTION_CLAIMED` 영속 표시가 막고, 재시작 시 결과가 없으면 `OUTCOME_UNKNOWN`/`UNKNOWN`으로 처리하며 자동 재실행하지 않는다. 분산 lease/fencing은 P2.

**PROPOSED**

- `drive(scope, initiating_refs)`는 Conversation lock 확보 → Prompt 구성 → raw request 저장 → Provider 1회 호출 → raw response 저장 → 정규화/Tool 분기 순서.
- 하나의 Run에서 여러 ToolCalls를 받으면 `call_id`별 모든 결과/실패/거부/취소가 종결 상태가 된 **완전한 호출 그룹**을 다음 Run의 근거로 사용한다.
- Resume는 저장된 원본 실행 상태에서 복원하되, 네트워크/외부 Tool의 실행 여부가 불명확한 상태를 임의로 재실행하지 않는다.

**OPEN:** `ask_user`와 다른 ToolCall 혼합 허용 여부(P0 제안: 금지, V3-P15), cancel, in-flight tool group 배리어·동시 사용자 입력. lease/queue/timeout은 P2.

## 6. Data Contract

**Owner/Provider:** Data · **Consumers:** Control, Prompt, Internal Tool, Environment (공개 Port만) · **Source of Truth:** Data에 영속된 각 record/registry · **Location:** 본 문서 §6 + **[data-contract-v0.2](data-contract-v0.2.md) (검수 후보)** 및 [v0.1](data-contract-v0.1.md) (초안 근거) / planned `data/api.py`.

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

**CONFIRMED (V3-039) — sub-port 분리:**

| Sub-port | 담는 계약 | 주 Consumer |
| --- | --- | --- |
| `ConversationLog` | DATA-01/02/03/05/08 (Event, Run raw, ToolExecution, Snapshot, Tool outcome 커밋) | Control, Prompt loader, Tool Dispatcher |
| `ArtifactStore` | DATA-04 (Plan/Note 읽기·revision) | Prompt loader, Internal ToolHandler(읽기) |
| `WorkspaceState` | DATA-07/11, FILE-02 (Registry·Binding·cwd·FileMetadata) | Environment, Prompt loader |
| `StateQuery` | DATA-06 (CurrentStateBasis 합성 일관 조회, 읽기 전용) | Prompt loader |
| `ExternalSourceSettings` | DATA-13 (대화별 External Tool 출처 선택, V3-P53) | `ExternalAccess`(Prompt loader·Dispatcher), Application |
| `ControlState` | DATA-09/10 (**P2**) | Control |

하나의 백엔드·트랜잭션을 공유해도 된다(예: DATA-08은 ConversationLog와 ArtifactStore 테이블을 한 트랜잭션으로 묶음). **V3-038의 "공개 계약 변경" 판정은 sub-port 단위**로 한다.

**PROPOSED (이름은 [Data v0.2 §10](data-contract-v0.2.md) 기준)**: `append_event/read_events`, `prepare_run/record_raw_outcome`, `project_model_output`, `complete_tool_execution`/`commit_tool_outcome`, `read_current_state_basis`, `get/replace_latest_snapshot`, `read_artifact/list_artifacts`, `read_binding_authority`, `resolve_conversation_workspace`.

**원자성 권고:**

1. Provider 요청(raw)을 **송신 전** Data에 보존.
2. 실제 응답을 받으면 **정규화 이전에 raw response** 저장.
3. Assistant ToolCall 그룹과 각 pending ToolExecution을 누락 없는 관계로 연결.
4. Internal ToolHandler가 반환한 `ToolOutcome`의 Artifact 변경 + Tool 완료 결과 + 필요한 Conversation Event를 **Tool Dispatcher가 하나의 Data 트랜잭션**으로 저장; revision CAS와 call_id 멱등성 고려.
5. 외부 효과가 있는 Tool은 DB 트랜잭션으로 파일 변경을 되돌릴 수 없음. 실행 여부가 불명확하면 **UNKNOWN**, 임의 재실행 금지.
6. Conversation 밖에 저장된 ToolResult를 다음 Prompt에 쓸 때는 `call_id`·원래 그룹·순서·완결성 검증. 불완전 그룹 재생 거부.
7. **P0(V3-045):** 송신 직전·부작용 Tool 실행 직전 영속 표시(T3/T6), 재시작 시 `OUTCOME_UNKNOWN`/`UNKNOWN` 정리(T-R), 자동 재실행 금지. ask_user 답변은 `user_message` + ToolResult + `tool_result` Event를 한 트랜잭션(T7a)으로.

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

**CONFIRMED (V3-043) — 순수 핵심 + 얇은 loader**

```text
PromptContextLoader.load(scope) -> PromptInputs        # I/O: Data sub-port, Tool catalog, optional ENV observe
compose(PromptInputs) -> PreparedModelInput            # 순수 함수: 압축·정렬·CurrentState 렌더·coverage 검증
```

- 압축 알고리즘, 캐시 배치, CurrentState 렌더러 교체는 `compose`만 바꾼다. 테스트는 `PromptInputs` 고정값으로 한다.
- loader는 Prompt 모듈 내부 구현이며 Control은 둘을 함께 쓰는 `Prompt.build(scope)` facade만 호출해도 된다.

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

**Owner:** Provider · **Provider:** OpenAI/기타 Adapter · **Consumers:** Control(호출), Prompt(지원 capability 계약 소비) · **Sources:** PROMPT-01의 중립 입력, 현재 모델 설정, 실제 외부 Provider API 결과 · **Location:** 본 문서 §8 + [Control–Prompt–Provider v0.1 검수 후보](control-prompt-provider-v0.1.md) / planned `provider/api.py`.

**CONFIRMED**

- Run의 모델 호출은 **정확히 1회**이며, Provider의 암묵적 재시도가 호출 횟수를 숨겨서는 안 된다.
- Adapter가 Provider native 형식, Tool schema/role, function call/result adjacency, reasoning/continuation 표현을 담당한다.
- reasoning **설정**, reported token **usage**, opaque **continuation**은 서로 다른 데이터. 비공개 reasoning 원문을 얻는다고 가정하지 않는다.
- Run별 실제 요청·응답 원본은 Data에 저장; 정규화 응답은 원본을 대체하지 않는다.

**PROPOSED — ProviderAdapter SPI (V3-040, [CPP v0.1 §4](control-prompt-provider-v0.1.md))**:

```text
prepare_native_request(model_input) -> NativePreparedRequest     # 네트워크 없음
invoke_prepared(prepared) -> NativeInvocationOutcome             # 정확히 1회 송신
normalize(raw) -> NormalizedModelOutput                          # FINAL | TOOL_CALLS
describe_capabilities(model) -> ProviderCapabilities
```

**P0 원본 요청 보존 기준은 wire-exact로 확정(V3-048).** Adapter가 만든 bytes를 송신 전에 저장하고 그 bytes를 그대로 보낸다. reasoning 책임 분담은 V3-049(설정·continuation 선택은 중립 계층, 네이티브 삽입은 Provider의 `prepare` 단계).

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
| `origin` = Internal / Environment / External (V3-P52) | 실제 실행 주체의 선택 |
| `workspace_requirement` = None / Required / Default | 인자 또는 유효한 기본 Binding 해석 |
| `effect_profile` = Read / Scoped Write / Unknown | 동시 실행의 검증 가능한 충돌 판단 |
| `conversation_record` / `presentation` | 원본 기록/모델 결과/UI 표시 분리 |

**CONFIRMED (V3-040~042) — ToolHandler SPI 형태 (시그니처는 PROPOSED)**

```python
class ToolHandler(Protocol):
    definition: ToolDefinition            # tool_id, input_schema, workspace_requirement, effect_profile,
                                          # completion: immediate | suspending, ...
    async def run(self, call: ToolInvocation, ctx: ToolContext) -> ToolOutcome: ...

@dataclass(frozen=True)
class ToolOutcome:
    status: Literal["succeeded", "failed", "denied", "cancelled"]
    model_result: ModelToolResult
    artifact_mutations: tuple[ArtifactMutation, ...] = ()
    raw_result: RawToolResult | None = None
```

- 등록 경로는 네 가지다: Internal Tool(Memory/Plan/Note), `EnvironmentPlugin.tools()`, External Tool(HIMARI 밖 서버, 예: MCP — `external_source`, V3-P52), Control의 `ask_user`.
- `completion = suspending`인 Tool(P0에서는 `ask_user`만)은 handler를 실행하지 않고 call을 `PENDING`으로 둔 채 Control에 대기를 알린다.
- **External Tool 노출과 실행 권한(V3-P53):** External Tool은 `ExternalAccess.enabled_sources(scope)`가 켠 출처의 것만 catalog에 보이고, Dispatcher는 실행 직전에 같은 질문을 다시 해서 꺼진 출처의 호출을 `DENIED`로 끝낸다(노출은 권한이 아니다). P0 구현은 운영자 설정(`mcp.json`)의 출처별 기본값 위에 대화별 선택(DATA-13)을 얹는다.
- **Environment가 제공한 Tool과 External Tool** 중 `effect_profile`이 `Read`가 아닌 것은 Dispatcher가 handler 호출 전에 `EXECUTION_CLAIMED`를 영속한다(V3-045, V3-P52). Internal Tool은 효과가 ToolOutcome뿐이고 결과와 원자 커밋되므로 claim하지 않는다([Data v0.2 §6](data-contract-v0.2.md)). Tool 구현은 이를 알 필요가 없다.
- `ToolContext`는 신뢰된 scope, 해석된 Binding, 읽기용 Data sub-port 등 **필요한 것만** 담는다. 쓰기 커밋은 Dispatcher가 DATA-08로 한다.

- 같은 Workspace의 범위 불명 `run_command`는 기본 직렬 후보; 확인된 서로 다른 파일 범위 수정은 공유 부작용이 없으면 병렬 후보.
- **Binding ID가 달라도 실제 Workspace·공유 자원이 같으면 충돌** 가능. 다른 Conversation도 Scheduler 충돌 영역에 포함.
- 모델이 주장한 수정 파일명/문자열 동일 여부는 안전성 증명이 아니다.
- Plan/Note 변경 + Tool 완료 결과 원자 커밋은 **Dispatcher가 ToolOutcome을 받아** DATA-08 복합 API로 실현한다.

**OPEN:** 동시성 limit/locking/resource alias, Internal/Environment dispatcher 세부 구현, 전체 Tool group 완료 배리어. (Connector 확장 유형은 V3-P52 External Tool로 제안.)

## 10. Environment Contract (Workspace 자원 접근)

**Owner:** Environment · **Providers:** Inside 등 Environment 구현 · **Consumers:** Tool(실행), Prompt(관측) · **Sources:** Data Registry/Binding, 실제 연결/실행 대상, 실행 정책 · **Location:** 본 문서 §10 + [current-state-contract-v0.1](current-state-contract-v0.1.md) + [sandbox-execution-v0.1](sandbox-execution-v0.1.md) / planned `environment/api.py`.

**CONFIRMED**

- **Workspace는 독립 자원**이며 Environment 종료/Binding 해제가 Workspace 삭제를 의미하지 않는다.
- 동일 Workspace는 여러 Conversation 및 Environment에서 참조할 수 있고, 각 Environment의 실행·권한 정책은 달라질 수 있다.
- Workspace 전용 소프트웨어 모듈은 **없다**.
- 현재 온라인 상태는 Binding active에서 자동 추론하지 않는다. 권한은 Tool 실행 시점에 재검증한다.

**PROPOSED**

- **EnvironmentPlugin SPI (V3-040/041 CONFIRMED, 시그니처 PROPOSED):** `kind`, `tools() -> Sequence[ToolHandler]`, `observe(...)`. Environment 전용 Tool(`inside/run_command`, Inside 파일 Tool)은 Environment 패키지가 직접 제공하며, 실행 메커니즘(Sandbox 등)은 그 패키지 내부 구현이다.
- **Instance·Binding과 ToolHandler 관계 (V3-P19 PROPOSED):**
  ```text
  Bootstrap: EnvironmentPlugin(kind="inside") 1개 등록 → tools() = [inside/run_command, inside/read_file, ...]  (kind 수준, 고정)
  Data.WorkspaceState: Conversation C의 Activation(environment_instance_id=e1, kind=inside)
                       Binding(b1: e1 → ws_A), default selection(C, e1) = b1
  Tool catalog(C): Internal + ask_user + {kind의 Tool | C에 활성 Instance가 있는 kind}
  Dispatch: call.workspace_binding_id(또는 Default 규칙) → read_binding_authority → (e1, ws_A, cwd)
            → binding의 kind == ToolHandler의 kind 확인 → ToolContext.binding에 담아 run()
            → Environment가 실행 직전 다시 검증
  ```
  ToolHandler는 Instance별로 만들지 않는다. Instance·Workspace 상태는 `ToolContext`로만 들어온다. P0는 Conversation당 kind별 활성 Instance 최대 1개라서 `environment/tool` 이름이 모호하지 않다.
- `observe_bindings(environment_instance_id, allowed_binding_ids)` → online/offline/unknown 및 관측 시각. **Best effort**, 무부작용, 타임아웃/한도 필요.
- `authorize_and_execute(binding_id, tool_request, trusted_scope)` → 권한·연결 재검증 후 결과 또는 명시적 DENIED/UNAVAILABLE.
- 기본 Binding 선택은 `(conversation, environment_instance)`당 0~1개로 두는 방향.
- Data에는 Activation/Binding/Registry가 영속되고, Environment는 실시간 연결 가능 상태를 관측. **Prompt CurrentState는 둘의 읽기 투영**, 새로운 원본이 아님.

**OPEN:** Instance/Binding 생성·철회 주체, default scope, Sandbox의 기술/자원/네트워크 정책, remote/local 종류별 실행, 안전한 상태 관측 API, Environment 권한 계약. **기본 Workspace의 run_command에 OS Sandbox를 적용하는 요구사항은 확정(V3-032)**이며, 초기 사용자 Approval은 요구하지 않는다(V3-034).

## 11. 종단 간 하나의 Run 예시 (출처/호출/데이터 구분)

```text
0. UI → Control.drive(trusted conversation scope, raw user input)   [UI는 user Event를 직접 기록하지 않음]
1. Control: Conversation lock 확보 (P0 프로세스 내: 동시 실행만 방지. 크래시 후 중복은 6·8단계 영속 표시가 방지; P2 lease)
2. Control: 사용자 입력 기록 (입력당 한 경로, lock 안에서)
      ask_user 대기 중(find_waiting_call) → answer_pending_call (T7a: user_message + ToolResult + tool_result)
      그 외                             → append_event (T1: 일반 user_message)
3. Control → Prompt.build                        [loader: ConversationLog / StateQuery / Tool.catalog / optional ENV observe]
      → compose(PromptInputs)                     [순수 함수]
   Prompt → Control                               [neutral input + source basis]
4. Control → Provider.prepare_native_request     [native body, 네트워크 없음]
5. Control → Data.ConversationLog.prepare_run     [persist raw request BEFORE send]
6. Control → Data.ConversationLog.mark_dispatch → Provider.invoke_prepared  [DISPATCH_CLAIMED 영속 후 정확히 1회]
7. Control → Data.ConversationLog.record_raw_outcome
8. Control → Provider.normalize → Data.ConversationLog.project_model_output
   ├── FINAL: stop loop
   └── TOOL_CALLS: pending records → Tool.dispatch_group
         ├── ask_user (suspending) → WAITING_FOR_USER, lock 해제 → 답변 시 T7a 커밋 후 새 Run
         ├── Internal ToolHandler → ToolOutcome
         └── Environment 제공 ToolHandler → (부작용 시 EXECUTION_CLAIMED 영속) → 내부 실행 → ToolOutcome
9. Tool Dispatcher → Data.ConversationLog.commit_tool_outcome (+ArtifactStore)  [원자 커밋]
10. Control repeats from Prompt                   [next Run trigger = existing Event/group refs]
재시작: T-R 정리 — DISPATCH_CLAIMED→OUTCOME_UNKNOWN, EXECUTION_CLAIMED→UNKNOWN, 자동 재실행 없음. ask_user 대기는 영속 기록에서 복원
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
- **V3-038 테스트 실행 정책 (CONFIRMED):** 공개 계약 변경 없음 → 변경한 모듈별 pytest만, 공개 Port/DTO/동작·보안·오류 계약 도입/변경/제거 → 전체 pytest. 여러 모듈의 구현이 바뀌어도 계약이 유지되면 바뀐 모듈만 테스트. 파일 수나 리팩터링 규모로 전체 실행을 결정하지 않는다. 상세 [testing-policy.md](testing-policy.md).
- **실제 Sandbox 호스트 검증은 별도 게이트**다. Environment 내부 구현만 수정했다면 Environment 모듈 pytest + 필요한 Linux 실호스트 격리 검증이고, 공개 ENV 계약까지 바뀌면 전체 pytest + 실호스트 검증이다.
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
| 새 Tool 추가 | 해당 ToolHandler 패키지 | TOOL-07 SPI. 다른 모듈 수정 없음 |
| 새 Environment 추가 (예: 로컬 PC) | 새 `environment/<kind>/` 패키지 | ENV-06 SPI, DATA-07 WorkspaceState. Tool 모듈 수정 없음 |
| 새 Provider 추가 | 새 Adapter | PROVIDER-SPI, native tool history 합법성 |

## 13. 남은 결정 — 이 문서로 확정하지 않는다

**PR #1~#6으로 구현된 P0 계약**

- `api.py`/공용 DTO, import 검사, Fake 기반 Run–Tool–Run 루프, `FINAL | TOOL_CALLS`와 `ask_user`.
- T3/T6 claim, UNKNOWN 모델 보고(V3-047), OpenRouter wire-exact 요청(V3-048), 턴별 continuation(V3-049).
- 인메모리 Event/Run/ToolExecution/Artifact는 구현됐지만 **실제 DB의 트랜잭션·재시작 내구성은 미검증**이다.

**남은 구현 및 검수**

1. 영속 Data, Run/Tool crash 내구성, 원본 저장·권한 관리.
2. Snapshot coverage, Context 압축·예산, continuation 크기 관리.
3. Inside Environment·파일/명령 Tool 및 실호스트 Sandbox 검증.
4. CLI/Web, 정식 Pause/Cancel, Streaming, Memory/Plan 및 충돌 Scheduler.

**Inside/Workspace 연결(P1) 전에 결정할 것**

7. Environment 인스턴스·Binding·기본 Workspace 선택 스코프 및 활성/철회 권한.
8. Workspace online observation/timeout/freshness의 책임과 Provider 투영 방법.
9. Scheduler read/write footprint, 같은 Workspace의 unsafe command 직렬화, cross-conversation alias conflict.
10. **초기 기본 Workspace에 OS Sandbox + run_command 및 Sandbox 네트워크 기본 차단 적용은 확정**. Approval/Worktree 사본 격리는 요구하지 않는다. v2 Sandbox를 선택적으로 재사용하되 변경된 사용자 Root·Conversation cwd·네트워크 정책을 재검증한다.

**확장 단계(P2)에서 결정할 것**

11. Memory 세부 수명, Plan/Note revision·삭제·outside export, Artifact 변경 이력.
12. Prompt 압축 시점/품질/예산, opaque Provider continuation/캐시 최적화/streaming.
13. 데이터 삭제 cascade/retention, v2 데이터 이관, 외부 서비스 Connector 형태.

상세 추적은 [open-questions.md](open-questions.md), [Data Contract v0.2](data-contract-v0.2.md)의 DR 목록과 [CurrentState Contract](current-state-contract-v0.1.md)의 CS 목록을 따른다.

## 14. 문서의 권위와 앞으로의 업데이트

- **이 문서**: 모듈 간 관계·Contract ownership·source map·공개 Port 인벤토리를 한눈에 조회하는 통합 기준선.
- **decisions.md**: 사용자가 실제로 승인한 **확정 결정**의 원본 이력. 이번에 채택한 계약 메타데이터 형식도 여기 기록한다.
- **상세 v0.1 초안**: Data, CurrentState, Prompt/Tool의 구현 전 설계 근거. 변경 사항은 통합 기준서에도 반영해 표가 어긋나지 않도록 한다.
- **현재 저장소에는 P0 실행 코드(영속 Data·Inside/Sandbox·Web 포함), 테스트 및 실제 OpenRouter Adapter가 있다.** 계약 인벤토리에는 아직 미구현인 장래 기능도 포함된다. 구현 여부는 소스·테스트와 구분해서 확인한다.

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
- Sandbox 미지원 시 **unsandboxed `run_command` fallback을 두지 않는다**. **초기에는 네트워크 차단 전용 단일 프로파일만 지원(V3-037)**하며, 네트워크 Namespace·host socket·DNS/loopback 검증이 실패하면 실행을 거부한다. `connected` 모드/프록시/도메인 allowlist는 첫 버전 범위가 아니다. Linux backend와 CPU·메모리·프로세스·출력 한도는 상세 설계 대상이다. 별도 Web Search Tool은 향후 가능하지만 명령 Sandbox의 네트워크 권한을 확대하지 않는다.
- 같은 공유 Workspace root를 읽고 쓸 수 있는 임의 명령은 다른 Conversation의 파일도 손상시킬 수 있다. UNKNOWN file effects를 가진 run_command는 같은 Workspace 파일 작업과 기본 충돌하며, 완료/결과 불명은 ToolExecution으로 보존한다.
- Conversation 삭제가 공유 filesystem 경로와 published files를 어떻게 정리하는지는 OPEN; 다른 Conversation이 사용 중이면 자동 삭제하지 않는다.

**Contract metadata:** FILE-01~05, ENV-04~05, DATA-11, TOOL-05; future TOOL-06 (§4). 세부: [Default Workspace v0.2](default-workspace-files-v0.2.md), [Sandbox Execution v0.1](sandbox-execution-v0.1.md), [Sandbox Implementation Candidate v0.2](sandbox-implementation-v0.2.md). 이전 [Default Workspace v0.1](default-workspace-files-v0.1.md)은 Superseded.

## 16. Conversation Pause/Resume — 2026-10-13 신규 설계

**CONFIRMED:** Run은 Provider 호출 1회, Run 원본·ToolExecution은 Data에 보존, Conversation별 Control/Run은 직렬화.

**PROPOSED:** Pause는 진행 중 Run을 토큰 단위로 일시 정지하는 기능이 아니라 **새로운 Run/Tool 작업을 시작하지 않고 안전한 경계에서 멈추는 요청**이다. Conversation-scoped `ControlExecutionState`(새 Task 계층 아님)는 `RUNNING/PAUSE_REQUESTED/PAUSED/WAITING_FOR_USER/RECOVERY_REQUIRED` 등 실행 위치/의도를 저장한다.

- Provider 응답 대기 중 Pause: 수신 가능한 실제 결과를 저장하고 새 Run을 시작하지 않음.
- Provider 응답이 ToolCall 그룹인데 아직 실행 전: `PENDING` 호출을 보존하고 PAUSED; **Resume 시 이 호출들을 먼저 완료**해야 하며 그 전에는 새로운 Provider Run 금지.
- 일부 Tool만 완료: 완료한 호출은 재실행하지 않고 나머지 `PENDING`만 Resume. 결과 불명인 호출은 자동 재실행하지 않음.
- final/question terminal의 의미는 Pause 요청으로 덮어쓰지 않음.
- Resume 시 기존 Run의 Provider 요청을 다시 전송하는 것이 아니라 **완결된 기록에서 다음 작업 지점을 복원**함.

**Contract metadata:** CTRL-03/04, DATA-09/10, TOOL-04 (§4, 상세 [control-pause-v0.1.md](control-pause-v0.1.md)). 실제 status enum, DB lease, Pause timeout 및 Cancel 의미는 검토 후 결정. **V3-045에 따라 P0의 Pause는 프로세스 내 상태로 시작하고, 영속 ControlState·fencing은 P2.** 크래시 후 중복 방지는 Run/Tool의 영속 표시(T3/T6/T-R)가 담당한다. question 대기는 미완결 `ask_user` call로 표현된다(V3-044).

## 17. Data v0.2 / Control–Prompt–Provider v0.1 설계 진전 (2026-10-13)

**현 상태: 독립 설계 검수 후보 · 아직 확정/구현 전.** 기존 V3-001~038 원칙을 새 결정을 한 것처럼 바꾸지 않는다.

- **Data:** [v0.2](data-contract-v0.2.md)에 Event kind 후보, Run `PREPARED → DISPATCH_CLAIMED → RECEIVED → PROJECTED`, Tool `PENDING → EXECUTION_CLAIMED → terminal/UNKNOWN`, 단일 PromptSnapshot CAS, 트랜잭션 T0~T10 및 공개 Port를 정의.
- **Provider 경계:** [Control–Prompt–Provider v0.1](control-prompt-provider-v0.1.md)은 `prepare_native_request`(네트워크 없이 native body 확정)와 `invoke_prepared`(정확히 그 body 송신)를 분리하여 **Data가 실제 송신 요청 원본을 선기록할 수 있게** 설계.
- **Control:** 같은 Conversation 단일 실행 흐름, final/question 종료, ToolCall 완전 그룹 commit 후 Tool 실행, UNKNOWN/불완전 그룹에서는 다음 Run 거부, Pause 시 미실행 Call만 재개.
- **Prompt:** complete group/source coverage 검증과 최신 CurrentState 재조회. Native 변환이 Provider 규칙과 충돌하면 전송 전에 거부.
- **Open:** SDK가 실제 wire body를 제어할 수 있는지, 혼합 final/question/tool output 처리, Provider-native role 배치, 실행 lease, raw 보안, Snapshot–Run prepare 원자성, 스트리밍.
- **Test selection:** 신규 공개 계약이 실제 구현된 이후 변경된다면 V3-038에 따라 전체 pytest; 문서 초안 작성 자체는 pytest 실행 대상이 아니다.

## 18. 모듈 경계 개정 — 2026-10-13 (V3-039~046)

설계 검토 결과 **6개 모듈 분해는 유지**하고, 확장성과 경량성을 위해 다음을 바꿨다.

| 문제 | 개정 | 결정 |
| --- | --- | --- |
| Data가 모든 상태를 가진 단일 Port → 계약 변경이 사실상 항상 전체 pytest | Consumer별 sub-port (`ConversationLog`/`ArtifactStore`/`WorkspaceState`/`StateQuery`/`ControlState`) | V3-039 |
| 내부 프로토콜은 상세, 확장 지점은 미정 | `ToolHandler`/`ProviderAdapter`/`EnvironmentPlugin` SPI 우선 확정 | V3-040 |
| `run_command`가 TOOL-05와 ENV-04로 쪼개져 Environment 추가 시 두 모듈 수정 | Environment가 `tools()`로 자기 Tool 제공 | V3-041 |
| Internal Tool이 `run_id/call_id` 트랜잭션을 직접 다룸 | `ToolOutcome` 반환, Dispatcher가 커밋 | V3-042 |
| Prompt가 Data/Tool/Environment를 직접 호출 | 순수 `compose` + 얇은 loader | V3-043 |
| question 판별(CP-01)이 텍스트 해석에 의존 | suspending Tool `ask_user`, 정규화는 `FINAL \| TOOL_CALLS`, 대기는 영속 기록에서 파생 | V3-044 |
| 1인용 단일 프로세스에 다중 worker급 내구성 장치 | P0 프로세스 내 lock(동시 실행) + 송신·부작용 직전 영속 표시와 UNKNOWN 처리(크래시 중복). 분산 lease/fencing/정밀 reconcile은 P2 | V3-045 |
| 문서 간 이름 불일치(`invoke` vs `prepare/invoke_prepared`, `commit_model_output` vs `project_model_output` 등) | 계약은 `api.py` 코드가 원본 | V3-046 |

본 문서 §1·§3·§4·§5~§13은 위 개정을 반영했다. 시그니처는 여전히 PROPOSED이며 첫 코드 Skeleton에서 확정한다.

**독립 검수(NOT_READY) 후속 수정 — 2026-10-13**

| Blocker | 수정 | 위치 |
| --- | --- | --- |
| B1 P0 실행 내구성 | lock(동시 실행)과 영속 표시(크래시 중복)를 구분. T3 `DISPATCH_CLAIMED`·T6 `EXECUTION_CLAIMED`를 P0로 되돌리고 재시작 정리 T-R 정의 | V3-045, §5, [Data v0.2 §1.1·§5·§6·§9](data-contract-v0.2.md) |
| B2 ask_user 대기 | suspending Tool, lock 해제, 원래 `call_id` ToolResult 커밋 후 새 Run, 파생 대기 판별 | V3-044, V3-P17, [CPP §5.1](control-prompt-provider-v0.1.md) |
| B3 native 재생 | 답변 Event와 ToolResult 모두 보존, 렌더링은 ToolResult로 1회, 그룹 인접성 규칙, Provider 배열 계약 테스트 R1–R7 | V3-P18, [CPP §6.1–6.2](control-prompt-provider-v0.1.md) |
| 정리 | `assistant_question` 폐지, raw 보존 기준 OPEN, Environment 등록 관계, Confirmed 범위 명시 | V3-P14/P19, [decisions](decisions.md) |
