# Data Contract v0.2 — Implementable Candidate

> 작성: 2026-10-13 · **READY_FOR_INDEPENDENT_DESIGN_REVIEW / NOT FROZEN / NOT IMPLEMENTED**. 기존 [Data v0.1](data-contract-v0.1.md)의 확정된 불변식은 유지하되, 필드·상태·트랜잭션·오류를 구현 가능한 후보 계약으로 좁힌다. 아래의 **PROPOSED 세부 항목을 사용자 확정 결정으로 승격하지 않는다.**
>
> 관련: [통합 계약](contracts.md) · [Control Pause](control-pause-v0.1.md) · [Current State](current-state-contract-v0.1.md) · [Prompt composition](prompt-composition.md) · [Sandbox implementation](sandbox-implementation-v0.2.md) · [미결정](open-questions.md).
>
> **Owner / Provider:** Data. **Consumers:** Control, Prompt, Internal Tool, Environment(Registry/Binding public Port), Web/CLI(권한 제한 조회). **현재 정의 위치:** 본 문서. **향후 선언 위치 제안:** `src/himari/data/api.py`. 저장소 내부 SQL·Repository는 Data private 구현이다.

## 1. 무엇이 확정되었고 무엇이 제안인가

**기존 CONFIRMED (V3-001, 003~004, 016~020, 024~028):**
- Run은 Provider 요청/응답 호출 **1회**를 표현한다. 여러 Run을 묶는 Task 계층 없음.
- Conversation에는 선택한 정규화 Input/Output Event의 **exact immutable canonical payload**를 보존한다.
- 실제 Provider 요청/응답 원본은 **모든 Run에 대해** 기록. 수신하지 않은 응답은 없다고 기록할 뿐 날조하지 않는다.
- Tool 실행 원본/상태와 Conversation Event/Artifact는 논리적으로 별도다. Provider-native ToolCall/Result는 원래 `call_id` 그룹 연결이 필요하다.
- PromptSnapshot은 **각 Conversation에 가장 최근 구성한 전체 provider-neutral Prompt 1개만** 유지한다. 이전 Run의 원본 provider request는 RunRecord에 남는다.
- Plan/Note는 Data의 **conversation-scoped Artifact**, Environment/Workspace 활성화·Binding은 **Operational State**이다.
- 같은 Conversation에서 Control 실행은 직렬화; 한 Run에서의 독립 ToolCall은 병렬 가능.

**이 v0.2에서 새로 제안하는 내용:** kind enum, 데이터 필드, row/revision 모델, fencing, 원자적 commit Port, error 이름, retention guard, exact raw capture 프로토콜. 이 항목들은 독립 검수 및 결정을 거쳐 Freeze한다.

## 1.1 2026-10-13 개정 — sub-port 분리와 P0 범위 (V3-039, V3-042, V3-045)

- **공개 Port는 Consumer별 sub-port**로 나눈다: `ConversationLog`, `ArtifactStore`, `WorkspaceState`, `StateQuery`, `ControlState`(P2). 저장 백엔드·트랜잭션은 공유할 수 있다. 계약 변경 판정(V3-038)은 sub-port 단위다. 매핑은 §10.
- **Tool 구현은 Data에 쓰지 않는다.** Internal ToolHandler는 `ToolOutcome`을 반환하고 Tool Dispatcher가 DATA-08로 커밋한다(V3-042).
- **P0 범위(V3-045):** 단일 프로세스. Conversation별 in-process lock은 **살아 있는 프로세스 안의 동시 실행만** 막는다. 크래시 후 중복 송신·중복 부작용은 lock이 아니라 **아래 영속 표시**로 막는다.
  - **P0 유지:** T3 `PREPARED → DISPATCH_CLAIMED`(Provider 송신 직전), T6 `PENDING → EXECUTION_CLAIMED`(부작용 있는 Tool 실행 직전). 둘 다 단일 행의 조건부 갱신(`이전 상태일 때만 갱신`)이면 충분하며 epoch·token·lease는 쓰지 않는다.
  - **P0 재시작 정리(T-R):** `DISPATCH_CLAIMED`인데 응답 기록이 없는 Run → `OUTCOME_UNKNOWN`. `EXECUTION_CLAIMED`인데 terminal이 없는 Tool → `UNKNOWN`. **둘 다 자동 재송신·재실행하지 않는다.** claim 전 `PREPARED` Run → `ABANDONED_NOT_SENT`, claim 전 `PENDING` Tool → 재개 시 실행 가능.
  - **P2 연기:** `control_generation` fencing, lease, ControlState(T10), 다중 worker claim 경합, UNKNOWN 정밀 reconcile.
  - 새 상태 값은 추가하지 않는다. 위 상태는 모두 이 문서의 기존 RunStatus/ToolExecutionStatus다.
