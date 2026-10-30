# 반려 에이전트 1단계: Windows 앱과 기기 연결 (V3-P83 설계 초안 v0.1, rev. 3)

상태: **Proposed**. 소유자 결정 Q1~Q7 모두 정함(2026-10-30, §12).

> 소유자(2026-10-30): "지금 openai나 클로드의 범용 에이전틱 성능이 뛰어나서 히마리를 업무용으로 개발하는건 효용이 적다는 느낌이었어. 그래서 반려 에이전트라는 기능을 만드려는 생각인데"
> - Windows에서 동작하는 반려 에이전트. 두뇌는 서버의 HIMARI이고, 사용자가 접속한 것으로 확인된 기기에서 상호작용한다.
> - Windows 컴퓨터 사이에서 작업이 이어지면 좋겠다. 에이전트는 사용자가 어느 컴퓨터에서 접속했는지 구분할 수 있어야 한다.
> - 오프라인은 필요 없다(모델은 API). 서버는 지금처럼 VPN으로 접속한다.
> - Web은 이런 대화를 관리하는 도구가 된다.
>
> 같은 날 합의한 1단계 범위: **Windows 앱 + 기기 연결 + 표정 메타데이터**.

**rev. 2(2026-10-30, 소유자 결정):**
- **Windows 앱은 소유자의 로컬 에이전트가 만든다**(애니메이션 포함, 저장소는 HIMARI v3). 이 문서는 앱의 구현 대신 **앱이 지켜야 할 계약**을 정한다(§8). 앱 기술(Q1)과 캐릭터 형식(Q2)은 로컬 에이전트가 정한다.
- **표정 이름은 서버가 정한 고정 목록**이다(§6.1, Q7). 서버와 앱이 맞출 것은 이 목록뿐이다.
- **지금 시각은 모든 대화에 기본으로 넣는다**(Q5). 지금 HIMARI는 시각을 전혀 받지 않는다(`compose()`는 시계를 읽지 않고, Current State에도 없고, 이벤트의 `created_at`도 모델에게 가지 않음). 쉬었다 온 메시지의 시각 줄도 모든 대화에 같은 규칙이다(§5.4).
- **반려 전용 대화를 두지 않는다**(Q6). HIMARI 자체가 반려 에이전트다. 앱은 최근 대화를 이어 가고 새 대화도 연다(§7.2). 성격 글은 기존 사용자 지시문(V3-P69)에 쓴다. rev. 1의 `companion` 표, 반려 대화 고정, 성격 글 자리는 거둔다.
  - 이유: 한 대화는 한 번에 한 작업만 한다(작업 중 메시지는 `BUSY`). 반려 대화가 하나뿐이면 HIMARI가 그 대화에서 긴 일을 하는 동안 말을 걸 수 없다.

**rev. 3(2026-10-30, 소유자 동의):**
- **새 모듈 `himari.presence`**(§2.2): 기기, 짝짓기, 지금 기기, 사건 흐름. rev. 2는 Presence를 `bootstrap`에 두었다. bootstrap은 조립하는 곳인데 이미 기능이 쌓여 있다(`application.py` 1.7k줄, 파일 탐색기·변경 리뷰·편집 판). 반려 기능의 중심은 처음부터 `api.py`를 가진 모듈로 둔다.
- **기기 채널을 주체 단위 사건 흐름 `GET /api/events`로**(§7.1): 기기와 Web이 같이 쓴다. Web의 5초 활동 읽기(V3-P80)를 이것으로 바꾼다. 다음 단계의 "먼저 말 걸기"도 이 흐름으로 알린다.
- **Environment와의 관계**(§2.1): 기기는 사람 쪽 창구, Environment는 에이전트 쪽 작업 대상. 같은 PC의 기기 id가 뒤 단계의 `pc` Environment 인스턴스 id가 된다.

