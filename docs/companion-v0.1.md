# 반려 에이전트 1단계: Windows 앱과 기기 연결 (V3-P83 설계 초안 v0.1)

상태: **Proposed**. 소유자 결정 Q1~Q6 대기(§12).

> 소유자(2026-10-30): "지금 openai나 클로드의 범용 에이전틱 성능이 뛰어나서 히마리를 업무용으로 개발하는건 효용이 적다는 느낌이었어. 그래서 반려 에이전트라는 기능을 만드려는 생각인데"
> - Windows에서 동작하는 반려 에이전트. 두뇌는 서버의 HIMARI이고, 사용자가 접속한 것으로 확인된 기기에서 상호작용한다.
> - Windows 컴퓨터 사이에서 작업이 이어지면 좋겠다. 에이전트는 사용자가 어느 컴퓨터에서 접속했는지 구분할 수 있어야 한다.
> - 오프라인은 필요 없다(모델은 API). 서버는 지금처럼 VPN으로 접속한다.
> - Web은 이런 대화를 관리하는 도구가 된다.
>
> 같은 날 합의한 1단계 범위: **Windows 앱 + 기기 연결 + 표정 메타데이터**.

앞선 결정
- **Run = LLM 호출 한 번**(V3-001). 모델이 Tool을 부르면 결과를 받아 한 번 더 부른다. 그래서 표정은 Tool이 아니라 답의 표시로 보낸다(§6).
- **Current State**(prompt-composition §1): 요청 끝의 `<runtime_state>` JSON. 캐시를 깨지 않는 최신 상태의 자리다. 기기와 시각은 여기에 넣는다(§5).
- **V3-P69 사용자 지시문, V3-P71 메모리:** 턴이 시작될 때 한 번 읽고 그 턴 내내 같다. 기기 상태도 같은 규칙을 따른다(§5.3).
- **V3-P73 스트리밍:** `GET /api/conversations/{id}/live` SSE. 앱의 말풍선은 이것을 쓴다(§7).
- **운영자 인증:** `operator.secret`의 bearer 토큰 또는 Web 세션 쿠키. P0 주체는 `operator` 하나다. 기기 토큰은 그 주체의 **좁은 권한**이다(§4).
- **CORS를 열지 않는다**(Web 보안 계층). 앱의 화면(WebView)은 서버에 직접 요청하지 않고, 앱의 네이티브 쪽이 대신 요청한다(§8.2).

## 1. 범위

**이 패킷에서 한다**
1. **기기 등록:** Web에서 짝짓기 코드를 만들고, 앱이 코드를 기기 토큰으로 바꾼다. 기기 목록, 이름 바꾸기, 끊기(§4).
2. **기기 토큰의 좁은 권한:** 반려 대화와 접속 신호만 쓴다(§4.3).
3. **반려 대화:** 주체마다 하나. 반려 성격 글과 반려 안내문이 이 대화에만 들어간다(§3).
4. **접속 신호와 "지금 기기":** 앱이 입력 활동을 알리고, 서버가 지금 사용자가 있는 기기를 정한다. 모델은 Current State에서 본다(§5).
5. **시각:** 지금 시각(기기의 시간대)과 오래 쉬었다 온 메시지의 시각을 모델이 안다(§5.4).
6. **표정 표시:** 답 안의 `[[mood:이름]]`. 서버가 떼어 내 표정 사건으로 보낸다(§6).
7. **Windows 앱:** 트레이, 투명한 캐릭터 창, 말풍선, 입력, 표정 그림 바꾸기, 가만히 있을 때의 움직임(§8).
8. **기본 캐릭터 한 벌:** 표정마다 그림 한 장과 목록 파일(§9).

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
            │ Data v14: devices, companion (+ 기존 전부)       │
            │ Presence(메모리 안): 기기별 연결·마지막 입력     │
            └──────┬─────────────────────────┬──────────────┘
   기기 토큰 HTTPS·SSE (VPN)                  │ 세션 쿠키 HTTPS (VPN)
                   │                          │
   ┌───────────────┴─────────────┐   ┌────────┴──────────────┐
   │ Windows 앱                   │   │ Web                    │
   │ 네이티브 쪽: 토큰, HTTP·SSE,  │   │ 대화 기록, 기억, 지시문  │
   │   입력 활동(GetLastInputInfo)│   │ 기기, 반려 설정          │
   │ 화면(WebView): 캐릭터, 말풍선 │   │ 반려 대화도 여기서 이어감 │
   └─────────────────────────────┘   └───────────────────────┘
