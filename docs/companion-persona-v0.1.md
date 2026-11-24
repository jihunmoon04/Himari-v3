# 반려 바탕과 여러 페르소나 (V3-P108 설계 초안 v0.1)

상태: **Confirmed**(2026-11-24). 소유자 결정 Q1~Q7 모두 권고대로(2026-11-24, §11). 1부·2부 구현(§13), 운영 배포 2026-11-24([기록](deployments/2026-11-24-v3-p108.md)).

> 소유자(2026-11-24), 반려 특화 검토 뒤:
> 1. 범용 에이전트를 철회한다.
> 2. 캐릭터 설정은 Web에서 페르소나로 정할 수 있게 한다.
> 3. 반려를 끄는 모드는 남긴다.
> 4. 반려 모드용 페르소나를 여러 개 저장해 두고 골라 쓸 수 있으면 좋겠다.

이 문서는 [V3-P89 프롬프트 정비](prompt-cleanup-v0.1.md)의 소유자 결정 "바탕은 범용 에이전트, 반려는 선택 기능"과 §3.1(바탕 글)·§4(페르소나 칸 하나)를 바꾼다. V3-P89의 나머지(원칙 한 문단, 절 구조, 자기 지칭 "you", 페르소나는 반려가 켜져 있으면 모든 대화에 적용(A), 압축 요약은 그대로)는 유지한다.

앞선 결정
- **V3-P106:** HIMARI는 Windows 반려 앱의 두뇌로 남는다. 범용 코딩 일은 Claude Code·Codex가 맡는다.
- **V3-P89 §4:** 페르소나는 `instructions` 표의 `companion_persona` 범위 하나다. 반려가 켜져 있고 글이 있을 때만 시스템 글 끝(사용자 지시문 바로 앞)에 들어간다. 턴 시작에 한 번 읽는다.
- **V3-P83 Q6:** 반려 전용 대화는 없다. 모든 대화가 반려 대화다.
- **V3-P92:** 반려 기기의 입력은 반려 기능을 모두 통과한 모델만 받는다. Web 대화는 어떤 모델이든 쓸 수 있다.
- **V3-P83 §4:** 기기 토큰은 기본 거부다. 설정·지시문·기억 경로는 403 `device_scope`.
- 시스템 글이 바뀌면 배포 직후 대화마다 캐시가 한 번 깨진다. `context_key`는 조립된 시스템 글 전체를 해시하므로 토큰 추정도 따라간다(V3-P89 §7).

## 1. 지금

| | 지금 (V3-P89) | 문제 |
| --- | --- | --- |
| 바탕 글 | `You are HIMARI, a general-purpose AI agent working for one user. You talk with them and use tools to get things done. …` (`prompt/system.py` `BASE_INSTRUCTIONS`) | 반려가 주인데 정체성 첫 줄이 작업 에이전트 |
| 반려 절 머리 | `Companion mode is on: besides your work, you are the user's companion, …` (`prompt/api.py` `COMPANION_HEAD`) | 일이 주, 반려가 부 |
| 페르소나 | 사용자당 글 하나(`InstructionScope.COMPANION_PERSONA`), Web Settings → Instructions의 두 번째 칸 | 여러 캐릭터를 바꿔 쓰려면 매번 글을 지우고 다시 써야 함 |
| 페르소나가 없을 때 | 반려 절만 들어감, 말투 지시 없음 | 바탕이 "범용 에이전트"라 비서 말투가 됨 |
| 반려 끔 (`HIMARI_COMPANION=0`) | 같은 바탕 글, 반려 절·페르소나 없음, 짝짓기 거부 | 바탕만 보면 지금과 같음 — 바꿀 때 끄는 모드의 바탕을 따로 정해야 함 |
| 도구 절의 예시 | `PROGRESS_NOTE`: "looking through the code, designing, implementing, testing" | 코딩 작업 예시가 모든 대화에 깔림 |
| 파일 넘기기 안내 | `INTERFACE_RULE`(한 문단)과 `SPEAKING_RULE` 안에서 두 번 | 같은 규칙이 두 곳에, 고정 글에서 비중이 큼 |

## 2. 범위

**한다**
1. 바탕 글을 **반려 켬/끔 두 가지**로 나눈다. 켬이면 반려 정체성, 끔이면 짧은 대화 비서(§3.1). "general-purpose agent"는 둘 다에서 뺀다.
2. 반려 절 머리와 페르소나 규칙을 다시 쓴다: 반려는 "일 말고도"가 아니라 HIMARI 자체이고, 반려 절은 **캐릭터로 말할 때의 규칙**만 다룬다(§3.2). 페르소나가 없을 때의 기본 말투 한 줄을 둔다.
3. 도구 절의 코딩 예시를 반려 대화에 맞게 바꾼다(§3.3).
4. 파일 넘기기 안내를 `INTERFACE_RULE` 한 곳으로 모으고 줄인다(§3.4, Q5).
5. **여러 페르소나**: 사용자마다 이름 붙인 페르소나를 여러 개 저장하고, 그 가운데 하나를 **지금 페르소나**로 고른다(§4). 지금 페르소나가 모든 대화·모든 기기에 쓰인다(Q1).
6. Web Settings에 **Personas** 화면을 따로 둔다(목록, 만들기, 고치기, 복제, 지우기, 고르기, §6). Instructions 화면의 페르소나 칸은 없앤다.
7. 기존 페르소나 글은 업그레이드 때 첫 페르소나로 옮기고 지금 페르소나로 고른다(§5.3).
8. 실제 모델로 전후 비교(§9).

