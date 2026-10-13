# Control–Prompt–Provider Boundary Contract v0.1

> 2026-10-13 · **REVIEW CANDIDATE / NOT FROZEN / NOT IMPLEMENTED**. This is a companion design to [Data v0.2](data-contract-v0.2.md), not an independent new architecture. Previous confirmed principles in [decisions](decisions.md) take precedence.
>
> **Scope:** Control's per-Conversation run loop, Prompt provider-neutral input, Provider native serialization/invocation/normalization, Data run/tool group persistence, Tool dispatch barrier. Exact v3 Python DTOs/enum values remain PROPOSED.

> **2026-10-13 개정 (V3-042~046, 독립 검수 B1~B3 반영):** question은 suspending Tool `ask_user`로 표현하고 정규화 출력은 `FINAL | TOOL_CALLS` 두 가지다(§5, 대기 계약 §5.1). Prompt 핵심은 순수 `compose(PromptInputs)`(§2). P0는 프로세스 내 Conversation lock으로 **동시 실행**을 막고, **크래시 후 중복**은 Data의 송신·실행 직전 영속 표시(`DISPATCH_CLAIMED`/`EXECUTION_CLAIMED`)와 재시작 시 UNKNOWN 처리로 막는다(§3, §7). 분산 lease·fencing은 P2. native 재생 규칙과 Provider 배열 계약 테스트는 §6.1–6.2. raw 요청 보존 기준은 P0 미확정(§4). Provider 계약은 `ProviderAdapter` SPI로 묶는다.

## 1. Module responsibility and authoritative sources

| Contract | Owner / implementer | Consumer | Source of truth | Output or effect |
| --- | --- | --- | --- | --- |
| CTRL-01 `DriveConversation` | Control | Web/CLI | authenticated user input (Control이 T1 또는 T7a로 기록) + Data Run/Tool state | serialized run loop; final/question stops |
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
0) UI/CLI → Control.drive(trusted scope, raw user input)   # UI는 user Event를 직접 기록하지 않음
1) Control acquires this Conversation's lock (P0: in-process; P2: Data T10 lease/fencing)
1a) 사용자 입력 기록 — Control만 기록하며, 입력 하나당 정확히 한 경로:
      find_waiting_call(scope) 있음 → T7a answer_pending_call   # user_message(answers_call) + ToolResult + tool_result
      없음                         → T1 append_event            # 일반 user_message
2) Control → Prompt.compose()
      Data read exact events/latest snapshot, Execution and State basis;
      Prompt verifies coverage, tool groups, latest State.
3) Provider.prepare_native_request(model_input)  # pure, no network
      Validate support & native role/tool-adjacency protocol.
4) Data T9 replace latest full Snapshot + Data T2 PREPARED native bytes
      Candidate: must pin exact basis; reject stale/unsafe mismatch.
5) Data T3 mark DISPATCH_CLAIMED (P0: 조건부 갱신; P2: fenced claim)
      → Provider.invoke_prepared(prepared request)   # 정확히 1회
6) Data T4 save RAW response (or typed uncertain/no-response failure)
7) Provider.normalize(raw), then Data T5 project:
     FINAL      → exact final Event → STOP
     TOOL_CALLS → exact group Event + pending intents atomically
                  (ask_user 단독 호출 → WAITING_FOR_USER; 사용자 응답이 그 call을 완결)
8) Tool dispatcher runs original pending calls: 부작용 있는 call은 먼저 T6 EXECUTION_CLAIMED 영속;
     executes independently when no shared effects; ToolHandler returns ToolOutcome;
     Dispatcher commits T7/T8. ask_user는 실행하지 않고 대기(§5.1).
9) Barrier: ALL original calls have truthfully completed, model-facing results.
     If UNKNOWN/unpaired: BLOCK / RECOVERY, not fabricated completion.
10) New Prompt.compose and **new Run** (restarting at step 2).
~~~

