# 스트리밍 v0.1

> 2026-10-24 · **PROPOSED (V3-P73)** · 구현: 1단계(§14), 2단계 Web(§15). rev. 3 — 소유자 결정 Q1~Q6 권고대로(§11). rev. 2 — 설계 검수 B1(취소된 스트림의 받은 부분, §3.5·§5.3)·B2(놓친 조각 없는 구독, §5.2·§6.1)·N1(continuation 합치기를 보수적으로, §3.3) 반영, 소유자 결정 Q7(생각 중 표시, §7.4). rev. 1: 설계 초안.
>
> 배경: 지금은 모델이 답을 다 쓴 뒤에야 화면에 나온다. 긴 답이나 추론이 긴 모델은 수십 초 동안 "HIMARI is working…"만 보인다. 소유자 방향(2026-10-24): 작은 후속 수정 다음으로 스트리밍을 진행한다.
>
> 앞선 결정:
> - V3-018: 모든 Run의 원본 요청·응답을 보존한다. 스트리밍의 정확한 캡처 방식은 별도 정책이다(이 문서 §4).
> - V3-048: 요청은 wire-exact. 응답은 받은 HTTP 본문 바이트 그대로.
> - V3-049: continuation(추론 블록·서명)은 받은 그대로 다음 턴에 다시 싣는다.
> - V3-045: 한 Run은 한 번만 보낸다. 보낸 뒤의 실패는 `OUTCOME_UNKNOWN`이고 다시 보내지 않는다.
> - data-contract-v0.2 §(Provider streaming): 스트리밍은 받은 순서의 원본과 끝 표시가 있어야 하고, 이미 받은 바이트를 고치지 않는다.
> - V3-P46: 브라우저는 polling으로 상태와 Event를 읽는다.

## 1. 왜, 그리고 범위

모델이 쓰는 대로 글을 화면에 보인다. 대화 기록(Event)은 지금과 같다. 답이 끝나 정규화된 뒤에만 Event가 생기고, 흘러가는 글은 화면에만 있는 임시 표시다.

| 포함 (이 패킷) | 제외 (이후) |
| --- | --- |
| OpenRouter 스트리밍 요청과 SSE 응답 읽기(§3) | 다른 Provider |
| SSE 원본 그대로 보존, 끊긴 스트림의 받은 부분 보존(§4) | 추론 글 보이기(Q2) |
| Control → Runner의 임시 "쓰는 중" 상태(§5) | CLI 스트리밍(CLI는 지금처럼 끝난 답을 보임, §8) |
| Web의 실시간 연결과 쓰는 중인 답 표시(§6, §7) | 압축 요약 요청의 스트리밍(보일 화면이 없음) |
| 모델별 확인과 끄는 설정(§9) | 끊긴 답 이어 쓰기 |

## 2. 다른 호스트

- **ChatGPT·Claude(claude.ai):** 답을 글자 단위로 흘려 보이고, 추론은 접힌 "생각 중" 영역에 보인다. 도구를 부르면 그 자리에 도구 표시가 생긴다. 중지하면 받은 데까지 남는다.
- **OpenAI·Anthropic API:** SSE(`text/event-stream`)로 조각(delta)을 보낸다. OpenRouter도 같은 형식이다(§3).
- **Open WebUI:** 서버가 Provider의 SSE를 받아 브라우저에 다시 흘린다. 대화 기록에는 끝난 답만 저장한다.

HIMARI도 마지막 방식을 따른다. 서버가 Provider 스트림을 읽고, 브라우저에는 별도 연결로 흘린다. 기록은 끝난 답만이다. 중지하거나 끊긴 답은 기록에 들어가지 않는다(지금과 같음, §7.3).

## 3. Provider (OpenRouter)

### 3.1 요청

- 스트리밍하는 Run의 요청 본문에 `"stream": true`를 넣는다. 본문은 지금처럼 `prepare_native_request`가 만들고 Data가 보내기 전에 저장한다(V3-048). 그래서 저장된 요청만 보고도 스트리밍한 Run인지 안다.
- 스트리밍 여부는 Driver가 `RequestOptions.stream`으로 정한다(§5.1). Provider는 모델이 `streaming` 능력을 선언했을 때만 넣는다(§9). 선언하지 않은 모델은 지금처럼 보낸다.
- `NativePreparedRequest.streamed`가 결과를 알린다.

### 3.2 응답 읽기 (OpenRouter 문서, 2026-10-24 확인)

- HTTP 200이면 SSE다. 각 `data:` 줄이 JSON 조각이고, 끝은 `data: [DONE]`이다.
- `:`로 시작하는 줄(`: OPENROUTER PROCESSING`)은 연결 유지용 주석이라 건너뛴다.
- 마지막에 `usage`만 담은 조각이 한 번 더 온다(`[DONE]` 바로 앞). 사용량은 여기서 읽는다.
- 200을 보낸 뒤의 오류는 최상위 `error`와 `finish_reason: "error"`를 가진 조각으로 온다. 오류 조각 하나만 오는 수도 있다.
- 200이 아니면 지금처럼 JSON 오류 본문이다(`NativeNoResponse`).
- 줄 나누기는 MCP 클라이언트와 같은 바이트 단위 방식(LF·CRLF·CR)을 쓰고, 한도는 받은 바이트로 센다(V3-P53).

### 3.3 조각 합치기 (정규화)

`normalize`는 저장된 SSE 본문 전체를 읽어, 지금 JSON 응답에서 얻던 것과 같은 결과를 만든다. 재시작 뒤 저장본을 다시 정규화할 때도 같은 함수다.
- `delta.content`를 이어 붙여 글을 만든다.
- `delta.tool_calls`는 `index`별로 모아 `id`·`function.name`을 정하고 `function.arguments`를 이어 붙인다.
- `delta.reasoning_details`와 `delta.reasoning`은 §3.3.1의 규칙으로 합친다.
- `finish_reason`은 마지막으로 온 비어 있지 않은 값이다. 그 뒤 판정은 지금과 같다: `stop` ↔ FINAL, `tool_calls` ↔ TOOL_CALLS, 나머지는 `INVALID_MODEL_OUTPUT`.
- **닫힘 실패(fail closed):** 다음은 모두 정규화하지 않는다.
  - `[DONE]` 없이 끝난 스트림
  - `error`가 있는 조각
  - JSON이 아닌 `data:`
  - `index`가 맞지 않는 Tool 조각
  - choice가 둘 이상인 조각
