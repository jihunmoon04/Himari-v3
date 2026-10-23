# 사용자 지시문(고정 프롬프트) v0.1

> 2026-10-23 · **PROPOSED (V3-P69)** · rev. 3 — 소유자 설계 검수 READY, 비차단 N1(턴별 스냅샷의 구현 자리)·N2(경계는 이스케이프로) 반영(§12). rev. 2 — 소유자 결정 Q1~Q4 권고대로(§9). 소유자 방향 반영: 범위 키(§6), 모델이 고칠 때는 승인이 필요한 변경안으로만(§3.1), 메모리와의 경계(§3.2). rev. 1: 설계 초안.
>
> 소유자 방향(2026-10-23): 프로젝트·lab 같은 묶음 단위와 명제를 추출하는 메모리가 나중에 필요하다. 프로젝트별 지시문은 사용자의 명시적 요청에 따라 에이전트가 고쳐야 할 수 있다.
>
> 소유자 방향(2026-10-22): 기본 에이전트 기능으로 "CLAUDE.md처럼 사용자가 입력할 수 있는 고정 프롬프트"를 둔다. 파일(V3-P66) → 이미지(V3-P67) → 작업 진행 줄(V3-P68) 다음이다. 이어서 plan/note, 메모리를 한다.
>
> 앞선 결정: 프롬프트의 논리적 순서 Stable Prefix → Conversation → Current State, 사용자 데이터·Plan·Memory는 높은 우선순위의 지시문으로 승격하지 않는다(prompt-composition §1). 턴마다 모델을 한 번 정한다(V3-P64). MCP 서버 안내문의 자리(V3-P60). 업그레이드 전 백업(V3-P65).

## 1. 왜, 그리고 범위

지금 모델이 받는 고정 지시문은 코드에 박힌 것뿐이다.
- `DEFAULT_INSTRUCTIONS = ("You are HIMARI, a helpful assistant.",)`, 그 뒤에 시스템 안내 세 단락(`ATTACHMENT_NOTE`, `TOOL_IMAGES_NOTE`, `PROGRESS_NOTE`).
- 운영자가 바꿀 설정도, 사용자가 쓸 자리도 없다. "답은 한국어로", "코드는 이 규칙으로", "나는 이런 사람이다" 같은 것을 대화마다 다시 말해야 한다.

| 포함 | 제외 (이후) |
| --- | --- |
| 사용자가 쓰는 지시문 하나: 모든 대화의 모든 턴에 들어감(Q1) | 대화별 지시문(Q1) |
| Web 설정 화면과 CLI 명령으로 읽고 고침 | Workspace 파일(`AGENTS.md` 등)을 지시문으로 읽기(Q2) |
| 길이 상한(Q3), 동시 수정 충돌 방지 | 모델의 변경안과 사용자 승인(§3.1) — 프로젝트·메모리 패킷에서 |
| 범위 키 `(owner, scope)`, 지금은 `user`만(§6) | 프로젝트·lab 범위의 지시문(§3.1) |
| 다음 턴부터 적용(Q4) | 지시문의 변경 이력 화면 |

## 2. 다른 호스트

- **Claude Code:** `CLAUDE.md`. 사용자 전체(`~/.claude/CLAUDE.md`)와 프로젝트별(저장소 안) 파일이 있고, 세션을 시작할 때 문맥에 넣는다. 파일이므로 모델도 Tool로 고칠 수 있다.
- **Codex 등 코드 에이전트:** `AGENTS.md`를 저장소의 지시문 파일로 읽는다(여러 도구가 함께 쓰는 관례).
- **ChatGPT:** 사용자 전체의 "맞춤 지시"와 프로젝트별 지시. 설정 화면에서 사용자가 쓴다.
- **claude.ai:** 사용자 전체의 "개인 선호"와 프로젝트별 지시. 설정 화면에서 사용자가 쓴다.

