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
- **P0 범위(V3-045):** 단일 프로세스, Conversation별 in-process lock. 아래 항목은 상태 enum·필드 자리만 남기고 **P2로 연기**한다.
  - T3 dispatch claim CAS와 `DISPATCH_CLAIMED` 상태 → P0는 `PREPARED → RECEIVED | FAILED_NO_RESPONSE → PROJECTED | PROJECTION_FAILED`.
  - T6 `EXECUTION_CLAIMED` durable claim → P0는 `PENDING → terminal`. 크래시로 결과를 모르면 `UNKNOWN`으로 기록하고 다음 Run을 막는 규칙은 유지하되, 정밀 reconcile 절차는 P2.
  - T10 `ControlState`, `control_generation` fencing, lease → P2.
- **유지:** Run별 원본 요청·응답 보존(V3-018), 송신 전 raw 저장, 원자 커밋(T1/T2/T4/T5/T7/T8/T9), 불완전 그룹 재생 금지.
- 원본 요청 보존 기준을 "Adapter가 transport에 넘긴 직렬화 body"로 완화하는 안은 V3-P14로 검토한다.

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
| `user_message` | exact text + ordered attachment file/version refs, sender/source identity | trusted UI/CLI |
| `assistant_final` | final content blocks in order, normalized content type | Provider normalized output |
| `assistant_question` | question content blocks in order | Provider normalized output |
| `assistant_tool_calls` | ordered ToolCall list `{call_id,ordinal,tool_id,arguments}` | Provider normalized output |
| `tool_result` | `{origin_run_id,call_id,model_result}` in exact configured record form | ToolExecution completion |

Comments/partial streaming are **not** silently promoted to final Events; commentary, progress and message editing need separate deliberate event kinds or ephemeral presentation projection. These five kinds are sufficient for the first non-streaming skeleton but **not a confirmed exhaustive enumeration**.

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
| PREPARED | `DISPATCH_CLAIMED` | **(P2, V3-045)** durable claim immediately **before one external send attempt**. Crash after claim means send may or may not have occurred. P0는 이 상태 없이 `PREPARED`에서 바로 송신 |
| DISPATCH_CLAIMED | `RECEIVED` | actual provider response body durably stored, no guarantee normalized/projection complete |
| DISPATCH_CLAIMED | `FAILED_NO_RESPONSE` or `OUTCOME_UNKNOWN` | known failure class vs uncertain send/response; raw response absent |
| RECEIVED | `PROJECTED` | normalized assistant Event/tool-group and pending ToolExecution Records committed idempotently |
| RECEIVED | `PROJECTION_FAILED` | raw body preserved, native or schema error; fix/reconcile without making another Provider call |
| PREPARED | `ABANDONED_NOT_SENT` | only when no send was ever claimed/proven; control may compose **a new Run**, not reuse attempt ID |

**Critical no-double-call rule:** prepare native Provider bytes before dispatch; persist `PREPARED`; acquire once-only dispatch claim using durable compare-and-set; send at most once per Run. After crash in `DISPATCH_CLAIMED` with no response, **do not resend automatically**, regardless of TCP error appearance. Provider adapter retries must be disabled unless the action is explicitly modelled as a separate new Run with its own persisted raw request and the policy authorizes it.

The exact set of provider transport headers that are *not part of raw request body* and streaming semantics must be explicit. API credentials, Authorization headers, secret connection settings and unbounded debug logs are not captured as Run body. **Do not redact bytes *inside* a request and still claim that redacted payload is exact**: confidential model body content must be protected with Data access controls/encryption, and inability to preserve securely must cause the request to fail before dispatch.

**Capture boundary proposal:** Provider exposes a `PrepareNativeRequest` operation (no network) that returns precisely the body bytes it would send and a trusted opaque transport descriptor. Data persists those bytes *before* Provider performs network I/O. The sending operation must use **the recorded bytes, not rebuild them** after persistence. Transport SDKs unable to guarantee native request body equality require an alternate adapter or declared weaker/raw representation for independent review; do not pretend a JSON re-serialization equals physical wire bytes.

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
- **(P2, V3-045)** Proposed dispatch transition `PENDING → EXECUTION_CLAIMED` happens durably **before** handing an effectful call to the Tool adapter. This window may produce `UNKNOWN` after a crash even if process never spawned; that uncertainty is preferred to duplicated side effects.
- Outcome states: `SUCCEEDED, FAILED, DENIED, CANCELLED` include a completed model-facing ToolResult when the tool contract can truthfully produce one. `UNKNOWN` is **not equivalent to a synthetic failure ToolResult** and blocks native group replay until explicitly reconciled under a later recovery contract.
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
| T1 | Append user or other canonical Event | allocate ordered `sequence` and immutable payload atomically |
| T2 | Prepare Run | commit raw native request bytes + trigger/basis + PREPARED *before* any send |
| T3 | **(P2)** Claim provider send | one valid `DISPATCH_CLAIMED` CAS per Run; stale Control owner rejected |
| T4 | Receive raw response | store exact received bytes and `RECEIVED` together |
| T5 | Project response | commit normalized assistant Event, exact ordered ToolCall group, and *all* pending ToolExecution rows atomically; assign `PROJECTED` |
| T6 | **(P2)** Tool claim before effects | `PENDING → EXECUTION_CLAIMED` claim/epoch in Data; no duplicate dispatch under concurrent workers |
| T7 | Tool complete | terminal state + truthfully derived `model_result` + configured exact ToolResult Event in one Data transaction |
| T8 | Internal Tool Artifact mutation (Dispatcher가 `ToolOutcome`으로 커밋) | CAS Artifact revision + Tool final state + model_result + optional Event as a single transaction |
| T9 | PromptSnapshot replace | latest-only CAS; no impact on Events, raw Run records or past Tool records |
| T10 | **(P2)** Control pause/claim cursor (proposal) | Conversation execution state/lease/epoch one-writer proof, separate from Run statuses |

