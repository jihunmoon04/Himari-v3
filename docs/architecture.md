# HIMARI v3 아키텍처

> 상태: **현행 구조**, 2026-11-24 (V3-P106·V3-P108 기준). 2026-10-13 설계 기준선을 지금 코드에 맞춰 고쳐 썼다. 뺀 기능(작업 에이전트 쪽)은 §9에 모았고, 각 결정의 근거는 [결정 기록](decisions.md)에 있다. 세부 계약은 [통합 모듈 계약 기준서](contracts.md)와 각 설계 문서가 원본이다.

## 1. 한눈에

HIMARI v3는 **Windows 반려 에이전트의 서버 쪽 두뇌**다.

| 부분 | 위치 | 맡은 일 |
| --- | --- | --- |
| 몸 | `clients/windows/Himari.Companion` (C#/WPF) | 픽셀 캐릭터·말풍선·표정, 활동 신호, 화면, 단축키. 기기 토큰으로 서버와 짝지음 |
| 두뇌 | `src/himari` (Python, FastAPI, 단일 프로세스) | 대화, 기억, 먼저 말 걸기, 기기·활동 파악, Tool 실행 |
| 관리 도구 | `web/` (React+TypeScript) | 대화와 설정(Instructions·Personas·Memory·Devices·Tool servers·Trash) |
| 작은 CLI | `src/himari/interfaces/cli` | 운영용 명령 |

```text
Windows 반려 앱 ──┐                         ┌── OpenRouter (모델)
Web ──────────────┼─ HTTP ─▶ HIMARI 서버 ───┼── MCP 서버 (stdio는 server sandbox 안)
CLI ──────────────┘ (loopback 전용)         └── himari-runner (대화 폴더 파일, 별도 프로세스)
```

- 서버는 평문 HTTP라 loopback에만 바인딩한다. 다른 기기에서 쓰려면 같은 기기의 TLS reverse proxy 뒤에 둔다.
- 앱은 `GET /api/events`(사건 흐름)로 상태 변화를 받고, 자기가 보낸 메시지의 답과 표정은 그 대화의 `live`로 받는다([반려 1단계](companion-v0.1.md) §8).

## 2. 한 턴의 실행 — Run 반복

```text
User input (Web·앱) 또는 wake (먼저 말 걸기)
    │
    ▼
Control ── 입력을 Data에 기록
    │
    ├──▶ Prompt ── Data의 원본 이력·Snapshot·설정, Tool 카탈로그를 읽어 compose
    │       ▼
    │   Provider ── LLM 호출 정확히 1회 = Run (원본 요청·응답은 Data에)
    │
    └── 응답에 따라
         ├── final response ── 턴 끝
         └── tool call ── Tool 경로로 실행 → ToolResult를 Data에 기록 → 다음 Run
```

- **Run = LLM 호출 1회**(V3-001). Task/ManagerTurn 같은 상위 계층은 없다.
- 한 턴은 **`HIMARI_MAX_RUNS`(기본 12) Run**에서 멈춘다(V3-P106). 멈춘 턴은 Web의 **Continue**로 잇는다. 실행 중인 턴의 취소(`/cancel`)는 있다.
- 사용자에게 묻고 싶은 것은 **답의 글로 묻는다.** 사용자의 다음 메시지가 새 턴이다(질문 Tool `ask_user`는 V3-P106에서 뺐다).
- **Tool 실패는 실패 ToolResult로 모델에 전달**한다. 그 뒤의 대응은 다음 Run에서 모델이 판단하며, Runtime의 숨은 의미론적 자동 복구는 없다.
- **동시성:** 같은 Conversation의 실행은 프로세스 안 Conversation별 lock으로 직렬화한다. 크래시 후 중복은 Provider 송신·부작용 Tool 실행 직전의 영속 표시와 재시작 때의 UNKNOWN 처리(자동 재실행 없음)로 막는다(V3-045). 한 Run 안의 서로 충돌하지 않는 ToolCall은 병렬로 실행할 수 있다.

예외 경로의 상세는 [Control–Prompt–Provider v0.1](control-prompt-provider-v0.1.md)에 있다.

## 3. 서버 모듈

| 모듈 (`src/himari/…`) | 소유한 책임 | 소유하지 않는 책임 |
| --- | --- | --- |
| `control` | Run 반복·종료·취소, Tool 호출 오케스트레이션, 턴의 Run 상한 | Provider API 세부, DB 스키마, Tool 구현 |
| `prompt` | 순수 함수 `compose(PromptInputs)`, loader, 시스템 글, 문맥 압축 | Conversation 원본 변경, Provider 전송 |
| `provider` | 모델 API 통신(OpenRouter, 스트리밍·reasoning)·응답 정규화 | Run 반복 정책 |
| `tool` | `ToolHandler` SPI, 카탈로그·디스패치, `ToolOutcome` 원자 커밋, Internal Tool(기억·대화 검색·화면) | Environment별 실행 방식 |
| `data` | SQLite(WAL) 영속 저장, 원본 기록, Consumer별 공개 sub-port | 프롬프트 정책, 실제 파일 I/O |
| `environment` | `EnvironmentPlugin` SPI. `machine` Environment가 대화 폴더 파일 Tool을 직접 제공 | Run 반복 |
| `runner` | `himari-runner`: 사용자 권한으로 파일 연산을 하는 별도 프로세스(`runner.api`로만 닿음) | 그 밖의 HIMARI 코드(아무것도 import하지 않음) |
| `sandbox` | server sandbox(Bubblewrap/Landlock, 네트워크 차단)와 시작 시 자체 검사 | 무엇을 실행할지의 판단 |
| `documents`·`imaging` | 문서 읽기(`read_document`)·이미지 처리 worker, sandbox 안에서 | 대화 상태 |
| `mcp` | 범용 MCP 클라이언트(Streamable HTTP·stdio), 프롬프트·리소스, External Tool 등록 | 효과 등급 판정(운영자가 `mcp.json`에 정함) |
| `memory` | 대화가 잠잠해진 뒤 자동 기억·일화·열린 고리를 뽑는 worker([메모리 2단계](memory-v0.2.md)) | 대화의 Run |
| `presence` | 기기 연결·지금 기기·활동 신호, 사건 흐름 허브(메모리에만) | 영속 저장 |
| `proactive` | 먼저 말 걸기의 계기와 규칙(순수 함수, [v0.3](proactive-v0.3.md)) | 실행 주기(→ `bootstrap/schedule`) |
| `interfaces` | Web 서버(라우트·보안·`event_stream`·`live`)와 CLI | 도메인 규칙 |
| `bootstrap` | Composition Root: `Application.open`이 저장소를 열고 sandbox를 시작하고 플러그인을 등록해 공개 Port만 주입. 주기 작업(`schedule`: `proactive` 30초, `purge`) | 각 모듈의 내부 |

### 3.1 계약과 확장 지점

- **Contract-based Modular Architecture:** 모듈은 서로의 내부가 아니라 각자 `api.py`의 Protocol + frozen dataclass로만 닿는다. import-linter가 의존 방향을 강제한다([모듈 경계](module-boundaries.md)).
- **확장 SPI(V3-040):** 새 Tool은 `ToolHandler`, 새 Provider는 `ProviderAdapter`, 새 Environment는 `EnvironmentPlugin` 하나를 구현해 Composition Root에 등록한다.
- **Data Port는 Consumer별로 나뉜다(V3-039):** `ConversationLog`·`ArtifactStore`·`StateQuery` 등. 백엔드는 공유해도 계약 변경 단위는 sub-port다. 다른 모듈은 DB 테이블을 직접 읽거나 고치지 않는다.

## 4. Data

| 구분 | 원칙 |
| --- | --- |
| Conversation | 사용자와 HIMARI의 입력·출력을 **exact하게 보존**하는 원본 |
| Run 원본 기록 | **모든 Run의 원본 Provider 요청·응답**(스트림은 SSE 바이트 그대로) |
| Tool 실행 기록 | ToolCall 인자, 실행 사실과 결과. 원자 커밋은 Tool Dispatcher가 한다(V3-042) |
| 최신 Prompt Snapshot | 압축 요약 + 이후 exact 입력. 대화별 **최신 1개**만 유지 |
| 기억 | 메모(`user`·`preference`·`project`·`reference`), 일화, 열린 고리, 기억 작업의 원본 호출 |
| 설정 | 대화별 모델·reasoning, Tool 서버 켬/끔, 페르소나, 기기 |

- **원본 ≠ 가공된 Prompt.** 압축해도 Conversation 원본과 Run 원본 기록은 바뀌지 않는다.
- 삭제는 휴지통 → `HIMARI_TRASH_DAYS`일 뒤 영구 삭제다. 영구 삭제는 대화의 모든 기록과 대화 폴더, 그 대화에서 저장된 메모를 지운다([대화 삭제](conversation-delete-v0.1.md)).
- 스키마를 올리기 전에 DB 백업을 만들고 검증한다. 실패하면 시작하지 않는다.
- 상세: [Data Contract v0.2](data-contract-v0.2.md), [데이터·Tool·Artifact 설계](data-and-tools.md).

## 5. Prompt — 캐시 우선 구성

```text
Stable Prefix             (시스템 글: 바탕·원칙·반려 절·페르소나·지시, Tool 정의)
    ↓
Conversation             (압축 요약 + 순서를 지킨 exact Event / native Tool 그룹)
    ↓
Current State             (now, 지금 기기·활동, user_memory, 최근 일화·열린 고리, 인터페이스 능력)
    ↓
Provider 설정             (model · reasoning; 별도 API 필드)
```

- 현재 상태를 마지막에 두어 앞쪽 접두부를 가능한 한 그대로 둔다. 캐시 hit는 기대 효과이지 보장값이 아니다.
- 바탕 글은 반려 켬/끔 두 가지다. 반려가 켜져 있으면 Web에서 고른 **페르소나**가 모든 대화에 들어간다([반려 바탕과 여러 페르소나](companion-persona-v0.1.md)).
- **지시가 되는 것은 시스템 글과 사용자의 메시지뿐이다.** Tool 결과, 파일, 기억, 창 제목, wake 사유 등은 정보일 뿐 지시가 아니다(V3-P89).
- ToolCall과 그 결과는 provider-native 결합을 유지한다. Provider별 변환은 Provider Adapter의 책임이다.
- 상세: [프롬프트 구성 계약](prompt-composition.md).

## 6. 반려 기능

- **기기 짝짓기와 기기 토큰:** 좁은 권한의 토큰으로 앱이 서버에 붙는다. 서버는 기기마다 연결과 활동 신호를 보고 **지금 기기**를 정한다(`presence`).
- **표정:** Tool이 아니라 답 안의 **`[[mood:이름]]`** 표시다. 서버가 떼어 `live`의 `moods`로 보낸다. 이름은 서버가 정한 고정 목록 8개다. 듣는 중·기다리는 중·말하는 중·졸림 같은 앱 자신의 상태는 앱이 사건을 보고 정한다.
- **먼저 말 걸기(wake):** `bootstrap/schedule`이 30초마다 `proactive` 규칙으로 판단한다. 모든 wake는 깨어 있고 일하지 않는 지금 기기를 기다리며, 계기에 따라 조용함·대화 종료·간격·하루 개수 조건이 더해진다([v0.3](proactive-v0.3.md)).
- **화면:** 앱이 보여 준 화면과 화면 Tool(`look_at_screen`). 확인된 모델에서만 쓴다([screen-tool-v0.1.md](screen-tool-v0.1.md)).
- 상세: [반려 1단계](companion-v0.1.md), [활동 신호](activity-signals-v0.1.md).

## 7. Tool

| 출처 | 예 | 특징 |
| --- | --- | --- |
| Internal | `memory_save`·`memory_forget`·`memory_search`, `conversation_search`·`conversation_read`, `look_at_screen` | Data 공개 API를 직접 사용 |
| Environment (`machine`) | `machine/list_files`·`read_file`·`write_file`·`edit_file`·`delete_file`·`present_file`·`read_document` | `machine` Environment가 `tools()`로 제공. runner 경유 |
| External (MCP) | `mcp.json`에 적은 서버의 Tool | 목록에 적은 Tool만 등록, 효과 등급(`read`·`scoped_write`·`unknown`)은 운영자가 정함, 대화마다 서버 켬/끔 |

- 모든 Tool은 같은 `ToolHandler` SPI를 구현하고 `ToolOutcome`만 반환한다.
- **파일 범위:** 쓰기는 **대화 폴더**(사용자별 Root 아래 `conversations/<id>`)에만 한다. 읽기는 runner 사용자가 읽을 수 있는 곳에서 숨김 목록을 뺀 범위다([machine-workspace-v0.1.md](machine-workspace-v0.1.md), V3-P100).
- **sandbox:** 문서·이미지 처리와 stdio MCP 서버는 server sandbox 안에서 돈다. 네트워크가 없고, HIMARI 데이터 폴더에는 닿지 못한다. 자체 검사를 통과하지 못하면 stdio 서버는 띄우지 않는다. `HIMARI_SANDBOX=0`으로 끈다.

## 8. 기억

- **1단계(V3-P71):** 모델이 사용자가 말한 것만 `memory_save`로 저장한다. `user`·`preference`는 매 요청의 Current State에 들어가고(2,000자까지), 나머지는 검색한다.
- **2단계(V3-P85):** 대화가 잠잠해진 뒤 `memory` worker가 싼 모델로 한 번 읽어 자동 기억·일화·열린 고리를 만든다. 자동 기억은 물러남(`retired_at`)으로 지우고 30일 안에 되살릴 수 있다. 최근 일화와 열린 고리는 Current State에 들어간다.
- **대화 검색(V3-P72):** 지난 대화의 원문 회상은 메모리가 아니라 `conversation_search`가 맡는다.
- 상세: [memory-v0.1.md](memory-v0.1.md), [memory-v0.2.md](memory-v0.2.md), [conversation-search-v0.1.md](conversation-search-v0.1.md).

## 9. 뺀 것

| 뺀 것 | 결정 |
| --- | --- |
| hub 중계·작업 콘솔, 작업 앱 | V3-P106 (V3-P102 hub 부분·V3-P103·V3-P104 철회) |
| `run_command`와 명령용 sandbox(네트워크 프록시, cgroup) | V3-P106 |
| 붙인·등록한 Workspace, Workspace Registry·Binding의 사용, 서버 파일 탐색기 | V3-P106 (`workspaces` 표는 스키마에 남지만 쓰지 않음) |
| 질문 Tool `ask_user`, Pause, 남은 턴 닫기 | V3-P106 |
| 턴의 토큰 예산·진척 없음·자리 비움 | V3-P106 (Run 상한 하나로) |
| 붙인 Workspace 쓰기, `git_push` | V3-P100 |
| MCP Apps | V3-P97 |

범용 코딩 일은 Claude Code·Codex 같은 도구가 맡는다. 옛 Workspace/Environment/Binding 설계와 Pause 초안은 기록으로 남는다: [Default Workspace v0.2](default-workspace-files-v0.2.md), [Sandbox Execution v0.1](sandbox-execution-v0.1.md), [Control Pause/Resume v0.1](control-pause-v0.1.md), [CurrentState Contract v0.1](current-state-contract-v0.1.md).

## 10. v2로부터의 단순화

v3는 v2의 계층을 그대로 옮기지 않는다. Run 중심 실행 모델에는 Task/ManagerTurn 계층이 없다. Worktree, Git을 통한 Workspace 권한 정의, Work Apply/Discard, HumanDirection 전용 계층도 쓰지 않는다. v2의 구현·운영·데이터가 v3와 자동으로 호환된다고 가정하지 않는다.

## 11. 호환성과 검증

공개 계약(타입·오류·순서·취소·권한 의미)을 지키는 구현 교체가 다른 모듈의 수정을 요구하지 않게 한다. 계약 테스트, import 의존성 검사, Fake 구현 교체 테스트로 검증한다. pytest는 계약이 바뀔 때만 전체를, 그 밖에는 바뀐 모듈만 돌린다([Pytest 운영 정책](testing-policy.md)).

미해결 사항은 [open-questions.md](open-questions.md)를 따른다.