- **ask_user (V3-044):** suspending Tool. 대기 여부는 저장된 상태 값이 아니라 기존 기록에서 파생한다(§6, V3-P17).
- **유지:** Run별 원본 요청·응답 보존(V3-018), 송신 전 raw 저장, 원자 커밋(T1/T2/T4/T5/T7/T7a/T8/T9), 불완전 그룹 재생 금지.
- **P0 원본 요청 보존 기준은 미확정**(V3-P14, DR-02). §5 참조.

## 2. 소유권·연결 관계

~~~text
Principal (authenticated ownership)
  └─ Conversation
      ├─ immutable ConversationEvents (seq by conversation)
      ├─ RunRecord[ ]                # one per *intended* provider invocation attempt
      │    └─ ToolExecutionRecords   # one unique (run_id, call_id)
      ├─ PromptSnapshot[0..1]        # last full composed prompt ONLY
      ├─ Artifact[ ]                 # Plan/Note current revision + provenance
      └─ ConversationControlState    # proposed execution cursor/owner fencing
Principal ── Default Workspace Registry ── Conversation/Environment Bindings
~~~

**Source of Truth:**

| Subject | Canonical Data | Not authoritative |
| --- | --- | --- |
| Exact chat events and ordering | ConversationEvents | UI projection, Prompt summary, timestamps |
| Actual provider request/received response | RunRecord raw request/response body | normalized assistant message, PromptSnapshot |
| Actual Tool intent / effect / model-facing result | ToolExecutionRecords | assistant text, UI display, optional Conversation ToolResult |
| Most recently compiled provider-neutral input | PromptSnapshot | old snapshots (not retained), per-Run native raw bytes |
| Current Plan/Note | Artifact revision | Conversation message or exported workspace file |
| Workspace registrations/Bindings/Current cwd | Data OperationalState | Prompt CurrentState / model-supplied path |
| OS connectivity and file bytes | Environment/current filesystem | persisted online guess or a file path string |

**Temporal semantics:** reading a consistent Data basis does not imply OS files/Environment reachability are unchanged. Environment revalidates before effects.

## 3. Stable IDs and ordering

IDs are opaque and unique within their documented scope; format/UUID generation remains adapter-internal. **No ID is an authorization bearer.**

| Identifier | Scope and invariant |
| --- | --- |
| `conversation_id` | durable principal-owned Conversation ID |
| `event_id` | immutable Event ID; unique across Data scope |
| `sequence` | monotonically increasing integer **per Conversation**, allocated atomically on append; no rewrite/reindex |
| `run_id` | one Provider invocation intent/attempt; unique; not Task |
| `call_id` | Provider's id within one Run (exact form retained), plus ordinal; unique pair (`run_id`, `call_id`) |
| `execution_id` | Data's unique ToolExecution row identity |
| `snapshot_revision` | CAS version for the Conversation's single latest snapshot |
| `artifact_revision` | CAS version scoped to an Artifact |
| `control_generation` | fencing token for one Conversation driver (implementation candidate) |

Use **`sequence`**, not wall-clock timestamps or order of Tool completion, for Event chronology. Original ToolCall order is stored as **`ordinal`**. Native replay must use it even when ToolResults completed out of order.

## 4. ConversationEvent — exact canonical events

**Proposed public DTO:**

~~~text
ConversationEvent:
  event_id: OpaqueId
  conversation_id: OpaqueId
  sequence: PositiveInt
  kind: EventKind
  payload_schema_version: PositiveInt
  payload: CanonicalEventPayload
  origin_run_id?: OpaqueId
  created_at: Timestamp  # observational, not ordering authority
~~~

**Proposed initial EventKinds:**