- Control **does not** create a Task/ManagerTurn wrapper. One provider invocation attempt is one Run; tool executions are associated records, not nested Run invocations.
- No DB transaction encloses Provider network I/O or OS Sandbox process lifespan.
- Per-Conversation Control serialization is confirmed. **P0 uses an in-process lock (V3-045)**; driver claim/lease and epoch fencing are P2. Cancel/User input queue and Pause timing are still proposed.
- **lock ≠ 크래시 안전성.** in-process lock은 살아 있는 프로세스 안의 동시 driver만 막는다. 프로세스가 죽으면 lock도 사라지므로, 크래시 후 같은 Run 재송신·같은 Tool 재실행은 **Data의 T3/T6 영속 표시와 재시작 정리(T-R)**로 막는다. 재시작 시 `DISPATCH_CLAIMED`(응답 없음) → `OUTCOME_UNKNOWN`, `EXECUTION_CLAIMED`(결과 없음) → `UNKNOWN`이며 자동 재실행하지 않는다.
- Trigger of next Run after tools is the **already persisted completed tool group**, not a newly synthesized User Event.
- **사용자 입력 단일 기록 규칙:** user 입력은 step 1a에서 lock 안에서 한 번만 기록된다. ask_user 대기 중이면 T7a가 `user_message`와 ToolResult를 함께 쓰고, T1을 따로 호출하지 않는다. 대기가 아니면 T1만 쓴다. 따라서 같은 입력이 `user_message` 두 개로 남는 경우는 없다. Trigger는 T1이면 그 Event, T7a면 완결된 `ask_user` 그룹이다.
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
- Data persists the prepared request **before** send. **What the stored body is equal to is OPEN for P0 (V3-P14 / CP-02):** wire-exact(송신은 저장 bytes를 그대로 사용) vs adapter-serialized(Adapter가 transport에 넘긴 body). 선택과 무관하게 RunRecord는 `raw_representation`을 기록한다.
- Headers or transport auth are supplied by trusted Provider adapter at send time; they must not be serialized into Data's native body capture.
- Never claim the stored JSON is byte-exact wire if it is only a semantic equivalent; an SDK with unobservable reserialization is only compatible with the adapter-serialized representation.
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

**question은 별도 kind가 아니라 Control이 등록한 `ask_user` Tool 호출이다.** 모델은 사용자에게 물어야 할 때 `ask_user({question, …})`를 호출하고, Control은 해당 call을 미완결로 둔 채 `WAITING_FOR_USER`로 멈춘다. 사용자의 다음 입력은 exact `user_message` Event로 기록되고 동시에 그 call의 ToolResult로 완결되어, 다음 Run이 합법적인 native ToolCall/ToolResult 그룹으로 이어진다. Pause/Resume의 미완결 call 처리와 같은 메커니즘이다. 대기 중 lock 해제, 재시작 후 대기 판별, 답변 트랜잭션은 §5.1, 답변의 재생 규칙은 §6.1.

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

### 5.1 ask_user — suspending Tool 대기 계약 (V3-044 CONFIRMED, 메커니즘 V3-P17 PROPOSED)

`ToolDefinition.completion = immediate | suspending`. P0의 suspending Tool은 Control이 등록한 `ask_user` 하나다. 새 상태 머신·Task 계층은 없다.

~~~text
Run N → TOOL_CALLS [ask_user(call_id=c1)]
  T5: assistant_tool_calls Event + ToolExecution(c1, ask_user, PENDING)     # 한 트랜잭션
  Dispatcher: suspending → handler 실행·T6 claim 없음, c1은 PENDING 유지
  Control: WAITING_FOR_USER 반환, Conversation lock 해제
  ── (프로세스 재시작 가능) ──
User answer → Control.drive(scope, user input)
  lock 획득 → find_waiting_call(scope) → c1 발견 → T7a:
        user_message Event {text, attachments, answers_call: {run N, c1}}
      + ToolExecution c1: PENDING → SUCCEEDED, model_result = 답변에서 파생
      + tool_result Event (c1)                                              # 한 트랜잭션
  → 새 Run N+1 (trigger = 완결된 그룹 [c1])
~~~