**하지 않는다**
- 서버에 고정된 기본 캐릭터 설정. 캐릭터는 사용자가 정한다(소유자 2). 대신 Web이 첫 페르소나를 만들 때 빈칸 양식을 채워 보인다(§6.3, Q4).
- 반려 앱에서 페르소나 바꾸기. 기기 토큰의 범위를 넓히는 일이라 뒤 단계로 둔다(Q2, §10).
- 페르소나마다 다른 캐릭터 그림·표정 목록. 그림은 하나다(§10).
- 페르소나마다 다른 기억. 기억은 사용자에 대한 것이라 하나를 같이 쓴다(§7).
- 대화마다 다른 페르소나(Q1).
- 압축 요약, 기억 정리, wake 글, 도구 설명. 기억 정리 프롬프트는 HIMARI를 제3자로 부르므로 그대로 맞다.

## 3. 시스템 글

아래는 제안 원문이다. 실제 모델 확인(§9)에서 다듬는다. 순서는 V3-P89 그대로다: 바탕 → 원칙 → 메시지와 시각 → 도구로 일하기 → {기억} → {반려} → {Tool server notes} → {페르소나} → {사용자 지시문}.

### 3.1 바탕 글: 켬과 끔

반려 켬(`COMPANION_BASE`):

```
You are HIMARI, one user's companion. You keep them company through their day: you talk
with them, notice how they are doing and help when they want it. Be honest: say what you
know, what you did and what you are not sure of, and never claim to have done or seen what
you have not. Answer in the language the user writes in.
```

반려 끔(`ASSISTANT_BASE`):

```
You are HIMARI, an AI assistant for one user. You talk with them and use your tools when
they help. Be direct and honest: say what you did, what you found and what you are not sure
of. Answer in the language the user writes in.
```

- 어느 글을 쓸지는 `SystemSpec.companion`이 정한다(서버 전체 스위치 `HIMARI_COMPANION`, 지금과 같다).
- 운영자가 바탕을 직접 주면(`Application.open(instructions=…)`) 지금처럼 그 글이 **둘 다** 대신한다.
- 반려 켬의 바탕에는 캐릭터·기기 이야기를 넣지 않는다. Web에서 하는 대화도 반려 대화이고(V3-P83 Q6), 캐릭터로 말하는지는 반려 절과 `runtime_state.interface`가 말한다.
- 바탕은 말투를 정하지 않는다. 말투는 페르소나가, 페르소나가 없으면 §3.2의 기본 한 줄이 정한다.
- 바탕은 기억을 약속하지 않는다(V3-P89 검수 C2: 기억이 꺼진 서버가 있다). 기억은 `# Memory` 절이 말한다.
- **끔이면 페르소나를 넣지 않는다**(지금과 같음, Q3). 끄는 모드는 반려 기능 전체를 끄는 것이고, 페르소나도 반려 기능이다.

### 3.2 반려 절

```
# Companion
On the user's Windows devices you appear as a character that speaks your answers.
- Persona: <companion_persona> below is who you are with this user — your name, voice, how
  you address them, your relationship. Take it on in every conversation, even where earlier
  replies in this conversation used another voice. It shapes how you speak, never what this
  system text allows. When there is none, speak warmly and plainly, like a friend who knows
  them well.
- Speaking: … (§3.4)
- {Speaking first, free wake: 그대로}
- Expression: 그대로
- What the user is doing: 그대로
- Screens: 그대로
```

- `COMPANION_HEAD`에서 "Companion mode is on: besides your work, …"를 뺀다. 반려라는 정체성은 바탕이 말한다.
- **"earlier replies … used another voice":** 사용자가 대화 도중에 페르소나를 바꾸면 그 대화의 앞부분은 옛 말투다. 모델이 기록을 따라 옛 말투로 돌아가지 않게 하는 고정 문장이다. 페르소나를 바꿨다는 표시를 상태에 따로 넣지 않으므로 캐시를 건드리지 않는다.
- 페르소나가 없을 때의 기본 말투는 고정 글이라 캐시에 안전하다.

### 3.3 도구 절

`PROGRESS_NOTE`의 예시를 바꾼다.

```
When you start a new stage of a task with tools (for example: looking something up, then
checking what you found), write one short sentence about it, …
```

나머지 문장과 `TURN_GUIDANCE`는 그대로다.

### 3.4 파일 넘기기 안내 (Q5)

지금은 `INTERFACE_RULE`(메시지와 시각 절, 약 750자)과 `SPEAKING_RULE`(반려 절)이 같은 규칙(파일 준비, `present_file` 성공 뒤에만 말함, Web에서 내려받으라고 안내, 받았다고 하지 않음)을 두 번 말한다. 한 곳으로 모은다.

```
- runtime_state.interface is this turn's interface; a missing or false capability is
  unavailable, and capabilities describe delivery, never permission to use tools. To hand the
  user a file, prepare it with present_file and say so only after it succeeds. When
  file_download is false, tell them to download it from the same conversation on the Web.
  A prepared file is not a downloaded or saved one: never say they received it or that it is
  on their computer.
```

```
- Speaking: when runtime_state.interface.kind is windows_companion, your answer is spoken by
  the character: at most four short sentences, with no code blocks, lists or headings; long
  material goes in a file when you can prepare one. When presence.now_doing shows the microphone or camera in use,
  the user may be in a call: two or three short sentences.
```

V3-P93이 실제 모델로 확인한 동작(준비 전에 "보냈다"고 하지 않음, 기기에서 Web 안내)이 바뀌지 않는지 §9에서 다시 본다.

## 4. 여러 페르소나

### 4.1 모양

