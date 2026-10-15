# MCP Apps 1단계 — 읽기 전용 화면 표시 v0.1

> 2026-10-15 · **PROPOSED (V3-P58)** · 설계 초안이며 구현 전이다.
>
> 기준 사양: MCP Apps 확장 `io.modelcontextprotocol/ui`, **안정판 2026-01-26** ([apps.mdx](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx), SEP-1865 Final). `draft`는 따르지 않는다.
>
> 이미 정한 것: 커넥터 패키지 규칙(V3-059), 화면 HTML은 Tool을 실행할 때 스냅샷으로 Data에 저장, sandbox origin은 같은 Web 서버의 별도 포트(2026-10-15 사용자 결정).

## 1. 범위

**1단계는 MCP 서버가 준 화면(View)을 Tool 결과 옆에 보여 주기만 한다.** 실행 경로(Control·Dispatcher·claim·Pause/Cancel)는 바뀌지 않는다.

| 포함 | 제외 (단계) |
| --- | --- |
| Tool의 `_meta.ui.resourceUri` 해석, `resources/read`로 HTML 받기 | 화면이 시작하는 `tools/call`·`resources/read` (2단계) |
| HTML·CSP를 실행 기록과 함께 저장(SQLite v5) | 앱 전용 Tool(`visibility: ["app"]`) (2단계) |
| Web: sandbox origin의 proxy, 화면 iframe, 호스트 브리지 | `ui/message`, `ui/update-model-context` (3단계) |
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
- **데이터 전달:** `initialized` 뒤에 `ui/notifications/tool-input {arguments}`를 한 번, 이어서 `ui/notifications/tool-result`(표준 `CallToolResult`)를 보낸다.
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
- 저장하는 것은 **서버가 선언한 값**이다. 교집합은 화면을 열 때 그때의 운영자 설정으로 계산한다. 그래서 운영자가 도메인을 빼거나 서버의 화면을 끄고 다시 시작하면(`mcp.json`은 시작할 때 읽는다), 예전 대화의 화면에도 적용된다. 감사를 위해 선언 값은 그대로 남는다.

### 3.4 Sandbox: 별도 포트, 서명된 CSP, opaque View

```
메인 origin  http://127.0.0.1:8765          (Web 화면·API, 세션 쿠키)
  └ iframe ── sandbox origin http://127.0.0.1:8766/sandbox?csp=…&sig=…
                (HIMARI가 쓴 proxy. 응답 헤더 CSP = 이 화면의 CSP)
                └ iframe srcdoc, sandbox="allow-scripts"   (View: opaque origin)
```

- **별도 포트의 작은 서버:** `/sandbox`(proxy 페이지)와 `/sandbox.js`만 서빙하는 ASGI 앱을 같은 프로세스에서 두 번째 포트로 띄운다. API 경로는 하나도 없고 세션을 모른다. 화면을 켠 서버가 하나도 없으면 띄우지 않는다.
- **CSP는 proxy 페이지의 응답 헤더로:** `srcdoc` 문서는 부모의 CSP를 물려받는다. 참조 구현처럼 proxy 응답 헤더에 그 화면의 CSP를 넣는다. 사양의 CSP 틀에 §3.3의 도메인을 넣고, `frame-ancestors <메인 origin들>`을 더한다. `meta` 태그와 달리 View가 바꿀 수 없다.
- **CSP 파라미터는 서명한다:** 메인 API가 `csp`(정규화한 JSON, base64url)와 `sig`(운영자 비밀에서 파생한 키로 계산한 HMAC-SHA256)를 준다. sandbox 서버는 서명이 맞지 않으면 400을 준다. 누군가 HIMARI의 sandbox origin을 느슨한 CSP로 띄워 쓰는 것을 막는다.
- **View는 opaque origin:** 사양의 proxy와 달리 안쪽 iframe에 `allow-same-origin`을 주지 않는다(`sandbox="allow-scripts"`, 사양이 허용하는 더 좁은 설정). 모든 서버의 화면이 sandbox origin 하나를 같이 쓰므로, 같은 origin을 주면 서로의 localStorage·IndexedDB와 proxy 페이지에 닿는다. 대가: 저장소를 쓰는 앱은 동작하지 않는다(사양은 앱에 기능 감지를 요구한다). 서버별 origin을 둘 수 있을 때 다시 본다.
- **쿠키는 포트로 나뉘지 않는다:** sandbox origin은 메인과 같은 사이트라 `SameSite=Strict` 쿠키도 같이 간다. 그래서 막는 것은 쿠키 속성이 아니라 서버 쪽 규칙이다:
  - 세션 쿠키는 `HttpOnly`라 스크립트로 읽을 수 없다(지금도 그렇다).
  - CORS를 열지 않으므로 다른 origin은 API 응답을 읽을 수 없다.
  - 상태를 바꾸는 요청은 `OriginGuard`가 막는다(sandbox origin은 허용 목록에 없다).
  - View의 `connect-src`에는 메인 origin이 없다(운영자가 넣지 않는 한).

  이 네 가지를 각각 테스트로 고정한다(§8).