- 예전 Run(JSON 응답)은 content type으로 구별해 지금 코드로 정규화한다.

#### 3.3.1 continuation 합치기 (검수 N1)

**위험.** OpenRouter 문서는 스트리밍에서 암호화된 추론이 `[REDACTED]`로 올 수 있다고 한다. 같은 `index` 조각을 어떻게 합치는지도 정하지 않는다. 합친 값이 비스트리밍 응답과 다르면 다음 Tool 턴이 거부되거나 추론이 끊긴다.

**원칙:** V3-049보다 약해지지 않는다. 지금 Adapter는 `reasoning_details`를 불투명한 값으로 받은 그대로 지킨다. 스트리밍에서도 **애매하면 추측해서 합치지 않고 잘못된 응답으로 다룬다**(`INVALID_MODEL_OUTPUT`, 원본은 보존). 지금 코드가 형식이 깨진 details를 잘못된 응답으로 다루는 것과 같다.

규칙:
- **항목 나누기:** `reasoning_details`의 각 항목은 `index`로 이어진다.
  - 조각의 항목이 바로 앞 항목과 `index`가 같으면 이어 붙이는 후보다.
  - `index`가 다르거나 없으면 새 항목이다.
  - 이미 지나간 `index`가 다시 나오면 잘못된 응답이다.
- **이어 붙일 수 있는 조건:** `type`이 같다. `id`·`format`은 없거나 지금 항목과 같다. 하나라도 어긋나면 이어 붙이지 않고 잘못된 응답이다. `type`만 같다고 서로 다른 항목을 합치지 않는다.
- **필드:** `text`·`summary`·`data`는 이어 붙인다. `signature`는 한 번만 정해진다. 다른 값이 또 오면 잘못된 응답이다. 그 밖의 필드는 처음 값을 지키고, 다른 값이 오면 잘못된 응답이다.
- **있음과 비어 있음을 구분한다:**
  - 어느 조각에든 `reasoning_details` 키가 있었으면, 합친 목록(빈 목록 `[]` 포함)이 continuation이다. 지금 코드가 빈 목록도 지키는 것과 같다(DeepSeek V4처럼 빈 목록을 요구하는 모델).
  - 키가 한 번도 없었으면 이어 붙인 `reasoning` 글이 대체값이다.
  - 둘 다 없으면 continuation이 없다.
- **`[REDACTED]`:** 암호화 항목(`reasoning.encrypted`)의 `data`가 `[REDACTED]`이면 다시 실을 수 없으므로 잘못된 응답이다.
- **모델 확인(§9, Q5):** 모델마다 실제 요청으로 확인한 뒤에만 `streaming`을 선언한다. 확인 항목:
  - 같은 질문의 스트리밍·비스트리밍 continuation이 같은 모양인지
  - 합친 continuation을 다시 실은 다음 Tool 턴이 받아들여지는지
  - `[REDACTED]`가 오지 않는지
- 확인할 수 없는 모델은 스트리밍하지 않는다. 운영 중 위 규칙 때문에 잘못된 응답이 생기면 그 모델의 선언을 끈다.

### 3.4 시간과 크기

- 연결 10초, 바이트 사이 간격 120초(지금 timeout과 같은 값). OpenRouter의 주석 줄도 간격을 줄인다.
- 전체 시간 상한은 두지 않는다. 출력 토큰 상한(`max_output_tokens`)이 길이를 묶는다.
- 받은 본문은 32 MiB까지다. 넘으면 연결을 끊고 결과 불명(`NativeUncertain("response_too_large")`)으로 둔다. 이미 처리됐을 수 있기 때문이다.

### 3.5 Provider SPI

```python
type OutputSink = Callable[[OutputDelta], None]

@dataclass(frozen=True)
class OutputDelta:
    text: str = ""            # 이어 붙일 답(또는 Tool 앞 해설) 글
    tool: str | None = None   # 새로 시작한 Tool 호출의 Tool id
    thinking: bool = False    # 추론 조각이 왔다(내용은 넘기지 않음)

class RawCapture:
    """받은 응답 바이트. Run 하나에 하나, Driver가 만들고 가진다(검수 B1)."""
    content_type: str          # 응답 머리가 오면 Provider가 정한다
    def append(self, chunk: bytes) -> None: ...
    def body(self) -> bytes | None: ...   # 받은 것이 없으면 None

async def invoke_prepared(self, request, sink: OutputSink | None = None,
                          capture: RawCapture | None = None) -> NativeInvocationOutcome
```

- `capture`의 계약(B1):
  - Provider는 받은 본문 바이트를 **해석하기 전에** `capture.append`로 넘긴다. 그래서 Provider 작업이 어떻게 끝나든(정상, 취소, 예외), 끝난 순간 `capture`에는 받은 데까지가 들어 있다.
  - 32 MiB를 넘는 바이트는 넣지 않는다(§3.4).
  - Provider는 `capture`를 끝난 뒤에 쓰지 않는다. 바이트의 주인과 수명은 §5.3이 정한다.
- `sink`는 선택이다. 스트리밍하지 않은 요청이면 부르지 않는다. 예외를 던지지 않는다고 가정하지 않는다: Provider는 sink의 예외를 삼키고 읽기를 계속한다(화면 문제로 Run이 실패하지 않게).
- 한 번 보내기(PROVIDER-01)와 결과 분류는 그대로다: 연결 전 실패는 `NativeNoResponse`, 보낸 뒤 끊김은 `NativeUncertain`.
- `ScriptedAdapter`는 스크립트의 답을 몇 글자씩 sink에 넘기는 모드를 갖는다(테스트용).

## 4. 원본 보존 (V3-018 · V3-048)

- 받은 SSE 본문을 **바이트 그대로** `raw_response_body`에 저장하고 content type은 `text/event-stream`이다. 조각을 합친 JSON을 따로 만들어 저장하지 않는다.
  - 받은 순서가 곧 바이트 순서다. 끝 표시는 `data: [DONE]`이다(data-contract-v0.2의 조건).
  - TCP·chunked 경계는 본문이 아니므로 남기지 않는다(지금과 같음).