**External effects are not SQLite transactions.** T3 and the Provider HTTP call cannot commit atomically; T6 and an OS command cannot commit atomically. Crash recovery must preserve whether effect is proven or uncertain rather than retrying blind.

**Lock ordering / concurrency proposal:** Data transaction boundaries are short and never span Provider network I/O or OS child process lifetime. Control serialization is per Conversation, but Tool outcomes may be concurrent; tool result Event sequences are allocated at commit, and native replay reorders by original ordinal. Shared Workspace Scheduler synchronization is owned by Tool, not Data.

## 10. Data Public Ports — candidate implementation interface

All methods take a trusted, authenticated scope or opaque capability created by the trusted composition root, **not model-provided principal strings**. Port names are PROPOSED:

| Contract ID | Sub-port (V3-039) | Candidate Port | Callers | Effect |
| --- | --- | --- | --- | --- |
| DATA-01 | `ConversationLog` | `append_event(scope,payload,expected_sequence?,idempotency_key?)` / `read_events(scope,range)` | Control, Prompt loader, UI read | atomic ordered append / scoped immutable read |
| DATA-02 | `ConversationLog` | `prepare_run(scope,raw_body,trigger_refs,basis,model_config)` / `record_raw_outcome(scope,run_id,body_or_error)`; P2 `claim_dispatch(scope,run_id,control_generation)` | Control | Provider send ledger (P0: in-process lock가 단일 송신 보장) |
| DATA-03 | `ConversationLog` | `project_model_output(scope,run_id,normalized_output)` / `complete_tool_execution(scope,run_id,call_id,outcome)`; P2 `claim_tool_execution` | Control, Tool Dispatcher | group intent atomic; effect/response provenance |
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
- D03 one `RunRecord` permits **at most one send** (P0: lock 기반; P2: claimed send CAS); uncertain send is not retried silently.
- D04 raw request bytes recorded before dispatch and transmitted exactly as captured; token/header not logged or embedded in request-body record.
- D05 received raw response survives projection failure and later idempotent projection; no second Provider invocation.
- D06 two tool calls project atomically with unique call IDs and stable ordinal; none executes if pending group transaction fails.
- D07 out-of-order completion keeps Event chronology by sequence but native replay pairs results by original ordinal.
- D08 a tool completing twice cannot duplicate results; PENDING or effect-claimed after crash never blindly reruns.
- D09 configured Conversation ToolResult matches committed model_result exactly; omitted Event has still durable Execution source.
- D10 Internal Plan mutation + ToolResult + Event all commit or all rollback; Artifact revision conflict does not silently report success.
- D11 replacement of latest full PromptSnapshot keeps **one** row, preserves all historical native Run request bodies and exact Conversation Events.
- D12 incomplete/UNKNOWN tool group cannot be silently passed to Provider as complete.
- D13 stored current Workspace Binding ≠ OS permission proof; stale/revoked binding denied at Environment Tool effect boundary.
- D14 **(P2)** lease/Control generation fencing prevents stale worker from committing after ownership change.
- D15 raw storage unavailable prevents Provider send.
- D16 deletion after future purge is separated from normal append-only behavior; no path from model ToolCall into purge.

**Test execution policy:** [V3-038](testing-policy.md): public Data contract create/change/remove → full pytest once code exists; Data implementation only with same contract → Data-module pytest. No executable v3 tests yet.

## 13. Decisions still required before freezing

1. **DR-01:** Final EventKind vocabulary, typed content blocks (text/images/files), exact Assistant question/final semantics and commentary history.
2. **DR-02:** Guarantee of captured Provider request bytes and compatibility with actual chosen SDK/network transport; how to persist model-visible continuation and future streaming raw chunks.
3. **DR-03:** ~~durable send claim CAS and Control lease/fencing~~ → **P2 연기(V3-045)**. P0에서는 `PREPARED` 상태에서 크래시한 Run을 재송신하지 않고 새 Run으로 대체하는 규칙만 확정하면 된다.
4. **DR-04:** Full Tool group commit order, how to treat UNKNOWN and cancellation without inventing results, canonical result/error structures.
5. **DR-05:** Latest PromptSnapshot CAS with Run prepare and source StateBasis consistency; how exactly native replay coverage and snapshot cursor refer to ToolExecution groups.
6. **DR-06:** Artifact revision/CAS, user mutation and future Memory storage/lifecycle.
7. **DR-07:** Concrete DB/Blob storage choice, at-rest protection, FileAsset exact immutable publish semantics, delete/purge/retention.
8. **DR-08:** Scope principal authentication, Conversation owner vs Workspace shared state, single-writer execution claim and cross-Conversation Tool reconciliation.
9. **DR-09:** Specific public DTO type signatures per **sub-port**(V3-039), declared as code in `data/api.py`(V3-046); migration/bootstrap and fake implementation test contracts.

**Freeze gate:** independently review these nine items, update decisions with only explicitly approved choices, and match [Control–Prompt–Provider](control-prompt-provider-v0.1.md) to this Data Port's exact Run/raw preparation and Tool group barriers.