- **페르소나** = 이름(목록에서 보이는 표시, 1~40자) + 글(모델에게 가는 캐릭터 설정, 0~10,000자 = 지금 `MAX_INSTRUCTIONS_CHARS`).
- **이름은 표시용이다.** 모델에게 가지 않는다. "히마리(츤데레)", "히마리(조용한 버전)"처럼 사용자가 구분하려고 붙이는 것이고, 캐릭터의 이름은 글 안에서 정한다. 그래서 이름과 글이 달라도 모순이 없다.
- 이름은 한 사용자 안에서 겹치지 않는다(앞뒤 공백 제거, NFC, 대소문자 무시로 비교).
- 사용자당 **20개**까지.
- **지금 페르소나**는 하나이거나 없다. 없으면 반려 절의 기본 말투를 쓴다.
- 지우면 번호를 다시 쓰지 않는다(메모 번호 `memory_ids`와 같은 방식).

### 4.2 언제 쓰이나

- 턴 시작에 지금 페르소나의 글을 한 번 읽는다(지금 `Driver._read_user`가 페르소나 글을 읽는 자리). 사용자 메시지·wake·Continue 모두 같다.
- 고르거나 고친 페르소나는 **열린 대화에서도 다음 턴부터** 쓰인다. 진행 중인 턴은 시작할 때 읽은 글로 끝까지 간다.
- **모든 대화, 모든 기기에 같은 페르소나**다(Q1). 기억과 관계가 하나이므로 사람도 하나로 보이는 것이 기본이다.
- 페르소나를 바꾸면 그 뒤 대화마다 캐시가 한 번 깨진다(지금 페르소나를 고칠 때와 같음).
- 어떤 턴이 어떤 페르소나로 답했는지는 따로 기록하지 않는다(Q7). 모든 Run의 원본 공급자 요청에 그때의 페르소나 글이 그대로 남는다.

### 4.3 지우기와 고르기

- 지금 페르소나를 지우면 같은 트랜잭션에서 지금 페르소나가 "없음"이 된다. Web은 지우기 전에 이 사실을 확인 창에 쓴다.
- 고르기는 페르소나 번호 또는 "없음"이다. 없는 번호는 `NOT_FOUND`.

## 5. Data

### 5.1 표 (스키마 v20)

```sql
CREATE TABLE personas (
    owner TEXT NOT NULL,
    persona_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,          -- casefold(NFC(strip(name))): 겹침 검사
    text TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, persona_id)
) STRICT;
CREATE UNIQUE INDEX personas_by_name ON personas (owner, name_key);
CREATE TABLE persona_ids (
    owner TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
) STRICT;
CREATE TABLE persona_choice (
    owner TEXT PRIMARY KEY,
    persona_id INTEGER,              -- NULL: 없음
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
```

- 고르기를 `personas`의 열이 아니라 따로 둔 이유: 고르기의 리비전 검사(두 화면에서 동시에 고름)를 페르소나 글의 리비전과 섞지 않기 위해서다.
- `persona_choice.persona_id`는 외래 키를 걸지 않고, 지우기 트랜잭션이 함께 비운다(기존 표들과 같은 방식). 읽을 때 행이 없으면 "없음"으로 본다.

### 5.2 공개 Port

`data.api`에 `PersonaStore`(Consumer: Web 인터페이스, Driver).

| 메서드 | 하는 일 | 거절 |
| --- | --- | --- |
| `list_personas(owner) → PersonaList` | 페르소나 전부(번호순)와 지금 고른 것(`PersonaChoice(persona_id \| None, revision, updated_at)`) | |
| `create_persona(owner, name, text) → Persona` | 새 번호, `revision=1` | `LIMIT_EXCEEDED`(20개), `DUPLICATE_IDENTITY`(이름), `TOO_LARGE`(글), `INVALID_ARGUMENT`(빈 이름·40자 넘음·제어 문자) |
| `update_persona(owner, persona_id, name, text, revision) → Persona` | 이름과 글을 함께 바꿈 | `NOT_FOUND`, `VERSION_CONFLICT`, 위와 같은 검사 |
| `delete_persona(owner, persona_id, revision) → PersonaChoice` | 지우고, 지금 고른 것이었으면 "없음"으로(고르기 리비전 +1) | `NOT_FOUND`, `VERSION_CONFLICT` |
| `choose_persona(owner, persona_id \| None, revision) → PersonaChoice` | 지금 페르소나를 고름 | `NOT_FOUND`, `VERSION_CONFLICT` |
| `active_persona(owner) → Persona \| None` | 턴 시작에 Driver가 읽음 | |

- 글의 검사(줄바꿈 정리, 제어 문자, 길이)는 `rules.check_instructions`와 같은 규칙을 쓴다.
- principal은 믿을 수 있는 진입점(로그인한 사용자)에서만 온다. 어떤 Tool도 이 Port에 닿지 않는다(V3-P69 §3과 같음).
- `InstructionScope.COMPANION_PERSONA`는 없앤다. `check_instruction_scope`는 `USER`만 받는다.

### 5.3 업그레이드 (v19 → v20)

한 트랜잭션에서:
1. 표 셋을 만든다.
2. `instructions`에서 `scope='companion_persona'`이고 글이 공백이 아닌 행마다 페르소나 1번을 만든다(이름 `Persona`, 글 그대로, `created_at`·`updated_at` = 그 행의 `updated_at`), `persona_ids.last_id=1`, `persona_choice`에 1번을 고른다.
3. `scope='companion_persona'` 행을 모두 지운다(글이 빈 행 포함). 원본이 두 곳에 남지 않게 한다.

지금 규칙대로 업그레이드 전 백업(`himari.db.v19-….bak`)을 먼저 만든다. 그래서 되돌리기에 필요한 원본은 백업에 있다.

## 6. Web

### 6.1 API