- **끊긴 스트림:** 받은 데까지의 바이트를 결과 불명 Run에 함께 저장한다. `RawUncertain`에 선택 필드 `body`·`content_type`을 더한다. SQLite 열은 이미 있어 스키마 변경이 없다. 사용자 중지, Provider 예외, 서버 종료도 같다. 경로는 §5.3이다.
- **프로세스가 죽으면** 메모리의 받은 부분은 사라진다. 그 Run은 지금처럼 재시작 복구에서 본문 없는 `OUTCOME_UNKNOWN`이 된다. 조각마다 DB에 쓰면 막을 수 있지만, 쓰기가 조각 수만큼 늘어 하지 않는다.
- 저장 크기: SSE는 같은 답의 JSON보다 몇 배 크다(조각마다 id·model 같은 머리가 붙음). 개인 서버 규모에서는 받아들인다. 늘어나는 양은 구현 때 재서 적는다.

## 5. Control과 Runner

### 5.1 어느 Run을 흘리나

- 사용자에게 답하는 Run(`drive`·`resume`의 모델 호출)만 흘린다. `RequestOptions.stream = True`는 Driver가 정한다.
- 압축 요약 Run은 흘리지 않는다. 보일 자리가 없다.
- `HIMARI_STREAMING=0`이면 아무 Run도 흘리지 않는다(§9).

### 5.2 쓰는 중 상태 (검수 B2)

- Driver는 `invoke_prepared`에 sink를 넘기고, `TurnProgress`의 새 메서드로 알린다.
  - `output_started(run_id)`: 흘리는 Run을 보내기 직전
  - `output(run_id, delta)`: 조각이 올 때
  - `output_ended(run_id)`: 그 Run의 결과를 기록한 뒤(어떻게 끝났든)
- `TurnProgress`의 규칙은 그대로다: 예외를 던지지 않고, Driver는 그 값에 기대지 않는다.
- Runner는 Conversation마다 Run 하나의 **누적 상태**를 메모리에 둔다: `LiveOutput(run_id, text, tools, thinking, truncated, ended, version)`.
  - **이 누적 상태가 권위 있는 값이다.** 구독자에게 보낼 것은 늘 이 상태에서 계산한다(§6.1). 조각을 따로 쌓는 큐는 두지 않는다.
  - `output_started`가 새 Run의 빈 상태로 바꾼다. `output`은 그 Run의 상태에 이어 붙이고, 다른 Run의 조각은 버린다. `output_ended`는 `ended = True`로 표시만 한다.
  - **끝난 상태는 지우지 않는다.** 다음 Run의 `output_started`, 다음 job의 시작, 또는 `forget`까지 남는다. 그래야 늦게 깨어난 구독자도 끝남을 본다.
  - `version`은 상태가 바뀔 때마다 1씩 오른다.
  - 한 Run의 글은 512 KiB까지 모은다. 넘으면 더 붙이지 않고 `truncated = True`. 화면용이고, 원본은 Data에 다 있다.
- 기다리는 쪽(§6)은 `asyncio.Condition`으로 깨운다. 깨우기는 합쳐질 수 있다. 그래도 빠지는 것이 없다: 구독자는 깨어날 때마다 지금 상태를 다시 읽는다.
- 재시작하면 사라진다. 쓰던 Run은 지금처럼 재시작 복구에서 `OUTCOME_UNKNOWN`이 된다.

### 5.3 끝나지 않은 Run의 받은 부분 (검수 B1)

**지금 코드:** `_interruptible`은 취소되면 Provider 작업을 `cancel()`하고 끝나기를 기다린 뒤 `_Cancelled`를 던진다. Driver는 `RawUncertain(CANCELLED_BY_USER)`만 기록한다. 서버 종료의 `CancelledError`는 기록 없이 올라가, 재시작 복구가 `OUTCOME_UNKNOWN`으로 만든다. 어느 쪽에도 받은 바이트를 넘길 길이 없다.

**정하는 것:**
- **주인과 수명:** Driver가 흘리는 Run마다 `RawCapture`를 만든다. `mark_dispatch` 뒤, 보내기 전이다. Provider는 받은 바이트를 넣기만 한다(§3.5). Driver가 그 Run의 결과를 기록하면 `capture`를 버린다. 메모리는 Run 하나에 32 MiB까지다.
- **기록:** 어떻게 끝나든 **Provider 작업이 끝난 뒤에** `capture`를 읽는다. 그리고 결과와 받은 부분을 `record_raw_outcome` 한 번에 기록한다. 그 호출이 트랜잭션 하나다.

| 끝난 방식 | 기록 |
| --- | --- |
| 정상(`NativeReceived`) | 지금과 같다. 본문은 `capture`의 바이트와 같다 |
| Provider가 돌려준 `NativeUncertain`(끊김, 32 MiB 초과) | `RawUncertain(error_code, body=capture.body())` |
| 사용자 중지(`_Cancelled`) | `_interruptible`이 Provider 작업이 끝나기를 이미 기다렸다. 그래서 `capture`가 완전하다. `RawUncertain(CANCELLED_BY_USER, body=…)` |
| Provider 예외 | `RawUncertain(예외 이름, body=…)` |
| 서버 종료(밖에서 온 `CancelledError`) | Run 단계를 감싼 `except CancelledError`에서, Provider 작업이 끝나기를 기다린 뒤 `RawUncertain("shutdown", body=…)`를 기록하고 다시 던진다. 이 기록이 실패하거나 시간 안에 끝나지 않으면 지금처럼 재시작 복구가 본문 없이 `OUTCOME_UNKNOWN`으로 만든다 |
| 프로세스 죽음 | 받은 부분은 사라진다(§4). 재시작 복구가 지금처럼 처리한다 |

- 모든 경로에서 `output_ended(run_id)`를 부른다(§5.2). 기록 뒤, `finally`에서다.
- 비스트리밍 Run도 같은 `capture`를 쓴다. 결과는 지금과 같다(본문 전체가 한 번에 들어온다).

## 6. Web API

### 6.1 실시간 연결 (Q1)

`GET /api/conversations/{id}/live`는 `text/event-stream`을 돌려준다.
- 인증과 권한은 다른 경로와 같다(세션 쿠키 또는 Bearer, 남의 대화는 404). `EventSource`는 같은 출처의 쿠키를 보낸다.
- 보내는 것:
  - `snapshot {run_id, text, tools, thinking, truncated, ended}`: 상태 전체
  - `idle`: 상태가 없음
  - `delta {run_id, text, tools, thinking?, truncated?}`: 이어 붙일 부분
  - `end {run_id}`: 그 Run이 끝남
