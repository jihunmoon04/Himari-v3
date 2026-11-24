# HIMARI v3

HIMARI v3는 **Windows 반려 에이전트**의 서버 쪽 두뇌입니다. Windows 반려 앱(`clients/windows/Himari.Companion`)이 몸이고, 서버의 HIMARI가 대화·기억·먼저 말 걸기를 맡습니다. Web(`web/`)은 대화와 설정을 관리하는 도구입니다. 2026-11-23 결정(V3-P106)으로 hub 작업 콘솔, 작업 앱, 명령 실행, 붙인 폴더, 질문 Tool, 일시정지, 턴 예산 같은 작업 에이전트 기능을 걷어 냈습니다. 범용 코딩 일은 Claude Code·Codex 같은 도구가 맡습니다.

## 구현 현황 (2026-11-24)

지금 상태(배포, 확인하지 않은 것, 부채, 다음 후보)는 [docs/status.md](docs/status.md)에 한 쪽으로 모았습니다.

| 영역 | 있는 것 |
| --- | --- |
| 반려 | 기기 짝짓기와 기기 토큰, 지금 기기·활동 신호, 표정 표시, 화면 보여 주기와 화면 도구, 먼저 말 걸기(wake), 사건 흐름 `GET /api/events` |
| Control | Run 반복(Run = LLM 호출 1회), 한 턴의 Run 상한 `HIMARI_MAX_RUNS`(기본 12), 취소, 문맥 압축, 대화별 모델 |
| Data | SQLite 영속 저장(WAL, 업그레이드 전 백업), 원본 요청·응답 기록, 휴지통과 영구 삭제, 기억·일화 |
| Prompt·Provider | Cache-first 구성, 지금 시각·기기, OpenRouter(스트리밍, reasoning) |
| Tool | 기억, 대화 검색, 화면, 범용 MCP(HTTP, 프롬프트·리소스), 대화 폴더의 파일 Tool·`present_file`·`read_document` |
| 파일 | 올리기·내려받기, 이미지 입력, 문서 읽기(server sandbox), 대화 폴더(데이터 폴더 안, 그 대화 폴더에서만 읽고 씀, V3-P109) |
| 인터페이스 | Web(React+TypeScript), Windows 반려 앱, 작은 CLI |

뺀 것: hub 중계·작업 콘솔(V3-P102 hub 부분, V3-P103), 작업 앱(V3-P104), `run_command`와 명령용 sandbox, 붙인·등록한 Workspace, 서버 파일 탐색기와 파일 참조, `ask_user`, 일시정지, 토큰 예산·진척 없음·자리 비움. 자세한 목록은 [옛 결정 기록](docs/archive/decisions-v3.md)의 V3-P106에, 빠진 기능의 설계는 [보관 문서](docs/archive/README.md)에 있습니다.

> 설계 원칙: **Contract-based Modular Architecture** — 모듈은 서로의 내부 구현에 의존하지 않고, 명시된 계약을 유지하는 범위에서 독립적으로 수정·교체할 수 있어야 합니다.

## 결정된 핵심 방향

전체 구조는 [아키텍처](docs/architecture.md)에, 지금 유효한 결정은 [결정 기록](docs/decisions.md)에 있습니다. 아래 괄호의 `V3-…` 번호는 [옛 결정 기록](docs/archive/decisions-v3.md)의 번호입니다.

