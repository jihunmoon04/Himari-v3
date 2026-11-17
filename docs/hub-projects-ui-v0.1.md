# Web에서 hub 프로젝트와 기기 폴더 다루기 (V3-P103 설계 초안 v0.1)

> 2026-11-18 · rev. 1 · **설계 초안** · 대상: v3 `cb56a53`(V3-P102 작업 콘솔 운영 중), hub `f1f5119`(프로젝트 요청 구현, 운영 배포 전)
> 앞 문서: [작업 콘솔](web-console-v0.1.md)(V3-P102), hub [프로젝트 요청 설계](https://github.com/jihunmoon04/himari-hub/blob/main/docs/projects-v0.1.md), hub [user 경로 계약 v1](https://github.com/jihunmoon04/himari-hub/blob/main/docs/user-api-v1.md) §4.6·§5.2

## 1. 목표와 범위

- **Web에서 hub 프로젝트를 만들고, 기기 폴더를 더하고 빼고, 프로젝트 설정을 바꾼다.** hub는 이 일을 user 경로의 op로 이미 받는다(hub #39). v3는 그 op를 중계하고 화면을 붙인다.
- **HIMARI(모델)가 요청한 것은 사용자가 Web에서 허가한다.** 모델의 `hub_project_request`는 hub에서 `waiting_user`로 멈추고, 허가는 user 경로로만 된다. v3는 그 요청을 받은 승인 화면과 대화 안의 카드로 보인다.
- **범위 밖:** HIMARI 자체 프로젝트(대화를 프로젝트에 묶기, 작업 단위), 기기 앱(→ [V3-P104](workbench-v0.1.md)), hub의 허가 방식 바꾸기(그 기기에서만 함).

## 2. hub가 주는 것 (요약)

자세한 것은 hub 계약 §4.6·§5.2.

- **허가 방식(기기마다, 그 기기에서만 정함):** `off`(원격 등록을 받지 않음, 빼기는 받음), `local`(그 기기 화면의 확인 창에서 사용자가 폴더를 고름, 짝 코드), `user`(user 경로의 요청·허가가 곧 허가, roots 아래만).
- **요청 상태:** `waiting_user`(모델의 요청, 사용자 허가를 기다림) → `waiting_device`(5분) → `applying` → `done`, 또는 `refused`·`expired`·`failed`·`cancelled`, 그리고 `lost`(claim 뒤 결과를 모름: 사용자가 정리).
- **op:** `project_request`, `project_requests`, `project_request_answer`, `project_request_cancel`, `project_request_resolve`(펜스, 기기를 잃었을 때만 `force`), `project_folder_remove`, `project_set`, `project_adopt`, `runner_dirs`. `projects`의 `runners[]`에 `approve`·`folders`.
- **목록 스트림:** `project_request`(바뀐 요청 하나, `rev`)와 `projects`(본문 없음, 다시 읽으라는 신호). 처음은 늘 `sessions`, 둘 다 SSE `id` 없음.

## 3. hub에 더 필요한 것

| id | 무엇 | 왜 |
| --- | --- | --- |
| H8 | `projects`의 `runners[]`에 `held`(그 기기가 알렸지만 예약 때문에 보류된 이름) | 지금은 보류가 끝난 `create` 요청의 `same_name_on`으로만 보여, 요청이 목록에서 사라지면 보류된 폴더를 채택할 길이 화면에 없다. user 경로에만(MCP에는 없음) |

H8은 계약 v1에 더하기다. 없는 hub에서는 화면이 채택 단추를 보이지 않는다(§6.4).

## 4. 서버 중계 (`/api/hub/…`)

지금의 중계 원칙(V3-P95, V3-P102 §6)을 그대로 쓴다: 운영자만, 종류마다 아는 칸만 크기 한도와 함께 다시 만들어 넘김, hub 400 → 409 `hub_refused`(문구 그대로), 연결 실패 → 503 `hub_unavailable`, 401·403 → 503 `hub_misconfigured`, 보냈지만 답을 못 받음 → 503 `hub_unknown`. 상태를 바꾸는 요청은 지금처럼 `OriginGuardMiddleware`(같은 출처)와 운영자 세션 쿠키를 거친다.

| v3 경로 | hub op | 비고 |
| --- | --- | --- |
| `GET /api/hub/project-requests` | `project_requests` | `open`(bool), `limit` |
| `POST /api/hub/project-requests` | `project_request` | `kind`, `project`, `runner`, `path`?, `create_dir`?, `agent`? |
| `POST /api/hub/project-requests/{id}/answer` | `project_request_answer` | `approve` |
| `POST /api/hub/project-requests/{id}/cancel` | `project_request_cancel` | |
| `POST /api/hub/project-requests/{id}/resolve` | `project_request_resolve` | `force`?, `outcome`?. 시간 제한 40초(펜스는 기기 답을 30초 기다림) |
| `PATCH /api/hub/projects/{project}` | `project_set` | `agent`?, `profile`?, `preset`?(준 것만, null은 지움). 지금의 `PUT …/preset`은 그대로 둠 |
| `POST /api/hub/projects/{project}/folders/{runner}/remove` | `project_folder_remove` | |
| `POST /api/hub/projects/{project}/adopt` | `project_adopt` | `runner` |
| `GET /api/hub/runners/{runner}/dirs` | `runner_dirs` | `path`?. 시간 제한 15초 |

- **요청 레코드(`REQUEST`):** `id`, `kind`, `project`, `runner`, `origin`, `path`, `create_dir`, `agent`, `why`(500자), `code`, `mode`, `state`, `rev`, `result_path`, `reason`, `created_at`, `decided_at`, `expires_at`, `claimed_at`, `ended_at`, `same_name_on`.
- **`PROJECTS`:** `runners[]`에 `approve`(`mode`, `roots`, `why`), `folders`({이름: 경로}), `held`(H8, 있을 때). 기기 폴더 경로는 운영자 화면에만 간다(모델에는 hub가 이미 주지 않음).
- **`hub_unknown`의 뜻:** `project_request`·`answer`가 이 답이면 요청이 생겼을 수도 있다. 화면은 다시 보내지 않고 `project-requests?open=true`를 다시 읽어 확인하게 한다. `resolve`가 이 답이면 다시 눌러도 된다(펜스는 같은 답을 다시 준다).

### 4.1 목록 스트림

v3는 hub 목록 스트림 하나를 브라우저들에 나눠 준다(V3-P102 §6). 거기에 두 이벤트를 더한다.

- **`project_request`:** 받은 그대로(레코드를 다시 만들어) 모든 브라우저에 보낸다. v3는 들고 있지 않는다(늦게 붙은 브라우저는 §6.1처럼 스스로 읽는다).
- **`projects`:** 그대로 보낸다.
- **upstream이 다시 붙거나 폴링으로 내려갈 때:** 그 사이의 `project_request`는 잃었을 수 있다. v3는 새 `sessions`를 보낼 때 `projects`도 한 번 보내, 화면이 프로젝트와 열린 요청을 다시 읽게 한다.
- 브라우저 쪽 `sessions`·`changed`의 뜻과 순서는 바꾸지 않는다. 새 이벤트에는 `id`가 없다.

## 5. 화면 상태

- **`useHubRequests`:** 열린 요청 전체를 들고 있는다. hub 계약 §5.2의 규칙대로 맞춘다: 목록 스트림을 연 뒤 `project-requests?open=true`를 읽고, 그 사이 온 `project_request`는 모아 두었다가 `id`별로 `rev`가 가장 큰 것을 남긴다. 끝난 상태가 오면 열린 목록에서 빼고, 끝난 요청 하나를 잠시(그 화면을 떠날 때까지) 따로 들고 있어 "끝났습니다"를 보일 수 있게 한다. 다시 연결하면 목록을 버리고 새로 만든다.
- **`projects` 이벤트:** `useHubProjects`를 다시 읽는다(읽는 중에 또 오면 끝난 뒤 한 번 더).
- 스트림이 폴링으로 내려간 동안은 열린 요청을 10초마다 읽는다(지금의 목록 폴링과 같은 주기).

## 6. 화면

### 6.1 새 프로젝트

사이드바 Projects 머리에 **+** 단추 → 대화상자.

1. **이름:** hub 규칙(80자, 앞뒤 공백 없음, 인쇄 가능한 글자). 이미 있는 이름이면 그 자리에서 알린다(`projects`로).
2. **기기:** 연결된 기기 목록. 기기마다 방식(`local` "그 기기 화면에서 고름", `user` "여기서 고름(roots 아래)")을 보인다. `off`나 끊긴 기기, 기기 토큰이 아니어서 요청을 받지 않는 기기(`approve.mode`가 `off`로 옴)는 고를 수 없게 흐리게 하고 이유(`why`)와 "그 기기에서 `himari-hub runner approve`"를 보인다.
3. **폴더:**
   - `local`: "폴더는 그 기기 화면에서 고릅니다." 선택으로 "새 폴더 만들기"(그 기기 창에서 이름을 적음)와 "시작 위치"(제안, 글).
   - `user`: 폴더 고르기(§6.5)와 "새 폴더 만들기"(고른 폴더 아래 이름 하나: 구분자·`.`·`..` 없음). 경로는 고른 결과로만 채운다.
4. **기본 에이전트**(선택).
5. **요청** → 요청 카드(§6.3)로 바뀐다. `done`이면 그 프로젝트 화면(§6.2)으로 옮긴다.

### 6.2 프로젝트 화면 `/projects/:name`

사이드바의 프로젝트 이름을 누르면 연다(지금은 세션만 펼침: 이름 옆 정보 단추로 연다).

- **설정:** 기본 에이전트, 프로필, 권한 묶음(`PATCH`). 권한 묶음은 지금처럼 바꾸기 전에 확인을 받는다(에이전트가 묻지 않고 하는 범위가 바뀜).
- **기기별 폴더:** `runners[]` 중 이 프로젝트를 가진 기기: 이름, 연결, 폴더(`folders`, 아는 것만), "빼기"(확인 대화상자: "그 기기의 runner.json에서만 빠지고, 폴더와 작업 기록은 그대로입니다"). 빼기는 `off` 기기에도 된다.
- **기기에 폴더 더하기:** §6.1의 2~3단계와 같은 대화상자(`kind: add_folder`).
- **보류된 폴더(H8):** "laptop이 같은 이름의 폴더를 알렸지만 다른 요청이 이름을 쓰던 중이라 보류됨" + **채택**(`adopt`). H8이 없는 hub면 이 줄이 없다.
- **진행 중·모름인 요청:** 이 프로젝트의 열린 요청 카드(§6.3).

### 6.3 요청 카드

새 프로젝트 대화상자, 프로젝트 화면, 받은 승인 화면, 대화 안(§6.6)에서 같은 컴포넌트를 쓴다.

| 상태 | 보이는 것 | 할 수 있는 것 |
| --- | --- | --- |
| `waiting_user` | "HIMARI가 요청함", 이유(`why`), 기기, 종류, 경로 | 허가 / 거절 |
| `waiting_device`(local) | "**{기기} 화면에서 폴더를 고르세요**", 짝 코드(크게), 남은 시간 | 취소 |
| `waiting_device`(user) | "기기가 확인하는 중" | 취소 |
| `applying` | "기기가 등록하는 중" | — |
| `lost` | "기기에 적용됐는지 모릅니다" | **정리**(§6.4) |
| `done` | 결과 경로, 프로젝트로 가기 | — |
| `refused`·`expired`·`failed`·`cancelled` | 이유(`reason`을 사람 말로: `policy_changed` "기기의 허가 방식이 바뀌었습니다" 등, 모르는 값은 그대로) | 다시 요청(같은 값으로 대화상자 열기) |

`hub_refused`(409)는 hub 문구를 그대로 보인다(이미 있는 이름, 기기에 진행 중인 요청 등).

### 6.4 `lost` 정리

1. **정리** → `resolve`(force 없이). 기기가 연결돼 있으면 기기가 lock 안에서 답하고 요청이 `done`·`failed`가 된다(최대 30초, 단추는 그동안 "기기에 묻는 중").
2. 기기가 연결돼 있지 않거나 답이 없으면(409) 문구를 보이고 단추를 그대로 둔다. 그 아래에 접힌 **"기기를 잃었을 때"**: 적용됨 / 적용 안 됨을 사용자가 고르고, 결과를 함께 적는다.
   - 적용 안 됨: "그 기기가 다시 켜져 실제로 등록돼 있었다면, 그 기기의 폴더는 기본 에이전트 없이 프로젝트가 됩니다."
   - 적용됨: "기기에 없다면 폴더 없는 프로젝트가 남습니다."
3. 모델은 정리하지 못한다(이 단추는 운영자 화면에만).

### 6.5 폴더 고르기 (`user` 방식)

`runner_dirs`로 루트 목록 → 한 단계씩 들어감(이름만, 숨김·링크는 hub가 뺌). 고른 경로를 그대로 `path`로 보낸다(서버는 해석하지 않고 기기가 검사). 시간 제한·오류는 문구로.

### 6.6 대화 안의 카드

HIMARI의 `<hub source>/hub_project_request` Tool 호출을 맡긴 작업 카드(V3-P102 §5.1)처럼 카드로 그린다. 결과의 `request.id`로 `useHubRequests`에서 따라가고, 목록에 없으면(끝남) 한 번 `project-requests`(최근 50개)에서 찾는다. `hub_project_requests` 호출은 카드가 아니라 지금처럼 Tool 블록이다.

### 6.7 받은 승인

지금의 목록(에이전트의 승인·질문) 위에 **"프로젝트 요청"** 묶음: `waiting_user`(허가/거절)와 `lost`(정리). 탭 제목 대기 수에 `waiting_user` 수를 더한다.

## 7. 바꾸지 않는 것

- 운영자 로그인, CSP, 토큰을 브라우저·모델·Tool에 주지 않는 원칙(V3-P95). user 토큰은 서버만 든다.
- 모델이 허가·정리·설정·빼기를 하지 못하는 원칙(hub가 user 경로로만 받음).
- 기존 `PUT /api/hub/projects/{project}/preset`과 화면의 권한 묶음 바꾸기.

## 8. 검사

- 중계 pytest(가짜 hub): op마다 필드 다시 만들기·크기 한도, 오류 매핑(409/503 종류), `resolve`의 긴 시간 제한, 목록 스트림의 새 이벤트 두 개 전달과 upstream 재연결 때의 `projects`, Origin 검사.
- Vitest: `useHubRequests` 합치기(먼저 온 끝남 + 늦은 목록, `rev`), 상태별 카드, 이유 문구.
- Playwright(가짜 hub `scripts/web_next_hub.py`에 op 더함): 새 프로젝트(local: 짝 코드 → 가짜 기기 done → 프로젝트 화면), user 방식 폴더 고르기, 모델 요청 허가, `lost` 정리(펜스 성공·기기 없음 → force), 빼기, 보류 채택, 설정 바꾸기.
- 운영: hub와 함께 올린 뒤 laptop(`local`)에서 새 프로젝트, `linux`(`user`)에서 루트 아래 새 폴더, HIMARI에게 "프로젝트 만들어 줘"를 말해 허가까지.

## 9. 단계

| 단계 | 내용 |
| --- | --- |
| P1 | 중계(§4)와 스트림 전달(§4.1), 가짜 hub op |
| P2 | `useHubRequests`, 요청 카드, 새 프로젝트 대화상자, 받은 승인 묶음 |
| P3 | 프로젝트 화면(설정, 기기별 폴더, 빼기, 폴더 더하기), `lost` 정리 |
| P4 | 대화 안의 카드, H8(hub)과 채택 |

hub(#39 + H8)를 먼저 올리고 v3를 올린다. hub만 올라간 동안 v3는 지금처럼 돈다(새 칸·이벤트를 무시).

## 10. 열린 질문

1. 프로젝트 화면을 라우트(`/projects/:name`)로 둘지, 오른쪽 패널의 탭으로 둘지. 권고는 라우트(링크로 열 수 있고 좁은 화면에서도 같음).
2. 받은 승인 화면에 `lost`까지 둘지(권고), 프로젝트 화면에만 둘지.
