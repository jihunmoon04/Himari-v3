# 대화별 모델 선택 v0.1

> 2026-10-20 · **PROPOSED (V3-P64)** · rev. 2 — 검수 NOT_READY(B1; C1·C2) 반영(§12), 재검수 READY(§13). 설계 PR #51 병합. 구현: 1단계 Data·Control·Bootstrap 핵심(§14), 2단계 운영자 목록·Web·CLI는 다음 PR.
>
> **정정(2026-10-20, 구현 중 발견):** §6.2의 "결함"은 없었다. 측정값은 이미 모델이 같을 때만 쓰인다(§6.2, §14).
>
> 소유자 결정(2026-10-20): **Q1~Q4 모두 권고대로**(§9). 운영자 목록, 고른 것을 그대로 저장하고 빠진 모델은 기본 모델 + 표시, 추론 강도도 대화별, 다음 턴부터 적용.
>
> 사용자 방향(2026-10-20): 파일 업로드·다운로드보다 먼저 한다. 파일 첨부의 미디어 처리(Q-31)는 모델의 기능에 따라 달라지므로, 대화마다 모델이 다를 수 있다는 전제를 먼저 세운다.

## 1. 왜, 그리고 범위

지금 HIMARI는 서버 하나에 모델 하나다. 운영자가 `HIMARI_MODEL`·`HIMARI_REASONING_EFFORT`로 정하고, `Application.open`이 그 값으로 Driver를 만든다. 모델을 바꾸려면 서버를 다시 시작해야 하고, 바꾸면 모든 대화가 함께 바뀐다.

이미 갖춰진 것과 없는 것은 다음과 같다.

| 이미 있음 | 없음 |
| --- | --- |
| Run마다 `provider_key`·`model_id` 기록(`RunRecord`, `RunPreparation`) | 운영자가 여러 모델을 제공하는 설정 |
| continuation은 그것을 만든 provider·model이 같을 때만 다시 싣는다(V3-049, `openrouter.py` `_replay`) | 대화별 모델·추론 강도 저장 |
| 캐시 breakpoint는 측정된 모델에만(`ProviderCapabilities.cache_breakpoints`) | Driver가 대화마다 다른 요청 옵션·예산을 쓰는 것 |
| 창·최대 출력은 모델별로 선언(`describe_capabilities`) | Web·CLI의 모델 선택 |

~~**결함 하나:** 크기 측정값(`UsageMark`)은 `provider_key`만 비교한다. 같은 OpenRouter 안에서 모델을 바꾸면 이전 모델로 잰 값을 새 모델 요청의 크기로 쓴다.~~ *정정(2026-10-20): 결함이 아니다. 측정값은 `context_key`도 같아야 쓰이고, `context_key`에는 모델 id가 들어 있다(§6.2).*

| 포함 | 제외 (이후) |
| --- | --- |
| 운영자가 제공 모델 목록과 기본 모델을 정함 | 사용자가 임의의 모델 id를 입력 |
| 대화별 모델·추론 강도 선택, Web·CLI | Provider 여러 개 동시 사용(OpenRouter 하나로 시작; 구조는 막지 않음) |
| 모델별 문맥 예산·입력 한도·출력 예약 | 모델별 가격·비용 표시 |
| 답변마다 그것을 쓴 모델 표시 | 같은 질문을 다른 모델로 다시 생성(regenerate) |
| (~~`UsageMark` 모델 일치 검사~~ — 이미 됨, §6.2) | 운영 중 모델 목록 다시 읽기(재시작으로 바꿈) |

## 2. 다른 호스트들