- **Run = LLM 호출 1회**. Control이 Run 반복을 조정하며, 모델의 final response에서 반복을 중단하고, 한 턴은 `HIMARI_MAX_RUNS`(기본 12) Run에서 멈춥니다. 사용자에게 묻고 싶은 것은 답의 글로 묻고, 사용자의 다음 메시지가 새 턴이 됩니다(V3-P106에서 질문 Tool `ask_user` 제거; 기록에 남은 대기 중 질문은 다음 메시지에서 취소로 닫힘). v2의 Task/ManagerTurn 계층을 v3의 실행 기본 단위로 가져오지 않습니다.
- **Data**는 Conversation의 원본 입력·출력을 exact하게 보존합니다. Prompt는 Data를 가공해 다음 모델 입력을 만들고, **최신 Prompt Snapshot 1개**(압축 문맥 + 최근 exact 입력 데이터)만 유지합니다. **모든 Run의 원본 Provider 요청·응답**도 기록합니다.
- **Tool 실패**는 ToolResult로 전달하며, 그 이후의 대응은 다음 Run에서 모델이 판단합니다. Runtime의 숨은 의미론적 자동 복구는 기본 동작이 아닙니다.
- **프롬프트는 Cache-first 순서**: Stable Prefix(시스템 글·페르소나·Tool 정의) → 압축 이력 + exact Conversation → Current State(지금 시각·기기·활동, 기억, 최근 일화). ToolCall/Result는 provider-native 결합을 유지합니다.
- **지시가 되는 것은 시스템 글과 사용자의 메시지뿐**입니다. Tool 결과, 파일, 기억, 창 제목, wake 사유는 정보일 뿐 지시가 아닙니다(V3-P89).
- **Provider reasoning** 설정과 사용량/공개된 continuation 정보는 Data에서 관리하고 Prompt·Provider가 지원 가능한 모델 요청을 구성합니다. 비공개 내부 추론 원문은 이용 가능하다고 전제하지 않습니다.
- **표정은 Tool이 아니라 답 안의 `[[mood:이름]]` 표시**입니다. 서버가 떼어 앱의 `live`로 보냅니다(V3-P83). 앱 자신의 상태(듣는 중·졸림 등)는 앱이 정합니다.
- **Memory는 Tool과 worker**로 처리합니다. 모델이 `memory_save`·`memory_forget`·`memory_search`로 쓰고(V3-P71), 대화가 잠잠해진 뒤 `memory` worker가 자동 기억·일화·열린 고리를 만듭니다(V3-P85). 지난 대화의 원문 회상은 `conversation_search`가 맡습니다(V3-P72).
- **Tool 출처는 셋**: Internal(기억·대화 검색·화면), Environment(`machine`의 대화 폴더 파일 Tool), External(MCP). **ToolCall은 충돌이 없으면 병렬 실행**할 수 있습니다.
- **파일 Tool은 대화 폴더에서만 읽고 씁니다**(`<HIMARI_DATA_ROOT>/conversations/<id>`, 서버 프로세스 안에서; V3-P109). 문서·이미지 처리는 server sandbox(네트워크 차단)에서 돕니다. 명령 실행(`run_command`), 붙인 Workspace와 Binding은 V3-P106에서, `himari-runner`와 stdio MCP는 V3-P109에서 뺐습니다. 향후 Web Search는 별도 Tool로 설계합니다.

### 모듈 경계 (V3-039~046)

- **확장 SPI 우선:** 새 Tool은 `ToolHandler`, 새 Provider는 `ProviderAdapter`, 새 Environment는 `EnvironmentPlugin` 하나를 구현해 등록합니다. Environment는 자기 Tool을 직접 제공합니다.
- **Data Port는 Consumer별로 분리:** `ConversationLog` · `StateQuery` · `MemoryStore` · `PersonaStore` · `DeviceStore` 등. 백엔드는 공유해도 계약 변경 단위는 sub-port입니다.
- **Tool 구현은 실행 원장을 모릅니다:** `ToolOutcome`을 반환하고 원자 커밋은 Tool Dispatcher가 합니다.
- **Prompt 핵심은 순수 함수** `compose(PromptInputs)`입니다.
- **단일 프로세스 규모:** Conversation별 in-process lock으로 동시 실행을 막고, 크래시 후 중복은 송신·부작용 직전 영속 표시와 UNKNOWN 처리(자동 재실행 없음)로 막습니다. 분산 lease·fencing·다중 worker 복구는 P2로 연기합니다.
- **계약은 코드로:** 각 모듈 `api.py`의 Protocol + frozen dataclass가 원본이고, import-linter로 의존 방향을 강제합니다.

## 문서

먼저 읽을 것:

- **[지금 상태](docs/status.md)** — 한 쪽 요약: 구성, 동작하는 기능, 운영(배포·스키마), 확인하지 않은 것, 알려진 부채, 다음 후보
- **[아키텍처](docs/architecture.md)** — 지금 구조: 앱·서버·Web, Run 반복, 서버 모듈, 반려 기능, Tool, 기억, 뺀 것
- **[결정 기록](docs/decisions.md)** — 지금 유효한 결정(H 번호, 2026-11-24에 새로 매김)과 열린 문제. 옛 번호(`V3-…`)는 [옛 결정 기록](docs/archive/decisions-v3.md)에

기준 문서:

- [통합 모듈 계약 기준서](docs/contracts.md) — 모듈별 계약, Owner/Provider/Consumer, Source of Truth, 선언 위치 (Inside·`run_command` 등 옛 내용이 섞여 있음; 어긋나면 결정 기록이 우선)
- [모듈 경계 및 계약](docs/module-boundaries.md) — 의존성 규칙, 인터페이스, 계약 검증
- [프롬프트 구성 계약](docs/prompt-composition.md) — Cache-first 순서, Trigger 참조, Tool 그룹, Reasoning/Provider 매핑
- [Control–Prompt–Provider v0.1](docs/control-prompt-provider-v0.1.md) — 한 Run의 네이티브 요청·응답, ToolCall 배리어 (질문·Pause 부분은 V3-P106에서 빠짐)
- [데이터·Tool·Artifact 설계](docs/data-and-tools.md) — 원본 실행 기록, Conversation, Artifact, 병렬 실행
- [Data Contract v0.2](docs/data-contract-v0.2.md) — kind별 Event, Run/Tool 상태 전이, raw 저장, 트랜잭션·원자성, Data 공개 Port ([v0.1](docs/data-contract-v0.1.md)은 그 바탕 원칙)
- [Pytest 운영 정책](docs/testing-policy.md) — 계약 변경에만 전체 pytest, 그 외에는 변경 모듈별 pytest

기능별 설계 (지금 있는 기능):

- 대화와 실행: [대화별 모델](docs/conversation-model-v0.1.md), [스트리밍](docs/streaming-v0.1.md), [작업 진행 줄](docs/work-progress-v0.1.md), [대화 삭제와 업그레이드 백업](docs/conversation-delete-v0.1.md)
- 프롬프트: [프롬프트 정비](docs/prompt-cleanup-v0.1.md), [인터페이스 능력과 반려 파일 수신](docs/interface-capabilities-v0.1.md), [사용자 지시문](docs/user-instructions-v0.1.md), [반려 바탕과 여러 페르소나](docs/companion-persona-v0.1.md)
- 파일: [서버 줄이기(대화 폴더)](docs/server-slim-v0.1.md), [파일 올리기·내려받기](docs/file-upload-download-v0.1.md), [이미지](docs/images-v0.1.md), [문서 읽기](docs/documents-v0.1.md)
- MCP: [서버의 안내문·프롬프트·리소스](docs/mcp-context-v0.1.md), [Settings의 기본값](docs/mcp-defaults-v0.1.md)
- 반려: [반려 1단계](docs/companion-v0.1.md), [화면 보여 주기](docs/companion-screen-v0.1.md), [화면 도구](docs/screen-tool-v0.1.md), [활동 신호](docs/activity-signals-v0.1.md), [캐릭터 애니메이션](docs/companion-animation-v0.1.md), [앱의 답 준비 표시](docs/windows-reply-progress.md), [모델 카탈로그](docs/openrouter-model-catalog.md)
- 먼저 말 걸기: [v0.3 간략화](docs/proactive-v0.3.md)(지금 규칙), [v0.2](docs/proactive-v0.2.md)·[v0.1](docs/proactive-v0.1.md)(나머지 세부)
- 기억: [메모리 1단계](docs/memory-v0.1.md), [메모리 2단계](docs/memory-v0.2.md), [대화 검색](docs/conversation-search-v0.1.md)
- Web: [Web 다시 디자인](docs/web-console-v0.1.md) — §4·§7이 지금 React Web의 설계(hub 작업 콘솔 부분은 V3-P106에서 빠짐)

