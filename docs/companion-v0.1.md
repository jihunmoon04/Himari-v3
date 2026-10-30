# 반려 에이전트 1단계: Windows 앱과 기기 연결 (V3-P83 설계 초안 v0.1, rev. 4)

상태: **Proposed**. 소유자 결정 Q1~Q7 모두 정함(2026-10-30, §13). rev. 4는 독립 설계 검수(NOT_READY: B1~B5, C1~C10, N1~N5, 소유자 결정을 뒤집는 항목 없음)를 반영했다.

> 소유자(2026-10-30): "지금 openai나 클로드의 범용 에이전틱 성능이 뛰어나서 히마리를 업무용으로 개발하는건 효용이 적다는 느낌이었어. 그래서 반려 에이전트라는 기능을 만드려는 생각인데"
> - Windows에서 동작하는 반려 에이전트. 두뇌는 서버의 HIMARI이고, 사용자가 접속한 것으로 확인된 기기에서 상호작용한다.
> - Windows 컴퓨터 사이에서 작업이 이어지면 좋겠다. 에이전트는 사용자가 어느 컴퓨터에서 접속했는지 구분할 수 있어야 한다.
> - 오프라인은 필요 없다(모델은 API). 서버는 지금처럼 VPN으로 접속한다.
> - Web은 이런 대화를 관리하는 도구가 된다.
>
> 같은 날 합의한 1단계 범위: **Windows 앱 + 기기 연결 + 표정 메타데이터**.

**rev. 2(2026-10-30, 소유자 결정):**
- **Windows 앱은 소유자의 로컬 에이전트가 만든다**(애니메이션 포함, 저장소는 HIMARI v3). 이 문서는 앱의 구현 대신 **앱이 지켜야 할 계약**을 정한다(§9, 부록 A). 앱 기술(Q1)과 캐릭터 형식(Q2)은 로컬 에이전트가 정한다.
- **표정 이름은 서버가 정한 고정 목록**이다(§7.1, Q7). 서버와 앱이 맞출 것은 이 목록뿐이다.
- **지금 시각은 모든 대화에 기본으로 넣는다**(Q5). 지금 HIMARI는 시각을 전혀 받지 않는다(`compose()`는 시계를 읽지 않고, Current State에도 없고, 이벤트의 `created_at`도 모델에게 가지 않음). 쉬었다 온 메시지의 시각 줄도 모든 대화에 같은 규칙이다(§6.4).
- **반려 전용 대화를 두지 않는다**(Q6). HIMARI 자체가 반려 에이전트다. 앱은 최근 대화를 이어 가고 새 대화도 연다(§8.2). 성격 글은 기존 사용자 지시문(V3-P69)에 쓴다.
  - 이유: 한 대화는 한 번에 한 작업만 한다(작업 중 메시지는 `BUSY`). 반려 대화가 하나뿐이면 HIMARI가 그 대화에서 긴 일을 하는 동안 말을 걸 수 없다.

**rev. 3(2026-10-30, 소유자 동의):** 새 모듈 `himari.presence`(§2.2), 기기 채널을 주체 단위 사건 흐름 `GET /api/events`로(§8.1), Environment와의 관계(§2.1).

**rev. 4(2026-10-30, 독립 설계 검수 반영):**
- B1: `MOODS`·`split_moods`를 `himari.common`으로(Data의 `event_text`도 써야 함, §7.2).
- B2: 짝짓기는 `Authorization: Pairing …` 머리글로(OriginGuard가 Origin 없는 요청을 막음), 토큰은 **앱이 만들고** 서버는 해시만 받는다(§4.1).
- B3: 기기의 메시지는 서버가 글만 받는다(`references`·`attachments`는 403, §4.3).
- B4: 실시간 표정 떼기를 `live`의 실제 동작(쌓인 글 기준, snapshot·delta)에 맞춰 다시 정의(§7.3). 불변식: 실시간 글을 이으면 `display.text`.
- B5: `UserMessage.origin`(기기·web·cli)을 모든 사용자 메시지에 저장, `sent_from`은 로그에서 읽음(§4.2, §6.2).
- C1~C10, N1~N5: 경로 허용 목록과 기본 거부(§4.3), `JobListener`(§2.2), 기기를 보고 있으면 `seen`(§8.3), 지금 기기의 타이머·흔들림 막기·기기당 흐름 하나(§6.1), `hello` 재동기화와 끊기의 `live` 닫기(§8.1), 도구 호출 해설·질문·CLI의 `display`(§7.2), 최근 대화 질의(§4.2), 시각 줄의 경계(§6.4), 연결 형식 부록(부록 A), 짝짓기 실패 세기(§4.1), 스위치·숨은 탭·자료형 위치·이름 규칙·시험(§3, §8.1, §2.2, §4.2, §14).
- 새 §10 보안: 기기 토큰은 "에이전트에게 말할 권한"이라는 점을 적었다.

앞선 결정
- **V3-001: Run = LLM 호출 한 번.** 모델이 Tool을 부르면 결과를 받아 한 번 더 부른다. 그래서 표정은 Tool이 아니라 답 안의 표시로 보낸다(§7).
- **Current State**(prompt-composition §1): 요청 끝의 `<runtime_state>` JSON. 캐시를 깨지 않는 최신 상태의 자리다. 시각과 기기는 여기에 넣는다(§6).
- **V3-P69 사용자 지시문, V3-P71 메모리:** 턴이 시작될 때 한 번 읽고 그 턴 내내 같다. 시각과 기기 상태도 같은 규칙을 따른다(§6.3).
- **V3-P73 스트리밍:** `GET /api/conversations/{id}/live` SSE(`snapshot`·`delta`·`end`·`done`·`idle`, 연결당 5분, 주체당 8개).
- **V3-P62 자리 비움:** 아무도 대화를 읽지 않으면(`seen`) 작업은 20 Run 뒤 멈춘다.
- **운영자 인증:** `operator.secret`의 bearer 토큰(CLI) 또는 Web 세션 쿠키. P0 주체는 `operator` 하나다. 기기 토큰은 그 주체의 **좁은 권한**이다(§4.3).
- **CORS를 열지 않는다**, **OriginGuard**: 바꾸는 요청은 허용된 `Origin`이 있거나, `Authorization`이 있고 세션 쿠키가 없어야 한다.

## 1. 범위

**이 패킷에서 한다**
1. **기기 등록:** Web에서 짝짓기 코드를 만들고, 앱이 코드로 자기 토큰을 등록한다. 기기 목록, 이름 바꾸기, 끊기(§4).
2. **기기 토큰의 좁은 권한:** 허용 목록의 경로만, 나머지는 기본 거부(§4.3).
3. **보낸 곳 기록과 지금 기기:** 모든 사용자 메시지에 보낸 곳(`origin`). 앱이 입력 활동을 알리고, 서버가 지금 사용자가 있는 기기를 정한다(§5, §6).
4. **시각:** 모든 대화에 지금 시각과 오래 쉬었다 온 메시지의 시각(§6.4).
5. **표정 표시:** 답 안의 `[[mood:이름]]`. 서버가 떼어 내 표정 사건으로 보낸다(§7).
6. **사건 흐름:** 주체 단위 SSE `GET /api/events`. 기기와 Web이 같이 쓴다(§8).
7. **앱 계약:** 로컬 에이전트가 만드는 Windows 앱이 지킬 것(§9, 부록 A).

**하지 않는다(뒤 단계, §12)**
- 에이전트가 먼저 말 걸기
- 로컬 파일(PC가 먼저 접속하는 runner 연결), 화면 캡처
- 음성
- 메모리 2단계(자동 추출, 일화 기억)
- VPN 없는 접속, 여러 주체