대화형 LLM 호스트는 대체로 같은 모양이다.
- 모델 선택기는 대화 화면에 있고, 선택은 **그 대화에 남는다**. 다른 대화에 번지지 않는다.
- 대화 중간에 바꿀 수 있고, **다음 메시지부터** 적용된다. 이미 쓴 답은 그대로다.
- 고를 수 있는 모델은 **운영자(관리자)가 정한 목록**이다(자체 호스팅 도구들은 관리자 설정의 모델 목록을 쓴다).
- 추론 강도("thinking" 정도)는 모델에 딸린 선택이다. 지원하지 않는 모델에서는 보이지 않는다.

이 설계는 이 모양을 따른다.

## 3. 운영자 설정: 제공 모델

```
HIMARI_MODEL=openai/gpt-6-luna                     # 기본 모델 (지금과 같음)
HIMARI_REASONING_EFFORT=high                       # 기본 모델의 기본 추론 강도 (지금과 같음)
HIMARI_MODELS=openai/gpt-6-luna,anthropic/claude-haiku-5.5=medium,openai/gpt-5-nano=low
```

- `HIMARI_MODELS`는 쉼표로 나눈 `모델 id[=기본 추론 강도]` 목록이다. 모델 id에 `:`(예: OpenRouter의 `:free`)가 있을 수 있으므로 강도는 `=`로 붙인다.
- `HIMARI_MODEL`은 목록에 있어야 한다. 그 항목에 강도가 없으면 `HIMARI_REASONING_EFFORT`를 쓴다.
- `HIMARI_MODELS`가 없으면 목록은 `HIMARI_MODEL` 하나다. **지금 배포는 설정을 바꾸지 않아도 똑같이 돈다.**
- 시작할 때 모델마다 확인하고, 하나라도 어긋나면 시작을 거부한다(오류에 모델 이름).
  - 기본 강도가 그 모델이 선언한 강도(`reasoning_efforts`)에 없음. 지금은 보내기 직전에 거부되는 것을 시작 시점으로 당긴다.
  - 운영자 예산(`HIMARI_CONTEXT_TOKENS`)이 그 모델의 입력 한도를 넘음(V3-056의 규칙을 모델마다).
  - 중복된 모델 id.
- **목록의 모델은 Tool 호출을 지원해야 한다(운영자 계약).** HIMARI의 요청에는 항상 Tool 목록이 실린다. 사용자에게 묻는 `ask_user`가 늘 카탈로그에 있기 때문이다(`openrouter.py` `prepare_native_request`의 `tools`). Tool 호출을 지원하지 않는 모델은 OpenRouter가 거부한다(404). 그래서 그런 모델로는 대화가 한 번도 돌지 않는다.
- 선언되지 않은 모델(Provider가 기능을 모름)도 목록에 넣을 수 있다. 지금 `HIMARI_MODEL`과 같은 규칙: 예산 `UNKNOWN_WINDOW_TOKENS`, 추론 강도 없음. 시작 로그에 경고를 남긴다: "창·추론 강도·Tool 호출 지원을 확인하지 않은 모델". 선언된 모델(`KNOWN_CAPABILITIES`)은 모두 Tool 호출을 실제로 확인한 모델이다.
- 그런 모델이 요청을 거부하면 지금의 실패 경로를 그대로 탄다. 응답이 비정상(200이 아님)이면 `NativeNoResponse` → 턴은 `PROVIDER_FAILED`이고 다시 보내지 않는다. 받은 오류 본문은 Run에 남는다(V3-018). 사용자는 그 대화의 모델을 다른 모델로 바꿔 다음 턴을 보낼 수 있다.
- 이번에는 Tool 호출 지원을 기능 선언으로 두지 않는다. 모델별 자동 확인(OpenRouter `/models`의 `supported_parameters`)도 하지 않는다. 필요해지면 `ProviderCapabilities`에 `tool_calls`를 더하고 시작 검사로 올린다.

## 4. 대화별 선택

### 4.1 저장 (Data)

새 포트 `ConversationModelSettings`(Data 공개 API). 외부 출처 설정(V3-P53)과 같은 원칙이다: **Data는 들은 것만 저장하고 기본값을 모른다.**