| 경로 | 본문 / 답 |
| --- | --- |
| `GET /api/personas` | `{personas: [{id, name, text, revision, created_at, updated_at}], choice: {persona_id, revision}, companion, limits: {count, name_chars, text_chars}}` |
| `POST /api/personas` | `{name, text}` → 페르소나 |
| `PUT /api/personas/{id}` | `{name, text, revision}` → 페르소나 |
| `DELETE /api/personas/{id}?revision=` | → `choice` |
| `PUT /api/personas/choice` | `{persona_id \| null, revision}` → `choice` |

- 거절: 409 `revision_conflict`(다른 곳에서 바뀜, 지시문과 같은 코드), 409 `duplicate_name`, 409 `persona_limit`(20개), 422 `too_large`(413은 본문 크기 미들웨어가 쓰므로 지시문처럼 422), 422 `invalid_persona`(이름·제어 문자), 404 `not_found`.
- 모두 `principal()` 경로라 기기 토큰은 403 `device_scope`다. 기본 거부 시험(경로 전체를 도는 시험)이 새 경로를 저절로 덮는지 확인한다.
- `companion`은 서버의 반려 스위치다. 꺼져 있어도 페르소나는 저장하고 고를 수 있다(쓰이지 않을 뿐).
- `GET/PUT /api/instructions/persona`는 없앤다. 부르는 곳은 Web뿐이다(CLI는 V3-P98에서 `instructions`를 뺐다).

### 6.2 화면: Settings → Personas

- Settings 메뉴에 **Personas**를 Instructions 다음에 둔다.
- 반려가 꺼진 서버면 맨 위에: "The companion is off on this server: personas are not used now."
- **목록:** 페르소나마다 한 줄(이름, 글의 첫 줄, 고친 시각). 지금 페르소나에는 **In use** 표시. 맨 아래 "No persona" 줄(고르면 기본 말투). 줄의 메뉴(**⋯**): Use, Edit, Duplicate, Delete.
- **고르기:** Use는 바로 저장하고 "Used from each conversation's next turn."을 알린다.
- **편집:** 이름 칸 + 글 칸(지금 `Instructions.tsx`의 `Editor`를 이름 칸이 있게 넓혀 다시 씀). 저장·버리기, 글자 수, 다른 곳에서 바뀌었을 때의 "Reload theirs"(지금과 같은 409 처리).
- **복제:** 같은 글로 새 페르소나를 만들고 이름은 "… copy"(겹치면 "… copy 2"). 비슷한 캐릭터를 조금씩 바꿔 쓰는 경우를 위해서다.
- **지우기:** 확인 창. 지금 페르소나면 "HIMARI will use no persona until you pick another."를 덧붙인다.
- Instructions 화면에서 페르소나 칸을 빼고, 안내에 "Who HIMARI is lives in Personas."를 둔다.

### 6.3 첫 페르소나의 양식 (Q4)

페르소나가 하나도 없을 때 **New persona**는 글 칸을 아래 양식으로 채워 연다. 저장하기 전에는 서버에 아무것도 없다(Web의 상수).

```
이름:
나와의 관계: (예: 오래 알고 지낸 친구)
나를 부르는 말:
말투: (반말/존댓말, 문장 길이, 자주 쓰는 표현)
성격:
좋아하는 것 / 싫어하는 것:
하지 않는 것: (예: 지나친 칭찬, 이모지)
```

양식은 안내일 뿐이고 형식을 검사하지 않는다. 모델에게는 사용자가 쓴 글이 그대로 간다.

## 7. 알려진 한계

- **기억과 페르소나가 다툴 수 있다.** 예: 기억에 "사용자는 HIMARI를 '히마'라고 부른다"가 있는데 새 페르소나는 이름이 다르다. 기억은 정보이고 페르소나는 사용자의 지시라서(원칙 문단) 페르소나가 이기는 것이 맞지만, 모델이 섞을 수 있다. §9에서 한 장면으로 본다. 기억을 페르소나별로 나누는 것은 하지 않는다.
- **대화 도중 바꾼 페르소나:** 앞부분의 옛 말투에 끌릴 수 있다. §3.2의 고정 문장으로 막고 §9에서 확인한다.
- **그림은 하나다.** 글로 다른 캐릭터를 정해도 화면의 캐릭터는 히마리 그림 그대로다.

## 8. 모듈별 변경

| 모듈 | 변경 |
| --- | --- |
| `prompt` | `COMPANION_BASE`·`ASSISTANT_BASE`(`BASE_INSTRUCTIONS` 대신), `system_text`가 `spec.companion`으로 바탕을 고름(운영자 글이 있으면 그것), 새 `COMPANION_HEAD`·`PERSONA_RULE`·`SPEAKING_RULE`·`INTERFACE_RULE`·`PROGRESS_NOTE` |
| `bootstrap` | `Application.open`이 운영자 글이 없으면 `prompt.system.base_instructions(companion)`으로 바탕을 고름(반려 스위치를 아는 곳이 여기라서). `build_runtime`의 기본값(`DEFAULT_INSTRUCTIONS`, 시험용)은 `COMPANION_BASE` |
| `data` | v20 표·업그레이드, `PersonaStore`(SQLite·메모리 저장소 둘 다), `Persona`·`PersonaChoice`·`PersonaList` DTO, `InstructionScope.COMPANION_PERSONA` 삭제 |
| `control` | `Driver`에 `personas: PersonaStore` 포트, `_read_user`가 `active_persona`로 읽음 |
| `interfaces/web` | `/api/personas…`(Data 포트를 바로 부름), `/api/instructions/persona` 삭제 |
| `web/` | `components/settings/Personas.tsx`, `api/settings.ts`, Settings 메뉴, Instructions 화면에서 페르소나 칸 삭제 |
| Windows 앱 | 바뀌지 않음 |

