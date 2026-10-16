# 한 턴의 Run 예산과 비용 — v0.1 (V3-P62, 설계 초안)

> 2026-10-16 · **Proposed.** V3-P23(drive당 Run 예산 16)을 고친다. 관련: [V3-005](decisions.md)(Tool 실패는 모델에게), [V3-P55](decisions.md)(Pause·Cancel·Continue), [prompt-composition](prompt-composition.md) §5(prefix 캐시), [open-questions](open-questions.md) Q-25(캐시 성능), [mcp-context-v0.1](mcp-context-v0.1.md) §14.5(실제 모델 시험).

## 1. 왜

한 사용자 메시지(또는 Continue 한 번)의 drive는 지금 Run 16번에서 멈춘다(`max_runs_per_drive`, V3-P23). 무한 루프를 막는 예산이고, 코드 기본값이라 운영자가 바꿀 수 없다. 멈추면 `RUN_LIMIT_REACHED`이고, Continue(Web 대화 메뉴, CLI `/continue`)가 16번을 더 준다.

실제 모델 시험(2026-10-16, `openai/gpt-6-luna`, Calliope 해설 한 수)에서 이 숫자가 문제가 됐다.

| 시험 | Run | 입력 토큰 | 캐시 적중 | 결과 |
| --- | --- | --- | --- | --- |
| 처음(Calliope AG5 전, HIMARI 결함 둘 있음) | 16, 상한 | 425,673 | Run마다 2,125 | 해설 없음 |
| AG5 뒤 3회 | 7 · 9 · 5 | 132k · 192k · 81k | Run마다 2,691 | 모두 저장(얕음) |

여기서 세 가지를 읽는다.
1. **16번은 길을 잃은 모델에게는 비싸고, 제대로 일하는 모델에게는 빠듯할 수 있다.** 처음 시험은 같은 종류의 실패를 되풀이하다 끝났다(추측한 URI 다섯 번, 같은 오류의 제출 두 번). 이번 시험은 5~9번이 들었는데, 해설이 "왜"까지 가면 더 든다.
2. **모델은 예산을 모른다.** 남은 단계를 모르므로 마지막 단계에도 탐색을 계속한다.
3. **비용의 대부분은 반복해서 보내는 앞부분이다.** 큰 Tool 결과 하나(Calliope `ply`, 약 18k 토큰)가 이후 모든 Run에 다시 실린다. 그런데 공급자 캐시는 Run마다 앞의 2,691 토큰(시스템 메시지와 첫 사용자 메시지)만 적중했다.
   - HIMARI의 요청은 덧붙기만 한다. 연속한 두 요청은 맨 끝의 `<runtime_state>` 앞까지 바이트가 같다(시험 DB에서 확인).
   - 그러니 캐시를 놓치는 쪽은 공급자 경로다. OpenRouter는 `session_id`가 없으면 "캐시 적중을 본 뒤에야" 같은 공급자로 보낸다(sticky routing). GPT-5.6 이후 모델은 명시적 캐시 구간(`prompt_cache_breakpoint`, Anthropic식 `cache_control`을 변환)을 받는다.

**숫자만 올리지 않는다.** 되풀이를 막지 못하면 올린 만큼 비용이 는다. 그래서 상한을 올리기 전에 되풀이 감지(§3)와 예산 알림(§4)을 함께 둔다.

## 2. 상한: 운영자가 정한다

- `HIMARI_MAX_RUNS`(Web 서버 환경 변수, 1~200)가 drive 하나의 Run 상한이다. `Application.open(max_runs=…)`, `build_runtime(max_runs_per_drive=…)`로 이어진다.
- **기본값은 O1이다**(§9; 권고 24). 근거는 셋이다.
  - 이번 시험이 5~9번을 썼고, 비교·확인이 붙으면 그 두 배쯤 된다.
  - §3·§4가 길 잃은 반복을 줄인다.
  - 다른 에이전트 런타임도 비슷한 범위의 고정 상한을 기본으로 둔다(턴 10~25회). 상한은 끝이 아니라 Continue 지점이다.
- 상한에 닿으면 지금처럼 `RUN_LIMIT_REACHED`이고, 마지막 그룹의 결과는 모두 커밋된다. **Continue가 같은 상한을 다시 준다**(지금과 같음).
- **보이는 곳:**
  - Web: 상태 줄에 "N단계를 썼습니다 — Continue로 이어 가기"와 **Continue 버튼**(지금은 대화 메뉴 안에만 있음).
  - CLI: `(stopped after N steps — /continue to go on)`.
  - `GET /api/server`가 상한을 알린다.