기록:

- [배포 기록](docs/deployments/) — 운영 서버 배포마다 하나
- [보관 문서](docs/archive/README.md) — 빠지거나 대체된 기능의 설계, 옛 결정 기록, 옛 미결정 사항(2026-10-13)

## 코드 및 실행

### Web 서버 실행

```bash
uv pip install -e ".[web]"                      # FastAPI·uvicorn (선택 설치)
(cd web && npm ci && npm run build)              # Web 화면(V3-P102)을 패키지의 dist/로 빌드: 없으면 /는 빌드 안내만
mkdir -m 700 -p ~/.himari                       # 실행 사용자 전용 데이터 폴더
HIMARI_DATA_ROOT=~/.himari python -m himari.interfaces.web --env-file .env
```

- `.env`(git 제외)에 `OPENROUTER_API_KEY=...`를 둡니다. 선택: `HIMARI_MODEL`(기본 모델, 기본 `openai/gpt-6-luna`), `HIMARI_REASONING_EFFORT`(기본 모델의 추론 강도), `HIMARI_MODELS`(대화마다 고를 수 있는 모델 목록, `모델id[=강도]`를 쉼표로; 기본 모델을 포함해야 함. 예: `openai/gpt-6-luna,anthropic/claude-haiku-5.5=medium`), `HIMARI_WEB_PORT`(기본 8765), `HIMARI_SANDBOX=0`(문서·이미지 처리의 sandbox 끄기).
- 한 턴(메시지 하나에 대한 작업)은 `HIMARI_MAX_RUNS`(기본 12, 1 이상) Run에서 멈춥니다(V3-P106). 멈춘 턴은 Web의 **Continue**로 이어 갑니다. 쓴 토큰은 Web 입력창 아래(작업 중)에 보입니다.
- **대화 삭제 (V3-P65):** Web 대화 메뉴(**⋯**)의 **Move to trash…**는 대화를 휴지통으로 옮깁니다(실행 중이면 먼저 멈춤). 휴지통(사이드바 **Settings → Trash**)에서 복원하거나 바로 영구 삭제할 수 있고, `HIMARI_TRASH_DAYS`(기본 30, 1~365)일이 지나면 서버가 영구 삭제합니다. 영구 삭제는 대화의 모든 기록과 대화 폴더(`conversations/<id>`)를 지웁니다. MCP 서버가 자기 쪽에 저장한 데이터는 지우지 않으며, 확인 창이 그 서버 이름을 알려 줍니다.
- **메모리 (V3-P71):** HIMARI가 대화에서 사용자에 대한 메모(1~2문장)를 저장해 다음 대화에서 씁니다. 분류는 넷입니다: `user`(하는 일·환경), `preference`(선호), `project`(진행 중인 일), `reference`(경로·링크·이름). 모델은 사용자가 말한 것만 `memory_save`로 저장하고, `memory_forget`으로 지우고, `memory_search`로 찾습니다(승인 없이 실행, 저장할 때마다 대화에 줄이 남음). `user`·`preference` 메모는 매 요청의 Current State(`user_memory`, 2,000자까지)에 데이터로 들어가 캐시를 깨지 않고, 나머지는 개수만 알려 모델이 검색합니다. 턴이 시작될 때 읽으므로 고치거나 지운 메모는 열린 대화에서도 다음 메시지부터 반영됩니다. 메모는 하나 500자, 1,000개까지입니다. Web의 Memory 화면에서 관리합니다. 대화를 영구 삭제하면 그 대화에서 저장된 메모도 함께 지워집니다. `HIMARI_MEMORY=0`이면 끕니다(메모는 남음).
- **대화 검색 (V3-P72):** 모델이 `conversation_search`로 지난 대화의 원문(사용자 메시지와 HIMARI의 답)을 낱말로 찾고, `conversation_read`로 찾은 대화를 이어 읽습니다. 같은 사용자의 대화만, 휴지통은 빼고, 지금 대화는 압축 요약이 덮는 앞부분만 찾습니다(압축해도 원문은 남음). 도구 결과와 첨부는 찾지 않습니다. 검색 결과는 그 대화의 기록에 남아, 원래 대화를 영구 삭제해도 옮겨 온 글은 남습니다. `HIMARI_CONVERSATION_SEARCH=0`이면 끕니다.
- **스트리밍 (V3-P73):** 확인한 모델(gpt-6-luna, claude-haiku-5.5)은 답을 OpenRouter 스트림으로 받아 Web 화면에 쓰는 대로 보입니다. 첫 글 전에는 움직이는 생각 중 표시(밝은 테마는 해바라기, 어두운 테마는 볕 드는 자리 단어)와 경과 시간이 나옵니다. 원본 SSE를 바이트 그대로 보존하고, 중지·끊김·서버 종료에도 받은 부분을 남기며, 중지하면 연결을 끊습니다. 끝나지 않은 답은 흐리게 남고 기록·모델 입력에는 들어가지 않습니다. `HIMARI_STREAMING=0`으로 끕니다. 설계: [streaming-v0.1.md](docs/streaming-v0.1.md).
- **DB 업그레이드 전 백업:** 새 릴리스가 DB 스키마를 올릴 때는 먼저 `himari.db.v<이전 버전>-<시각>.bak`(0600)을 만들고 검증한 뒤에만 올립니다. 백업에 실패하면 시작하지 않습니다. 경로는 시작 로그에 나오며, 백업은 자동으로 지우지 않습니다(그 시점의 모든 대화가 들어 있으니 필요 없어지면 운영자가 지웁니다). 되돌리는 절차는 [설계 §8.2](docs/conversation-delete-v0.1.md#82-되돌리기-절차-운영자)에 있습니다.
- 브라우저에서 `http://127.0.0.1:8765/`를 열고 `~/.himari/control/operator.secret`의 토큰으로 로그인합니다.
- Web 사이드바 하단의 **Settings** 메뉴에서 **Look**(**Classic · 담백한**: 흰색·차콜·고딕체 / **Warm · 차분한**: 미색·테라코타·답변 명조체)과 **Mode**(System·Light·Dark)를 즉시 전환합니다. 선택은 이 브라우저에 저장하며, 새로고침 후에도 유지됩니다. 브라우저 저장소를 사용할 수 없으면 현재 페이지만 적용하고 저장하지 못했다는 안내를 표시합니다. **Instructions**·**Personas**·**Memory**·**Devices**·**Tool servers**·**Trash**·**Sign out**도 이 메뉴에 있습니다.
- **페르소나 (V3-P108):** Settings → **Personas**에서 반려 페르소나(목록에만 보이는 이름 + HIMARI가 누구인지 적은 글)를 20개까지 저장하고, 그 가운데 하나(또는 없음)를 고릅니다. 고른 페르소나는 모든 대화·모든 기기에서 다음 턴부터 쓰입니다. 반려가 꺼진 서버(`HIMARI_COMPANION=0`)에서는 저장만 되고 쓰이지 않으며, HIMARI는 짧은 대화 비서의 바탕 글로 답합니다. 설계: [companion-persona-v0.1.md](docs/companion-persona-v0.1.md).
- 홈 입력창에서 바로 질문하거나 파일을 첨부할 수 있습니다(첫 전송·첨부 때 대화 생성). 모델·Reasoning 선택은 대화 상단, 이름 변경·휴지통 이동은 사이드바 항목별 **⋯**에서도 제공합니다. 이미지 썸네일은 확대 보기를 열며 원본 다운로드는 별도 파일 카드에서 합니다.
- 답변의 수식(`$…$`·`\(…\)` 인라인, `$$…$$`·`\[…\]` 블록)은 KaTeX로 그립니다. KaTeX(0.18.11, MIT)는 서버가 직접 서빙하고(`static/katex`, 외부 CDN 없음) 첫 수식이 나올 때만 불러옵니다. `$5 and $10` 같은 금액과 코드 안의 `$`는 글자 그대로이고, KaTeX가 거부한 수식은 쓰인 그대로 보입니다.
- 서버는 평문 HTTP라서 loopback 주소(`127.0.0.1`·`::1`·`localhost`)에만 바인딩합니다. 다른 기기에서 쓰려면 같은 기기의 TLS reverse proxy 뒤에 두고 `HIMARI_WEB_ALLOWED_HOSTS=<공개 이름>`·`HIMARI_WEB_ALLOWED_ORIGINS=https://<공개 이름>`을 지정하세요. 외부 주소 바인딩이나 다른 기기용 평문 `http://` Origin은 시작 단계에서 거부됩니다(토큰 보호).

### MCP 서버 연결 (V3-P53)

`<HIMARI_DATA_ROOT>/mcp.json`(권한 0600)에 연결할 MCP 서버(Streamable HTTP)를 적으면 시작할 때 연결하고, **목록에 적은 Tool만** External Tool로 등록합니다. `effect`(`read`·`scoped_write`·`unknown`)는 운영자가 정하며 서버가 보내는 힌트는 믿지 않습니다.

```json
{"servers": {"calliope": {
  "url": "http://127.0.0.1:8790/mcp",
  "token_env": "CALLIOPE_MCP_TOKEN",
  "default": "on",
  "tools": {"position": {"effect": "read"}, "engine": {"effect": "read"}}
}}}
```

- Stop이나 응답 시간 초과 때는 서버에 MCP 취소 알림(`notifications/cancelled`)을 보냅니다. 최선 노력이라 서버 안의 작업이 멈췄다고 보장하지는 않습니다(쓰기 Tool의 호출은 결과 불명으로 기록됩니다).
- URL은 `https://` 또는 loopback 주소의 `http://`만 허용합니다. 토큰은 파일이 아니라 `token_env`가 가리키는 환경 변수(또는 `--env-file`)에서 읽고, `Authorization: Bearer`로 그 URL에만 보냅니다(리다이렉트를 따라가지 않음).
- 연결되지 않은 서버는 그 Tool만 빼고 시작하며, 시작 로그와 `/tools`에 이유가 표시됩니다(재시작해야 다시 연결).
- 대화마다 서버를 켜고 끌 수 있습니다: Web 대화 메뉴(**⋯**)의 **Tool servers…**. 따로 정하지 않은 대화는 서버의 `default`를 따릅니다.
- **`read`의 뜻:** 다시 실행해도, 실행됐는지 몰라도 아무것도 달라지지 않는 Tool입니다. 외부 파일뿐 아니라 서버의 세션 상태(발급한 id, 예산 등)도 포함합니다. `read` 호출은 claim되지 않아서, 요청이 간 뒤 Stop이 오면 "실행 안 됨"으로 보고되기 때문입니다. 그 보고가 해로울 수 있으면 `scoped_write`로 적습니다.

**stdio 서버는 띄우지 않습니다(V3-P109).** `url` 대신 `command`가 적힌 항목은 시작을 막지 않고 그 서버만 빠지며, `/tools`와 시작 로그에 이유가 보입니다. stdio로만 나오는 서버는 HTTP로 이어 주는 다리를 따로 띄워 `url`로 붙입니다.

**서버 안내문과 리소스 (V3-P60).** Tool 말고도 서버가 주는 것을 모델이 쓰게 합니다. `tools`는 선택이라, 안내문이나 리소스만 주는 서버도 연결됩니다.

```json
"instructions": "system",
"resources": {"effect": "read", "uris": "listed"}
```

- **`instructions`**: 서버가 `initialize`에서 준 안내문을 어디에 둘지 정합니다.
  - `system`(기본): 시스템 메시지의 HIMARI 지시문 뒤에 "Tool server notes" 절로 넣습니다. 내용을 아는, 신뢰하는 서버에만 씁니다.
  - `data`: 매 요청 끝의 현재 상태 블록 안에 데이터로만 넣습니다. 외부에서 받았거나 내용을 모르는 서버에 씁니다.
  - `off`: 넣지 않습니다.
  - 그 대화에서 켜진 서버의 것만, 서버당 4,000자·모두 합쳐 16,000자까지 들어갑니다.
- **`resources`**: 서버가 리소스를 제공하면 모델에게 `<서버>/list_resources`와 `<서버>/read_resource`(32,000자씩, `offset`으로 이어 읽기)를 줍니다.
  - `effect`(기본 `unknown`): 다른 Tool과 같은 뜻입니다. 읽기가 서버에서 아무것도 바꾸지 않는다고 확인한 서버만 `read`로 둡니다.
  - `uris`(기본 `listed`): 서버의 목록에 있거나 URI 템플릿에 맞는 URI만 읽습니다. `any`로 넓히면 서버가 받아들이는 모든 URI를 읽게 되어, 접근 범위가 그 서버의 리소스 전체가 됩니다.
  - 화면(`ui://`)과 서버가 사용자용(`audience: ["user"]`)으로 표시한 리소스는 목록에서 빠지고 읽지 않습니다. 읽은 결과에 섞여 온 것도 빼고 `[omitted: restricted resource]`만 남깁니다.
  - 서버의 목록이 너무 커서(항목 5,000개, 페이지 20개 또는 2 MiB 초과) 끝까지 읽지 못하면 그 서버의 리소스는 읽지 않습니다. 읽지 못한 부분이 어떤 URI를 사용자용으로 표시했을 수 있기 때문입니다.
  - `false`로 끕니다. 서버의 Tool에 같은 이름(`list_resources`·`read_resource`)이 있으면 서버의 것이 쓰입니다.
- **`prompts`**(기본 `true`): 서버가 주는 프롬프트를 **사용자가** 골라 씁니다. 모델은 스스로 꺼내 쓰지 못합니다.
  - Web: 입력창 맨 앞에 `/`를 치면 그 대화에서 켜진 서버의 프롬프트가 나옵니다. 이어 치면 걸러지고, 화살표·Enter로 고르며 Esc로 닫습니다. 인자가 있으면 입력 칸이 뜹니다. **Fill in**을 누르면 서버의 글이 출처 줄(`[MCP prompt 서버/이름 · 인자]`)과 함께 입력창에 채워집니다. 읽고 고친 뒤 보내면 일반 메시지로 갑니다. 고르기만으로는 아무것도 보내지 않습니다.
  - assistant 메시지, 이미지·오디오·바이너리 리소스를 담은 프롬프트와 32,000자를 넘는 프롬프트는 거부합니다(무엇도 채우지 않음).

**Tool 화면 (MCP Apps).** 보여 주지 않습니다(V3-P97). 서버 항목에 `"ui"`가 남아 있어도 무시하며, 모델이 부를 수 없는 화면 전용 Tool(`visibility`에 `model`이 없음)은 등록하지 않습니다.

브라우저 검증: 서버 프롬프트의 `/` 고르기는 `python scripts/mcp_prompts_smoke.py`.

### CLI

Web에 없는 명령만 남아 있습니다(V3-P98): 대화·파일·메모·지침·폴더는 Web에서 합니다. 서버가 실행 중일 때 같은 사용자로 실행합니다(DB를 직접 열지 않고 서버에 접속).

```bash
export HIMARI_DATA_ROOT=~/.himari               # 운영자 토큰을 읽을 데이터 폴더
himari proactive                                # 먼저 말 걸기 상태 (python -m himari.interfaces.cli proactive)
himari proactive try                            # 지금 한 번 말 걸어 보기 (가장 최근 대화, 현재 기기)
himari memory passes conv_1                     # 그 대화에 적용된 기억 정리 목록
himari memory backfill conv_1                   # 아직 돌지 않은 기억 정리를 지금 실행
himari memory retry conv_1 mpass_3              # 실패한 기억 정리 하나를 다시 실행
```

종료 코드: 0 완료, 1 거부·서버에 닿지 못함, 2 사용법·설정 오류, 4 결과 불명.

공개 계약은 각 모듈의 `api.py`가 원본입니다(V3-046). P0 실행 경로는 `build_runtime(provider, store=SqliteStore(path))`로 영속 Data를, 저장소를 생략하면 인메모리 Data를 사용하며, Fake Provider뿐 아니라 **실제 OpenRouter LLM**으로도 실행할 수 있습니다.

| 모듈 | 계약 |
| --- | --- |
| `src/himari/common.py` | 공용 식별자·값 타입 (`TrustedScope`, `ToolCall`, `ModelToolResult` …) |
| `src/himari/data/api.py` | `ConversationLog`, `ArtifactStore`, `WorkspaceState`, `StateQuery`, `ExternalSourceSettings` |
| `src/himari/tool/api.py` | `ToolHandler` SPI, `ToolOutcome`, `ToolCatalog`, `ExternalAccess`, `ToolDispatcher` |
| `src/himari/environment/api.py` | `EnvironmentPlugin` SPI |
| `src/himari/prompt/api.py` | `PromptComposer`(순수), `PromptContextLoader`, `ModelInput` |
| `src/himari/provider/api.py` | `ProviderAdapter` SPI, 정규화 출력 `FINAL \| TOOL_CALLS` |
| `src/himari/control/api.py` | `ConversationDriver`, `DriveResult` / `DriveState` |

구현(P0): `data/memory.py`(인메모리 Data)·`data/sqlite.py`+`data/codec.py`(SQLite 영속 Data, V3-P42), `tool/registry.py`·`tool/dispatcher.py`, `prompt/composer.py`(순수)·`prompt/loader.py`, `provider/scripted.py`(Fake), `control/driver.py`, `bootstrap/`(조립).

MCP 클라이언트: `mcp/`(설정 `config.py`, 클라이언트·Streamable HTTP `client.py`, JSON-RPC `jsonrpc.py`, 프로토콜 판 `revision.py`, Tool 연결 `tools.py`, 리소스 Tool `resources.py`·URI 템플릿 `uritemplate.py`, 프롬프트 글 `prompts.py`; V3-P52/P53/P60; stdio는 V3-P109에서 뺐다). Environment와 같은 층의 플러그인 패키지로 `tool.api`만 사용한다(V3-P57). External Tool 노출·실행 권한은 `tool/access.py`.

Machine Environment: `environment/machine/`(V3-P74, V3-P109). 대화마다 데이터 폴더 아래 `conversations/<id>` 폴더를 두고, 파일 Tool·업로드는 서버 프로세스 안에서 그 폴더에서만 읽고 쓴다(`folders.py`, 링크는 따라가지 않음). `HIMARI_MACHINE_PRINCIPALS`에 없는 사용자의 대화는 말만 한다(Inside Environment는 V3-P101에서 없어졌다). `HIMARI_MACHINE_SOCKET`은 더 이상 쓰지 않는다.

실호스트 Sandbox 검증(선택 실행): `HIMARI_SANDBOX_HOST=1 pytest tests/sandbox_host`.

실제 Provider: `provider/openrouter.py`(OpenRouter, wire-exact V3-048) 및 턴별 continuation 재생(V3-049). 실제 호출 검증은 opt-in입니다: `.env` 또는 환경 변수에 `OPENROUTER_API_KEY` 설정 후 `HIMARI_LIVE=1 python -m pytest -s tests/live`. 일반 pytest/CI에서는 라이브 호출을 건너뜁니다.

```bash
uv venv && uv pip install -e ".[dev]"
python -I scripts/check_imports.py   # 모듈 경계 (정적)
python -m mypy                       # strict 타입 검사 (정적)
python -m pytest tests/<module>/     # 변경 모듈만; 공개 계약 변경 시 전체 pytest (V3-038)
```

## 상태 및 규칙

이 문서는 v2 코드와 호환성을 보장하는 명세가 아닙니다. v2의 구성을 무조건 이식하지 않고, v3 결정과 모듈 경계에 따라 새로 설계합니다.

지금 유효한 결정은 [결정 기록](docs/decisions.md)에만 있습니다. 거기 없는 설계 예시(JSON, ID 이름, 디렉터리 등)는 구현 계약이 아닙니다. 설계 문서의 Confirmed·Proposed 표시는 그 문서를 쓸 때의 상태입니다. 아직 정하지 않은 것은 결정 기록 끝의 "열린 문제"에 있습니다.

기록 기준: 2026-11-24.
