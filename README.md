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
- [결정 기록](docs/decisions.md) — 현재까지 합의한 내용과 설계상 권고 사항 (모듈 경계 개정 V3-039~046 포함)
- [미결정 사항](docs/open-questions.md) — 초기 구현에 앞서 확정할 계약과 후속 결정

## 코드 및 실행

### Web 서버 실행

```bash
uv pip install -e ".[web]"                      # FastAPI·uvicorn (선택 설치)
mkdir -m 700 -p ~/.himari                       # 실행 사용자 전용 데이터 폴더
HIMARI_DATA_ROOT=~/.himari python -m himari.interfaces.web --env-file .env
```

- `.env`(git 제외)에 `OPENROUTER_API_KEY=...`를 둡니다. 선택: `HIMARI_MODEL`(기본 `openai/gpt-6-luna`), `HIMARI_REASONING_EFFORT`, `HIMARI_WEB_PORT`(기본 8765), `HIMARI_SANDBOX=0`(명령 실행 끄기).
- 브라우저에서 `http://127.0.0.1:8765/`를 열고 `~/.himari/control/operator.secret`의 토큰으로 로그인합니다.
- 서버는 평문 HTTP라서 loopback 주소(`127.0.0.1`·`::1`·`localhost`)에만 바인딩합니다. 다른 기기에서 쓰려면 같은 기기의 TLS reverse proxy 뒤에 두고 `HIMARI_WEB_ALLOWED_HOSTS=<공개 이름>`·`HIMARI_WEB_ALLOWED_ORIGINS=https://<공개 이름>`을 지정하세요. 외부 주소 바인딩이나 다른 기기용 평문 `http://` Origin은 시작 단계에서 거부됩니다(토큰 보호).

### CLI

서버가 실행 중일 때 같은 사용자로 실행합니다(DB를 직접 열지 않고 서버에 접속).

```bash
export HIMARI_DATA_ROOT=~/.himari               # 운영자 토큰을 읽을 데이터 폴더
himari chat                                     # 새 대화 (python -m himari.interfaces.cli chat)
himari list; himari show conv_1; himari send conv_1 "질문"; himari continue conv_1
```

대화형 모드에서는 질문이 오면 `answer>`로 답하고, `/continue`·`/rename 이름`·`/id`·`/exit`를 쓸 수 있습니다. 종료 코드: 0 완료, 3 답변 대기, 1 미완료·거부, 2 설정 오류, 4 결과 불명(다시 보내기 전에 `show`로 확인).

공개 계약은 각 모듈의 `api.py`가 원본입니다(V3-046). P0 실행 경로는 `build_runtime(provider, store=SqliteStore(path))`로 영속 Data를, 저장소를 생략하면 인메모리 Data를 사용하며, Fake Provider뿐 아니라 **실제 OpenRouter LLM**으로도 실행할 수 있습니다.

| 모듈 | 계약 |
| --- | --- |
| `src/himari/common.py` | 공용 식별자·값 타입 (`TrustedScope`, `ToolCall`, `ModelToolResult` …) |
| `src/himari/data/api.py` | `ConversationLog`, `ArtifactStore`, `WorkspaceState`, `StateQuery` |
| `src/himari/tool/api.py` | `ToolHandler` SPI, `ToolOutcome`, `ToolCatalog`, `ToolDispatcher` |
| `src/himari/environment/api.py` | `EnvironmentPlugin` SPI |
| `src/himari/prompt/api.py` | `PromptComposer`(순수), `PromptContextLoader`, `ModelInput` |
| `src/himari/provider/api.py` | `ProviderAdapter` SPI, 정규화 출력 `FINAL \| TOOL_CALLS` |
| `src/himari/control/api.py` | `ConversationDriver`, `DriveResult` / `DriveState` |

구현(P0): `data/memory.py`(인메모리 Data)·`data/sqlite.py`+`data/codec.py`(SQLite 영속 Data, V3-P42), `tool/registry.py`·`tool/dispatcher.py`, `prompt/composer.py`(순수)·`prompt/loader.py`, `provider/scripted.py`(Fake), `control/driver.py`·`control/ask_user.py`, `bootstrap/`(조립).

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