- **구독자의 커서(검수 B2):** 연결마다 "보낸 것"을 기억한다: `run_id`, 보낸 글자 수, 보낸 Tool 수, 보낸 `thinking`·`truncated`·`ended`, 본 `version`.
- **깨어날 때마다** 그 순간의 누적 상태(§5.2)를 읽고 커서와 비교한다. 알림이 몇 번 합쳐졌든 상관없다.
  - **같은 Run이면:** `text[보낸 글자 수:]`와 `tools[보낸 Tool 수:]`를 `delta`로 보낸다. 바뀐 표시도 함께 보낸다. `ended`가 됐고 아직 보내지 않았으면 `end`를 보낸다.
  - **Run이 바뀌었거나 상태가 없으면:** 이어 붙일 것을 추측하지 않는다. `snapshot`(또는 `idle`)으로 다시 맞추고 커서를 새로 잡는다. 브라우저는 다른 Run의 `snapshot`을 받으면, 앞 Run의 쓰는 중 항목을 끝난 것으로 다룬다(§7.2·§7.3). 그 Run의 기록은 polling이 가져온다.
- 연결하면 먼저 `snapshot` 또는 `idle`을 보낸다.
- 일이 끝나고(job running=false) 마지막 상태의 `end`까지 보냈으면(또는 상태가 없으면) 서버가 연결을 닫는다. 페이지는 일이 돌 때만 연결한다(§7.1). 그래서 열려 있는 연결 수는 일하는 대화를 보는 페이지 수로 묶인다.
- 15초마다 주석 줄로 연결을 유지한다.
- **한 연결은 최대 5분이다.** 운영 Caddy가 응답 쓰기를 600초에 끊기 때문이다(`servers.timeouts.write 600s`). 5분이 되면 서버가 닫고, 일이 아직 돌면 페이지가 다시 연결해 `snapshot`부터 받는다. `Last-Event-ID` 이어 받기는 두지 않는다: 매번 상태 전체로 시작하는 쪽이 단순하고 틀리지 않는다.
- 주체 하나의 동시 연결은 8개까지, 넘으면 429.
- 연결을 기다리는 일은 Runner의 상태만 읽는다. DB를 읽지 않고 대화 잠금을 잡지 않는다.

### 6.2 그대로인 것

- Event와 job 상태는 지금처럼 1초 polling으로 읽는다. 실시간 연결은 화면에 흘릴 글만 맡는다.
- 그래서 실시간 연결이 막히거나(프록시, 네트워크) 끊겨도 기능은 지금과 같다. 글이 끝에 한 번에 나올 뿐이다.
- `/api/server`에 `streaming: true|false`를 더한다.

## 7. Web 화면

### 7.1 언제 연결하나

polling이 "일이 돈다"를 보면 `EventSource`를 연다. 서버가 닫으면, 일이 아직 돌 때만 다시 연다. 대화를 떠나면 닫는다.

### 7.2 쓰는 중인 답

- 타임라인 끝에 HIMARI 항목 하나를 "쓰는 중" 상태로 둔다.
  - 글은 지금 답과 같은 렌더러(Markdown·KaTeX, 같은 소독 규칙)로 그린다.
  - 다시 그리기는 한 프레임에 한 번이고, 많아야 100 ms마다다. 완성되지 않은 `$…`는 지금 규칙대로 글로 남는다.
- 답 글이 오기 전(보낸 직후, 추론 조각만 오는 동안)에는 생각 중 표시(§7.4)를 보인다. 추론 내용은 보이지 않는다(Q2).
- Tool 호출이 시작되면 이름을 보인다(`Preparing inside/write_file…`). 인자는 보이지 않는다.
- `end`가 오면 polling이 그 Run의 Event를 가져올 때까지 쓰는 중 항목을 둔다. Event가 오면 그것으로 바꾼다. 해설(commentary)과 Tool 행, 최종 답은 지금 화면 그대로다.
- 스크롤: 사용자가 맨 아래에 있을 때만 따라 내려간다. 위로 올려 읽는 중이면 움직이지 않는다.

### 7.3 끝나지 않은 답 (Q3)

중지, 연결 끊김, 결과 불명, 잘못된 응답이면 그 Run에서는 Event가 생기지 않는다.
- 쓰는 중이던 글은 **흐리게 그 자리에 남기고** "Stopped — this part was not kept, and HIMARI will not see it."을 붙인다.
- 새로 고치거나 대화를 다시 열면 사라진다. 기록에 없기 때문이다.
- 기록과 모델 입력은 지금과 같다: 끝나지 않은 답은 다음 요청에 들어가지 않는다.

### 7.4 생각 중 표시 (Q7, 소유자 결정)

Claude Code처럼 움직이는 한 줄이다. HIMARI의 이름(陽葵: 해, 해바라기)과 비슷한 소리의 "히다마리(陽だまり, 볕이 고인 자리)"에서 단어를 가져온다.

- **언제:** 흘리는 Run을 보낸 뒤 첫 답 글이 올 때까지 보인다. 글이 오기 시작하면 사라진다. Tool 호출이 시작되면 그 자리는 `Preparing inside/write_file…`(움직임 없음)이 대신한다.
- **모습:** `✾ Blooming… · 12s`
  - 앞의 기호가 움직이고, 단어 위로 빛이 천천히 지나간다.
  - 끝에 경과 시간이 1초마다 오른다. 60초부터는 `1m 05s`. 오래 걸려도 멈춘 게 아니라는 표시다.
- **단어:** 생각을 시작할 때 하나를 골라, 그 동안은 바꾸지 않는다(소유자: (a)). 단어가 계속 바뀌면 진행 상황이 바뀐 것처럼 읽히기 때문이다. 다음 Run에서는 새로 고른다. 단어는 영어다(화면의 다른 글과 같음).
- **테마마다 다름(소유자 결정):** 밝은 테마는 A, 어두운 테마는 B다.

