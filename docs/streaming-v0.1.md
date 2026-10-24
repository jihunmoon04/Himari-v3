# 스트리밍 v0.1

> 2026-10-24 · **PROPOSED (V3-P73)** · rev. 1: 설계 초안.
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
- `delta.reasoning_details`: 같은 `index`·`type`의 조각에서 `text`/`summary`/`data`를 이어 붙이고 `signature`·`id`·`format`은 비어 있지 않은 마지막 값을 쓴다. `reasoning` 글도 이어 붙인다(continuation의 대체값, V3-049).
- `finish_reason`은 마지막으로 온 비어 있지 않은 값이다. 그 뒤 판정은 지금과 같다: `stop` ↔ FINAL, `tool_calls` ↔ TOOL_CALLS, 나머지는 `INVALID_MODEL_OUTPUT`.
- **닫힘 실패(fail closed):** 다음은 모두 정규화하지 않는다.
  - `[DONE]` 없이 끝난 스트림
  - `error`가 있는 조각
  - JSON이 아닌 `data:`
  - `index`가 맞지 않는 Tool 조각
  - choice가 둘 이상인 조각
- 예전 Run(JSON 응답)은 content type으로 구별해 지금 코드로 정규화한다.

**위험: continuation.** 문서는 스트리밍에서 암호화된 추론이 `[REDACTED]`로 올 수 있다고 하고, 같은 `index` 조각을 어떻게 합치는지는 정하지 않는다. 합친 값이 비스트리밍 응답과 다르면 다음 Tool 턴이 거부되거나 추론이 끊긴다. 그래서 **모델마다 실제 요청으로 확인한 뒤에만 `streaming`을 선언**한다(§9, Q5). 확인 항목:
- 같은 질문의 스트리밍·비스트리밍 continuation이 같은 모양인지
- 합친 continuation을 다시 실은 Tool 턴이 받아들여지는지

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