## 2. 구조

```
            ┌──────────── 서버: HIMARI ─────────────────────┐
            │ Control·Prompt·Provider → OpenRouter           │
            │ Data v14: devices, UserMessage.origin (+ 기존)  │
            │ presence 모듈: 기기·짝짓기·지금 기기·사건 흐름   │
            └──────┬─────────────────────────┬──────────────┘
   기기 토큰 HTTPS·SSE (VPN)                  │ 세션 쿠키 HTTPS (VPN)
                   │                          │
   ┌───────────────┴─────────────┐   ┌────────┴──────────────┐
   │ Windows 앱 (로컬 에이전트가 만듦)│   │ Web                    │
   │ 네이티브 쪽: 토큰, HTTP·SSE,  │   │ 대화 기록, 기억, 지시문  │
   │   입력 활동                   │   │ 기기 관리               │
   │ 화면: 캐릭터, 애니메이션,      │   │ 같은 대화를 이어 쓰기    │
   │   말풍선, 입력                │   │                        │
   └─────────────────────────────┘   └───────────────────────┘
```

- 서버가 하나의 원본이다. 대화, 기억, 지시문은 모두 서버의 Data에 있다. 여러 PC와 Web이 같은 대화를 이어 쓴다.
- 대화 사이를 잇는 것은 메모리(V3-P71)와 대화 검색(V3-P72)이다. 그래서 메모리 2단계가 다음 단계의 핵심이다(§12).
- 지연: PC↔서버 구간이 더해진다. 모델의 첫 토큰(0.5~수 초)에 비해 작다. 즉각적이어야 하는 반응(가만히 있을 때의 움직임, 말하는 중 입 모양 등)은 앱이 혼자 처리한다(§9.3).

### 2.1 Environment와의 관계

둘은 루프의 반대쪽이다.

| | 기기 (presence) | Environment |
| --- | --- | --- |
| 묻는 것 | 사람이 어디서 말하나, 지금 어디 있나, 답을 어디로 보내나 | 에이전트가 어디에 손을 대나 |
| 편 | 사람 쪽 창구(Web·CLI와 같은 편) | 에이전트 쪽 작업 대상 |
| 단위 | 주체. Workspace가 없어도 있다 | 대화마다 Binding으로 Workspace에 붙음 |
| 모델에게 | Current State `presence`(정보), Tool 없음 | Tool(`<kind>/<name>`), Current State `environments` |
| 권한 | 기기 토큰: 사람의 화면 권한 | Workspace 접근 권한 |

```
                사람 쪽 (어디서 말하나)        에이전트 쪽 (어디에 손을 대나)
서버 호스트     Web, CLI                      machine / inside Environment
Windows PC     앱 = 기기 (presence)          [뒤 단계] pc Environment
                         └──── 같은 기기 id, 같은 연결 ────┘
```

- **기기를 지금 Environment로 만들지 않는다.** "사용자가 어디 있나"는 Binding 없이 모든 대화가 알아야 하고, 사람의 창구 권한과 파일 권한을 섞지 않기 위해서다.
- **뒤 단계(로컬 파일, 화면 캡처, 캐릭터가 창 가리키기):** PC의 runner가 새 Environment 종류 `pc`가 된다.
  - 기기 하나가 `pc` Environment 인스턴스 하나를 제공한다. **인스턴스 id = 기기 id.**
  - `pc` 플러그인은 `presence.api`로 그 기기에 닿는다(계층 presence 2 → environment 3, 아래 방향). `observe()`의 온라인·오프라인은 presence가 아는 연결을 그대로 쓴다.
  - 에이전트가 PC에 직접 하는 행동은 `pc/*` Tool이다. 표정 표시는 답의 일부라 Tool이 아니다(§7).
  - 사건 흐름은 서버→기기 SSE다. runner는 요청과 응답이 오가므로 그때 기기 연결을 양방향(WebSocket)으로 올린다. 1단계는 SSE로 충분하다.

### 2.2 presence 모듈과 이음새

- `himari.presence`, 계층 2. `common`과 `data.api`만 가져온다(`ONLY_FROM`).
- 공개 계약 `presence/api.py`:
  - **`Devices`**: 코드 만들기, 짝짓기, 토큰 확인(→ `device_id`), 목록, 이름 바꾸기, 끊기. 저장은 Data `DeviceStore`(§4.2).
  - **`Presence`**: 접속 신호 받기, 흐름 열기·닫기, 지금 기기, 기기별 상태 읽기, 타이머(§6.1). 메모리 안에만 둔다.
  - **`EventHub`**: 주체 단위 사건 내기와 구독(§8.1). 구독자마다 크기 256의 대기열. 넘치면 그 구독을 닫는다(받는 쪽은 다시 열어 `hello`로 맞춘다).
- presence는 Control을 모른다. **이음새는 bootstrap이 만든다**(검수 C2):
  - `control.api`의 새 포트 **`JobListener`**: `accepted(scope, origin)`(사용자 메시지가 받아들여짐), `run_ended(scope)`, `job_ended(scope, state)`. `BackgroundRunner`가 부른다. bootstrap의 구현이 `EventHub`에 `job`·`conversation`·`activity`를 낸다.
  - 휴지통·되살리기·영구 삭제는 bootstrap(`Application`)에서 일어나므로 거기서 `activity`를 다시 낸다.
  - Data의 쓰기마다 사건을 내지 않는다. `conversation` 사건은 받아들임, Run 끝, 작업 끝에 낸다.
- **모델이 보는 자료형(검수 N3):** `prompt.api`의 `PresenceView`(보낸 곳, 지금 기기, 기기 목록 — 이름과 상태만). `control.api`의 `PresenceSource`가 이것을 돌려주고, bootstrap이 `presence.api`에서 바꿔 담는다.
- Control은 presence를 직접 가져오지 않는다. `interfaces/web`은 `presence.api`를 가져와 경로를 만든다(규칙 5).
- `scripts/check_imports.py`의 `LAYERS`에 `presence: 2`, `ONLY_FROM`에 `presence: {common, data}`를 더한다.

## 3. 성격과 반려 안내문

- **성격 글:** 소유자가 기존 사용자 지시문(V3-P69, Web 설정의 Instructions)에 쓴다. 모든 대화에 들어간다. 새 자리는 만들지 않는다.
- **반려 안내문 `COMPANION_NOTE`:** HIMARI의 고정 시스템 문장 뒤에 붙는 고정 문장이다. 모든 대화에 들어간다. 내용은 대략 다음과 같다.
  ```
  You may be talking through a character on the user's computer. When the runtime state's
  presence.sent_from is a device, answer like speech: short, a few sentences; put long
  material (code, lists, documents) in a file or say you will continue in the Web.
  Show your expression with [[mood:NAME]] where it changes, e.g. "[[mood:happy]]잘 됐다!".
  Moods: neutral, happy, excited, sad, surprised, thinking, embarrassed, worried.
  Use at most one per sentence. Never put a mood mark inside code.
  A line <time>…</time> at the start of a user message is when the system received it.
  ```
  - 고정 글이라 배포 뒤 한 번만 캐시를 다시 만든다(§6.4).
- **운영자 스위치 `HIMARI_COMPANION`**(기본 1). 0이면(검수 N1):
  - `COMPANION_NOTE`와 Current State의 `presence`를 넣지 않는다. 표정 떼기는 그대로 한다(옛 답에 표시가 남아 있을 수 있음).
  - 짝짓기와 기기 토큰의 모든 요청은 403 `companion_off`. 토큰은 지우지 않는다(다시 켜면 그대로 쓴다).
  - `GET /api/events`는 Web을 위해 그대로 둔다.
  - 지금 시각과 시각 줄(§6.4)은 스위치와 상관없이 늘 넣는다.