## 3. 같은 실패의 반복을 막는다

**규칙.** 한 drive 안에서 **같은 Tool·같은 인자의 호출이 같은 결과로 두 번 확정 실패했으면, 세 번째는 실행하지 않는다.**
- **같은 호출:** Tool id가 같고, 인자의 정규 JSON(키 정렬)이 같다.
- **확정 실패:** 기록이 `FAILED` 또는 `DENIED`이고, 모델이 받은 결과의 글이 두 번 같다. 결과 불명(`UNKNOWN`)·취소·성공은 세지 않는다. 결과를 모르는 호출을 다시 확인하는 일은 막지 않는다(V3-047).
- **세 번째:** 실행하지 않고 `DENIED`로 기록한다. 모델에게는 "Not run: this exact call (same tool and arguments) already failed twice in this turn with the same result. Change the arguments, use another tool, or answer with what you have."를 준다. 서버에는 아무것도 가지 않는다.
- **범위는 drive 하나다.** Continue나 새 메시지는 새로 센다. 사용자가 상황을 바꿨을 수 있기 때문이다.

**왜 이 정도만 하나.**
- 같은 인자로 같은 실패를 세 번째 받는 일은 거의 늘 낭비다. 결정적인 거부(스키마, 권한, 없는 리소스)는 같은 답을 준다.
- 일시적 실패(서버 down 등)를 다시 시도하는 것은 두 번까지 된다.
- 성공한 호출의 반복은 막지 않는다. 상태가 변하는 조회(목록, 진행 상황)는 같은 인자로 다른 답을 줄 수 있다. 성공의 반복은 §4가 다룬다.
- V3-005(Tool 실패는 모델에게 알리고, Runtime이 의미론적 복구를 하지 않음)와 맞는다. 이 규칙은 복구가 아니라 실행하지 않는 규칙이고, 이유를 모델에게 알린다.

**어디서.** Control(Driver)이 정하고 Tool 모듈이 기록한다.
- Driver는 이 drive에서 커밋된 실행 기록으로 셈을 한다(Data에 이미 있다). 새 영속 상태는 없다.
- 새 그룹을 보내기 전에, 막을 호출 id와 그 이유를 Tool 모듈의 `dispatch_group(…, refuse=)`에 넘긴다. Dispatcher는 그 호출을 검증·claim 없이 `DENIED`로 커밋한다.
- 같은 그룹의 다른 호출은 그대로 실행된다.

## 4. 모델에게 남은 예산을 알린다

- 남은 Run이 **3 이하**일 때부터 `<runtime_state>`에 `run_budget` 항목을 넣는다: `{"used": u, "limit": N, "left": n, "note": …}`.
- `note`는 셋 중 하나다.
  - 남은 수가 2~3일 때: "Few steps are left in this turn: finish with what you have, or ask the user (ask_user) whether to go on."
  - 마지막 Run(남은 1)일 때: "This is the last step of this turn. Tool calls made now will run, but you will see their results only if the user continues: answer now, or ask the user."
- **왜 `<runtime_state>`인가.** 매 요청의 맨 끝, 데이터로 보이는 자리다(CPP). 앞부분을 바꾸지 않으므로 prefix 캐시를 깨지 않는다. 문맥 키에도 들어가지 않고, 토큰 추정은 상태 블록을 그대로 재므로 저절로 들어간다(V3-P60 §14.2.1).
- 그보다 이른 단계에는 넣지 않는다. 매번 예산을 보이면 모델이 일찍 포기할 수 있다.

## 5. 비용: 캐시가 대화 기록을 쓰게 한다

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
- 같은 Calliope 시험을 B1 전후로 3번씩 돌려 Run별 입력·캐시 적중·쓰기 토큰을 비교한다. 결과는 이 문서 §10에 적는다.

**이번에 하지 않는 것.**
- **큰 Tool 결과 줄이기**(예: `ply`의 claim graph 18k 토큰). 서버의 답 형식은 서버 쪽 일이다(Calliope). HIMARI는 이미 결과를 64 KiB에서 자른다.
- **턴 단위 토큰 예산.** O3에서 정한다.

## 6. 계약 변경 (Proposed)

