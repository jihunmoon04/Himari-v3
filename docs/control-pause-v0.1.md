# Control Pause/Resume & Run Continuity — v0.1

> 2026-10-13 · **PROPOSED**, except prior Confirmed: `Run = exactly one LLM invocation`, `Conversation execution serial`, `raw Run/Tool data durable`, `tool-result group native replay`.
>
> 범위: 별도 Task/ManagerTurn 계층을 복원하지 않고 **Conversation의 Control 실행 흐름**이 Pause/Resume를 지원할 수 있는 최소 계약.

> **2026-10-13 개정:** P0는 단일 프로세스·Conversation별 in-process lock으로 시작하며, 아래의 영속 `ConversationControlState`·lease·fencing·다중 worker 복구는 **P2**다(V3-045). question 대기는 미완결 `ask_user` call로 표현되므로(V3-044) Pause의 "미완결 call 보존·재개" 메커니즘과 같은 경로를 쓴다.

## 1. 주된 결론

**이전 Run이 보존되는 것만으로 Pause/Resume는 완결되지 않는다.** RunRecord는 한 번의 Provider 호출(요청·응답·실패)을 재현할 수 있지만 다음 정보는 표현하지 못한다.

1. 사용자가 `PAUSE`를 요구했는지, Control이 멈춘 **안전 경계**는 어디인지.
2. Provider 응답이 이미 기록되었지만 아직 **정규화/Conversation Event 커밋** 전인지.
3. ToolCall 그룹의 어떤 호출이 `PENDING/RUNNING/TERMINAL`인지.
4. 다음 단계가 **Tool 실행 재개인지, 다음 LLM Run인지, 종료 응답/질문 대기인지**.
5. 실행 흐름을 재개할 권한/lease 보유자와 요청 중복 방지.

추천: Data 내부의 **ConversationControlState** (새 최상위 Task 모듈 아님)와 원본 Run/Tool 기록을 결합한다.

## 2. 용어와 분리

- `RunRecord`: Provider 호출 하나의 불변 Raw I/O + 상태; **Run 자체는 중간부터 토큰 추론 재개 불가**.
- `ConversationControlState`: 동일 Conversation 실행 흐름의 제어 상태(상위 Task 아님). 진행/요청된 pause/재개 위치/claim fencing을 영속화.
- `ExecutionCursor`: 처리된 **Run ID + assistant event/tool group stage + pending call IDs** 등을 가리키는 cursor 또는 재구성 가능한 참조.
- `ToolExecutionRecord`: 외부 효과의 durable provenance; 도중 끊긴 결과는 `UNKNOWN`일 수 있음.
- `PromptSnapshot`: 마지막 구성 프롬프트 1개; **execution cursor의 대체물이 아니다**.
- `Pause`: 현재 진행 중인 행동을 강제로 중단하는 기능이 아니라 **새로운 진행을 더 시작하지 않고 안전 경계에서 정지하는 제어 요청**.
- `Cancel`: 진행 중 작업 취소 신호 및 외부 효과 처리 정책이 별도로 필요한 기능으로, Pause와 다르다.

## 3. 권고 상태 모델

```text
IDLE
  ↓ user asks agent to work
RUNNING
  ├── PAUSE_REQUESTED ──(safe settle / no new steps)──▶ PAUSED
  │                                               └─ Resume → RUNNING
  ├── QUESTION / WAITING_FOR_USER ────────────────▶ WAITING_FOR_USER
  ├── final ──────────────────────────────────────▶ COMPLETED / IDLE
  ├── cancel request ─────────────────────────────▶ CANCEL_REQUESTED → CANCELLED
  └── unknown external outcome ───────────────────▶ RECOVERY_REQUIRED
```

**정확한 상태 enum/전이 결정은 아직 미정.** `COMPLETED`는 개별 실행 흐름 완료, `IDLE`은 Conversation에서 새 입력을 받을 수 있음으로 구분하거나 결합할 수 있다.

