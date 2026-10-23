# HIMARI v3

HIMARI v3의 **아키텍처와 구현 현황**입니다. 2026-10-13 기준 PR #1~#9가 `main`에 병합됐고 영속 Data(SQLite, Q-41)가 검수 중입니다. P0 실행 엔진(인메모리 또는 SQLite Data), 실제 OpenRouter Provider, 턴별 reasoning continuation, Inside Environment 파일 Tool과 Sandbox `run_command`가 구현돼 있습니다. **사용자용 Web/CLI·자원 한도(cgroup/rlimit)는 아직 없습니다.**

## 구현 현황 (2026-10-13, PR #9 병합 + 영속 Data PR 기준)

| 영역 | 구현됨 | 아직 미구현 |
| --- | --- | --- |
| Control | Run 반복, FINAL, `ask_user`, `resume`, Conversation별 프로세스 내 lock | 정식 Pause/Cancel, 다중 worker |
| Data | Conversation Event/Run/ToolExecution/Artifact, raw 요청·응답, T3/T6 claim 및 UNKNOWN 복구. 인메모리(테스트) + **SQLite 영속 저장**(WAL·`synchronous=FULL`, 포트 호출당 트랜잭션 1개, 단일 writer 잠금, 0600 파일·비공개 폴더 강제, 스키마 식별 검증) | 스키마 마이그레이션, 저장 데이터 암호화·보존/삭제 정책, 백업 |
| Prompt | 전체 이력·ToolResult·CurrentState·턴별 continuation 재구성 | Context 압축·예산, Snapshot의 압축 활용 |
| Provider | Scripted Fake + 실제 OpenRouter, wire-exact 요청 전송·요청/응답 원본 기록, reasoning effort/usage/continuation | 추가 Provider, Streaming |
| Tool | Registry·Schema 검증·Dispatcher·결과 및 Artifact 커밋, Inside 파일 Tool 5종, `inside/run_command` | Memory·Plan Tool, 병렬 Scheduler(현재 순차 + Workspace별 직렬화) |
| Environment | EnvironmentPlugin SPI, Binding별 작업 폴더, Inside(openat2 경계), bubblewrap+Landlock Sandbox·시작 시 탈출 자체 검사·Workspace admission·이 서버 실호스트 게이트 | CPU·메모리·프로세스 수 한도, 다른 배포 호스트 검증, 원격·로컬 PC Environment |
| 인터페이스 | **Web 서버와 화면**(v2 화면·보안 계층 재사용, 운영자 로그인, 대화 목록·실행·질문 답변), **CLI**(Web 서버의 클라이언트), `Application` 조립, 대화 목록 포트, 백그라운드 실행기 | 파일 업로드·다운로드, 실시간 스트리밍(현재 폴링), 다중 사용자 |

**검증 경계:** Data·통합 테스트는 인메모리와 SQLite 두 백엔드로 모두 실행되며, SQLite에서는 재시작이 DB 파일을 닫고 다시 여는 것이고 실제 `SIGKILL` 크래시(송신 중·부작용 중·트랜잭션 도중·커밋 직후)도 검증합니다. 전원 차단 내구성은 SQLite·파일시스템의 fsync 보장에 의존하며 직접 시험하지 않았습니다. OS Sandbox는 이 서버에서 실호스트 게이트(`HIMARI_SANDBOX_HOST=1 pytest tests/sandbox_host`)를 통과했지만 다른 호스트에서는 다시 실행해야 하고, 자원 한도는 아직 없습니다. 구현된 공개 계약은 각 모듈 `api.py`가 원본입니다.

> 설계 원칙: **Contract-based Modular Architecture** — 모듈은 서로의 내부 구현에 의존하지 않고, 명시된 계약을 유지하는 범위에서 독립적으로 수정·교체할 수 있어야 합니다.

## 결정된 핵심 방향