공통점: **사용자 전체 하나 + 묶음(프로젝트)별 하나**, 그리고 **사용자가 쓴다**. HIMARI에는 프로젝트가 없고 대화만 있다. 그래서 먼저 사용자 전체 하나를 두고(Q1), 저장소 파일 방식은 지금 하지 않는다(Q2).

## 3. 무엇을, 누가

- **사용자 지시문:** 주체(principal)마다 글 하나. 비어 있으면 없는 것이다(머리말도 붙이지 않음).
- **쓰는 사람은 사용자뿐이다.** Web·CLI의 설정으로만 바뀐다. 모델에게는 이를 고치는 Tool이 없다.
  - 이유: 모델이 고칠 수 있으면, 웹 페이지나 Tool 결과에 숨은 지시(프롬프트 주입)가 모델을 통해 지시문에 들어가 **모든 대화에 계속 남는다**. 모델이 스스로 남기는 기억은 메모리 패킷에서 따로, 그 위험을 다루며 설계한다.
- 지금 Web·CLI의 주체는 운영자 한 명(`OPERATOR`)이다. Data는 주체별로 두므로, 주체가 늘어도 각자의 지시문이 된다.

### 3.1 나중의 범위와 모델의 변경 (방향, 이 패킷에서 구현하지 않음)

- **범위:** 사용자 전체 → 프로젝트(lab) → 대화. 지시문과 메모리 모두 이 범위를 따른다. 저장 키를 처음부터 `(owner, scope)`로 둔다(§6). 프로젝트가 생기면 `scope = 'project:<id>'` 행을 더한다. 모델에게는 사용자 지시문 다음에 프로젝트 지시문이 같은 방식(머리말 + 경계)으로 간다.
- **모델이 고쳐야 할 때:** 사용자가 대화에서 "이 프로젝트 지시문에 X를 넣어 줘"라고 해도 모델은 지시문을 직접 바꾸지 않는다. **변경안을 내고, 사용자가 화면에서 승인한다.**
  - 모델은 "사용자가 정말 요청했는지"를 확인할 수 없다. Tool 결과나 웹 페이지에 "사용자가 이렇게 바꾸라고 했다"가 숨어 있어도 구분하지 못한다. 그래서 승인은 대화 밖의 경로로 받는다.
  - 변경안 Tool(예: `propose_instructions_change(scope, text, reason)`)은 아무것도 바꾸지 않고 변경안만 저장한다. Web·CLI가 차이(diff)를 보이고, 사용자가 승인하면 서버가 리비전 검사(§6)를 거쳐 반영하고 대화에 기록한다.
  - 대화 속의 "응, 바꿔"라는 글은 승인이 아니다. 그 글도 모델을 거쳐 해석되기 때문이다.
  - 프로젝트 지시문을 저장소 안의 파일(`AGENTS.md` 등)로 두게 되면, 그 파일을 보호 경로로 두어 일반 쓰기 Tool로는 바뀌지 않게 하고 같은 변경안 경로로만 바꾼다.
- 이 패킷의 규칙("모델에게는 고치는 Tool이 없다")은 위 방향과 어긋나지 않는다. 모델은 고칠 수 없고 제안만 할 수 있다.

### 3.2 메모리와의 경계

| | 지시문(이 패킷) | 메모리(이후 패킷) |
| --- | --- | --- |
| 쓰는 주체 | 사용자. 모델은 변경안만(§3.1) | 모델이 대화에서 명제를 추출 |
| 성격 | 규칙. 따라야 하는 것 | 사실·관찰. 참고하는 것이고 틀릴 수 있음 |
| 넣는 방식 | 늘 전부 | 관련된 것만 골라서 |
| 자리 | 시스템 메시지(§4) | Current State에 출처와 함께. 지시로 올리지 않음(prompt-composition §1) |
| 고침 | 사용자가 직접 | 모델이 더하고 고침. 사용자가 보고 지움. 오래되면 만료 |