| 묶음 | 테마 | 단어 | 움직임 |
| --- | --- | --- | --- |
| A. 해바라기(`sun`) | 밝은 테마(지금의 `classic`·`warm`) | Turning to the sun, Blooming, Basking, Unfurling, Gathering light, Following the sun, Soaking up sunlight, Opening petals | 꽃잎 기호 `· ✢ ✣ ❋ ✾ ❀`가 피었다 오므리기를 되풀이하고(한 단계 약 120 ms), 테마의 따뜻한 색 빛이 단어를 지나간다 |
| B. 볕 드는 자리(`hidamari`) | 어두운 테마(나중에 더함) | Sitting in the sun, Mulling it over, Brewing tea, Warming up, Curled up thinking, Resting in the warm spot | 따뜻한 점 하나가 천천히 숨 쉬고(약 2.4초), 은은한 빛이 느리게 지나간다 |

  - 단어 목록은 구현 때 다듬을 수 있다. 묶음과 규칙은 위와 같다.
- **고르는 방법:** 테마 정의(CSS)가 값 하나를 적는다: `--thinking-style: sun` 또는 `hidamari`. 표시는 그 값으로 묶음을 고른다.
  - 어두운 테마를 더할 때는 그 정의에 `--thinking-style: hidamari`만 적으면 된다. 표시 코드는 바꾸지 않는다.
  - 값이 없거나 모르는 값이면 `sun`이다.
  - 테마를 바꾸면(Appearance 대화상자, 다른 탭의 변경, 나중의 "시스템 따라가기") 그 자리에서 바로 다른 묶음으로 바뀐다. 같은 순번의 단어를 써서, 바뀐 뒤에도 단어가 이리저리 흔들리지 않는다.
  - 색은 테마 토큰에서 가져온다.
- **접근성:**
  - 화면 읽기 프로그램에는 움직이는 기호 없이 `HIMARI is thinking`을 한 번 알린다(`role="status"`). 경과 시간은 읽지 않는다.
  - `prefers-reduced-motion`이면 기호·빛·숨쉬기를 멈추고 기호 하나를 그대로 둔다. 단어와 경과 시간은 남는다.

## 8. CLI

CLI는 Web 서버의 클라이언트다(V3-P47). 이 패킷에서는 바꾸지 않는다. 끝난 답을 지금처럼 보인다. 필요해지면 같은 `/live`를 읽어 흘린다.

## 9. 모델 확인과 설정

- `ProviderCapabilities.streaming`: 실제 요청으로 확인한 모델만 `True`다(§3.3의 continuation 확인 포함, `image_input`과 같은 방식). 확인하지 않은 모델은 지금처럼 끝난 답을 보낸다.
- 운영자 설정 `HIMARI_STREAMING`(기본 켬). `0`이면 Provider에 스트리밍을 요청하지 않는다. 시작 로그에 한 줄, `/api/server`·`himari status`에 `streaming: on|off`.
- 사용자 중지: 스트리밍이면 연결을 끊는 것으로 끝난다. OpenRouter는 지원하는 공급자(OpenAI·Anthropic·DeepSeek 등)에서 이것으로 처리와 과금을 멈춘다. 지금(비스트리밍)은 중지해도 모델이 끝까지 쓰고 과금된다. 기록은 지금과 같다(`CANCELLED_BY_USER`, 다시 보내지 않음). 받은 부분은 §4대로 보존한다.

## 10. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 흘러온 글에 마크업·스크립트 | 끝난 답과 같은 렌더러·소독(V3-P46). 쓰는 중이라고 다른 길로 그리지 않는다 |
| 남의 대화의 글을 엿보기 | `/live`는 다른 경로와 같은 권한 확인, 남의 대화는 404 |
| 연결을 많이 열어 서버 자원 쓰기 | 주체당 8개, 일하는 동안만 열림, 5분 상한, 주석 줄 외에 할 일 없음 |
| 끊긴 답이 기록에 섞임 | Event는 끝난 답의 정규화에서만 생긴다(지금과 같음). 화면의 흐린 글은 기록이 아님을 적는다 |
| 합친 continuation이 원래와 달라 다음 턴 실패 | 애매하면 합치지 않고 잘못된 응답(§3.3.1), 모델별 실측 뒤에만 `streaming`(Q5). 실패하면 그 모델의 선언을 끈다 |
| 화면에 글이 빠짐(깨우기가 합쳐짐) | 구독자는 누적 상태와 자기 커서의 차이만 보낸다. Run이 바뀌면 `snapshot`(§6.1) |
| 중지·종료 때 받은 원본을 잃음 | Driver가 가진 `RawCapture`를 Provider 작업이 끝난 뒤 결과와 함께 한 번에 기록(§5.3). 프로세스가 죽을 때만 잃는다 |
| 끝 표시 없는 스트림을 답으로 씀 | `[DONE]` 없으면 정규화하지 않음(닫힘 실패) |
| 끝없는 스트림 | 바이트 사이 120초, 32 MiB, 출력 토큰 상한 |
| sink 예외로 Run 실패 | Provider가 삼킨다. Runner의 `TurnProgress`도 예외를 던지지 않는다 |

## 11. 소유자 결정

2026-10-24 소유자 결정: Q1~Q6 권고대로, Q7은 소유자가 정함.

| # | 질문 | 권고 → 결정 |
| --- | --- | --- |
| Q1 | 브라우저에 어떻게 흘리나? | **SSE 연결 하나를 따로 둔다**(§6.1). polling을 빠르게 하는 방법보다 지연이 적고 바이트가 적다. Event와 상태는 지금 polling 그대로라, 연결이 막혀도 기능은 남는다 → 권고대로 |
| Q2 | 무엇을 보이나? | **답 글과 Tool 앞 해설, 시작한 Tool의 이름.** 추론은 생각 중 표시(§7.4)만 보인다. 추론 글은 모델마다 형식이 다르고(요약·암호화), 보이려면 따로 정할 것이 많다 → 권고대로 |
| Q3 | 중지·끊김으로 끝나지 않은 답은? | **흐리게 남기고 "기록되지 않았다"고 적는다. 기록과 모델 입력에는 넣지 않는다**(§7.3). 받은 부분을 기록에 넣으면 끝나지 않은 답을 모델이 사실로 읽는다. 원본은 보존하므로 나중에 "이어 쓰기"를 더할 수 있다 → 권고대로 |
| Q4 | 원본은 어떻게 남기나? | **SSE 바이트 그대로**(§4). 합친 JSON을 만들어 저장하면 wire-exact가 아니게 된다. 정규화는 저장본으로 언제든 다시 할 수 있다 → 권고대로 |
| Q5 | 어느 모델에서 켜나? | **실제 요청으로 확인한 모델만**(§9). continuation 합치기가 문서에 정해져 있지 않아, 확인하지 않은 모델에서 다음 Tool 턴이 깨질 수 있다. 처음에는 기본 모델 `openai/gpt-6-luna`와 `anthropic/claude-haiku-5.5`를 확인한다 → 권고대로 |
| Q6 | 끄는 설정? | **`HIMARI_STREAMING=0`**(§9). 문제가 생기면 배포 없이 지금 동작으로 돌아간다 → 권고대로 |
| Q7 | 생각 중 표시? | **소유자 결정(2026-10-24):** Claude Code처럼 움직이는 한 줄(§7.4). 영어 단어, 밝은 테마는 A(해바라기)·어두운 테마는 B(볕 드는 자리), 테마 정의의 `--thinking-style` 값으로 고름. 경과 시간을 1초마다 보이고, 단어는 생각을 시작할 때 하나만 고름 |