```python
@dataclass(frozen=True)
class ModelSetting(DeepFrozen):
    model_id: str
    reasoning_effort: str | None  # None: 그 모델의 기본 강도

class ConversationModelSettings(Protocol):
    async def read_model_setting(self, scope: TrustedScope) -> ModelSetting | None: ...
    async def set_model_setting(self, scope: TrustedScope, setting: ModelSetting | None) -> None:
        """None: 선택을 지운다(기본 모델을 따른다). 모델 id·강도는 길이와 문자만 검사한다
        (INVALID_ARGUMENT); 제공 목록에 있는지는 Application이 검사한다."""
```

- SQLite v6: `model_settings(conversation_id TEXT PRIMARY KEY REFERENCES conversations, model_id TEXT NOT NULL, reasoning_effort TEXT) STRICT`. v5에서 제자리 업그레이드. 대화 삭제는 아직 없다(Q-19). 생기면 `external_settings`와 함께 지운다.
- `state_revision`은 바뀌지 않는다. 모델은 Prompt 내용이 아니다(V3-049: Prompt는 모델을 모른다).

### 4.2 해석: `ModelCatalog` (Control 공개 API)

```python
@dataclass(frozen=True)
class ModelOffer(DeepFrozen):
    model_id: str
    default_effort: str | None
    efforts: frozenset[str]           # 그 모델이 선언한 강도
    budget: ContextBudget             # 그 모델의 예산·입력 한도 (§6.3)
    max_output_tokens: int | None     # 턴의 출력 예약 (V3-P62)

class ModelCatalog:
    default: ModelOffer
    offers: tuple[ModelOffer, ...]    # 운영자 목록 순서
    def resolve(self, setting: ModelSetting | None) -> ResolvedModel: ...
```

`resolve`는 저장된 선택을 실제로 쓸 모델로 바꾼다.

| 저장된 것 | 쓰는 것 |
| --- | --- |
| 없음 | 기본 모델, 그 기본 강도 |
| 목록에 있는 모델, 강도 없음 | 그 모델, 그 기본 강도 |
| 목록에 있는 모델, 그 모델이 선언한 강도 | 그 모델, 그 강도 |
| 목록에서 빠진 모델 | 기본 모델, 그 기본 강도. `fallback`에 이유(Q2) |
| 목록에 있는 모델, 더는 선언되지 않은 강도 | 그 모델, 그 기본 강도. `fallback`에 이유(Q2) |

`ResolvedModel`은 `RequestOptions`(model_id·reasoning_effort)와 그 `ModelOffer`, 그리고 "선택이 그대로 쓰였는지"(`fallback: str | None`, 화면 표시용)를 담는다.

### 4.3 바꾸기 (Application)

`Application.set_model(scope, model_id | None, effort | None)`:
- 대화를 읽을 수 있는지 먼저 확인한다(NOT_AUTHORIZED, 다른 읽기와 같음).
- 모델이 제공 목록에 없으면 거부(`UnknownModel`, Web 404). 강도가 그 모델의 선언에 없으면 거부(`invalid_effort`, Web 422).
- 강도를 생략하고 모델만 바꾸면 강도 선택은 지워진다(새 모델의 기본 강도). 이전 모델의 강도는 새 모델에서 뜻이 다를 수 있기 때문이다.
- 고른 것을 **그대로 저장한다**. 기본 모델과 같아도 남긴다(Q2). 그래서 운영자가 기본 모델을 바꿔도 이 대화는 고른 모델을 계속 쓴다. 기본을 따르게 하려면 선택을 지운다(`model: null`, CLI `/model default`).
- 저장된 선택은 지우지 않는다. 운영자가 그 모델을 목록에 다시 넣으면 다시 쓰인다.
- **실행 중이어도 저장한다.** 적용 시점은 §5.

## 5. 언제 적용되나: 턴이 시작할 때