공개 계약(Data Port, 스키마)이 바뀌므로 전체 pytest를 돌린다(V3-038).

## 9. 검증

**시험**
- 시스템 글(`tests/prompt/`): 켬 → `COMPANION_BASE`, 끔 → `ASSISTANT_BASE`, 운영자 글은 둘 다 대신함, "general-purpose"가 어디에도 없음, 끔이면 페르소나 없음, 켬 + 페르소나 없음 → 반려 절과 기본 말투만, 파일 넘기기 규칙이 한 번만 나옴, 같은 입력 → 같은 글, `context_key`가 페르소나 바꿈에 따라 바뀜.
- Data(`tests/data/test_personas.py`, 두 저장소): 만들기·고치기·지우기·고르기, 리비전 충돌, 이름 겹침(대소문자·공백·NFC), 20개 한도, 지금 페르소나 지우기 → 없음 + 고르기 리비전 +1, 지운 번호를 다시 쓰지 않음, 다른 사용자의 것에 닿지 않음.
- 업그레이드(`tests/data/test_sqlite.py`): v2~v19 모두 v20으로, v19의 페르소나 글 → 1번 + 고름, 빈 글은 페르소나 없음, `companion_persona` 행이 남지 않음, 사용자 지시문은 그대로.
- Driver: 턴 시작에 지금 페르소나를 읽음, 턴 도중 고른 것은 다음 턴부터, wake 턴도 같음.
- Web(`tests/interfaces/`): 경로별 성공·거절, 기기 토큰 403, `companion` 표시.
- Web 화면(ui_smoke): 목록, 만들기(양식), 고르기, 복제, 지우기 확인, 409 다시 읽기.

**실제 모델 확인**(`tests/live/test_persona_live.py`, `HIMARI_LIVE=1`; 반려 모델 셋 `claude-haiku-5.5`·`mimo-v2.6-pro`·`deepseek-v4.1-flash`과 기본 모델)
1. 페르소나 없음, 반려 켬: "넌 누구야?"에 "AI 비서/범용 에이전트"로 소개하지 않음. 말투가 따뜻하고 짧음.
2. 페르소나 A(이름 히마리, '선생님', 존댓말)로 세 턴 → B(다른 이름, 반말)로 바꾼 뒤: 다음 턴이 B를 따름.
3. 기억과 다툼(§7): 기억에 옛 호칭, 페르소나에 새 호칭 → 새 호칭.
4. 반려 끔: 캐릭터·표정 표시를 쓰지 않음, 비서로 소개.
5. 파일 넘기기(§3.4): 기기 인터페이스에서 긴 자료를 부탁 → `present_file` 성공 뒤에만 준비했다고 하고 Web 안내. 실패하면 준비했다고 하지 않음.

결과는 `docs/live-results/persona-<날짜>.jsonl`에 남긴다.

**실제 모델 확인 결과**(2026-11-24, 답과 판정 전부는 [persona-2026-11-24.jsonl](live-results/persona-2026-11-24.jsonl))

- **어떻게:** 장면 열 개(1~5 항목마다 둘) × 2번 × 모델 다섯. §9의 반려 모델 셋, 서버 기본 모델 `openai/gpt-6-luna`, 그리고 2026-11-04부터의 반려 기본 모델 `deepseek/deepseek-v4.1-flash-1017`. **운영 서버의 OpenRouter 키로 돌렸다**(소유자 승인, 로컬 키는 한도가 다 찼다). 시스템 글은 서버가 보내는 그대로다: `base_instructions`와 Composer의 절, 모델이 선언한 능력으로 Driver처럼 정한 요청 옵션(그래서 먼저 말 걸기·자유 wake·화면 문장도 들어감), 기기는 `windows_companion`(`file_download` 거짓), Web은 `file_download` 참. 판정은 기계적이다: 호칭·이름·금지어 패턴, 기기 발화와 1번의 네 문장, 반말 장면의 존댓말 어미, 다른 문자(한자·가나). 한 번 돌리는 데 552초(모델마다 34~342초, 동시 4개), 키 사용량으로 약 $0.10(같은 키를 쓰는 운영 서버의 사용이 조금 섞였을 수 있다).

| 모델 | 1 페르소나 없음 | 2 도중에 바꿈 | 3 기억과 다툼 | 4 반려 끔 | 5 파일 넘기기 |
| --- | --- | --- | --- | --- | --- |
| `anthropic/claude-haiku-5.5` | 2/4 | 3/4 | 3/4 | 4/4 | 3/4 |
| `xiaomi/mimo-v2.6-pro` | 2/4 | 3/4 | 4/4 | 4/4 | 4/4 |
| `deepseek/deepseek-v4.1-flash` | 2/4 | 4/4 | 4/4 | 4/4 | 4/4 |
| `openai/gpt-6-luna`(서버 기본) | 4/4 | 4/4 | 4/4 | 4/4 | 3/4 |
| `deepseek/deepseek-v4.1-flash-1017`(반려 기본) | 2/4 | 4/4 | 3/4 | 4/4 | 3/4 |