## 12. 검증 계획

**Provider 단위**
- 기록해 둔 SSE 본문으로 정규화한다.
  - 글, 여러 Tool 호출(조각난 인자), 추론 조각, 사용량 조각
  - 주석 줄, CRLF·CR 줄 끝, 한 바이트씩 나눈 전송
- 닫힘 실패: `[DONE]` 없음, 오류 조각, 오류 조각만, 깨진 JSON, `index` 어긋남, 32 MiB 넘음.
- 같은 내용의 JSON 응답과 SSE 응답이 같은 정규화 결과와 continuation을 내는지 확인한다(fuzz 포함).
- sink에 간 조각을 이으면 정규화한 글과 같은지, sink 예외가 Run을 바꾸지 않는지 확인한다.
- continuation 합치기(§3.3.1):
  - 같은 `index`의 조각을 합치고 서명을 한 번 지킨다.
  - 다음은 모두 잘못된 응답이다: `index`가 같은데 `type`·`id`가 다름, 지나간 `index`가 다시 옴, 서명이 둘, `[REDACTED]`.
  - `reasoning_details: []`는 빈 목록으로 남고, 키가 없으면 `reasoning` 글로 대체한다. 비스트리밍과 같다.

**Control·Runner**
- 조각이 Runner 상태에 쌓이고, 결과를 기록한 뒤 지워진다.
- 압축 요약 Run은 흘리지 않는다.
- 받은 부분(§5.3): 사용자 중지, Provider 예외, 끊김, 서버 종료 각각에서 받은 데까지의 바이트가 `RawUncertain`에 남는다(조각을 몇 개 보낸 뒤 멈추는 가짜 Provider). 모든 경로에서 `output_ended`가 불린다.
- 재시작 뒤 저장된 SSE 응답을 다시 정규화한다.

**Web**
- `/live`: snapshot·delta·end 순서, 남의 대화 404, 일이 끝나면 닫힘, 5분 상한(시계 주입), 연결 수 한도.
- 놓친 조각 없음(B2):
  - 구독자가 깨어나기 전에 조각 여러 개와 `output_ended`가 한꺼번에 온다. 그래도 받은 글을 이으면 전체 글과 같고 `end`가 온다.
  - 깨어나기 전에 Run이 바뀌면 `snapshot`으로 다시 맞춘다.
  - 512 KiB에서 `truncated`.
- 생각 중 표시(§7.4, 브라우저):
  - 첫 글 전에 보이고 글이 오면 사라진다.
  - 경과 시간이 오른다.
  - 단어가 그 동안 바뀌지 않는다.
  - `--thinking-style`을 바꾸면 그 자리에서 묶음이 바뀐다.
  - reduced motion에서는 움직임이 없다.
- 브라우저 스모크(느린 스크립트 Provider):
  - 답이 끝나기 전에 글 일부가 보이고, Event로 바뀐 뒤 중복되지 않는다.
  - Tool 이름이 보인다.
  - 중지하면 흐린 글과 안내가 남는다.
  - 연결을 막아도(route abort) 끝난 답이 나온다.
  - 위로 스크롤한 동안은 따라 내려가지 않는다.

**실제 모델(로컬 임시 서버)**
- luna와 haiku에서 다음을 확인한다.
  - 긴 답이 흐른다.
  - Tool 턴이 이어진다(continuation 재생).
  - 중지하면 연결이 끊긴다.
- 저장 크기를 같은 답의 JSON과 비교해 적는다.

## 13. 구현 단계

1. **1단계: Provider·Data·Control.**
   - OpenRouter 스트리밍 요청·SSE 읽기·정규화, `RawUncertain`의 받은 부분, `RequestOptions.stream`, sink, `TurnProgress.output`, Runner의 `LiveOutput`.
   - 모델 확인과 `streaming` 선언, `HIMARI_STREAMING`.
   - 화면 없이도 원본·기록이 맞는지 테스트한다.
2. **2단계: Web.** `/live`, 쓰는 중 항목, 끝나지 않은 답 표시, 스모크.

1단계만 배포하면 화면은 지금과 같고, 중지가 과금을 멈추는 효과만 생긴다. 두 단계를 함께 배포하기를 권한다.

## 14. 구현 노트 (2026-10-24, 1단계)

**Provider**
- `provider/api.py`:
  - `ProviderCapabilities.streaming`, `NativePreparedRequest.streamed`
  - `OutputDelta`·`OutputSink`
  - `RawCapture`: `append`는 32 MiB(`MAX_CAPTURE_BYTES`)를 넘으면 `False`를 돌려준다.
  - `invoke_prepared(request, sink=None, capture=None)`
- `prompt/api.py`의 `RequestOptions.stream`은 Driver가 보내기 직전에만 켠다. 그래서 Prompt와 압축 요약 요청에는 들어가지 않는다.
- `provider/openrouter_stream.py`(새 모듈):
  - `SseReader`: 바이트 단위로 줄을 나눈다. CR이 조각 끝에 오면 다음 조각을 기다린다. 닫히지 않은 마지막 이벤트는 버린다.
  - `StreamView`: 받는 동안 sink에 보이는 것을 맡는다. 예외를 던지지 않는다.
  - `assemble`: 저장된 스트림을 JSON 응답 모양으로 합친다. 그 뒤 판정은 기존 `_normalize` 그대로다. §3.3.1의 규칙이 여기 있다.
