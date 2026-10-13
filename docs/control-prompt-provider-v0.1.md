# Control–Prompt–Provider Boundary Contract v0.1

> 2026-10-13 · **REVIEW CANDIDATE / NOT FROZEN / NOT IMPLEMENTED**. This is a companion design to [Data v0.2](data-contract-v0.2.md), not an independent new architecture. Previous confirmed principles in [decisions](decisions.md) take precedence.
>
> **Scope:** Control's per-Conversation run loop, Prompt provider-neutral input, Provider native serialization/invocation/normalization, Data run/tool group persistence, Tool dispatch barrier. Exact v3 Python DTOs/enum values remain PROPOSED.

> **2026-10-13 개정 (V3-042~046):** question은 `ask_user` Tool로 표현하고 정규화 출력은 `FINAL | TOOL_CALLS` 두 가지다(§5). Prompt 핵심은 순수 `compose(PromptInputs)`(§2). P0는 프로세스 내 Conversation lock을 쓰고 dispatch claim·fencing·lease는 P2(§3, §7). Provider 계약은 `ProviderAdapter` SPI로 묶는다.

## 1. Module responsibility and authoritative sources

| Contract | Owner / implementer | Consumer | Source of truth | Output or effect |
| --- | --- | --- | --- | --- |
| CTRL-01 `DriveConversation` | Control | Web/CLI | authenticated User Event + Data Run/Tool state | serialized run loop; final/question stops |
| PROMPT-01 `ComposeModelInput` | Prompt | Control | exact Data Event/Execution/Artifact/current basis + selected Snapshot + Tool definitions | typed provider-neutral model input + coverage proof |
| PROVIDER-02 `PrepareNativeRequest` | Provider adapter | Control | Prompt input + actual target model/capabilities | immutable native request body bytes + send descriptor |
| DATA-02 `PrepareRun` | Data | Control | prepared native bytes, trigger and Prompt basis | one PREPARED RunRecord |
| PROVIDER-01 `InvokePreparedRequest` | Provider adapter | Control | persisted native request bytes and descriptor | at-most-one invocation attempt; raw response/error |
| PROVIDER-03 `NormalizeNativeResponse` | Provider adapter | Control/Data projection | actual received raw response | normalized `FINAL` / ordered `TOOL_CALLS` (question = `ask_user` call) |
| DATA-03 `ProjectModelOutput` | Data | Control | normalized response + raw Run ID | exact assistant Event + all pending Tool intents atomically |
| TOOL-02 `DispatchToolCalls` | Tool | Control | persisted ToolExecution intents + verified Workspace Binding | `ToolHandler.run()` → `ToolOutcome`, Dispatcher가 DATA-08로 커밋 |
| PROMPT-03 `ValidateNativeToolGroup` | Prompt | Control/Provider | exact assistant calls + ToolExecution Records | complete ordered history or typed failure |

No module can silently substitute a private SQL call, native SDK response class, or model-generated Workspace identity for an authorized public Port.

## 2. Provider-neutral model input (PROPOSED)

~~~text
ModelInput:
    stable_prefix: ordered[InstructionBlock]
    conversation: ordered[ConversationHistoryPart]
    current_state: CurrentStateProjection
    tool_catalog: ordered[ToolDefinition]
    trigger_refs: ordered[EventRef | CompletedExecutionGroupRef]
    history_coverage: CoverageProof
    request_options: {model_id, reasoning_effort?, other_supported_options?}
    provider_continuation_ref?: ProviderOpaqueRef
    prompt_basis: {event_sequence, snapshot_revision?, state_token?, artifact_revisions?}
~~~

Constraints:
- Logical segment order is **Stable Prefix → Conversation(compressed old + exact recent) → Current State** as already confirmed. The Provider adapter may use provider-defined metadata/configuration slots rather than inventing a trailing message.
- Trigger refs refer to already persisted inputs/Tool group, with no copying or reordering of original history to a fake “latest” User message.
- Prompt must never silently infer a missing ToolResult from UI, summary, cached model text or incomplete ToolExecution.
- Tool catalog is owned by Tool, used by Prompt to select/authorize model-visible tools and Provider to serialize native schemas. Exposing a tool does not authorize its current execution.
- Data snapshot is last **full** compiled provider-neutral model input only, not the actual Provider wire body. Actual native request bytes are preserved in RunRecord.
- CurrentStateProjection is a time-scoped observation, not authorization. Exact read coherency/version checks are [CurrentState CS-03](current-state-contract-v0.1.md) proposed.