- **Run = LLM 호출 1회**. Control이 Run 반복을 조정하며, 모델의 final response 또는 question에서 반복을 중단합니다. **question은 suspending Tool `ask_user` 호출**로 표현하고, 대기 중에는 lock을 해제하며 사용자 답변을 원래 call의 ToolResult로 커밋한 뒤 재개합니다. v2의 Task/ManagerTurn 계층을 v3의 실행 기본 단위로 가져오지 않습니다.
- **Data**는 Conversation의 원본 입력·출력을 exact하게 보존합니다. Prompt는 Data를 가공해 다음 모델 입력을 만들고, **최신 Prompt Snapshot 1개**(압축 문맥 + 최근 exact 입력 데이터)만 유지합니다.
- **Tool 실패**는 ToolResult로 전달하며, 그 이후의 대응은 다음 Run에서 모델이 판단합니다. Runtime의 숨은 의미론적 자동 복구는 기본 동작이 아닙니다.
- **Workspace**는 독립적인 수명을 가진 이종 작업 대상입니다(기본 제공 작업 공간, 사용자 로컬 PC, 원격 환경 등). Conversation 사이에서도 참조할 수 있습니다.
- **Environment**는 Workspace의 연결·접근·실행 방식을 규정합니다. 한 Conversation에서 여러 Environment가 활성화될 수 있고, 동일한 Workspace를 Environment마다 다르게 다룰 수 있습니다.
- **Workspace Registry와 Binding**을 구분합니다. Tool은 Workspace Binding이 필요한 도구와 필요하지 않은 도구로 나뉩니다. 환경 도구의 이름은 `environment/tool` 형태를 고려합니다.
- **Memory**는 별도 특권 계층이 아니라 Tool 형태로 처리할 방향이며, 세부 설계는 보류합니다.
- **Provider reasoning** 설정과 사용량/공개된 continuation 정보는 Data에서 관리하고 Prompt·Provider가 지원 가능한 모델 요청을 구성합니다. 비공개 내부 추론 원문은 이용 가능하다고 전제하지 않습니다.
- **프롬프트는 Cache-first 순서**: Stable Prefix → 압축 이력 + exact Conversation → Current State. Trigger는 기존 이벤트를 가리킬 뿐 이동/복제하지 않습니다. ToolCall/Result는 provider-native 결합을 유지합니다.
- **모든 Run의 원본 Provider 요청·응답**을 기록합니다. Execution Records, exact Conversation Events, Plan/Note 등의 Artifacts는 서로 다른 논리 영역으로 구분합니다.
- **ToolCall은 충돌이 없으면 병렬 실행**할 수 있습니다. 같은 Workspace에서 부작용 범위가 불명확한 명령은 기본 직렬 처리하도록 설계합니다.
- **Workspace 전용 소프트웨어 모듈은 만들지 않습니다.** Memory 등 Internal Tool은 Data의 공개 API를 직접 사용할 수 있으며, 환경 Tool은 Environment를 경유합니다.
- **기본 Workspace는 사용자별 공유 Root** 아래 Conversation별 기본 cwd를 사용하며 허용된 같은 사용자 다른 Conversation 폴더에 접근할 수 있습니다. `inside/run_command`는 **OS Sandbox에서 실행하며 초기에는 네트워크 차단 전용 프로파일만 지원**합니다. 인간 Approval/Worktree 복제 격리는 초기 요구사항이 아니며, 향후 Web Search는 별도 Tool로 설계합니다.

### 모듈 경계 개정 (2026-10-13, V3-039~046)

- **확장 SPI 우선:** 새 Tool은 `ToolHandler`, 새 Provider는 `ProviderAdapter`, 새 Environment는 `EnvironmentPlugin` 하나를 구현해 등록합니다. Environment는 자기 Tool을 직접 제공합니다.
- **Data Port는 Consumer별로 분리:** `ConversationLog` · `ArtifactStore` · `WorkspaceState` · `StateQuery` (P2 `ControlState`). 백엔드는 공유해도 계약 변경 단위는 sub-port입니다.
- **Tool 구현은 실행 원장을 모릅니다:** `ToolOutcome`을 반환하고 원자 커밋은 Tool Dispatcher가 합니다.
- **Prompt 핵심은 순수 함수** `compose(PromptInputs)`입니다.
- **P0는 단일 프로세스 규모:** Conversation별 in-process lock으로 동시 실행을 막고, 크래시 후 중복은 송신·부작용 직전 영속 표시와 UNKNOWN 처리(자동 재실행 없음)로 막습니다. 분산 lease·fencing·다중 worker 복구는 P2로 연기합니다.
- **계약은 코드로:** 각 모듈 `api.py`의 Protocol + frozen dataclass가 생기면 그것이 원본이고, import-linter로 의존 방향을 강제합니다.

## 문서