- `OpenRouterAdapter._invoke_stream`:
  - 받은 바이트를 해석하기 전에 `capture`에 넣는다.
  - 연결 제한은 10초, 바이트 사이 간격은 기존 timeout(120초)이다.
  - 결과 분류:

| 상황 | 결과 |
| --- | --- |
| 200이 아님 | `NativeNoResponse(http_N, 본문)` |
| 200인데 JSON 본문 | 지금과 같은 판정(`_whole`) |
| 오류 조각 | `NativeNoResponse(provider_error_코드, 본문)` |
| `[DONE]` 없이 끝남 | `NativeUncertain("stream_incomplete")` |
| 연결 끊김 | `NativeUncertain("transport:…")` |
| 32 MiB 초과 | `NativeUncertain("response_too_large")` |

- 저장된 스트림을 다시 정규화할 때도 `[DONE]`이 없으면 닫힘 실패다(§3.3).
- 설계와 다른 점:
  - **스트리밍하는 요청에만 `sink`·`capture`를 넘긴다.** 비스트리밍 응답은 본문이 한 번에 오므로 남길 "받은 부분"이 없다. 이렇게 하면 기존 Provider 대역(테스트 7곳)도 시그니처만 넓히면 된다.
  - 서버 종료 때의 `RawUncertain("shutdown")` 기록도 스트리밍 Run에만 한다. 비스트리밍 Run은 지금처럼 재시작 복구가 처리한다.
- `ScriptedAdapter(streaming=True, piece_delay=…)`는 답을 4자씩 sink에 넘긴다. `StreamCut(pieces, end)`은 중간에 멈추는 스트림이다(끝: 결과 불명, 예외, 또는 기다리기). 테스트와 2단계 스모크가 쓴다.

**Control**
- `OutputProgress`(`output_started`·`output`·`output_ended`)는 `TurnProgress`와 따로 둔 runtime-checkable Protocol이다. Driver는 job의 progress가 이것일 때만 조각을 넘긴다. Runner의 `_JobProgress`가 구현한다.
- Driver(`streaming=`):
  - Run 단계를 `try/finally`로 감싸, 어떻게 끝나든 기록한 뒤 `output_ended`를 부른다.
  - 사용자 중지·Provider 예외·Provider가 돌려준 결과 불명·서버 종료에서 `capture`의 바이트를 결과와 함께 기록한다(`_uncertain`).
- Runner:
  - `_Live`는 조각 목록과 끝 위치를 둔다. 작은 조각마다 문자열 전체를 복사하지 않고, 어느 글자부터든 읽는다.
  - API: `live_output`, `live_text(run_id, start)`, `live_version`, `wait_live(after_version, timeout)`.
  - 깨우기는 바뀔 때마다 `asyncio.Event`를 세우고 버린다. job이 끝날 때도 깨운다.
  - 512 KiB를 넘으면 `truncated`이고, **그 뒤로는 아무것도 붙이지 않는다.** 뒤의 작은 조각이 붙으면 글 중간이 빈다.

**설정·화면**
- `HIMARI_STREAMING`(기본 켬): `Application.open(streaming=True)`, `build_runtime(streaming=)`.
- 꺼져 있으면 시작 로그에 한 줄을 남긴다.
- `/api/server`의 `streaming`, `himari status`의 `streaming: on|off`.

**모델 확인 (실제 요청, 2026-10-24)** — `tests/live/test_openrouter_stream_live.py`(6/6 통과), 탐색 스크립트로 비스트리밍과 비교했다.

| 확인 | `openai/gpt-6-luna` | `anthropic/claude-haiku-5.5` |
| --- | --- | --- |
| 긴 답 | 43조각 | 37조각 |
| 저장한 스트림을 정규화한 글 | 흘린 글과 같음 | 흘린 글과 같음 |
| Tool 턴 continuation | `reasoning.summary`+`reasoning.encrypted`(서명 없음) | `reasoning.text`+서명 |
| 비스트리밍과 비교 | 같은 모양 | 같은 모양 |
| 다음 요청에 다시 실음 | 그대로 실림, 턴 완료(208) | 그대로 실림, 턴 완료(208) |
| `[REDACTED]` | 없음 | 없음 |
| 중간 중지 | 74자 뒤 끊김, 원본 47 KB 보존 | 89자 뒤 끊김, 원본 4.3 KB 보존 |

- 두 모델만 `streaming=True`다. 다른 모델은 같은 live 테스트를 통과한 뒤에 더한다(`test_only_the_models_checked_live_declare_streaming`).
- **저장 크기:** 같은 Tool 턴을 비교했다. luna는 4.8 KB → 46.8 KB(약 10배), haiku는 3.7 KB → 12.3 KB(약 3배). 조각마다 id·model 같은 머리가 붙기 때문이다. 개인 서버 규모에서는 받아들인다(§4).
- `Application` 기본값으로 luna에 물었다. 답이 쓰이는 동안 Runner의 글 길이가 네 번 바뀌었고, job이 끝난 뒤 `ended`, 최종 답과 흘린 글이 같았다.

**테스트**
- Provider 32개: SSE 읽기(한 바이트씩, LF·CRLF·CR), 받기·결과 분류, sink, 정규화와 닫힘 실패, continuation 규칙, 선언한 모델 고정.
- Driver·Runner 9개(두 백엔드 18개):
  - 흘림
  - 켜는 조건
  - 요약 호출은 흘리지 않음
  - 중지·결과 불명·예외·서버 종료에서 받은 부분 보존
  - 누적 상태와 늦게 깨어난 읽기
  - 글 길이 상한
- Application 2개, SQLite 재시작 1개, 설정 1개.
- 전체 pytest 2,254개, mypy, import 경계, Node 검사 12개, 브라우저 스모크 전체(화면은 바뀌지 않음).
- 실제 모델 확인용 공용 도우미는 `tests/livekit.py`로 옮겼다. 테스트 모듈끼리는 import할 수 없기 때문이다.

## 15. 구현 노트 (2026-10-24, 2단계: Web)