```

- 서버가 하나의 원본이다. 대화, 기억, 반려 설정은 모두 서버의 Data에 있다. 여러 PC와 Web이 같은 반려 대화를 이어 쓴다.
- 지연: PC↔서버 구간이 더해진다. 모델의 첫 토큰(0.5~수 초)에 비해 작다. 가만히 있을 때의 움직임처럼 즉각적이어야 하는 반응은 앱이 혼자 처리한다(§8.4).

## 3. 반려 대화 (Q6)

- **주체마다 반려 대화 하나.** Data `companion` 표의 `conversation_id`가 가리킨다.
- **처음 만들기:** 기기나 Web이 반려 대화를 처음 열 때 서버가 만든다. 제목은 캐릭터 이름이다.
- **보통 대화와 같은 것:** 모델 선택, Tool, 메모리, 압축, 검색. Workspace도 보통 대화처럼 붙는다.
- **다른 것:**
  - 시스템 메시지에 고정 문장 `COMPANION_NOTE`(§6.1)와 소유자가 쓴 **성격 글**(`persona`)이 들어간다. 성격 글은 반려 대화에만 들어간다(V3-P69 지시문은 모든 대화에 들어감).
  - Current State에 `companion` 항목이 들어간다(§5).
- **Web:** 사이드바 맨 위에 고정하고 표시를 붙인다. 휴지통으로 보내려면 한 번 더 묻는다. 보내면 `companion.conversation_id`를 비우고, 다음에 열 때 새로 만든다. 지운 대화의 기억 처리는 V3-P65·P71 규칙 그대로다.
- **성격 글이 바뀌면:** 다음 턴부터 들어간다. 시스템 메시지가 바뀌므로 그 턴은 캐시를 다시 만든다(드문 일이라 받아들인다).

## 4. 기기

### 4.1 짝짓기

1. Web 설정 → **Devices** → "Add device". 이름을 적는다(예: "데스크톱").
2. 서버가 **짝짓기 코드**를 보인다. 8글자(헷갈리는 글자를 뺀 32자 알파벳), 10분, 한 번만 쓴다. 동시에 하나만 살아 있다.
3. 앱 첫 실행: 서버 주소와 코드를 넣는다 → `POST /api/devices/pair {code}`.
4. 서버는 **기기 토큰**(32바이트 무작위, URL-safe base64)을 **한 번만** 돌려준다. 서버에는 SHA-256 해시만 둔다.
5. 앱의 네이티브 쪽이 토큰을 Windows 자격 증명 관리자(DPAPI)에 둔다. 화면(WebView) 쪽은 토큰을 보지 못한다.

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
CREATE TABLE companion (
    owner TEXT PRIMARY KEY,
    conversation_id TEXT,             -- 휴지통에 가면 NULL
    persona TEXT NOT NULL DEFAULT '', -- 성격 글, 8,000자까지
    character TEXT NOT NULL DEFAULT 'default',
    revision INTEGER NOT NULL
) STRICT;
```

