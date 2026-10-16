# 한 턴의 예산과 비용 — v0.1 (V3-P62)

> 2026-10-16 · **Proposed, rev. 4** — 독립 검수 NOT_READY(B1~B4, C1·C2) 반영(§14), 재검수 NOT_READY(N1~N3) 반영(§15), 재검수 NOT_READY(R1·R2, C3) 반영(§16), 재검수 READY(비차단 권고 둘, §17). 구현: 1단계 §18(PR #47), 2단계와 측정 §13·§19. V3-P23(drive당 Run 예산 16)을 대신한다. **소유자 방향(2026-10-16):** 다른 에이전트처럼 Run 수 상한 없이 돌게 한다. 그 대신 비용·진척·클라이언트 연결을 지키는 장치를 둔다(§2~§6, 제시한 방법 모두 채택). 수치 기본값은 §11의 O1~O4.
>
> 관련: [V3-005](decisions.md)(Tool 실패는 모델에게), [V3-047](decisions.md)(결과 불명은 모델이 확인), [V3-P55](decisions.md)(Pause·Cancel·Continue), [prompt-composition](prompt-composition.md) §5(prefix 캐시), [open-questions](open-questions.md) Q-25(캐시 성능), [mcp-context-v0.1](mcp-context-v0.1.md) §14.5(실제 모델 시험).

## 1. 왜

한 사용자 메시지(또는 Continue 한 번)의 drive는 지금 Run 16번에서 멈춘다(`max_runs_per_drive`, V3-P23). 무한 루프를 막는 예산이고, 코드 기본값이라 운영자가 바꿀 수 없다.

실제 모델 시험(2026-10-16, `openai/gpt-6-luna`, Calliope 해설 한 수):

| 시험 | Run | 입력 토큰 | 캐시 적중 | 결과 |
| --- | --- | --- | --- | --- |
| 처음(Calliope AG5 전, HIMARI 결함 둘 있음) | 16, 상한 | 425,673 | Run마다 2,125 | 해설 없음 |
| AG5 뒤 3회 | 7 · 9 · 5 | 132k · 192k · 81k | Run마다 2,691 | 모두 저장(얕음) |

여기서 네 가지를 읽는다.
1. **Run 수는 위험을 직접 재지 않는다.** 처음 시험은 같은 종류의 실패를 되풀이하다 16번에서 끝났다. 해설이 "왜"까지 가면 제대로 일하는 모델도 16번이 빠듯하다. 실제 위험은 비용과 진척 없는 반복이다.
2. **모델은 예산을 모른다.** 남은 여유를 모르니 마지막 단계에도 탐색을 계속한다.
3. **HIMARI는 작업을 서버에서 돌린다.** 브라우저를 닫아도 작업은 계속된다. 대화형 에이전트(사람이 화면 앞에 있고, Esc로 멈추고, 위험한 작업은 승인을 기다리느라 선다)는 단계 수 상한 없이 돈다. "누가 보고 있다"는 전제가 그 상한을 대신한다. HIMARI에서는 그 전제가 성립하지 않을 때가 있다.
4. **비용의 대부분은 반복해서 보내는 앞부분이다.** 큰 Tool 결과 하나(Calliope `ply`, 약 18k 토큰)가 이후 모든 Run에 다시 실린다. 그런데 공급자 캐시는 Run마다 앞의 2,691 토큰만 적중했다.
   - HIMARI의 요청은 덧붙기만 한다. 연속한 두 요청은 맨 끝의 `<runtime_state>` 앞까지 바이트가 같다(시험 DB에서 확인).
   - 놓치는 쪽은 공급자 경로다. OpenRouter는 `session_id`가 없으면 "캐시 적중을 본 뒤에야" 같은 공급자로 보낸다.

## 2. 무엇이 한 턴을 멈추나

Run 수 상한은 **기본으로 없다.** 한 턴은 다음 중 하나로 멈춘다.

| 멈추는 것 | 상태 | 이어 가기 | 기본 |
| --- | --- | --- | --- |
| 모델의 답(final) 또는 질문(`ask_user`) | `COMPLETED` / `WAITING_FOR_USER` | 새 메시지 | 늘 |
| 사용자의 Pause·Stop(V3-P55) | `PAUSED` / `CANCELLED` | Continue / 새 메시지 | 늘 |
| **토큰 예산**(§3) — 대화 Run과 압축 Run 모두, 보내기 전 | `BUDGET_REACHED` (새) | Continue: 새 예산 | 켬 |
| **진척 없음**(§4) — 새 관찰이 없다는 근거가 분명한 Run이 연달아 | `NO_PROGRESS` (새) | Continue | 켬 |
| **연결된 클라이언트 없음**(§5) | `PAUSED`(이유: 대화를 읽는 클라이언트가 없음) | Continue | 켬 |
| Run 수 상한(선택, §6) | `RUN_LIMIT_REACHED` | Continue | 끔 |
| 문맥 한도(지금처럼, 압축으로 못 줄일 때) | `REQUEST_REJECTED` | — | 늘 |

- 어떤 경우든 멈추기 전에 커밋한 것은 그대로다. 멈춤은 다음 Run을 시작하기 **전**의 안전 경계에서만 일어난다.
- 멈춤 요청(Pause·Stop)이 예산·진척보다 우선한다(V3-P55 ③과 같은 규칙).
- Continue는 V3-P55의 `resume`이다. 새 상태도 같은 길로 이어 간다.

## 3. 토큰 예산 (기본 켬)

- **`HIMARI_TURN_TOKEN_BUDGET`**(기본 O1, 권고 1,000,000)이 한 drive가 쓸 수 있는 토큰이다.
  - **센 것:** 그 drive의 모든 Run(대화 Run과 압축 요약 Run)이 공급자가 보고한 입력 + 출력 토큰(출력에는 reasoning 토큰이 들어 있다). 캐시 적중분도 입력으로 센다(단순하고 보수적으로). 캐시 적중·쓰기는 따로 보인다(§7).
  - 공급자가 usage를 보고하지 않은 Run은 그 Run을 보낼 때의 입력 추정과 출력 예약(아래)으로 센다.
  - 0이면 끈다. 단, §6의 Run 수 상한이 켜져 있을 때만 끌 수 있다. 둘 다 끄는 설정은 시작 때 거부한다.
- **보내는 요청마다, 보내기 직전에 판정한다 (검수 B1).** 대화 Run이든 압축의 요약 호출이든, 하나를 보내기 직전에 다음을 계산한다.

  ```text
  쓴 양 + 이 요청의 입력 추정 + 출력 예약 > 예산  →  보내지 않는다
  ```

  - **입력 추정:** 대화 Run은 압축 계획이 이미 쓰는 추정(`ContextEstimate`, §8의 보정 포함)이다. 요약 호출은 그 요청의 `request_tokens`다.
  - **출력 예약:** HIMARI는 출력 상한을 보내지 않는다(답을 자르지 않음, `input_ceiling`의 원칙). 대신 출력이 들어갈 자리를 예약한다. 예약은 이 drive에서 한 Run이 낸 가장 큰 출력의 2배이고, 8,192보다 작지 않으며, 모델이 밝힌 최대 출력보다 크지 않다. 요약 호출은 그 요약의 상한(`summary_limit`)을 토큰으로 바꾼 값이다.
  - 넘으면 대화 Run은 보내지 않고 `BUDGET_REACHED`로 멈춘다. 압축 도중이면 그 연쇄를 멈추고 아무것도 커밋하지 않는다(압축은 마지막 요약만 커밋한다). 그 뒤 같은 상태로 멈춘다.
- **이것은 추정에 기댄 상한이지, 과금의 절대 상한이 아니다.** 입력 추정은 보수적이지만(바이트 3개를 1토큰으로) 틀릴 수 있다. 한 Run의 실제 출력이 예약보다 클 수도 있다. 넘을 수 있는 양은 마지막 한 Run의 오차까지다. 화면과 문서에서도 "예산"이라고 부르고 "상한 보장"이라고 하지 않는다.
- **Continue는 새 예산을 준다.** 사용자가 보고 결정했기 때문이다.
- **근거(O1):** 이번 시험의 한 턴이 81k~192k를 썼다. "왜"까지 가는 해설은 그 몇 배가 들 수 있다. 1,000,000은 그 다섯 배쯤이고, 길 잃은 루프 하나가 낼 수 있는 비용의 상한이다.

## 4. 진척 없음 (기본 켬)

세 가지 원칙을 따른다(재검수 권고). **기록을 근거로 실행을 막지 않는다.** 반복은 데이터로 알리되 결과는 그대로 둔다. 멈추는 것은 새 관찰이 없다는 근거가 분명할 때만이다.

**실행을 막지 않는다 (재검수 N1·N3).**
- 모든 호출은 지금처럼 Dispatcher를 거친다. Dispatcher는 호출마다 그때의 상태를 다시 검사한다: 출처가 켜졌는지, Environment가 활성인지, Workspace binding이 있는지.
  - 그래서 사용자가 턴 도중 MCP 서버를 켜면, 다음 호출은 켜진 상태로 판정된다.
- 이미 실행 전에 거부되는 호출(모르는 Tool, 스키마에 맞지 않는 인자)은 기록 없이도 늘 거부되고, 서버에 가지 않는다. 기록을 근거로 따로 막아도 얻는 것은 안내 문구 하나뿐이다. 반면 거부가 어느 단계에서 났는지(Dispatcher인지 Handler인지), 상태에 따라 바뀌는지를 잘못 읽을 위험이 있다. 그래서 막지 않는다.
- rev. 2의 `dispatch_group(…, refuse=)`는 없앤다.

**반복을 알린다 (성공·실패, 모든 효과).**
- 같은 Tool·같은 인자(정규 JSON, 키 정렬)의 호출이 이 drive에서 이미 받은 것과 **같은 결과**를 다시 받으면, 다음 요청의 `<runtime_state>`에 `progress.repeated` 항목으로 넣는다.
- **"같은 결과"는 기록 상태와 `ModelToolResult` 전체가 같다는 뜻이다 (재검수 R2).** 블록의 수·순서·종류, 글 블록의 글(바이트 그대로), 첨부 참조(`AttachmentRef`)의 `file_id`와 `version`, 그리고 `is_error`가 모두 같아야 한다. 글이 같아도 첨부나 `is_error`가 다르면 새 결과다.
  - 넣는 것: Tool, 몇 번째로 같은 결과인지, 상태, 효과 등급.
- 결과 자체는 바꾸지 않는다. 결과 불명(`UNKNOWN`)인 호출을 확인하는 일도 그대로다(V3-047).

**새 관찰이 없다는 근거가 분명할 때만 멈춘다 (재검수 N2).** Run 하나가 "새것이 없다"는 것은, 그 Run의 모든 호출이 아래 둘 중 하나일 때뿐이다.
1. **입력만으로 정해지는 거부의 반복:** Dispatcher가 실행 전에 규칙 `unknown_tool` 또는 `invalid_arguments`로 거부했고(아래의 기계 판독 출처), 이 drive에서 같은 Tool·같은 인자가 같은 규칙으로 이미 거부됐다. 같은 입력이면 언제나 같은 거부다.
2. **읽기 Tool의 같은 결과:** Tool의 효과 등급이 `READ`이고, 같은 Tool·같은 인자가 이 drive에서 이미 같은 결과(위의 정의: 상태와 `ModelToolResult` 전체)를 받았다. 새 관찰이 없다.

다음은 "새것 없음"으로 세지 않는다. 그런 호출이 하나라도 있는 Run은 연속을 끊는다. **이 제외가 위의 1·2보다 먼저다**(§17): 예를 들어 꺼진 MCP 서버의 읽기 Tool이 같은 거부를 되풀이해도, 상태에 따른 거부이므로 세지 않는다.
- `SCOPED_WRITE`·`UNKNOWN` 효과 Tool의 호출. 같은 성공 글을 돌려주면서 실제로는 매번 다른 효과(파일 추가, 상태 변경)를 낼 수 있다. 반복은 알리지만 멈출 근거로 쓰지 않는다.
- 상태에 따라 바뀌는 거부: 꺼진 출처, 비활성 Environment, binding. 턴 도중에도 바뀔 수 있다.
- Handler가 돌려준 거부(`DENIED`)와 실패(`FAILED`)의 반복. 단, `READ` Tool이 같은 결과를 다시 받은 경우는 2에 들어간다.
- 결과 불명, 취소, 새 결과.

그런 Run이 **연달아 O3번(권고 3)** 이면, 다음 Run 전에 `NO_PROGRESS`로 멈춘다. Tool 호출 없는 Run은 답이므로 턴이 끝난다.

**거부의 출처는 Dispatcher가 돌려준다 (재검수 N1·R1).**
- `dispatch_group`의 결과(`GroupDispatchResult`)에 `refusals: Mapping[CallId, RefusalRule]`을 더한다. Dispatcher가 **실행 전에 스스로 거부한 호출**과 그 규칙이다. Handler가 돌려준 `DENIED`는 여기에 없다. 그래서 둘을 혼동할 수 없다.
  - 규칙(`RefusalRule`, `tool/api.py`)은 둘로 나뉜다. **입력으로 정해지는 것:** `unknown_tool`, `invalid_arguments`. **상태에 따라 바뀌는 것:** `source_disabled`, `environment_inactive`, `binding`.
- **저장하지 않는다.** Control은 drive 하나를 한 프로세스 안에서 도는 루프로 돌리고, 셈도 그 drive 동안만 쓴다. 그래서 이 정보는 그 루프의 메모리에 두면 충분하다. Data 스키마도, `raw_result`도 바뀌지 않는다. Handler의 `raw_result`는 받은 그대로 기록한다(rev. 3의 예약 키와 그것을 빼는 규칙은 없앤다).
- 재시작 뒤에는 drive가 없으므로 셀 것도 없다. Continue는 새로 센다.
- Control은 이 `refusals`와, 이 drive에서 커밋된 실행 기록(상태와 `model_result`), Tool 정의의 효과 등급으로 센다. 새 영속 상태는 없다.

## 5. 연결된 클라이언트가 없을 때 (기본 켬)

- 이 장치가 재는 것은 **클라이언트 연결이 살아 있는지**이지, 사람이 실제로 보고 있는지가 아니다 (검수 C2). 지난 60초 안에 클라이언트가 그 대화를 읽었으면 "연결됨"이다.
  - Web은 작업 중 대화를 매초 읽고, CLI는 따라가며 이벤트를 읽는다. Application이 그 읽기를 Runner에 알린다(`seen(scope)`, 프로세스 메모리).
  - 그래서 탭을 열어 둔 채 자리를 떠나면 "연결됨"으로 남는다. 그 경우는 토큰 예산(§3)과 진척 없음(§4)이 막는다.
- 연결된 클라이언트 없이 **O2번(권고 20)** 의 Run이 지나면, Runner가 그 작업에 Pause를 요청한다(V3-P55와 같은 경로).
  - 다음 안전 경계에서 `PAUSED`로 멈추고, 이유는 "paused: no client was reading this conversation"이다.
  - **이유가 전달되는 길 (재검수 C3):** `StopSignal.pause(reason=None)`이 이유를 받는다. 이유는 처음 요청한 쪽의 것이 남는다. Runner의 `request_pause(scope, reason=None)`도 그것을 넘긴다. Driver는 `PAUSED`의 `DriveResult.detail`에 그 이유를 넣는다. 사용자가 누른 Pause는 이유가 없고, 지금처럼 detail이 비어 있다.
  - Web·CLI는 끝난 작업의 detail을 이미 보여 준다(경계 있는 글로). 그래서 화면 계약은 그대로이고, 상태 줄의 Continue와 함께 이유가 보인다.
  - 다시 들어온 사용자가 결과를 보고 Continue로 이어 간다.
  - 클라이언트가 돌아오면 세던 수는 처음부터 다시 센다.
- Run 수는 §7의 진행 보고로 Runner가 안다.
- 재시작 뒤에는 작업이 없다(P0, V3-045). 그러니 이 상태를 영속할 필요도 없다.

## 6. Run 수 상한 (선택, 기본 끔)

- **`HIMARI_MAX_RUNS`**(0 = 끔, 기본 0; 1~1000)를 켜면 지금처럼 그 수에서 `RUN_LIMIT_REACHED`로 멈춘다. Continue가 같은 상한을 다시 준다.
- 토큰 예산을 끄려는 운영자, 또는 단계 수로 엄격히 묶고 싶은 운영자를 위한 것이다.

## 7. 보이게 하기: Driver → Runner 진행 보고 (검수 B3)

**계약.**
- `drive`·`resume`·`compact`가 선택 인자 `progress: TurnProgress | None`을 받는다. `stop`과 같이 작업을 시작한 Runner가 넘긴다.
- Driver는 Run 하나의 결과(usage)를 기록할 때마다 `progress.report(TurnUsage)`를 부른다. 대화 Run과 요약 Run 모두다.
  - `TurnUsage`는 이 drive의 Run 수, 입력·출력·캐시 적중·캐시 쓰기 토큰의 합, 예산이다.
  - 보고는 동기 호출이고 예외를 내지 않는다(Runner는 값만 저장한다). Driver의 동작은 보고에 따라 달라지지 않는다.
- Runner는 그 작업의 최신 `TurnUsage`를 프로세스 메모리에 둔다. `status(scope)`는 `JobStatus.turn_usage`로 그것을 준다. 작업이 끝나면 마지막 값을 남긴다(다음 작업이 시작할 때까지).
- Runner는 같은 보고로 §5의 Run 수를 센다.

**화면.**
- **Web:** 작업 중 입력창 옆 안내에 "이 턴: 입력 132k · 출력 2.9k / 예산 1,000k"를 보인다. 멈추면 상태 줄에 이유와 **Continue 버튼**을 둔다(지금은 대화 메뉴 안에만 있음).
- **CLI:** 턴이 끝날 때 한 줄(`used 132,332 in + 2,850 out of 1,000,000 tokens in 7 steps`)을 찍는다. 멈춤에는 이유와 `/continue` 안내를 붙인다.
- `GET /api/server`가 예산·상한·연결 판정 설정을 알린다.

## 8. 모델에게 남은 예산을 알린다: 규칙은 지시문, 사실은 상태 (검수 B2·B4)

**데이터와 지시를 나눈다 (B2).** `<runtime_state>`는 JSON 데이터이고, 모델에게 그 안의 지시를 따르지 말라고 말한다. 그러니 거기에는 행동 지시를 넣지 않는다.
- **규칙은 HIMARI 지시문에 둔다.** 이 장치가 켜져 있으면 Bootstrap이 시스템 메시지의 HIMARI 지시문(`stable_prefix`)에 짧은 절 하나를 더한다. 매 요청 같은 글이므로 prefix 캐시를 깨지 않는다.

  ```text
  Turn budget. The runtime state may carry `turn_budget` and `progress`. When `turn_budget.low` is true,
  finish with what you have, or ask the user (ask_user) whether to go on. When `turn_budget.last` is true,
  this is likely the last step of the turn: tool calls made now run, but you see their results only if
  the user continues, so answer now or ask. `progress.repeated` lists calls that got the same result
  again in this turn: do not repeat them unless something has changed.
  ```

- **사실은 `<runtime_state>`에 둔다.** 숫자와 표시만 들어간다: `turn_budget: {"used", "budget", "left", "low", "last"}`, `progress: {"repeated": [...]}`. 문장으로 된 지시는 없다.
  - 요청 하나의 예상 비용은 "직전 요청의 입력 + 출력 예약"이다.
  - `low`는 남은 예산이 그 세 개보다 작을 때 참이다.
  - `last`는 남은 예산이 그 두 개보다 작을 때 참이다. 이 요청은 들어가지만 다음 요청은 들어가지 않을 것이라는 뜻이다. 들어가지 않는 요청은 보내지 않으므로, `last`는 실제로 보내는 마지막 요청에서 보인다(PR #47 검수 C1).
  - `turn_budget`은 `low`가 참일 때만 넣는다. 매번 예산을 보이면 모델이 일찍 포기할 수 있다.
  - §6이 켜져 있으면 남은 Run이 3 이하일 때 같은 표시를 쓴다.
- 이 상태는 요청의 맨 끝에 있다. 앞부분을 바꾸지 않으므로 prefix 캐시를 깨지 않고, 문맥 키에도 들어가지 않는다.

**토큰 추정에 넣는다 (B4).** 이 항목은 Run마다 바뀌지만 Data의 `state_revision`과 무관하다. 지금의 측정 기반 추정(`Compactor.estimate`)은 지난 요청의 측정값에 새 대화 항목을 더하고, 상태는 `state_revision`이 바뀔 때만 다시 더한다. 그러니 그대로는 이 항목의 변화가 빠진다.
- **규칙:** `ModelInput.turn_state`가 있으면, 측정 기반 추정에 **그 항목을 그린 크기 전체를 늘 더한다.** 지난 요청에 있던 같은 항목과 두 번 셀 수 있지만 보수적이다. 항목은 수백 토큰을 넘지 않는다(`repeated`는 최근 10개까지).
- 측정이 없을 때의 전체 추정(`request_tokens`)은 상태 블록을 그대로 재므로 이미 들어간다.
- 예산 판정(§3)과 압축 판정이 같은 추정을 쓴다.

## 9. 비용: 캐시가 대화 기록을 쓰게 한다

두 단계로 나눈다. 첫 단계는 비용 위험이 없고, 둘째 단계는 첫 단계의 측정을 본 뒤 정한다.

**B1. 대화마다 같은 공급자로 (지금 넣음).**
- Driver가 `RequestOptions.session_key`에 대화마다 고정된 불투명 값을 넣는다. 값은 `sha256("himari-session:" + conversation_id)`의 앞 32자리다.
- OpenRouter 어댑터는 이것을 요청 본문의 `session_id`로 보낸다. OpenRouter는 첫 요청부터 그 대화를 같은 공급자로 보낸다(마지막 요청 뒤 10분 유지).
- **적중을 보장하지는 않는다.** 그 공급자에 장애가 나면 OpenRouter가 다른 공급자로 보낸다. 측정(아래)이 실제 효과를 말한다.
- 대화 id 자체는 보내지 않는다. 원본 요청 기록(V3-018)에 이 값이 그대로 남는다.
- 모르는 공급자 어댑터는 이 값을 무시한다.

**B2. 명시적 캐시 구간 (측정으로 넣음, §13; O4 확인 대기).**
- 대화 기록의 마지막 메시지(`<runtime_state>` 바로 앞)에 캐시 구간 표시를 붙인다. 처음 설계는 지원한다고 등록된 모델에만 하려 했다. 구현은 OpenRouter 요청이면 늘 붙인다(§19).
- **공급자마다 표현이 다르다.** OpenRouter는 Anthropic식 `cache_control`을 OpenAI의 `prompt_cache_breakpoint`로 바꿔 주지만, 모델과 메시지 구조(내용 블록 배열이어야 하는지 등)마다 지원이 다르다. B2를 정할 때 모델별로 확인해 Provider 능력(`ProviderCapabilities`)에 적는다.
- 대가: GPT-5.6 이후는 캐시 쓰기가 입력 가격의 1.25배다. 적중률이 낮으면 손해다. 그래서 B1만으로 적중이 충분한지 먼저 잰다.

**측정 (B1과 함께).**
- `TokenUsage`에 `cache_write_tokens`(선택)를 더한다. OpenRouter 응답의 `prompt_tokens_details.cache_write_tokens`에서 읽는다. 이전 기록은 없음(None)으로 읽는다.
- 같은 Calliope 시험을 B1 전후로 3번씩 돌려 Run별 입력·캐시 적중·쓰기 토큰을 비교한다. 결과는 §13에 적는다.

**이번에 하지 않는 것:** 큰 Tool 결과 줄이기(예: `ply`의 claim graph). 서버의 답 형식은 서버 쪽 일이다(Calliope). HIMARI는 이미 결과를 64 KiB에서 자른다.

## 10. 계약 변경 (Proposed)

| 계약 | 변경 |
| --- | --- |
| Web 설정 → `bootstrap` | `HIMARI_TURN_TOKEN_BUDGET`(기본 O1), `HIMARI_UNATTENDED_RUNS`(기본 O2), `HIMARI_MAX_RUNS`(기본 0 = 끔). 예산 0과 상한 0을 함께 두면 시작 거부. `Application.open(turn_budget=, unattended_runs=, max_runs=)` → `build_runtime`. 장치가 켜져 있으면 §8의 지시문 절을 더함. `GET /api/server`에 알림 |
| Control `DriveState` | `BUDGET_REACHED`, `NO_PROGRESS` 추가 |
| Control (Driver) | 보내는 요청마다 예산 판정(대화 Run, 요약 호출; 출력 예약 포함); 반복 셈과 `NO_PROGRESS` 판정(§4, 실행은 막지 않음); `turn_state`를 `ModelInput`에 넣음; `RequestOptions.session_key`; `max_runs`는 선택(None = 상한 없음) |
| Control 진행 보고 | `drive`·`resume`·`compact`에 `progress: TurnProgress \| None`. Driver가 Run마다 `report(TurnUsage)`. `TurnUsage`·`TurnProgress`는 `control/api.py`에 |
| Control (Runner, `StopSignal`) | `seen(scope)`; 연결된 클라이언트 없이 O2번 Run이 지나면 `request_pause(scope, reason=…)`; `StopSignal.pause(reason=None)`와 그 이유를 `PAUSED`의 `detail`로; `JobStatus.turn_usage`(최신 보고) |
| `prompt/api.py` | `ModelInput.turn_state: TurnState \| None`(`turn_budget`, `progress`; 기본 None). `state_block`이 그 항목을 데이터로 그림. `RequestOptions.session_key: str \| None` |
| `prompt/compaction.py` | 측정 기반 추정이 `turn_state`의 크기 전체를 늘 더함(§8) |
| Tool (`tool/api.py`, Dispatcher) | `GroupDispatchResult.refusals: Mapping[CallId, RefusalRule]`(Dispatcher가 실행 전에 거부한 호출만); `RefusalRule` 다섯(입력: `unknown_tool`·`invalid_arguments`; 상태: `source_disabled`·`environment_inactive`·`binding`). `dispatch_group`의 인자와 기록(`raw_result` 포함)은 바뀌지 않음 |
| Provider (OpenRouter) | `session_key` → 본문 `session_id`; usage의 `cache_write_tokens`; `ProviderCapabilities.cache_breakpoints`가 켜진 모델이면 기록의 마지막 글 메시지에 `cache_control`(B2, §19) |
| `common.TokenUsage` / Data codec | `cache_write_tokens: int \| None`(선택). 이전 행은 None |
| Interfaces | Application이 대화 읽기(상세, 이벤트)를 `seen`으로 알림. Web: 사용량 안내, 새 상태 글, 상태 줄 Continue. CLI: 사용량 한 줄, 새 상태 글 |
| 바뀌지 않음 | Data 스키마(usage는 이미 JSON), 확실성 규칙, Pause·Cancel 의미, 압축의 커밋 규칙 |

## 11. 소유자 결정

방향(상한 없음 + §3~§9)은 정해졌다(2026-10-16). 남은 것은 기본 수치다.

| # | 질문 | 권고 |
| --- | --- | --- |
| O1 | 턴 토큰 예산 기본값 | **1,000,000**(이번 시험 한 턴의 약 5배) → **소유자 결정(2026-10-16): 3,000,000, 토큰으로 센다.** B2 뒤 입력의 75~84%가 캐시 적중이라, 같은 비용으로 더 길게 돈다(§13.1) |
| O2 | 연결된 클라이언트 없이 이어 갈 Run 수, 그리고 "연결됨"의 시간 | **20 Run, 60초** |
| O3 | `NO_PROGRESS`까지의 진척 없는 Run 연속 수 | **3** |
| O4 | 명시적 캐시 구간(B2) | **B1의 측정을 본 뒤 결정** → 측정(§13): B1은 효과 없음, B2가 원인을 고침. **넣음을 권고**(2단계에 구현, 소유자 확인 대기) |

## 12. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 상한이 없어 길 잃은 루프가 비용을 냄 | 토큰 예산(보내는 요청마다, 출력 예약 포함), 진척 없음 멈춤, 연결 없음 일시정지. 예산은 추정에 기대므로 마지막 한 Run의 오차만큼 넘을 수 있다 |
| 반복 판정이 정당한 호출을 막음 | 기록으로는 아무것도 막지 않는다. Dispatcher가 호출마다 그때의 상태를 다시 검사한다 |
| 진척 판정이 효과 있는 반복을 "새것 없음"으로 봄 | `READ` Tool의 같은 결과와 입력으로 정해지는 거부만 센다. 쓰기·불명 효과 Tool은 세지 않는다 |
| Handler의 거부가 Dispatcher의 거부로 읽힘 | 출처는 `dispatch_group`의 결과로만 전달된다. Handler의 결과와 기록은 건드리지 않는다 |
| 글만 같은 다른 결과를 반복으로 봄 | 상태와 `ModelToolResult` 전체(블록·첨부 참조·`is_error`)를 비교한다 |
| 진척 판정이 탐색 중인 모델을 너무 일찍 멈춤 | 새 결과가 하나라도 있는 Run은 진척으로 본다. 연속 O3번일 때만 멈추고, Continue로 이어 간다 |
| 연결 판정은 사람의 관찰이 아님(탭을 열어 둔 채 떠남) | 그 경우는 "연결됨"으로 남는다. 토큰 예산과 진척 없음이 여전히 막는다 |
| 예산 알림이 모델을 일찍 멈추게 함 | 여유가 두 Run 남짓일 때만 알린다 |
| 상태 데이터 안의 지시가 데이터 경계를 흐림 | 행동 규칙은 시스템 지시문(§8)에, 상태에는 숫자와 표시만 |
| `session_id`로 대화를 추적할 수 있음 | 대화 id가 아닌 해시를 보낸다. 같은 대화의 요청이 같은 공급자로 간다는 것 이상은 드러나지 않는다(요청 내용은 원래 그 공급자에게 간다) |
| 캐시 쓰기 비용이 적중 이득보다 큼 | B2는 측정 뒤 결정(O4) |

## 13. 검증 계획과 측정 기록

- **단위:**
  - 예산:
    - 대화 Run과 요약 호출 모두, 보내기 직전에 "쓴 양 + 입력 추정 + 출력 예약 > 예산"이면 보내지 않음;
    - 출력 예약(가장 큰 출력의 2배, 8,192 이상, 모델 최대 이하; 요약은 그 상한);
    - 압축 도중 멈추면 아무것도 커밋하지 않음;
    - usage가 없을 때 추정과 예약으로 셈;
    - Continue가 새 예산을 줌;
    - 예산 0과 상한 0을 함께 두면 시작 거부.
  - 진척:
    - 기록으로 막지 않음: 같은 호출이 몇 번 거부·실패했든 다음 호출은 Dispatcher가 그때 상태로 판정한다. 출처를 켠 뒤의 같은 호출은 실행된다.
    - 반복 알림: 같은 Tool·인자·상태·결과면 `progress.repeated`에 보이고(성공·실패, 모든 효과 등급) 결과는 그대로다.
    - `NO_PROGRESS`: `READ` Tool의 같은 결과만, 또는 `unknown_tool`·`invalid_arguments` 거부의 반복만 있는 Run이 O3번 연달아일 때 멈춘다.
    - 연속이 끊기는 경우: `SCOPED_WRITE`·`UNKNOWN` Tool의 같은 성공, 상태에 따른 거부, Handler의 `DENIED`·`FAILED`(읽기 Tool의 같은 결과 제외), 결과 불명, 새 결과가 하나라도 있으면 끊긴다.
    - 출처: Dispatcher가 실행 전에 거부한 호출만 `refusals`에 규칙과 함께 있다. Handler가 돌려준 `DENIED`는 없다. Handler의 `raw_result`는 받은 그대로 기록된다(`refused_by` 같은 키도 그대로).
    - 같은 결과: 글이 같아도 첨부 참조(`file_id`·`version`)나 `is_error`가 다르면 새 결과다. 블록 순서가 다르면 새 결과다.
    - Pause 이유: 연결 없음으로 멈추면 `detail`에 이유가 있고, 사용자의 Pause는 비어 있다. 처음 요청한 이유가 남는다.
  - 연결: 읽기가 없으면 O2번 Run 뒤 `PAUSED`(이유 포함), 읽기가 이어지면 계속, 돌아오면 다시 셈.
  - 진행 보고: Run마다 `report`가 불림(요약 Run 포함), `JobStatus.turn_usage`가 최신 값, 끝난 작업은 마지막 값을 남김, 보고 중 예외가 Driver를 바꾸지 않음.
  - 예산 표시:
    - 지시문 절은 장치가 켜졌을 때만, 매 요청 같은 글;
    - `turn_budget`은 `low`일 때만, `last`는 마지막 추정에서;
    - `<runtime_state>`에는 숫자와 표시만;
    - 앞부분 바이트는 같음.
  - 추정(B4): 측정 기반 추정이 `turn_state` 크기를 더함. 그 항목이 커지면 추정도 커짐.
  - Run 수 상한: 켰을 때 지금과 같음, 끄면 없음.
  - B1: 같은 대화의 요청은 같은 `session_id`, 다른 대화는 다른 값, 대화 id 자체는 본문에 없음. `cache_write_tokens`를 읽고 저장함, 이전 행은 None.
- **Web·CLI:** 사용량 안내, 새 상태와 상태 줄 Continue(브라우저 smoke), CLI 사용량 한 줄과 안내.
- **실제 모델:** 같은 Calliope 시험을 B1 전후로 3번씩. Run, 반복 차단·알림 발생, 예산 알림 뒤 행동, 입력·캐시 적중·쓰기 토큰을 아래에 적는다.

### 13.1 측정 기록 — 실제 모델(2026-10-16)

조건:
- 모델 `openai/gpt-6-luna`(OpenRouter, 응답의 공급자는 모두 OpenAI);
- Calliope main `1f9ef15`, AG5 시험과 같은 요청("10수(5...h6) 해설을 써서 저장"), 매번 새 대화와 새 Calliope 데이터;
- 대화 Run 한 번(`drive`), 예산 1,000,000.

**측정한 토큰** (공급자가 보고한 값):

| 시험 | Run | 입력 | 캐시 적중 | 캐시 쓰기 |
| --- | --- | --- | --- | --- |
| B1 끔(off3) | 5 | 80,400 | 11,212 (14%) | 69,173 (86%) |
| B1 켬(on1) | 5 | 103,559 | 11,212 (11%) | 92,332 (89%) |
| B1 켬(on2) | 10 | 219,423 | 28,220 (13%) | 191,173 (87%) |
| B1 켬(on3) | 8 | 162,277 | 22,614 (14%) | 139,639 (86%) |
| B2, B1 끔(b2off1) | 6 | 139,305 | 104,972 (75%) | 34,315 (25%) |
| B2, B1 켬(b2on1) | 8 | 170,311 | 143,363 (84%) | 26,924 (16%) |
| B2, B1 켬(b2on2) | 9 | 268,172 | 226,504 (84%) | 41,641 (16%) |

**입력 비용** (위 토큰에 단가를 곱한 계산값, PR #48 검수 C1):
- 단가는 `openai/gpt-6-luna`의 공시 단가다(OpenRouter `/api/v1/models`의 `pricing`, 2026-10-16). 입력 $0.10/M, 캐시 읽기 $0.01/M(0.1배), 캐시 쓰기 $0.125/M(1.25배). 272,000 토큰을 넘는 요청은 단가가 두 배지만, 이 시험의 요청은 모두 그보다 작다.
- 캐시 읽기 단가는 모델마다 다르다(다른 OpenAI 모델은 0.25배·0.5배). 그래서 아래 비율을 다른 모델에 그대로 옮길 수 없다.

| 시험 | 입력 비용 | 캐시가 없을 때 | 비율 |
| --- | --- | --- | --- |
| off3 / on1 / on2 / on3 | $0.0088 / $0.0117 / $0.0242 / $0.0177 | $0.0080 / $0.0104 / $0.0219 / $0.0162 | 109~113% |
| b2off1 / b2on1 / b2on2 | $0.0053 / $0.0048 / $0.0075 | $0.0139 / $0.0170 / $0.0268 | 28~38% |

(B1 끔의 처음 두 시험은 시험 스크립트의 잘못으로 돌지 않았다. B1의 결론은 다섯 시험의 같은 모양에 기댄다.)

- **B1만으로는 아무것도 바뀌지 않았다.** `session_id`를 보내든 안 보내든, 각 Run의 적중은 2,803 토큰(시스템 메시지와 Tool 목록 쯤)에서 멈췄다. 나머지는 Run마다 새로 캐시에 **썼다.** 쓰기가 1.25배라 캐시가 없을 때보다 약 10% 비쌌다.
- **원인 (저장된 요청을 다시 보내 확인):**
  - 요청은 덧붙기만 한다. Run n+1의 메시지는 Run n의 메시지에서 마지막 `<runtime_state>` 메시지만 빼고 같다. 공급자도 같았다.
  - 같은 요청을 그대로 다시 보내면 26,893/26,896 토큰이 적중했다.
  - 같은 요청에서 마지막 상태 메시지만 빼고 보내면 2,803만 적중했다. 이전 요청의 **앞부분**은 다시 쓰이지 않는다.
  - 다음 Run에 이전 Run의 상태 메시지를 남겨 두면 26,386/26,645가 적중했다.
  - 추론 상태(`reasoning_details`)를 빼도 2,803이었다. 원인이 아니다.
  - 그러므로 이 공급자는 캐시를 **이전 요청의 끝**(그리고 몇몇 고정점)까지만 다시 쓴다. HIMARI는 상태를 요청의 끝에 두고 다음 요청에서는 빼므로, 이전 요청의 끝이 다음 요청에 없다. 2026-10-16의 첫 진단("Run마다 2,691만 적중, 공급자·경로 쪽 원인")은 틀렸다. 원인은 HIMARI 요청의 모양이었다.
- **B2로 고쳐짐:** 시스템 메시지에 임의 값을 넣어 캐시가 비어 있는 같은 요청 둘(Run 4, Run 5)을 보냈다. 표시가 없으면 Run 5가 2,815, 기록의 마지막 메시지에 `cache_control`을 붙이면 35,112/36,364가 적중했다. 위 표의 B2 시험이 실제 대화에서 같은 결과를 보인다: 적중 75~84%, 이 모델의 단가로 입력 비용 28~38%.
- **B1은 남긴다.** 비용이 없고, OpenRouter가 권하는 방식이며, 공급자가 여럿인 모델에서 같은 공급자로 보내는 데 쓰인다. 이번 측정에서 그 효과는 보이지 않았다(B2에서 끔 75%, 켬 84%, 표본 하나).
- **예산 알림(`last`)이 모델에 닿는지(§18.2의 권고):** 예산 100,000으로 한 번 돌렸다(B2 전).
  - 4번째 요청에서 `low`, 5번째 요청에서 `last`가 모델에 갔다(남은 42,606, 예상 비용 32,404의 두 배 미만).
  - 모델은 그 요청에서 `submit_commentary`를 불러 해설을 저장했다(저장 확인).
  - 그 뒤 마지막 답(6번째 요청)은 예산에 들어가지 않아 `BUDGET_REACHED`로 멈췄다. detail: "the turn used 82509 tokens; the next request (about 25041 in, 8192 kept for its answer) would pass the budget of 100000".
  - `last`는 이번 한 번 모델에 닿았다(1/1). 일은 저장됐지만 사용자에게 줄 답은 없었다. Continue하면 답을 얻는다.
- **반복 알림·`NO_PROGRESS`:** 이 시험들에서는 일어나지 않았다(`progress.repeated` 0). 모든 턴이 5~10 Run에 끝났다.

## 14. 검수 처리 — rev. 1(`f4c73ce`), 독립 검수: NOT_READY

| # | 지적 | 처리 |
| --- | --- | --- |
| B1 | 출력 토큰이 사전 판정에 없고, 압축의 요약 호출에도 같은 판정이 필요 | §3: 보내는 요청마다(대화 Run, 요약 호출) 직전에 판정하고, 출력 예약(가장 큰 출력의 2배, 8,192 이상, 모델 최대 이하; 요약은 그 상한)을 더한다. 압축 도중 멈추면 아무것도 커밋하지 않는다. "과금의 절대 상한이 아니다"를 명시했다 |
| B2 | `<runtime_state>`(데이터, 지시 무시)에 행동 지시를 넣음 | §8: 행동 규칙은 시스템 메시지의 HIMARI 지시문에 고정된 절로(prefix 캐시 유지), 상태에는 숫자와 표시(`low`, `last`, `repeated`)만 |
| B3 | Driver → Runner 진행 보고 계약이 없음 | §7: `drive`·`resume`·`compact`의 `progress: TurnProgress`, Run마다 `report(TurnUsage)`, Runner가 `JobStatus.turn_usage`로 줌; 연결 판정의 Run 수도 같은 보고로 |
| B4 | Run마다 바뀌는 항목이 측정 기반 추정에 빠짐 | §8: `turn_state`가 있으면 측정 기반 추정에 그 크기 전체를 늘 더한다(보수적; 항목은 수백 토큰 이내) |
| C1 | `FAILED`는 일시적일 수 있음 | §4: 실행을 막는 것은 HIMARI의 규칙 거부(`DENIED`)뿐. `FAILED`는 실행하고 `progress.repeated`로 알린다. 비교는 상태와 모델이 받은 정규화된 결과 글로. *(rev. 3에서 기록에 따른 실행 차단 자체를 없앴다, §15)* |
| C2 | "자리 비움"은 사람의 관찰이 아님 | §5: "연결된 클라이언트가 없을 때"로 이름과 설명을 고쳤다(`seen`) |
| — | `session_id`는 적중을 보장하지 않음; B2의 공급자별 표현 | §9에 적었다 |

## 15. 검수 처리 — rev. 2(`a7f5a3b`), 재검수: NOT_READY

B1~B4, C1·C2는 해결로 확인됐다.

| # | 지적 | 처리 |
| --- | --- | --- |
| N1 | `DENIED`만으로는 Dispatcher의 실행 전 거부와 Handler가 돌려준 거부를 구분할 수 없음 | §4: Dispatcher가 자기 거부의 `raw_result`에 `refused_by: dispatcher`와 규칙 코드를 남기고, 그 키를 예약해 Handler의 결과에서 뺀다. 판정은 이 출처와 규칙 코드로 한다. *(rev. 4에서 바꿈: 기록 대신 `dispatch_group`의 결과로, §16)* |
| N2 | 쓰기 Tool의 같은 성공 글은 진척 없음이 아님 | §4: "새것 없음"은 `READ` Tool의 같은 결과와 입력으로 정해지는 거부(`unknown_tool`, `invalid_arguments`)의 반복뿐이다. `SCOPED_WRITE`·`UNKNOWN`의 반복은 알리지만 멈출 근거로 쓰지 않는다 |
| N3 | 꺼진 출처·비활성 Environment는 턴 도중에도 바뀜 | §4: 기록을 근거로 실행을 막는 규칙(`refuse`)을 없앴다. Dispatcher가 호출마다 그때 상태를 다시 검사한다. 상태에 따른 거부는 "새것 없음"으로도 세지 않는다 |

## 16. 검수 처리 — rev. 3(`becc3cf`), 재검수: NOT_READY

N1~N3는 해결로 확인됐다.

| # | 지적 | 처리 |
| --- | --- | --- |
| R1 | 예약 키 `refused_by`를 Handler의 `raw_result`에서 빼면 실제 Tool 출력이 기록에서 달라짐 | §4: 기록은 건드리지 않는다. 거부의 출처는 `dispatch_group`의 결과(`GroupDispatchResult.refusals`)로 Dispatcher가 Control에 직접 돌려준다. drive는 한 프로세스의 루프라 메모리로 충분하다. Data 스키마와 `raw_result`는 그대로다 |
| R2 | 결과 글만 비교하면 첨부 참조나 `is_error`의 차이를 놓침 | §4: "같은 결과"는 기록 상태와 `ModelToolResult` 전체(블록의 수·순서·종류, 글, 첨부의 `file_id`·`version`, `is_error`)가 같은 것 |
| C3 | 자동 Pause의 이유를 전달할 계약이 없음 | §5: `StopSignal.pause(reason=)`, `request_pause(scope, reason=)`, Driver가 `PAUSED`의 `detail`에 이유를 넣음. 화면은 끝난 작업의 detail을 이미 보여 준다 |

## 17. 재검수 — rev. 4(`3145b43`): READY

R1·R2·C3는 해결로 확인됐다. 비차단 권고 둘을 구현의 규칙과 시험으로 적는다.

| # | 권고 | 처리 |
| --- | --- | --- |
| 1 | 상태에 따른 거부의 제외가 `READ`의 같은 결과보다 먼저임을 분명히 | §4: 제외가 먼저다. 꺼진 서버의 읽기 Tool이 같은 `DENIED`를 되풀이해도 `NO_PROGRESS`의 근거가 아니다. 구현 시험에 넣는다 |
| 2 | 압축 도중의 Pause에서도 자동 Pause의 이유가 남아야 함 | 지금 Driver는 압축 도중 Pause에 고정 문구를 `detail`로 준다. 이유가 있으면 그것을 앞에 둔다. 구현 시험에 넣는다 |


## 18. 구현 노트 — 1단계: Control·Tool·Prompt·Provider (2026-10-16)

계약과 판정을 구현했다. 환경 변수, Application 연결, Web·CLI 표시와 실제 모델 측정은 2단계다. 그래서 이 단계만으로는 기본 동작이 바뀌지 않는다: `build_runtime`의 기본은 여전히 Run 상한 16, 예산 없음이다.
- **`control/turn.py`(새)의 `Turn`:** drive 하나의 셈(프로세스 메모리). 사용량, 출력 예약, `fits`, `state()`(모델에게 줄 데이터), `observe()`(진척)를 둔다. Driver가 `_exclusive` 안에서 drive마다 하나 만든다.
- **Driver:**
  - Run 상한은 선택(`max_runs_per_drive=None`)이다. 상한도 예산도 없으면, 또는 예산은 있는데 문맥 예산(추정의 출처)이 없으면 만들 때 거부한다.
  - 대화 Run은 `_fit_budget`의 추정으로, 요약 호출은 그 요청의 `request_size`와 요약 상한으로, 보내기 직전에 판정한다.
  - 보낸 뒤에는 보고된 usage로 센다. 보고가 없으면 추정과 예약으로 센다.
  - 그룹이 끝나면 그 기록과 `refusals`, Tool 정의의 효과 등급으로 `observe`한다.
  - 모든 요청에 `RequestOptions.session_key`를 넣는다(요약 호출도 같은 값).
  - Pause의 이유는 `PAUSED`의 `detail`로 간다. 압축 도중이면 "이유; paused between summarizing calls; nothing was committed"다(§17의 2).
- **찾은 것 (구현 중):** 출력이 보고되지 않은 Run을 예약만큼 센 뒤 그 값을 "가장 큰 출력"으로 다시 쓰면, 예약이 두 배씩 불어난다(시험에서 8,192 → 8,388,608). 그래서 다음 예약을 정하는 것은 **보고된 출력뿐**이다. 보고가 없는 Run은 그 Run의 예약만큼 셀 뿐이다.
- **Tool:** `RefusalRule`(다섯)과 `GroupDispatchResult.refusals`. Dispatcher의 각 거부 지점이 규칙을 단다. 기록과 `raw_result`는 그대로다(Handler가 `refused_by` 키를 써도 그대로 남는 것을 시험으로 확인).
- **Prompt:**
  - `TurnBudget`·`RepeatedCall`·`TurnState`, `ModelInput.turn_state`.
  - `turn_state_data`가 숫자와 표시만 만든다. `state_block`이 그것을 상태 JSON에 넣는다.
  - 측정 기반 추정이 그 크기를 늘 더한다(B4).
- **Provider·Data:** OpenRouter가 `session_id`를 보내고 `prompt_tokens_details.cache_write_tokens`를 읽는다. `TokenUsage.cache_write_tokens`; codec은 그 키가 없는 이전 행을 None으로 읽는다.
- **Runner:**
  - 작업마다 `TurnProgress`를 넘기고, 최신 보고를 `JobStatus.turn_usage`로 둔다.
  - `seen(scope)`로 클라이언트 연결을 기록한다. 작업을 시작한 클라이언트는 연결된 것으로 본다.
  - `unattended_runs`(기본 None = 끔)와 `seen_seconds`(60)로, 연결 없이 그만큼의 Run이 지나면 이유와 함께 Pause한다.
- **Bootstrap:** `build_runtime(max_runs_per_drive=, turn_budget=, no_progress_runs=)`. 예산이 있으면 HIMARI 지시문 끝에 `TURN_GUIDANCE` 절을 더한다.
- **시험(`tests/control/test_turn_budget.py`, 18개):**
  - 상한 없이 21 Run, 선택 상한과 그 detail, 설정 거부;
  - 보내기 전 예산 판정과 Continue의 새 예산, 요약 호출의 판정(아무것도 커밋하지 않음), 출력 예약;
  - `low`·`last`와 상태 블록의 데이터, 지시문 절의 유무;
  - 측정 기반 추정이 상태 크기만큼 커짐;
  - "새것 없음" 판정 규칙: READ, 쓰기, 새 결과, 첨부·`is_error` 차이, 입력 거부, 상태 거부(우선), Handler의 거부, 결과 불명;
  - 반복 알림, 읽기 루프의 `NO_PROGRESS`와 쓰기 루프의 완료, 꺼진 출처가 매번 판정되고 켠 뒤 실행됨;
  - Dispatcher의 `refusals`와 Handler 기록 보존;
  - 대화별 `session_key`와 요청 본문의 `session_id`, 캐시 쓰기 토큰과 이전 행;
  - Pause 이유(압축 도중 포함, 처음 이유가 남음);
  - Runner의 사용량과 연결 없음 Pause, 연결된 동안은 계속.

### 18.1 검수 — PR #47(`af5c6e7`): NOT_READY

- **B1 — 1단계만으로도 기본 동작이 바뀜.** `no_progress_runs`의 기본이 3이라 `NO_PROGRESS`가 늘 켜져 있었다. 기본 설정에서도 `progress.repeated`와 남은 Run 수가 요청에 들어갔다.
  - **고침:** `NO_PROGRESS`는 `no_progress_runs`를 줄 때만 켠다(기본 None). 턴 상태 데이터(`turn_state`)와 지시문 절은 토큰 예산이 켜졌을 때만 넣는다. 둘은 함께 다닌다.
  - 이제 기본 설정(Run 상한 16, 예산 없음)에서 바뀌는 것은 둘뿐이다: 요청 본문의 `session_id`, usage의 `cache_write_tokens` 기록. 2단계가 V3-P62의 기본값(예산 O1, O2, O3)을 켠다.
- **C1 — `last`가 모델에 닿지 않을 수 있음.** `last`가 "다음 요청이 들어가지 않음"일 때 켜졌다. 그런데 그 요청은 보내지 않으므로 모델이 볼 수 없었다.
  - **고침:** §8의 정의를 한 요청 앞당겼다. `low`는 예상 비용 세 개 미만, `last`는 두 개 미만이다.
- **C2 — 기본 동작 회귀 시험.** 기본 설정에서 같은 결과를 되풀이하는 읽기 Tool이 16번까지 실행되고, 요청에 턴 상태와 지시문이 없음을 시험한다. 고치기 전 코드에서는 실패한다.

### 18.2 재검수 — `a40ce5d`: READY

B1·C2는 해결, C1은 개선으로 확인됐다. 비차단 권고 하나: `last`는 직전 요청의 입력으로 다음 요청의 비용을 어림한다. 그래서 다음 요청이 그보다 크면 모델이 알림을 보기 전에 예산에 닿을 수 있다. 보내기 전 판정은 따로 있으므로 안전에는 영향이 없다. 2단계의 실제 모델 시험에서 알림이 모델에 닿는 비율을 잰다.


## 19. 구현 노트 — 2단계: 설정·Application·Web·CLI (2026-10-16)

V3-P62의 기본값을 켰다. 이제 기본 서버에는 Run 수 상한이 없다.
- **기본값(§11):** O1 3,000,000 토큰(소유자 결정, 2026-10-16: 권고 1,000,000에서 올림; 토큰으로 센다), O2 20 Run·60초, O3 3(권고값, 소유자 확인 대기). O4는 아래 측정을 보고 정한다.
- **설정(`interfaces/web/config.py`):**
  - `HIMARI_TURN_TOKEN_BUDGET`(기본 3,000,000, `0` = 끔, 16,384~10⁹; 최소는 출력 예약 8,192의 두 배, PR #48 검수 C2);
  - `HIMARI_UNATTENDED_RUNS`(기본 20, `0` = 끔, 1~1,000);
  - `HIMARI_MAX_RUNS`(기본 `0` = 없음, 1~1,000).
  - 예산과 상한을 둘 다 끄면 시작을 거부한다(`WebConfigError`, `Application.open`도 `ValueError`).
- **Application:**
  - `Application.open(turn_budget=, unattended_runs=, max_runs=)`가 `build_runtime`(예산, 선택 상한, `no_progress_runs=3`)과 `BackgroundRunner`(`unattended_runs`, `seen_seconds=60`)에 넘긴다.
  - 쓰인 값은 `Application.turn`(`TurnSettings`)에 있다.
  - `Application.seen(scope)`는 Runner에 클라이언트 연결을 알린다.
- **Web:**
  - 대화 상세(`GET /api/conversations/{id}`)와 이벤트 읽기가, 권한 확인을 통과한 **뒤에만** `seen`을 부른다. 남의 대화나 없는 대화를 읽으면 세지 않는다(시험).
  - `job.turn_usage`(`runs`, 입력·출력·캐시 적중·캐시 쓰기 토큰, `budget`, `max_runs`). `/api/server`는 `turn`(쓰이는 설정)을 준다.
  - 작업 중 입력창 아래: "HIMARI is working… · 45k in · 1.0k out / 100k".
  - 답 없이 멈춘 턴: 입력창 위의 패널에 이번 턴의 사용량과 **Continue** 버튼을 둔다. 가드가 멈췄든, Pause든, 실패든 같다. 멈춘 이유는 상태 줄(live region)에 그대로 둔다. 같은 글을 두 곳에 두지 않으려고 패널에는 사용량만 쓴다. 대화 메뉴의 Continue도 남는다.
  - 새 상태 `budget_reached`·`no_progress`의 글. Pause와 "Not answered yet"의 안내는 이제 "Continue"만 말한다.
- **CLI:**
  - 턴이 끝나면 한 줄: `(used 90k in + 2.0k out of 100k tokens in 2 requests)`.
  - 새 상태의 글은 `continue`로 이어 가라고 말한다(`budget_reached`는 새 예산을 받는다는 것도).
- **시작 줄:** "turn budget: 3000000 tokens, no step limit, pauses after 20 steps unread".
- **B2 — 캐시 구간 (측정 §13.1 뒤에 넣음):**
  - OpenRouter 어댑터가 대화 기록의 마지막 글 메시지(시스템 메시지 뒤, `<runtime_state>` 앞)의 `content`를 `[{"type": "text", "text": …, "cache_control": {"type": "ephemeral"}}]`로 보낸다.
  - 글이 없는 메시지(Tool 호출만 있는 assistant)는 건너뛰고, 그 앞의 글 메시지에 붙인다. 표시는 한 요청에 하나다.
  - 이전 메시지는 보낸 그대로라서, 표시가 붙은 메시지까지가 다음 요청의 앞부분이다.
  - **측정한 모델에만 붙인다**(§9의 처음 설계대로, PR #48 검수 B1). `ProviderCapabilities.cache_breakpoints`(기본 False)가 켜진 모델은 지금 `openai/gpt-6-luna` 하나다(§13.1에서 측정). 다른 모델(`gpt-5-nano`, `claude-haiku-5.5`, 목록에 없는 모델)의 요청은 B2 전과 바이트까지 같다. 명시적 캐시 지원은 모델·공급자마다 다르므로, 모델을 더할 때는 §13.1처럼 재고 켠다.
- **시험:**
  - 설정: 기본값, `0`, 범위, 둘 다 끔;
  - Application: 연결(Driver의 예산·상한·진척, Runner의 연결 없음), 둘 다 끔 거부;
  - Web: `turn_usage`, 예산에서 멈춘 뒤 Continue가 새 예산으로 끝냄, 권한 확인 뒤에만 `seen`;
  - CLI: 토큰 표기, 상태별 안내, 실제 서버로 예산 멈춤과 `continue`.
  - 브라우저 smoke(`scripts/ui_smoke.py`의 세 번째 서버): 작업 중 사용량, 예산에서 멈춤, 패널, 좁은 화면, Continue로 끝냄.
  - Provider: 기록의 끝이 캐시 구간이고 상태는 그 뒤에 있음, 다음 요청에서 이전 메시지는 보낸 그대로, 글 없는 Tool 호출 메시지는 건너뜀, 측정하지 않은 모델은 같은 대화에서 표시만 빠진 같은 본문.
- **검수 — PR #48(`b289acc`): NOT_READY**
  - **B1 — B2가 모든 OpenRouter 모델에 붙음.** 고침: 위의 `cache_breakpoints`. 측정한 모델에만 붙인다.
  - **C1 — 비용 계산의 단가가 가정이었음.** 고침: §13.1에서 측정한 토큰과 계산한 비용을 나눴다. 비용은 그 모델의 공시 단가로 계산했다. 공시 단가가 가정과 같아 수치는 그대로다.
  - **C2 — 받아들인 설정값으로 요청을 하나도 못 보낼 수 있음.** 고침: `HIMARI_TURN_TOKEN_BUDGET`의 최소를 1,000에서 16,384(출력 예약 8,192의 두 배)로 올렸다. README에 예약을 적었다.