- **메인 페이지 CSP:** 지금의 고정 CSP에 `frame-src <sandbox origin들>`을 더한다. 메인의 `frame-ancestors 'none'`과 `X-Frame-Options: DENY`는 그대로다.

### 3.5 화면은 사용자가 열 때만 띄운다

긴 대화에서 모든 화면을 자동으로 띄우면 브라우저 자원을 많이 쓰고(사양의 "Resource consumption"), 사용자가 보지 않는 코드가 돈다. 화면이 있는 Tool 행에 "Open view" 버튼을 두고, 연 화면만 띄운다. 동시에 살아 있는 화면은 3개까지이고, 넘으면 가장 오래된 것을 닫는다. 대화를 바꾸거나 화면을 닫을 때는 `ui/resource-teardown`을 보내고 최대 1초 기다린다.

화면 테두리 위에는 항상 "App view from `<서버 이름>` · sandboxed"를 보인다. `prefersBorder`와 상관없이 보인다(사양의 사회공학 위험 대응).

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

- 도메인 항목: `https://host[:port]` 또는 `https://*.host[:port]`. loopback이면 `http://`도 허용한다. 경로·쿼리·공백·`;`·`'`는 거부한다(CSP 주입 방지). 항목은 종류별 32개까지.
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
- `McpConnection`은 서버별 현재 `ui` 설정(켜짐 여부·도메인 상한)을 Application에 알려 준다. Web이 화면을 열 때 이 값으로 CSP를 계산한다(§3.3).

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
| **TOOL-07** `ToolOutcome` | `app_view: AppView \| None = None` | Tool 구현은 화면을 돌려줄 뿐이고, 저장은 Dispatcher가 한다 |
| **DATA-08** `ToolCompletion` | `app_view: AppView \| None = None` | `SUCCEEDED`·`FAILED`에서만 허용하고, 그 밖의 상태는 계약 위반으로 거부 |
| **DATA-03** `ToolExecutionRecord` | `ui_digest: str \| None` | 화면이 있는 호출인지 표시 |
| **DATA-03** `ConversationLog` | `read_app_view(scope, call: CallRef) -> StoredAppView \| None` | 대화 범위 검사를 거친 읽기. `StoredAppView(tool_id, arguments, resource, payload)` |
| **TOOL-02** Dispatcher | `outcome.app_view`를 `ToolCompletion`으로 넘긴다 | 원자 커밋은 그대로 |
| **UI-01** Web | §6 | API 추가, sandbox 서버 |
| MCP 커넥터 | capability 선언, `resources/read`, `mcp.json`의 `ui` | 공개 계약 아님(V3-059) |
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

- `GET /api/conversations/{id}/events`: `tool_result` 항목에 `app_view: true|false`를 더한다. 그 페이지에 나온 Run들의 실행 기록을 Run마다 한 번씩 읽어 계산한다(Data 이벤트 payload는 바꾸지 않음).
- `GET /api/conversations/{id}/calls/{run_id}/{call_id}/app` → 200:

  ```json
  {"source": "calliope", "tool_id": "calliope/position", "tool_name": "position",
   "html": "<!DOCTYPE html>…", "prefers_border": true,
   "csp": {"connect_domains": [...], "resource_domains": [...]},
   "declared_csp": {...},
   "sandbox_url": "http://127.0.0.1:8766/sandbox?csp=…&sig=…",
   "arguments": {...}, "result": {...}}
  ```

  - 화면이 없으면 404 `no_app_view`. 그 서버의 화면을 운영자가 껐거나 서버가 설정에서 빠졌으면 404 `app_view_disabled`.
  - 인증과 대화 범위는 다른 조회와 같다. `cache-control: no-store`는 지금처럼 `/api/`에 붙는다.
  - HTML은 JSON 문자열로만 나간다. 메인 origin에서 `text/html`로 서빙하는 경로는 없다.

### 6.2 설정과 sandbox 서버

- `HIMARI_WEB_SANDBOX_PORT`(기본 `port + 1`). 메인 서버처럼 loopback에만 bind한다.
- sandbox origin = 허용된 메인 origin마다 같은 scheme·host에 sandbox 포트를 붙인 것. 역방향 프록시 뒤라면 `HIMARI_WEB_SANDBOX_ORIGINS`로 직접 적는다(메인 origin과 같은 규칙으로 검사).
- `/sandbox` 응답:
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
| `ui/initialize` (요청) | `{protocolVersion: "2026-01-26", hostInfo: {name: "himari", version: "3"}, hostCapabilities: {openLinks: {}, sandbox: {csp: <실제 CSP>}}, hostContext: {toolInfo: {tool: {name, description}}, theme, displayMode: "inline", availableDisplayModes: ["inline"], containerDimensions: {width: <행 너비>, maxHeight: 800}, locale, timeZone, platform: "web"}}` |
| `ui/notifications/initialized` | `tool-input {arguments}`, 이어서 `tool-result <payload>` 전송 |
| `ui/notifications/size-changed` | iframe 높이를 40~800px로 맞춤 |
| `ui/open-link` (요청) | `http(s)` URL만, 주소를 보여 주는 확인 창 → `window.open(url, "_blank", "noopener,noreferrer")`, 거절하면 `-32000` |
| `ping` | `{}` |
| `notifications/message` | 무시 |
| 그 밖의 요청 (`tools/call`, `resources/read`, `ui/message`, `ui/update-model-context`, `ui/request-display-mode` …) | `-32601` "not supported by this host" |
| 그 밖의 알림 | 무시 |