- **대기 판별(영속 근거만):** 최신 `PROJECTED` Run의 ToolCall 그룹이 단일 `ask_user`이고 그 ToolExecution이 `PENDING`이면 `WAITING_FOR_USER`. 저장 상태 값이 아닌 파생 상태이며 재시작 후에도 같은 조회로 복원된다.
- `ask_user`는 부작용이 없으므로 `EXECUTION_CLAIMED`/`UNKNOWN`을 쓰지 않는다. 재시작하면 다시 대기 상태일 뿐이다.
- 대기 중 lock은 잡고 있지 않다. 대기 중에는 새 Run을 시작하지 않으며, 미완결 `ask_user` 그룹을 Provider에 보내지 않는다(§6.2 R5).
- 대기 중 들어온 사용자 입력은 P0에서 항상 그 call의 답변이다. 답변이 커밋된 뒤 들어온 입력은 일반 `user_message`이며 같은 `call_id`에 두 번째 ToolResult를 만들지 않는다.
- OPEN: 대기 취소·만료, 답변 첨부파일의 native ToolResult 표현(Provider별 지원 차이).

## 6. Tool group completeness and prompt-native chronology

- The assistant ToolCall group is immutable and contains all original `(call_id,ordinal,tool_id,args)` in original order.
- Data `ToolExecutionRecords` persist every intent before launch and every truthful result. Different completion orders do not change ordinal.
- A Tool failure, refusal or cancellation that **produced a definite final ToolResult** is returned for model evaluation. An `UNKNOWN` effect is not an ordinary failure and is never sent as a synthetic success or failure. Per V3-047 the runtime attaches an honest "outcome unknown" result (status stays UNKNOWN) so the model verifies the state before retrying; Tools declaring `REQUIRE_USER` block instead.
- Prompt may select results not present in Conversation **only from Data ToolExecutionRecords**, verifying group identity/count/ID, Tool result exact content and chronology.
- Provider adapter renders a **legal native ToolCall/ToolResult group** according to target API. CurrentState and cache-first structure must not violate required adjacency. If no legal provider-native mapping exists, reject before Run send; do not discard or reorder required history to force a request.
- Multiple ToolCalls may execute in parallel if actual shared-resource effects permit. The barrier requires all original calls to have truthful results before the next LLM Run. Future managed processes may use their own interim read Tools, not an incomplete ToolCall result.
- Tool-result Event ordering (Data append sequence) can reflect completion time, but Provider native reconstruction uses original ordinal. An Event sequence is never rewritten to force visual or replay adjacency.

### 6.1 native 재생 순서와 중복 방지 (V3-P18 PROPOSED)

Prompt `compose`가 provider-neutral 이력을 만들 때의 규칙이다. 원본 Event는 모두 보존하고, **렌더링에서만** 중복을 없앤다.

1. Event `sequence` 순서로 걷되 **ToolCall 그룹은 하나의 단위**로 렌더링한다. `assistant_tool_calls`(commentary 포함) 바로 뒤에 그 그룹의 ToolResult를 **원래 ordinal 순서**로 붙인다. 그룹 사이에 다른 Event나 Current State를 끼우지 않는다.
2. `tool_result` Event는 완료 순서대로 sequence가 흩어져 있어도 그룹 위치에서만 렌더링한다. 같은 결과를 그룹 안팎에 두 번 넣지 않는다.
3. `answers_call`이 있는 `user_message`는 **해당 `ask_user` call의 ToolResult로만** 렌더링하고 독립 user 턴으로 다시 넣지 않는다. 답변 내용은 provider 배열에 정확히 한 번 나온다.
4. `ask_user` ToolResult의 `model_result`는 T7a에서 답변 Event로부터 파생되어 함께 커밋된다. 둘이 어긋나면 `INCOMPLETE_HISTORY`로 거부한다.
5. 압축(Snapshot) 경계는 그룹을 쪼개지 않는다. 그룹과 답변 Event는 함께 압축되거나 함께 exact tail에 남는다.
6. Current State는 마지막 그룹 뒤에 둔다. 구체 위치(예: tool result 블록 뒤 같은 user 턴의 text 블록, 또는 별도 메시지)는 Provider Adapter가 고르며, 합법적 위치가 없으면 송신 전 거부한다.