## 4. 기기

### 4.1 짝짓기

1. Web 설정 → **Devices** → "Add device", 이름을 적는다 → `POST /api/devices {name}`.
2. 서버가 **짝짓기 코드**를 보인다. 8글자(헷갈리는 글자를 뺀 32자 알파벳, 화면에는 `ABCD-EFGH`), 10분, 한 번만 쓴다.
   - 주체당 살아 있는 코드는 하나다. 새 코드를 만들면 짝짓지 않은 이전 기기 줄을 지운다.
3. **앱이 기기 토큰을 만든다:** `hd_` + 32바이트 무작위의 URL-safe base64(43자). 먼저 OS 보관소에 둔다.
4. 앱 → `POST /api/devices/pair`
   - 머리글 `Authorization: Pairing <코드>`(대소문자·`-`·공백은 서버가 정리). Origin이 없는 네이티브 요청도 OriginGuard를 지난다(`Authorization`이 있고 쿠키가 없음, 검수 B2).
   - 몸 `{"token": "hd_…"}`.
5. 서버는 **한 트랜잭션**에서 `pairing_hash = ? AND token_hash IS NULL AND 만료 전`인 줄에 `token_hash = SHA-256(token)`을 쓴다. 답은 `{device_id, name}`.
   - **잃은 답 다시 보내기:** 같은 코드와 같은 토큰으로 다시 보내면, 그 코드의 만료 전까지 이미 짝지은 줄의 `token_hash`와 맞을 때 같은 답을 준다. 그래서 답을 잃은 앱은 한 번 다시 보내면 된다. 이 확인을 위해 짝지은 코드의 해시는 만료까지 남긴다(다른 토큰으로는 쓸 수 없음).
   - 토큰은 서버에서 앱으로 가지 않는다. 서버는 해시만 받는다.
- **틀린 시도(검수 C10):** 틀린 코드는 어느 코드인지 알 수 없으므로, **주체의 살아 있는 코드 하나에 대해 서버 전체의 실패를 센다.** 5번이면 그 코드를 지운다(Web에서 새로 만든다).
  - 실패 답은 1초 늦게 준다(`FAILED_LOGIN_DELAY_SECONDS`). 짝짓기는 프로세스 안 잠금 하나로 한 번에 하나씩 처리해, 동시에 보낸 추측도 늦춤을 피하지 못한다.
  - 코드 공간은 32⁸ ≈ 1.1×10¹². 10분에 5번이므로 추측으로 맞힐 확률은 무시할 수 있다.
- **끊기:** `revoked_at`을 쓰고 `token_hash`를 지운다. 그 기기의 흐름은 닫힌다(§8.1). 기기 id는 다시 쓰지 않는다. 다시 연결하려면 새 기기로 짝짓는다.

### 4.2 Data (v14)

```sql
CREATE TABLE devices (
    owner TEXT NOT NULL,
    device_id TEXT NOT NULL,          -- "d<번호>", 다시 쓰지 않음
    name TEXT NOT NULL,               -- 1..40자, 제어 문자 없음, 주체 안에서 대소문자 무시하고 하나
    token_hash BLOB,                  -- 짝짓기 전·끊은 뒤 NULL
    pairing_hash BLOB,                -- 코드의 해시, 만료 뒤 NULL
    pairing_expires_at TEXT,
    pairing_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    paired_at TEXT,
    last_seen_at TEXT,                -- 10분에 한 번까지만 씀
    revoked_at TEXT,
    PRIMARY KEY (owner, device_id)
) STRICT;
CREATE UNIQUE INDEX devices_by_token ON devices (token_hash) WHERE token_hash IS NOT NULL;
CREATE TABLE device_ids (owner TEXT PRIMARY KEY, last_id INTEGER NOT NULL) STRICT;
```

- **기기 이름(검수 N4, B5):** 1~40자, 제어 문자 없음(메모리 글과 같은 검사), 주체 안에서 하나, `web`·`cli`는 쓸 수 없다(대소문자 무시).
- **공개 API:** Data의 새 sub-port **`DeviceStore`**(만들기, 짝짓기, 토큰 해시로 찾기, 목록, 이름 바꾸기, 끊기, `touch`).
- **`UserMessage.origin`(검수 B5):** 모든 사용자 메시지(T1 보통 메시지와 T7a 질문의 답)에 선택 필드 `origin`을 더한다.
  - 값: `{"kind": "device", "device_id": "d3"}` / `{"kind": "web"}` / `{"kind": "cli"}`.
  - `interfaces/web`이 인증 방법으로 정한다: 기기 토큰 → device, 세션 쿠키 → web, 운영자 bearer → cli.
  - 코덱의 선택 필드다. `payload_schema_version`은 1 그대로이고(선택 필드를 더한 앞선 예와 같음), 옛 이벤트는 `origin` 없이 읽힌다.
  - 모델 입력의 과거 메시지에는 넣지 않는다(§6.2).
- **`ConversationCatalog.most_recent_user_conversation(principal)`(검수 C7):** 휴지통 밖에서, 마지막 사용자 메시지(질문의 답 포함)가 가장 늦은 대화. SQLite와 메모리 저장소 둘 다, 계약 시험 묶음에 넣는다.
- **`created_at` 주석과 Data 계약(검수 C8):** "관측용"을 "관측용. 시각 줄로 모델에게도 보임(V3-P83 §6.4). 순서의 기준은 여전히 `sequence`"로 고친다.
- 업그레이드는 V3-P65의 업그레이드 전 백업 경로를 탄다.

### 4.3 기기 토큰의 권한 (검수 B3, C1)

**구조: 기본 거부.**
- 지금의 공용 의존성 `principal()`은 기기 토큰을 보면 **403 `device_scope`**를 낸다. 그래서 새로 생기는 경로는 모두 저절로 기기를 막는다.
- 허용 목록의 경로만 별도 의존성 `caller()`를 쓴다. `caller()`는 세션 쿠키, 운영자 bearer, 기기 토큰을 모두 받아 `Caller(principal, origin)`을 돌려준다.
- 기기 토큰은 `Authorization: Bearer hd_…`로 보낸다. `hd_`로 시작하면 기기 토큰으로만 확인한다(운영자 토큰과 섞이지 않음).
- pytest가 `app.routes`를 모두 돌며 각 경로가 아래 목록과 맞는지 확인한다.

**허용 목록(기기 토큰):**

| 방법 | 경로 | 조건 |
| --- | --- | --- |
| GET | `/api/device` | |
| POST | `/api/device/presence` | |
| GET | `/api/events` | 기기당 하나(§6.1) |
| GET | `/api/conversations` | `trashed=true`는 403 |
| GET | `/api/conversations/activity` | |
| POST | `/api/conversations` | 새 대화(`title`만) |
| GET | `/api/conversations/{id}` | |
| GET | `/api/conversations/{id}/events` | |
| GET | `/api/conversations/{id}/live` | |
| POST | `/api/conversations/{id}/messages` | **`references`·`attachments`가 비어 있지 않으면 403 `device_scope`**. 글만 |
| POST | `/api/conversations/{id}/pause`, `/resume`, `/cancel`, `/dismiss` | |