| Kind | Minimum canonical payload | Origin |
| --- | --- | --- |
| `user_message` | exact text + ordered attachment file/version refs, sender/source identity, **`answers_call?: {run_id, call_id}`**(ask_user 답변일 때, V3-P17) | trusted UI/CLI |
| `assistant_final` | final content blocks in order, normalized content type | Provider normalized output |
| `assistant_tool_calls` | ordered ToolCall list `{call_id,ordinal,tool_id,arguments}` + optional commentary blocks (V3-P16). question도 여기에 `ask_user` 호출로 기록 | Provider normalized output |
| `tool_result` | `{origin_run_id,call_id,model_result}` in exact configured record form | ToolExecution completion |

Commentary text that arrives **with** tool calls is kept as blocks inside the same `assistant_tool_calls` Event (V3-P16) and is never promoted to a final Event. Partial streaming, progress and message editing need separate deliberate event kinds or ephemeral presentation projection. These four kinds are sufficient for the first non-streaming skeleton but **not a confirmed exhaustive enumeration**. **`assistant_question`은 V3-044로 폐지**: question은 `ask_user`를 포함한 `assistant_tool_calls`로 기록되고, 답변은 `answers_call`을 가진 `user_message`와 해당 `tool_result`로 기록된다. 정규화 출력 `FINAL`은 `assistant_final`, `TOOL_CALLS`는 `assistant_tool_calls`에 1:1로 대응한다.

- Event payload is **kind-specific canonical typed value**. Exactness means **once committed the normalized payload is never rewritten**, not that it contains the original Provider wire bytes.
- Wire-serialization original lives in RunRecord. A Tool's original arguments and original call order must be preserved in ToolExecution and assistant ToolCall Event; normalized JSON must not silently drop fields that Provider native replay requires.
- **User uploads:** Event stores immutable versioned attachment references, not arbitrary unbounded bytes. File content is governed by FILE contracts.
- Conversation Event may omit some Tool results under a defined recording policy; the ToolExecution Record always exists and Prompt repairs/validates the complete native group from Execution records.
- No edits to past Event sequence for deletion/retention; purge is separate lifecycle authority and does not reinterpret append-only during normal operation.

## 5. RunRecord — exact one Provider attempt, raw body authority

**Proposed DTO:**

~~~text
RunRecord:
  run_id
  conversation_id
  trigger_refs: ordered[EventRef | CompletedExecutionGroupRef]
  prompt_basis_ref: {event_sequence, snapshot_revision?, artifact_revisions?, state_basis_token?}
  provider_key; model_id; normalized_call_config
  raw_request_body: ImmutableBytesRef
  raw_response_body?: ImmutableBytesRef
  raw_content_type?: str
  response_content_type?: str
  status: RunStatus
  provider_usage?: ProviderReportedUsage
  normalized_output_ref?: EventRef | ToolCallGroupRef
  error_code?: RunError
  timestamps: {prepared_at, dispatch_marked_at?, received_at?, completed_at?}
~~~

**RunStatus proposal:**

| From | Transition | Meaning / recovery |
| --- | --- | --- |
| none | `PREPARED` | native request body is durably recorded and **no dispatch claim yet**. Safe only after authoritative proof of no send; do not blindly resubmit an old Run |
| PREPARED | `DISPATCH_CLAIMED` | **P0 포함(V3-045).** durable mark immediately **before one external send attempt** (P0: 단일 행 조건부 갱신, P2: fenced claim). Crash after mark means send may or may not have occurred ⇒ 재시작 시 `OUTCOME_UNKNOWN` |
| DISPATCH_CLAIMED | `RECEIVED` | actual provider response body durably stored, no guarantee normalized/projection complete |
| DISPATCH_CLAIMED | `FAILED_NO_RESPONSE` or `OUTCOME_UNKNOWN` | known failure class vs uncertain send/response; raw response absent |
| RECEIVED | `PROJECTED` | normalized assistant Event/tool-group and pending ToolExecution Records committed idempotently |
| RECEIVED | `PROJECTION_FAILED` | raw body preserved, native or schema error; fix/reconcile without making another Provider call |
| PREPARED | `ABANDONED_NOT_SENT` | only when no send was ever claimed/proven; control may compose **a new Run**, not reuse attempt ID |

**Critical no-double-call rule:** prepare native Provider request before dispatch; persist `PREPARED`; durably mark `DISPATCH_CLAIMED` (P0: single-row conditional update; P2: fenced claim); send at most once per Run. After crash in `DISPATCH_CLAIMED` with no response, **do not resend automatically**, regardless of TCP error appearance. Provider adapter retries must be disabled unless the action is explicitly modelled as a separate new Run with its own persisted raw request and the policy authorizes it.