- **경계에 걸치는 것:** 대화 중 "앞으로 항상 X 해 줘" 같은 지시형 선호를 메모리 추출이 조용히 지시문으로 만들지 않는다. 지시문 변경안(§3.1)으로 내고 사용자가 승인한다.
- 메모리는 출처(대화·메시지)를 함께 남겨, 주입으로 들어온 명제를 사용자가 찾아 지울 수 있게 한다. 자세한 것은 메모리 패킷에서 정한다.

## 4. 모델에게 가는 자리

시스템 메시지(Stable Prefix) 안, 고정 지시문 뒤, MCP 서버 안내문 앞이다.

```text
You are HIMARI, a helpful assistant.            ← 운영자 지시문
<ATTACHMENT_NOTE> <TOOL_IMAGES_NOTE> <PROGRESS_NOTE>   ← 시스템 안내
The user's standing instructions follow, between <user_instructions> tags. The user wrote them for
every conversation; follow them unless they conflict with the instructions above. In them, the
characters <, > and & are written as &lt;, &gt; and &amp;.
<user_instructions>
…사용자가 쓴 글(<, >, & 이스케이프)…
</user_instructions>
<MCP 서버 안내문 (V3-P60)>
```

- **왜 시스템 메시지인가:** 사용자가 직접, 모든 대화에 대해 쓴 글이다. 대화 중의 메시지보다 오래 가는 지시이므로 고정 앞부분이 맞다. ChatGPT·claude.ai의 사용자 지시도 대화 밖의 고정 지시로 들어간다.
- **우선순위는 글로 말한다:** 머리말이 "위의 지시와 어긋나면 위를 따르라"고 한다. 운영자·시스템 안내(첨부 줄이 데이터라는 것, Tool 이미지 표시 등)가 사용자 글보다 앞선다. prompt-composition §1의 "사용자 데이터를 높은 우선순위로 승격하지 않는다"와 맞는다. 이 글은 사용자의 의도된 지시이고, 운영자 지시 아래에 있다.
- **MCP 서버 안내문보다 앞:** 사용자가 쓴 글이 외부 서버가 보낸 글보다 믿을 만하다.
- **경계 표시(검수 N2):** 글은 `<user_instructions>` 안에 넣되, `&`·`<`·`>`를 `&amp;`·`&lt;`·`&gt;`로 바꿔 넣는다(이 순서로, `&` 먼저). 그래서 글 안의 어떤 표기(`</user_instructions>`, 공백·대소문자를 바꾼 변형, 다른 태그)도 경계나 다른 구획처럼 보일 수 없다. 머리말이 이 표기를 모델에게 알린다. 저장된 글은 바꾸지 않는다. 이스케이프는 모델에게 보낼 때만 한다(`prompt` 쪽 한 함수, Provider는 받은 문자열을 그대로 쓴다).
- **태그는 강제가 아니다:** 경계와 머리말은 모델이 글의 출처와 우선순위를 알게 하는 표시일 뿐이다. 모델이 늘 따른다고 가정하지 않는다. 실제로 지켜야 하는 것(Tool 권한, Sandbox, 승인, 원문 보존)은 지시문과 상관없이 코드가 지킨다. 사용자 지시문은 사용자가 대화에서 말할 수 있는 것 이상의 권한을 주지 못한다.
- **프롬프트 캐시:** 고정 앞부분의 일부다. 지시문을 바꾸면 다음 턴의 첫 요청에서 캐시가 한 번 새로 시작된다. 자주 바꾸는 글이 아니므로 괜찮다.
- **압축 요약 요청**(`COMPACTION_INSTRUCTIONS`)에는 넣지 않는다. 요약은 지금처럼 대화만 본다.

## 5. 적용 시점 (Q4)

**턴이 시작될 때 읽고, 그 턴 내내 같다.** 모델 선택(V3-P64)과 같은 규칙이다.
- 한 턴 안의 요청들은 같은 앞부분을 쓴다. 캐시가 유지되고, 턴 중간에 규칙이 바뀌지 않는다.
- 일하는 중에 바꾸면 "다음 메시지부터 적용"이라고 알린다(Web 설정 화면, CLI 출력).
- Continue로 이어지는 턴은 새 턴이므로 그때의 지시문을 읽는다.
- 무엇이 쓰였는지는 각 Run의 요청 원문에 그대로 남는다(V3-048). 지시문은 `context_key`(`prompt/compaction.py`)에도 들어가 있어, 바뀌면 크기 추정을 새로 한다.