**Prompt 구조 (V3-043 CONFIRMED):** `PromptContextLoader.load(scope) -> PromptInputs`가 Data sub-port·Tool catalog·선택적 Environment 관측을 모으고, `compose(PromptInputs) -> PreparedModelInput`은 I/O 없는 순수 함수다. 위 제약(순서·coverage·Trigger 무복제)은 모두 `compose`의 계약이며 고정 입력으로 테스트한다.

**Output proposal** `PreparedModelInput` includes `ModelInput`, immutable source-basis/coverage proofs and a stable digest. Provider-specific native body must be compiled **before** Data PREPARED. A version mismatch requires rebuilding, not sending a stale request.

## 3. One Run loop — transaction vs network boundaries

~~~text
0) Trusted user event already committed (Data T1)
1) Control acquires this Conversation's lock (P0: in-process; P2: Data T10 lease/fencing)
2) Control → Prompt.compose()
      Data read exact events/latest snapshot, Execution and State basis;
      Prompt verifies coverage, tool groups, latest State.
3) Provider.prepare_native_request(model_input)  # pure, no network
      Validate support & native role/tool-adjacency protocol.
4) Data T9 replace latest full Snapshot + Data T2 PREPARED native bytes
      Candidate: must pin exact basis; reject stale/unsafe mismatch.
5) (P2: Data T3 claim dispatch) → Provider.invoke_prepared EXACT BYTES
6) Data T4 save RAW response (or typed uncertain/no-response failure)
7) Provider.normalize(raw), then Data T5 project:
     FINAL      → exact final Event → STOP
     TOOL_CALLS → exact group Event + pending intents atomically
                  (ask_user 단독 호출 → WAITING_FOR_USER; 사용자 응답이 그 call을 완결)
8) Tool dispatcher runs original pending calls (P2: T6 claim), executes independently
     when no shared effects; ToolHandler returns ToolOutcome; Dispatcher commits T7/T8.
9) Barrier: ALL original calls have truthfully completed, model-facing results.
     If UNKNOWN/unpaired: BLOCK / RECOVERY, not fabricated completion.
10) New Prompt.compose and **new Run** (restarting at step 2).
~~~

- Control **does not** create a Task/ManagerTurn wrapper. One provider invocation attempt is one Run; tool executions are associated records, not nested Run invocations.
- No DB transaction encloses Provider network I/O or OS Sandbox process lifespan.
- Per-Conversation Control serialization is confirmed. **P0 uses an in-process lock (V3-045)**; driver claim/lease and epoch fencing are P2. Cancel/User input queue and Pause timing are still proposed.
- Trigger of next Run after tools is the **already persisted completed tool group**, not a newly synthesized User Event.
- Changing current Plan/Note/Workspace state between Run composition and Tool effect is rechecked in Data/Environment, not silently trusted from Prompt.

## 4. Provider prepare/invoke boundary: raw correctness

**Port split proposal:**

~~~text
Provider.prepare_native_request(model_input) -> NativePreparedRequest:
    native_body_bytes
    content_type
    transport_descriptor_without_credentials
    capabilities_used
    native_input_digest

Provider.invoke_prepared(native_prepared_request) -> NativeInvocationOutcome:
    raw_response_bytes?  # exact received bytes as actually captured
    response_content_type?
    provider_usage?
    transport_error_or_uncertain?
~~~

- Native serialization and capability validation happen **before** `Data.prepare_run`.
- Data persists exact body that the Provider adapter will send. The Provider adapter must not regenerate body from Python dict after Data commit, even if semantically equivalent.
- Headers or transport auth are supplied by trusted Provider adapter at send time; they must not be serialized into Data's native body capture.
- A client SDK with unobservable request-body reserialization must be adapted or reviewed explicitly; never claim the stored JSON is byte-exact wire if it is only a semantic equivalent.
- Disable implicit HTTP retry that might issue multiple LLM requests under one Run ID. If Provider reports error after an uncertain send, Data marks the ambiguity; no blind replay.
- Reasoning effort, native continuation and provider cache options remain separate from history text. Missing capability is a typed request rejection before send, not an arbitrary downgrade.
- Provider-internal chain-of-thought is not presumed accessible; only actually returned usage/continuation metadata can be recorded.