- 공개 API: Data의 새 sub-port **`DeviceStore`**(만들기, 짝짓기, 토큰으로 찾기, 목록, 이름 바꾸기, 끊기, `touch`)와 **`CompanionStore`**(읽기, 성격 글·캐릭터 쓰기, 대화 정하기).
- **사용자 메시지의 보낸 기기:** `UserMessage` 페이로드에 선택 필드 `origin_device`(기기 id)를 더한다. Web·CLI에서 보낸 것은 없다. 코덱의 선택 필드라 옛 이벤트는 그대로 읽힌다. 모델 입력의 과거 메시지에는 넣지 않는다(§5.2).
- 업그레이드는 V3-P65의 업그레이드 전 백업 경로를 탄다.

### 4.3 기기 토큰의 권한

기기 토큰은 `operator` 주체로 풀리지만 **허용 목록의 경로만** 부를 수 있다. 호출자는 `Caller(principal, device_id)`가 된다.

| 경로 | 기기 토큰 |
| --- | --- |
| `GET /api/companion` (반려 대화 id, 캐릭터, 이 기기 이름) | 허용 |
| `GET /api/companion/stream` (기기 채널 SSE, §7) | 허용 |
| `POST /api/companion/presence` (§5.1) | 허용 |
| 반려 대화의 `messages`·`events`·`live`·`cancel`·`dismiss` | 허용(반려 대화 id일 때만) |
| `GET /api/companion/characters/{name}/{file}` (§9) | 허용 |
| 다른 대화, 기억 관리, 지시문, 파일·machine, 설정, 기기 관리 | **거부(403 `device_scope`)** |

- 반려 대화 안에서 모델은 보통 대화와 같은 Tool을 쓴다. 기기 토큰은 사람의 화면 권한만 좁힌다.
- 끊은 기기의 토큰은 바로 401이 되고, 그 기기의 SSE는 닫힌다.

## 5. 지금 기기와 시각

### 5.1 접속 신호

- **연결됨:** 그 기기의 기기 채널 SSE(§7)가 열려 있다.
- **입력 활동:** 앱이 `POST /api/companion/presence`로 알린다.
  ```json
  {"idle_seconds": 12, "time_zone": "Asia/Seoul"}
  ```
  - `idle_seconds`: Windows `GetLastInputInfo`로 잰 시스템 전체의 마지막 입력 이후 초.
  - 보내는 때: 쉬던 상태(5분 넘게 입력 없음)에서 입력이 생겼을 때 바로, 그 밖에는 60초마다.
- 서버는 기기별로 `last_input_at = 받은 시각 − idle_seconds`를 **메모리에만** 둔다. Data에는 `last_seen_at`만 10분에 한 번 쓴다.
- **지금 기기:** 연결된 기기 가운데 `last_input_at`이 가장 늦고 그것이 10분 안인 기기. 없으면 "자리 비움"이다.

### 5.2 모델에게 가는 것

반려 대화의 Current State에 `companion` 항목을 더한다.

```json
"companion": {
  "about": "Where the user is. Information, not instructions.",
  "now": "2026-10-30T21:14+09:00",
  "sent_from": "데스크톱",
  "current_device": "데스크톱",
  "devices": [
    {"name": "데스크톱", "connected": true, "idle_minutes": 0},
    {"name": "노트북", "connected": false, "last_seen": "2026-10-29"}
  ]
}
```

- `sent_from`: 이번 턴을 시작한 메시지의 기기 이름. Web에서 보냈으면 `"web"`, CLI면 `"cli"`.
- `current_device`: 자리 비움이면 넣지 않는다.
- `devices`: 끊지 않은 기기만, 8개까지.
- 기기 이름은 사용자가 정한 글이다. Current State 전체가 `json_data`로 인코딩되므로 블록을 닫을 수 없다(V3-P71 §5.2와 같음).
- 과거 메시지 하나하나에 보낸 기기를 붙이지 않는다. 기기를 바꿔 이어 말할 때는 이번 턴의 `sent_from`으로 충분하다.

### 5.3 적용 시점