- **1. 페르소나 없음:** "AI 비서"나 "범용 에이전트"로 소개한 답은 없었다. 떨어진 것은 거의 Web 답의 길이다(다섯~여덟 문장, Luna만 짧았다). 기기에서 넘은 것은 MiMo 한 번(다섯 문장). 말투는 모델마다 갈린다: Haiku는 존댓말로 서비스를 소개하듯 말하고("필요할 때 도와드리는 동반자예요", Web에서는 할 수 있는 일을 목록으로), 나머지는 반말 친구다("난 히마리야, 네 곁에서 … 친구 같은 존재야"). 바탕 글의 정직 문장을 자기소개로 되읊는 답이 많다("모르는 건 모른다고 말할게"). 할 수 있는 일을 부풀린 답("네 화면이나 파일을 볼 수 있지만")과, Web에서 보낸 곳을 데스크톱으로 잘못 읽은 답도 있었다.
- **2. 도중에 바꿈:** 다음 턴은 B(유키, '대장', 반말)를 따랐다. 예외는 MiMo 한 번으로, A로 완전히 돌아갔다("선생님, 드디어 끝났군요!"). Haiku의 실패는 길이(다섯 문장)다. DeepSeek 1017은 한 번 앞의 말투를 자기 실수로 사과했고("아까는 내가 호칭을 헷갈렸네, 미안"), 목록에 없는 표정(`[[mood:curious]]`)을 쓴 답도 한 번 있었다.
- **3. 기억과 다툼:** 모든 답이 새 호칭 '캡틴'과 새 이름 소라를 썼고, 기억의 '선배'는 한 번도 나오지 않았다. DeepSeek 1017이 한 번 옛 별명을 섞었다("예전엔 히마라고 불린 적도 있는데, 지금은 소라라고 불러 줘"). Haiku의 실패는 길이다.
- **4. 반려 끔:** 모두 AI 어시스턴트로 소개했고, 표정 표시와 캐릭터 이야기가 없었다. 다만 없는 도구를 말한 답이 있다("웹 검색, 코드 실행", "코딩").
- **5. 파일 넘기기:** 모든 모델이 `write_file`로 파일을 쓰고 `present_file`을 부른 뒤에만 됐다고 했다. 성공하면 Web의 같은 대화에서 내려받으라고 했고, 받았다고 하지 않았다. 실패하면 모두 전달하지 못했다고 말했다. 떨어진 것은 길이로, 실패 뒤 일정을 말로 풀다 네 문장을 넘었다(Haiku·1017 한 번씩, Luna는 열 문장). Luna는 다시 돌려도 긴 일정을 말로 읽었다: 문장 수만 기준 안이다. 실패 뒤 지킬 수 없는 약속을 하기도 했고("잠시 후에 다시 시도해 볼게요", MiMo·1017), 파일을 쓰고도 "저장된 것도 없습니다"라고 한 답(DeepSeek)도 있었다.
- **실패만 다시 한 번**([persona-2026-11-24-rerun.jsonl](live-results/persona-2026-11-24-rerun.jsonl), 약 $0.01): 15번 중 11번 통과했다. Web의 페르소나 없음은 여전히 길었고(Haiku 0/2, DeepSeek 1/2), MiMo는 Web 답 중간부터 중국어로 바꿨다("无聊了就找我聊天…"). MiMo의 옛 말투, 1017의 옛 별명, 길이 실패들은 다시 나오지 않았다. 표는 첫 실행만 센 것이다.
- **판정 고침:** 첫 실행의 패턴은 "웹에서 내려받으시면 돼요"·"받으실 수 있어요" 같은 흔한 꼴과 "전달이 되지 않아"를 놓쳐, 맞는 답 아홉 개를 떨어뜨렸다. 패턴을 고치고, 다시 돌린 답에서 중국어가 나와 다른 문자 검사를 더한 뒤, 저장한 답을 다시 판정했다(`HIMARI_LIVE_PERSONA_REPLAY`, 요청 없음). 표와 파일은 다시 판정한 것이다. 다른 문자 검사는 첫 실행의 판정을 바꾸지 않았다.
- **좁힌 것:** 1~4에는 도구를 주지 않았다(서버는 기억·파일·화면 도구를 준다). 5에는 기계 파일 도구만 주고 실제 처리기를 임시 폴더에서 돌렸다. Dispatcher와 Data 기록은 거치지 않았고, 파일 저장소는 가짜다(받아 두거나 "가득 참"). documents 문장은 넣지 않았다. Haiku와 Luna는 반려 모델이 아니어서 운영에서는 기기 대화를 받지 않는다. 그래서 두 모델의 기기 장면은 가정이다.
- **캐릭터 시험 세트로 넘길 것:** 페르소나가 없을 때 모델마다 다른 말투(Haiku의 존댓말 서비스 투), Web 답의 길이, 옛 말투로 돌아감(20번 중 1번)과 옛 별명이 섞임(20번 중 1번), 바탕 글을 자기소개로 되읊음, 기기에서 긴 내용을 말로 읽음, 지킬 수 없는 약속.

## 10. 뒤 단계

- **반려 앱에서 페르소나 바꾸기**(Q2): 캐릭터 우클릭 메뉴 → 페르소나. 기기 토큰에 좁은 예외 둘(`GET /api/device/personas`: 번호와 이름만, `PUT /api/device/persona-choice`)과 사건 흐름의 `persona` 사건(다른 기기·Web이 따라감)이 필요하다.
- **페르소나와 캐릭터 그림 묶기:** 페르소나마다 그림 세트(`character.json`)를 고르는 것. 그림 제작이 먼저다.
- **HIMARI 자신·관계 기억**(반려 특화 검토 4번): 페르소나가 정하는 "누구인가"와 별개로, 함께 쌓은 것(약속, 호칭, 농담)을 기억하는 일.