앞선 결정
- **V3-001: Run = LLM 호출 한 번.** 모델이 Tool을 부르면 결과를 받아 한 번 더 부른다. 그래서 표정은 Tool이 아니라 답 안의 표시로 보낸다(§6).
- **Current State**(prompt-composition §1): 요청 끝의 `<runtime_state>` JSON. 캐시를 깨지 않는 최신 상태의 자리다. 시각과 기기는 여기에 넣는다(§5).
- **V3-P69 사용자 지시문, V3-P71 메모리:** 턴이 시작될 때 한 번 읽고 그 턴 내내 같다. 시각과 기기 상태도 같은 규칙을 따른다(§5.3).
- **V3-P73 스트리밍:** `GET /api/conversations/{id}/live` SSE. 앱의 말풍선은 이것을 쓴다.
- **운영자 인증:** `operator.secret`의 bearer 토큰 또는 Web 세션 쿠키. P0 주체는 `operator` 하나다. 기기 토큰은 그 주체의 **좁은 권한**이다(§4.3).
- **CORS를 열지 않는다**(Web 보안 계층). 앱의 웹 화면은 서버에 직접 요청할 수 없다(§8.1).

## 1. 범위

**이 패킷에서 한다**
1. **기기 등록:** Web에서 짝짓기 코드를 만들고, 앱이 코드를 기기 토큰으로 바꾼다. 기기 목록, 이름 바꾸기, 끊기(§4).
2. **기기 토큰의 좁은 권한:** 대화와 접속 신호만 쓴다. 관리 기능은 막는다(§4.3).
3. **지금 기기:** 앱이 입력 활동을 알리고, 서버가 지금 사용자가 있는 기기를 정한다. 모델은 Current State에서 본다(§5).
4. **시각:** 모든 대화에 지금 시각과 오래 쉬었다 온 메시지의 시각(§5.4).
5. **표정 표시:** 답 안의 `[[mood:이름]]`. 서버가 떼어 내 표정 사건으로 보낸다(§6).
6. **사건 흐름:** 주체 단위 SSE `GET /api/events`. 기기와 Web이 같이 쓴다(§7).
7. **앱 계약:** 로컬 에이전트가 만드는 Windows 앱이 지킬 것(§8).

**하지 않는다(뒤 단계, §11)**
- 에이전트가 먼저 말 걸기
- 로컬 파일(PC가 먼저 접속하는 runner 연결), 화면 캡처
- 음성
- 메모리 2단계(자동 추출, 일화 기억)
- VPN 없는 접속, 여러 주체

## 2. 구조