**서버**
- `interfaces/web/live.py`(새 모듈): 구독자 커서와 `step`, `stream`. §6.1의 규칙이 여기 있다.
  - 깨어날 때마다 누적 상태와 커서를 비교한다. Run이 바뀌었으면 `snapshot`을 보낸다.
  - 15초 조용하면 주석 줄을 보낸다.
  - 5분이 되면 `done` 없이 끝낸다. 브라우저가 다시 연결해 `snapshot`부터 받는다.
- **설계에 더한 것: `done` 이벤트.** 서버가 응답을 닫으면 `EventSource`는 스스로 다시 연결한다(`retry: 1000`). 그래서 job이 끝나 다 보냈을 때는 `done`을 보내고, 페이지가 이것을 받으면 연결을 닫는다. 5분 상한에서는 `done`을 보내지 않으므로 다시 연결된다.
- **구현 중 찾은 것: 서버 종료.** uvicorn은 열린 응답이 끝나기를 시간 제한 없이 기다린 뒤에 애플리케이션을 멈춘다(`timeout_graceful_shutdown` 없음). 그래서 job이 도는 동안 열린 `/live`가 재시작을 붙잡는다. systemd의 기본 정지 제한(90초)에 걸리면 job이 깔끔하게 끝나기 전에 프로세스가 죽는다.
  - `begin_shutdown(app)`이 `closing` 이벤트를 세운다. `__main__`의 `MainServer.handle_exit`가 종료 신호에서 이것을 부른다(`call_soon_threadsafe`).
  - 모든 스트림이 바로 끝난다. lifespan 종료에서도 세운다.
- `GET /api/conversations/{id}/live`:
  - 대화 읽기로 권한을 먼저 확인한다(남의 대화는 404).
  - 주체당 8개(`live.PER_PRINCIPAL`), 넘으면 429 `too_many_streams`.
  - `X-Accel-Buffering: no`. `no-store`와 `nosniff`는 기존 미들웨어가 붙인다.
- `LiveOutput.chars`와 `live_output(text=False)`: 조각마다 깨어날 때 글 전체를 합치지 않는다. `snapshot`에만 전체 글을 쓴다.
- 최종 답 Event의 화면용 값에 `run_id`를 더했다. 쓰는 중 항목이 어느 Event로 바뀌는지 안다.

**화면 (`static/live.js`, 새 모듈)**
- 순수 함수: `reduce`(상태 갱신, 다른 Run의 조각은 무시), `thinkingStyle`·`thinkingWord`·`sunGlyph`·`elapsedText`.
- `LiveAnswer`가 연결과 항목을 맡는다. `app.js`가 부르는 곳:
  - polling이 job이 돈다고 보면 `open`
  - Event를 그리면 `shown(run_id)`, 그 뒤 `keepLast`
  - 상태를 반영한 뒤 `settle(running)`
  - 대화를 옮기면 `reset`
  - 테마가 바뀌면 `restyle`: `data-theme`의 MutationObserver와 `prefers-color-scheme` 변경이 부른다.
- 규칙:

| 상황 | 동작 |
| --- | --- |
| 그 Run의 Event가 이미 보임 | 늦게 온 `snapshot`을 무시한다 |
| Run이 Event 없이 끝남 | 흐린 항목과 안내 문구를 **그 자리에** 남긴다(뒤의 메시지보다 아래로 옮기지 않음) |
| 그 뒤 다음 Run이 시작됨 | 새 항목을 만든다. 끝난 항목은 새로 고칠 때까지 남는다 |
| 아무것도 쓰지 않고 멈춘 Run | 항목을 지운다 |
| 연결이 거부됨(404, 429, 세션 만료) | 10초 동안 다시 열지 않는다. 그동안도 polling이 끝난 답을 보인다 |

- 다시 그리기는 한 프레임에 한 번, 많아야 100 ms마다다. 맨 아래에 있을 때만 따라 내려간다.
- 생각 중 표시(§7.4):
  - `sun`은 꽃잎 기호가 120 ms마다 바뀐다. `hidamari`는 점이 숨 쉬고(CSS) 빛이 느리다.
  - 경과 시간은 이 페이지가 그 Run을 처음 본 때부터 센다. 다시 연결해도 이어지고, 새로 고치면 다시 시작한다.
  - 접근성: `role="status"`에 숨긴 글 `HIMARI is thinking`. 움직이는 부분은 `aria-hidden`이다.
  - reduced motion이면 기호를 하나로 고정하고 빛을 끈다.
- CSS 토큰: `:root`에 `--thinking-style: sun`과 `--thinking-shine`. `warm` 테마는 빛 색만 다르다. 어두운 테마를 더할 때 `--thinking-style: hidamari`를 적는다.

**테스트**
- 서버 10개:
  - 스트림 7개(가짜 상태 소스): 합쳐진 깨우기, Run 바뀜, 끝난 뒤 연결, 상태 없음, keep-alive, 시간 상한, 종료 신호
  - 경로 3개: 권한·`run_id`, 429, `begin_shutdown`
- Node `scripts/live_smoke.mjs` 8개(CI의 static-checks에 추가): 상태 갱신, 단어·기호·시간, 흐름에서 Event로 바뀜, 멈춘 항목이 자리를 지킴, 아무것도 쓰지 않은 Run, 테마 전환, reduced motion, 거부된 연결.
- 브라우저 스모크 `streaming` 시나리오(`ScriptedAdapter(streaming=True, piece_delay, first_delay)`):
  - 생각 중 표시와 테마 전환
  - 글이 한 항목에 흐름
  - 맨 아래에서는 따라 내려가고, 위로 올리면 멈춤
  - 끝난 답이 한 번만 남음
  - 중지하면 흐린 항목과 안내
  - `/live`를 막아도 끝난 답이 옴
  - 새로 고치면 사라짐

**실제 모델 확인 (2026-10-24, 로컬 임시 서버 + 브라우저, gpt-6-luna)**
- 생각 중 표시("Turning to the sun…")가 먼저 나왔다. 그 뒤 쓰는 중 항목의 글이 19번에 걸쳐 늘었고, 끝나자 최종 답 하나로 바뀌었다. 페이지 오류는 없었다.
- 긴 답이 흐르는 동안(`/live`가 열린 채) 서버에 SIGTERM을 보냈다. 4.1초 만에 끝났다(job의 정지 대기 포함).