The exact set of provider transport headers that are *not part of raw request body* and streaming semantics must be explicit. API credentials, Authorization headers, secret connection settings and unbounded debug logs are not captured as Run body. **Do not redact bytes *inside* a request and still claim that redacted payload is exact**: confidential model body content must be protected with Data access controls/encryption, and inability to preserve securely must cause the request to fail before dispatch.

**Capture boundary — P0 기준 미확정 (V3-P14 OPEN, DR-02):** Provider는 네트워크 없이 `PrepareNativeRequest`로 송신할 request를 만들고, Data는 그것을 **송신 전에** 영속한다. 저장물이 무엇과 같은지는 아직 정하지 않았다.

| 후보 | 저장물 | 비용 |
| --- | --- | --- |
| A. wire-exact | transport가 실제 송신한 body와 바이트 동일. 송신은 저장된 bytes를 그대로 사용 | 공식 SDK의 재직렬화를 우회해야 할 수 있음 |
| B. adapter-serialized | Adapter가 transport에 넘긴 직렬화 body (semantic 원본) | SDK 사용 가능, wire 바이트 동일성은 보장 안 함 |

불변 규칙: RunRecord는 `raw_representation`(예: `wire_exact` / `adapter_serialized`)을 기록하고 **실제보다 강한 정확성을 주장하지 않는다**. JSON 재직렬화를 물리적 wire bytes라고 표기하지 않는다.

**P0 `OUTCOME_UNKNOWN` 처리:** Run의 외부 효과는 Provider 과금뿐이고 Tool은 HIMARI가 실행하므로, `OUTCOME_UNKNOWN` Run은 ToolExecution을 만들지 않는다. 같은 Run은 재송신하지 않으며, **다음 사용자 입력 또는 명시적 재개에서만 새 Run**을 구성한다(자동 재개 없음).

**Provider streaming (future):** final assembled body alone may not represent streamed chunks/trailers. The initial non-streaming Port can store full received body; streaming requires separate ordered raw chunk provenance and a defined terminal marker without modifying earlier raw bytes.

## 6. ToolExecutionRecord — one original call, no fabricated replay

**Proposed DTO:**

~~~text
ToolExecutionRecord:
  execution_id
  conversation_id
  origin_run_id
  call_id
  ordinal
  tool_id
  canonical_arguments
  original_arguments_repr?
  workspace_binding_id?
  status: ToolExecutionStatus
  raw_execution_result?
  model_result?
  conversation_event_id?
  artifact_refs?
  created_at; started_at?; finished_at?
~~~

- Unique constraint: **(origin_run_id,call_id)**, plus unique `(origin_run_id,ordinal)`. ToolCall group admission rejects duplicate IDs or ordinals.
- Intents are inserted as `PENDING` in **the same transaction** as the assistant ToolCall Event. No Tool execution is launched before this transaction commits.
- **P0 포함(V3-045).** `effect_profile`이 `Read`가 아닌 Tool은 handler 호출 **전에** `PENDING → EXECUTION_CLAIMED`를 영속한다(단일 행 조건부 갱신). 크래시 후 terminal 없이 `EXECUTION_CLAIMED`이면 재시작 시 `UNKNOWN`. 프로세스가 실제로 시작되지 않았어도 `UNKNOWN`이 될 수 있으며, 이 불확실성이 중복 부작용보다 낫다.
- `Read` Tool과 Internal Tool(효과가 결과와 같은 Data 트랜잭션 T8로 커밋됨)은 claim 없이 `PENDING`에서 바로 terminal로 갈 수 있다. 크래시 후 `PENDING`이면 다시 실행해도 안전하다.
- **ask_user (suspending, V3-044 / V3-P17):** `ask_user` ToolExecution은 claim하지 않고 `PENDING`으로 남는다(부작용 없음, `UNKNOWN` 불가). **최신 `PROJECTED` Run의 ToolCall 그룹이 단일 `ask_user`이고 그 ToolExecution이 `PENDING`이면 Conversation은 `WAITING_FOR_USER`**다. 저장된 상태 값이 아닌 파생 상태이므로 재시작 후에도 같은 조회로 복원된다. 답변은 T7a로 완결한다.
- Outcome states: `SUCCEEDED, FAILED, DENIED, CANCELLED` include a completed model-facing ToolResult when the tool contract can truthfully produce one. `UNKNOWN` is **not equivalent to a synthetic failure ToolResult** and blocks native group replay (따라서 다음 Run) until explicitly reconciled. P0에서는 사용자의 명시 조치로만 해소하며 그 방식은 미결정(Q-37), 정밀 reconcile은 P2.
- The model-facing result is derived **once** from actual execution / deterministic denial and committed alongside terminal state. Optional exact Conversation ToolResult Event is appended in the same transaction; no data may report completed while its configured mandatory Event is absent.
- Completion time is not native group ordering. Prompt reconstructs native ToolResult ordered by original ToolCall ordinal and Provider requirements, and rejects a missing/unpaired result instead of guessing.

