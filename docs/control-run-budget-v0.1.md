# 한 턴의 예산과 비용 — v0.1 (V3-P62, 설계 초안)

> 2026-10-16 · **Proposed.** V3-P23(drive당 Run 예산 16)을 대신한다. **소유자 방향(2026-10-16):** 다른 에이전트처럼 Run 수 상한 없이 돌게 한다. 그 대신 비용·진척·자리 비움을 지키는 장치를 둔다(§2~§6, 제시한 방법 모두 채택). 수치 기본값은 §11의 O1~O4.
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
| **토큰 예산**(§3) | `BUDGET_REACHED` (새) | Continue: 새 예산 | 켬 |
| **진척 없음**(§4) | `NO_PROGRESS` (새) | Continue | 켬 |
| **자리 비움**(§5) | `PAUSED`(이유: 아무도 보고 있지 않음) | Continue | 켬 |
| Run 수 상한(선택, §6) | `RUN_LIMIT_REACHED` | Continue | 끔 |
| 문맥 한도(지금처럼, 압축으로 못 줄일 때) | `REQUEST_REJECTED` | — | 늘 |

- 어떤 경우든 멈추기 전에 커밋한 것은 그대로다. 멈춤은 다음 Run을 시작하기 **전**의 안전 경계에서만 일어난다.
- 멈춤 요청(Pause·Stop)이 예산·진척보다 우선한다(V3-P55 ③과 같은 규칙).
- Continue는 V3-P55의 `resume`이다. 새 상태도 같은 길로 이어 간다.

## 3. 토큰 예산 (기본 켬)

- **`HIMARI_TURN_TOKEN_BUDGET`**(기본 O1, 권고 1,000,000)이 한 drive가 쓸 수 있는 토큰이다.
  - **센 것:** 그 drive의 모든 Run(대화 Run과 압축 요약 Run)이 공급자가 보고한 입력 + 출력 토큰. 캐시 적중분도 입력으로 센다(값을 단순하고 보수적으로 둔다). 캐시 적중·쓰기는 따로 보인다(§7).
  - 0이면 끈다. 단, §6의 Run 수 상한이 켜져 있을 때만 끌 수 있다. 둘 다 끄는 설정은 시작 때 거부한다.
- **보내기 전에 판정한다.** 다음 Run을 보내기 전, 지금까지 쓴 양과 다음 요청의 추정 크기(압축 계획이 이미 쓰는 추정, `ContextEstimate`)를 더한다. 예산을 넘으면 보내지 않고 `BUDGET_REACHED`로 멈춘다.
  - 그래서 예산을 넘는 것은 마지막 응답의 출력 정도다. 출력 상한은 따로 두지 않는다(V3-P23과 같음).
  - 공급자가 usage를 보고하지 않으면 그 Run은 추정치로 센다.
- **Continue는 새 예산을 준다.** 사용자가 보고 결정했기 때문이다.
- **근거(O1):** 이번 시험의 한 턴이 81k~192k를 썼다. "왜"까지 가는 해설은 그 몇 배가 들 수 있다. 1,000,000은 그 다섯 배쯤이고, 길 잃은 루프 하나가 낼 수 있는 비용의 상한이다.

## 4. 진척 없음 (기본 켬)

**같은 확정 실패의 세 번째는 실행하지 않는다.**
- 한 drive에서 같은 Tool·같은 인자(정규 JSON, 키 정렬)의 호출이 **같은 결과 글**로 두 번 확정 실패(`FAILED`·`DENIED`)했으면, 세 번째는 실행하지 않는다.
- `DENIED`로 기록하고, 모델에게는 다음을 준다: "Not run: this exact call (same tool and arguments) already failed twice in this turn with the same result. Change the arguments, use another tool, or answer with what you have."
- 결과 불명(`UNKNOWN`)·취소·성공은 세지 않는다. 결과를 모르는 호출을 확인하는 일은 막지 않는다(V3-047).
- 일시적 실패의 재시도는 두 번까지 된다.

**같은 결과의 반복을 알린다.**
- 같은 Tool·같은 인자가 이 drive에서 이미 받은 것과 **같은 결과**를 다시 받으면, 다음 요청의 `<runtime_state>`에 `progress.repeated` 항목을 넣는다(무엇이 몇 번째로 같은 결과인지).
- 결과 자체는 바꾸지 않는다. 상태가 변하는 조회(목록, 진행 상황)는 같은 인자로 다른 답을 줄 수 있으므로, 성공한 호출은 실행을 막지 않는다.