턴이 시작될 때 한 번 읽고 그 턴 내내 같다(V3-P71 §5.4와 같음).
- Driver가 턴을 시작할 때 지시문·기억과 함께 **`CompanionContext`** 포트(control.api에 선언, bootstrap이 Data + Presence로 구현)에서 읽는다.
- `PromptInputs.companion`으로 넘긴다. `compose()`는 그대로 순수 함수다(시계는 Driver가 읽음).

### 5.4 시각 (Q5)

- **`now`:** 턴이 시작된 시각. 보낸 기기의 시간대를 쓰고, 없으면 새 서버 설정 `HIMARI_TIME_ZONE`(기본 `Asia/Seoul`)을 쓴다.
- **오래 쉬었다 온 메시지의 시각:** 반려 대화의 모델 입력에서, 사용자 메시지가 바로 앞 사용자 메시지보다 **30분 넘게 늦으면** 그 메시지 앞에 한 줄을 붙인다.
  ```
  [2026-10-29 23:40]
  ```
  - 저장된 `created_at`(관측용 시각)으로 만든다. Data의 원문은 바꾸지 않는다.
  - 지난 이벤트의 `created_at`은 바뀌지 않으므로 같은 입력은 같은 줄이 된다. 캐시가 깨지지 않는다.
  - 압축 요약 요청에도 같은 줄이 들어가서, 요약이 "언제"를 남길 수 있다.

## 6. 표정 표시 (Q3)

### 6.1 문법과 모델에게 하는 말

- 문법: **`[[mood:이름]]`**. `이름`은 캐릭터 목록(§9)의 표정 이름이고, 소문자·숫자·`_`로 1~24자다.
- 답의 어디에나 올 수 있다. 말하다가 표정이 바뀌는 곳에 둔다.
- `COMPANION_NOTE`(반려 대화의 시스템 메시지)가 문법과 표정 목록을 말한다. 대략 다음과 같다.
  ```
  You are present on the user's computer as a character. Keep replies short and spoken.
  Show your expression with [[mood:NAME]] where it changes, e.g. "[[mood:happy]]잘 됐다!".
  Moods: neutral, happy, sad, surprised, thinking, embarrassed. Use at most one per sentence.
  ```
- 목록은 캐릭터마다 다를 수 있다. 캐릭터를 바꾸면 시스템 메시지가 바뀐다(드문 일).

### 6.2 서버가 떼어 내기

- **Data는 모델이 쓴 원문을 그대로 둔다**(표시 포함). 모델의 과거 답에도 표시가 그대로 보이므로, 모델은 자기가 쓴 형식을 계속 본다.
- 순수 함수 `split_moods(text) -> (plain, marks)`를 **`prompt.api`**에 둔다. 모델에게 말한 문법이 거기 있기 때문이다. `marks`는 `(plain 안의 위치, 이름)` 목록이다.
  - 목록에 없는 이름도 떼어 내고 `neutral`로 바꾼다.
  - 코드 블록과 인라인 코드 안의 표시는 그대로 둔다(글로 보임).
- **API가 주는 것:**
  - `events` 응답: 반려 대화의 최종 답 이벤트에 `display: {text, moods}`를 더한다. Web과 앱은 `display.text`를 그린다. Copy는 `display.text`를 복사한다.
  - `live` SSE: 서버가 `[[`로 시작하는 조각을 `]]`까지 또는 40자까지 붙잡아 두었다가, 표시면 `event: mood`로, 아니면 글로 보낸다. 글 조각은 `delta`로 간다.
- **대화 검색(V3-P72):** `event_text`에 넣는 글에서 표시를 뺀다.
- 표정은 **Tool이 아니다.** Tool로 하면 표정 하나마다 LLM 호출이 한 번 더 든다.

## 7. 기기 채널

`GET /api/companion/stream` — 기기마다 하나씩 열어 두는 SSE.