- **[통합 모듈 계약 기준서](docs/contracts.md)** — 모듈별 계약, Owner/Provider/Consumer, Source of Truth, 선언 위치, 상태 및 의존성 (주요 진입점)
- [아키텍처](docs/architecture.md) — 역할, 데이터 흐름, Run·Prompt·Tool·Environment·Workspace 구조
- [모듈 경계 및 계약](docs/module-boundaries.md) — 의존성 규칙, 인터페이스, 계약 검증
- [프롬프트 구성 계약](docs/prompt-composition.md) — Cache-first 순서, Trigger 참조, Tool 그룹, Reasoning/Provider 매핑
- **[Control–Prompt–Provider v0.1 경계 초안](docs/control-prompt-provider-v0.1.md)** — 한 Run의 네이티브 요청·응답, ToolCall 배리어, final/question 종료 및 재개
- [데이터·Tool·Artifact 설계](docs/data-and-tools.md) — 원본 실행 기록, Conversation, Artifact, 병렬 실행
- **[Data Contract v0.2 독립 검수 후보](docs/data-contract-v0.2.md)** — kind별 Event, Run/Tool 상태 전이, raw 저장, 트랜잭션·원자성, Data 공개 Port
- [Data Contract v0.1 이전 초안](docs/data-contract-v0.1.md) — 초기 원칙·설계 근거
- [CurrentState Contract v0.1 초안](docs/current-state-contract-v0.1.md) — 활성 Environment·Workspace Binding·Artifact의 읽기 투영 및 최신 상태 검증 (검토 전)
- [기본 Workspace·파일/명령 실행 계약 v0.2](docs/default-workspace-files-v0.2.md) — Conversation별 폴더, 다른 대화 폴더 접근, Sandboxed `run_command` 및 파일 생성
- [Sandbox Execution v0.1 검토 초안](docs/sandbox-execution-v0.1.md) — v2 Bubblewrap/Landlock 기반 재설계, 사용자별 Root, 네트워크 기본 차단
- [Sandbox Implementation v0.2 검토 초안](docs/sandbox-implementation-v0.2.md) — v2 코드 이식 지도, Environment 공개 계약, Sandbox 활성화·검증 및 bounded 명령 실행
- **[Pytest 운영 정책](docs/testing-policy.md)** — 계약 변경에만 전체 pytest, 그 외에는 변경 모듈별 pytest (확정 V3-038)
- [Control Pause/Resume 계약 초안](docs/control-pause-v0.1.md) — Run 간 정지·재개와 미완결 ToolCall 처리
- [MCP Apps 1단계 설계 초안](docs/mcp-apps-v0.1.md) — MCP 서버가 준 Tool 화면을 sandbox에서 읽기 전용으로 표시(V3-P58)
- [MCP stdio 지원](docs/mcp-stdio-v0.1.md) — stdio MCP 서버를 server sandbox 안의 격리 프로세스로 실행(기본 서버당 하나), Sandbox 모듈 분리(V3-P59)
- [MCP 서버의 안내문·프롬프트·리소스](docs/mcp-context-v0.1.md) — 서버 안내문을 시스템 영역에, 프롬프트는 사용자가 `/`로, 리소스는 모델용 읽기 Tool로(V3-P60, 설계 초안)
- [한 턴의 예산과 비용](docs/control-run-budget-v0.1.md) — Run 수 상한 없이, 토큰 예산·진척 없음·자리 비움이 한 턴을 지킴; 남은 예산 알림, 사용량 표시, 대화별 캐시 경로(V3-P62)
- [대화 삭제와 업그레이드 백업](docs/conversation-delete-v0.1.md) — 휴지통 → N일 뒤 영구 삭제, 대화 폴더도 함께, 외부 서버 데이터는 알림만; DB 업그레이드 전 백업과 되돌리기(V3-P65)
- [결정 기록](docs/decisions.md) — 현재까지 합의한 내용과 설계상 권고 사항 (모듈 경계 개정 V3-039~046 포함)
- [미결정 사항](docs/open-questions.md) — 초기 구현에 앞서 확정할 계약과 후속 결정

## 코드 및 실행

### Web 서버 실행

```bash
uv pip install -e ".[web]"                      # FastAPI·uvicorn (선택 설치)
mkdir -m 700 -p ~/.himari                       # 실행 사용자 전용 데이터 폴더
HIMARI_DATA_ROOT=~/.himari python -m himari.interfaces.web --env-file .env
```