**턴 = Driver의 `drive`·`resume` 한 번**(V3-P62의 턴과 같다). 턴이 시작할 때 모델을 한 번 정하고, 그 턴이 끝날 때까지 바꾸지 않는다. 순서는 다음과 같다.

1. 대화 잠금을 잡는다(`_exclusive`).
2. Data에서 그 대화의 선택을 읽고(`read_model_setting`), `ModelCatalog.resolve`로 `ResolvedModel`을 만든다.
3. 그 값으로 턴 상태를 만든다: 요청 옵션(모델·강도·`provider_key`·`session_key`), 문맥 예산, 출력 예약. 지금 `Turn`이 `max_output_tokens`를 받는 자리에서 함께 만든다. 이 턴 상태 하나는 바뀌지 않는다(불변).
4. 그 턴의 모든 Run과 압축 Run(COMPACTION)은 이 턴 상태에서 옵션과 예산을 꺼내 쓴다. 지금 Driver의 고정 필드 `_options`·`_budget`을 읽는 곳(`_options_for`, `_fit_budget`, `_compact`, Run 준비의 `model_id`)이 모두 턴 상태를 읽도록 바뀐다.

Driver는 대화별 모델을 따로 저장하지 않는다. 턴 상태는 잠금을 가진 동안만 있고, 지금의 `_turns`처럼 턴이 끝나면 지운다.

- 실행 중에 바꾸면 지금 턴은 그대로 끝나고, **다음 턴**(다음 메시지, 멈춘 뒤 Continue, `ask_user` 답변)부터 새 모델이다.
- 턴 중간에 바꾸지 않는 이유:
  - 턴의 출력 예약(`Turn`의 `max_output_tokens`)과 예산 검사가 한 모델을 전제로 한다.
  - Tool 호출 묶음 사이에 모델이 바뀌면, 앞 모델의 추론(continuation)이 빠진 채 뒷 모델이 그 Tool 결과를 이어받는다. 턴 경계에서 바꾸면 이 일이 사용자가 고른 시점에만 생긴다.
- 화면은 "다음 메시지부터 적용"이라고 알린다(실행 중일 때).
- `context_status`(문맥 크기 표시)는 실행 중인 턴의 값이 아니라, **지금 저장된 선택으로 다음 턴이 쓸 모델·예산**을 보인다. 잠금 없이 같은 `resolve`를 부른다. 실행 중에 모델을 바꾸면 표시는 바로 새 모델 기준이 되고, 실행 중인 턴은 이전 모델로 끝난다.
- 수동 압축(`compact`)은 잠금을 잡는 하나의 턴이다. 위 1~3을 똑같이 거쳐 그때 선택된 모델로 요약한다.

## 6. 모델이 바뀌면 대화 기록은

### 6.1 추론 continuation — 바꿀 것 없음

이미 provider·model이 같을 때만 다시 싣는다(V3-049). 다른 모델이 쓴 턴은 텍스트·Tool 호출·결과만 간다. 서명·암호화 추론 블록이 다른 모델로 가지 않는다.

이전 모델로 돌아오면, **지금 Prompt에 남아 있고** provider·model이 일치하는 continuation만 다시 싣는다. 압축 요약에 들어간 부분은 Prompt가 그 Event를 읽지 않으므로(`prompt/loader.py` `load`: 요약 범위 다음부터 읽음), 그 부분의 continuation은 돌아오지 않는다. 이것은 모델 전환과 무관하게 지금 압축이 하는 일이다(V3-P48).

### 6.2 크기 측정 — 이미 모델별 (바꿀 것 없음)

