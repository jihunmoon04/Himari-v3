# MCP Apps 1단계 — 읽기 전용 화면 표시 v0.1

> 2026-10-15 · **PROPOSED (V3-P58)** · 1단계 구현됨(브랜치 `feat/mcp-apps-view`). 설계와 달라진 점은 §10.
>
> 기준 사양: MCP Apps 확장 `io.modelcontextprotocol/ui`, **안정판 2026-01-26** ([apps.mdx](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx), SEP-1865 Final). `draft`는 따르지 않는다.
>
> 이미 정한 것(2026-10-15 사용자 결정): 커넥터 패키지 규칙(V3-059), 화면 HTML은 Tool을 실행할 때 스냅샷으로 Data에 저장, sandbox origin은 같은 Web 서버의 별도 포트, 화면에 저장소 없음(opaque View), 가장 최근 화면은 자동 표시, 실행 중에도 표시.

## 1. 범위

**1단계는 MCP 서버가 준 화면(View)을 Tool 호출 옆에 보여 주기만 한다.** 실행 중에는 입력을, 끝나면 결과를 보인다. 실행 경로(Control·Dispatcher·claim·Pause/Cancel)는 바뀌지 않는다.

| 포함 | 제외 (단계) |
| --- | --- |
| Tool의 `_meta.ui.resourceUri` 해석, `resources/read`로 HTML 받기 | 화면이 시작하는 `tools/call`·`resources/read` (2단계) |
| HTML·CSP를 실행 기록과 함께 저장(SQLite v5) | 앱 전용 Tool(`visibility: ["app"]`) (2단계) |
| Web: sandbox origin의 proxy, 화면 iframe, 호스트 브리지 | `ui/message`, `ui/update-model-context` (3단계) |
| 가장 최근 화면 자동 표시, 실행 중 표시(입력 → 결과 또는 취소) | |
| View 메시지: `ui/initialize`, `initialized`, `size-changed`, `ui/open-link`, `ping` | `ui/request-display-mode`(inline만), `tool-input-partial`(스트리밍 없음) |
| 운영자 설정: 서버별 화면 켜기, CSP 도메인 상한 | 카메라·마이크 등 `permissions` 허용 |
| | CLI 화면 표시(지금처럼 텍스트) |

모델이 보는 것은 바뀌지 않는다. 모델은 지금처럼 `content`의 텍스트를 받고, `structuredContent`와 HTML은 화면에만 간다(사양의 Data Passing 권장과 같음).

## 2. 사양에서 가져오는 규칙

- **Tool과 화면 연결:** `tools/list` 항목의 `_meta.ui.resourceUri`(`ui://`로 시작). 폐기 예정인 평평한 `_meta["ui/resourceUri"]`는 읽지 않는다.
- **visibility:** 생략하면 `["model", "app"]`. `"model"`이 없는 Tool은 모델 목록에 넣으면 안 된다(MUST NOT). 1단계는 앱이 부를 수단이 없으므로 이런 Tool은 등록하지 않는다(§4.2).
- **리소스:** `resources/read`의 `contents` 중 요청한 URI와 같은 항목 하나. `mimeType`은 `text/html;profile=mcp-app`, 본문은 `text` 또는 `blob`(base64). `_meta.ui`에 `csp`(`connectDomains`·`resourceDomains`·`frameDomains`·`baseUriDomains`), `permissions`, `domain`, `prefersBorder`가 있다.
- **CSP:** 호스트는 선언된 도메인으로 CSP를 만들어야 하고(MUST), 선언되지 않은 도메인을 허용하면 안 된다. 더 좁히는 것은 허용된다(MAY). 선언이 없으면 사양의 기본 CSP를 쓴다.
- **Web 호스트의 이중 iframe:** 호스트와 sandbox는 **origin이 달라야** 하고(MUST), sandbox(바깥 proxy iframe)는 `allow-scripts`·`allow-same-origin`을 가진다. proxy는 `ui/notifications/sandbox-proxy-ready`를 보내고, 호스트는 `ui/notifications/sandbox-resource-ready`로 HTML을 넘긴다. proxy는 `ui/notifications/sandbox-*`를 제외한 모든 메시지를 양방향으로 중계한다. 호스트는 View의 `ui/notifications/initialized` 전에는 View에 아무것도 보내지 않는다.
- **데이터 전달:** `initialized` 뒤에 `ui/notifications/tool-input {arguments}`를 한 번 보낸다. 화면이 실행 중에 떠 있었다면, 실행이 끝날 때 `ui/notifications/tool-result`(표준 `CallToolResult`)를 보내야 한다(MUST). 실행이 어떤 이유로든 취소되면 `ui/notifications/tool-cancelled {reason}`을 보내야 한다(MUST).
- **협상:** 클라이언트는 `initialize`의 `capabilities.extensions["io.modelcontextprotocol/ui"] = {"mimeTypes": ["text/html;profile=mcp-app"]}`로 지원을 알린다. 서버는 이것을 보고 화면이 있는 Tool을 등록할 수 있고, 화면이 없는 호스트를 위해 텍스트 대체를 줘야 한다.

## 3. 핵심 결정

### 3.1 HTML은 연결할 때 받고, Tool을 실행할 때 스냅샷으로 고정한다

"실행할 때 스냅샷 저장"(사용자 결정)을 이렇게 구현한다:

1. **연결할 때(`connect`)** 화면을 켠 서버의, 등록된 Tool이 가리키는 `ui://` 리소스를 `resources/read`로 받아 검사하고 메모리에 둔다(URI별 한 번). 사양도 연결 시점의 미리 받기와 검토를 권한다.
2. **Tool을 실행할 때** handler는 `tools/call` 결과와 함께 그 리소스를 `ToolOutcome`에 담아 돌려주고, Dispatcher가 결과와 **같은 트랜잭션으로** Data에 저장한다(V3-042: Tool 구현은 원장을 모른다).

`tools/call` 뒤에 `resources/read`를 하지 않는 이유가 있다. Cancel(V3-057)은 진행 중인 Tool을 asyncio로 취소하고, claim된 호출을 결과 불명으로 닫는다. 결과를 받은 뒤 네트워크를 한 번 더 기다리면, **이미 끝난 호출이 UNKNOWN이 되는 구간이 넓어진다.** 연결할 때 받아 두면 실행 경로에 새 대기가 생기지 않는다.