| 계약 | 변경 |
| --- | --- |
| `bootstrap` / Web 설정 | `HIMARI_MAX_RUNS`(1~200, 기본 O1) → `Application.open(max_runs=)` → `build_runtime(max_runs_per_drive=)`. `GET /api/server`에 `max_runs` |
| Control (Driver) | 상한 값; §3의 셈과 `refuse`; §4의 `run_budget`을 `ModelInput`에 넣음; `RequestOptions.session_key` 설정; `RUN_LIMIT_REACHED`의 `detail`에 쓴 단계 수 |
| `prompt/api.py` | `ModelInput.run_budget: RunBudget \| None`(기본 None); `state_block`이 `run_budget` 항목을 그림; `RequestOptions.session_key: str \| None` |
| Tool (`dispatch_group`) | `refuse: Mapping[CallId, str]`(기본 빈 것): 그 호출은 검증·claim 없이 `DENIED`로 커밋 |
| Provider (OpenRouter) | `session_key` → 본문 `session_id`; usage의 `cache_write_tokens` |
| `common.TokenUsage` / Data codec | `cache_write_tokens: int \| None`(선택). 이전 행은 None |
| Web / CLI | 상한 도달 안내와 Continue 버튼 / `/continue` 안내 |
| 바뀌지 않음 | Data 스키마(usage는 이미 JSON), 확실성 규칙, Pause·Cancel, 압축 |

## 7. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 상한을 올려 길 잃은 루프의 비용이 늘어남 | §3 반복 차단, §4 예산 알림. 상한은 운영자가 정함 |
| 반복 차단이 정당한 재시도를 막음 | 확정 실패, 같은 인자, 같은 결과만 센다. 두 번까지는 된다. 결과 불명은 세지 않는다. drive가 바뀌면 새로 센다 |
| 예산 알림이 모델을 일찍 멈추게 함 | 남은 3 이하에서만 알린다 |
| `session_id`로 대화를 추적할 수 있음 | 대화 id가 아닌 해시를 보낸다. 같은 대화의 요청이 같은 공급자로 간다는 것 이상은 드러나지 않는다(요청 내용은 원래 그 공급자에게 간다) |
| 캐시 쓰기 비용이 적중 이득보다 큼 | B2는 측정 뒤 결정(O4) |

## 8. 검증 계획

- **단위:**
  - 상한 설정(범위 밖 값 거부), `RUN_LIMIT_REACHED`의 `detail`, Continue가 같은 상한을 다시 줌.
  - **반복 차단:** 두 번 같은 확정 실패 뒤 세 번째는 서버에 가지 않고 `DENIED`와 안내를 받음. 결과 글이 다르거나, 인자가 다르거나, `UNKNOWN`이거나, 다른 drive면 막지 않음. 같은 그룹의 다른 호출은 실행됨. 모델에게 간 결과에 안내가 있음.
  - **예산 알림:** 남은 4에서는 없음. 3·2에서는 첫 문구, 1에서는 마지막 문구. `<runtime_state>` 안에 있고 앞부분 바이트는 같음.
  - **B1:** 같은 대화의 요청들은 같은 `session_id`, 다른 대화는 다른 값. 대화 id 자체는 본문에 없음. usage의 `cache_write_tokens`를 읽고 저장함. 이전 행은 None.
- **Web·CLI:** 상한 도달 상태 줄과 Continue 버튼(브라우저 smoke), CLI 안내.
- **실제 모델:** 같은 Calliope 시험(`gpt-6-luna`, 5...h6)을 B1 전후로 3번씩 돌려 Run, 반복 차단 발생, 예산 알림 뒤 행동, 입력·캐시 토큰을 §10에 적는다.

## 9. 소유자 결정

| # | 질문 | 권고 |
| --- | --- | --- |
| O1 | 기본 상한 | **24**(16 유지 / 24 / 32 중). §3·§4와 함께라면 24가 시험의 두 배를 넘고, 넘으면 Continue |
| O2 | 같은 확정 실패 두 번 뒤 세 번째 | **실행하지 않음**(대안: 실행하고 경고만 덧붙임 — 비용은 그대로) |
| O3 | 턴 단위 토큰 예산(예: 입력 합계 상한) | **지금은 두지 않음.** 상한·반복 차단·캐시로 먼저 보고, 측정 뒤 다시 본다 |
| O4 | 명시적 캐시 구간(B2) | **B1의 측정을 본 뒤 결정** |

## 10. 측정 기록

(B1 구현 뒤 채운다.)