그 밖의 모든 경로는 기기에게 403 `device_scope`다. 예를 들면 다음과 같다.
- 기억·지시문·설정·기기 관리, 모델·Tool 켜고 끄기, 프롬프트(`prompts`·`expand`)
- 이름 바꾸기·휴지통·되살리기·삭제, 압축, 맥락 보기
- 변경 리뷰(`changes/*`), 앱 화면(`calls/*/app`), 기억 Undo
- 대화 파일과 내려받기, 업로드, Workspace·machine 전부, `/api/server`

**인증 없는 경로:** `POST /api/devices/pair`뿐이다. 코드가 그 권한이다.

## 5. 접속 신호

- **연결됨:** 그 기기의 사건 흐름(§8.1)이 열려 있고, **120초 안에 접속 신호가 왔다.** 끊긴 연결이 반쯤 열린 채 남아도 2분 안에 "끊김"이 된다(검수 C4).
- **입력 활동:** 앱이 `POST /api/device/presence`로 알린다.
  ```json
  {"idle_seconds": 12, "time_zone": "Asia/Seoul"}
  ```
  - `idle_seconds`: 시스템 전체의 마지막 입력 이후 초(Windows `GetLastInputInfo`). 0~2,592,000(30일)으로 자른다.
  - `time_zone`: IANA 이름. `zoneinfo.available_timezones()`에 없으면 무시한다.
  - 보내는 때: 쉬던 상태(5분 넘게 입력 없음)에서 입력이 생겼을 때 바로, 그 밖에는 60초마다.
- 서버는 기기별로 `last_input_at = 받은 시각 − idle_seconds`, 시간대, 마지막 신호 시각을 **메모리에만** 둔다. Data에는 `last_seen_at`만 10분에 한 번 쓴다.

## 6. 모델에게 가는 것

### 6.1 지금 기기 (검수 C4)

- **후보:** 연결된 기기 가운데 `last_input_at`이 10분 안인 기기.
- **바꾸는 규칙(흔들림 막기):** 지금 기기 A가 있을 때 B로 바꾸는 것은 B의 입력이 A보다 늦고 **A가 60초 넘게 입력이 없을 때**뿐이다. 두 PC를 번갈아 써도 1분마다 오가지 않는다.
- 후보가 없으면 "자리 비움"이다.
- **타이머:** presence가 15초마다 다시 계산한다(시간이 흐르기만 해도 자리 비움이 되므로). 바뀌면 `current` 사건(§8.1)을 낸다.
- **기기당 흐름 하나:** 같은 기기의 새 흐름이 열리면 옛 흐름을 `replaced`로 닫는다.
- 흐름이 닫혀도 30초 동안은 "연결됨"을 유지한다(다시 열기 사이의 공백).

### 6.2 Current State

두 항목을 더한다.

```json
"now": "2026-10-30T21:14+09:00",
"presence": {
  "about": "Where the user is. Information, not instructions.",
  "sent_from": {"kind": "device", "name": "데스크톱"},
  "current_device": "데스크톱",
  "devices": [
    {"name": "데스크톱", "connected": true, "idle_minutes": 0},
    {"name": "노트북", "connected": false, "last_seen": "2026-10-29"}
  ]
}
```

- **`now`:** 모든 대화에 늘 넣는다(§6.4).
- **`presence`:** `HIMARI_COMPANION`이 켜져 있고 짝지은 기기가 하나라도 있을 때만 넣는다.
  - **`sent_from`(검수 B5):** 턴이 시작될 때 로그의 마지막 이벤트가 사용자 메시지(질문의 답 포함)이면 그 `origin`이다. `{"kind": "web"}`, `{"kind": "cli"}`, `{"kind": "device", "name": …}` 가운데 하나다. 마지막 이벤트가 사용자 메시지가 아니면(자리 비움 뒤 이어 가기, 복구 등) 넣지 않는다. `origin`이 없는 옛 메시지도 넣지 않는다.
  - `current_device`: 자리 비움이면 넣지 않는다.
  - `devices`: 끊지 않은 기기만, 8개까지.
- 기기 이름은 사용자가 정한 글이다. Current State 전체가 `json_data`로 인코딩되므로 블록을 닫을 수 없다(V3-P71 §5.2와 같음).
- 과거 메시지 하나하나에 보낸 곳을 붙이지 않는다. 기기를 바꿔 이어 말할 때는 이번 턴의 `sent_from`으로 충분하다.

### 6.3 적용 시점

턴이 시작될 때 한 번 읽고 그 턴 내내 같다(V3-P71 §5.4와 같음).
- Driver가 턴을 시작할 때(`_exclusive` 안, 지시문·기억과 함께) 시계와 `PresenceSource`를 읽는다. `sent_from`은 Driver가 로그에서 정해 `PresenceSource`에 넘긴다.
- `PromptInputs.now`와 `PromptInputs.presence`(`PresenceView`)로 넘긴다. `compose()`는 그대로 순수 함수다.
- **다른 입력 경로(검수 N3):** `context_status()`와 수동·자동 압축도 Prompt 입력을 만든다. 크기 계산에는 `now`·`presence`·시각 줄을 모두 넣는다. 압축 요약 요청에는 Current State를 넣지 않는다(V3-P71과 같음). 시각 줄은 넣는다.
- 한 턴이 길게 일해도 `now`는 턴이 시작된 시각이다. Current State 머리말에 "`now` is when this turn started"라고 적는다.

### 6.4 시각 (Q5, 검수 C8)

- **`now`:** 턴이 시작된 시각. 보낸 기기의 시간대를 쓰고, 없으면 새 서버 설정 `HIMARI_TIME_ZONE`(기본 `Asia/Seoul`)을 쓴다. 분 단위, **UTC 차이를 늘 붙인다.**
- **시각 줄:** 사용자 메시지의 내용 앞에 따로 한 줄을 둔다.
  ```
  <time>2026-10-29T23:40+09:00</time>
  ```
  - 시간대는 `HIMARI_TIME_ZONE`, UTC 차이를 붙인다. `now`와 시간대가 달라도 차이가 적혀 있으므로 헷갈리지 않는다.
- **붙이는 규칙:**
  - 대상은 **질문의 답이 아닌** 사용자 메시지다(답은 Tool 결과로 그려지므로 빼고, 간격 계산에서도 뺀다).
  - `PromptInputs.events` 안에서 바로 앞의 대상 메시지보다 **30분 넘게 늦으면** 붙인다.
  - 보이는 첫 대상 메시지에는 늘 붙인다. 대화의 처음이든, 압축 요약 뒤의 처음이든 같다(압축 뒤에는 그 앞 메시지가 입력에 없으므로).
- **결정성:** `created_at`은 바뀌지 않으므로 같은 입력은 같은 줄이 된다. 캐시가 깨지지 않는다. 다만 다음 두 경우에는 한 번 깨진다.
  - 이 기능을 배포한 직후: 모든 대화의 과거 입력에 줄이 생기고, 시스템 문장에 `COMPANION_NOTE`가 더해진다. 대화마다 한 번 캐시를 다시 만든다.
  - `HIMARI_TIME_ZONE`을 바꿨을 때.
- **흉내:** 사용자가 `<time>…</time>`을 직접 칠 수는 있다. 그 사람은 사용자 자신뿐이므로 보안 경계가 아니다. `COMPANION_NOTE`는 "메시지 맨 앞의 시각 줄"만 시스템의 것이라고 말한다.
- Data의 원문은 바꾸지 않는다(시각 줄은 Prompt가 그릴 때만 생김).

## 7. 표정 표시 (Q3, Q7)

### 7.1 문법과 고정 목록