**구현 자리(검수 N1):**
- 지금 `PromptContextLoader`는 서버가 시작할 때 받은 지시문을 `_instructions`에 두고 모든 대화가 함께 쓴다. **이 값을 바꾸지 않는다.** 여러 대화가 동시에 돌 때 지시문이 섞인다.
- 대신 Driver가 턴을 시작할 때(`_exclusive`, 모델을 정하는 `_turn_models`와 같은 자리) 그 대화 주체의 지시문을 한 번 읽어 그 턴의 상태로 둔다. 각 Run이 `PromptInputs`를 만들 때 그 값을 넘긴다. 고정 지시문(운영자 지시문 + 시스템 안내) 뒤에 사용자 지시문 구획이 붙는 것은 Prompt 쪽 한 곳에서 한다.
- **다음 턴을 보는 곳은 최신 값을 읽는다:** `context_status()`(문맥 크기 표시), 수동 압축의 크기 판단은 지금 진행 중인 턴이 아니라 다음 턴의 문맥을 계산하므로 DB의 최신 지시문을 읽는다. 진행 중인 턴의 Run은 턴 스냅샷을 쓴다.
- 턴 밖에서 도는 압축 Run(요약 요청)은 지시문을 쓰지 않는다(§4).

## 6. Data

```sql
-- v10
CREATE TABLE instructions (
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,          -- 'user'; later 'project:<id>' (§3.1)
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, scope)
) STRICT;
```

- `InstructionScope`: 지금은 `USER` 하나. 다른 값은 거부한다(프로젝트는 그 패킷에서 더한다).
- `read_instructions(principal, scope) -> Instructions(text, revision, updated_at)`: 행이 없으면 빈 글, 리비전 0.
- `write_instructions(principal, scope, text, expected_revision) -> Instructions`: 리비전이 맞을 때만 바꾸고 1 올린다. 다르면 `REVISION_CONFLICT`. 두 탭에서 고칠 때 나중 것이 앞 것을 모르고 덮지 않는다.
- **저장 전 검사:**
  - 줄바꿈을 `\n`으로 맞춘다. 앞뒤 공백만 있으면 빈 글이다.
  - 길이 상한(Q3) 초과는 `TOO_LARGE`.
  - NUL과 줄바꿈·탭이 아닌 제어 문자는 `INVALID_ARGUMENT`. 태그 표기는 거부하지 않는다. 모델에게 보낼 때 이스케이프하므로(§4, 검수 N2) 경계를 흐릴 수 없다.
- 메모리 백엔드(`data/memory.py`)도 같은 계약을 따른다.
- **수명:** 대화를 지워도(V3-P65) 남는다. 대화의 것이 아니라 사용자의 것이다. 업그레이드(v9 → v10)는 V3-P65의 백업 절차를 따른다.

## 7. Web

- 사이드바 아래 `Trash` 위에 `Instructions` 항목. 누르면 설정 화면(`surface-instructions`)이 열린다.
- 화면 구성:
  - 설명 한 줄: "HIMARI follows these in every conversation."
  - 큰 입력란(`textarea`)과 글자 수 `1,234 / 10,000`.
  - `Save`와 `Discard changes`.
  - 마지막 저장 시각.
- 저장하면 상태 줄에 "Saved. Applies from the next message."를 띄운다(일하는 중인 대화가 없어도 같은 말).
- **충돌:** 저장할 때 읽은 리비전을 보낸다. 409면 "Changed elsewhere since you opened it."을 띄우고, 내 글은 입력란에 그대로 둔 채 `Load the saved version` 버튼을 보인다. 사용자가 고르게 하고 자동으로 덮지 않는다.
- **떠날 때:** 저장하지 않은 고침이 있으면 다른 화면으로 갈 때 묻는다(`confirm`).
- 글은 입력란의 값으로만 다룬다. HTML로 그리지 않는다.
- 좁은 화면: 입력란이 화면 폭에 맞고 가로 스크롤이 없다.