- 보내는 사건:
  - `hello {device, companion_conversation, character}`: 열렸을 때.
  - `job {state: "started"|"ended", sent_from}`: 반려 대화의 작업이 시작·끝남.
  - `conversation {changed: true}`: 반려 대화에 새 이벤트가 생김(다른 기기나 Web에서 보낸 것 포함).
  - `revoked`: 이 기기가 끊김. 바로 닫는다.
  - 30초마다 주석 줄(연결 유지).
- 답의 글과 표정은 기존 `live` SSE(§6.2)로 받는다. 기기 채널은 알림만 한다.
- 연결당 5분(Caddy `write` 600s 안), 앱이 바로 다시 연다. 다시 열기 사이의 짧은 공백은 "연결됨"을 끊지 않는다(30초 유예).
- 주체당 기기 채널은 16개까지다.

### 7.1 답이 나타나는 곳 (Q4)

- 메시지를 **보낸 기기**에서 캐릭터가 말한다(말풍선과 표정).
- 다른 기기는 기록만 갱신한다. 캐릭터가 말하지 않는다.
- Web에서 보냈으면 Web에만 보인다.

## 8. Windows 앱 (Q1)

### 8.1 기술

- **Tauri 2**(Rust 네이티브 쪽 + WebView2 화면).
- 저장소 안 `clients/windows/`에 둔다. 서버와 앱의 계약이 한 PR에서 같이 바뀐다.
- 빌드와 시험은 GitHub Actions의 Windows 러너에서 한다. 이 개발 서버(Linux)에서는 빌드하지 않는다.

### 8.2 네이티브 쪽과 화면 쪽

- **네이티브 쪽(Rust):**
  - 기기 토큰 보관(자격 증명 관리자)
  - 서버와의 HTTP·SSE 전부
  - `GetLastInputInfo`로 입력 활동 재기
  - 트레이 메뉴, 창 위치 기억, 자동 시작
- **화면 쪽(WebView):** 캐릭터 그림, 말풍선, 입력창.
  - 서버에 직접 요청하지 않는다(서버는 CORS를 열지 않음). Tauri 명령·이벤트로 네이티브 쪽과만 말한다.
  - 원격 콘텐츠를 불러오지 않는다. CSP `default-src 'self'`.
- 캐릭터 그림은 네이티브 쪽이 서버에서 받아 앱 캐시 폴더에 두고, 화면 쪽에는 로컬 자산으로 넘긴다.

### 8.3 화면

- **캐릭터 창:** 테두리 없음, 투명, 항상 위, 작업 표시줄에 안 보임. 캐릭터를 끌어 옮긴다. 캐릭터 밖의 투명한 곳은 클릭이 아래 창으로 지나간다.
- **말풍선:** 답이 흘러오는 대로 그린다. 표정은 `mood` 사건의 위치에서 바뀐다. 다 읽을 시간이 지나면 접힌다.
- **입력:** 캐릭터를 클릭하거나 단축키(기본 `Ctrl+Alt+Space`)로 작은 입력창이 열린다. Enter로 보낸다. 1단계는 글만 보낸다(첨부는 Web에서).
- **멈춤:** 답하는 중 말풍선의 ■ → `cancel`.
- **질문(`ask_user`):** 말풍선에 질문을 보이고 입력창을 연다. 답은 보통 메시지 경로로 간다(Web과 같음).
- **트레이:** 보이기·숨기기, 다시 연결, Web 열기, 끝내기. 연결 상태 점(연결됨/끊김/끊긴 기기).
- **연결이 끊기면:** 캐릭터를 흐리게 하고 입력을 막는다. 보내지 못한 입력은 앱 안에 남긴다.

### 8.4 가만히 있을 때

- 숨쉬기(살짝 위아래), 깜빡이기(그림에 눈 감은 장이 있으면)는 앱이 혼자 한다. LLM을 부르지 않는다.
- 사용자가 5분 넘게 입력이 없으면 `idle` 표정(목록에 있을 때)으로 바꾼다. 이것도 앱이 혼자 한다.