대가: 서버가 HTML을 바꿔도 다시 시작해야 반영된다. Tool 목록과 같은 규칙이다(V3-055: 다시 연결은 재시작). 이미 저장된 화면은 바뀌지 않는다. 예전 대화는 그때 실행된 코드 그대로 보인다.

### 3.2 화면은 운영자가 서버별로 켠다

화면은 서버가 준 코드를 사용자의 브라우저에서 실행한다. Tool 허용 목록(V3-055)과 같은 이유로, 기본은 꺼짐이고 운영자가 `mcp.json`에서 서버별로 켠다(§4.1). 끈 서버에는 UI capability를 선언하지 않으므로, 서버는 사양대로 텍스트 전용 Tool을 등록할 수 있다.

### 3.3 CSP = 서버 선언 ∩ 운영자 상한, 볼 때 계산한다

- 운영자는 서버별로 허용할 `connect_domains`·`resource_domains` 상한을 적는다. 기본은 빈 목록, 곧 외부 연결 없음이다.
- 실제 CSP의 도메인 = **서버가 선언한 항목 중 운영자 목록에 똑같이 있는 것**(정규화 후 문자열 비교. 와일드카드도 같은 문자열일 때만 인정).
- `frameDomains`·`baseUriDomains`·`permissions`는 1단계에서 허용하지 않는다(`frame-src 'none'`, `base-uri 'self'`, `allow` 속성 없음).
- 저장하는 것은 **서버가 선언한 값**이다. 교집합은 화면을 열 때 그때의 운영자 설정으로 계산한다. 그래서 운영자가 도메인을 빼거나 서버의 화면을 끄고 다시 시작하면(`mcp.json`은 시작할 때 읽는다), 예전 대화의 화면을 새로 띄울 때도 적용된다. 이미 열려 있는 화면은 예외다(§3.7 보장 수준). 감사를 위해 선언 값은 그대로 남는다.

### 3.4 Sandbox: 별도 포트, 서명된 CSP, opaque View

```
메인 origin  http://127.0.0.1:8765          (Web 화면·API, 세션 쿠키)
  └ iframe ── sandbox origin http://127.0.0.1:8766/sandbox?csp=…&sig=…
                (HIMARI가 쓴 proxy. 응답 헤더 CSP = 이 화면의 CSP)
                └ iframe srcdoc, sandbox="allow-scripts"   (View: opaque origin)
```