**API**
- `GET /api/instructions` → `{text, revision, updated_at, max_chars}`
- `PUT /api/instructions` `{text, revision}` → 200 `{…}`. 오류는 다음과 같다.
  - 409 `revision_conflict`
  - 422 `too_large` (구현: 413이 아님 — 본문 크기 미들웨어가 모든 413을 `request_too_large`로 바꾸므로, §13)
  - 422 `invalid_instructions`
- 인증·CSRF·Origin 검사는 다른 쓰기 API와 같다.

## 8. CLI

- `himari instructions` — 지금 글을 그대로 찍는다. 비었으면 `(no instructions)`를 stderr로.
- `himari instructions set FILE` 또는 `-`(stdin) — 파일의 글로 바꾼다. 읽은 리비전으로 저장하므로, 그 사이 Web에서 바뀌었으면 충돌로 실패하고 다시 하라고 알린다.
- `himari instructions clear` — 묻고(`y`) 비운다. `--yes`면 묻지 않는다.
- 저장 뒤 stderr: `(saved; applies from the next message)`.
- `himari chat`의 `/instructions`는 지금 글을 보인다(고치기는 위 명령으로).

## 9. 소유자 결정

2026-10-23 소유자 결정: Q1~Q4 모두 권고대로.

| # | 질문 | 권고 → 결정 |
| --- | --- | --- |
| Q1 | 범위: 사용자 전체 하나만인가, 대화별 지시문도 함께인가? | **사용자 전체 하나만.** 다른 호스트의 "묶음별"은 프로젝트별인데 HIMARI에는 프로젝트가 없다. 대화별은 그 대화의 첫 메시지로 대신할 수 있다. 필요해지면 같은 표에 범위를 더한다(§3.1) → 권고대로 |
| Q2 | 어디에: DB에 두고 설정으로 고치나, Workspace의 파일(`AGENTS.md` 등)을 읽나? | **DB와 설정 화면.** 파일이면 모델도 Tool로 고칠 수 있어 주입이 모든 대화에 남는다(§3). 파일 관례는 저장소 단위 코드 작업용이고, HIMARI의 Workspace는 사용자 전체 폴더라 맞지 않는다. 모델이 쓰는 기억은 메모리 패킷에서(§3.2), 모델의 변경은 변경안으로(§3.1) → 권고대로 |
| Q3 | 길이 상한? | **10,000자.** 지시문은 모든 요청에 들어간다. 한국어는 글자당 토큰이 많아 10,000자가 기본 문맥 예산(128k 토큰)의 한 자릿수 % 안팎이다. 대부분의 지시문은 수천 자 안이다 → 권고대로 |
| Q4 | 언제부터 적용? | **다음 턴부터**(§5). 모델 선택과 같다. 턴 안에서 규칙과 캐시가 바뀌지 않는다 → 권고대로 |

## 10. 위협과 대응

- **지시문을 통한 지속 주입:** 모델이 지시문을 고칠 길을 두지 않는다(§3). 사용자의 설정 API만 쓴다. 이 API는 인증된 사용자의 쓰기이고, Tool이나 MCP에서 부를 수 없다.
- **경계 흐리기:** 모델에게 보낼 때 `&`·`<`·`>`를 이스케이프하고 제어 문자는 저장 때 거부한다(§4·§6). 머리말이 우선순위를 밝히지만 강제가 아니다. 지켜야 하는 것은 코드가 지킨다(§4).
- **크기로 문맥 압박:** 상한(Q3). 크기 추정에 들어가므로, 앞부분만으로 예산을 넘으면 지금처럼 요청 전에 거부되고 이유가 보인다.
- **다른 사용자의 글:** 주체별 행이고, 읽기·쓰기 모두 인증된 주체 것만 다룬다.
- **XSS:** Web은 입력란 값으로만 다룬다(§7). CLI는 사용자의 글을 그대로 찍지만 표시 전용으로 안전하게 바꾼다(`safe_block`).

