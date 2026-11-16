# Web 다시 디자인: hub 작업 콘솔 (V3-P102 설계 초안 v0.1)

> 2026-11-16 · **설계 초안**, rev. 3(Codex 설계 검수 1·2차 반영) · **W0 구현**(`interfaces/web/hub.py`·`hub_stream.py`) · 설계 기준 v3 `3ada276`(#189 병합), hub `0730c33`(himari-hub #25 병합,
> [사용자 경로 계약 v1](https://github.com/jihunmoon04/himari-hub/blob/main/docs/user-api-v1.md)).
>
> 소유자 요청(2026-11-16): HIMARI v3를 HIMARI hub에 맞게 바꾼다. ① hub를 움직이는 두 경로(MCP 경로와
> user 경로)를 Web에서 모두 쓴다. ② Web이 Antigravity·Codex·Claude Code처럼 작업을 위한 UX로 동작한다.
> 공개된 오픈소스는 적극 참고한다. ③ HIMARI는 hub를 통해 에이전트를 라우팅하고, 간단한 작업과
> 오케스트레이션을 맡고, 복잡한 작업은 hub의 에이전트·프로젝트로 관리하는 통합 비서다.
> 소유자 결정: 프론트엔드 구조는 **장기적으로 유리한 쪽**, 화면은 **상용·공개 UI/UX와 비슷한 느낌**,
> 나머지는 권고대로.

관련: [hub 연결 V3-P94·P95·P100](decisions.md), [Web 기본 V3-P79](web-chat-basics-v0.1.md),
[통합 작업 공간](unified-workspace-v0.1.md), [스트리밍 V3-P73](streaming-v0.1.md),
[인터페이스 능력 V3-P93](interface-capabilities-v0.1.md), [테스트 정책](testing-policy.md).

## 1. 목표와 범위

**목표.** Web 하나에서 다음이 된다.

1. HIMARI와 이야기하고, HIMARI가 hub에 맡긴 일을 대화 안에서 따라간다(**MCP 경로**).
2. hub의 에이전트 세션을 직접 열고, 보고, 보내고, 멈추고, 승인한다(**user 경로**).
3. 둘이 같은 화면 언어를 쓴다: 세션 목록, 대화 기록, 도구 카드, 계획, 변경(diff), 승인함.
4. 데스크톱 브라우저가 먼저이고, 휴대폰에서도 확인·승인·짧은 지시를 할 수 있다.

**범위 밖(이 문서).** HIMARI가 언제 직접 하고 언제 hub에 맡길지 정하는 프롬프트(라우팅 지시문)는
따로 설계한다(§10). hub v1에 없는 기능(슬래시 명령, 에이전트에 첨부, 프로젝트 파일·git 보기, 로그인
중계, 턴 중 끼어들기, 같은 프로젝트 동시 실행)은 hub가 먼저 더해야 하므로 §8에 목록만 둔다.
Windows 반려 앱은 바꾸지 않는다.

## 2. 지금 상태

### 2.1 v3 Web

- 프레임워크 없는 JS다: `app.js` 4,815줄, `app.css` 4,293줄, 그 밖 모듈 7개(`live.js`, `files.js`,
  `rich_text.js`, `drafts.js`, `hub.js` 등). 상태는 DOM과 전역 객체에 흩어져 있고, 검사는 DOM을 흉내 내는
  스모크 스크립트(`scripts/*_smoke.mjs`)다. 화면 하나를 더할 때마다 `app.js`가 커지는 구조다.
- 보안 계층은 좋다: 운영자 로그인, `default-src 'self'` CSP(`security.py`), 외부 CDN 없음(KaTeX 자체 서빙),
  모델이 쓴 글은 안전한 렌더러(`rich_text.js`)로만 그림.
- hub와 닿는 곳은 둘이다.
  - **MCP:** `mcp.json`의 `hub` 서버가 Tool 8개(`hub/hub_delegate`, `hub/hub_task`, …)를 준다. Web에는
    다른 Tool과 같은 한 줄 도구 행으로 보이고, 맡긴 작업이 어느 세션에서 도는지 이어지지 않는다.
  - **user 경로:** `interfaces/web/hub.py`가 `pending`·`answer`·`cancel`만 중계한다(V3-P95, 구석의 승인 창).
- 이름 충돌: `application.hub`는 반려 기기의 `PresenceHub`다. hub 중계를 넓히기 전에 나눈다(§9 W0).

### 2.2 hub가 이미 주는 것 (user 경로 계약 v1)

| 묶음 | op |
| --- | --- |
| 목록 | `projects`(프로젝트·프로필·에이전트와 모드·모델·강도 허용 목록·실행기), `sessions` |
| 세션 | `session`(상세·`pending`·`attempts`·`queued`), `events`(`seq` 커서) |
| 실시간 | `GET /user/stream`: 세션 하나(`state`·`event`·`reset`, `Last-Event-ID`) 또는 목록(`sessions`·`changed`) |
| 조작 | `send`, `new`, `close`, `cancel`, `answer`, `preset` |

이벤트 종류는 `session`, `setup`, `turn_start`, `text`, `thought`, `tool`(kind·status·input·output·
locations·`diffs`), `plan`, `usage`, `request`, `answer`, `turn_end`, `truncated`다. hub가 ACP
`session/update`를 정규화한 모양이라, Claude·Codex·Antigravity를 같은 카드로 그릴 수 있다.

계약에 **없는** 것(화면이 지어내지 않는다): 세션의 마지막 활동 시각, 목록에서의 작업 글, 세션별 승인 수,
턴이 쓴 토큰. `usage`(`used`/`size`)는 컨텍스트 사용량이지 턴의 소비량이 아니다. `new`의 응답(작업)에는
세션 id가 없다. 이것들을 어떻게 얻는지는 §4·§8에 적는다. v1 안에서는 더하기만
하므로 클라이언트는 모르는 칸과 종류를 무시한다.

## 3. 참고한 제품

| 제품 | 가져올 것 |
| --- | --- |
| Claude Code (데스크톱·웹) | 세션 사이드바, 접히는 도구 호출 행, 대화 안의 권한 요청, 입력창 위의 할 일 목록, 오른쪽 diff 패널 |
| Codex 앱 | 프로젝트별 작업 목록과 상태, 턴 단위로 모은 변경 보기 |
| Antigravity Agent Manager | 여러 작업 공간의 에이전트를 한 화면에서 보는 관리자 화면, 승인이 필요한 것만 모은 받은 편지함 |
| opencode (MIT) | REST로 조작하고 SSE 버스 하나로 상태를 맞추는 구조, 세션 상태·권한·diff·todo를 따로 받는 API |
| kandev (AGPL) | hub와 같은 형태(ACP, 여러 실행기, 리뷰 게이트)의 화면 구성. **코드는 가져오지 않고 UX만 참고** |
| OpenHands (MIT) | 이벤트 로그를 화면으로 투영하는 방식, 재연결 때 스냅샷을 받고 이어 받는 절차 |

공통된 화면 언어를 따른다: **왼쪽 목록 · 가운데 대화 · 오른쪽 맥락 패널**, 도구 호출은 한 줄로 접고
펼치면 입출력, 승인은 대화 흐름 안의 카드, 상태는 점 하나의 색. 특정 제품의 이름·로고·그림은 쓰지 않는다.

## 4. 화면 구성

```
┌──────────────┬───────────────────────────────────────┬────────────────────┐
│ ＋ 새 작업     │ 머리줄: 프로젝트 › 에이전트@실행기 · 상태 │ 오른쪽 패널 (토글)   │
│ ⌕ 찾기 Ctrl+K │───────────────────────────────────────│ [변경][계획][정보]  │
│ ▣ 받은 승인 2  │ 대화 기록                              │                    │
│ ◎ 작업 현황    │  · 사용자 글                           │ 파일별 diff         │
│              │  · 글/생각(접힘)                        │ 할 일 목록          │
│ HIMARI       │  · 도구 카드 ▸ Edit app.py  +12 −3     │ 컨텍스트 사용량      │
│  ● 오늘 일정   │  · 맡긴 작업 카드 ▸ calliope · claude   │ 시도 기록           │
│  ○ 리팩터 상의 │  · 승인 카드 [허용][거절]               │                    │
│ 프로젝트      │  · 턴 끝 ── 완료 · 3분 · 41k            │                    │
│  calliope ▾  │───────────────────────────────────────│                    │
│   ● claude   │ 대기 중인 글 1  [취소]                  │                    │
│   ◐ codex    │ ┌───────────────────────────────────┐ │                    │
│  himari-v3 ▸ │ │ 입력창                             │ │                    │
│ ⚙ 설정        │ │ 모델 ▾ 강도 ▾ 모드 ▾      [보내기/중지]│ │                    │
└──────────────┴─┴───────────────────────────────────┴─┴────────────────────┘
```

### 4.1 왼쪽 사이드바

- **＋ 새 작업:** 입력창 하나로 시작한다. 받는 쪽을 고른다: 기본은 HIMARI(새 대화), 또는 프로젝트 하나
  (에이전트·실행기·프로필은 hub 기본값, 바꿀 수 있음 → `new`).
- **받은 승인:** 모든 세션의 `pending` 수. 누르면 받은 편지함(§4.6).
- **작업 현황:** 관리자 화면(§4.5).
- **HIMARI:** 대화 목록(지금의 목록과 같음). 대화 아래에 그 대화가 맡긴 세션이 들여 쓰여 붙는다(§5.1).
- **프로젝트:** 프로젝트 → 세션. 세션 줄은 `에이전트@실행기 · 프로필`, 상태 점(`running` 움직이는 점,
  `waiting` 주황, `idle` 회색, `closed` 비움), 실행기 끊김 표시, 대기 글 수, `current` 표시.
  기본은 열린 세션만, 필터로 닫힌 것도.
- 아래에 설정(모양, 지침, 메모리, 기기, Tool 서버, 휴지통, 로그아웃)과 서버 상태.

### 4.2 가운데: 대화 기록

HIMARI 대화와 에이전트 세션이 **같은 기록 컴포넌트**를 쓴다. 두 기록을 공통 항목 모델(§7.4)로 바꿔 그린다.

- **사용자 글**, **답 글**(안전한 Markdown·수식 렌더러), **생각**(접힘, 기본 닫힘).
- **도구 카드:** 한 줄 = 종류 아이콘 · 제목 · 상태 · 요약(파일 경로, `+n −m`). 펼치면 입력·출력, diff는
  인라인 미리보기와 "오른쪽에서 보기". 같은 `id`의 갱신은 같은 카드에 합친다. 연속된 읽기 도구는
  "도구 5개" 묶음으로 접는다.
- **계획:** 최신 계획을 입력창 위에 고정해 보이고(진행 중 항목 강조), 기록에는 바뀐 때만 작은 줄.
- **승인·질문 카드:** 대화 흐름 안, 그 앞의 글 뒤에 놓인다. 답할 수 있는지와 선택지·질문 칸은 **늘 최신
  `pending`에서** 가져온다(계약 §7). `request` 이벤트는 자리만 정한다. `pending`에 없으면 결과
  (`answer` 이벤트의 `answered`·`refused`·`expired`·`cancelled`)만 보인다. 다시 읽는 때는 §7.3.
- **턴 경계:** `turn_start`의 모델·강도·모드 칩, `turn_end`의 결과와 걸린 시간(`turn_start`·`turn_end`의
  `at` 차이). 컨텍스트 사용량은 최신 `usage`로 정보 탭에 보인다(턴이 쓴 토큰은 hub가 주지 않으므로 보이지
  않는다). `failed`·`lost`·`limited`는 `why`와 함께 눈에 띄게.
- **잘림:** `truncated` 이벤트와 잘린 diff(`cut`, `more`)는 회색 안내로.
- **처음 열 때와 앞부분:** `events`는 `after` 뒤를 오름차순으로만 준다. 그래서 세션 상세의 `last_seq`로
  `after = max(first_seq - 1, last_seq - 200)`을 정해 최근 구간을 읽고, 위로 올리면 그 앞 구간을 같은 방식
  (`after`를 200씩 앞으로, `limit` 200)으로 읽어 앞에 붙인다. 이어 받기 커서는 **실제로 받은 마지막 `seq`**
  (응답의 `next`)이고 `last_seq`가 아니다. 긴 기록은 가상 스크롤로 그린다.

### 4.3 입력창

- 에이전트 세션: 글, 모델·강도·**모드**(그 글 하나에만, `projects`의 `agents[].modes`에 있는 것만), 보내기.
  턴이 돌면 보내기 옆에 **중지**(`cancel`). 작업 중에 보낸 글은 입력창 위 **대기 줄**에 쌓이고 하나씩 취소한다.
- HIMARI 대화: 지금과 같다(첨부, 붙여넣기, `/` 프롬프트, 모델·강도, 중지, Continue).
- 초안은 대화·세션마다 이 브라우저에 남긴다(지금의 `drafts.js`와 같은 규칙).

### 4.4 오른쪽 패널

탭 셋(세션에 따라 보이는 것만): **변경**(이 세션의 diff를 파일별로, 턴별로 고를 수 있음, 통합/나란히 보기),
**계획**(할 일 전체와 변경 이력), **정보**(에이전트·실행기·프로필·모델, 컨텍스트 `used/size` 막대, 최근 시도,
세션 닫기). HIMARI 대화에서는 **파일**(지금의 보기 전용 파일 패널)과 **맡긴 작업**(§5.1)이다.
너비는 끌어서 바꾸고 이 브라우저에 기억한다.

### 4.5 작업 현황(관리자 화면)

열린 세션 전부를 카드 격자로: 프로젝트, 에이전트@실행기, 상태, 대기 글 수, 컨텍스트 사용량은 목록
스트림(`sessions`·`changed`)에서 바로 온다. 그 밖은 따로 얻는다.
- 지금 도는 작업의 글 앞부분: 세션의 `task` id로 `task`(H1)를 읽는다.
- 승인 대기 수: `pending`의 `task_id`를 세션의 `task`와 맞춰 센다.
- 마지막 활동 시각: 세션에 `updated_at`(H7)이 생길 때까지 보이지 않는다.

프로젝트·상태로 거른다. 홈 화면의
입력창 아래에도 이 카드의 요약(돌고 있는 것과 기다리는 것)을 보인다.

### 4.6 받은 승인

모든 `pending`을 오래된 순으로. 줄마다 프로젝트·에이전트·요청 글, 바로 답하는 버튼, "세션에서 보기".
탭 제목 대기 수와 백그라운드 알림(V3-P95)은 그대로 이어 간다.

### 4.7 좁은 화면

- 768px 미만: 사이드바는 왼쪽 서랍, 오른쪽 패널은 아래에서 올라오는 시트, 머리줄에 둘을 여는 단추.
- 승인 카드는 화면 아래에 붙어 늘 보인다. 입력창은 키보드 위에 붙는다.
- 터치 목표 44px, 가로 스크롤 없음.

### 4.8 찾기와 단축키

`Ctrl+K`: 대화·프로젝트·세션을 이름과 상태로 찾아 연다. `Ctrl+Enter` 보내기, `Esc` 패널 닫기,
`Alt+↑/↓` 목록 이동. 화면에 단축키 안내를 둔다.

## 5. 두 경로를 한 화면에

### 5.1 MCP 경로: HIMARI가 맡긴 일

- Web은 `mcp.json`에서 hub로 지정된 서버(설정 `HIMARI_HUB_MCP_SOURCE`, 기본 `hub`)의 Tool 호출을 알아본다.
  이름 추측이 아니라 그 서버의 Tool id(`<source>/hub_delegate` 등)로 판단한다.
- `hub_delegate`의 결과(작업 id)와 `hub_task`의 결과(상태, 작업이 실제로 돈 `session`)를 **맡긴 작업 카드**로
  그린다: 프로젝트·에이전트·상태·진행 요약, "세션 열기".
- 카드는 **작업 id로 끝까지** 따라간다: hub의 `task` op(§8 H1)로 상태·세션을 읽고, 작업이 끝날
  (`done`·`failed`) 때까지 다시 읽는다. 세션 상태는 작업 상태를 대신하지 않는다(성공과 실패가 모두 `idle`로
  보이고, 같은 세션의 다음 작업이 이전 카드에 붙으며, 세션을 잃으면 작업이 다른 세션에서 돈다). 목록
  스트림에서 그 세션이 바뀐 것은 "다시 읽을 때"라는 신호로만 쓴다. 신호가 없어도 실행 중에는 15초마다 읽는다.
- 사이드바에서 그 대화 아래에 맡긴 세션을 붙이고, 오른쪽 **맡긴 작업** 탭에 그 대화의 작업을 모은다.
- HIMARI의 승인 원칙은 그대로다: 모델은 승인에 답하지 못하고, 승인 카드의 답은 사용자 경로로만 간다.

### 5.2 user 경로: 사용자가 직접

세션 열기·보기·보내기·새 대화·닫기·중지·승인을 Web이 직접 한다. **보낸 글은 작업 id로 따라간다.**
`new`의 응답에는 세션 id가 없고, `send`도 에이전트가 그 세션을 잃었으면 새 세션에서 돈다(계약 §4.4). 그래서
`new`·`send` 모두 돌려받은 작업을 `task`(H1)로 읽어(§5.1과 같은 주기) 실제 `session`을 알아낸다.
- `new`: 그 세션 화면으로 옮긴다.
- `send`: 실제 세션이 지금 화면과 다르면 "새 세션에서 이어 갑니다" 안내와 함께 그 세션으로 옮기고 스트림을 바꾼다.
- 세션이 생기기 전에 끝난 작업(`failed`, `not_started`)은 입력창 위 대기 줄 자리에 실패로 남긴다.

H1은 W3 전에 필요하다. 사용자가 직접 보낸 글도 hub 원장에
작업으로 남으므로, HIMARI는 `hub_sessions`·`hub_task`로 그 일을 볼 수 있다(연속성은 hub 프로젝트가 쥔다).

## 6. 서버 중계 (`/api/hub/…`)

브라우저는 hub에 닿지 않는다. v3 서버가 `user.token`을 들고 중계한다(계약 §2, §7). 운영자만 쓴다.

| v3 경로 | hub | 비고 |
| --- | --- | --- |
| `GET /api/hub/projects` | `projects` | |
| `GET /api/hub/sessions` | `sessions` | `project`, `runner`, `closed`, `limit` |
| `GET /api/hub/sessions/{id}` | `session` | |
| `GET /api/hub/sessions/{id}/events` | `events` | `after`, `limit`; `bad_cursor`는 409 `hub_bad_cursor` |
| `GET /api/hub/stream` | 목록 스트림 | SSE |
| `GET /api/hub/sessions/{id}/stream` | 세션 스트림 | SSE, `after`·`Last-Event-ID` |
| `POST /api/hub/sessions/{id}/messages` | `send` | `prompt`, `model`?, `effort`?, `mode`? |
| `POST /api/hub/sessions` | `new` | `project`, `prompt`, 선택 인자 |
| `POST /api/hub/sessions/{id}/close` | `close` | |
| `GET /api/hub/tasks/{id}` | `task`(H1) | MCP 경로 카드. H1이 hub에 생기면 더함(W0에는 없음) |
| `PUT /api/hub/projects/{name}/preset` | `preset` | |
| `GET /api/hub/pending`, `POST …/answer`, `POST /api/hub/tasks/{id}/cancel` | 그대로 | V3-P95 |

- **필드 다시 만들기:** 지금의 원칙(V3-P95)대로 종류마다 아는 칸만 크기 한도와 함께 옮긴다. 모르는 이벤트
  종류는 `seq`·`kind`·`at`만 넘겨 화면이 "알 수 없는 항목"으로 그린다(커서가 끊기지 않게).
- **목록 스트림(나눠 줌):** v3 서버가 hub에 **하나만** 연다. 서버는 마지막 전체 목록을 들고 있는다
  (`sessions`면 통째로 바꾸고, `changed`면 id로 합침). 브라우저가 붙으면 먼저 그 전체 목록을 `sessions`로
  보내고 그 뒤 `changed`를 나눈다. hub에 다시 붙어 새 `sessions`가 오면 서버 목록을 바꾸고 브라우저에도
  `sessions`로 보내 **바꾸게** 한다(합치지 않음). 마지막 브라우저가 떠나면 30초 뒤 upstream을 닫는다.
- **세션 스트림(나누지 않음):** 브라우저 연결마다 hub 스트림을 하나 열고, 그 브라우저의 `after`·
  `Last-Event-ID`를 그대로 넘긴다. 커서가 브라우저마다 달라 하나의 upstream으로 나눌 수 없기 때문이다.
  첫 `state`의 `reset: true`와 `reset` 이벤트를 모두 브라우저에 넘긴다. 브라우저가 끊으면 upstream도 닫는다.
- **상한과 포화:** hub의 동시 스트림 한도(32)는 목록·세션 구분 없이 전역이고, 넘으면 hub가 503을 준다.
  v3 프로세스 하나는 목록 1 + 세션 16까지만 연다(v3는 프로세스 하나로 운영한다). 그래도 다른 클라이언트나
  다시 붙는 동안의 겹침으로 hub가 503을 줄 수 있으므로, 어떤 upstream이든 거절되면:
  - 세션: 브라우저에 503 `hub_stream_limit`을 주고, 화면은 그 세션을 `events` 폴링(5초)으로 내려 보인다.
  - 목록: 서버가 `sessions` 폴링(10초)으로 목록을 만들어 `sessions`로 브라우저에 보내고, 지수 백오프로 스트림을
    다시 연다. `pending`은 §7.3의 30초 주기가 계속 맡는다.
  - 다시 붙을 때는 **앞의 upstream을 닫은 뒤** 새로 연다(슬롯을 둘 쥐지 않음).
- **역압:** 브라우저 연결마다 큐를 둔다(이벤트 256개 또는 4 MiB). 넘치면 그 연결만 닫는다. 브라우저는
  `Last-Event-ID`로 다시 붙어 빠진 것을 받는다. 느린 브라우저가 공유 목록 reader를 막지 않는다.
- **인증 수명:** 브라우저 스트림은 열 때 운영자 세션 쿠키를 확인하고, 그 뒤로는 **연결마다의 15초 타이머**로
  다시 확인한다. 이벤트가 계속 와도, hub의 ping(쓰기가 15초 없을 때만 옴)이 없어도 같은 간격이다. 로그아웃하거나
  세션이 끝나면 늦어도 15초 안에 닫는다. 지금의 `/api/events`는 열 때만 확인하므로 이 규칙은 새로 만든다.
- **끊김:** httpx 읽기 시간 제한은 30초(hub ping 15초의 두 배)다. 그동안 아무것도 없거나 hub가 끊으면 브라우저
  스트림에 `hub_unavailable`을 보내고 닫는다. 목록 upstream은 서버가 지수 백오프로 다시 붙는다.
- **오류:** hub 400 → 409 `hub_refused`(사람용 문구 그대로, 500자; `bad_cursor`면 409 `hub_bad_cursor`), 404 → 404,
  연결 실패 → 503 `hub_unavailable`, 401·403 → 503 `hub_misconfigured`(로그에 원인; V3-P95의 승인 경로도 같음),
  남은 스트림 없음 → 503 `hub_stream_limit`. 계약대로 판단은 상태 코드로만 한다.
- **설정:** 이미 있는 `HIMARI_HUB_URL`·`HIMARI_HUB_USER_TOKEN_FILE`에 `HIMARI_HUB_MCP_SOURCE`만 더한다.

## 7. 프론트엔드 구조

### 7.1 선택: React + TypeScript + Vite

| 후보 | 판단 |
| --- | --- |
| 지금처럼 바닐라 JS | 화면 수가 두 배가 되면 상태·재사용·검사가 감당되지 않는다. 버림 |
| Preact / Solid / Svelte | 가볍고 빠르지만, 필요한 부품(접근성 있는 메뉴·대화상자, 가상 스크롤, diff, 명령 팔레트)의 생태계가 작다 |
| **React + TypeScript** | 부품·인력·참고 코드가 가장 많다. OpenHands·kandev·claudecodeui가 React라 화면 패턴을 그대로 옮기기 쉽다. **채택** |

라이브러리(모두 허용 라이선스):

- **UI 부품:** Radix UI 기본 부품 위에 저장소에 복사해 고치는 방식(shadcn/ui 형태, MIT). 상용 도구와 같은
  느낌을 빠르게 내고, 부품 코드가 우리 것이 된다.
- **스타일:** Tailwind CSS v4 + CSS 변수 토큰. 지금의 **Classic·Warm** 두 테마와 밝음·어둠을 토큰으로 옮긴다.
- **라우팅:** React Router. 경로: `/`, `/c/{대화}`, `/s/{세션}`, `/inbox`, `/agents`, `/settings/…`.
  전환 기간에는 Router `basename`과 Vite `base`를 `/next/`로 둔다(§7.2).
- **서버 상태:** TanStack Query(REST 스냅샷, 캐시, 재시도). **실시간 상태:** 작은 저장소(Zustand)에 스트림을
  리듀서로 쌓는다(§7.3).
- **가상 스크롤:** TanStack Virtual. **diff:** jsdiff로 줄 차이를 구하고 우리 렌더러로 그림. **아이콘:** lucide.
- **Markdown·수식:** 지금의 `rich_text.js`를 TypeScript로 옮겨 그대로 쓴다(이미 검사된 안전 규칙과 KaTeX
  늦은 불러오기를 유지). 새 Markdown 라이브러리를 들이지 않는다.

### 7.2 저장소 배치와 빌드

- 소스는 `web/`(최상위)에 둔다: `web/src/{app,routes,components,features/{himari,hub,settings},lib,api}`.
- `npm run build`가 `src/himari/interfaces/web/dist/`로 낸다. 빌드 결과는 커밋하지 않는다(`.gitignore`).
  Node는 빌드하는 곳에만 필요하다.
- **릴리스 순서(W1에서 스크립트로 고정):** `npm ci` → `npm run build` → `python -m build`(sdist·wheel) →
  wheel에 `dist/index.html`과 `dist/assets/`가 있는지 검사 → 설치해 서빙 확인. `package-data`에 `dist/**`를
  넣는 것만으로는 빌드가 돌지 않으므로, `dist`가 없으면 **릴리스가 실패**한다. 개발 중 `dist`가 없으면 서버는
  시작 로그에 알리고 `/next/`를 404로 둔다(옛 화면은 그대로).
- **KaTeX:** 지금 `/assets/katex/…`에서 서빙하는 KaTeX와 글꼴을 `web/public/katex/`로 옮겨 빌드 결과에
  함께 넣고(`<base>katex/…`), 옮긴 렌더러는 base 경로로 불러온다. 옛 `static/katex`는 W6에서 옛 화면과 함께 지운다.
- **경로(전환 기간):** Vite `base: "/next/"`, Router `basename: "/next"`. 서버는 `/next/assets/{이름}`을
  `dist/assets`의 **목록에 있는 이름만**(빌드 manifest로 만든 허용 목록), `/next/katex/{이름}`을 `dist/katex`의
  **목록에 있는 이름만**(빌드 때 만든 목록: JS·CSS·글꼴) 주고, 그 밖의 `/next/` 아래 GET은 `index.html`을 준다
  (SPA fallback). `/api`·`/next/assets`·`/next/katex`는 fallback에서 빠지므로, 목록에 없는 이름은 HTML이 아니라
  404다. 설치한 wheel로 띄워 수식과 글꼴이 실제로 불러와지는지도 W1에서 검사한다. 새 화면 안의 링크는 모두 basename 아래라
  옛 `/c/{id}`로 새지 않는다. **W6 전환:** base를 `/`로 바꾸고 `/assets/`는 새 빌드 것만, `/c/{id}`는 새 화면의
  같은 경로라 링크가 그대로 산다. `/next/…`는 같은 경로의 `/…`로 308 이동한다.
- 개발: `npm run dev`(Vite)가 `/api`를 로컬 v3 서버로 넘긴다.
- **CSP:** 스크립트는 그대로 `script-src 'self'`(인라인 없음)다. 다만 Radix의 대화상자·팝오버는 스크롤 잠금
  (`react-remove-scroll` → `react-style-singleton`)이 실행 중에 `<style>` 요소를 넣는다. 그래서 새 화면의
  HTML에만 `style-src 'self' 'nonce-{요청마다}'`를 더한다: 서버가 `index.html`을 줄 때마다 nonce를 만들어
  `<meta name="csp-nonce">`와 CSP 머리에 넣고, 앱은 그리기 전에 `get-nonce`의 `setNonce`로 그 값을 준다.
  CSP 머리는 **응답마다 하나**여야 한다. 지금은 `SecurityHeadersMiddleware`(`security.py`)가 모든 응답에 고정
  CSP를 붙이고, 정책이 둘이면 둘 다 적용되어 nonce가 소용없다. 그래서 미들웨어가 CSP를 만든다: 라우트가
  요청 상태(`request.state`)에 nonce를 남기면 미들웨어가 그 nonce를 넣은 최종 CSP **하나**를 붙이고, 없으면
  지금의 고정 CSP를 붙인다. 라우트는 CSP 머리를 직접 붙이지 않는다(파일 보기의 `sandbox` CSP처럼 이미 직접
  붙이는 응답은 지금 규칙 그대로 둔다).
  스타일 nonce만이고 스크립트는 넓히지 않는다. 나머지 스타일은 파일과 CSSOM으로만 준다(HTML `style`
  속성을 쓰지 않음). 외부 글꼴·CDN 없음. **W1의 완료 조건:** Playwright로 대화상자·메뉴·팝오버를 열어도
  `securitypolicyviolation`이 0개다. 안 되면 그 부품을 CSSOM만 쓰는 것으로 바꾼다(예: React Aria).

### 7.3 상태 모델

- 스트림 하나 = 리듀서 하나. 세션 저장소는 `seq` 순의 이벤트 배열과, 그것을 접은 항목 목록(도구 카드는
  `id`로 합침, 계획은 최신 값)을 가진다. 화면은 항목 목록만 읽는다.
- 처음: `events`로 최근 구간(§4.2) → 받은 마지막 `seq`부터 스트림. 첫 `state`의 `reset: true`나 `reset`
  이벤트면 저장소를 비우고 `first_seq`부터 다시 그린다. 끊기면 지수 백오프로 다시 붙고 `Last-Event-ID`
  (받은 마지막 `seq`)로 이어 받는다.
- 목록 저장소는 `sessions`가 오면 **통째로 바꾸고**(처음이든 다시 붙었든), `changed`면 id로 합친다.
- **`pending` 다시 읽기:** (다시) 붙었을 때, 세션 스트림에 `request`·`answer`·`turn_end`가 왔을 때, 목록에서
  어떤 세션의 `activity`나 `task`가 바뀌었을 때, 내가 답한 뒤(성공이든 409 거절이든), 그리고 대기 중인 것이
  하나라도 보이는 동안 30초마다. 다른 브라우저의 답, 만료, 이미 `waiting`인 작업에 더해진 요청을 이렇게 잡는다.
- HIMARI 대화도 같은 방식으로 옮긴다(지금의 `/api/conversations/{id}/events`·`live`).

### 7.4 공통 항목 모델

HIMARI 이벤트(`user_message`, `assistant_tool_calls`, `tool_result`, `assistant_final`, …)와 hub 이벤트를
같은 항목 종류로 바꾼다: `user`, `answer`, `thought`, `tool`, `delegation`(맡긴 작업 카드), `plan`,
`request`, `turn`, `notice`. 기록 컴포넌트는 이 종류만 안다. 바꾸는 함수는 출처별로 따로 두고 단위 시험한다.

### 7.5 검사

- Vitest: 리듀서, 항목 변환, Markdown 렌더러(지금의 `rich_text_smoke.mjs` 시험을 옮김).
- Playwright: 가짜 hub(`tests/`의 가짜와 같은 방식)와 실제로 띄운 v3로 화면 흐름 몇 개. 데스크톱·모바일 크기.
- CI(`static-checks`)에 `npm ci`·타입 검사·Vitest·빌드를 더한다. Playwright는 Web을 바꾸는 PR에서만.
- 옛 스모크(`ui_state_smoke.mjs` 등)는 옛 화면을 지울 때 함께 지운다.

## 8. hub에 필요한 것

| id | 무엇 | 크기 | 언제 |
| --- | --- | --- | --- |
| H1 | `/user/task {id}`: §3.1 작업에 `session`, 결과 글 앞부분 | 작음, v1 더하기 | **W3 전** |
| H7 | 세션에 `updated_at`(마지막 이벤트나 상태 변화 시각) | 작음, v1 더하기 | W2와 함께, 없으면 표시 생략 |
| H2 | 세션 이름(`rename`), 닫힌 세션 다시 열기 | 작음~중간 | 나중 |
| H3 | 슬래시 명령 목록과 실행 | 중간 | 나중 |
| H4 | 에이전트에 이미지·파일 첨부(ACP 리소스) | 중간 | 나중 |
| H5 | 프로젝트 파일·git 상태 읽기(실행기 연산) | 중간 | 나중 |
| H6 | 에이전트 로그인 중계 | 큼 | 나중 |

H2~H6이 생기면 화면 자리는 이미 있다(세션 메뉴, 입력창 `/`, 첨부 단추, 오른쪽 패널의 파일 탭).
hub PR #25(diff·모드·`projects`)는 아직 운영에 없다. W2 전에 서버와 실행기를 함께 올린다.

## 9. 단계 (PR마다 운영에서 확인)

옛 화면은 마지막 단계 전까지 `/`에 남고, 새 화면은 `/next/`에서 자란다.

| 단계 | 내용 | 확인 |
| --- | --- | --- |
| W0 | `PresenceHub` 이름 나누기(`application.presence`). 중계 전체(§6): 목록 나눠 주기, 세션별 스트림, 상한·포화 때 폴링·역압·연결별 인증 타이머, 가짜 hub 시험 | pytest(web·bootstrap), mypy |
| W1 | `web/` 뼈대, 릴리스 빌드 순서와 wheel 검사, CI, `/next/` 서빙(base·basename·자산과 KaTeX 허용 목록·fallback), 미들웨어가 만드는 스타일 nonce CSP, 로그인, 틀(사이드바·머리줄·패널), 테마 토큰, HIMARI 대화 읽기·보내기·스트리밍 | Vitest, Playwright(CSP 위반 0), 운영에서 `/next/` |
| W2 | 프로젝트·세션 목록(실시간), 세션 기록(읽기 전용, 최근 구간과 앞 구간), 재연결·`reset`, 작업 현황 화면 | 실제 Claude 세션을 실시간으로 봄 |
| W3 | 보내기·새 대화(둘 다 H1로 실제 세션 따라가기)·닫기·대기 줄·중지·모델/강도/모드, 대화 안 승인 카드, 받은 승인, `pending` 다시 읽기 규칙 | 운영의 승인·질문·중지, 두 브라우저에서 답 |
| W4 | MCP 경로: 맡긴 작업 카드(작업 id로 끝까지), 대화 아래 세션, 맡긴 작업 탭 | HIMARI 위임 → 세션 열기, 실패한 작업이 실패로 보임 |
| W5 | 오른쪽 패널: 변경(diff), 계획, 정보; HIMARI 파일 패널 | 다른 기기 diff 포함 |
| W6 | 나머지 화면 옮기기(설정·지침·메모리·기기·Tool 서버·휴지통·`/` 프롬프트·초안·붙여넣기·첨부·폴더), 좁은 화면 다듬기, `/`를 새 화면으로 바꾸고 옛 정적 파일·스모크 지우기 | 전체 pytest, Playwright, 휴대폰 실기 |

W0은 Python만 바꾸므로 W1과 따로 진행할 수 있다. hub 쪽은 H1이 W3 전, hub PR #25 배포가 W2 전이다.

## 10. 바꾸지 않는 것과 따로 할 것

- 운영자 로그인, CSP, 토큰을 브라우저·모델·Tool에 주지 않는 원칙, 모델이 승인에 답하지 못하는 원칙.
- 기기 API와 Windows 반려 앱.
- **따로 할 것:** HIMARI 라우팅 지시문(언제 직접 하고 언제 맡길지, 같은 세션으로 이을지 `fresh`로 시작할지,
  맡긴 뒤 기다리는 법). 이 화면이 W4까지 오면 실제 사용을 보고 프롬프트 정비(V3-P89)의 후속으로 설계한다.
- 대화 밖 알림(승인 대기, 작업 끝)을 반려 앱이나 먼저 말 걸기로 전하는 것(hub README 다음 단계 3).

## 11. 열린 질문

1. **빌드 위치:** 운영 서버에서 릴리스마다 빌드(Node 필요)할지, CI가 만든 결과물을 올릴지. 권고는 CI 결과물.
2. **화면 글의 언어:** 지금 Web 글은 영어다. 새 화면을 한국어로 할지, 둘 다 둘지. 권고는 문자열을 한 곳에
   모으고 처음엔 지금과 같은 영어, 한국어는 그 뒤.
3. **홈 화면:** 빈 입력창(지금)과 작업 현황 중 무엇을 기본으로 둘지. 권고는 입력창 + 그 아래 작업 요약.
4. **읽기 전용 세션의 동시 실행:** hub는 프로젝트당 작업 하나다. 검수 같은 읽기 세션도 기다리게 되는데,
   화면에서 "차례 기다림"을 어떻게 보일지(대기 줄 위치 표시)는 W3에서 정한다.