- 받는 메시지는 `event.source`가 그 화면의 iframe이고 `event.origin`이 sandbox origin일 때만 처리한다. `jsonrpc: "2.0"`인 평범한 객체만 받고, JSON 직렬화 길이가 1 MiB를 넘으면 버린다.
- `hostCapabilities`에 `serverTools`·`serverResources`가 없으므로, 앱은 호출 기능이 없음을 안다.

### 6.4 CLI

바뀌지 않는다. `show`는 지금처럼 결과 텍스트를 보인다(서버가 텍스트 대체를 주는 것이 사양의 요구다).

## 7. 위협과 대응

| 위협 (사양 Threat Model) | 대응 |
| --- | --- |
| View가 HIMARI 데이터를 읽음 | 다른 origin, sandbox 서버에 API 없음, CORS 없음, `HttpOnly` 쿠키 |
| View가 상태를 바꿈 | `OriginGuard`(sandbox origin은 허용 안 됨), View에 `allow-forms` 없음 |
| 인자·결과를 밖으로 보냄 | `connect-src` = 선언 ∩ 운영자 상한(기본 없음), `allow-popups`·`allow-top-navigation` 없음, 링크는 사용자 확인 |
| 다른 서버의 화면을 엿봄 | View는 opaque origin(저장소 없음), 화면마다 별도 iframe |
| HIMARI sandbox origin을 느슨한 CSP로 악용 | CSP 파라미터 HMAC 서명 |
| 위조된 메시지 | proxy는 메인 origin만, 호스트는 자기 iframe·sandbox origin만, 크기 한도 |
| 자원 소모 | 열 때만 띄움, 동시 3개, HTML 2 MiB |
| 사회공학 | 항상 보이는 "App view from <서버> · sandboxed" 테두리 |
| 운영자가 신뢰를 거둠 | CSP와 켜짐 여부는 볼 때의 설정으로 계산, 다시 시작하면 예전 화면에도 적용(§3.3) |

## 8. 검증 계획

- **MCP:** 가짜 서버에 `_meta.ui`·`resources/read`를 추가한다. 확인할 것:
  - capability는 `ui`를 켠 서버에만 선언한다.
  - 앱 전용 Tool은 거부한다.
  - 잘못된 URI·mime·크기·모양이면 화면만 빠진다.
  - payload가 1 MiB를 넘으면 `ui_problem`을 남긴다.
  - 실행 중에는 `resources/read`를 하지 않는다(요청 기록으로 확인).
  - 공식 MCP Python SDK의 MCP Apps 예제 서버와 상호운용을 확인한다(V3-P53과 같은 방식).
- **Data:** v4 → v5 업그레이드(데이터 유지, 정확한 모양만), 결과와 화면의 원자 커밋, 다른 대화에서는 `read_app_view`를 읽을 수 없음, 상태별 거부, digest 불일치 거부. 같은 계약 테스트를 두 저장소에 돌린다.
- **Web:**
  - API 모양, 404 두 종류, `app_view` 플래그
  - 메인 CSP의 `frame-src`
  - sandbox 응답 헤더(서명된 CSP, 서명이 틀리면 400, `frame-ancestors`), sandbox 서버에 API 경로가 없음
  - sandbox origin의 POST가 쿠키를 가지고도 403
  - 운영자가 도메인을 빼면 예전 화면의 CSP도 줄어듦
- **브라우저 smoke:** proxy 메시지 흐름, `srcdoc`가 CSP를 물려받는지, proxy 자신의 CSP(`frame-src 'none'`, `frame-ancestors`)가 안쪽 `srcdoc` iframe을 막지 않는지(참조 구현은 `document.write`를 쓰므로 이 경우를 직접 보여 주지 않는다), View가 `parent.parent`와 메인 쿠키에 닿지 못하는지. 이 호스트에는 Chromium 시스템 라이브러리가 없어 지금은 돌릴 수 없다(PR #24와 같음). 구현 PR에서 돌릴 수 있는 곳을 정한다.
- 공개 계약이 바뀌므로 전체 pytest를 돌린다(V3-038).

## 9. 남은 질문

1. opaque View(§3.4)로 저장소를 쓰는 앱이 동작하지 않는 것을 받아들일지. 대안은 서버별 sandbox origin(서버마다 포트 또는 하위 도메인)이다.
2. 화면을 사용자가 열 때만 띄울지(§3.5), 마지막 Tool의 화면은 자동으로 띄울지.
3. `ui_resources`의 보존과 삭제(Q-19 대화 삭제와 함께).
4. 2단계 진입 조건: 앱이 시작한 호출의 직렬화 범위(Run 중에 받을지)와 모델이 그 호출을 알지(이 대화의 이전 정리, 결정 2·3).