*정정(2026-10-20, 구현 중 발견).* rev. 2까지 이 절은 `UsageMark`가 provider만 비교하는 결함이 있다고 적었다. 사실이 아니다. `Compactor.estimate`는 측정값의 `context_key`가 지금 요청의 것과 같을 때만 그 값을 쓴다. `context_key`(`compaction.py` `context_key`)는 provider, **model id**, 추론 강도, 지시문, 서버 안내문, Tool 목록의 digest다. 그래서 다른 모델(그리고 다른 추론 강도)로 잰 값은 이미 쓰이지 않는다. 기존 테스트 `test_a_measurement_under_another_composition_is_not_used`의 `model`·`effort` 경우가 이것을 확인한다. `UsageMark`는 바꾸지 않는다.

모델이 바뀌면 바이트 기반 추정(`measured=False`)이다. 모델을 바꾼 첫 요청은 크게 추정될 수 있다(영어 약 1.4배, Q-14). 그 요청 응답으로 다시 측정되면 정확해진다.

### 6.3 예산과 입력 한도 — 모델마다

`ContextBudget`을 모델마다 만든다. 규칙은 지금과 같다(V3-056, `input_ceiling`·`context_limit`).
- 입력 한도 = 그 모델의 창 − 출력 몫.
- 예산 = 운영자 예산, 없으면 입력 한도를 `DEFAULT_CONTEXT_TOKENS`로 자른 값.

**큰 모델에서 작은 모델로 바꾸면** 대화가 새 예산의 80%를 넘을 수 있다. 이때 새 장치를 두지 않는다. 다음 턴의 첫 Run 앞에서 **지금의 자동 압축**이 돈다(V3-P48, 요약 요청은 예산 안에서만, 큰 메시지는 연쇄 요약 V3-P49). 압축해도 입력 한도를 넘으면 지금처럼 Run을 보내지 않는다(REQUEST_REJECTED, 이유 표시).
- 압축은 **새 모델로** 한다. 요약을 쓰는 모델이 그 요약을 읽을 모델이기 때문이다.
- 화면은 모델을 고를 때 그 모델 기준 크기를 보여 준다. 예산을 넘으면 "다음 메시지 전에 요약됩니다"라고 알린다(§7).

작은 모델에서 큰 모델로 바꾸면 아무 일도 없다. 이미 만든 요약은 그대로 쓴다.

### 6.4 캐시 — 비용만

공급자의 프롬프트 캐시는 모델별이다. 바꾼 뒤 첫 요청은 캐시를 쓰지 못한다(비용이 그만큼 든다). 정확성 문제는 아니다. 세션 키(`session_key`)는 대화별로 그대로 둔다.

### 6.5 Tool·안내문 — 바꿀 것 없음

Tool 목록, 서버 안내문, 지시문은 모델과 무관하다. 모델이 병렬 Tool 호출을 선언하지 않아도 지금처럼 Provider가 처리한다. Tool 호출 자체를 지원하지 않는 모델은 §3의 운영자 계약으로 막는다.

## 7. Web·CLI

**API**
- `GET /api/server`: `model`(기본 모델 id)을 유지하고 `models: [{id, default_effort, efforts, window_known}]`를 더한다. `context_budget`은 기본 모델의 값으로 남긴다(호환).
- `GET /api/conversations/{id}/model` → `{model, effort, chosen: {model, effort} | null, fallback: str | null}`. `model`·`effort`는 다음 턴이 쓸 값, `chosen`은 저장된 선택.
- `PUT /api/conversations/{id}/model` 본문 `{model: str | null, effort: str | null}`. `model: null`이면 선택을 지운다. 응답은 GET과 같다.
- 이벤트 보기: 답변(`assistant_final`)·Tool 호출 묶음에 그것을 만든 Run의 `model`을 붙인다.

**Web**
- 대화 메뉴(`chat-menu`, Tool servers 위)에 "Model" 선택과, 그 모델이 강도를 선언했으면 "Reasoning" 선택. 실행 중이면 "Applies from the next message".
- 새 대화: 첫 메시지 전에도 고를 수 있다(대화가 만들어진 뒤 저장).
- 답변 머리에 모델 이름은 **바로 앞 답변과 다를 때만** 보인다(같은 모델이 이어지면 소음).
- 사이드바의 `server-info`는 기본 모델을 그대로 보인다.