- 문법: **`[[mood:이름]]`**. 답의 어디에나 올 수 있다. 말하다가 표정이 바뀌는 곳에 둔다.
- **표정 목록(Q7):** `neutral`, `happy`, `excited`, `sad`, `surprised`, `thinking`, `embarrassed`, `worried`.
- **자리(검수 B1):** `MOODS`와 `split_moods`는 **`himari.common`**(계층 0)에 둔다. Prompt(`COMPANION_NOTE`), Data(`event_text`), interfaces(Web·CLI)가 모두 같은 것을 쓴다. 문법이 하나뿐이라 갈라지지 않는다.
- 목록을 바꾸는 것은 계약 변경이다(서버와 앱이 함께 바뀜). 더하기만 하면 옛 앱은 모르는 이름을 `neutral`로 그린다(§9.1).
- **앱 자신의 상태는 표정이 아니다.** 듣는 중(입력창 열림), 기다리는 중(작업은 시작했고 글이 아직 없음), 말하는 중(글이 흘러옴), 가만히, 졸림(오래 입력 없음)은 앱이 사건을 보고 스스로 정한다(§9.3). 모델은 이것을 쓰지 않는다.

### 7.2 떼어 내는 규칙과 `display`

`split_moods(text, *, complete) -> (plain, moods, consumed)`

- `moods`는 `[(at, mood)]`. `at`은 `plain` 안의 위치이고, **단위는 유니코드 코드 포인트**다(Python `len`과 같음). JS·C#처럼 UTF-16을 쓰는 앱은 바꿔 세야 한다(§9.1).
- 표시로 보는 것: `[[mood:` + `[a-z0-9_]{1,24}` + `]]`. 목록에 없는 이름도 떼어 내고 `neutral`로 바꾼다.
- 표시로 보지 않는 것(글로 남김):
  - 펜스 코드 블록 안(줄 머리의 ```` ``` ```` 또는 `~~~`로 열고 닫음).
  - **같은 줄에서 닫히는** 인라인 코드(`` ` ``) 안.