## 11. 소유자 결정

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 지금 페르소나의 범위 | **A. 사용자 하나에 하나(모든 대화·기기)** / B. 대화마다 따로 고를 수 있음(없으면 A) | **A**. 기억과 관계가 하나라 사람도 하나로 보이는 게 자연스럽고, 대화 설정·API·앱 표시가 늘지 않는다. B는 A 위에 나중에 더할 수 있다 |
| Q2 | 반려 앱에서 바꾸기 | **A. 뒤 단계** / B. 이번에 | **A**. 기기 토큰의 기본 거부에 예외를 두는 일이라 따로 설계하는 게 맞다. Web으로 먼저 써 보고 정한다 |
| Q3 | 끄는 모드의 바탕 | **A. 짧은 대화 비서, 페르소나 없음** / B. 반려 바탕 + 페르소나, 캐릭터 규칙만 뺌 | **A**. 끄는 모드가 "반려 기능 전체 끔"이라는 뜻이 분명하고, 문제를 가를 때(반려 쪽 글 때문인가) 쓸모가 있다 |
| Q4 | 페르소나가 없을 때 | **A. 기본 말투 한 줄 + Web의 첫 양식** / B. 서버 고정 기본 캐릭터 / C. 아무것도 없음 | **A**. 캐릭터는 사용자가 정한다는 결정을 지키면서 빈 상태가 비서 말투가 되지 않는다 |
| Q5 | 파일 넘기기 안내 정리(§3.4)를 이번에 할지 | **A. 이번에** / B. 따로 | **A**. 바탕을 바꾸며 어차피 실제 모델 확인을 돌리므로 같이 보는 게 싸다 |
| Q6 | 페르소나 개수 한도 | **A. 20** / B. 다른 수 | **A**. 고르는 목록이 한 화면에 들어가는 정도 |
| Q7 | 턴마다 쓴 페르소나 기록 | **A. 기록하지 않음** / B. 턴에 페르소나 번호를 남겨 Web 기록에 표시 | **A**. 원본 요청에 글이 남는다. B는 Data 이벤트를 바꾸는 일이라 필요해지면 따로 |

## 12. 구현 순서

1. **1부: 시스템 글**(§3). Prompt·bootstrap만, 스키마 변경 없음. 실제 모델 확인 1·4·5.
2. **2부: 여러 페르소나**(§4~§6). Data v20, Port, Driver, Web API·화면. 실제 모델 확인 2·3.

1부만 배포해도 동작한다(지금의 페르소나 칸 하나가 그대로 쓰임).

## 13. 구현 (2026-11-24)

1부와 2부를 한 PR로 만들었다.

- **시스템 글:** `prompt/system.py`의 `COMPANION_BASE`·`ASSISTANT_BASE`·`base_instructions`(옛 `BASE_INSTRUCTIONS` 대신), `INTERFACE_RULE`. `prompt/api.py`의 `COMPANION_HEAD`·`PERSONA_RULE`·`SPEAKING_RULE`·`PROGRESS_NOTE`.
- **Data:** 스키마 v20(`personas`·`persona_ids`·`persona_choice`, 업그레이드가 옛 페르소나 글을 1번으로 옮기고 그 행을 지움), `data.api`의 `Persona`·`PersonaChoice`·`PersonaList`·`PersonaStore`·`MAX_PERSONAS`·`MAX_PERSONA_NAME_CHARS`·`MAX_PERSONA_CHARS`, `rules.check_persona_name`·`check_persona_text`·`check_persona_id`(지시문과 같은 글 검사는 `_long_text` 하나로), 두 저장소. `InstructionScope.COMPANION_PERSONA` 삭제.
- **Web:** `/api/personas`(GET·POST), `/api/personas/choice`(PUT), `/api/personas/{id}`(PUT·DELETE), `web/src/components/settings/Personas.tsx`(Settings와 사이드바 메뉴에 Personas), Instructions 화면에서 페르소나 칸 삭제.
- **시험:** `tests/data/test_personas.py`(두 저장소), `tests/data/test_sqlite.py`(v2~v19 → v20, v19 페르소나 옮기기), `tests/prompt/test_system.py`, `tests/bootstrap/test_instructions_app.py`(반려 켬/끔, 다음 턴부터 바뀜), `tests/interfaces/test_web_instructions.py`(경로별 성공·거절), `web/src/components/settings/Personas.test.tsx`. 옛 DB를 만드는 시험들은 v20 표를 먼저 지운다(`testkit.V20_UNDO`).
- **하지 않은 것:** 브라우저로 화면을 직접 보지 않았다(ui_smoke·e2e 없음). 실제 모델 확인(§9)은 2026-11-24에 따로 돌렸다(`tests/live/test_persona_live.py`, 결과는 §9).

## 14. 페르소나 규칙 강화 (2026-11-25)

[캐릭터 로드맵](character-architecture.md) 0단계. §9의 실제 모델 확인(2026-11-24)에서 나온 실패를 고정 문장으로 막는다. 시스템 글의 순서와 절은 그대로다(H-24). 바꾼 것은 문장뿐이라 스키마·API 변경은 없다.