## 5. Normalized response discriminant (V3-044 CONFIRMED, 세부 PROPOSED)

~~~text
NormalizedModelOutput:
    kind: FINAL | TOOL_CALLS
    blocks: ordered[NormalizedContentBlock]   # FINAL 본문, 또는 TOOL_CALLS와 함께 온 commentary
    tool_calls: ordered[NormalizedToolCall]    # TOOL_CALLS
    provider_continuation_ref?
    usage?
~~~

**question은 별도 kind가 아니라 Control이 등록한 `ask_user` Tool 호출이다.** 모델은 사용자에게 물어야 할 때 `ask_user({question, …})`를 호출하고, Control은 해당 call을 미완결로 둔 채 `WAITING_FOR_USER`로 멈춘다. 사용자의 다음 입력은 exact `user_message` Event로 기록되고 동시에 그 call의 ToolResult로 완결되어, 다음 Run이 합법적인 native ToolCall/ToolResult 그룹으로 이어진다. Pause/Resume의 미완결 call 처리와 같은 메커니즘이다.

| Native output | Normalized outcome | Control |
| --- | --- | --- |
| final content, without tool calls | `FINAL` | commit final Event, stop |
| one or multiple ordered tool calls (commentary text 동반 가능, V3-P16) | `TOOL_CALLS` | commit full group, execute, then next Run |
| `ask_user` as the only call | `TOOL_CALLS` | commit group, `WAITING_FOR_USER` until user answers |
| `ask_user` mixed with other calls | `INVALID_MODEL_OUTPUT` (P0 제안, V3-P15) | persist raw; no tools launched |
| empty/invalid/duplicate call IDs/unsupported type | `INVALID_MODEL_OUTPUT` | persist raw, reject projection, no external effects |
| provider transport timeout or status without full response | `FAILED_NO_RESPONSE / OUTCOME_UNKNOWN` | never fabricate response or silently redo same Run |

- 물음표 등 텍스트 휴리스틱으로 question을 추정하지 않는다.
- Commentary/progress 텍스트는 final 신호가 아니다.

## 6. Tool group completeness and prompt-native chronology

- The assistant ToolCall group is immutable and contains all original `(call_id,ordinal,tool_id,args)` in original order.
- Data `ToolExecutionRecords` persist every intent before launch and every truthful result. Different completion orders do not change ordinal.
- A Tool failure, refusal or cancellation that **produced a definite final ToolResult** is returned for model evaluation. An `UNKNOWN` effect is not an ordinary failure: control must block/fail closed until reconciled, since sending a synthetic result can be wrong.
- Prompt may select results not present in Conversation **only from Data ToolExecutionRecords**, verifying group identity/count/ID, Tool result exact content and chronology.
- Provider adapter renders a **legal native ToolCall/ToolResult group** according to target API. CurrentState and cache-first structure must not violate required adjacency. If no legal provider-native mapping exists, reject before Run send; do not discard or reorder required history to force a request.
- Multiple ToolCalls may execute in parallel if actual shared-resource effects permit. The barrier requires all original calls to have truthful results before the next LLM Run. Future managed processes may use their own interim read Tools, not an incomplete ToolCall result.
- Tool-result Event ordering (Data append sequence) can reflect completion time, but Provider native reconstruction uses original ordinal. An Event sequence is never rewritten to force visual or replay adjacency.

## 7. Control serialization, Pause and recovery

**CONFIRMED:** One active Conversation run loop, independently scheduled ToolCalls and independent Conversations. No hidden Task layer.