## 11. 검증 계획

- **Data(`tests/data`):** 메모리·SQLite가 같은 계약을 지킨다. `USER` 밖의 범위는 거부한다.
  - 비었을 때 리비전 0이다. 쓰면 리비전이 오르고, 리비전이 틀리면 충돌이다.
  - 상한과 제어 문자, 닫는 태그를 거부한다. 줄바꿈을 맞춘다.
  - 주체별로 따로 다룬다. v9 → v10 업그레이드에서 기존 데이터가 남는다.
- **Prompt/Bootstrap:** 지시문의 자리를 확인한다(시스템 안내 뒤, MCP 안내문 앞). 비면 머리말도 없다.
  - 턴 중간에 바꾸면 그 턴의 다음 요청은 옛 글이고, 다음 턴은 새 글이다.
  - 압축 요청에는 없다. `context_key`가 바뀐다.
  - 이스케이프: `</user_instructions>`, `</USER_INSTRUCTIONS >`, `<system>`, `&lt;` 같은 글이 모두 `&lt;…`·`&amp;lt;`로 가서 경계가 하나뿐이다. 저장된 글은 원래대로다.
  - 동시성(검수 N1): 두 대화가 동시에 돌고 있을 때 지시문을 바꾸면 둘 다 그 턴은 옛 글, 다음 턴은 새 글이다. 다른 주체의 글이 섞이지 않는다. 서버 공유 상태(`PromptContextLoader`)는 바뀌지 않는다.
  - `context_status()`는 진행 중인 턴과 상관없이 최신 지시문으로 크기를 계산한다.
- **Web API(`tests/interfaces`):** GET·PUT, 409·413·422, 인증 없으면 401, Origin 검사.
- **CLI:** 보기, `set FILE`·`set -`, `clear`(묻기·`--yes`), 충돌 안내, `/instructions`.
- **브라우저 스모크:** 설정 화면에서 쓰고 저장하면 그 다음 메시지의 요청에 글이 들어간다(대본 모델이 받은 앞부분 확인). 다른 탭에서 바뀐 뒤 저장하면 충돌 안내가 나오고 내 글이 남는다. 저장하지 않고 떠나면 묻는다. 좁은 화면을 본다.

## 12. 소유자 설계 검수 — rev. 2(`7bd25ef`): READY

| # | 지적 | 반영 |
| --- | --- | --- |
| N1 | `PromptContextLoader._instructions`는 서버가 공유한다. 이를 바꾸면 동시에 도는 대화의 지시문이 섞인다. 턴을 시작할 때 읽어 그 턴의 상태로 두고 `PromptInputs`에 넘겨야 한다. `context_status()`는 다음 턴의 문맥이므로 최신 지시문을 읽어야 한다 | §5에 구현 자리를 적었다(`Driver._exclusive`에서 한 번 읽어 턴 상태로 두기, 공유 상태는 그대로). 동시성 테스트를 §11에 더했다 |
| N2 | 닫는 태그만 거부하면 변형 표기를 다 막지 못한다. 이스케이프해 넣는 편이 명확하다. 태그가 우선순위를 강제한다고 가정하면 안 된다 | 모델에게 보낼 때 `&`·`<`·`>`를 이스케이프하고 머리말에 알린다. 저장 때의 태그 거부는 없앴다(§4·§6). 태그와 머리말은 표시일 뿐이고, 지켜야 하는 것은 코드가 지킨다고 적었다(§4·§10) |

## 13. 구현 노트 — 1단계: Data·Prompt·Control·Bootstrap·Web API·CLI (2026-10-23)