## 7. Single latest PromptSnapshot — complete provider-neutral input

**Proposed DTO:**

~~~text
PromptSnapshot:
  conversation_id                         # PRIMARY KEY, only one
  revision                                # CAS version
  schema_version
  provider_neutral_full_prompt            # stable prefix + history + current-state at time assembled
  compressed_history_section
  exact_history_ref_range
  completed_tool_group_coverage
  covers_through_sequence
  source_event_sequence
  artifact_revision_basis
  operational_state_basis_token?
  related_run_id?
  updated_at
~~~

- **Full prompt, not summary only:** latest compiled provider-neutral input includes stable instructions, compressed old conversation, exact recent Event history and the Current State **as of that composition**. It may contain tool catalog and model-relevant settings as structured segments, but is not the actual provider-native raw request.
- **One snapshot per Conversation:** replace atomically with revision CAS; no historic snapshot rows, backups or shadow history created by Data's normal write path. Old **raw native requests** remain on each Run.
- Coverage excludes partial ToolCall groups and records exact boundary, so a summary cannot hide missing model-visible ToolResults.
- A snapshot's Current State can go stale immediately; Prompt **must rebuild the latest Current State** from authorized Data and optional Environment observation before each new Run. The old Current State is not an execution permission.
- The latest snapshot is a *cache artifact*. Deleting/replacing it must not delete/rewrite Conversation Events or Run raw bodies.
- **Proposal for coherent run prepare:** `replace_latest_snapshot(expected_revision, observed_event_sequence, new_full_input)` and `prepare_run` enforce an event/cursor version check. Exact atomic split across run-prepare and snapshot-write remains subject to review: a crash between these cannot manufacture that an old snapshot was the one actually used; RunRecord prompt basis must pin its actual version, or hold the composed input's digest/basis token.

## 8. Artifact and OperationalState boundary

- Artifacts (Plan/Note) have current `artifact_revision` and original Data ownership; edits should use revision CAS. An Internal Tool's mutation **and its ToolExecution terminal result**, plus configured Conversation Event, should be committed atomically with `(run_id,call_id)` idempotency.
- OperationalState (Workspace Registry, per-Conversation Activation/Binding, principal-owned user root/cwd mapping, default selection) is **not** an Artifact, and changing it must not overwrite Plan/Note.
- Data authenticates the supplied trusted principal/conversation scope against persisted ownership; Workspace/Binding ID in Tool arguments alone is not a grant. Environment **rechecks filesystem identity and access** before effects.
- CurrentStateProjection comes from Data read basis plus optional Environment observation, owned as a read projection by Prompt; never a second source of truth.

## 9. Transaction ledger — operations that must be atomic