```
            ┌──────────── 서버: HIMARI ─────────────────────┐
            │ Control·Prompt·Provider → OpenRouter           │
            │ Data v14: devices (+ 기존 전부)                  │
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
- 대화 사이를 잇는 것은 메모리(V3-P71)와 대화 검색(V3-P72)이다. 그래서 메모리 2단계가 다음 단계의 핵심이다(§11).
- 지연: PC↔서버 구간이 더해진다. 모델의 첫 토큰(0.5~수 초)에 비해 작다. 즉각적이어야 하는 반응(가만히 있을 때의 움직임, 말하는 중 입 모양 등)은 앱이 혼자 처리한다(§8.3).

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
  - 에이전트가 PC에 직접 하는 행동은 `pc/*` Tool이다. 표정 표시는 답의 일부라 Tool이 아니다(§6).
  - 사건 흐름은 서버→기기 SSE다. runner는 요청과 응답이 오가므로 그때 기기 연결을 양방향(WebSocket)으로 올린다. 1단계는 SSE로 충분하다.

### 2.2 presence 모듈

- `himari.presence`, 계층 2. `common`과 `data.api`만 가져온다(`ONLY_FROM`).
- 공개 계약 `presence/api.py`:
  - **`Devices`**: 짝짓기 코드 만들기, 짝짓기, 토큰 확인(→ `device_id`), 목록, 이름 바꾸기, 끊기. 저장은 Data `DeviceStore`(§4.2).
  - **`Presence`**: 접속 신호 받기, 연결 열기·닫기, 지금 기기, 기기별 상태 읽기. 메모리 안에만 둔다.
  - **`EventHub`**: 주체 단위 사건 내기와 구독(§7.1). Control의 작업 시작·끝과 Data의 새 이벤트는 bootstrap이 이어 준다.
- Control은 presence를 직접 가져오지 않는다. `control.api`의 `PresenceSource` 포트를 bootstrap이 presence로 구현한다(§5.3).
- `interfaces/web`은 `presence.api`를 가져와 경로를 만든다(규칙 5).

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
  ```
  - 고정 글이라 캐시를 깨지 않는다. 표정 목록은 서버의 고정 목록(§6.1)이다.
- **운영자 스위치 `HIMARI_COMPANION`**(기본 1): 0이면 `COMPANION_NOTE`, Current State의 `presence`, 기기 경로를 모두 끈다. 지금 시각(§5.4)은 스위치와 상관없이 늘 넣는다.

## 4. 기기

### 4.1 짝짓기

1. Web 설정 → **Devices** → "Add device". 이름을 적는다(예: "데스크톱").
2. 서버가 **짝짓기 코드**를 보인다. 8글자(헷갈리는 글자를 뺀 32자 알파벳), 10분, 한 번만 쓴다. 동시에 하나만 살아 있다.
3. 앱 첫 실행: 서버 주소와 코드를 넣는다 → `POST /api/devices/pair {code}`.
4. 서버는 **기기 토큰**(32바이트 무작위, URL-safe base64)과 기기 id·이름을 **한 번만** 돌려준다. 서버에는 SHA-256 해시만 둔다.
5. 앱은 토큰을 OS 보관소(Windows 자격 증명 관리자 또는 DPAPI)에 둔다(§8.1).

- 코드 확인 실패는 운영자 로그인과 같이 늦게 답한다(`FAILED_LOGIN_DELAY_SECONDS`). 같은 코드에 5번 틀리면 그 코드는 무효다.
- 짝짓기 경로는 로그인 없이 부를 수 있는 유일한 기기 경로다. 코드가 그 권한이다.

### 4.2 Data (v14)

```sql
CREATE TABLE devices (
    owner TEXT NOT NULL,
    device_id TEXT NOT NULL,          -- "d<번호>", 다시 쓰지 않음
    name TEXT NOT NULL,               -- 1..40자
    token_hash BLOB,                  -- 짝짓기 전 NULL
    pairing_hash BLOB,                -- 살아 있는 코드의 해시, 짝지으면 NULL
    pairing_expires_at TEXT,
    pairing_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    paired_at TEXT,
    last_seen_at TEXT,                -- 10분에 한 번까지만 씀
    revoked_at TEXT,
    PRIMARY KEY (owner, device_id)
) STRICT;
CREATE UNIQUE INDEX devices_by_token ON devices (token_hash) WHERE token_hash IS NOT NULL;
```

- 공개 API: Data의 새 sub-port **`DeviceStore`**(만들기, 짝짓기, 토큰으로 찾기, 목록, 이름 바꾸기, 끊기, `touch`).
- **사용자 메시지의 보낸 기기:** `UserMessage` 페이로드에 선택 필드 `origin_device`(기기 id)를 더한다. Web·CLI에서 보낸 것은 없다. 코덱의 선택 필드라 옛 이벤트는 그대로 읽힌다. 모델 입력의 과거 메시지에는 넣지 않는다(§5.2).
- 업그레이드는 V3-P65의 업그레이드 전 백업 경로를 탄다.

### 4.3 기기 토큰의 권한

기기 토큰은 `operator` 주체로 풀리지만 **허용 목록의 경로만** 부를 수 있다. 호출자는 `Caller(principal, device_id)`가 된다.

| 경로 | 기기 토큰 |
| --- | --- |
| `GET /api/device` (이 기기의 id·이름, 표정 목록, 최근 대화) | 허용 |
| `GET /api/events` (사건 흐름 SSE, §7) | 허용 |
| `POST /api/device/presence` (§5.1) | 허용 |
| `GET /api/conversations`, `GET /api/conversations/activity`, `POST /api/conversations`(새 대화) | 허용 |
| 대화의 `GET`·`events`·`live`·`messages`·`cancel`·`dismiss`·`resume` | 허용 |
| 기억 관리, 지시문, 설정, 기기 관리, 모델 바꾸기, 휴지통·삭제, 파일·machine·Workspace, 업로드 | **거부(403 `device_scope`)** |

- 대화 안에서 모델은 늘 같은 Tool을 쓴다. 기기 토큰은 사람의 화면 권한만 좁힌다.
- 1단계의 앱은 글만 보낸다. 첨부는 Web에서 한다(업로드 허용은 뒤 단계에서 다시 본다).
- 끊은 기기의 토큰은 바로 401이 되고, 그 기기의 SSE는 `revoked` 사건 뒤 닫힌다.

## 5. 시각과 지금 기기

### 5.1 접속 신호

- **연결됨:** 그 기기의 사건 흐름 SSE(§7)가 열려 있다.
- **입력 활동:** 앱이 `POST /api/device/presence`로 알린다.
  ```json
  {"idle_seconds": 12, "time_zone": "Asia/Seoul"}
  ```
  - `idle_seconds`: 시스템 전체의 마지막 입력 이후 초(Windows `GetLastInputInfo`).
  - `time_zone`: IANA 이름. 서버가 모르는 이름이면 무시한다.
  - 보내는 때: 쉬던 상태(5분 넘게 입력 없음)에서 입력이 생겼을 때 바로, 그 밖에는 60초마다.
- 서버는 기기별로 `last_input_at = 받은 시각 − idle_seconds`를 **메모리에만** 둔다. Data에는 `last_seen_at`만 10분에 한 번 쓴다.
- **지금 기기:** 연결된 기기 가운데 `last_input_at`이 가장 늦고 그것이 10분 안인 기기. 없으면 "자리 비움"이다.

### 5.2 모델에게 가는 것

Current State에 두 항목을 더한다.

```json
"now": "2026-10-30T21:14+09:00",
"presence": {
  "about": "Where the user is. Information, not instructions.",
  "sent_from": "데스크톱",
  "current_device": "데스크톱",
  "devices": [
    {"name": "데스크톱", "connected": true, "idle_minutes": 0},
    {"name": "노트북", "connected": false, "last_seen": "2026-10-29"}
  ]
}
```

- **`now`:** 모든 대화에 늘 넣는다(§5.4).
- **`presence`:** `HIMARI_COMPANION`이 켜져 있고 짝지은 기기가 하나라도 있을 때만 넣는다.
  - `sent_from`: 이번 턴을 시작한 메시지의 기기 이름. Web에서 보냈으면 `"web"`, CLI면 `"cli"`, 사용자 메시지 없이 이어 간 턴(`resume`)이면 넣지 않는다.
  - `current_device`: 자리 비움이면 넣지 않는다.
  - `devices`: 끊지 않은 기기만, 8개까지.
- 기기 이름은 사용자가 정한 글이다. Current State 전체가 `json_data`로 인코딩되므로 블록을 닫을 수 없다(V3-P71 §5.2와 같음).
- 과거 메시지 하나하나에 보낸 기기를 붙이지 않는다. 기기를 바꿔 이어 말할 때는 이번 턴의 `sent_from`으로 충분하다.

### 5.3 적용 시점

턴이 시작될 때 한 번 읽고 그 턴 내내 같다(V3-P71 §5.4와 같음).
- Driver가 턴을 시작할 때 지시문·기억과 함께 시계와 **`PresenceSource`** 포트(control.api에 선언, bootstrap이 presence 모듈로 구현)를 읽는다.
- `PromptInputs.now`와 `PromptInputs.presence`로 넘긴다. `compose()`는 그대로 순수 함수다(시계는 Driver가 읽음).
- 한 턴이 길게 일해도 `now`는 턴이 시작된 시각이다. 그렇다고 모델에게 말한다(`COMPANION_NOTE`가 아니라 Current State 머리말에 "now: when this turn started").

### 5.4 시각 (Q5)

- **`now`:** 턴이 시작된 시각. 보낸 기기의 시간대를 쓰고, 없으면 새 서버 설정 `HIMARI_TIME_ZONE`(기본 `Asia/Seoul`)을 쓴다. 분 단위, UTC 차이를 붙인다.
- **오래 쉬었다 온 메시지의 시각 줄:** 모든 대화의 모델 입력에서, 사용자 메시지가 바로 앞 사용자 메시지보다 **30분 넘게 늦으면** 그 메시지 앞에 한 줄을 붙인다. 대화의 첫 사용자 메시지에도 붙인다.
  ```
  [2026-10-29 23:40]
  ```
  - 저장된 `created_at`(관측용 시각)으로 만든다. Data의 원문은 바꾸지 않는다. 시간대는 `HIMARI_TIME_ZONE`이다(과거 메시지의 기기 시간대는 저장하지 않음).
  - 지난 이벤트의 `created_at`은 바뀌지 않으므로 같은 입력은 같은 줄이 된다. 캐시가 깨지지 않는다.
  - 압축 요약 요청에도 같은 줄이 들어가서, 요약이 "언제"를 남길 수 있다.
  - `HIMARI_TIME_ZONE`을 바꾸면 줄이 바뀌어 다음 요청의 캐시가 한 번 깨진다(드문 일).

## 6. 표정 표시 (Q3, Q7)

### 6.1 문법과 고정 목록

- 문법: **`[[mood:이름]]`**. 답의 어디에나 올 수 있다. 말하다가 표정이 바뀌는 곳에 둔다.
- **표정 목록(서버가 정함, Q7):** `neutral`, `happy`, `excited`, `sad`, `surprised`, `thinking`, `embarrassed`, `worried`.
  - 목록은 `prompt.api`의 상수 `MOODS`이고, `GET /api/device`가 앱에 알려 준다.
  - 목록을 바꾸는 것은 계약 변경이다(서버와 앱이 함께 바뀜). 더하기만 하면 옛 앱은 모르는 이름을 `neutral`로 그린다(§8.2).
- **앱 자신의 상태는 표정이 아니다.** 듣는 중(입력창 열림), 기다리는 중(작업은 시작했고 글이 아직 없음), 말하는 중(글이 흘러옴), 가만히, 졸림(오래 입력 없음)은 앱이 사건을 보고 스스로 정한다(§8.3). 모델은 이것을 쓰지 않는다.

### 6.2 서버가 떼어 내기

- **Data는 모델이 쓴 원문을 그대로 둔다**(표시 포함). 모델의 과거 답에도 표시가 그대로 보이므로, 모델은 자기가 쓴 형식을 계속 본다.
- 순수 함수 `split_moods(text) -> (plain, marks)`를 **`prompt.api`**에 둔다. 모델에게 말한 문법이 거기 있기 때문이다. `marks`는 `(plain 안의 위치, 이름)` 목록이다.
  - 목록에 없는 이름도 떼어 내고 `neutral`로 바꾼다.
  - 코드 블록과 인라인 코드 안의 표시는 그대로 둔다(글로 보임).
- **API가 주는 것(모든 대화):**
  - `events` 응답: 최종 답 이벤트에 `display: {text, moods}`를 더한다. Web과 앱은 `display.text`를 그린다. Web의 Copy는 `display.text`를 복사한다.
  - `live` SSE: 서버가 `[[`로 시작하는 조각을 `]]`까지 또는 40자까지 붙잡아 두었다가, 표시면 `event: mood {"mood": "happy", "at": <지금까지 보낸 글자 수>}`로, 아니면 글로 보낸다. 글 조각은 지금처럼 간다.
- **대화 검색(V3-P72):** `event_text`에 넣는 글에서 표시를 뺀다.
- 표정은 **Tool이 아니다.** Tool로 하면 표정 하나마다 LLM 호출이 한 번 더 든다.

## 7. 사건 흐름과 대화 고르기

### 7.1 사건 흐름

`GET /api/events` — 주체 단위 SSE. 기기(기기 토큰)와 Web(세션 쿠키)이 같이 쓴다.

- 보내는 사건:
  - `hello {device, moods, recent_conversation, activity}`: 열렸을 때. `device`는 Web이면 `null`.
  - `activity {working, waiting}`: 일하는 중인 대화와 답을 기다리는 대화의 id 목록(V3-P80과 같은 규칙). 바뀔 때마다.
  - `job {conversation, state: "started"|"ended", sent_from}`: 어느 대화의 작업이 시작·끝남. `sent_from`은 기기 id, `"web"`, `"cli"` 중 하나이거나 없음.
  - `conversation {conversation}`: 그 대화에 새 이벤트가 생김(다른 기기나 Web에서 보낸 것 포함).
  - `current {device}`: 지금 기기가 바뀜(자리 비움이면 `null`).
  - `revoked`: 이 기기가 끊김. 바로 닫는다.
  - 30초마다 주석 줄(연결 유지).
  - `revoked`는 그 기기의 흐름에만 간다. 나머지는 주체의 모든 흐름에 간다.
- 답의 글과 표정은 그 대화의 `live` SSE(§6.2)로 받는다. 사건 흐름은 알림만 한다.
- 연결당 5분(Caddy `write` 600s 안), 받는 쪽이 바로 다시 연다. 기기는 다시 열기 사이의 짧은 공백에 "연결됨"이 끊기지 않는다(30초 유예).
- 주체당 사건 흐름은 16개까지다(기기와 Web 탭을 합쳐).
- **Web:** 사이드바·탭 제목의 활동 표시(V3-P80)를 `activity` 사건으로 그린다. 5초 활동 읽기는 흐름이 끊긴 동안만 쓴다. `GET /api/conversations/activity`는 남긴다.

### 7.2 어느 대화에서 말하나

- **최근 대화:** 휴지통 밖에서 마지막 사용자 메시지가 가장 늦은 대화. `hello`와 `GET /api/device`가 알려 준다.
- 앱은 **켜질 때와 이 기기가 다시 지금 기기가 될 때**(`current` 사건, 다른 기기나 자리 비움에서 돌아옴) 최근 대화로 옮긴다. 입력 중이면 옮기지 않는다.
  - 그래서 데스크톱에서 하던 말을 노트북에 와서 이어 간다.
- 앱에서 **새 대화**를 열 수 있고, **대화 목록**에서 고를 수 있다.
- 지금 대화가 작업 중이면(`BUSY`) 앱은 그 사실을 보이고, 새 대화로 말을 걸 수 있게 한다.

### 7.3 답이 나타나는 곳 (Q4)

- 메시지를 **보낸 기기**에서 캐릭터가 말한다(말풍선과 표정).
- 다른 기기는 기록만 갱신한다. 캐릭터가 말하지 않는다.
- Web에서 보냈으면 Web에만 보인다.

## 8. 앱 계약 (로컬 에이전트용)

Windows 앱은 소유자의 로컬 에이전트가 `clients/windows/`에 만든다. 앱 기술, 애니메이션, 캐릭터 형식, 화면 배치는 로컬 에이전트가 정한다. 아래만 지킨다.

### 8.1 반드시

1. **토큰:** 기기 토큰은 OS 보관소(자격 증명 관리자 또는 DPAPI)에만 둔다. 파일·로그·화면에 쓰지 않는다.
2. **서버 요청은 네이티브 쪽에서:** 서버는 CORS를 열지 않는다. 웹 기술로 화면을 만들면 화면 쪽은 서버에 직접 요청하지 않고, 네이티브 쪽(또는 같은 프로세스의 HTTP 클라이언트)이 대신 요청한다. 화면 쪽은 토큰을 보지 않는다.
3. **원격 코드 금지:** 화면은 원격 스크립트·스타일을 불러오지 않는다. 모델이 쓴 글은 HTML로 넣지 않고 글(또는 안전하게 다듬은 마크다운)로 그린다.
4. **서버 주소:** 사용자가 넣은 HTTPS 주소만 쓴다. 인증서 검사를 끄지 않는다.
5. **접속 신호:** §5.1대로 보낸다. `idle_seconds`는 시스템 전체의 입력으로 잰다.
6. **사건 흐름:** 늘 열어 두고 끊기면 다시 연다(1초부터 30초까지 늘려 가며). `revoked`면 토큰을 지우고 짝짓기 화면으로 간다.
7. **표정:** `GET /api/device`의 목록에 있는 표정을 모두 그릴 수 있어야 한다. 모르는 이름은 `neutral`로 그린다.
8. **글만 보낸다**(1단계). 메시지는 `POST /api/conversations/{id}/messages {text}`.
9. **오류:** 401은 다시 짝짓기, 403 `device_scope`는 앱의 잘못이므로 로그에 남긴다, 409 `BUSY`는 사용자에게 보인다.

### 8.2 해야 할 것

- **말풍선:** `live`의 글을 흘러오는 대로 그리고, `mood` 사건의 `at` 위치에 오면 표정을 바꾼다. 다 읽을 시간이 지나면 접는다. 긴 답은 펼쳐 보기나 "Web에서 열기"를 둔다.
- **입력:** 캐릭터 클릭 또는 단축키로 입력창. Enter로 보낸다.
- **멈춤:** 답하는 중 멈춤 → `cancel`.
- **질문(`ask_user`):** 질문을 말풍선에 보이고 입력창을 연다. 답은 보통 메시지 경로로 간다(Web과 같음).
- **트레이:** 보이기·숨기기, 다시 연결, Web 열기, 끝내기, 연결 상태.
- **연결이 끊기면:** 캐릭터를 흐리게 하고 입력을 막는다. 보내지 못한 입력은 앱 안에 남긴다.

### 8.3 앱이 혼자 하는 것 (LLM을 부르지 않음)

- 가만히 있을 때의 움직임(숨쉬기, 깜빡임 등), 듣는 중·기다리는 중·말하는 중의 움직임(§6.1), 오래 입력이 없을 때의 졸림.
- 이것들은 서버에 알리지 않는다.

### 8.4 참조 클라이언트

- 서버 2부와 함께 **가짜 기기 스크립트**(`scripts/device_client.py`)를 둔다. 짝짓기 → 사건 흐름 → 접속 신호 → 메시지 → `live`의 글과 `mood`를 순서대로 해 보인다. CI에서 돌고, 로컬 에이전트에게는 실행되는 계약 예시가 된다.
- `clients/windows/README.md`(로컬 에이전트가 씀)에 빌드·실행 방법을 둔다. Python 검사(mypy, pytest, import 규칙)는 `clients/`를 보지 않는다.

## 9. 애니메이션을 먼저 만들 때

애니메이션은 서버와 독립이라 **지금 바로 시작해도 된다.** 서버와 맞출 것은 다음 둘뿐이다.
- 표정 이름 8개(§6.1).
- 앱 상태 다섯(듣는 중, 기다리는 중, 말하는 중, 가만히, 졸림)은 앱 안의 일이다.

서버가 준비되기 전에는 가짜 사건(표정이 섞인 글)을 앱 안에서 흘려 시험한다.

## 10. 모듈 경계

| 모듈 | 바뀌는 것 |
| --- | --- |
| `data` | v14: `devices`, `UserMessage.origin_device`. `DeviceStore` sub-port. `event_text`에서 표시 빼기 |
| `prompt` | `PromptInputs.now`·`presence`, `state_block`의 `now`·`presence`, 쉬었다 온 메시지의 시각 줄, `COMPANION_NOTE`, `MOODS`, `split_moods` |
| `presence` (새, 계층 2) | `Devices`·`Presence`·`EventHub`(§2.2). `common`·`data.api`만 |
| `control` | `PresenceSource` 포트(api), Driver가 턴 시작에 시계와 함께 읽음 |
| `bootstrap` | presence 조립, `PresenceSource` 구현, 작업 시작·끝과 새 이벤트를 `EventHub`에 이음, 최근 대화 |
| `interfaces/web` | 짝짓기·기기 경로, `Caller`와 허용 목록, `GET /api/events`, `events`의 `display`, `live`의 `mood`, Web 설정 화면 Devices, 화면이 `display.text`를 그림, 활동 표시를 사건 흐름으로 |
| `clients/windows` | 새 앱(로컬 에이전트) |

- `split_moods`와 `MOODS`는 `prompt.api`에 있어 `interfaces`가 부를 수 있다(규칙 5).
- `scripts/check_imports.py`의 `LAYERS`에 `presence: 2`, `ONLY_FROM`에 `presence: {common, data}`를 더한다.
- Provider, Tool, Environment, runner, sandbox, MCP는 바뀌지 않는다.

## 11. 다음 단계 (방향, 이 패킷에서 구현하지 않음)

1. **먼저 말 걸기:** 서버의 계기(정한 시각, 사용자가 돌아옴, 예정된 일)가 턴을 연다. 사건 흐름으로 지금 기기에 알린다.
2. **메모리 2단계와 일화 기억:** 대화를 나누어 쓰므로 대화 사이의 연속성은 메모리가 맡는다. 자동 추출과 일화(언제 무엇을 함께 했는지)를 더한다.
3. **로컬 파일:** PC가 먼저 접속하는 runner 연결(runner 프로토콜을 바이트 전송·기기 인증으로). 처음엔 읽기만.
4. **화면 캡처:** 모델이 부르는 Tool. 캡처 중 표시. 이미지는 Data의 파일 저장소(V3-P67).
5. 앱의 첨부·붙여넣기, 음성, 앱 자동 업데이트.

## 12. 소유자 결정

2026-10-30 소유자 결정: Q1·Q2는 로컬 에이전트에게 맡김, Q5·Q6은 rev. 2의 방향대로, Q3·Q4·Q7은 권고대로("전부 괜찮아").

| # | 질문 | 선택지 | 권고 → 결정 |
| --- | --- | --- | --- |
| Q1 | Windows 앱 기술 | A. Tauri 2 / B. Electron / C. .NET + WebView2 | rev. 1 권고는 A → **로컬 에이전트가 정함**(§8.1의 조건 안에서) |
| Q2 | 캐릭터 형식 | A. 표정마다 PNG / B. Live2D / C. VRM | rev. 1 권고는 A → **로컬 에이전트가 정함**. 서버와는 표정 이름만 맞춤(§9) |
| Q3 | 표정 표시의 위치 | **A. 답의 어디에나(바뀌는 곳마다)** / B. 답 맨 앞에 하나만 | **A**. 말하는 중에 표정이 바뀌어야 살아 보인다. 서버가 떼어 내므로 화면에는 보이지 않는다. → 권고대로 |
| Q4 | 답이 나타나는 곳 | **A. 보낸 기기에서만 말함, 다른 기기는 기록만** / B. 지금 기기에서 말함 / C. 연결된 모든 기기에서 말함 | **A**. 말을 건 곳에서 답이 오는 게 자연스럽다. C는 PC 두 대가 동시에 말한다. → 권고대로 |
| Q5 | 모델이 아는 시각 | A. 지금 시각 + 30분 넘게 쉬었다 온 메시지의 시각 줄 / B. 지금 시각만 / C. 모든 메시지 앞에 시각 | rev. 1은 반려 대화에만 A → **모든 대화에 A**(소유자: "시간을 알려줄 방법이 있어야 할 것 같다") |
| Q6 | 반려 대화 | A. 주체마다 하나 / B. 여러 개 / C. 구분 없음 | rev. 1 권고는 A → **C. 구분 없음**. HIMARI 자체가 반려 에이전트, 앱은 최근 대화를 이어 감(§7.2) |
| Q7 | 표정 목록 | **`neutral`, `happy`, `excited`, `sad`, `surprised`, `thinking`, `embarrassed`, `worried`** / 더 적게(5개) / 더 많이 | **8개**. 대화에서 자주 필요한 결을 덮으면서 애니메이션 부담이 크지 않다. 더하기는 뒤에 호환되게 할 수 있다(§6.1). → 권고대로 |

## 13. 검증 계획

- **pytest(서버):**
  - 짝짓기: 코드 만료, 한 번만, 5번 실패, 동시에 하나, 토큰 해시만 저장.
  - 권한: 허용 목록 밖 경로 403 `device_scope`, 끊은 토큰 401과 SSE `revoked`.
  - presence: 지금 기기 고르기(입력이 가장 늦은 연결된 기기, 10분), 자리 비움, 다시 열기 유예 30초, `current` 사건.
  - Prompt: 모든 대화의 `now`, 시각 줄(첫 메시지, 30분 경계, 같은 입력 → 같은 줄, 압축 요약 요청), `presence`는 스위치와 기기가 있을 때만, `COMPANION_NOTE`는 스위치를 따름.
  - `split_moods`: 시험 벡터(여러 표시, 목록 밖 이름, 코드 안, 깨진 `[[`, 한국어 사이).
  - `live`: 조각 경계에 걸친 표시(`[[mo` + `od:happy]]`), 40자 넘는 `[[`는 글로, `at` 위치.
  - 최근 대화 고르기(휴지통 제외).
  - Data v13 → v14 업그레이드와 백업, `origin_device` 없는 옛 이벤트.
- **가짜 기기 스크립트**(§8.4): CI에서 실제 uvicorn 서버와 Scripted Provider로 돈다.
- **ui_smoke:** Web이 표시를 떼어 그리고 Copy가 `display.text`인지, Devices 화면.
- **Windows 앱:** 로컬 에이전트가 자기 환경에서 확인한다. 소유자가 실제 PC 두 대 사이에서 이어 말하기를 확인한다.
- **실제 모델 확인:** 표정 표시를 문법대로 쓰는지, 기기에서 보낸 메시지에 짧게 답하는지(모델마다 몇 턴).

## 14. 구현 단계

1. **서버 1부:** Data v14, `DeviceStore`, presence 모듈의 `Devices`, 짝짓기·기기 토큰·`Caller`·허용 목록, Web 설정 화면 Devices.
2. **서버 2부:** 시각(`now`, 시각 줄 — 모든 대화), presence의 `Presence`·`EventHub`와 `GET /api/events`(Web 활동 표시 포함), `PresenceSource`, `presence` 항목, `COMPANION_NOTE`·`MOODS`·`split_moods`, `events`의 `display`와 `live`의 `mood`, Web이 `display.text`를 그림, 가짜 기기 스크립트.
3. **Windows 앱(로컬 에이전트):** §8의 계약대로. 애니메이션은 서버와 상관없이 지금 시작할 수 있다(§9).

배포는 1·2부를 함께 한다(v14 업그레이드). 시각(2부의 일부)은 따로 먼저 배포해도 된다(DB 변경 없음).