- `.env`(git 제외)에 `OPENROUTER_API_KEY=...`를 둡니다. 선택: `HIMARI_MODEL`(기본 모델, 기본 `openai/gpt-6-luna`), `HIMARI_REASONING_EFFORT`(기본 모델의 추론 강도), `HIMARI_MODELS`(대화마다 고를 수 있는 모델 목록, `모델id[=강도]`를 쉼표로; 기본 모델을 포함해야 함. 예: `openai/gpt-6-luna,anthropic/claude-haiku-5.5=medium`), `HIMARI_WEB_PORT`(기본 8765), `HIMARI_SANDBOX=0`(명령 실행 끄기).
- 한 턴(메시지 하나에 대한 작업)은 Run 수 상한 없이 돕니다(V3-P62). 대신 세 가지가 턴을 지킵니다.
  - 토큰 예산 `HIMARI_TURN_TOKEN_BUDGET`(기본 3,000,000, `0`이면 끔, 최소 16,384): 쓴 토큰, 다음 요청의 추정, 그 답을 위해 남겨 두는 양(8,192 이상)을 더해 예산을 넘으면 그 요청을 보내지 않고 멈춥니다. 셈은 토큰이며, 캐시에서 읽힌 입력도 같이 셉니다.
  - 진척 없음: 같은 결과만 되풀이하는 Run이 3번 이어지면 멈춥니다.
  - 자리 비움 `HIMARI_UNATTENDED_RUNS`(기본 20, `0`이면 끔): 60초 안에 대화를 읽은 클라이언트가 없는 채로 그만큼 Run이 지나면 일시정지합니다.
  - `HIMARI_MAX_RUNS`(기본 `0` = 없음)로 Run 수 상한을 더할 수 있습니다. 예산과 상한을 둘 다 끄면 시작이 거부됩니다.
  - 멈춘 턴은 Web의 **Continue** 버튼(입력창 위)이나 CLI의 `continue`로 이어 가며, 이어 갈 때 새 예산을 받습니다. 쓴 토큰은 Web 입력창 아래(작업 중)와 CLI 턴 끝에 보입니다.