`PAUSE_REQUESTED`는 사용자 의도이고 `PAUSED`는 실제 실행 정지 상태다. `PAUSE_REQUESTED` 즉시 `PAUSED`라고 보고하면 안 된다.

## 4. Pause 발생 위치별 의미

| 위치 | 권고 처리 | Resume 시 |
| --- | --- | --- |
| Provider 호출을 아직 보내지 않음 | Run을 새로 전송하지 않고 정지. 저장된 PREPARED raw request의 취소/무효화 의미는 별도 전이로 기록 | 최신 context/state 재검증 후 새로운 Run 준비 |
| Provider 호출 진행 중 | **Soft pause**: 기존 요청을 기다려 실제 응답/오류를 RunRecord에 저장; 곧바로 새 Run 시작 금지 | 저장된 결과와 실행 cursor를 보고 다음 행동 결정 |
| Raw response 저장됐으나 normalize 전 | normalize 단계는 중복 없이 커밋 가능한지 확인하고 정지 | 이미 영속된 raw를 재정규화(새 Provider 호출 아님) |
| Assistant 결과가 `final` | 원래 Run이 정상 종료되었다면 `COMPLETED`가 우선. 억지로 PAUSED 상태 만들지 않음 | 새로운 사용자 입력에서 새 작업 흐름 |
| Assistant 결과가 `ask_user` 호출 | call 그룹을 정확히 보존하고 `WAITING_FOR_USER` 처리. Pause에 의한 응답 대기와 구분 | 사용자 답변 Event로 해당 call을 완결한 뒤 다음 새 Run |
| ToolCall 그룹 수신 / 실행 전 | **미실행 Tool을 새로 시작하지 않고 PAUSED**, 완전한 호출 group과 `PENDING` 보존 | `PENDING` 작업 실행 후 완전한 ToolResult group 구성; 그다음 새 Run |
| ToolCall 그룹 일부 실행 중 | 새 Tool launching 중단, **이미 시작한 작업만 종결/확인**; 남은 미실행 호출은 PENDING | 이미 성공한 call은 재실행 금지, 나머지 PENDING만 재개; group 완료 후 다음 Run |
| ToolCall 결과 모두 저장됨 | 새 Run 호출 전에 PAUSED | 기존 ToolResult를 replay-safe input으로 포함하여 **새 Run** 시작 |
| Tool/Provider 결과 여부가 불명 | 임의의 완료를 날조하거나 부작용 있는 호출을 재실행하지 않고 RECOVERY_REQUIRED | 확인 가능한 원본 및 사용자 정책에 따라 해결 |

**중요**: Provider는 ToolCall/ToolResult **미완결 그룹을 인자로 받아 호출되어서는 안 된다**. Pause 후 Resume가 PENDING ToolCall부터 시작해야 하는 경우가 있으므로, 단순히 Run의 마지막 응답에서 곧바로 '다음 Run'을 호출하면 안 된다.

## 5. In-flight 한계

- HTTP/SDK Provider 호출을 일반적으로 '토큰 N부터 멈추고 그 자리에서 이어서 실행'할 수는 없다. **Pause는 Provider 호출 사이/Tool 경계에서의 협력적 정지**.
- Run을 Abort/Cancel하면 결과 수신 여부 또는 비용 청구 여부가 불확실할 수 있다. 결과가 확실치 않으면 새 Run으로 자동 재전송하지 않는다.
- 초기 구조화 파일 Tool은 완료 시간이 짧을 수 있지만, 네트워크 업로드/대형 파일 작업은 오래 걸릴 수 있으므로 `PAUSE_REQUESTED`에서 시간을 보낼 수 있다.
- Long-running process가 추후 추가되면 기존 Process가 그대로 유지될지 중단될지 ManagedProcess 수명 정책을 별도로 요구한다.
- Pause를 요청한 사용자와 Resume 사용자의 권한 일치, 서비스 재시작에서 단일 실행 흐름 lease/fencing을 검사한다.

## 6. Ownership & Port inventory