- **Data:**
  - `data/api.py`에 `MAX_INSTRUCTIONS_CHARS = 10_000`, `InstructionScope`(`USER`), `Instructions(text, revision, updated_at)`, `InstructionsStore` 포트를 두었다.
  - `data/rules.py`의 `check_instructions`가 줄바꿈을 맞추고, 공백뿐이면 빈 글로 하고, 길이(코드 포인트)를 보고, `Cc` 제어 문자를 거부한다. 줄바꿈·탭은 허용한다. 범위 검사(`check_instruction_scope`)는 읽기에도 쓴다.
  - 메모리 백엔드는 `InMemoryStore.instructions`, SQLite는 v10 `instructions` 표를 쓴다. 키는 `(owner, scope)`이다. 리비전이 다르면 `VERSION_CONFLICT`이다.
  - 업그레이드 테스트는 v2~v9 → v10 모두다.
- **Prompt:**
  - `USER_INSTRUCTIONS_HEADER`와 `user_instructions_section`(`&` → `<` → `>` 순으로 이스케이프)을 두었다. 공백뿐이면 `None`이다.
  - `PromptInputs.user_instructions`가 있고, `Composer`의 `stable_prefix`가 고정 지시문 뒤에 그 구획을 붙인다. Provider는 지금처럼 `stable_prefix` 뒤에 MCP 안내문을 붙이므로 §4의 순서가 된다.
  - `context_key`에 `user_instructions`가 들어간다.
  - `PromptContextLoader.load`는 `user_instructions` 인자를 받는다. 공유 상태(`_instructions`)는 그대로다.
- **Control:**
  - `Driver(user_instructions=…)`가 `_exclusive`에서 모델과 함께 한 번 읽어 `_turn_instructions`에 둔다. 턴 안의 모든 `load`(Run 두 곳, 수동 압축)는 이 값을 쓴다.
  - `context_status`는 다음 턴을 보므로 DB의 최신 값을 읽는다.
- **Bootstrap:** `build_runtime`이 Data를 넘긴다. `Application.instructions(principal)`과 `set_instructions(principal, text, revision)`를 두었다.
- **Web API:**
  - `GET`·`PUT /api/instructions`를 두었다. 거절은 409 `revision_conflict`, 422 `too_large`, 422 `invalid_instructions`이고, 메시지에 입력을 담지 않는다. 형식이 틀린 본문은 400/422(다른 API와 같은 검증)이다.
  - **설계와 다른 점:** `too_large`는 413이 아니라 422다. `BodyLimitMiddleware`가 모든 413 응답을 `request_too_large`로 바꾸기 때문이다(업로드 경로는 그래서 예외로 둔다). 보안 미들웨어를 넓히지 않고 상태 코드를 바꿨다. 10,000자 한국어(약 30 KB)는 본문 상한 256 KiB 안이다(테스트).
- **CLI:**
  - `himari instructions [set FILE|- | clear [--yes]]`를 두었다.
  - 보이기는 줄과 탭을 그대로 두고 터미널에 위험한 글자만 이스케이프한다. 그래서 출력한 것을 다시 `set`할 수 있다.
  - 저장은 방금 읽은 리비전으로 한다. 그 사이 바뀌었으면 `refused (revision_conflict)`이다.
  - 채팅의 `/instructions`는 보이기만 한다.
- **테스트:**
  - `tests/data/test_instructions.py`(두 백엔드), `tests/data/test_sqlite.py`(v10 업그레이드), `tests/prompt/test_composer.py`(자리·이스케이프·빈 글·`context_key`).
  - `tests/bootstrap/test_instructions_app.py`:
    - 턴 중간에 바꿔도 그 턴은 옛 글, 다음 턴은 새 글이다.
    - 두 대화가 동시에 돌 때 둘 다 옛 글이다.
    - 주체별로 따로이고, 없으면 구획이 없다.
    - `context_status`는 최신 글로 계산한다.
    - 요청 원문에 남는다.
  - `tests/interfaces/test_web_instructions.py`, `tests/interfaces/test_cli_instructions.py`.
- **2단계:** Web 설정 화면(`surface-instructions`)과 브라우저 스모크.