- **대화 삭제 (V3-P65):** Web 대화 메뉴의 **Delete conversation**이나 CLI `himari delete ID`는 대화를 휴지통으로 옮깁니다(실행 중이면 먼저 멈춤). 휴지통(Web 사이드바 **Trash**, CLI `himari trash`)에서 복원하거나 바로 영구 삭제할 수 있고, `HIMARI_TRASH_DAYS`(기본 30, 1~365)일이 지나면 서버가 영구 삭제합니다. 영구 삭제는 대화의 모든 기록과 대화 폴더(`conversations/<id>`)를 지웁니다. MCP 서버가 자기 쪽에 저장한 데이터는 지우지 않으며, 확인 창이 그 서버 이름을 알려 줍니다.
- **DB 업그레이드 전 백업:** 새 릴리스가 DB 스키마를 올릴 때는 먼저 `himari.db.v<이전 버전>-<시각>.bak`(0600)을 만들고 검증한 뒤에만 올립니다. 백업에 실패하면 시작하지 않습니다. 경로는 시작 로그에 나오며, 백업은 자동으로 지우지 않습니다(그 시점의 모든 대화가 들어 있으니 필요 없어지면 운영자가 지웁니다). 되돌리는 절차는 [설계 §8.2](docs/conversation-delete-v0.1.md#82-되돌리기-절차-운영자)에 있습니다.
- 브라우저에서 `http://127.0.0.1:8765/`를 열고 `~/.himari/control/operator.secret`의 토큰으로 로그인합니다.
- Web 사이드바의 **Settings → Appearance**에서 **Classic · 담백한**(흰색·차콜·고딕체)과 **Warm · 차분한**(미색·테라코타·답변 명조체)을 즉시 전환합니다. 선택은 이 브라우저에 저장하며, 새로고침 후에도 유지됩니다. 브라우저 저장소를 사용할 수 없으면 현재 페이지만 적용하고 저장하지 못했다는 안내를 표시합니다. **Instructions**와 **Sign out**도 Settings에 있습니다. 사이드바 하단 상태를 펼치면 모델·명령 실행 상태를 확인합니다.
- 홈 입력창에서 바로 질문하거나 파일을 첨부할 수 있습니다(첫 전송·첨부 때 대화 생성). 모델·Reasoning 선택은 대화 상단, 이름 변경·휴지통 이동은 사이드바 항목별 **⋯**에서도 제공합니다. 이미지 썸네일은 확대 보기를 열며 원본 다운로드는 별도 파일 카드에서 합니다.
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
- 대화마다 서버를 켜고 끌 수 있습니다: Web 대화 메뉴의 **Tool servers**, CLI `/tools on|off|default 서버` 또는 `himari tools ID on calliope`. 따로 정하지 않은 대화는 서버의 `default`를 따릅니다.
- **`read`의 뜻:** 다시 실행해도, 실행됐는지 몰라도 아무것도 달라지지 않는 Tool입니다. 외부 파일뿐 아니라 서버의 세션 상태(발급한 id, 예산 등)도 포함합니다. `read` 호출은 claim되지 않아서, 요청이 간 뒤 Stop이 오면 "실행 안 됨"으로 보고되기 때문입니다. 그 보고가 해로울 수 있으면 `scoped_write`로 적습니다.

**stdio 서버 (V3-P59).** `url` 대신 `command`를 적으면 HIMARI가 그 서버를 직접 띄워 표준 입출력으로 이야기합니다. 서버는 **server sandbox 안에서만** 돕니다. 네트워크가 없고, 적은 폴더만 호스트와 같은 경로로 보이며, HIMARI 데이터 폴더(DB·비밀값·Workspaces)에는 어떤 경로로도 닿지 못합니다. 이 호스트에서 server sandbox가 시작 시 자체 검사를 통과하지 못하면 stdio 서버는 띄우지 않습니다.

```json
{"servers": {"calliope": {
  "command": "/home/coder/Calliope/.venv/bin/python",
  "args": ["-m", "tools.mcp", "--data", "/home/coder/.local/share/calliope"],
  "cwd": "/home/coder/Calliope",
  "env": {"PYTHONPATH": "."},
  "read_only": ["/home/coder/Calliope", "/home/coder/opt/stockfish"],
  "writable": ["/home/coder/.local/share/calliope"],
  "processes": {"scope": "shared"},
  "tools": {"open_game": {"effect": "scoped_write"}}
}}}
```

- `command`는 `read_only` 폴더나 시스템 경로(`/usr` 등) 안에 있어야 합니다(Sandbox 안에서 보이는 곳이 그것뿐). `read_only` 16개·`writable` 4개까지, `writable`은 HIMARI 실행 사용자의 폴더여야 합니다.
- 환경 변수는 고정된 것(`PATH`, `HOME=/tmp/home` 등)에 `env`와 `env_from`(HIMARI 환경에서 이름으로 복사; 토큰용)만 더합니다. HIMARI 자신의 환경은 넘기지 않습니다.
- `processes.scope`: `shared`(기본, 서버당 프로세스 하나를 모든 대화가 같이 씀) 또는 `per_conversation`(대화마다 하나, 최대 `max`개). `idle_seconds`(기본 1800) 동안 쓰지 않으면 종료하고, 다음 호출이 새로 띄웁니다. 새로 띄운 뒤의 첫 결과에는 "서버가 다시 시작됐다"는 안내가 붙습니다.
- **Stop은 최선 노력 취소입니다(MCP 표준).** 서버에 취소를 알리고 기다리기를 멈출 뿐, 서버 안의 작업이 멈췄다고 보장하지 않습니다. 취소된 작업이 서버에서 끝까지 돌아 효과(파일, 캐시 등)를 낼 수 있습니다. 쓰기 Tool의 호출은 "결과 불명"으로 기록되고 다시 실행되지 않습니다. 그래서 `read`는 늦게 끝나도 해가 없는 Tool에만 줍니다.
- Stop이나 시간 초과 뒤에는 서버에 `ping`을 보내 살아 있는지 확인합니다(그동안 새 호출은 잠깐 기다립니다). 10초 안에 답하지 않을 때만 프로세스를 끝내고 다시 띄웁니다. 그때는 그 프로세스의 다른 진행 중 호출도 결과 불명이 됩니다. 서버가 연결 단위 상태에 기대면 `per_conversation`을 고릅니다.

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
  - CLI: `/prompts`로 목록을 보고, `/prompt 서버 이름 인자=값 …`(공백이 있는 값은 따옴표)으로 펼칩니다. 펼친 글을 보여 주고 `Send it? [Y/n]`에 답하면 보냅니다.
  - assistant 메시지, 이미지·오디오·바이너리 리소스를 담은 프롬프트와 32,000자를 넘는 프롬프트는 거부합니다(무엇도 채우지 않음).

**Tool 화면 (MCP Apps, V3-P58).** 서버 항목에 `"ui"`를 적으면 그 서버가 Tool에 붙인 화면(`ui://`)을 Web에서 보여 줍니다. 적지 않으면 서버에 화면 지원을 알리지 않습니다.

```json
"ui": {"connect_domains": ["https://api.example.com"], "resource_domains": ["https://cdn.example.com"]}
```

- 두 목록은 화면이 접속할 수 있는 상한입니다. 서버가 선언한 origin 중 여기에도 있는 것만 허용합니다(기본: 외부 접속 없음).
- 화면은 별도 포트(`HIMARI_WEB_SANDBOX_PORT`, 기본 `HIMARI_WEB_PORT` + 1)의 sandbox origin에서, 저장소 없이 격리되어 돕니다. 역방향 프록시를 쓰면 이 포트도 같은 호스트로 전달해야 합니다.
- 가장 최근 호출의 화면은 자동으로, 나머지는 "Open view"로 엽니다. 실행 중에도 보이며, 대화에서 서버를 끄면 그 서버의 화면은 닫힙니다. 화면은 1단계라 읽기 전용입니다(화면에서 Tool 호출·메시지 보내기는 안 됨).
- 브라우저 검증: `python scripts/mcp_apps_smoke.py`(실제 서버, Playwright 필요), 비동기 순서는 `python scripts/app_view_races/run.py`, 설계 모형은 `python -I scripts/mcp_apps_probe/run.py`. 서버 프롬프트의 `/` 고르기는 `python scripts/mcp_prompts_smoke.py`.

### CLI

서버가 실행 중일 때 같은 사용자로 실행합니다(DB를 직접 열지 않고 서버에 접속).

```bash
export HIMARI_DATA_ROOT=~/.himari               # 운영자 토큰을 읽을 데이터 폴더
himari chat                                     # 새 대화 (python -m himari.interfaces.cli chat)
himari list; himari show conv_1; himari send conv_1 "질문"; himari continue conv_1
himari delete conv_1 [--now] [--yes]            # 휴지통으로 (--now: 바로 영구 삭제, --yes: 묻지 않음)
himari trash; himari restore conv_1             # 휴지통 목록, 복원
himari list --offset 50; himari trash --offset 50  # 다음 쪽 (더 있으면 "(more: --offset N)")
```

대화형 모드에서는 질문이 오면 `answer>`로 답하고, `/continue`·`/context`·`/compact`·`/tools`·`/prompts`·`/prompt 서버 이름 인자=값`·`/rename 이름`·`/delete`(확인 뒤 휴지통으로 옮기고 나감)·`/id`·`/exit`를 쓸 수 있습니다. `/`로 시작하는 글을 그대로 보내려면 `//`로 시작합니다. 종료 코드: 0 완료, 3 답변 대기, 1 미완료·거부, 2 설정 오류, 4 결과 불명(다시 보내기 전에 `show`로 확인).

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

구현(P0): `data/memory.py`(인메모리 Data)·`data/sqlite.py`+`data/codec.py`(SQLite 영속 Data, V3-P42), `tool/registry.py`·`tool/dispatcher.py`, `prompt/composer.py`(순수)·`prompt/loader.py`, `provider/scripted.py`(Fake), `control/driver.py`·`control/ask_user.py`, `bootstrap/`(조립).

MCP 클라이언트: `mcp/`(설정 `config.py`, 클라이언트·Streamable HTTP `client.py`, JSON-RPC `jsonrpc.py`, 프로토콜 판 `revision.py`, stdio `stdio.py`, Tool 연결 `tools.py`, 리소스 Tool `resources.py`·URI 템플릿 `uritemplate.py`, 프롬프트 글 `prompts.py`; V3-P52/P53/P59/P60). stdio 서버는 Bootstrap이 만든 server sandbox(`sandbox/server.py`)의 실행기로만 띄운다. Environment와 같은 층의 플러그인 패키지로 `tool.api`만 사용한다(V3-P57). External Tool 노출·실행 권한은 `tool/access.py`.

Inside Environment: `environment/inside/`(사용자별 Workspace, 파일 Tool 5종, openat2 경계, V3-P40)와 `inside/run_command`(bubblewrap + Landlock Sandbox, 시작 시 탈출 자체 검사, V3-P41).

```python
sandbox = SandboxBackend.discover()
await sandbox.start()            # ENFORCED only if the real-spawn self-test passes
inside = InsideEnvironment(workspaces_root, sandbox=sandbox)
```

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

**확정(Confirmed)**, **권고(Proposed)**, **미결정(Open)**을 구분합니다. 설계 예시(JSON, ID 이름, 디렉터리 등)는 명시적으로 확정하지 않는 한 구현 계약이 아닙니다.

기록 기준: 2026-10-13.