**PROPOSED:**
- **(P2, V3-045)** Data `ConversationControlState` + `control_generation` fence stale workers. Only one driver claim writes new Run/Tool stages. A lease expiry alone does not prove a network/OS effect is safe to repeat.
- Soft Pause: stop beginning new Run or launching additional ToolCalls. Existing Provider response/started bounded Tool finishes if possible; commit truthful records. Distinguish `PAUSE_REQUESTED` from `PAUSED`.
- On Resume first inspect `RunRecord` and Tool groups: project stored raw response if not projected; complete outstanding **unstarted** PENDING calls after current authorization; only after complete Tool group construct a **new** Run.
- A crashed tool with `EXECUTION_CLAIMED` and no definite outcome cannot be treated as PENDING; classify uncertain and require reconciliation.
- final/question stops the loop; any Race with Pause must not overwrite a durable terminal or waiting-user state.
- New user input during an active/paused execution: queue/reject/interrupt policy is OPEN, not silently appended into a stale current Prompt.

## 8. Contracts / errors to independently review

| Boundary | Proposed typed error | Guarantee to test |
| --- | --- | --- |
| Prompt → Control | `INCOMPLETE_HISTORY`, `STALE_BASIS` | no missing group, no fabricated tool result |
| Provider prepare → Control | `UNSUPPORTED_CAPABILITY`, `INVALID_NATIVE_ORDER` | no send of malformed provider-native input |
| Data prepare → Control | `RAW_PERSISTENCE_UNAVAILABLE`, `VERSION_CONFLICT` | native bytes durably stored before send |
| Provider invoke → Control | `SEND_UNCERTAIN`, `REMOTE_FAILURE` | one Run attempt; no hidden auto-retry |
| Provider normalize → Control | `INVALID_MODEL_OUTPUT`, `INVALID_MIXED_OUTPUT` | raw saved, zero Tool effects |
| Tool group → Control | `UNKNOWN_TOOL_EFFECT`, `INCOMPLETE_TOOL_GROUP` | no next provider invocation with missing results |
| Environment execute → Tool | `SANDBOX_UNAVAILABLE`, `BINDING_REVOKED` | current identity verified; fail closed |
| Control state → UI/CLI | `BUSY`, `RECOVERY_REQUIRED` | no concurrent second driver/re-execution |

## 9. First implementation acceptance sequence

1. Fake Provider, Fake Tool, in-memory Data Port (contract-compliant) demonstrate User→Run1→ToolCall group→ToolResults→Run2→final.
2. User→Run1→`ask_user` call stops with persisted call group, `WAITING_FOR_USER`, no second Run; user answer completes the call and Run2 follows.
3. Run1 tool-group has two calls, completed in reverse order: History reconstruction native order correct.
4. Raw native request body persisted **before network** and exactly identical to sent bytes; raw response survives normalize failure.
5. Provider low-level retry suppressed, uncertain after dispatch never automatically resent as same Run.
6. Intent transaction failure prevents **any** Tool launch; Tool failure with certain result is returned to model next Run.
7. UNKNOWN Tool group prevents next Run and does not invent a ToolResult.
8. Stale Workspace Binding or unsupported Provider native capabilities refuse execution.
9. Stable Prefix/history/current State compose correctly without illegal tool-result adjacency.
10. Same Conversation rejects conflicting new driver (P0: in-process lock); different Conversations remain independently eligible to progress.

**Testing policy:** public contract changes ⇒ full pytest; contract-preserving internal changes ⇒ changed module pytest. No v3 implementation or tests exist yet.

## 10. Decisions required before freeze

- CP-01 ~~question output contract~~ → **`ask_user` Tool로 해결(V3-044)**. 남은 것: `ask_user` 혼합 허용(V3-P15), commentary 처리(V3-P16).
- CP-02 Exact immutable request body capture via selected Provider SDK and transport; P0 완화안 V3-P14 검토. streaming/chunk boundaries later.
- CP-03 Snapshot persistence/version coherence with Run prepare and Source StateBasis.
- CP-04 Data T5/T6/T7 semantics: all call IDs/ordinals, concurrency barrier, failure-vs-unknown resolution.
- CP-05 ~~lease/fencing~~ → P2(V3-045). 남은 것: busy/paused 중 사용자 메시지 처리.
- CP-06 Provider-native CurrentState role and historical Tool group legal rendering for each target model.
- CP-07 Memory/Plan Internal Tool commits and exact vs omitted Conversation ToolResult policy.

**Next:** confirm this boundary contract against [Data v0.2](data-contract-v0.2.md), then separate module-focused code implementation packets. Do not mark READY_FOR_IMPLEMENTATION until DR/CP design blockers are resolved.