**CLI**
- 대화 안: `/model`(지금 모델과 목록), `/model <id> [effort]`, `/model default`.
- 명령: `himari model ID [MODEL [EFFORT]]`.
- 대화를 열 때 첫 줄에 모델을 적는다. 답변 앞 모델 표시는 Web과 같은 규칙.

## 8. 계약 변경 (Proposed)

| 모듈 | 변경 |
| --- | --- |
| Data | `ModelSetting`, `ConversationModelSettings` 포트(인메모리·SQLite). SQLite v6 `model_settings` |
| Prompt | 없음. 측정값은 이미 `context_key`(모델 포함)로 구별된다(§6.2 정정) |
| Control | `ModelCatalog`·`ModelOffer`·`ResolvedModel`. Driver는 `request_options`·`budget` 하나 대신 catalog를 받고, 턴 시작 때 resolve한 값으로 불변의 턴 상태를 만든다(§5 1~4). `compact`는 같은 절차, `context_status`는 잠금 없이 resolve |
| Provider | 없음. continuation·캐시 규칙은 이미 모델별 |
| Bootstrap | `HIMARI_MODELS` 해석, 모델별 시작 검사(§3), `Application.models`·`model_setting`·`set_model` |
| Web·CLI | §7 |

`Application.model_id`·`context_tokens`는 기본 모델의 값으로 남긴다(호환).

## 9. 소유자 결정

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 고를 수 있는 모델 | (a) 운영자 목록 `HIMARI_MODELS` (b) Provider가 아는 모델 전부(OpenRouter `/models`) | **(a)** — 창·강도·캐시를 확인한 모델만 내놓는다. (b)는 수백 개이고 대부분 창·강도를 모른다 |
| Q2 | 저장과 목록에서 빠진 모델 | (a) 고른 것을 그대로 저장(기본값과 같아도). 빠진 모델이면 기본 모델로 돌고 화면에 "X는 더 제공되지 않아 기본 모델 사용"이라고 표시 (b) 기본값과 다른 것만 저장(V3-055와 같음); 빠진 모델 처리는 (a)와 같음 (c) (a)처럼 저장하되, 빠진 모델이면 턴을 시작하지 않고 사용자가 다시 고르게 함 | **(a)** — 사용자가 고른 모델은 운영자가 기본값을 바꿔도 남는 것이 다른 호스트와 같다. 조용히 바꾸지 않도록 표시한다 |
| Q3 | 추론 강도 | (a) 대화별로 고름(모델이 선언한 것 중) (b) 운영자가 모델별로만 정함 | **(a)** — 같은 모델로 가볍게/깊게를 대화마다 고르는 것이 흔한 쓰임 |
| Q4 | 적용 시점 | (a) 다음 턴부터(실행 중 저장 허용) (b) 다음 Run부터 (c) 실행 중에는 바꾸기 거부 | **(a)** — §5의 이유 |

**소유자 결정(2026-10-20): Q1 (a), Q2 (a), Q3 (a), Q4 (a) — 모두 권고대로.**

## 10. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 사용자가 운영자가 허용하지 않은 모델(비싼 모델)을 고름 | 제공 목록 밖은 저장 전에 거부(Application). Data는 길이·문자만 검사 |
| 저장된 값이 목록에서 빠진 뒤 그대로 보내짐 | 턴마다 `resolve`가 목록과 대조. 목록 밖은 보내지 않고 기본 모델로 돌며, 화면이 그 사실을 보인다(Q2) |
| 다른 모델의 서명된 추론 블록이 섞여 공급자 오류 | 이미 provider·model 일치만 재생(V3-049). 회귀 테스트로 고정 |
| 다른 모델의 측정값으로 크기를 잘못 잡아 한도를 넘김 | 측정값은 `context_key`(모델 포함)가 같을 때만 쓴다(§6.2). 넘어도 공급자 오류는 Run 실패로 남고 재전송하지 않음 |
| 작은 모델로 바꾸자 요약이 반복되거나 실패 | 기존 압축 규칙과 한도 거부를 그대로 탄다. 요약 요청은 예산 안에서만(V3-P48/P49) |
| 다른 사용자의 대화 설정을 읽거나 바꿈 | 모든 읽기·쓰기가 `TrustedScope` 권한 검사를 거친다(다른 대화 설정과 같음) |