**진척 없는 Run이 이어지면 멈춘다.**
- "새것이 없는 Run": 그 Run의 모든 호출이 위의 반복(같은 결과)이거나, 실행하지 않은 세 번째 실패다.
- 그런 Run이 **연달아 O3번(권고 3)** 이면, 다음 Run 전에 `NO_PROGRESS`로 멈춘다.
- Tool 호출 없는 Run은 답이므로 턴이 끝난다.

**어디서.** Control(Driver)이 이 drive에서 커밋된 실행 기록으로 센다. 새 영속 상태는 없다.
- 실행하지 않을 호출과 그 이유는 Tool 모듈의 `dispatch_group(…, refuse=)`에 넘긴다. Dispatcher는 그 호출을 검증·claim 없이 `DENIED`로 커밋하고, 같은 그룹의 다른 호출은 그대로 실행한다.
- 이것은 의미론적 복구가 아니라 "실행하지 않음"과 "알림"이다(V3-005).

## 5. 자리 비움 (기본 켬)

- **보고 있다**는 것은, 지난 60초 안에 클라이언트가 그 대화를 읽었다는 뜻이다. Web은 작업 중 대화를 매초 읽고, CLI는 따라가며 이벤트를 읽는다. Application이 그 읽기를 Runner에 알린다(`watched(scope)`, 프로세스 메모리).
- 보는 사람이 없는 동안 **O2번(권고 20)** 의 Run이 지나면, Runner가 그 작업에 Pause를 요청한다(V3-P55와 같은 경로).
  - 다음 안전 경계에서 `PAUSED`로 멈추고, 이유는 "paused while nobody was watching"이다.
  - 다시 들어온 사용자가 결과를 보고 Continue로 이어 간다.
  - 보는 사람이 돌아오면 세던 수는 처음부터 다시 센다.
- 사람이 지켜보는 동안에는 이 장치가 걸리지 않는다. 그래서 "보고 있으면 상한 없이, 자리를 비우면 멈춤"이 된다.
- 재시작 뒤에는 작업이 없다(P0, V3-045). 그러니 이 상태를 영속할 필요도 없다.

## 6. Run 수 상한 (선택, 기본 끔)

- **`HIMARI_MAX_RUNS`**(0 = 끔, 기본 0; 1~1000)를 켜면 지금처럼 그 수에서 `RUN_LIMIT_REACHED`로 멈춘다. Continue가 같은 상한을 다시 준다.
- 토큰 예산을 끄려는 운영자, 또는 단계 수로 엄격히 묶고 싶은 운영자를 위한 것이다.

## 7. 보이게 하기

- **작업 상태(`JobStatus`)에 `turn_usage`:** 이 drive의 Run 수, 입력·출력·캐시 적중·캐시 쓰기 토큰, 예산. Driver가 Run마다 갱신하는 프로세스 메모리 값이다.
- **Web:** 작업 중 입력창 옆 안내에 "이 턴: 입력 132k · 출력 2.9k / 예산 1,000k"를 보인다. 멈추면 상태 줄에 이유와 **Continue 버튼**을 둔다(지금은 대화 메뉴 안에만 있음).
- **CLI:** 턴이 끝날 때 한 줄(`used 132,332 in + 2,850 out of 1,000,000 tokens in 7 steps`)을 찍는다. 멈춤에는 이유와 `/continue` 안내를 붙인다.
- `GET /api/server`가 예산·상한·자리 비움 설정을 알린다.

## 8. 모델에게 남은 예산을 알린다

- **남은 토큰 예산이 직전 요청 입력의 2배보다 작아지면**(대략 두 Run 남짓 남음), `<runtime_state>`에 `budget` 항목을 넣는다: `{"used": …, "budget": …, "left": …, "note": …}`.
- `note`는 둘 중 하나다.
  - 여유가 조금 남았을 때: "Little budget is left in this turn: finish with what you have, or ask the user (ask_user) whether to go on."
  - 다음 요청이 마지막으로 보일 때: "This is likely the last step of this turn's budget. Tool calls made now will run, but you will see their results only if the user continues: answer now, or ask the user."