| Txn | Boundary / owner | Before / after invariant |
| --- | --- | --- |
| T0 | Create Conversation + authorized initial principal/workspace mapping | no orphan owned conversation / wrong-principal root |
| T1 | Append user or other canonical Event | allocate ordered `sequence` and immutable payload atomically. ask_user 대기 중 들어온 사용자 입력은 T1이 아니라 T7a로만 기록(입력당 한 경로) |
| T2 | Prepare Run | commit raw native request bytes + trigger/basis + PREPARED *before* any send |
| T3 | Mark provider send (**P0**: 단일 행 조건부 갱신 / P2: fenced claim) | `PREPARED → DISPATCH_CLAIMED` 영속 후에만 송신. 이미 claim된 Run은 다시 송신 불가 |
| T4 | Receive raw response | store exact received bytes and `RECEIVED` together |
| T5 | Project response | commit normalized assistant Event, exact ordered ToolCall group, and *all* pending ToolExecution rows atomically; assign `PROJECTED` |
| T6 | Tool claim before effects (**P0**, `Read`·Internal·`ask_user` 제외) | `PENDING → EXECUTION_CLAIMED` 영속 후에만 handler 호출. 이미 claim된 call은 다시 실행 불가 |
| T7 | Tool complete | terminal state + truthfully derived `model_result` + configured exact ToolResult Event in one Data transaction |
| T7a | ask_user 답변 (V3-044 / V3-P17) | `user_message` Event(`answers_call`) + 해당 ToolExecution `PENDING → SUCCEEDED` + 답변에서 파생한 `model_result` + `tool_result` Event를 한 트랜잭션으로. 이미 완결된 call이면 `ALREADY_COMMITTED`이고 입력은 일반 `user_message`로 처리 |
| T-R | 재시작 정리 (P0, V3-045) | `DISPATCH_CLAIMED`(응답 없음) → `OUTCOME_UNKNOWN`, `EXECUTION_CLAIMED`(terminal 없음) → `UNKNOWN`, claim 전 `PREPARED` → `ABANDONED_NOT_SENT`. 어떤 재실행도 하지 않음 |
| T8 | Internal Tool Artifact mutation (Dispatcher가 `ToolOutcome`으로 커밋) | CAS Artifact revision + Tool final state + model_result + optional Event as a single transaction |
| T9 | PromptSnapshot replace | latest-only CAS; no impact on Events, raw Run records or past Tool records |
| T10 | **(P2)** Control pause/claim cursor (proposal) | Conversation execution state/lease/epoch one-writer proof, separate from Run statuses |

**External effects are not SQLite transactions.** T3 and the Provider HTTP call cannot commit atomically; T6 and an OS command cannot commit atomically. Crash recovery must preserve whether effect is proven or uncertain rather than retrying blind.

**Lock ordering / concurrency proposal:** Data transaction boundaries are short and never span Provider network I/O or OS child process lifetime. Control serialization is per Conversation, but Tool outcomes may be concurrent; tool result Event sequences are allocated at commit, and native replay reorders by original ordinal. Shared Workspace Scheduler synchronization is owned by Tool, not Data.

## 10. Data Public Ports — candidate implementation interface

> **2026-10-13:** 이 표는 [`src/himari/data/api.py`](../src/himari/data/api.py)로 선언되었고 이후 코드가 원본이다(V3-046). 코드 선언 시 정리한 이름: `complete_tool_execution`은 DATA-08 `commit_tool_outcome` 하나로 통합(Artifact 변경이 없는 완료도 같은 경로), 조회용 `read_run`·`read_tool_executions`·`record_projection_failure`, WorkspaceState의 `resolve_default_binding`을 추가했다.

All methods take a trusted, authenticated scope or opaque capability created by the trusted composition root, **not model-provided principal strings**. Port names are PROPOSED:

| Contract ID | Sub-port (V3-039) | Candidate Port | Callers | Effect |
| --- | --- | --- | --- | --- |
| DATA-01 | `ConversationLog` | `append_event(scope,payload,expected_sequence?,idempotency_key?)` / `read_events(scope,range)` | Control, Prompt loader, UI read | atomic ordered append / scoped immutable read |
| DATA-02 | `ConversationLog` | `prepare_run(scope,raw_body,raw_representation,trigger_refs,basis,model_config)` / `mark_dispatch(scope,run_id)` (P0, T3) / `record_raw_outcome(scope,run_id,body_or_error)`; P2에서 `control_generation` 인자 추가 | Control | Provider send ledger. 크래시 후 중복 송신 방지는 `mark_dispatch` 영속 표시가 담당 |
| DATA-03 | `ConversationLog` | `project_model_output(scope,run_id,normalized_output)` / `claim_tool_execution(scope,run_id,call_id)` (P0, T6) / `complete_tool_execution(scope,run_id,call_id,outcome)` / `answer_pending_call(scope,run_id,call_id,user_payload)` (T7a) / `find_waiting_call(scope)` / `recover_interrupted(scope)` (T-R) | Control, Tool Dispatcher | group intent atomic; effect/response provenance; ask_user 대기 판별·답변 |
| DATA-04 | `ArtifactStore` | `read_artifact(scope,artifact_id)` / `list_artifacts(scope)` | Prompt loader, Internal ToolHandler(읽기) | current authoritative Plan/Note |
| DATA-05 | `ConversationLog` | `get_latest_snapshot(scope)` / `replace_latest_snapshot(scope,expected_revision,basis,input)` | Prompt/Control | one latest full composed provider-neutral prompt |
| DATA-06 | `StateQuery` | `read_current_state_basis(scope)` | Prompt loader | coherent current authorized persisted projection (읽기 전용 합성) |
| DATA-07 | `WorkspaceState` | `read_binding_authority(scope,binding_id)` | Environment, Tool Dispatcher | authorized and current Workspace/Environment binding metadata |
| DATA-08 | `ConversationLog` (+`ArtifactStore` 같은 트랜잭션) | `commit_tool_outcome(scope,run_id,call_id,outcome)` — outcome의 `artifact_mutations`에 expected revision 포함 | **Tool Dispatcher만** | one atomic Artifact+ToolResult+Event transaction. Tool 구현은 직접 호출하지 않음(V3-042) |
| DATA-09/10 | `ControlState` (**P2**) | `read_control_state(scope)` / `claim_control_execution(scope,expected_generation)` / `transition_control_state(scope,claim,cursor)` | Control | proposed one-writer execution cursor/fencing |
| DATA-11 | `WorkspaceState` | `resolve_conversation_workspace(scope)` | Environment | principal-owned root record + current Conversation relative cwd |

**Module purity:** no other module directly imports Data's DB connection, repository objects, SQL schema or migration helpers. Data DTOs crossing Port boundaries are provider-neutral immutable values. Authenticated Web/CLI read endpoints use deliberately reduced DTO projections rather than raw protected records by default.

## 11. Errors and privacy policy boundary

**Typed error classes (PROPOSED):**

| Error | Example |
| --- | --- |
| `NOT_AUTHORIZED` / `NOT_FOUND` | foreign Conversation or missing resource; public exposure indistinguishability policy TBD |
| `STALE_BASIS` / `VERSION_CONFLICT` | event sequence, snapshot basis, artifact revision or control generation mismatch |
| `ALREADY_COMMITTED` / `DUPLICATE_IDENTITY` | same idempotency key replay vs conflicting args/hash on same call_id |
| `INVALID_EVENT_GROUP` | Tool call IDs/ordinals malformed or group mismatched |
| `INCOMPLETE_TOOL_GROUP` | ToolResult absent, UNKNOWN, or native replay has broken parent group |
| `RUN_SEND_UNCERTAIN` / `TOOL_EFFECT_UNCERTAIN` | crash window with unknowable external effect; never retry as if definitely unsent |
| `RAW_PERSISTENCE_UNAVAILABLE` | unable to store protected raw bytes before Provider send; **do not send** |
| `INVALID_STATE_TRANSITION` | completion without recorded intent, stale owner or impossible Run/Tool transition |

**Privacy/security requirements for implementation review:**
1. Restrict Run raw body, Tool raw effect output and exact sensitive Events to authenticated principals/services. No implicit inclusion in UI logs, exception messages, plaintext diagnostics or ToolResult.
2. Provider transport headers/tokens must not be stored as body. Body itself may contain user secrets and must be stored faithfully but with access control, encryption and eventual deletion/lifecycle authority; no false promise of secure at-rest until implemented.
3. Crash between bytes persistence and metadata commit: ensure blob/object durability and orphan cleanup without silently losing the raw record. The blob storage format/DB choice is not frozen.
4. Redaction for diagnostics/projections must not mutate immutable canonical records or raw bytes.
5. Purge/lifecycle future policy is explicit exception to ordinary immutable record retention; model-authored tool command cannot exercise purge authority.

## 12. Minimum contract tests / acceptance