- **별도 포트의 작은 서버:** `/sandbox`(proxy 페이지)와 `/sandbox.js`만 서빙하는 ASGI 앱을 같은 프로세스에서 두 번째 포트로 띄운다. API 경로는 하나도 없고 세션을 모른다. 화면을 켠 서버가 하나도 없으면 띄우지 않는다.
- **CSP는 proxy 페이지의 응답 헤더로:** `srcdoc` 문서는 부모의 CSP를 물려받는다. 참조 구현처럼 proxy 응답 헤더에 그 화면의 CSP를 넣는다. 사양의 CSP 틀에 §3.3의 도메인을 넣고, `frame-ancestors <서명에 묶인 메인 origin>`을 더한다. `meta` 태그와 달리 View가 바꿀 수 없다. 이 구성이 실제 브라우저에서 동작하는지는 §3.6에서 확인했다.
- **CSP 파라미터는 서명한다(PR #27 검수 B1):** 누군가 HIMARI의 sandbox origin을 느슨한 CSP로 띄워 쓰는 것을 막는다.
  - **키:** 프로세스가 시작할 때 무작위 32바이트로 만들고 메모리에만 둔다(운영자 비밀에서 파생하지 않는다). 운영자 정책(`mcp.json`)은 시작할 때만 읽으므로, 정책이 바뀌는 때가 곧 키가 바뀌는 때다. 그래서 재시작하면 예전 정책으로 발급한 URL은 모두 무효가 된다. 운영자가 도메인을 거둔 뒤 예전 서명 URL을 다시 쓰는 길이 없다.
  - **서명하는 것:** 정규화한 JSON `{"v": 1, "csp": <실제 CSP 도메인>, "host": <이 화면을 띄울 메인 origin 하나>, "exp": <발급 시각 + 10분>}`. 메인 API가 이것을 base64url로 `p`에, HMAC-SHA256을 `sig`에 넣어 `sandbox_url`을 준다.
  - **sandbox 서버:** 서명이 틀리거나, 만료됐거나, `v`를 모르면 400을 준다. `frame-ancestors`에는 `host` 하나만 넣는다. 이미 로드된 화면은 만료 뒤에도 계속 동작한다(검사는 proxy를 로드할 때 한 번). 화면을 다시 열 때는 API에서 새 URL을 받는다.
  - **묶지 않는 것:** 화면(digest)이나 호출은 묶지 않는다. HTML은 postMessage로 가므로, 서명이 허락하는 것은 "이 CSP의 빈 proxy를 이 메인 origin 아래에서 10분 안에 띄우는 것"뿐이다. proxy 페이지에는 데이터가 없다.
  - **이후:** 재시작 없이 정책을 바꾸게 되면 정책 세대 번호를 서명에 넣는다.
- **View는 opaque origin:** 사양의 proxy와 달리 안쪽 iframe에 `allow-same-origin`을 주지 않는다(`sandbox="allow-scripts"`, 사양이 허용하는 더 좁은 설정). 모든 서버의 화면이 sandbox origin 하나를 같이 쓰므로, 같은 origin을 주면 서로의 localStorage·IndexedDB와 proxy 페이지에 닿는다. 대가: 저장소를 쓰는 앱은 동작하지 않는다(사양은 앱에 기능 감지를 요구한다). **사용자 결정(2026-10-15): 이 대가를 받아들인다.** 동시에 띄우는 화면 수를 줄이는 것으로는 대신할 수 없다. 저장소는 화면을 닫아도 남아서 다음에 뜬 다른 서버의 화면이 읽을 수 있고, 같은 origin 권한이면 service worker로 이후의 proxy 요청을 가로챌 수도 있다. 저장소가 필요해지면 서버별 origin(서버마다 포트 또는 하위 도메인)으로 간다. Claude·ChatGPT도 앱마다 origin을 나눈다.
- **쿠키는 포트로 나뉘지 않는다:** sandbox origin은 메인과 같은 사이트라 `SameSite=Strict` 쿠키도 같이 간다. 그래서 막는 것은 쿠키 속성이 아니라 서버 쪽 규칙이다:
  - 세션 쿠키는 `HttpOnly`라 스크립트로 읽을 수 없다(지금도 그렇다).
  - CORS를 열지 않으므로 다른 origin은 API 응답을 읽을 수 없다.
  - 상태를 바꾸는 요청은 `OriginGuard`가 막는다(sandbox origin은 허용 목록에 없다).
  - View의 `connect-src`에는 메인 origin이 없다(운영자가 넣지 않는 한).

  이 네 가지를 각각 테스트로 고정한다(§8).
- **메인 페이지 CSP:** 지금의 고정 CSP에 `frame-src <sandbox origin들>`을 더한다. 메인의 `frame-ancestors 'none'`과 `X-Frame-Options: DENY`는 그대로다.

### 3.5 가장 최근 화면은 자동으로, 실행 중부터 보인다

사용자 결정(2026-10-15): 가장 최근 화면 자동 표시, 실행 중 표시.

- **자동 표시:** 대화에서 화면이 있는 **가장 최근 Tool 호출 하나**만 자동으로 띄운다. 대화를 열 때와 새 호출이 나타날 때 다시 고른다. 나머지 호출은 Tool 행의 "Open view" 버튼으로 연다. 모든 화면을 자동으로 띄우지 않는 것은, 긴 대화에서 브라우저 자원을 많이 쓰고(사양의 "Resource consumption") 사용자가 보지 않는 코드가 돌기 때문이다.
- **동시 3개:** 동시에 살아 있는 화면은 3개까지이고, 넘으면 가장 오래 전에 띄운 것을 닫는다. 대화를 바꾸거나 화면을 닫을 때는 `ui/resource-teardown`을 보내고 최대 1초 기다린다.
- **실행 중 표시:** 호출이 기록되면(`assistant_tool_calls` 이벤트) 결과를 기다리지 않고 화면을 띄운다. HTML은 연결할 때 받아 둔 것(§3.1)이라 실행 전에도 있다. 결과에 함께 저장되는 것도 같은 리소스다. 화면이 초기화되면 `tool-input`을 보내고, 호출이 끝나면 다음을 보낸다:

  | 호출이 끝난 모양 | 화면에 보내는 것 |
  | --- | --- |
  | `SUCCEEDED`·`FAILED`이고, 결과와 함께 화면이 저장됨 | `ui/notifications/tool-result` (저장된 payload) |
  | 위와 같지만, 저장된 화면의 digest가 띄운 화면과 다름 (그 사이 재시작해서 HTML이 바뀜) | 저장된 화면으로 다시 띄운다 |
  | `CANCELLED`·`DENIED` — 실행되지 않음 (사용자가 멈춤·방향 전환·Dismiss, 권한 거부, 인자 오류) | `ui/notifications/tool-cancelled {reason}` |
  | `SUCCEEDED`·`FAILED`인데 화면이 저장되지 않음 (결과가 1 MiB를 넘음, 서버가 실행하지 않음 등) | **`tool-cancelled`를 보내지 않는다.** 호스트가 화면을 닫고(`ui/resource-teardown` → iframe 제거), 그 자리에 "This result can't be shown in the view"를 보인다. 결과는 Tool 행의 텍스트로 보인다 |
  | `UNKNOWN` — 실행됐을 수 있음 (V3-047) | `tool-cancelled`도 결과도 보내지 않는다. 호스트가 화면을 닫고 "The outcome of this call is unknown"을 보인다 |

  **원칙(PR #27 재검수 N1):** 화면에 보내는 알림은 호출의 실제 상태와 맞아야 한다. 사양은 실행이 끝나면 `tool-result`, 취소되면 `tool-cancelled`로 나눈다. 그래서 `tool-cancelled`는 **실행되지 않은 호출**에만 보낸다. 실행된 호출이나 실행됐을 수 있는 호출을 취소됐다고 알리면 안 된다. 결과를 화면에 줄 수 없을 때는, 그것을 호출 상태로 꾸미지 않고 호스트 자신의 표시 실패로 다룬다. 사용자 확인 없이 결과를 성공·실패로 꾸미지 않는다는 V3-047과 같은 원칙이다.

  - Pause로 멈춘 호출(`PENDING`)의 화면은 입력만 받은 채 기다린다. Continue로 실행되면 결과를 받고, 새 메시지나 Dismiss로 닫히면(`CANCELLED`) `tool-cancelled`를 받는다.
  - 결과는 지금처럼 이벤트 폴링(1초)으로 알게 되므로, 화면에 반영되기까지 최대 약 1초 걸린다.
  - 실행 중인 화면도 저장된 화면과 같은 규칙을 따른다: 서버별 켜짐(§3.2), CSP 교집합(§3.3), sandbox(§3.4), 그리고 대화별 출처 켜짐(§3.7).
  - 실행 중 표시 때문에 `ToolDefinition`을 바꾸지 않는다. Web이 Application을 통해 커넥터에 "이 Tool의 지금 화면"을 묻는다(§4.2).

화면 테두리 위에는 항상 "App view from `<서버 이름>` · sandboxed"를 보인다. `prefersBorder`와 상관없이 보인다(사양의 사회공학 위험 대응).

### 3.6 브라우저 검증 (PR #27 검수 B3, 2026-10-15)

§3.4의 이중 iframe 구성을 실제 브라우저에서 확인했다. 환경: **Chrome for Testing 156.0.8078.4 (headless shell)**, 이 호스트에서 loopback 두 포트. 메인 origin은 지금의 HIMARI CSP + `frame-src <sandbox>`, 쿠키는 `HttpOnly; SameSite=Strict`. proxy는 사양 CSP 틀(선언 도메인 없음) + `frame-ancestors <메인>` 헤더, 안쪽 View는 `sandbox="allow-scripts"` + `srcdoc`. 재현: `python -I scripts/mcp_apps_probe/run.py`(14개 확인, 하나라도 틀리면 종료 코드 1).

| 확인한 것 | 결과 |
| --- | --- |
| proxy 로드, `sandbox-proxy-ready` → `sandbox-resource-ready` | 통과 |
| proxy의 `frame-src 'none'`이 안쪽 `srcdoc` iframe을 막지 않음 | 통과 (View 실행됨) |
| 물려받은 `frame-ancestors <메인>`이 안쪽 `srcdoc`을 막지 않음 | 통과 |
| View의 인라인 스크립트 실행, `ui/initialize` 왕복, `initialized` → `tool-input`·`tool-result` 전달 | 통과 |
| View가 CSP를 물려받음 (자기 CSP 없이 `connect-src`·`frame-src` 위반 발생) | 통과 |
| View origin이 opaque (`self.origin === "null"`) | 통과 |
| View에서 메인·proxy DOM 접근, localStorage, `document.cookie` | 모두 `SecurityError` |
| View의 팝업(`window.open` → `null`), top 이동(`SecurityError`), 중첩 iframe(`frame-src` 위반) | 모두 막힘 |
| View·proxy의 메인 API 요청 (기본 CSP) | `connect-src` 위반, 메인에 아무 요청도 도착하지 않음 |
| **대조 실험:** `connect-src`에 메인을 일부러 넣었을 때 | proxy(sandbox origin)의 POST는 **쿠키를 가지고** 도착(Origin = sandbox) → `OriginGuard`가 403. View의 POST는 Origin `null`, 쿠키 없음 |

**음성 대조:** 같은 스크립트에서 View에 참조 구현처럼 `allow-same-origin`을 주면 14개 중 5개가 실패한다. View의 origin이 sandbox origin이 되고, proxy DOM과 localStorage에 닿으며, View의 POST가 **세션 쿠키를 가지고** 메인에 도착한다(이 경우 `OriginGuard`만 남는다). 그래서 확인 항목들이 실제로 차이를 잡아낸다는 것과, View를 opaque로 둔 선택(§3.4)의 근거가 함께 확인된다.

대조 실험이 §3.4의 판단을 뒷받침한다. 포트만 다른 origin은 같은 사이트라 `SameSite=Strict` 쿠키가 함께 간다. 그래서 막는 것은 `OriginGuard`이고, View를 opaque로 두면 쿠키 자체가 가지 않는다.

**남은 확인:** Firefox·Safari는 이 호스트에서 돌릴 수 없어 확인하지 못했다. 구현 PR에서 돌릴 수 있는 곳을 찾고, 안 되면 지원 브라우저를 Chromium 계열로 적는다.

**대체 구성** (어떤 브라우저가 proxy CSP 아래의 `srcdoc`을 막는 경우): View를 `srcdoc`이 아니라 sandbox 서버가 직접 서빙하는 문서로 띄운다(`/view?p=…&sig=…`). 그 응답에 View 전용 CSP 헤더와 CSP `sandbox allow-scripts` 지시어(속성과 별개로 opaque를 강제)를 넣고, proxy 자신은 `script-src 'self'; frame-src <sandbox>`만 갖는다. 이 경우 sandbox 서버가 서명에 묶인 digest로 HTML을 읽어야 하므로, 서명에 digest를 넣고 sandbox 서버에 Data 읽기를 준다. 1차 구성이 통과했으므로 이것은 쓰지 않는다.

### 3.7 대화에서 끈 출처의 화면은 띄우지 않는다 (PR #27 재검수 N2)

대화별 External Tool 출처 선택(V3-055, `ExternalAccess`)은 화면에도 적용한다. 출처가 꺼져 있으면 모델이 그 출처의 Tool을 불러도 Dispatcher가 거부한다(`DENIED`). 그런데 실행 중 표시는 호출이 기록되는 즉시 화면을 띄우므로, 확인하지 않으면 **거부될 호출의 화면 코드가 먼저 실행된다.**

- **화면을 보일 수 있는 조건** = 운영자가 그 서버의 화면을 켬(§3.2) **그리고** 이 대화에서 그 출처가 지금 켜져 있음(`ExternalAccess.enabled_sources(scope)`). `running`·`done` 모두, 그리고 이벤트의 `app_view` 플래그도 같은 조건으로 계산한다.
- 대화에서 출처를 끄면 그 출처의 화면은 예전 것도 띄우지 않는다. 출처를 끈다는 것은 이 대화에서 그 서버의 것을 쓰지 않겠다는 뜻이고, 저장된 화면도 그 서버의 코드다. 결과 텍스트는 지금처럼 보인다.
- 이미 떠 있는 화면은 다음 때 닫는다(`ui/resource-teardown` → iframe 제거 → "This server is turned off in this conversation"):
  - 같은 탭에서 사용자가 출처를 끈 직후
  - Tool 출처 목록을 다시 읽을 때(대화를 열 때, 작업이 끝날 때) 꺼져 있으면
  - 화면 조회가 `source_disabled`로 거부될 때(예: 실행 중 화면의 결과를 받으러 갈 때)
- **철회의 보장 수준 (PR #27 재검수 C1):** 끄거나 거둔 권한은 **새로 띄우는 화면**에는 즉시 적용되지만, **이미 로드된 화면**은 닫힐 때까지 그대로 돈다.
  - 다른 탭에 떠 있는 화면은 그 탭이 위 세 가지 중 하나를 만날 때까지 남는다. 운영자가 도메인을 거두고 다시 시작한 경우에도, 이미 열린 탭의 화면은 다시 로드될 때까지 예전 CSP를 쓴다(로드된 문서의 CSP는 바뀌지 않는다).
  - 그동안 화면은 HIMARI에는 닿지 못한다(sandbox origin, opaque View, CORS 없음, `OriginGuard` — §3.6). 하지만 **로드될 때 허용된 외부 도메인(`connect_domains`·`resource_domains`)에는 계속 요청할 수 있다.** 이미 받은 입력과 결과도 화면 안에 있다.
  - 구현에서 검토할 것: 메인 origin의 `BroadcastChannel`로, 같은 브라우저의 다른 HIMARI 탭에 "이 출처의 화면을 닫으라"고 알린다. 같은 브라우저 안에서는 간격이 줄지만, 다른 브라우저·기기의 탭에는 닿지 않는다.
  - 즉시 철회를 엄격히 보장하려면, 화면의 모든 외부 요청을 HIMARI 서버가 중계하고 통제해야 한다(CSP는 HIMARI만 허용). 1단계 범위가 아니며, 필요해지면 따로 결정한다.

## 4. MCP 커넥터 (`src/himari/mcp/`)

### 4.1 `mcp.json` 확장

```json
{"servers": {"calliope": {
  "url": "...", "tools": {"position": {"effect": "read"}},
  "ui": {                                         선택. 있으면 이 서버의 화면을 보인다
    "connect_domains": ["https://api.example.com"],   선택. CSP connect-src 상한
    "resource_domains": ["https://cdn.example.com"]   선택. script/style/img/font/media 상한
  }
}}}
```

- 도메인 항목: `https://host[:port]` 또는 `https://*.host[:port]`. `connect_domains`는 WebSocket을 위해 `wss://`도 받는다(PR #27 검수 C2, 사양의 `connectDomains` 예시와 같음). loopback이면 `http://`·`ws://`도 허용한다. 경로·쿼리·공백·`;`·`'`는 거부한다(CSP 주입 방지). 항목은 종류별 32개까지.
- 모르는 키는 지금처럼 거부한다.

### 4.2 연결

- `ui`가 있는 서버에만 `initialize`에서 UI extension capability를 선언한다.
- `tools/list`에서 허용 목록의 Tool마다:
  - `visibility`에 `"model"`이 없으면 등록하지 않고 문제로 보인다: "app-only Tool: not usable until app-initiated calls are supported".
  - `_meta.ui.resourceUri`가 문자열이 아니거나, `ui://`로 시작하지 않거나, 2,048자를 넘으면 **화면만** 빼고 Tool은 등록한다(문제로 보임).
- 등록된 Tool의 서로 다른 `resourceUri`마다 `resources/read`를 한 번 한다. 기존 서버당 시작 시간(15초) 안에서 한다. 다음은 그 Tool의 화면만 빼고 문제로 보인다:
  - 응답이 없거나, 요청한 URI의 항목이 정확히 하나가 아닌 경우
  - `mimeType`이 다르거나, 본문이 UTF-8 HTML이 아닌 경우
  - HTML이 2 MiB를 넘거나, 한 서버의 화면 합계가 8 MiB를 넘는 경우
  - `_meta.ui`의 모양이 틀린 경우
- 받은 리소스는 `UiResource`(§5)로 만들고, digest = SHA-256(정규화한 JSON `{source, uri, html, csp, prefers_border}`)이다.
- `McpConnection`은 Application에 두 가지를 알려 준다(공개 계약이 아니라 Composition Root 안의 연결):
  - 서버별 현재 `ui` 설정(켜짐 여부·도메인 상한). Web이 화면을 열 때 이 값으로 CSP를 계산한다(§3.3).
  - `ui_for(tool_id) -> UiResource | None`: 그 Tool에 연결할 때 받아 둔 리소스. 실행 중 화면(§3.5)에 쓰고, `McpTool`이 결과에 붙이는 것과 같은 객체다.
  - `tool_info(tool_id) -> {name, description, inputSchema} | None`: 서버가 `tools/list`에서 준 그대로의 Tool 정보(이름은 서버의 원래 이름). `ui/initialize`의 `hostContext.toolInfo.tool`에 쓴다(§6.3).

### 4.3 실행

`McpTool.run`은 지금과 같고, 성공(`isError` 없음)이나 서버가 실행한 실패(`isError: true`)이면 다음을 덧붙인다:

```python
app_view=AppView(resource=<연결 때 받은 UiResource>, payload=<CallToolResult: content·structuredContent·_meta·isError>)
```

- payload의 JSON이 1 MiB를 넘으면 `app_view`를 넣지 않고, `raw_result`에 `"ui_problem": "the result is larger than 1 MiB"`를 남긴다. 화면이 없는 결과는 지금처럼 텍스트만 보인다.
- 실행 경로에 네트워크 요청은 추가되지 않는다(§3.1).

## 5. 계약 변경 (Proposed)

| 계약 | 변경 | 비고 |
| --- | --- | --- |
| `data.api` 새 값 타입 | `UiCsp(connect_domains, resource_domains, frame_domains, base_uri_domains)`, `UiResource(digest, source, uri, html, csp: UiCsp, prefers_border: bool \| None)`, `AppView(resource: UiResource, payload: JsonValue)` | `ArtifactMutation`처럼 Data 계약에 둔다(Tool·Data·Web이 같이 씀) |
| **TOOL-07** `tool.api` 재노출 | `UiCsp`·`UiResource`·`AppView`를 `tool.api`가 다시 내보낸다(`from himari.data.api import AppView as AppView` 형식: mypy strict가 인정하는 명시적 재노출. `tool.api`에는 `__all__`이 없다) | Data가 소유하고, MCP 커넥터는 V3-059 규칙 6대로 `tool.api`에서만 가져온다(PR #27 검수 B2). import 검사로 확인 |
| **TOOL-07** `ToolOutcome` | `app_view: AppView \| None = None` | Tool 구현은 화면을 돌려줄 뿐이고, 저장은 Dispatcher가 한다 |
| **DATA-08** `ToolCompletion` | `app_view: AppView \| None = None` | `SUCCEEDED`·`FAILED`에서만 허용하고, 그 밖의 상태는 계약 위반으로 거부 |
| **DATA-03** `ToolExecutionRecord` | `ui_digest: str \| None` | 화면이 있는 호출인지 표시 |
| **DATA-03** `ConversationLog` | `read_app_view(scope, call: CallRef) -> StoredAppView \| None` | 대화 범위 검사를 거친 읽기. `StoredAppView(tool_id, arguments, resource, payload)` |
| **TOOL-02** Dispatcher | `outcome.app_view`를 `ToolCompletion`으로 넘긴다 | 원자 커밋은 그대로 |
| **UI-01** Web | §6 | API 추가, sandbox 서버 |
| MCP 커넥터 | capability 선언, `resources/read`, `mcp.json`의 `ui`, `McpConnection.ui_for` | 공개 계약 아님(V3-059) |
| 바뀌지 않음 | Control, Prompt, Provider, Environment, `ToolDefinition`, `ToolCatalog` | 화면 정보는 2단계(앱 전용 Tool)에서 `ToolDefinition`에 들어간다 |

**이전 스케치와 다른 점:** 이 대화의 초기 스케치에는 `ToolUiSource` 포트와 별도 Data sub-port(`UiResourceStore`)가 있었다. 둘 다 두지 않는다. HTML은 handler가 결과와 함께 돌려주므로 따로 받는 포트가 필요 없다. 저장은 Tool 결과와 한 트랜잭션이어야 하므로, 다른 sub-port로 나누면 원자 커밋이 두 포트에 걸친다. 읽기는 실행 기록을 통해서만 하므로 ConversationLog에 둔다.

**Data 규칙(두 저장소 공통, `data/rules.py`):** HTML ≤ 2 MiB, payload JSON ≤ 1 MiB, `uri`는 `ui://`로 시작, digest는 내용과 일치해야 한다(저장소가 다시 계산해 확인). 같은 digest가 이미 있으면 내용이 같은지 확인하고 새로 쓰지 않는다.

### 5.1 SQLite v5

```sql
CREATE TABLE ui_resources (
  digest TEXT PRIMARY KEY, source TEXT NOT NULL, uri TEXT NOT NULL,
  html TEXT NOT NULL, meta TEXT NOT NULL            -- csp·prefers_border JSON
) STRICT;
ALTER TABLE tool_executions ADD COLUMN ui_digest TEXT REFERENCES ui_resources (digest);
ALTER TABLE tool_executions ADD COLUMN app_payload TEXT;
```

- 기존 방식대로, 정확히 v4 모양인 DB만 한 트랜잭션에서 올린다. 새 DB도 같은 DDL을 실행해서 스키마 서명이 같아진다(`ALTER`가 저장된 `CREATE` 문장을 바꾸므로).
- `ui_resources`는 대화에 속하지 않는다(digest로 공유). 지우는 규칙은 대화 삭제(Q-19)와 함께 정한다. 그때까지 지우지 않는다.

## 6. Web (`interfaces/web/`)

### 6.1 API

- `GET /api/conversations/{id}/events`에 화면 표시를 더한다(Data 이벤트 payload는 바꾸지 않음):
  - `assistant_tool_calls`의 각 call에 `app_view: true|false`: 그 Tool에 지금 보일 화면이 있는지(서버 화면 켜짐 + 이 대화에서 출처 켜짐(§3.7) + `ui_for` 있음). 실행 중 표시와 자동 표시에 쓴다.
  - `tool_result`에 `app_view: true|false`: 결과와 함께 화면이 저장됐고 지금 보일 수 있는지(서버 화면 켜짐 + 출처 켜짐). 그 페이지에 나온 Run들의 실행 기록을 Run마다 한 번씩 읽어 계산한다.
- `GET /api/conversations/{id}/calls/{run_id}/{call_id}/app` → 200, `state`에 따라 세 모양:

  ```json
  {"state": "done", "source": "calliope", "tool_id": "calliope/position",
   "tool": {"name": "position", "description": "…", "inputSchema": {...}},
   "digest": "…", "html": "<!DOCTYPE html>…", "prefers_border": true,
   "csp": {"connect_domains": [...], "resource_domains": [...]}, "declared_csp": {...},
   "sandbox_url": "http://127.0.0.1:8766/sandbox?csp=…&sig=…",
   "arguments": {...}, "result": {...}}
  ```

  - `done`: 결과와 함께 저장된 화면. HTML과 선언 CSP는 저장된 것이다.
  - `tool`: 지금 연결된 서버가 준 Tool 정보(`tool_info`). 그 Tool이 더 이상 등록돼 있지 않으면 `null`이다.
  - `running`: 호출이 아직 끝나지 않음(`PENDING`·`EXECUTION_CLAIMED`). 같은 모양이지만 HTML은 `ui_for`의 지금 리소스이고 `result`는 `null`이다.
  - `ended`: 끝났지만 화면이 저장되지 않음. HTML 없이 다음만 준다:

    ```json
    {"state": "ended", "status": "succeeded|failed|cancelled|denied|unknown", "outcome": "not_run|view_unavailable|unknown", "reason": "…"}
    ```

    - `status`: 실행 기록의 실제 상태 그대로다. 성공한 호출도 `succeeded`로 나온다(재검수 N1).
    - `outcome`: 화면을 어떻게 끝낼지 정한다. `cancelled`·`denied` → `not_run`(브리지가 `tool-cancelled`), `succeeded`·`failed` → `view_unavailable`(호스트가 닫고 안내), `unknown` → `unknown`(호스트가 닫고 안내). §3.5 표와 같다.
  - 저장된 화면도 지금 화면도 없으면 404 `no_app_view`. 그 서버의 화면을 운영자가 껐거나 서버가 설정에서 빠졌으면 404 `app_view_disabled`. 이 대화에서 그 출처가 꺼져 있으면 404 `source_disabled`(§3.7). 셋 다 상태와 상관없이 같은 순서로 확인한다: 출처 → 운영자 설정 → 화면 유무.
  - 인증과 대화 범위는 다른 조회와 같다. `cache-control: no-store`는 지금처럼 `/api/`에 붙는다.
  - HTML은 JSON 문자열로만 나간다. 메인 origin에서 `text/html`로 서빙하는 경로는 없다.

### 6.2 설정과 sandbox 서버

- `HIMARI_WEB_SANDBOX_PORT`(기본 `port + 1`). 메인 서버처럼 loopback에만 bind한다.
- sandbox origin = 허용된 메인 origin마다 같은 scheme·host에 sandbox 포트를 붙인 것. 역방향 프록시 뒤라면 프록시가 이 포트도 같은 호스트로 전달해야 한다(구현: 다른 모양을 적는 설정은 두지 않았다, §10).
- `/sandbox?p=…&sig=…` 응답 (서명·만료·`v`가 맞지 않으면 400, §3.4):
  - 헤더: CSP(§3.4), `cache-control: no-store`, `referrer-policy: no-referrer`, `x-content-type-options: nosniff`
  - 본문: 고정 HTML. 메인 origin 목록을 `<meta name="himari-host-origins">`로 넣는다. 메인 페이지가 `no-referrer`라 proxy는 referrer로 부모를 확인할 수 없기 때문이다.
- `/sandbox.js`(proxy):
  - 부모 메시지는 `event.source === parent`이고 `event.origin`이 메인 origin 목록에 있을 때만 받는다.
  - View 메시지는 `event.source === inner.contentWindow`일 때만 받는다(opaque라 `event.origin`은 `"null"`).
  - `sandbox-resource-ready`의 `sandbox`·`permissions` 값은 무시하고, 항상 `sandbox="allow-scripts"`, `allow` 없음, `srcdoc`로 싣는다.
  - 부모에게는 `targetOrigin`을 메인 origin으로 지정해 보낸다.
- TrustedHost는 메인과 같은 허용 host 목록을 쓴다. 다른 모든 경로는 404다.

### 6.3 호스트 브리지 (`static/app_view.js`, 새 파일)

`app.js`의 규칙(textContent만, `innerHTML`·`eval` 금지, await 뒤 generation 확인)을 따른다.

| View → 호스트 | 처리 |
| --- | --- |
| `ui/initialize` (요청, `tool`이 `null`이면 `toolInfo`를 뺀다. PR #27 검수 C1: 사양의 `Tool`은 `inputSchema`가 필수) | `{protocolVersion: "2026-01-26", hostInfo: {name: "himari", version: "3"}, hostCapabilities: {openLinks: {}, sandbox: {csp: <실제 CSP>}}, hostContext: {toolInfo: {tool: <API의 tool>}, theme, displayMode: "inline", availableDisplayModes: ["inline"], containerDimensions: {width: <행 너비>, maxHeight: 800}, locale, timeZone, platform: "web"}}` |
| `ui/notifications/initialized` | `tool-input {arguments}` 전송. 이미 `done`이면 이어서 `tool-result <payload>`, `ended`이면 `outcome`대로(§3.5) |
| `ui/notifications/size-changed` | iframe 높이를 40~800px로 맞춤 |
| `ui/open-link` (요청) | `http(s)` URL만, 주소를 보여 주는 확인 창 → `window.open(url, "_blank", "noopener,noreferrer")`, 거절하면 `-32000` |
| `ping` | `{}` |
| `notifications/message` | 무시 |
| 그 밖의 요청 (`tools/call`, `resources/read`, `ui/message`, `ui/update-model-context`, `ui/request-display-mode` …) | `-32601` "not supported by this host" |
| 그 밖의 알림 | 무시 |

- 받는 메시지는 `event.source`가 그 화면의 iframe이고 `event.origin`이 sandbox origin일 때만 처리한다. `jsonrpc: "2.0"`인 평범한 객체만 받고, JSON 직렬화 길이가 1 MiB를 넘으면 버린다.
- `hostCapabilities`에 `serverTools`·`serverResources`가 없으므로, 앱은 호출 기능이 없음을 안다.
- **실행 중인 화면의 마무리:** 띄워 둔 `running` 화면의 `tool_result` 이벤트가 오면 `…/app`을 다시 읽어 §3.5 표대로 처리한다: `done` → `tool-result`(digest가 다르면 다시 띄우기), `ended` → `outcome`대로(`not_run`만 `tool-cancelled`, 나머지는 호스트가 닫고 안내), 404 `source_disabled`·`app_view_disabled` → 닫고 안내. View의 `initialized` 전에 끝났으면, `initialized` 뒤에 `tool-input`과 함께 처리한다.
- **출처를 끌 때:** §3.7의 세 경우에 그 출처의 화면을 모두 닫는다.
- **자동 표시:** 이벤트를 그린 뒤 `app_view`가 참인 가장 최근 call을 고른다(가장 최근 Run의 가장 큰 ordinal). 아직 띄우지 않았으면 띄운다. 사용자가 닫은 화면은 다시 자동으로 띄우지 않는다(그 대화를 다시 열 때까지).

### 6.4 CLI

바뀌지 않는다. `show`는 지금처럼 결과 텍스트를 보인다(서버가 텍스트 대체를 주는 것이 사양의 요구다).

## 7. 위협과 대응

| 위협 (사양 Threat Model) | 대응 |
| --- | --- |
| View가 HIMARI 데이터를 읽음 | 다른 origin, sandbox 서버에 API 없음, CORS 없음, `HttpOnly` 쿠키 |
| View가 상태를 바꿈 | `OriginGuard`(sandbox origin은 허용 안 됨), View에 `allow-forms` 없음 |
| 인자·결과를 밖으로 보냄 | `connect-src` = 선언 ∩ 운영자 상한(기본 없음), `allow-popups`·`allow-top-navigation` 없음, 링크는 사용자 확인 |
| 다른 서버의 화면을 엿봄 | View는 opaque origin(저장소 없음), 화면마다 별도 iframe |
| HIMARI sandbox origin을 느슨한 CSP로 악용 | 프로세스마다 새로 만드는 키로 HMAC 서명, 메인 origin 하나에 묶음, 10분 만료 |
| 대화에서 끈 출처의 화면 코드가 실행됨 | 화면 조건에 `ExternalAccess`를 넣음, 끄면 같은 탭의 떠 있는 화면도 닫음. 다른 탭의 화면은 닫힐 때까지 허용됐던 외부 도메인에 요청할 수 있음(§3.7 보장 수준) |
| 운영자가 거둔 뒤 예전 서명 URL을 다시 씀 | 정책은 시작할 때만 바뀌고, 시작할 때 키도 바뀐다. 재시작하면 예전 URL은 모두 400 |
| 위조된 메시지 | proxy는 메인 origin만, 호스트는 자기 iframe·sandbox origin만, 크기 한도 |
| 자원 소모 | 가장 최근 하나만 자동, 나머지는 열 때만, 동시 3개, HTML 2 MiB |
| 사회공학 | 항상 보이는 "App view from <서버> · sandboxed" 테두리 |
| 운영자가 신뢰를 거둠 | CSP와 켜짐 여부는 볼 때의 설정으로 계산, 다시 시작하면 **새로 띄우는** 예전 화면에도 적용(§3.3). 이미 열린 화면은 다시 로드될 때까지 예전 CSP(§3.7) |

## 8. 검증 계획

- **MCP:** 가짜 서버에 `_meta.ui`·`resources/read`를 추가한다. 확인할 것:
  - capability는 `ui`를 켠 서버에만 선언한다.
  - 앱 전용 Tool은 거부한다.
  - 잘못된 URI·mime·크기·모양이면 화면만 빠진다.
  - payload가 1 MiB를 넘으면 `ui_problem`을 남긴다.
  - 실행 중에는 `resources/read`를 하지 않는다(요청 기록으로 확인).
  - `ui_for`가 돌려주는 리소스와 결과에 붙는 리소스가 같다.
  - 공식 MCP Python SDK의 MCP Apps 예제 서버와 상호운용을 확인한다(V3-P53과 같은 방식).
- **Data:** v4 → v5 업그레이드(데이터 유지, 정확한 모양만), 결과와 화면의 원자 커밋, 다른 대화에서는 `read_app_view`를 읽을 수 없음, 상태별 거부, digest 불일치 거부. 같은 계약 테스트를 두 저장소에 돌린다.
- **Web:**
  - API의 세 상태(`running` → `done`, `running` → `ended`: Pause 후 Dismiss, Cancel, 결과 불명), 404 세 종류, 두 이벤트의 `app_view` 플래그
  - **N1:** 실행 중 화면이 떠 있는 호출이 성공했지만 결과가 1 MiB를 넘을 때 → `ended`의 `status: succeeded`·`outcome: view_unavailable`. 브라우저 smoke에서는 `tool-cancelled`가 가지 않고 화면이 안내로 바뀌는지 본다. `UNKNOWN`·서버가 실행하지 않은 `FAILED`도 `tool-cancelled` 없음. `CANCELLED`·`DENIED`만 `tool-cancelled`
  - **N2:** 출처가 꺼진 대화에서 모델이 그 출처의 Tool을 부를 때 → `assistant_tool_calls`의 `app_view: false`, `…/app`은 `running`·`done` 모두 404 `source_disabled`, 호출은 `DENIED`. 출처를 켠 상태에서 화면을 띄우고 끄면 → 다음 조회가 `source_disabled`, 브라우저 smoke에서 화면이 닫히는지 본다
  - 메인 CSP의 `frame-src`
  - sandbox 응답 헤더(서명된 CSP, 서명이 틀리면 400, `frame-ancestors`), sandbox 서버에 API 경로가 없음
  - sandbox origin의 POST가 쿠키를 가지고도 403
  - 운영자가 도메인을 빼면 예전 화면의 CSP도 줄어듦
- **sandbox 서명:** 예전 프로세스의 서명 URL은 400, 만료된 URL은 400, 다른 메인 origin으로 바꾼 `p`는 400, `wss://` 항목은 `connect-src`에 들어감.
- **import 검사:** MCP 커넥터가 `UiResource`·`AppView`를 `tool.api`에서 가져오고, `data.api`를 직접 import하면 실패(규칙 6).
- **브라우저 smoke:** `scripts/mcp_apps_probe/run.py`(설계 모형)는 이미 있다. 구현 PR에서는 같은 확인을 실제 HIMARI 서버로 다시 돌린다. 여기에 더해 자동 표시(가장 최근 하나, 닫은 화면은 다시 띄우지 않음)와 실행 중 → 결과·취소 전환을 본다. 이 호스트에서는 Playwright의 Chromium에 빠진 시스템 라이브러리 4개(`libatk`, `libatspi`, `libasound`, `libXdamage`)를 Ubuntu 패키지에서 풀어 쓰면 돌아간다. 구현 PR에서 이 절차를 smoke 스크립트에 적는다. Firefox·Safari는 §3.6의 남은 확인.
- 공개 계약이 바뀌므로 전체 pytest를 돌린다(V3-038).

## 9. 결정된 것과 남은 질문

**결정됨(2026-10-15 사용자):** 화면에 저장소 없음(§3.4), 가장 최근 화면 자동 표시(§3.5), 실행 중 표시(§3.5).

**남은 질문:**

1. `ui_resources`의 보존과 삭제(Q-19 대화 삭제와 함께).
2. 2단계 진입 조건: 앱이 시작한 호출의 직렬화 범위(Run 중에 받을지)와 모델이 그 호출을 알지(이 대화의 이전 정리, 결정 2·3).

## 10. 구현 노트 (2026-10-15)

설계대로 구현했고, 다음만 다르거나 구체화했다.

- **sandbox origin 설정:** `HIMARI_WEB_SANDBOX_ORIGINS`(다른 모양을 직접 적는 설정)는 두지 않았다. sandbox origin은 언제나 "페이지 origin의 scheme·host + sandbox 포트"다. 필요해지면 추가한다.
- **CSP:** 사양 틀에 `form-action 'none'`을 더했다(더 좁힘, 사양이 허용). View에는 `allow-forms`도 없다.
- **API:** `GET …/calls/{run}/{call}/app?origin=<페이지 origin>`. 같은 origin의 GET에는 브라우저가 `Origin` 헤더를 보내지 않으므로, 페이지가 자기 origin을 알리고 서버는 허용 목록과 대조한다. 그 origin이 서명에 묶이고 `frame-ancestors`가 된다.
- **같은 ExternalAccess:** 화면의 출처 확인(§3.7)은 catalog·Dispatcher가 쓰는 것과 같은 `ExternalAccess` 인스턴스(`Runtime.access`)에 묻는다.
- **Dispatcher의 안전장치:** handler가 실행되지 않은 호출에 화면을 붙이면 버린다. Data가 화면을 거부하면(크기·digest 등) 화면 없이 결과만 다시 커밋한다. claim된 호출의 확실한 결과가 화면 때문에 UNKNOWN이 되지 않는다.
- **화면 읽기 시간:** 서버의 Tool 목록과 별도로, 서버당 15초 안에 화면을 읽는다. 넘으면 남은 화면만 빠진다.
- **sandbox 서버:** 화면을 켠 서버가 있을 때만 `__main__`이 같은 프로세스에서 띄운다. 신호는 메인 서버가 받고, 메인이 끝나면 함께 끝난다.

**검증 (Chrome for Testing 156, 이 호스트):**

- `scripts/mcp_apps_smoke.py`: 실제 페이지 서버와 sandbox 서버, 가짜 MCP 서버, 기록하는 테스트용 화면으로 15개를 확인했고 모두 통과했다.
  - 자동 표시와 브리지 왕복(`ui/initialize`·`tool-input`·`tool-result`·`size-changed`)
  - 화면의 격리: opaque origin, 저장소·부모·top·페이지 API 모두 막힘
  - `tools/call` 거절(`-32601`)
  - 실행 중 표시 → 결과
  - 쓰기 실행 중 Stop → "outcome unknown"으로 닫힘(`tool-cancelled` 없음)
  - 읽기 실행 중 Stop → `tool-cancelled`
  - 동시 3개
  - 출처 끄기 → 닫힘
- 기존 `scripts/ui_smoke.py`도 통과했다(화면 추가 후 회귀 없음).
- 이 호스트에서 브라우저를 돌리는 방법: Ubuntu 패키지 4개를 `apt-get download`로 받아 풀고 `LD_LIBRARY_PATH`로 준다(root 불필요).
- Firefox·Safari는 여전히 확인하지 못했다.