## 11. 검증 계획

- **Data:** 인메모리·SQLite 양쪽 계약 테스트(저장·지우기·잘못된 값), v5 → v6 업그레이드.
- **Prompt:** 같은 provider, 다른 model의 측정값은 `measured=False`(기존 테스트가 확인, §6.2). 통합: 모델을 바꾼 뒤 문맥 크기 표시가 `measured=False`.
- **Control:** 턴 시작 때 resolve. 실행 중 바꾸면 그 턴은 이전 모델, 다음 턴은 새 모델(Run 기록의 `model_id`). 압축 Run의 모델. 작은 모델로 바꾼 뒤 자동 압축과 한도 거부. 목록에서 빠진 모델(Q2).
- **Provider·Prompt:** 모델 A → B → A 순서에서 A의 continuation만 A 요청에 실리는 것(OpenRouter, 실제 바이트 비교). 압축 전과 후로 나눈다. 압축 뒤에는 요약에 들어간 A 턴의 continuation이 실리지 않고, 요약 뒤에 남은 A 턴의 것만 실린다.
- **Tool 호출 미지원 모델(B1):** 가짜 Provider가 404 오류 본문을 주면 턴은 `PROVIDER_FAILED`, Run에 본문이 남고, 다시 보내지 않는다. 선언되지 않은 모델이 목록에 있으면 시작 로그에 경고가 남는다.
- **Bootstrap:** `HIMARI_MODELS` 해석(`:` 포함 id, `=` 강도, 중복, 기본 모델 없음), 모델별 시작 검사, `HIMARI_MODELS` 없을 때 지금과 같음.
- **Web·CLI:** API 계약, 권한, 브라우저 스모크(선택 → 메시지 → 답변의 모델 표시).
- **실제 모델(opt-in live):** 한 대화에서 gpt-6-luna → claude-haiku-5.5 → gpt-6-luna. Tool 호출이 있는 턴을 사이에 두고 공급자 오류가 없는 것.

## 12. 검수 처리 — rev. 1(`de0b837`), 독립 검수: NOT_READY

| # | 지적 | 처리 |
| --- | --- | --- |
| B1 | 모델의 Tool 호출 지원 여부를 다루지 않음. 요청에는 Tool 목록이 실리고, 지원하지 않는 모델은 OpenRouter가 거부(404) | §3에 운영자 계약(목록의 모델은 Tool 호출 지원)을 명시했다. `ask_user`가 늘 카탈로그에 있어서 모든 요청에 `tools`가 실린다는 근거도 적었다. 선언되지 않은 모델은 시작 로그에 경고하고, 거부되면 기존 `PROVIDER_FAILED` 경로를 타며 다시 보내지 않는다. 기능 선언(`ProviderCapabilities.tool_calls`)은 필요할 때 더한다. §6.5·§11 보완 |
| C1 | A → B → A에서 continuation이 돌아온다는 설명은 압축된 부분에는 맞지 않음 | §6.1을 "지금 Prompt에 남은 것만" 실린다로 고쳤다. 압축된 부분은 돌아오지 않는다(`loader.py`). §11 테스트를 압축 전·후로 나눴다 |
| C2 | 턴 시작 시 모델을 정하는 절차가 불명확 | §5에 잠금 → 설정 읽기·resolve → 불변 턴 상태 → 모든 Run·압축이 사용하는 순서를 적었다. 고정 필드를 읽던 곳이 턴 상태를 읽도록 바뀐다는 점도 적었다. `context_status`는 저장된 선택 기준 다음 턴 값(잠금 없음), `compact`는 같은 절차. §8 갱신 |