- §6이 켜져 있으면 남은 Run이 3 이하일 때 같은 방식으로 알린다.
- **왜 `<runtime_state>`인가.** 매 요청의 맨 끝, 데이터로 보이는 자리다(CPP). 앞부분을 바꾸지 않으므로 prefix 캐시를 깨지 않는다. 문맥 키에도 들어가지 않고, 토큰 추정은 상태 블록을 그대로 재므로 저절로 들어간다(V3-P60 §14.2.1).
- 그보다 이른 단계에는 넣지 않는다. 매번 예산을 보이면 모델이 일찍 포기할 수 있다.

## 9. 비용: 캐시가 대화 기록을 쓰게 한다

두 단계로 나눈다. 첫 단계는 비용 위험이 없고, 둘째 단계는 첫 단계의 측정을 본 뒤 정한다.

**B1. 대화마다 같은 공급자로 (지금 넣음).**
- Driver가 `RequestOptions.session_key`에 대화마다 고정된 불투명 값을 넣는다. 값은 `sha256("himari-session:" + conversation_id)`의 앞 32자리다.
- OpenRouter 어댑터는 이것을 요청 본문의 `session_id`로 보낸다. OpenRouter는 첫 요청부터 그 대화를 같은 공급자로 보낸다(마지막 요청 뒤 10분 유지).
- 대화 id 자체는 보내지 않는다. 원본 요청 기록(V3-018)에 이 값이 그대로 남는다.
- 모르는 공급자 어댑터는 이 값을 무시한다.

**B2. 명시적 캐시 구간 (측정 뒤 O4로 정함).**
- 대화 기록의 마지막 메시지(`<runtime_state>` 바로 앞)에 캐시 구간 표시(`cache_control`)를 붙인다. 이것을 지원한다고 등록된 모델에만 한다.
- 대가: GPT-5.6 이후는 캐시 쓰기가 입력 가격의 1.25배다. 적중률이 낮으면 손해다. 그래서 B1만으로 적중이 충분한지 먼저 잰다.

**측정 (B1과 함께).**
- `TokenUsage`에 `cache_write_tokens`(선택)를 더한다. OpenRouter 응답의 `prompt_tokens_details.cache_write_tokens`에서 읽는다. 이전 기록은 없음(None)으로 읽는다.
- 같은 Calliope 시험을 B1 전후로 3번씩 돌려 Run별 입력·캐시 적중·쓰기 토큰을 비교한다. 결과는 §13에 적는다.

**이번에 하지 않는 것:** 큰 Tool 결과 줄이기(예: `ply`의 claim graph). 서버의 답 형식은 서버 쪽 일이다(Calliope). HIMARI는 이미 결과를 64 KiB에서 자른다.

## 10. 계약 변경 (Proposed)

| 계약 | 변경 |
| --- | --- |
| Web 설정 → `bootstrap` | `HIMARI_TURN_TOKEN_BUDGET`(기본 O1), `HIMARI_UNATTENDED_RUNS`(기본 O2), `HIMARI_MAX_RUNS`(기본 0 = 끔). 예산 0과 상한 0을 함께 두면 시작 거부. `Application.open(turn_budget=, unattended_runs=, max_runs=)` → `build_runtime`. `GET /api/server`에 알림 |
| Control `DriveState` | `BUDGET_REACHED`, `NO_PROGRESS` 추가 |
| Control (Driver) | 예산 판정(보내기 전), 진척 셈과 `refuse`, `progress`·`budget`을 `ModelInput`에 넣음, `RequestOptions.session_key`, `turn_usage` 갱신. `max_runs`는 선택(None = 상한 없음) |
| Control (Runner) | `watched(scope)`; 보는 사람 없이 O2번 Run이 지나면 `request_pause`; `JobStatus.turn_usage` |
| `prompt/api.py` | `ModelInput.turn_state: TurnState \| None`(예산 알림, 반복 알림; 기본 None). `state_block`이 그 항목을 그림. `RequestOptions.session_key: str \| None` |
| Tool (`dispatch_group`) | `refuse: Mapping[CallId, str]`(기본 빈 것): 그 호출은 검증·claim 없이 `DENIED`로 커밋 |
| Provider (OpenRouter) | `session_key` → 본문 `session_id`; usage의 `cache_write_tokens` |
| `common.TokenUsage` / Data codec | `cache_write_tokens: int \| None`(선택). 이전 행은 None |
| Interfaces | Application이 대화 읽기(상세, 이벤트)를 `watched`로 알림. Web: 사용량 안내, 새 상태 글, 상태 줄 Continue. CLI: 사용량 한 줄, 새 상태 글 |
| 바뀌지 않음 | Data 스키마(usage는 이미 JSON), 확실성 규칙, Pause·Cancel 의미, 압축 |