async def invoke_prepared(self, request, sink: OutputSink | None = None) -> NativeInvocationOutcome
```

- `sink`는 선택이다. 스트리밍하지 않은 요청이면 부르지 않는다. 예외를 던지지 않는다고 가정하지 않는다: Provider는 sink의 예외를 삼키고 읽기를 계속한다(화면 문제로 Run이 실패하지 않게).
- 한 번 보내기(PROVIDER-01)와 결과 분류는 그대로다: 연결 전 실패는 `NativeNoResponse`, 보낸 뒤 끊김은 `NativeUncertain`.
- `ScriptedAdapter`는 스크립트의 답을 몇 글자씩 sink에 넘기는 모드를 갖는다(테스트용).

## 4. 원본 보존 (V3-018 · V3-048)

- 받은 SSE 본문을 **바이트 그대로** `raw_response_body`에 저장하고 content type은 `text/event-stream`이다. 조각을 합친 JSON을 따로 만들어 저장하지 않는다.
  - 받은 순서가 곧 바이트 순서다. 끝 표시는 `data: [DONE]`이다(data-contract-v0.2의 조건).
  - TCP·chunked 경계는 본문이 아니므로 남기지 않는다(지금과 같음).
- **끊긴 스트림:** 받은 데까지의 바이트를 결과 불명 Run에 함께 저장한다. `RawUncertain`에 선택 필드 `body`·`content_type`을 더한다. SQLite 열은 이미 있어 스키마 변경이 없다. 사용자 중지(cancel)도 같다.
- 저장 크기: SSE는 같은 답의 JSON보다 몇 배 크다(조각마다 id·model 같은 머리가 붙음). 개인 서버 규모에서는 받아들인다. 늘어나는 양은 구현 때 재서 적는다.

## 5. Control과 Runner

### 5.1 어느 Run을 흘리나

- 사용자에게 답하는 Run(`drive`·`resume`의 모델 호출)만 흘린다. `RequestOptions.stream = True`는 Driver가 정한다.
- 압축 요약 Run은 흘리지 않는다. 보일 자리가 없다.
- `HIMARI_STREAMING=0`이면 아무 Run도 흘리지 않는다(§9).

### 5.2 쓰는 중 상태

- Driver는 `invoke_prepared`에 sink를 넘기고, 조각을 `TurnProgress`의 새 메서드로 전한다.
  - `output(run_id, delta)`: 조각이 올 때
  - `output_ended(run_id)`: 그 Run의 결과를 기록한 뒤
- `TurnProgress`의 규칙은 그대로다: 예외를 던지지 않고, Driver는 그 값에 기대지 않는다.
- Runner는 Conversation마다 **지금 쓰는 Run 하나**의 상태를 메모리에 둔다: `LiveOutput(run_id, text, tools, thinking, version)`.
  - `version`은 조각마다 1씩 오른다.
  - 그 Run의 결과가 기록되면(`output_ended`) 지운다.
  - 한 Run의 글은 512 KiB까지 모으고, 넘는 조각은 버린다. 화면용이고, 원본은 Data에 다 있다.
- 기다리는 쪽(§6)은 `asyncio.Condition`으로 깨운다.
- 재시작하면 사라진다. 쓰던 Run은 지금처럼 재시작 복구에서 `OUTCOME_UNKNOWN`이 된다.

## 6. Web API

### 6.1 실시간 연결 (Q1)

`GET /api/conversations/{id}/live`는 `text/event-stream`을 돌려준다.
- 인증과 권한은 다른 경로와 같다(세션 쿠키 또는 Bearer, 남의 대화는 404). `EventSource`는 같은 출처의 쿠키를 보낸다.
- 연결하면 먼저 지금 상태를 보낸다. `snapshot {run_id, text, tools, thinking}`, 쓰는 Run이 없으면 `idle`.
- 그 뒤로는 `delta {run_id, text, tools?, thinking?}`가 이어 붙일 부분만 보낸다.
- 그 Run의 결과가 기록되면 `end {run_id}`를 보낸다.
- 새 Run이 시작되면 그 Run의 `snapshot`을 보낸다.
- 일이 끝나면(job running=false) 서버가 연결을 닫는다. 페이지는 일이 돌 때만 연결한다(§7.1). 그래서 열려 있는 연결 수는 일하는 대화를 보는 페이지 수로 묶인다.
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
- 추론 조각만 오는 동안은 "Thinking…" 한 줄을 보인다. 추론 내용은 보이지 않는다(Q2).
- Tool 호출이 시작되면 이름을 보인다(`Preparing inside/write_file…`). 인자는 보이지 않는다.
- `end`가 오면 polling이 그 Run의 Event를 가져올 때까지 쓰는 중 항목을 둔다. Event가 오면 그것으로 바꾼다. 해설(commentary)과 Tool 행, 최종 답은 지금 화면 그대로다.
- 스크롤: 사용자가 맨 아래에 있을 때만 따라 내려간다. 위로 올려 읽는 중이면 움직이지 않는다.

### 7.3 끝나지 않은 답 (Q3)

중지, 연결 끊김, 결과 불명, 잘못된 응답이면 그 Run에서는 Event가 생기지 않는다.
- 쓰는 중이던 글은 **흐리게 그 자리에 남기고** "Stopped — this part was not kept, and HIMARI will not see it."을 붙인다.
- 새로 고치거나 대화를 다시 열면 사라진다. 기록에 없기 때문이다.
- 기록과 모델 입력은 지금과 같다: 끝나지 않은 답은 다음 요청에 들어가지 않는다.

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
| 합친 continuation이 원래와 달라 다음 턴 실패 | 모델별 실측 뒤에만 `streaming`(Q5). 실패하면 그 모델의 선언을 끈다 |
| 끝 표시 없는 스트림을 답으로 씀 | `[DONE]` 없으면 정규화하지 않음(닫힘 실패) |
| 끝없는 스트림 | 바이트 사이 120초, 32 MiB, 출력 토큰 상한 |
| sink 예외로 Run 실패 | Provider가 삼킨다. Runner의 `TurnProgress`도 예외를 던지지 않는다 |

## 11. 소유자 결정 (제안)

| # | 질문 | 권고 |
| --- | --- | --- |
| Q1 | 브라우저에 어떻게 흘리나? | **SSE 연결 하나를 따로 둔다**(§6.1). polling을 빠르게 하는 방법보다 지연이 적고 바이트가 적다. Event와 상태는 지금 polling 그대로라, 연결이 막혀도 기능은 남는다 |
| Q2 | 무엇을 보이나? | **답 글과 Tool 앞 해설, 시작한 Tool의 이름.** 추론은 "Thinking…" 한 줄만 보인다. 추론 글은 모델마다 형식이 다르고(요약·암호화), 보이려면 따로 정할 것이 많다 |
| Q3 | 중지·끊김으로 끝나지 않은 답은? | **흐리게 남기고 "기록되지 않았다"고 적는다. 기록과 모델 입력에는 넣지 않는다**(§7.3). 받은 부분을 기록에 넣으면 끝나지 않은 답을 모델이 사실로 읽는다. 원본은 보존하므로 나중에 "이어 쓰기"를 더할 수 있다 |
| Q4 | 원본은 어떻게 남기나? | **SSE 바이트 그대로**(§4). 합친 JSON을 만들어 저장하면 wire-exact가 아니게 된다. 정규화는 저장본으로 언제든 다시 할 수 있다 |
| Q5 | 어느 모델에서 켜나? | **실제 요청으로 확인한 모델만**(§9). continuation 합치기가 문서에 정해져 있지 않아, 확인하지 않은 모델에서 다음 Tool 턴이 깨질 수 있다. 처음에는 기본 모델 `openai/gpt-6-luna`와 `anthropic/claude-haiku-5.5`를 확인한다 |
| Q6 | 끄는 설정? | **`HIMARI_STREAMING=0`**(§9). 문제가 생기면 배포 없이 지금 동작으로 돌아간다 |

## 12. 검증 계획

**Provider 단위**
- 기록해 둔 SSE 본문으로 정규화한다.
  - 글, 여러 Tool 호출(조각난 인자), 추론 조각, 사용량 조각
  - 주석 줄, CRLF·CR 줄 끝, 한 바이트씩 나눈 전송
- 닫힘 실패: `[DONE]` 없음, 오류 조각, 오류 조각만, 깨진 JSON, `index` 어긋남, 32 MiB 넘음.
- 같은 내용의 JSON 응답과 SSE 응답이 같은 정규화 결과와 continuation을 내는지 확인한다(fuzz 포함).
- sink에 간 조각을 이으면 정규화한 글과 같은지, sink 예외가 Run을 바꾸지 않는지 확인한다.

**Control·Runner**
- 조각이 Runner 상태에 쌓이고, 결과를 기록한 뒤 지워진다.
- 압축 요약 Run은 흘리지 않는다.
- 중지하면 연결이 끊기고, 받은 부분이 `RawUncertain` 원본으로 남는다.
- 재시작 뒤 저장된 SSE 응답을 다시 정규화한다.

**Web**
- `/live`: snapshot·delta·end 순서, 남의 대화 404, 일이 끝나면 닫힘, 5분 상한(시계 주입), 연결 수 한도.
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