### 6.2 Provider 메시지 배열 최소 계약 테스트 (설계)

모든 `ProviderAdapter`가 통과해야 하는 conformance suite다(Provider 모듈 소유, V3-040). 고정 `PreparedModelInput`을 `prepare_native_request`에 넣고, 만들어진 body를 provider별 **구조 검증기**로 확인한다. 네트워크는 쓰지 않는다.

| ID | 입력 | 기대 |
| --- | --- | --- |
| R1 | ask_user 그룹 + 답변 | assistant tool call 바로 뒤에 같은 call_id의 tool result. 답변 텍스트는 배열 전체에서 정확히 1회. 답변만 담은 독립 user 메시지 없음 |
| R2 | 호출 2개, 역순 완료 | 결과가 원래 ordinal 순서, 모든 call_id 짝 일치, 사이에 다른 메시지 없음 |
| R3 | tool result로 끝나는 이력 + Current State | Current State가 tool result 인접성을 깨지 않는 위치에 있음 |
| R4 | commentary + tool calls | 텍스트와 호출이 같은 assistant 턴에 있음 |
| R5 | UNKNOWN 또는 미완결 그룹(대기 중 ask_user 포함) | body 생성 전에 typed error로 거부 |
| R6 | 연속 user 입력, 압축 경계 직후 그룹 | provider의 role 교대·첫 메시지 규칙 위반 없음 |
| R7 | 같은 입력을 두 번 prepare | 동일 body (prefix cache 안정성) |

구조 검증기는 provider 규칙을 최소한으로 코드화한 것이다(예: Anthropic Messages의 user/assistant 교대와 `tool_use`↔`tool_result` id 일치, OpenAI의 `function_call`↔`function_call_output` call_id 일치). 실제 API 호출 검증은 별도 선택 게이트다.

## 7. Control serialization, Pause and recovery

**CONFIRMED:** One active Conversation run loop, independently scheduled ToolCalls and independent Conversations. No hidden Task layer.

**PROPOSED:**
- **(P2, V3-045)** Data `ConversationControlState` + `control_generation` fence stale workers. Only one driver claim writes new Run/Tool stages. A lease expiry alone does not prove a network/OS effect is safe to repeat.
- Soft Pause: stop beginning new Run or launching additional ToolCalls. Existing Provider response/started bounded Tool finishes if possible; commit truthful records. Distinguish `PAUSE_REQUESTED` from `PAUSED`.
- On Resume first inspect `RunRecord` and Tool groups: project stored raw response if not projected; complete outstanding **unstarted** PENDING calls after current authorization; only after complete Tool group construct a **new** Run.
- A crashed tool with `EXECUTION_CLAIMED` and no definite outcome cannot be treated as PENDING; restart cleanup (T-R) classifies it `UNKNOWN` and never re-invokes the handler; the next `drive`/`resume` reports it to the model as "outcome unknown" (V3-047), or blocks for `REQUIRE_USER` Tools. Precise reconcile is P2.
- A crashed Run with `DISPATCH_CLAIMED` and no response becomes `OUTCOME_UNKNOWN`; it is never resent, and a new Run starts only on the next user input or explicit resume.
- final/question stops the loop; any Race with Pause must not overwrite a durable terminal or waiting-user state.
- New user input during an active/paused execution: queue/reject/interrupt policy is OPEN, not silently appended into a stale current Prompt.

## 8. Contracts / errors to independently review