| Contract | Owner / Provider → Consumer | Source of Truth | Planned location |
| --- | --- | --- | --- |
| CTRL-03 `RequestPause` | Control → Web/CLI; 내부 Data Port 사용 | Data ConversationControlState | `control/api.py` |
| CTRL-04 `ResumeExecution` | Control → Web/CLI; 내부 Data Port 사용 | ControlState + Run/Tool/Event originals | `control/api.py` |
| DATA-09 `ReadWriteControlState` (**P2**) | Data·ControlState → Control | Data ConversationControlState / execution cursor | `data/api.py` |
| DATA-10 `ClaimControlLease` (**P2**) | Data·ControlState → Control | Data owner generation/lease/fencing record | `data/api.py` |
| TOOL-04 `ResumePendingGroup` | Tool → Control | ToolExecutionRecord + verified pending tool requests | `tool/api.py` |

**Contract Location**은 본 문서 및 [contracts.md](contracts.md), code 경로는 전부 planned이다.

## 7. 권고하는 재개 처리 우선순위

```text
Resume(conversation, trusted_user)
  1) Data에서 ConversationControlState, lease, cursor, 최근 Run/Tool records 조회
  2) 진행 중 실행 권한을 새로 claim하고 이전 owner의 stale commit 차단
  3) Raw Run response는 있는데 projection이 누락되었다면 idempotent 복구
  4) PENDING ToolCall Group 있다면 미실행 call만 실행 또는 user intervention
  5) Tool 결과 전체가 terminal이면 complete native group reconstruct
  6) 그 이후 Prompt 최신 State 재조회 → NEW Run 호출 1회
```

- **RunRecord가 있음을 근거로 같은 Provider 요청을 자동 재시도하지 않는다.**
- `UNKNOWN` 실행 결과와 stale/revoked Workspace Binding이 있으면 실제 재실행 금지; 실패·복구 요구를 명시적으로 반환한다.
- Event 시퀀스/Call ID, idempotent DB 트랜잭션, lease fencing을 검증한다.
- Pause 중 새로운 사용자 메시지의 삽입/처리 순서를 어떻게 관리할지는 별도 정책으로 결정한다.

## 8. 예시: Tool 호출 이후 즉시 Pause

```text
Run 14 raw response: ToolCall A, ToolCall B
Data: append assistant call group + PENDING A, PENDING B

Control: PAUSE_REQUESTED
  → mark PAUSED before launching A or B
  → no fake ToolResult is created
  → latest raw Run response retained

Resume:
  → verify authorized workspace binding and execution authority
  → execute A and B (can parallel if verified independent)
  → persist exact ToolResults paired to original call_ids
  → Run 15 (new Provider call 1회)
```

## 9. Contract tests — first implementation subset

1. Pause requested before sending Provider: zero invocation, no phantom response.
2. Pause during invocation: one completed Run logged, no unintended second Run.
3. Pause between raw response and normalization: original raw retained; idempotent projection after resume.
4. Pause with pending ToolCalls: no premature provider replay, no fabricated ToolResult.
5. Partial tool group: completed call never reruns; pending call only on Resume.
6. Concurrent Resume attempts: one lease wins, other returns busy/conflict.
7. Crash with unknown external effect: no blind retry, RECOVERY_REQUIRED.
8. final/question and pause races: terminal/waiting outcomes remain coherent, no fake PAUSED terminal.
9. PromptSnapshot presence does not authorize replay/Tool execution.
10. No Task/ManagerTurn table required to pass Resume tests.

## 10. OPEN

- Whether to call state `ConversationControlState` or a finer `ExecutionSession`; user-facing Task identity remains intentionally absent.
- Pause command and current Provider call interaction: wait-to-boundary (recommended) vs cancellable mode.
- Pending but unstarted Tool calls after Pause: explicit confirmation/expiry limits, future approval mechanism, suspension lifetime.
- Exact Run PREPARED/IN_FLIGHT raw/duplicate-send crash recovery contract.
- Cancel semantics, pause timeout, resource cleanup for managed processes.
- Lease implementation/fencing, concurrent user inputs, Conversation delete/Trash during paused state.