- D01 same Conversation concurrent append produces unique increasing sequences, immutability.
- D02 foreign principal cannot read/write Events/Run raw/Bindings/Artifacts even with valid IDs.
- D03 one `RunRecord` permits **at most one send**: `DISPATCH_CLAIMED` is persisted before send; crash after it → `OUTCOME_UNKNOWN` on restart, never resent. (lock만으로 통과시키지 않는다: 프로세스 재시작 시나리오로 검증)
- D04 raw request recorded before dispatch with an honest `raw_representation` (기준은 DR-02에서 결정; wire-exact 선택 시 송신 bytes 동일성까지 검증); token/header not logged or embedded in request-body record.
- D05 received raw response survives projection failure and later idempotent projection; no second Provider invocation.
- D06 two tool calls project atomically with unique call IDs and stable ordinal; none executes if pending group transaction fails.
- D07 out-of-order completion keeps Event chronology by sequence but native replay pairs results by original ordinal.
- D08 a tool completing twice cannot duplicate results; a call left `EXECUTION_CLAIMED` after crash is never re-run (becomes `UNKNOWN`, D17). A never-claimed `PENDING` call is not a rerun and may execute once on resume (D20).
- D09 configured Conversation ToolResult matches committed model_result exactly; omitted Event has still durable Execution source.
- D10 Internal Plan mutation + ToolResult + Event all commit or all rollback; Artifact revision conflict does not silently report success.
- D11 replacement of latest full PromptSnapshot keeps **one** row, preserves all historical native Run request bodies and exact Conversation Events.
- D12 incomplete/UNKNOWN tool group cannot be silently passed to Provider as complete.
- D13 stored current Workspace Binding ≠ OS permission proof; stale/revoked binding denied at Environment Tool effect boundary.
- D14 **(P2)** lease/Control generation fencing prevents stale worker from committing after ownership change.
- D15 raw storage unavailable prevents Provider send.
- D16 deletion after future purge is separated from normal append-only behavior; no path from model ToolCall into purge.
- D17 crash after `EXECUTION_CLAIMED` → `UNKNOWN` on restart; handler not re-invoked; next Run blocked until explicit resolution.
- D18 restart while waiting: pending `ask_user` is identified from persisted rows only (`find_waiting_call`); answer commits `user_message` + ToolResult + `tool_result` Event atomically, then exactly one new Run.
- D19 second answer after the call is completed becomes an ordinary `user_message`; never a second ToolResult for the same `call_id`.
- D19a one user input produces exactly one `user_message`: T7a while an `ask_user` is waiting, T1 otherwise; never both.
- D20 Read/Internal Tool left `PENDING` after crash may be re-executed; effectful Tool left `PENDING` (never claimed) may be executed once on resume.

**Test execution policy:** [V3-038](testing-policy.md): public Data contract create/change/remove → full pytest once code exists; Data implementation only with same contract → Data-module pytest. No executable v3 tests yet.

## 13. Decisions still required before freezing

1. **DR-01:** Final EventKind vocabulary (`assistant_question` 폐지, `answers_call` 필드 확정 포함), typed content blocks (text/images/files), exact Assistant question/final semantics and commentary history.
2. **DR-02:** **P0 raw 보존 기준 선택(V3-P14: wire-exact vs adapter-serialized)**, guarantee of captured Provider request bytes and compatibility with actual chosen SDK/network transport; how to persist model-visible continuation and future streaming raw chunks.
3. **DR-03:** P0의 T3/T6/T-R 트랜잭션 경계와 UNKNOWN의 사용자 명시 해소 방식(Q-37). Control lease/fencing·fenced claim은 **P2(V3-045)**.
4. **DR-04:** Full Tool group commit order, how to treat UNKNOWN and cancellation without inventing results, canonical result/error structures.
5. **DR-05:** Latest PromptSnapshot CAS with Run prepare and source StateBasis consistency; how exactly native replay coverage and snapshot cursor refer to ToolExecution groups.
6. **DR-06:** Artifact revision/CAS, user mutation and future Memory storage/lifecycle.
7. **DR-07:** Concrete DB/Blob storage choice, at-rest protection, FileAsset exact immutable publish semantics, delete/purge/retention.
8. **DR-08:** Scope principal authentication, Conversation owner vs Workspace shared state, single-writer execution claim and cross-Conversation Tool reconciliation.
9. **DR-09:** Specific public DTO type signatures per **sub-port**(V3-039), declared as code in `data/api.py`(V3-046); migration/bootstrap and fake implementation test contracts.

**Freeze gate:** independently review these nine items, update decisions with only explicitly approved choices, and match [Control–Prompt–Provider](control-prompt-provider-v0.1.md) to this Data Port's exact Run/raw preparation and Tool group barriers.