## 9. 캐릭터 묶음 (Q2)

- 서버 `<data_root>/companion/characters/<name>/`에 둔다. 1단계에는 기본 캐릭터 `default` 하나를 저장소에 넣어 배포한다.
- `character.json`:
  ```json
  {
    "name": "Himari",
    "format": "sprites-v1",
    "size": [320, 480],
    "moods": {
      "neutral": "neutral.png",
      "happy": "happy.png",
      "sad": "sad.png",
      "surprised": "surprised.png",
      "thinking": "thinking.png",
      "embarrassed": "embarrassed.png"
    },
    "blink": {"neutral": "neutral_blink.png"},
    "idle": "neutral"
  }
  ```
- 그림은 투명 PNG이고, 한 장에 2 MiB, 한 벌에 32 MiB까지다.
- `format`을 두어 나중에 Live2D나 VRM을 다른 값으로 더할 수 있게 한다.
- 기본 그림은 임시 그림이다. 실제 캐릭터 그림은 소유자가 정한다(§12 Q2).

## 10. 모듈 경계

| 모듈 | 바뀌는 것 |
| --- | --- |
| `data` | v14: `devices`, `companion`, `UserMessage.origin_device`. `DeviceStore`·`CompanionStore` sub-port. `event_text`에서 표시 빼기 |
| `prompt` | `PromptInputs.companion`, `state_block`의 `companion` 항목, `COMPANION_NOTE`와 성격 글 자리, 쉬었다 온 메시지의 시각 줄, `split_moods` |
| `control` | `CompanionContext` 포트(api), Driver가 턴 시작에 읽음 |
| `bootstrap` | `Presence`(메모리 안), `CompanionContext` 구현, 반려 대화 만들기 |
| `interfaces/web` | 기기·짝짓기·반려 경로, `Caller`와 허용 목록, 기기 채널 SSE, `live`의 표정 사건, Web 설정 화면(Devices, Companion), 사이드바 고정 |
| `clients/windows` | 새 앱 |

- `split_moods`는 `prompt.api`의 순수 함수라 `interfaces`가 부를 수 있다(규칙 5).
- Provider, Tool, Environment, runner, sandbox, MCP는 바뀌지 않는다.

## 11. 다음 단계 (방향, 이 패킷에서 구현하지 않음)

1. **먼저 말 걸기:** 서버의 계기(정한 시각, 사용자가 돌아옴, 예정된 일)가 반려 대화의 턴을 연다. 기기 채널로 지금 기기에 알린다.
2. **로컬 파일:** PC가 먼저 접속하는 runner 연결(runner 프로토콜을 바이트 전송·기기 인증으로). 처음엔 읽기만.
3. **화면 캡처:** 모델이 부르는 Tool. 캡처 중 표시. 이미지는 Data의 파일 저장소(V3-P67).
4. **메모리 2단계와 일화 기억:** 반려 대화의 세션마다 일화를 떼어 저장하고 검색한다.
5. 음성, 캐릭터 고르기·올리기, 앱 자동 업데이트.