## 11. 소유자 결정

방향(상한 없음 + §3~§9)은 정해졌다(2026-10-16). 남은 것은 기본 수치다.

| # | 질문 | 권고 |
| --- | --- | --- |
| O1 | 턴 토큰 예산 기본값 | **1,000,000**(이번 시험 한 턴의 약 5배) |
| O2 | 보는 사람 없이 이어 갈 Run 수, 그리고 "보고 있음"의 시간 | **20 Run, 60초** |
| O3 | `NO_PROGRESS`까지의 진척 없는 Run 연속 수 | **3** |
| O4 | 명시적 캐시 구간(B2) | **B1의 측정을 본 뒤 결정** |

## 12. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 상한이 없어 길 잃은 루프가 비용을 냄 | 토큰 예산(보내기 전 판정), 진척 없음 멈춤, 자리 비움 일시정지 |
| 반복 차단이 정당한 재시도를 막음 | 확정 실패, 같은 인자, 같은 결과만 센다. 두 번까지는 된다. 결과 불명은 세지 않는다. 성공은 막지 않고 알리기만 한다 |
| 진척 판정이 탐색 중인 모델을 너무 일찍 멈춤 | 새 결과가 하나라도 있는 Run은 진척으로 본다. 연속 O3번일 때만 멈추고, Continue로 이어 간다 |
| 자리 비움 판정이 틀림(창을 열어 둔 채 떠남) | 그 경우는 "보고 있음"으로 남는다. 토큰 예산과 진척 없음이 여전히 막는다 |
| 예산 알림이 모델을 일찍 멈추게 함 | 여유가 두 Run 남짓일 때만 알린다 |
| `session_id`로 대화를 추적할 수 있음 | 대화 id가 아닌 해시를 보낸다. 같은 대화의 요청이 같은 공급자로 간다는 것 이상은 드러나지 않는다(요청 내용은 원래 그 공급자에게 간다) |
| 캐시 쓰기 비용이 적중 이득보다 큼 | B2는 측정 뒤 결정(O4) |

## 13. 검증 계획과 측정 기록

- **단위:**
  - 예산: 보내기 전 판정(지금까지 쓴 양 + 다음 추정 > 예산이면 보내지 않음), 압축 Run도 셈, usage가 없을 때 추정치로 셈, Continue가 새 예산을 줌, 예산 0과 상한 0을 함께 두면 시작 거부.
  - 진척: 세 번째 같은 확정 실패는 서버에 가지 않고 `DENIED`와 안내를 받음. 결과 글이 다르거나, 인자가 다르거나, `UNKNOWN`이거나, 다른 drive면 막지 않음. 같은 결과의 반복은 실행되고 `progress.repeated`에 보임. 진척 없는 Run이 O3번 연달아면 `NO_PROGRESS`이고, 새 결과가 하나라도 있으면 다시 셈.
  - 자리 비움: 읽기가 없으면 O2번 Run 뒤 `PAUSED`(이유 포함), 읽기가 이어지면 계속, 돌아오면 다시 셈.
  - Run 수 상한: 켰을 때 지금과 같음, 끄면 없음.
  - 예산 알림: 여유가 크면 없음, 두 Run 남짓에서 첫 문구, 마지막 추정에서 둘째 문구. `<runtime_state>` 안에 있고 앞부분 바이트는 같음.
  - B1: 같은 대화의 요청은 같은 `session_id`, 다른 대화는 다른 값, 대화 id 자체는 본문에 없음. `cache_write_tokens`를 읽고 저장함, 이전 행은 None.
- **Web·CLI:** 사용량 안내, 새 상태와 상태 줄 Continue(브라우저 smoke), CLI 사용량 한 줄과 안내.
- **실제 모델:** 같은 Calliope 시험을 B1 전후로 3번씩. Run, 반복 차단·알림 발생, 예산 알림 뒤 행동, 입력·캐시 적중·쓰기 토큰을 아래에 적는다.

(측정은 구현 뒤 채운다.)
