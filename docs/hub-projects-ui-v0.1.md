# Web에서 hub 프로젝트와 기기 폴더 다루기 (V3-P103 설계 초안 v0.1)

> 2026-11-18 · rev. 2(Codex 설계 검수 1차 NOT_READY 반영, §12) · **설계 초안** · 대상: v3 `cb56a53`(V3-P102 작업 콘솔 운영 중), hub `f1f5119`(프로젝트 요청 구현, 운영 배포 전)
> 앞 문서: [작업 콘솔](web-console-v0.1.md)(V3-P102), hub [프로젝트 요청 설계](https://github.com/jihunmoon04/himari-hub/blob/main/docs/projects-v0.1.md), hub [user 경로 계약 v1](https://github.com/jihunmoon04/himari-hub/blob/main/docs/user-api-v1.md) §4.6·§5.2

## 1. 목표와 범위

- **Web에서 hub 프로젝트를 만들고, 기기 폴더를 더하고 빼고, 프로젝트 설정을 바꾼다.** hub는 이 일을 user 경로의 op로 이미 받는다(hub #39). v3는 그 op를 중계하고 화면을 붙인다.
- **HIMARI(모델)가 요청한 것은 사용자가 Web에서 허가한다.** 모델의 `hub_project_request`는 hub에서 `waiting_user`로 멈추고, 허가는 user 경로로만 된다. v3는 그 요청을 받은 승인 화면과 대화 안의 카드로 보인다.
- **범위 밖:** HIMARI 자체 프로젝트(대화를 프로젝트에 묶기, 작업 단위), 기기 앱(→ [V3-P104](workbench-v0.1.md)), hub의 허가 방식 바꾸기(그 기기에서만 함).

## 2. hub가 주는 것 (요약)

자세한 것은 hub 계약 §4.6·§5.2.

- **허가 방식(기기마다, 그 기기에서만 정함):** `off`(원격 등록을 받지 않음, 빼기는 받음), `local`(그 기기 화면의 확인 창에서 사용자가 폴더를 고름, 짝 코드), `user`(user 경로의 요청·허가가 곧 허가, roots 아래만).
- **요청 상태:** `waiting_user`(모델의 요청) → `waiting_device`(5분) → `applying` → `done`, 또는 `refused`·`expired`·`failed`·`cancelled`, 그리고 `lost`(claim 뒤 결과를 모름: 사용자가 정리). 종류는 `create`·`add_folder`·`remove_folder`.
- **op:** `project_request`, `project_requests`, `project_request_answer`, `project_request_cancel`, `project_request_resolve`(펜스, 기기를 잃었을 때만 `force`), `project_folder_remove`(즉시 빼지 않고 `remove_folder` 요청을 만듦), `project_set`, `project_adopt`, `runner_dirs`. `projects`의 `runners[]`에 `approve`·`folders`.
- **목록 스트림:** `project_request`(바뀐 요청 하나, `rev`)와 `projects`(본문 없음, 다시 읽으라는 신호). 처음은 늘 `sessions`, 둘 다 SSE `id` 없음.
- 끝난 요청은 hub에 30일 남는다(`lost`는 정리될 때까지).

## 3. hub에 더 필요한 것

계약 v1에 더하기다(H11은 고침).

| id | 무엇 | 왜 |
| --- | --- | --- |
| H8 | user `projects`의 `runners[]`에 `held`(그 기기가 알렸지만 예약 때문에 보류된 이름) | 보류된 폴더를 화면이 늘 찾을 수 있어야 함(요청 카드가 사라진 뒤에도) |
| H11 | **MCP 응답의 요청에서 `result_path`·`code`를 뺀다**(`hub_project_request`·`hub_project_requests`). `path`는 모델이 준 것만 남긴다(`origin = model`), 사용자 요청의 `path`도 뺀다 | hub 설계는 기기 경로를 모델에 보이지 않기로 했는데 지금 hub의 MCP 응답은 user 경로와 같은 뷰를 돌려줌(hub `projects.py`의 `view`). **hub 버그로 고친다** |
| H12 | user `project_requests`에 `ids`(최대 50): 그 요청들을 상태와 상관없이 돌려줌. 없는 id(지워짐)는 빠짐 | 응답을 받지 못한 동작과 끊긴 동안 끝난 요청의 결과를 확인(§5) |
| H13 | `project_request`·`project_folder_remove`에 `client_key`(1~64자): 같은 키로 24시간 안에 다시 오면 새로 만들지 않고 그 요청을 돌려줌 | `hub_unknown` 뒤 다시 보내도 요청이 둘 생기지 않음 |

없는 hub에서의 동작: H8 없음 → 보류 채택 화면을 보이지 않음. H12 없음 → 끊긴 동안 끝난 요청은 "결과 확인 불가". H13 없음 → `hub_unknown` 뒤 "다시 보내기"를 막고 목록을 다시 읽게만 함. H11은 v3와 상관없이 hub에서 고친다.

## 4. 서버 중계 (`/api/hub/…`)

지금의 중계 원칙(V3-P95, V3-P102 §6)을 그대로 쓴다: 운영자만(새 경로 모두 지금의 운영자 검사 함수를 거침), 종류마다 아는 칸만 크기 한도와 함께 다시 만들어 넘김, hub 400 → 409 `hub_refused`(문구 그대로), 연결 실패 → 503 `hub_unavailable`, 401·403 → 503 `hub_misconfigured`, 보냈지만 답을 못 받음 → 503 `hub_unknown`. 상태를 바꾸는 요청은 지금처럼 `OriginGuardMiddleware`를 거친다(쿠키로 오는 요청은 같은 출처만, 쿠키 없는 bearer는 운영자 CLI).

**프로젝트·기기 이름은 URL 경로에 넣지 않는다.** hub 이름 규칙은 `/`·`?`·`#`도 받으므로, 본문(JSON)이나 쿼리 값으로만 넘긴다. 요청 id(정수)만 경로에 둔다.

| v3 경로 | hub op | 비고 |
| --- | --- | --- |
| `GET /api/hub/project-requests` | `project_requests` | `open`(bool), `limit`, `ids`(쉼표, H12) |
| `POST /api/hub/project-requests` | `project_request` | `kind`, `project`, `runner`, `path`?, `create_dir`?, `agent`?, `client_key`(H13) |
| `POST /api/hub/project-requests/{id}/answer` | `project_request_answer` | `approve` |
| `POST /api/hub/project-requests/{id}/cancel` | `project_request_cancel` | |
| `POST /api/hub/project-requests/{id}/resolve` | `project_request_resolve` | `force`?, `outcome`?. 시간 제한 40초(펜스는 기기 답을 30초 기다림) |
| `POST /api/hub/projects/settings` | `project_set` | `project`, `agent`?, `profile`?, `preset`?(준 것만, null은 지움). 지금의 `PUT …/{project}/preset`은 그대로 둠 |
| `POST /api/hub/projects/remove-folder` | `project_folder_remove` | `project`, `runner`, `client_key`(H13) |
| `POST /api/hub/projects/adopt` | `project_adopt` | `project`, `runner` |
| `GET /api/hub/runner-dirs` | `runner_dirs` | `runner`, `path`?. 시간 제한 15초 |

- **요청 레코드(`REQUEST`):** `id`, `kind`, `project`, `runner`, `origin`, `path`, `create_dir`, `agent`, `why`(500자), `code`, `mode`, `state`, `rev`, `result_path`, `reason`, `created_at`, `decided_at`, `expires_at`, `claimed_at`, `ended_at`, `same_name_on`.
- **`PROJECTS`:** `runners[]`에 `approve`(`mode`, `roots`, `why`), `folders`({이름: 경로}), `held`(H8). 기기 경로와 짝 코드는 운영자 화면에만 간다. v3는 이 칸들을 모델에게 주지 않는다(모델은 hub의 MCP로만 보고, 그것은 H11이 막는다).
- 로그에는 요청 id·상태·종류만 남기고 경로·코드는 남기지 않는다.

### 4.1 `hub_unknown` 뒤

| 동작 | 화면이 하는 것 |
| --- | --- |
| 요청 만들기, 폴더 빼기 | 같은 `client_key`로 다시 보낼 수 있다(H13: 같은 요청이 돌아옴). H13이 없으면 다시 보내기를 막고 열린 요청을 다시 읽어 그 키의 요청(같은 `kind`·`project`·`runner`)이 있는지 보인다 |
| 허가·거절, 취소 | 그 요청을 `ids`로 다시 읽는다(H12). 아직 그 전 상태면 다시 할 수 있다 |
| 정리(`resolve`) | 단추는 답이 올 때까지 다시 누를 수 없다(같은 화면의 중복). `hub_unknown`이면 `ids`로 다시 읽어 `lost`가 아니면 그 결과를 보이고, 아직 `lost`면 다시 누를 수 있다(펜스는 같은 답을 다시 줌) |
| 설정 바꾸기, 채택 | `projects`를 다시 읽어 바뀌었는지 보인다 |

### 4.2 목록 스트림

v3는 hub 목록 스트림 하나를 브라우저들에 나눠 준다(V3-P102 §6). 거기에 두 이벤트를 더한다.

- **`project_request`·`projects`:** 받은 그대로(요청은 레코드를 다시 만들어) 모든 브라우저에 보낸다. v3는 들고 있지 않는다.
- **upstream이 다시 붙을 때와 폴링으로 내려갈 때:** 그 사이의 이벤트는 잃었을 수 있다. v3는 새 `sessions`를 보낼 때 `projects`를 한 번 보낸다.
- **폴링 동안:** v3는 30초마다 `projects`를 보낸다(그동안 프로젝트·기기·요청의 변화는 이것으로만 알림). 화면은 이것을 받을 때마다 §5의 다시 읽기를 한다.
- 브라우저 스트림의 인증 수명(연결마다 15초 다시 확인, 로그아웃이면 닫힘)은 지금 그대로 이 이벤트에도 적용된다.
- `sessions`·`changed`의 뜻과 순서는 바꾸지 않는다. 새 이벤트에는 `id`가 없다.

## 5. 화면 상태

- **`useHubRequests`:** 열린 요청 전체와, 화면이 따라가는 요청 id들(카드가 보이는 것)을 들고 있는다.
  - 스트림을 연 뒤 `project-requests?open=true`를 읽고, 그 사이 온 `project_request`는 모아 두었다가 `id`별로 `rev`가 가장 큰 것을 남긴다. 끝난 상태가 오면 열린 목록에서 빼되, 따라가는 id면 끝난 레코드를 들고 있는다.
  - **다시 연결할 때와 `projects` 신호를 받을 때:** 열린 목록을 새로 읽고, 따라가는 id 중 열린 목록에 없는 것은 `ids`(H12)로 읽어 끝난 결과를 얻는다. 거기에도 없으면(30일이 지나 지워짐) "결과를 확인할 수 없습니다".
- **`projects` 신호:** `useHubProjects`를 다시 읽는다(읽는 중에 또 오면 끝난 뒤 한 번 더).

## 6. 화면

### 6.1 새 프로젝트

사이드바 Projects 머리에 **+** 단추 → 대화상자.

1. **이름:** hub 규칙(80자, 앞뒤 공백 없음, 인쇄 가능한 글자). 이미 있는 이름이면 그 자리에서 알린다.
2. **기기:** 연결된 기기 목록. 기기마다 방식(`local` "그 기기 화면에서 고름", `user` "여기서 고름(roots 아래)")을 보인다. `off`나 끊긴 기기, 요청을 받지 않는 기기(공유 토큰·옛 실행기: `approve.mode`가 `off`)는 흐리게 하고 이유(`why`)와 "그 기기에서 `himari-hub runner approve`"를 보인다.
3. **폴더:**
   - `local`: "폴더는 그 기기 화면에서 고릅니다." 선택으로 "새 폴더 만들기"와 "시작 위치"(제안).
   - `user`: 폴더 고르기(§6.5)와 "새 폴더 만들기"(고른 폴더 아래 이름 하나: 구분자·`.`·`..` 없음). 경로는 고른 결과로만 채운다.
4. **기본 에이전트**(선택).
5. **요청**(대화상자를 열 때 만든 `client_key`와 함께) → 요청 카드(§6.3). `done`이면 그 프로젝트 화면으로 옮긴다.

### 6.2 프로젝트 화면 `/project?name=<이름>`

이름은 쿼리 값으로(URL 인코딩). 사이드바 프로젝트 이름 옆 정보 단추로 연다.

- **설정:** 기본 에이전트, 프로필, 권한 묶음. 권한 묶음은 바꾸기 전에 확인을 받는다.
- **기기별 폴더:** 이 프로젝트를 가진 기기: 이름, 연결, 폴더(`folders`), **빼기**. 빼기는 `remove_folder` 요청을 만들고 그 카드(§6.3)를 보인다(그 기기가 `runner.json`에서 뺄 때 끝남). 확인 대화상자: "그 기기의 `runner.json`에서만 빠지고, 폴더와 작업 기록은 그대로입니다." `off` 기기에도 된다.
- **기기에 폴더 더하기:** §6.1의 2~3단계와 같은 대화상자(`kind: add_folder`).
- **보류된 폴더(H8):** 이 이름을 보류 중인 기기 + **채택**.
- **이 프로젝트의 열린 요청 카드.**

**사이드바의 보류 목록:** 프로젝트가 아직 없는 이름이 어떤 기기에서 보류 중이면(H8: 그 이름의 프로젝트가 `projects`에 없음) Projects 아래 "보류된 폴더"에 그 이름과 기기를 보이고 **채택**(그 이름의 프로젝트를 만들고 붙임)을 둔다. 채택은 그 기기가 연결돼 있고 지금도 그 이름을 알릴 때만 된다(아니면 hub 문구).

### 6.3 요청 카드

새 프로젝트 대화상자, 프로젝트 화면, 받은 승인 화면, 대화 안(§6.6)에서 같은 컴포넌트를 쓴다. 문구는 종류(`kind`)마다 다르다.

| 상태 | 보이는 것 | 할 수 있는 것 |
| --- | --- | --- |
| `waiting_user` | "HIMARI가 요청함", 이유(`why`), 기기, 종류, 경로 | 허가 / 거절 |
| `waiting_device`(local, 더하기) | "**{기기} 화면에서 폴더를 고르세요**", 짝 코드(크게), 남은 시간 | 취소 |
| `waiting_device`(user, 또는 빼기) | "기기가 확인하는 중" | 취소 |
| `applying` | 더하기 "기기가 등록하는 중" / 빼기 "기기가 빼는 중" | — |
| `lost` | 더하기 "기기에 등록됐는지 모릅니다" / 빼기 "기기에서 빠졌는지 모릅니다" | **정리**(§6.4) |
| `done` | 더하기: 결과 경로, 프로젝트로 가기 / 빼기: "빠졌습니다" | — |
| `refused`·`expired`·`failed`·`cancelled` | 이유(`reason`을 사람 말로, 모르는 값은 그대로) | 다시 요청(같은 값으로 대화상자를 엶, 새 `client_key`) |
| 확인 불가 | "결과를 확인할 수 없습니다(기록이 지워짐)" | 프로젝트 다시 읽기 |

### 6.4 `lost` 정리

1. **정리** → `resolve`(force 없이). 기기가 연결돼 있으면 기기가 lock 안에서 답하고 요청이 끝난다(최대 30초, 단추는 그동안 "기기에 묻는 중").
2. 기기가 연결돼 있지 않거나 답이 없으면(409) 문구를 보이고 단추를 그대로 둔다. 그 아래에 접힌 **"기기를 잃었을 때"**: 사용자가 고르고, 결과를 함께 적는다.
   - 더하기·만들기 — 적용 안 됨: "그 기기가 다시 켜져 실제로 등록돼 있었다면, 그 기기의 폴더는 기본 에이전트 없이 프로젝트가 됩니다(만들기일 때)." 적용됨: "기기에 없다면 폴더 없는 프로젝트가 남습니다(만들기일 때)."
   - 빼기 — 빠짐(`applied`): "기기에 남아 있다면 그 폴더는 다음 연결 때 다시 이 프로젝트의 폴더로 알려집니다." 안 빠짐(`not_applied`): "기기에서 빠져 있다면 다음 연결 때 목록에서 사라집니다." 빼기는 어느 쪽이든 원장에서 바뀌는 것이 없다(기기의 실제 상태가 다음 보고로 보임).
3. 모델은 정리하지 못한다(이 단추는 운영자 화면에만).

### 6.5 폴더 고르기 (`user` 방식)

`runner-dirs`로 루트 목록 → 한 단계씩 들어감(이름만, 숨김·링크는 기기가 뺌). 고른 경로를 그대로 `path`로 보낸다(서버는 해석하지 않고 기기가 검사). 시간 제한·오류는 문구로.

### 6.6 대화 안의 카드

HIMARI의 `<hub source>/hub_project_request` Tool 호출을 맡긴 작업 카드(V3-P102 §5.1)처럼 카드로 그린다. 결과의 요청 id를 따라가는 id로 등록한다(§5). 카드는 운영자 화면이므로 user 경로의 레코드(경로·코드 포함)를 보인다. `hub_project_requests` 호출은 지금처럼 Tool 블록이다.

### 6.7 받은 승인

지금의 목록 위에 **"프로젝트 요청"** 묶음: `waiting_user`(허가/거절)와 `lost`(정리). 탭 제목 대기 수에 `waiting_user` 수를 더한다.

## 7. 바꾸지 않는 것

- 운영자 로그인, CSP, 토큰을 브라우저·모델·Tool에 주지 않는 원칙(V3-P95). user 토큰은 서버만 든다.
- 모델이 허가·정리·설정·빼기를 하지 못하는 원칙(hub가 user 경로로만 받음).
- 기존 `PUT /api/hub/projects/{project}/preset`(이름이 경로에 들어가는 문제는 그대로 남음: 권한 묶음은 새 `settings`로도 바꿀 수 있음).

## 8. 검사

- 중계 pytest(가짜 hub):
  - 새 REST 경로 모두 운영자만(비운영자·기기 토큰 거절), 쿠키 요청의 Origin 검사, 응답과 로그에 토큰이 없고 경로·코드는 운영자 응답에만.
  - op마다 필드 다시 만들기·크기 한도, 오류 매핑(409/503 종류), `resolve`의 긴 시간 제한, `client_key`·`ids` 전달.
  - 이름에 `/`·`?`·`#`·한글·공백이 있는 프로젝트의 설정·빼기·채택.
  - 목록 스트림: 새 이벤트 둘 전달, upstream 재연결 때 `projects`, 폴링 동안 30초마다 `projects`, 로그아웃하면 15초 안에 닫힘.
- Vitest: `useHubRequests`(먼저 온 끝남 + 늦은 목록, `rev`, 따라가는 id를 `ids`로 찾음, 못 찾음 → 확인 불가), 종류·상태별 카드 문구, `hub_unknown` 뒤 동작.
- Playwright(가짜 hub `scripts/web_next_hub.py`에 op 더함): 새 프로젝트(local: 짝 코드 → done → 프로젝트 화면), user 방식 폴더 고르기, 모델 요청 허가, `lost` 정리(펜스 성공·기기 없음 → force, 빼기 포함), 빼기 카드, 보류 채택(프로젝트 있음·없음), 설정, 특수 문자 이름.
- 운영: hub와 함께 올린 뒤 laptop(`local`)에서 새 프로젝트, `linux`(`user`)에서 루트 아래 새 폴더, HIMARI에게 "프로젝트 만들어 줘"를 말해 허가까지, MCP 응답에 경로가 없는지(H11).

## 9. 단계

| 단계 | 내용 |
| --- | --- |
| H | hub H8·H11·H12·H13(한 PR) |
| P1 | 중계(§4)와 스트림 전달(§4.2), 가짜 hub op |
| P2 | `useHubRequests`, 요청 카드, 새 프로젝트 대화상자, 받은 승인 묶음 |
| P3 | 프로젝트 화면(설정, 기기별 폴더, 빼기, 폴더 더하기), `lost` 정리, 보류 채택 |
| P4 | 대화 안의 카드 |

hub를 먼저 올리고 v3를 올린다. hub만 올라간 동안 v3는 지금처럼 돈다(새 칸·이벤트를 무시).

## 10. 열린 질문

1. 받은 승인 화면에 `lost`까지 둘지(권고), 프로젝트 화면에만 둘지.

## 11. 이 판에 없는 것

- 끝난 요청의 긴 기록 화면(hub가 30일 남김: 프로젝트 화면에 열린 것과 따라가는 것만).
- 기기의 허가 방식이나 roots를 Web에서 바꾸기(hub가 그 기기에서만 받음).

## 12. 설계 검수 (Codex)

### 1차 (rev. 1 → rev. 2): NOT_READY, P1 다섯, P2 셋

- P1 모델에 경로가 나감(hub의 MCP 응답이 user 뷰를 그대로 줌) → hub 버그로 H11(§3), v3는 운영자 화면에만(§4).
- P1 `hub_unknown` 뒤 다시 하기가 안전하지 않음 → H12(id로 읽기)·H13(`client_key`), 동작별 처리 표, 정리 중복 막기(§3, §4.1).
- P1 끊긴 동안 끝난 요청을 잃음 → 따라가는 id를 `ids`로 다시 읽기, 못 찾으면 확인 불가(§5, §6.3).
- P1 빼기의 상태와 `force`의 뜻이 빠짐 → 빼기도 요청 카드로 따라가고, 종류별 문구와 `force` 결과(§6.2, §6.3, §6.4).
- P1 이름과 URL 경로가 충돌 → 이름은 본문·쿼리로만, 화면 경로 `/project?name=`, 특수 문자 시험(§4, §6.2, §8).
- P2 폴링 동안 프로젝트 갱신 → 30초마다 `projects`(§4.2).
- P2 프로젝트 없는 보류 이름의 채택 → 사이드바 보류 목록(§6.2).
- P2 보안 검사 → 새 REST·SSE 모두 운영자만, Origin, 로그아웃 뒤 닫힘, 토큰·경로가 로그에 없음(§4, §8).