## 12. 소유자 결정

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | Windows 앱 기술 | **A. Tauri 2(Rust + WebView2)** / B. Electron / C. .NET(WPF) + WebView2 | **A**. 앱이 작고(수 MB, 메모리 적음) 투명 창을 지원한다. 화면은 JS라 Web과 같은 방식으로 만든다. Rust는 네이티브 쪽의 얇은 부분(토큰, HTTP, 입력 활동)뿐이다. B는 가장 쉽지만 무겁다(100 MB대, 늘 켜 두는 앱에 부담). |
| Q2 | 1단계 캐릭터 형식 | **A. 표정마다 PNG 한 장(+깜빡임 장)** / B. Live2D / C. 3D(VRM) | **A**. 그림만 있으면 바로 된다. `format` 필드로 B·C를 나중에 더한다. B는 SDK 사용 조건을 따로 봐야 한다. 실제 그림을 어떻게 구할지도 함께 알려 주세요. |
| Q3 | 표정 표시의 위치 | **A. 답의 어디에나(바뀌는 곳마다)** / B. 답 맨 앞에 하나만 | **A**. 말하는 중에 표정이 바뀌어야 살아 보인다. 서버가 떼어 내므로 화면에는 보이지 않는다. |
| Q4 | 답이 나타나는 곳 | **A. 보낸 기기에서만 말함, 다른 기기는 기록만** / B. 지금 기기에서 말함 / C. 연결된 모든 기기에서 말함 | **A**. 말을 건 곳에서 답이 오는 게 자연스럽다. C는 PC 두 대가 동시에 말한다. |
| Q5 | 모델이 아는 시각 | **A. 지금 시각 + 30분 넘게 쉬었다 온 메시지 앞에 시각 줄** / B. 지금 시각만 / C. 모든 메시지 앞에 시각 | **A**. "어제 얘기한 거"를 이해하려면 과거 시각이 필요하다. C는 짧은 대화에서 잡음이다. |
| Q6 | 반려 대화의 수 | **A. 주체마다 하나** / B. 여러 개를 만들고 앱에서 고름 | **A**. 연속성이 목적이라 하나가 맞다. 길어지면 압축이 처리한다(일화 기억은 §11). |

## 13. 검증 계획

- **pytest(서버):**
  - 짝짓기: 코드 만료, 한 번만, 5번 실패, 동시에 하나, 토큰 해시만 저장.
  - 권한: 허용 목록 밖 경로 403, 다른 대화 id 403, 끊은 토큰 401과 SSE 닫힘.
  - Presence: 지금 기기 고르기(입력이 가장 늦은 연결된 기기, 10분), 자리 비움, 다시 열기 유예 30초.
  - Prompt: `companion` 항목, 시각 줄(30분 경계, 같은 입력 → 같은 줄), 반려 대화가 아닌 대화에는 아무것도 없음, `COMPANION_NOTE`와 성격 글은 반려 대화에만.
  - `split_moods`: 시험 벡터(여러 표시, 목록 밖 이름, 코드 안, 깨진 `[[`, 한국어 사이).
  - `live`: 조각 경계에 걸친 표시(`[[mo` + `od:happy]]`), 40자 넘는 `[[`는 글로.
  - Data v13 → v14 업그레이드와 백업, `origin_device` 없는 옛 이벤트.
- **가짜 기기 스크립트(Python):** 짝짓기 → 기기 채널 → 접속 신호 → 메시지 → `live`의 `mood`·`delta`. CI에서 돌린다.
- **Windows 앱:** CI(Windows 러너)에서 빌드. 화면과 동작은 소유자가 실제 PC에서 확인한다(투명 창 클릭 통과, 끌어 옮기기, 트레이, 다시 연결, 두 PC 사이 이어 말하기).
- 실제 모델 확인: 반려 대화에서 표정 표시를 문법대로 쓰는지(모델마다 몇 턴).

## 14. 구현 단계

1. **서버 1부:** Data v14, `DeviceStore`·`CompanionStore`, 짝짓기·기기 토큰·허용 목록, Web 설정 화면(Devices, Companion), 반려 대화 만들기·사이드바 고정.
2. **서버 2부:** Presence와 기기 채널, `CompanionContext`, Current State `companion`, 시각 줄, `COMPANION_NOTE`·성격 글, `split_moods`와 `events`·`live`의 표정, 가짜 기기 스크립트.
3. **Windows 앱:** `clients/windows/` Tauri 앱, 기본 캐릭터 묶음, CI 빌드.

배포는 1·2부를 함께 한다(v14 업그레이드). 앱은 2부 배포 뒤에 설치한다.