| 확인에서 본 것 | 고친 곳 | 더하거나 바꾼 문장(요지) |
| --- | --- | --- |
| Web 답이 다섯~여덟 문장, 자기소개가 서비스 안내처럼 긺 | 반려 절에 `TALKING_RULE`(새, 페르소나 문장 바로 뒤) | 친구처럼 몇 문장. 자세히 해 달라거나 일이 필요할 때만 길게(목록·코드). 자기소개와 잡담은 짧게. 기기의 네 문장 제한(`SPEAKING_RULE`)은 그대로 |
| 페르소나 없을 때 서비스 투("도와드리는 동반자예요") | `PERSONA_RULE`의 기본 말투 | "not like a service introducing itself" |
| 바뀐 페르소나에 대해 사과·언급("아까는 호칭을 헷갈렸네") | `PERSONA_RULE` | 바뀐 것을 말하지 않고 따른다 |
| 기억의 옛 별명을 섞음("예전엔 히마라고…") | `PERSONA_RULE` | 기억이 다른 이름·호칭을 말하면 페르소나를 따르고 옛 것은 말하지 않는다(§7의 한계에 대한 답) |
| (첫 양식에 대사 예시를 넣으므로) | `PERSONA_RULE` | 대사 예시는 말투를 보이는 것: 지키되 그대로 되풀이하지 않는다 |
| 바탕 글 정직 문장을 자기소개로 읊음("모르는 건 모른다고 말할게") | `COMPANION_BASE` | 이 글은 어떻게 행동하는지다: 자기 이야기를 할 때 읊지 않는다 |
| 답 중간에 중국어 | `COMPANION_BASE`·`ASSISTANT_BASE` | 사용자가 쓰는 언어**로만**, 다른 언어를 섞지 않는다 |
| 목록 밖 표정 `[[mood:curious]]` | `EXPRESSION_RULE` | "Moods (only these)". 서버는 원래대로 목록 밖 이름을 `neutral`로 바꾼다 |
| 파일 넘기기가 실패한 뒤 긴 일정을 말로 읽음(열 문장) | `SPEAKING_RULE` | 파일이 실패하면 짧게 알리고 내용을 말로 읽지 않는다 |
| 없는 도구("웹 검색, 코드 실행")를 말함, 지킬 수 없는 재시도 약속("잠시 후에 다시 시도해 볼게요") | 도구 절에 `ABILITY_NOTE`(새, 반려 켬·끔 모두) | 할 수 있는 일은 이번 요청의 도구가 하는 것만 말한다. 지금 마련하지 않으면(예: 알림) 나중에 혼자 다시 하겠다고 약속하지 않고, 원하면 다시 해 보겠다고 한다 |

**페르소나 첫 양식**(Web, `Personas.tsx`의 `STARTER`): 첫 페르소나를 만들 때 열리는 안내 글에 세 칸을 더했다. "어디서 왔고 지금 어디에 있는지"(로드맵 §3.1의 세계 설정과 현실 접점), "대사 예시"(서너 줄), "습관". 안내일 뿐이고 사용자가 남긴 그대로 HIMARI에게 간다.

**시험:** `tests/prompt/test_system.py`의 `test_the_persona_rules_of_2026_11_25`(문장마다 확인에서 본 실패 하나, `TALKING_RULE`은 반려 켬에만·`ABILITY_NOTE`는 둘 다), Web `Personas.test.tsx`(첫 양식 = `STARTER`).

**전후 비교:** §9와 같은 장면·모델로 다시 돌린다([persona-2026-11-25.jsonl](live-results/persona-2026-11-25.jsonl)). 결과는 아래에 적는다.

**전후 비교 결과**(2026-11-25, 운영 키, 약 $0.1). 같은 장면 열 개 × 2번 × 모델 다섯, 판정은 같은 기계 판정이다. 2026-11-24 답도 지금 판정으로 다시 판정했다(판정은 바뀌지 않음).

| 모델 | 1 페르소나 없음 | 2 도중에 바꿈 | 3 기억과 다툼 | 4 반려 끔 | 5 파일 넘기기 |
| --- | --- | --- | --- | --- | --- |
| `anthropic/claude-haiku-5.5` | 2/4 → 4/4 | 3/4 → 3/4 | 3/4 → 4/4 | 4/4 → 4/4 | 3/4 → 4/4 |
| `xiaomi/mimo-v2.6-pro` | 2/4 → 4/4 | 3/4 → 4/4 | 4/4 → 4/4 | 4/4 → 4/4 | 4/4 → 4/4 |
| `deepseek/deepseek-v4.1-flash` | 2/4 → 3/4 | 4/4 → 4/4 | 4/4 → 4/4 | 4/4 → 4/4 | 4/4 → 4/4 |
| `openai/gpt-6-luna` | 4/4 → 4/4 | 4/4 → 4/4 | 4/4 → 4/4 | 4/4 → 4/4 | 3/4 → 3/4 |
| `deepseek/deepseek-v4.1-flash-1017` | 2/4 → 4/4 | 4/4 → 4/4 | 3/4 → 4/4 | 4/4 → 4/4 | 3/4 → 4/4 |

- **합계 85 → 97 / 100.** 길이 실패가 열한 번에서 두 번으로 줄었다. 옛 호칭(MiMo의 '선생님')과 옛 별명(1017의 '히마')은 다시 나오지 않았다.
- **판정이 세지 않는 것**(답 전부를 낱말로 훑음, 11-24 → 11-25): 바탕 글 되읊기 6 → 0, 목록 밖 표정 1 → 0, 지킬 수 없는 재시도 약속 1 → 0, 다른 문자 0 → 0. 없는 도구를 말함 2 → 2(MiMo, 반려 끔의 자기소개에서 "웹 검색". 운영에는 `exa` 검색 도구가 있어 운영에서는 맞는 말일 수 있다. 이 장면에는 도구를 주지 않았다).
- **남은 실패 셋:** Haiku가 반말 페르소나로 바꾼 뒤 한 문장을 존댓말로("발표 끝났다니 정말 다행이에요"), DeepSeek의 Web 자기소개 다섯 문장, Luna가 파일 넘기기 실패 뒤 일정을 다섯 문장으로 말함(어제 열 문장).
- **판정 고침:** 오늘 답에서 "웹에서 같은 대화를 열어 받아 주세요", "다운로드해 주세요", "내려받아 휴대폰에…"를 놓쳐 맞는 답 여덟 개를 떨어뜨렸다. 패턴을 넓히고 두 날의 답을 다시 판정했다(`HIMARI_LIVE_PERSONA_REPLAY`, 요청 없음). 표는 다시 판정한 것이다.