## 13. 재검수 — rev. 2(`27e89fb`): READY

소유자가 전달한 재검수 판정(2026-10-20). 추가 지적 없음.

## 14. 구현 노트 — 1단계: Data·Control·Bootstrap 핵심 (2026-10-20)

| 모듈 | 구현 |
| --- | --- |
| Data | `ModelSetting`, `ConversationModelSettings`(인메모리·SQLite). 모양만 검사한다: 모델 id 1~200자, 강도 1~32자, 보이는 ASCII(`!`~`~`). SQLite v6 `model_settings`. v2~v5에서 제자리 업그레이드(업그레이드 목록을 버전별 추가분에서 만든다) |
| Control | `control/api.py`: `ModelOffer`, `ResolvedModel`, `ModelCatalog`(시작 검사: 중복 id, 목록에 없는 기본 모델, 선언되지 않은 기본 강도 → `ValueError`; `resolve`는 §4.2 표대로). Driver는 `request_options`·`budget` 대신 `models`·`settings`를 받는다. `_exclusive`가 잠금을 잡은 뒤 한 번 resolve해 `_turn_models`에 두고, 턴이 끝나면 지운다(§5 1~4). Run 기록의 `model_id`는 준비한 요청의 `request_options.model_id`에서 가져온다(보낸 것과 기록이 같다). `context_status`는 잠금 없이 resolve |
| Bootstrap | `build_runtime(models=...)`. 없으면 지금처럼 `model_id`·`reasoning_effort`·`context_budget` 하나로 목록을 만든다. 이 경우에도 `ModelCatalog` 검사를 거치므로, 모델이 선언하지 않은 `HIMARI_REASONING_EFFORT`는 이제 보내기 직전이 아니라 **시작할 때** 거부된다(§3). `Runtime.models` |
| Prompt·Provider | 바꾸지 않음(§6.1, §6.2 정정) |

**테스트**(`tests/data/test_model_settings.py`, `tests/control/test_api.py`, `tests/integration/test_conversation_model.py`, SQLite 업그레이드 v5 포함):
- 대화마다 자기 모델·강도. Run 기록이 그 모델이다.
- 턴 중간에 바꾸면 그 턴의 두 Run은 이전 모델, 다음 턴부터 새 모델.
- 큰 모델 → 작은 모델: 다음 턴 앞에서 자동 압축. 요약 요청이 한도에 맞지 않아 두 번의 연쇄 요약이 되었고, 모든 압축 Run이 작은 모델이다. 바꾼 직후 크기 표시는 `measured=False`.
- 목록에서 빠진 모델·강도: 기본으로 돌고 `fallback`이 이유를 말한다. 저장된 선택은 그대로다.
- OpenRouter 바이트(가짜 전송): nano → nano → (압축) → haiku → nano. haiku 요청에는 nano의 추론이 없다. 마지막 nano 요청에는 Prompt에 남은 nano 턴의 것만 실린다. 압축하지 않으면 q1·q2, 압축하면 q2만(C1).
- B1: Tool 호출을 거부하는 모델(404)은 `PROVIDER_FAILED`. 오류 본문이 Run에 남고, 한 번만 보내며, 기본 모델로 돌리면 다음 턴이 된다.

검증: 전체 pytest 1386 passed·51 skipped, mypy --strict, import 검사 통과.

**남은 것(2단계):** `HIMARI_MODELS` 해석과 모델별 예산(Application), 확인되지 않은 모델의 시작 경고(B1), `Application.set_model`과 거부 규칙(§4.3), Web API·화면, CLI `/model`, 답변의 모델 표시.