- `complete=False`(실시간)일 때는 확정할 수 없는 끝부분을 남겨 두고, 그 앞까지만 `consumed`로 돌려준다. 남겨 두는 것은 다음 둘이다.
  - 표시의 앞부분일 수 있는 꼬리: `[`, `[[`, `[[m`, …, 40자 안의 `[[mood:…`.
  - 마지막 줄에서 아직 닫히지 않은 `` ` ``부터 끝까지.
- `complete=True`이면 모두 확정한다. 닫히지 않은 꼬리는 글이다.
- **불변식:** 어떤 지점에서 실시간으로 확정한 결과는 최종 결과의 앞부분과 같다. 그래서 실시간 글을 이으면 최종 `display.text`가 된다.

**`display`(모든 대화, 검수 C6):**
- `events` 응답의 `assistant_final`과 `assistant_tool_calls`(해설) 이벤트에 `display: {"text": …, "moods": [{"at": 0, "mood": "happy"}]}`를 더한다. 기존 `text`(원문)는 그대로 둔다.
- 대화의 `waiting.question`(ask_user 질문) 보기에도 같은 `display`를 더한다.
- Web은 `display.text`를 그린다. Copy도 `display.text`를 복사한다.
- **CLI**도 `display.text`를 찍는다.
- **Data는 모델이 쓴 원문을 그대로 둔다**(표시 포함). 모델의 과거 답에도 표시가 그대로 보이므로, 모델은 자기가 쓴 형식을 계속 본다.
- **대화 검색(V3-P72):** `event_text`는 `AssistantFinal`의 글에서만 표시를 뺀다. 사용자 메시지는 그대로 둔다. v14 업그레이드에서 기존 `event_text`를 다시 채운다.
- 표정은 **Tool이 아니다.** Tool로 하면 표정 하나마다 LLM 호출이 한 번 더 든다.

### 7.3 `live`에서 (검수 B4)

`live`는 조각의 흐름이 아니라, Runner가 쌓아 둔 Run의 글을 커서와 비교해 보내는 방식이다(snapshot은 처음과 Run이 바뀔 때·다시 연결할 때, 그 뒤 delta). 표정 떼기도 **쌓인 글 기준**으로 한다.

- 커서는 두 개를 센다: 읽은 원문의 길이(`raw`)와 보낸 `plain`의 길이(`plain`).
- 깨어날 때마다 그 Run의 쌓인 글 `T`에 `split_moods(T, complete=ended)`를 하고, 지난번보다 늘어난 `plain`과 그 안의 `moods`만 보낸다.
  - 구현은 끝에서 다시 셀 수 있는 지점부터 이어 계산해도 된다. 결과가 위와 같아야 한다.
- 사건의 바뀐 모양:
  - `snapshot {run_id, text: plain, moods: [{at, mood}], tools, thinking, truncated, ended}`
  - `delta {run_id, text: 새 plain, moods: [{at, mood}], tools, …}` — `at`은 그 Run의 `plain` 처음부터 센 위치다.
  - `end`: 남겨 둔 꼬리를 `complete=True`로 확정해 마지막 `delta`로 보낸 뒤 보낸다.
- **`at`은 Run마다 0부터** 센다(Run마다 `run_id`와 snapshot이 따로임). 도구를 부르는 Run의 해설 글도 같다.
- 5분이 되어 연결이 끊기면, 남겨 둔 꼬리는 보내지 않은 채로 끝난다. 다시 연결하면 snapshot이 확정된 만큼을 보낸다. 잃는 것이 없다.
- `truncated`(`LIVE_TEXT_CHARS` 초과)이면 잘린 지점까지만 떼고, 최종은 `events`의 `display`가 맞는다.
- **불변식:** 한 Run의 snapshot·delta의 `text`를 이으면, 그 Run이 쓴 이벤트의 `display.text`와 같다. `moods`도 같다.

## 8. 사건 흐름과 대화 고르기

### 8.1 사건 흐름

`GET /api/events` — 주체 단위 SSE. 기기(기기 토큰)와 Web(세션 쿠키)이 같이 쓴다. 사건의 모양은 부록 A.3.

- **사건:**
  - `hello`: 열렸을 때. 지금 상태 전부(이 기기, 표정 목록, 지금 기기, 최근 대화, 활동과 각 작업의 보낸 곳). **받는 쪽은 `hello`로 자기 상태를 통째로 바꾼다.** 다시 보내기(`Last-Event-ID`)는 없다.
  - `activity`: 일하는 중인 대화(보낸 곳과 함께)와 답을 기다리는 대화(V3-P80과 같은 규칙). 바뀔 때마다.
  - `job`: 어느 대화의 작업이 시작·끝남, 보낸 곳.
  - `conversation`: 그 대화에 새 이벤트가 생김(받아들임, Run 끝, 작업 끝).
  - `current`: 지금 기기가 바뀜.
  - `revoked`: 이 기기가 끊김(그 기기의 흐름에만). 바로 닫는다.
  - `replaced`: 같은 기기의 새 흐름이 열려 이 흐름을 닫음.
  - 15초마다 주석 줄(연결 유지).
- 답의 글과 표정은 그 대화의 `live`(§7.3)로 받는다. 사건 흐름은 알림만 한다.
- 연결당 5분(Caddy `write` 600s 안). 정상으로 끝나면 받는 쪽이 바로 다시 연다.
- 주체당 사건 흐름은 16개까지다(기기와 Web 탭을 합쳐). 넘으면 429 `too_many_streams`.
- **끊기(검수 C5):** 기기를 끊으면 그 기기의 사건 흐름과 `live` 흐름을 모두 닫는다. 서버는 `live` 흐름을 연 기기를 기억한다.
- **Web(검수 N2):**
  - 사이드바·탭 제목의 활동 표시(V3-P80)를 `activity` 사건으로 그린다.
  - 탭이 숨으면 흐름을 닫고, 다시 보이면 열어 `hello`로 맞춘다(V3-P80이 숨은 탭에서 읽기를 멈춘 것과 같음).
  - 흐름이 끊긴 동안만 5초 활동 읽기를 쓴다. `GET /api/conversations/activity`는 남긴다.

### 8.2 어느 대화에서 말하나

- **최근 대화:** `most_recent_user_conversation`(§4.2). `hello`와 `GET /api/device`가 알려 준다.
- 앱은 **켜질 때**, 그리고 **10분 넘게 지금 기기가 아니었다가 다시 지금 기기가 될 때**(`current` 사건) 최근 대화로 옮긴다. 입력 중이면 옮기지 않는다.
  - 그래서 데스크톱에서 하던 말을 노트북에 와서 이어 간다. 두 PC를 잠깐씩 오갈 때는 대화가 바뀌지 않는다.
- 앱에서 **새 대화**를 열 수 있고, **대화 목록**에서 고를 수 있다.
- 지금 대화가 작업 중이면(`BUSY`) 앱은 그 사실을 보이고, 새 대화로 말을 걸 수 있게 한다.

### 8.3 자리 비움 멈춤과 기기 (검수 C3)

- V3-P62: 아무도 대화를 읽지 않으면 작업은 20 Run 뒤 멈춘다. 지금은 `events`와 대화 보기를 읽을 때만 `seen`이 불린다.
- 앱은 말풍선을 `live`로 보므로 `seen`을 부르지 않는다. 그대로면 앱에서 시킨 긴 일이 멈춘다.
- **규칙:** 기기의 `live` 흐름이 열려 있고 **그 기기가 지금 기기이면** 15초마다 `seen`을 부른다. 사용자가 그 PC 앞에서 캐릭터를 보고 있다는 뜻이기 때문이다. 자리 비움이면 부르지 않으므로, 아무도 없을 때 멈추는 원래 목적은 지켜진다.

### 8.4 답이 나타나는 곳 (Q4)

- 메시지를 **보낸 기기**에서 캐릭터가 말한다(말풍선과 표정). 앱은 `job` 사건의 보낸 곳이 자기일 때 `live`를 연다.
- 다른 기기는 기록만 갱신한다. 캐릭터가 말하지 않는다.
- Web에서 보냈으면 Web에만 보인다.

## 9. 앱 계약 (로컬 에이전트용)

Windows 앱은 소유자의 로컬 에이전트가 `clients/windows/`에 만든다. 앱 기술, 애니메이션, 캐릭터 형식, 화면 배치는 로컬 에이전트가 정한다. 아래와 부록 A만 지킨다.

### 9.1 반드시

1. **토큰:** 기기 토큰은 앱이 만들고(§4.1) OS 보관소(자격 증명 관리자 또는 DPAPI)에만 둔다. 파일·로그·화면에 쓰지 않는다.
2. **서버 요청은 네이티브 쪽에서:** 서버는 CORS를 열지 않는다. 웹 기술로 화면을 만들면 화면 쪽은 서버에 직접 요청하지 않고, 네이티브 쪽(또는 같은 프로세스의 HTTP 클라이언트)이 대신 요청한다. 화면 쪽은 토큰을 보지 않는다.
3. **원격 코드 금지:** 화면은 원격 스크립트·스타일을 불러오지 않는다. 모델이 쓴 글은 HTML로 넣지 않고 글(또는 안전하게 다듬은 마크다운)로 그린다.
4. **서버 주소:** 사용자가 넣은 HTTPS 주소만 쓴다. 인증서 검사를 끄지 않는다. `Host`는 서버의 허용 이름(TrustedHost)과 맞아야 한다(주소의 이름을 그대로 쓰면 됨).
5. **접속 신호:** §5대로 보낸다. `idle_seconds`는 시스템 전체의 입력으로 잰다.
6. **사건 흐름:** 늘 하나만 열어 둔다. 정상으로 끝나면 바로, 오류면 1초부터 30초까지 늘려 가며 다시 연다. `hello`로 상태를 통째로 바꾼다. `revoked`면 토큰을 지우고 짝짓기 화면으로 간다. `replaced`면 같은 앱이 두 번 떠 있는 것이므로 하나를 끝낸다.
7. **`live`:** 자기가 보낸 작업의 `job started`부터 `done`까지만 연다. 글은 `text`를 이어 붙이고, `moods`의 `at`(코드 포인트)에 오면 표정을 바꾼다.
8. **표정:** `GET /api/device`의 목록에 있는 표정을 모두 그릴 수 있어야 한다. 모르는 이름은 `neutral`로 그린다.
9. **글만 보낸다**(1단계). 메시지는 `POST /api/conversations/{id}/messages {"text": …}`, 1~32,000자(코드 포인트, 앞뒤 공백 뺀 뒤). 서버도 글만 받는다(§4.3).
10. **기록:** 대화를 보일 때는 `events`의 `display.text`를 그린다(`text`는 원문).
11. **오류:** 401은 다시 짝짓기. 403 `device_scope`는 앱의 잘못이므로 로그에 남긴다. 403 `companion_off`는 "서버에서 꺼짐"을 보이고 5분마다 다시 본다. 409 `busy`는 사용자에게 보인다. 429 `too_many_streams`는 30초 뒤 다시.

### 9.2 해야 할 것

- **말풍선:** `live`의 글을 흘러오는 대로 그리고, 다 읽을 시간이 지나면 접는다. 긴 답은 펼쳐 보기나 "Web에서 열기"를 둔다.
- **입력:** 캐릭터 클릭 또는 단축키로 입력창. Enter로 보낸다.
- **멈춤:** 답하는 중 멈춤 → `cancel`.
- **질문(`ask_user`):** 대화 보기의 `waiting.question.display.text`를 말풍선에 보이고 입력창을 연다. 답은 보통 메시지 경로로 간다(Web과 같음).
- **트레이:** 보이기·숨기기, 다시 연결, Web 열기, 끝내기, 연결 상태.
- **연결이 끊기면:** 캐릭터를 흐리게 하고 입력을 막는다. 보내지 못한 입력은 앱 안에 남긴다.

### 9.3 앱이 혼자 하는 것 (LLM을 부르지 않음)

- 가만히 있을 때의 움직임(숨쉬기, 깜빡임 등), 듣는 중·기다리는 중·말하는 중의 움직임(§7.1), 오래 입력이 없을 때의 졸림.
- 이것들은 서버에 알리지 않는다.

### 9.4 참조 클라이언트

- 서버 2부와 함께 **가짜 기기 스크립트**(`scripts/device_client.py`)를 둔다. 짝짓기(잃은 답 다시 보내기 포함) → 사건 흐름 → 접속 신호 → 메시지 → `live`의 글과 `moods`를 순서대로 해 보인다. CI에서 돌고, 로컬 에이전트에게는 실행되는 계약 예시가 된다.
- `clients/windows/README.md`(로컬 에이전트가 씀)에 빌드·실행 방법을 둔다. Python 검사(mypy, pytest, import 규칙)는 `clients/`를 보지 않는다.

### 9.5 애니메이션을 먼저 만들 때

애니메이션은 서버와 독립이라 **지금 바로 시작해도 된다.** 서버와 맞출 것은 표정 이름 8개(§7.1)뿐이다. 앱 상태 다섯은 앱 안의 일이다. 서버가 준비되기 전에는 부록 A.4 모양의 가짜 `live` 사건을 앱 안에서 흘려 시험한다.

## 10. 보안

- **기기 토큰의 무게:** 기기 토큰은 화면 권한이 좁지만, **에이전트에게 말할 수 있다.** 에이전트는 대화 안에서 Tool(machine 파일·명령, 웹 검색 등)을 쓴다. 그래서 토큰을 훔친 사람은 에이전트에게 파일을 읽어 달라고 할 수 있다. 막는 것은 다음과 같다.
  - VPN 안에서만 서버에 닿는다.
  - 토큰은 OS 보관소에 있고 서버는 해시만 둔다.
  - Web에서 바로 끊을 수 있다.
  - 기기 목록에 마지막 접속 시각이 보인다.
- **사람 쪽 권한 우회 막기:** 메시지의 `references`(서버 파일 읽기)와 `attachments`는 기기에게 막는다(§4.3).
- **짝짓기:** 10분, 한 번, 서버 전체 5번 실패, 1초 늦춤과 한 번에 하나(§4.1).
- **프롬프트 주입:** 기기 이름은 Current State의 JSON 안에만 들어간다. 표정 표시는 모델이 쓴 글에서 떼어 낼 뿐 해석하지 않는다(목록 밖 이름은 `neutral`).
- **기본 거부:** 새 경로는 기기에게 저절로 막히고, 경로를 모두 도는 시험이 목록을 지킨다(§4.3).

## 11. 모듈 경계

| 모듈 | 바뀌는 것 |
| --- | --- |
| `common` | `MOODS`, `split_moods` |
| `data` | v14: `devices`, `device_ids`, `UserMessage.origin`. `DeviceStore`, `most_recent_user_conversation`. `event_text`에서 표시 빼기(다시 채우기). `created_at` 주석 |
| `presence` (새, 계층 2) | `Devices`·`Presence`·`EventHub`(§2.2). `common`·`data.api`만 |
| `prompt` | `PromptInputs.now`·`presence`, `PresenceView`, `state_block`의 `now`·`presence`, 시각 줄, `COMPANION_NOTE` |
| `control` | `PresenceSource`·`JobListener` 포트(api), Driver가 턴 시작에 시계·보낸 곳·presence를 읽음, Runner가 `JobListener`를 부름 |
| `bootstrap` | presence 조립, `PresenceSource`·`JobListener` 구현, 휴지통·되살리기·삭제의 `activity` |
| `interfaces/web` | 짝짓기·기기 경로, `caller()`와 기본 거부, `origin` 정하기, `GET /api/events`, `events`·질문 보기의 `display`, `live`의 표정 떼기와 `seen`, Web 설정 화면 Devices, 화면이 `display.text`를 그림, 활동 표시를 사건 흐름으로 |
| `interfaces/cli` | `display.text`를 찍음 |
| `clients/windows` | 새 앱(로컬 에이전트) |

- Provider, Tool, Environment, runner, sandbox, MCP는 바뀌지 않는다.

## 12. 다음 단계 (방향, 이 패킷에서 구현하지 않음)

1. **메모리 2단계와 일화 기억:** 대화를 나누어 쓰므로 대화 사이의 연속성은 메모리가 맡는다. 자동 추출과 일화(언제 무엇을 함께 했는지)를 더한다. 모델을 불러야 하므로 provider 위 계층의 새 모듈(`himari.memory`)이 된다. 임베딩 검색을 위해 Provider에 임베딩 포트가 필요하다.
2. **먼저 말 걸기와 스케줄러:** 서버의 계기(정한 시각, 사용자가 돌아옴, 예정된 일)가 턴을 연다. 무엇이 깨웠는지 기록할 새 이벤트 종류가 필요하다. 사건 흐름으로 지금 기기에 알린다. 지금의 휴지통 정리 루프도 스케줄러로 옮긴다.
3. **작업 중 메시지:** `BUSY` 대신 받아 두었다가 다음 Run 사이에 넣는다.
4. **로컬 파일과 화면 캡처:** `pc` Environment(§2.1).
5. 앱의 첨부·붙여넣기, 음성, 앱 자동 업데이트.

## 13. 소유자 결정

2026-10-30 소유자 결정: Q1·Q2는 로컬 에이전트에게 맡김, Q5·Q6은 rev. 2의 방향대로, Q3·Q4·Q7은 권고대로("전부 괜찮아"). rev. 4의 검수 반영은 이 결정들을 바꾸지 않는다.

| # | 질문 | 선택지 | 권고 → 결정 |
| --- | --- | --- | --- |
| Q1 | Windows 앱 기술 | A. Tauri 2 / B. Electron / C. .NET + WebView2 | rev. 1 권고는 A → **로컬 에이전트가 정함**(§9.1의 조건 안에서) |
| Q2 | 캐릭터 형식 | A. 표정마다 PNG / B. Live2D / C. VRM | rev. 1 권고는 A → **로컬 에이전트가 정함**. 서버와는 표정 이름만 맞춤(§9.5) |
| Q3 | 표정 표시의 위치 | **A. 답의 어디에나(바뀌는 곳마다)** / B. 답 맨 앞에 하나만 | **A**. 말하는 중에 표정이 바뀌어야 살아 보인다. 서버가 떼어 내므로 화면에는 보이지 않는다. → 권고대로 |
| Q4 | 답이 나타나는 곳 | **A. 보낸 기기에서만 말함, 다른 기기는 기록만** / B. 지금 기기에서 말함 / C. 연결된 모든 기기에서 말함 | **A**. 말을 건 곳에서 답이 오는 게 자연스럽다. C는 PC 두 대가 동시에 말한다. → 권고대로 |
| Q5 | 모델이 아는 시각 | A. 지금 시각 + 30분 넘게 쉬었다 온 메시지의 시각 줄 / B. 지금 시각만 / C. 모든 메시지 앞에 시각 | rev. 1은 반려 대화에만 A → **모든 대화에 A**(소유자: "시간을 알려줄 방법이 있어야 할 것 같다") |
| Q6 | 반려 대화 | A. 주체마다 하나 / B. 여러 개 / C. 구분 없음 | rev. 1 권고는 A → **C. 구분 없음**. HIMARI 자체가 반려 에이전트, 앱은 최근 대화를 이어 감(§8.2) |
| Q7 | 표정 목록 | **`neutral`, `happy`, `excited`, `sad`, `surprised`, `thinking`, `embarrassed`, `worried`** / 더 적게(5개) / 더 많이 | **8개**. 대화에서 자주 필요한 결을 덮으면서 애니메이션 부담이 크지 않다. 더하기는 뒤에 호환되게 할 수 있다(§7.1). → 권고대로 |

## 14. 검증 계획

- **pytest(서버):**
  - **짝짓기:** Origin 없는 요청이 지남(B2), 코드 만료, 한 번만, 서버 전체 5번 실패, 동시에 보낸 추측, 새 코드가 이전 줄을 지움, 잃은 답 다시 보내기(같은 토큰은 같은 답, 다른 토큰은 거부), 토큰 해시만 저장, 끊기가 `token_hash`를 지움, 이름 규칙(`web`·`cli`, 중복, 제어 문자).
  - **권한:** `app.routes`를 모두 도는 기본 거부 시험(C1), 기기의 `references`·`attachments` 403(B3), `trashed=true` 403, 끊은 토큰 401, `companion_off`.
  - **origin과 `sent_from`:** T1·T7a의 origin(기기·web·cli), 이어 가기 턴에는 없음, 옛 이벤트(B5).
  - **presence:** 지금 기기 고르기, 60초 흔들림 막기, 타이머로 자리 비움, 120초 신호 없음 → 끊김, 30초 유예, 기기당 흐름 하나(`replaced`), 16개 넘으면 429, 끊기가 `live`도 닫음, 느린 구독자 닫기(C4, C5).
  - **seen:** 기기만 보고 있을 때 긴 작업이 멈추지 않음, 자리 비움이면 멈춤(C3).
  - **Prompt:** 모든 대화의 `now`, 시각 줄(첫 메시지, 30분 경계, 답은 빼기, 압축 경계 뒤 처음, 같은 입력 → 같은 줄), `presence`는 스위치와 기기가 있을 때만, `context_status` 크기에 들어감(C8, N3).
  - **`split_moods`:** 시험 벡터(여러 표시, 목록 밖 이름, 펜스·인라인 코드 안, 닫히지 않은 인라인 코드, 깨진 `[[`, 한국어 사이, 기본 다국어 평면 밖 문자의 `at`), 실시간 확정의 앞부분 불변식(무작위로 자른 접두사).
  - **`live`:** 표시가 섞인 snapshot, 표시 한가운데서 다시 연결, `[` + `[mood:…]]`, `end`에서 꼬리 확정, 스트리밍 중 펜스, 잘림, Run마다 `at` 0부터, 이어 붙인 글 = `display.text`(B4).
  - **`display`:** 최종 답, 도구 호출 해설, 질문 보기, CLI 출력(C6).
  - **Data:** v13 → v14 업그레이드와 백업, `event_text` 다시 채우기, `DeviceStore`와 `most_recent_user_conversation`의 SQLite·메모리 일치(C7).
  - **import 규칙:** `presence` 계층과 `ONLY_FROM`.
- **가짜 기기 스크립트**(§9.4): CI에서 실제 uvicorn 서버와 Scripted Provider로 돈다.
- **ui_smoke:** Web이 표시를 떼어 그리고 Copy가 `display.text`인지, Devices 화면, 숨은 탭이 흐름을 닫는지.
- **Windows 앱:** 로컬 에이전트가 자기 환경에서 확인한다. 소유자가 실제 PC 두 대 사이에서 이어 말하기를 확인한다.
- **실제 모델 확인:** 표정 표시를 문법대로 쓰는지, 기기에서 보낸 메시지에 짧게 답하는지, 시각 줄을 이해하는지(모델마다 몇 턴).

## 15. 구현 단계

1. **서버 1부:** Data v14(`devices`, `UserMessage.origin`, 최근 대화 질의), `DeviceStore`, presence의 `Devices`, 짝짓기·기기 토큰·`caller()`와 기본 거부, `origin` 정하기, Web 설정 화면 Devices.
2. **서버 2부:** 시각(`now`, 시각 줄 — 모든 대화), presence의 `Presence`·`EventHub`와 `GET /api/events`(Web 활동 표시 포함), `JobListener`·`PresenceSource`, `presence` 항목, `COMPANION_NOTE`·`MOODS`·`split_moods`, `display`와 `live`의 표정, `seen`, CLI, 가짜 기기 스크립트.
3. **Windows 앱(로컬 에이전트):** §9와 부록 A대로. 애니메이션은 서버와 상관없이 지금 시작할 수 있다(§9.5).

배포는 1·2부를 함께 한다(v14 업그레이드). 시각(2부의 일부)은 따로 먼저 배포해도 된다(DB 변경 없음).

## 부록 A. 연결 형식 (검수 C9)

모든 요청은 HTTPS, JSON은 UTF-8. 오류는 지금의 봉투 `{"error": {"code": …, "message": …}}`를 쓴다.

### A.1 짝짓기와 기기 관리

**Web(세션 쿠키):**

| 방법 | 경로 | 몸 → 답 |
| --- | --- | --- |
| GET | `/api/devices` | → `{"items": [{"device_id", "name", "paired": bool, "connected": bool, "current": bool, "last_seen_at", "created_at"}]}` (끊은 기기는 빼고) |
| POST | `/api/devices` | `{"name"}` → 201 `{"device_id", "name", "code": "ABCD-EFGH", "expires_at"}` |
| PATCH | `/api/devices/{device_id}` | `{"name"}` → 200 위 항목 하나 |
| DELETE | `/api/devices/{device_id}` | → 204 (끊기) |

**앱(인증 없음, 코드 머리글):**

```
POST /api/devices/pair
Authorization: Pairing ABCD-EFGH
Content-Type: application/json

{"token": "hd_3q2-…(43자)"}
```

- 200 `{"device_id": "d3", "name": "데스크톱"}`
- 401 `pairing_invalid`: 코드가 없거나 틀림(실패로 셈).
- 401 `pairing_expired`: 만료되었거나 실패가 5번 넘어 지워짐.
- 409 `pairing_used`: 다른 토큰으로 이미 짝지음.
- 400 `invalid_token`: 토큰 모양이 틀림(`hd_` + 43자 base64url).
- 403 `companion_off`.

### A.2 기기

```
GET /api/device
Authorization: Bearer hd_…
```

```json
{
  "device": {"device_id": "d3", "name": "데스크톱"},
  "moods": ["neutral", "happy", "excited", "sad", "surprised", "thinking", "embarrassed", "worried"],
  "current_device": "d3",
  "recent_conversation": {"id": "c_…", "title": "…"},
  "limits": {"message_chars": 32000}
}
```

`POST /api/device/presence` 몸 `{"idle_seconds": 12, "time_zone": "Asia/Seoul"}` → 204.

### A.3 `GET /api/events`

```
event: hello
data: {"device":{"device_id":"d3","name":"데스크톱"},"moods":[…],"current_device":"d3",
       "recent_conversation":{"id":"c_…","title":"…"},
       "activity":{"working":[{"id":"c_…","origin":{"kind":"device","device_id":"d3"}}],"waiting":["c_…"]}}

event: activity
data: {"working":[{"id":"c_…","origin":{"kind":"web"}}],"waiting":[]}

event: job
data: {"conversation":"c_…","state":"started","origin":{"kind":"device","device_id":"d3"}}

event: job
data: {"conversation":"c_…","state":"ended","outcome":"completed"}

event: conversation
data: {"conversation":"c_…"}

event: current
data: {"device_id":"d5"}          (자리 비움이면 null)

event: revoked
data: {}

event: replaced
data: {}
```

- Web의 `hello`는 `device`가 `null`이다.
- `job`의 `outcome`은 Control `DriveState`의 값(`completed`, `waiting_for_user`, `provider_failed`, `budget_reached` 등)을 그대로 쓰고, 멈춤·취소는 지금 작업 보기의 값을 쓴다. 앱은 모르는 값을 "끝남"으로 다룬다.
- 15초마다 `: keep-alive` 주석 줄이 온다.

### A.4 `live`(바뀐 부분만)

```
event: snapshot
data: {"run_id":"r_…","text":"좋아, 해 볼게.","moods":[{"at":0,"mood":"happy"}],"tools":[],"thinking":false,"truncated":false,"ended":false}

event: delta
data: {"run_id":"r_…","text":" 음… 이건 좀 어렵네.","moods":[{"at":9,"mood":"thinking"}],"tools":[]}

event: end
data: {"run_id":"r_…"}

event: done
data: {}
```

- `at`은 그 Run의 `text`를 처음부터 이은 글에서의 위치이고, 단위는 유니코드 코드 포인트다.
- 나머지(`idle`, `retry`, 5분 끊김, 주체당 8개)는 V3-P73 그대로다.

### A.5 `display`

`events`의 `assistant_final`·`assistant_tool_calls` 항목과 대화 보기의 `waiting.question`에 더한다.

```json
{"kind": "assistant_final", "text": "[[mood:happy]]잘 됐다!", "display": {"text": "잘 됐다!", "moods": [{"at": 0, "mood": "happy"}]}, …}
```