| Boundary | Proposed typed error | Guarantee to test |
| --- | --- | --- |
| Prompt → Control | `INCOMPLETE_HISTORY`, `STALE_BASIS` | no missing group, no fabricated tool result |
| Provider prepare → Control | `UNSUPPORTED_CAPABILITY`, `INVALID_NATIVE_ORDER` | no send of malformed provider-native input |
| Data prepare → Control | `RAW_PERSISTENCE_UNAVAILABLE`, `VERSION_CONFLICT` | native bytes durably stored before send |
| Provider invoke → Control | `SEND_UNCERTAIN`, `REMOTE_FAILURE` | one Run attempt; no hidden auto-retry |
| Provider normalize → Control | `INVALID_MODEL_OUTPUT` (`ask_user` 혼합 포함, V3-P15) | raw saved, zero Tool effects |
| Tool group → Control | `UNKNOWN_TOOL_EFFECT`, `INCOMPLETE_TOOL_GROUP` | no next provider invocation with missing results |
| Environment execute → Tool | `SANDBOX_UNAVAILABLE`, `BINDING_REVOKED` | current identity verified; fail closed |
| Control state → UI/CLI | `BUSY`, `RECOVERY_REQUIRED` | no concurrent second driver/re-execution |

## 9. First implementation acceptance sequence

1. Fake Provider, Fake Tool, in-memory Data Port (contract-compliant) demonstrate User→Run1→ToolCall group→ToolResults→Run2→final.
2. User→Run1→`ask_user` call stops with persisted call group, `WAITING_FOR_USER`, no second Run; user answer completes the call and Run2 follows.
3. Run1 tool-group has two calls, completed in reverse order: History reconstruction native order correct.
4. Raw native request persisted **before network** with an honest `raw_representation` (wire-exact 선택 시 송신 bytes 동일성까지); raw response survives normalize failure.
5. Provider low-level retry suppressed; **process restart** after `DISPATCH_CLAIMED` yields `OUTCOME_UNKNOWN`, never resent (lock alone must not be what makes this pass).
6. Intent transaction failure prevents **any** Tool launch; Tool failure with certain result is returned to model next Run.
7. An UNKNOWN call never gets an invented success/failure: it reaches the next Run only as an "outcome unknown" result (V3-047), or blocks the next Run for `REQUIRE_USER` Tools.
8. Stale Workspace Binding or unsupported Provider native capabilities refuse execution.
9. Stable Prefix/history/current State compose correctly without illegal tool-result adjacency.
10. Same Conversation rejects conflicting new driver (P0: in-process lock); different Conversations remain independently eligible to progress.
11. Process restart after `EXECUTION_CLAIMED` without result: Tool `UNKNOWN`, handler not re-invoked, next Run blocked.
12. Process restart while waiting on `ask_user`: wait state recovered from persisted rows; answer commits ToolResult for the original `call_id`, then exactly one new Run.
13. Provider array conformance R1–R7 (§6.2) for every registered adapter.

**Testing policy:** public contract changes ⇒ full pytest; contract-preserving internal changes ⇒ changed module pytest. No v3 implementation or tests exist yet.

## 10. Decisions required before freeze

- CP-01 ~~question output contract~~ → **`ask_user` Tool로 해결(V3-044)**. 남은 것: `ask_user` 혼합 허용(V3-P15), commentary 처리(V3-P16).
- CP-02 **P0 raw 보존 기준 미확정(V3-P14 OPEN)**: wire-exact vs adapter-serialized. streaming/chunk boundaries later.
- CP-03 Snapshot persistence/version coherence with Run prepare and Source StateBasis.
- CP-04 Data T5/T6/T7/T7a/T-R semantics: all call IDs/ordinals, concurrency barrier, UNKNOWN 처리는 V3-047로 결정.
- CP-05 ~~lease/fencing~~ → P2(V3-045). 남은 것: busy/paused 중 사용자 메시지 처리.
- CP-06 Provider-native CurrentState role and historical Tool group legal rendering for each target model — §6.1 규칙과 §6.2 R1–R7로 검증. ask_user 답변 첨부파일 표현 OPEN.
- CP-07 Memory/Plan Internal Tool commits and exact vs omitted Conversation ToolResult policy.

**Next:** confirm this boundary contract against [Data v0.2](data-contract-v0.2.md), then separate module-focused code implementation packets. Do not mark READY_FOR_IMPLEMENTATION until DR/CP design blockers are resolved.
