# MCP stdio 지원과 Sandbox 모듈 분리 v0.1

> 2026-10-15 · **PROPOSED (V3-P59)** · 설계 초안이며 구현 전이다.
>
> 기준 사양: MCP 2025-11-25 [Transports §stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [Lifecycle §Shutdown·Timeouts](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle), [Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation).
>
> 이미 정한 것(2026-10-15 사용자 결정):
> - stdio 서버 프로세스는 **대화당 하나**다.
> - Sandbox를 쓸 수 없으면 **띄우지 않는다**.
> - 실행 중 Stop은 **취소 알림 후, 안 끝나면 프로세스를 종료**한다.
>
> 첫 대상: [Calliope](https://github.com/jihunmoon04/Calliope)의 MCP 서버(`python -m tools.mcp`, stdio 전용, AG-D 결정 O1).

## 1. 왜, 그리고 범위

MCP의 표준 전송은 stdio와 Streamable HTTP 두 가지이고, 사양은 "클라이언트는 가능하면 stdio를 지원해야 한다(SHOULD)"고 한다. 로컬 MCP 서버 대부분은 stdio로 배포되고, Calliope도 stdio만 연다. HIMARI는 지금 HTTP만 지원한다(V3-055).

stdio에서는 **클라이언트가 서버를 자기 자식 프로세스로 띄운다.** 따라서 HIMARI가 운영자가 적은 명령을 실행하게 된다. 이 설계의 중심은 그 프로세스를 HIMARI의 데이터와 비밀에서 떼어 놓는 것이다.

| 포함 | 제외 |
| --- | --- |
| Sandbox를 독립 모듈 `himari/sandbox/`로 분리, 프로파일로 정책을 나눔 | 네트워크가 필요한 stdio 서버 (V3-037 개정 필요, 별도 결정) |
| 오래 사는 격리 프로세스(stdin/stdout 파이프) | cgroup·rlimit 자원 제한 (Q-40, 그대로 미결) |
| stdio 전송, 대화당 세션, 유휴 정리, 취소·종료 | MCP 서버의 안내문·프롬프트·리소스를 모델 문맥에 넣기 (별도 V3-P60) |
| `mcp.json`의 stdio 항목, Calliope 설정 예 | MCP Apps 2단계 (화면의 Tool 호출) |

## 2. 사양에서 가져오는 규칙

- 클라이언트가 서버를 자식 프로세스로 띄운다. 서버는 stdin으로 받고 stdout으로 보낸다.
- 메시지는 JSON-RPC 요청·알림·응답 하나씩이고, **줄바꿈으로 구분하며 메시지 안에 줄바꿈이 있으면 안 된다(MUST NOT).**
- 서버는 stdout에 MCP 메시지가 아닌 것을 쓰면 안 된다(MUST NOT). 클라이언트도 stdin에 MCP 메시지가 아닌 것을 쓰면 안 된다.
- 서버는 stderr에 로그를 쓸 수 있다(MAY). 클라이언트는 그것을 오류로 여기면 안 된다(SHOULD NOT).
- **종료:** 클라이언트는 ① stdin을 닫고 ② 서버가 끝나길 기다리거나, 적당한 시간 뒤 `SIGTERM`을 보내고 ③ 그래도 안 끝나면 `SIGKILL`을 보낸다(SHOULD).
- **시간 제한:** 요청마다 시간 제한을 두고, 넘으면 취소 알림을 보내고 기다림을 멈춘다(SHOULD).
- **취소:** `notifications/cancelled {requestId, reason}`. 받는 쪽은 무시할 수 있고(MAY), 보낸 쪽은 그 뒤에 오는 응답을 무시한다(SHOULD).
- `initialize` 요청은 취소하면 안 된다(MUST NOT).

## 3. Sandbox 모듈 분리 (1단계, 동작 변경 없음)

### 3.1 지금의 모양

Sandbox(`environment/inside/sandbox/`, V3-P41)는 Inside의 `run_command` 한 가지 용도에 맞춰져 있다.

- **정책이 코드에 박혀 있다:** 쓸 수 있는 곳은 `/workspace`와 `/tmp`이고, 작업 위치는 `/workspace` 아래여야 하며, Workspace를 `--bind-fd`로 붙인다.
- **한 번 실행하고 끝난다:** stdin은 항상 `/dev/null`이고, 끝날 때까지 기다린 뒤 출력을 모은다(최대 600초, 256 KiB). 프로세스나 스트림을 밖으로 내주는 API가 없다.
- **시작 시 자체 검사가 Workspace 모양만 증명한다:** 작업 위치, 쓰기 가능 위치, 마운트 목록의 기대값이 Workspace 기준이다.

### 3.2 새 모양

```
src/himari/sandbox/            새 최상위 모듈 (층: common 바로 위)
  api.py                       SandboxState, 오류, CommandResult, 프로파일, 실행 Protocol
  backend.py · landlock_helper.py · self_test.py   (environment/inside/sandbox/에서 옮김)
```

- **격리 장치는 하나, 정책은 프로파일로 나눈다.** 1단계의 프로파일은 지금의 Workspace 하나뿐이다. 2단계에서 서버 프로파일(§4)을 더한다.
- **import 규칙:** `sandbox`는 `common`만 쓴다(`ONLY_FROM`). Inside는 `sandbox.api`를 쓴다. Bootstrap이 백엔드를 만들어 넘기는 방식은 지금과 같다.
- **바뀌지 않는 것:** `run_command`의 동작과 한도(V3-051), 네 가지 상태, "검사를 통과해야 실행하고 대체 실행은 없다"(V3-050), 기존 단위·호스트 테스트. 테스트는 import 경로만 바뀐다.
- `api.py`는 자기 모듈의 구현을 import할 수 없다(규칙 3). 그래서 상태·오류·결과·프로파일 같은 값 타입과 Protocol을 `api.py`에 선언하고, 구현은 그것을 따른다.

## 4. 서버 프로파일과 오래 사는 프로세스 (2단계)

### 4.1 서버 프로파일이 보이는 세상

| | Workspace 프로파일 (`run_command`) | **서버 프로파일 (stdio MCP)** |
| --- | --- | --- |
| 쓸 수 있는 곳 | 사용자 Workspace(`/workspace`), `/tmp` | 운영자가 적은 `writable` 폴더들, `/tmp` |
| 읽기만 되는 곳 | 시스템(`/usr` 등) | 시스템 + 운영자가 적은 `read_only` 경로들 |
| 경로 | Workspace를 `/workspace`에 붙임 | **호스트와 같은 경로에 붙임**: 서버 설정에 적힌 경로가 그대로 통한다 |
| 네트워크 | 없음 | **없음** (V3-037 그대로) |
| 환경 변수 | 고정 5개 | 고정 5개 + 운영자가 적은 것만. HIMARI의 환경 변수는 넘기지 않는다 |
| stdin | `/dev/null` | **파이프** |
| 수명 | 한 번 실행 후 종료(최대 600초) | **계속** (종료 절차 §5.5) |

- **붙이는 방법:** 경로마다 HIMARI가 먼저 열어 확인한 fd로 붙인다(`--ro-bind-fd`, `--bind-fd`). 이름을 다시 해석하지 않으므로, 확인과 붙이기 사이에 경로가 바뀌어도 소용없다. Workspace와 같은 원칙이다.
- **Landlock:** 읽기는 `/`(실제로는 bwrap이 보여 주는 것만), 쓰기는 `writable`과 `/tmp`, 네트워크(TCP bind·connect)는 막는다. 같은 helper를 쓴다.
- **환경 변수:** `HOME=/tmp/home`이다. 서버가 `~` 아래의 기본 경로에 기대면 안 되고, 경로는 인자나 환경 변수로 줘야 한다. Calliope는 `--data`로 준다.

### 4.2 운영자가 적은 경로의 검사 (시작 전에, 실패하면 그 서버만 거부)

- 절대 경로이고, 존재하고, 마지막 구성 요소가 심볼릭 링크가 아니다.
- **HIMARI 데이터 폴더, Workspaces 폴더와 겹치면 거부한다.** 그 안에 있거나 그것들을 안에 품는 경로 모두다. 예를 들어 데이터 폴더가 홈 아래에 있으면 홈 전체를 붙일 수 없다.
- `writable`은 HIMARI 서버 사용자 소유의 폴더여야 한다.
- `command`는 `read_only` 경로 안이나 시스템 경로(`/usr` 등) 안에 있어야 한다. 밖에 있으면 Sandbox 안에서 보이지 않는다.
- 개수 한도: `read_only` 16개, `writable` 4개.

### 4.3 오래 사는 프로세스 API (`sandbox.api`)

```python
class IsolatedProcess(Protocol):
    async def write_line(self, data: bytes) -> None          # stdin; 끝났으면 ProcessGone
    async def read_line(self) -> bytes                       # stdout 한 줄 (최대 MAX_LINE_BYTES), EOF면 ProcessGone
    @property
    def returncode(self) -> int | None
    def stderr_tail(self) -> bytes                           # 최근 stderr (최대 64 KiB, 나머지는 버림)
    async def shutdown(self, grace: float) -> None           # stdin 닫기 → 대기 → SIGTERM → 대기 → SIGKILL (프로세스 그룹 전체)

class ProcessLauncher(Protocol):
    async def launch(self, profile: ServerProfile, argv, env, cwd) -> IsolatedProcess   # ENFORCED가 아니면 SandboxUnavailableError
```

- 실행 순서는 지금과 같다: Landlock을 적용한 뒤 sync fd로 신호를 보내고, 그다음 exec한다. 신호가 오지 않으면 실행되지 않은 것이다(`SandboxSetupError`).
- stdout은 줄 단위로 읽는다. 한 줄이 `MAX_LINE_BYTES`(4 MiB, HTTP 응답 한도와 같음)를 넘으면 그 프로세스는 망가진 것으로 보고 종료한다.
- stderr는 계속 비우고 마지막 64 KiB만 남긴다. 서버가 stderr를 너무 많이 써서 막히는 일이 없다.
- **자원 제한은 아직 없다**(Q-40). 대신 프로세스 수 상한(§5.3)과 유휴 정리로 묶는다.

### 4.4 서버 프로파일의 자체 검사

시작할 때, stdio 서버가 하나라도 설정돼 있으면 서버 프로파일로도 실제 프로세스를 띄워 확인한다(V3-050과 같은 원칙). 이 검사는 Workspace 프로파일의 검사와 따로 상태를 가진다. 서버 프로파일이 ENFORCED가 아니면 stdio 서버는 하나도 띄우지 않는다(사용자 결정). `run_command`에는 영향이 없다.

확인하는 것:
- 임시 `read_only` 폴더는 읽히고 써지지 않는다. 임시 `writable` 폴더는 써진다.
- 데이터 폴더·Workspaces·보호 경로는 보이지 않는다.
- 네트워크(외부·loopback TCP/UDP, 호스트 Unix 소켓)가 막혀 있다.
- 환경 변수는 정해진 것만 있다.
- stdin으로 쓴 줄이 stdout으로 돌아온다(파이프 왕복).
- 마운트 목록이 기대와 정확히 같다.

## 5. stdio 전송 (3단계)

### 5.1 `mcp.json`

한 서버는 `url`(HTTP)이나 `command`(stdio) 중 하나만 가진다. 둘 다 있거나 둘 다 없으면 거부한다.

```json
{"servers": {"calliope": {
  "command": "/home/coder/Calliope/.venv/bin/python",
  "args": ["-m", "tools.mcp", "--data", "/home/coder/.local/share/calliope", "--viewer", "/home/coder/Calliope/viewer"],
  "cwd": "/home/coder/Calliope",
  "env": {"CALLIOPE_STOCKFISH_PATH": "/home/coder/opt/stockfish/stockfish", "PYTHONPATH": "."},
  "read_only": ["/home/coder/Calliope", "/home/coder/opt/stockfish"],
  "writable": ["/home/coder/.local/share/calliope"],
  "sessions": {"idle_seconds": 1800, "max": 4},
  "timeout_seconds": 300,
  "tools": {"open_game": {"effect": "read"}, "ply": {"effect": "read"}, "judge_move": {"effect": "read"},
            "facts": {"effect": "read"}, "explore": {"effect": "read"}, "definitions": {"effect": "read"},
            "commentary": {"effect": "read"}, "show_game": {"effect": "read"},
            "submit_commentary": {"effect": "scoped_write"}},
  "ui": {}
}}}
```

- `args`는 64개, 각 4,096자까지다(`run_command`와 같음). `env` 값은 문자열이고 32개까지다.
- `env_from: ["NAME"]`은 HIMARI 환경에서 그 변수만 복사한다. 토큰을 넘길 때 쓰고, `token_env`(HTTP)와 같은 역할이다. 이름이 설정되지 않았으면 거부한다.
- `sessions.idle_seconds`(기본 1800, 60~86400): 이만큼 쓰지 않은 대화의 프로세스는 종료한다.
- `sessions.max`(기본 4, 1~16): 그 서버에 동시에 살아 있을 수 있는 프로세스 수.
- `token_env`는 stdio에 쓸 수 없다. `ui`·`tools`·`default`·`timeout_seconds`는 HTTP와 같다.
- 효과(`effect`)는 지금처럼 운영자가 정한다. Calliope의 `ply`는 분석해서 캐시에 쓰지만 같은 결과를 다시 쓸 뿐이고, 예산은 세션 안의 일이라 `read`로 둔다. `submit_commentary`는 부를 때마다 새 해설을 남기므로 `scoped_write`다.

### 5.2 클라이언트

`McpClient`의 JSON-RPC 처리(요청 id, 응답 짝 맞추기, 결과 확실성 분류)는 그대로 두고, 전송만 둘로 나눈다(`HttpTransport`, `StdioTransport`).

- 보낼 때는 `json.dumps`를 한 줄로 쓰고 `\n`을 붙인다. JSON 직렬화는 문자열 안의 줄바꿈을 `\n`으로 이스케이프하므로 메시지 안에 실제 줄바꿈이 생기지 않는다.
- 받을 때는 한 줄씩 JSON으로 읽는다.
  - 응답은 id로 짝을 맞춘다.
  - 서버의 알림(진행 상황, 로그 등)은 무시한다.
  - 서버의 요청 중 `ping`에는 `{}`로 답한다. HIMARI는 capability를 선언하지 않으므로 다른 요청은 `-32601`로 답한다.
- JSON이 아닌 줄, 한도를 넘는 줄, JSON-RPC가 아닌 메시지는 사양 위반이다(서버는 stdout에 MCP 메시지만 써야 한다). 그 프로세스를 종료하고, 진행 중인 요청은 결과 불명으로 처리한다.
- 세션 id와 `MCP-Protocol-Version` 헤더는 없다(HTTP 전용). 프로토콜 버전 협상은 `initialize`에서 지금처럼 한다.

### 5.3 대화당 세션 (사용자 결정)

- **키는 (서버, 대화)다.** 대화에서 그 서버의 Tool을 처음 부를 때 프로세스를 띄우고 `initialize`를 한다. 그 뒤로는 같은 프로세스를 쓴다.
  - Calliope 기준으로 대화마다 예산(분석 16수, 탐색 40회)과 게임 핸들이 따로다.
  - 같은 대화의 여러 호출은 한 프로세스로 동시에 보낸다(JSON-RPC는 id로 짝을 맞춘다). 서버가 하나씩 처리하는 것은 서버의 일이다.
- **유휴 정리:** `idle_seconds` 동안 진행 중인 호출이 없으면 종료한다(§5.5). 다음 호출은 새 세션이라 Calliope의 핸들은 사라진다. 모델이 쓴 옛 핸들은 서버가 Tool 오류로 거절하고, 모델은 게임을 다시 연다.
- **상한:** 살아 있는 프로세스가 `max`개면 가장 오래 쉰 것을 종료하고 띄운다. 모두 호출 중이면 그 호출을 "실행하지 않음"으로 거절한다(`FAILED`, "this server has no free session; try again").
- **프로세스가 죽으면:** 진행 중이던 호출은 결과 불명이다(§5.4). 다음 호출이 새로 띄운다. 연속으로 실패하면 띄우기를 늦춘다(1·2·4초…, 최대 60초).
- **목록과 화면은 따로 읽는다:** HIMARI가 시작할 때 서버마다 **목록용 세션**을 한 번 띄워 `initialize`·`tools/list`·화면 읽기(`resources/read`, V3-P58)를 하고 종료한다. Tool 정의와 화면은 이것으로 정한다. 대화 세션의 목록이 달라도 다시 읽지 않는다(HTTP와 같이 "다시 연결은 재시작").
- **기록하지 않는다:** 세션은 프로세스 안에만 있다. HIMARI를 재시작하면 모든 세션이 새로 시작된다. Data는 바뀌지 않는다.

### 5.4 결과의 확실성 (V3-055의 stdio판)

| 언제 | 판정 |
| --- | --- |
| 프로세스를 띄우지 못함, Sandbox가 거부, `initialize` 실패, 상한 초과 — **요청을 쓰기 전** | 실행 안 됨 (`McpRefused` → `FAILED`) |
| JSON-RPC parse / invalid request / method not found / invalid params 오류 | 실행 안 됨 |
| 쓰기 직전에 이미 죽어 있음 (returncode 있음) | 실행 안 됨 |
| 요청을 쓴 뒤: 프로세스 종료, 시간 초과, 사양 위반 출력, 취소, 그 밖의 JSON-RPC 오류 | **결과 불명** (`McpUncertain` → claim된 호출은 UNKNOWN, 재실행 안 함, V3-047) |
| 쓰다가 실패(BrokenPipe)했는데 직전까지 살아 있었음 | 결과 불명 (일부를 읽었을 수 있음) |
| `isError: true` | 서버가 실행해서 실패한 것 (`FAILED`, 지금과 같음) |

### 5.5 취소와 종료 (사용자 결정)

- **Stop(Cancel)이나 시간 초과로 호출을 기다리지 않게 되면:**
  1. 그 요청 id로 `notifications/cancelled`를 보낸다(사유: "cancelled by the user" 또는 "timed out").
  2. **10초 안에 그 요청의 응답이 오면** 프로세스는 그대로 둔다. 응답은 사양대로 무시한다.
  3. 안 오면 그 대화의 프로세스를 종료한다. 다음 호출은 새 세션이다.

  이 기다림은 취소된 handler가 아니라 세션 관리자(pool)의 백그라운드 작업이 한다. Cancel이 Tool 호출을 바로 멈춘다는 V3-057의 의미는 그대로다. 호출의 결과는 V3-057대로다(claim된 호출은 결과 불명).
- **종료 절차(사양):** stdin을 닫고 2초 기다리고, `SIGTERM`(프로세스 그룹)을 보내고 2초 기다리고, `SIGKILL`을 보낸다. bwrap이 PID namespace를 가지므로 그룹을 죽이면 서버가 띄운 하위 프로세스(Calliope의 Stockfish)도 함께 끝난다.
- **HIMARI가 끝날 때:** 모든 세션에 종료 절차를 동시에 적용한다(합쳐서 최대 약 4초).

### 5.6 화면(V3-P58)과의 관계

화면은 목록용 세션에서 읽은 것을 쓴다(§5.3). 그 밖에 바뀌는 것은 없다. Calliope의 `show_game`은 `ui://calliope/viewer.html` 화면을 가지므로 1단계 표시는 그대로 동작한다. 뷰어의 "해설 새로고침"(화면의 `tools/call`)은 2단계 전까지 거절된다.

## 6. 계약 변경 (Proposed)

| 계약 | 변경 | 비고 |
| --- | --- | --- |
| 새 모듈 `sandbox` | `api.py`: `SandboxState`, `SandboxUnavailableError`, `SandboxSetupError`, `CommandResult`, `WorkspaceProfile`, `ServerProfile`, `ProcessLauncher`, `IsolatedProcess` | `ONLY_FROM["sandbox"] = {"common"}`. 층은 common 바로 위 |
| ENV-04/05 | Sandbox의 소유가 Environment에서 `sandbox` 모듈로 옮겨진다. Inside는 소비자가 된다 | 동작 변경 없음 (1단계) |
| **V3-059 개정 (Confirmed)** | MCP 커넥터가 `sandbox.api`도 import할 수 있게 한다(`ONLY_FROM["mcp"] = {"common", "tool", "sandbox"}`) | V3-059가 "허용 import를 넓히려면 이 결정을 개정한다"고 정했다. **사용자 승인이 필요하다** |
| MCP 커넥터 | stdio 전송, 대화별 세션 관리자, `mcp.json`의 stdio 키 | 공개 계약 아님 (V3-059) |
| Bootstrap | 서버 프로파일 자체 검사, launcher 주입, 종료 시 세션 정리 | |
| 바뀌지 않음 | Data, Control, Prompt, Provider, Tool 계약, Web·CLI API | 세션 상태는 표시 대상이 아니다. 시작 문제는 지금처럼 `SourceStatus.problems`에 보인다 |

## 7. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 운영자가 적은 명령이 HIMARI 데이터·비밀에 닿음 | 서버 프로파일 Sandbox: 데이터 폴더·Workspaces는 마운트 자체가 없고, 겹치는 경로는 설정에서 거부 |
| HIMARI의 API 키·토큰이 서버로 샘 | 환경 변수는 정해진 것과 운영자가 적은 것만 (`env_from`은 이름을 콕 집어 복사) |
| 서버가 네트워크로 무언가를 보냄 | 네트워크 없음 (V3-037). 필요한 서버는 이 설계로는 쓸 수 없다 |
| 한 대화의 세션 상태가 다른 대화로 샘 | 대화당 프로세스 |
| 서버가 멈추거나 출력을 쏟아냄 | 시간 제한·줄 한도·stderr 링버퍼, 취소 후 10초면 종료 |
| 프로세스가 쌓임 | 서버당 `max`, 유휴 정리, 종료 시 프로세스 그룹 전체 종료 |
| 메모리·CPU 과다 사용 | **대응 없음 (Q-40).** 프로세스 수로만 묶는다 |
| Sandbox가 없는 호스트 | stdio 서버를 띄우지 않음 (사용자 결정) |

## 8. 검증 계획

- **1단계 (분리):** 기존 단위·호스트 테스트를 import 경로만 바꿔 그대로 통과한다. import 검사에 `sandbox` 규칙을 추가한다. 전체 pytest를 돌린다(계약 위치가 바뀌므로).
- **2단계 (서버 프로파일):**
  - 단위: bwrap 인자(같은 경로 fd 붙이기, 네트워크 없음, `--bind`류 없음), 경로 검사(데이터 폴더와 겹침·심볼릭 링크·소유자), 종료 절차 순서(가짜 프로세스).
  - 호스트(`HIMARI_SANDBOX_HOST=1`): §4.4의 모든 항목, 그리고 하위 프로세스까지 종료되는지.
- **3단계 (stdio):**
  - 가짜 stdio MCP 서버(Python 스크립트)로 다음을 확인한다:
    - 한 줄 메시지, 응답 짝 맞추기, 알림 무시, `ping` 응답
    - 비 JSON 줄과 한도 초과 줄은 종료와 결과 불명
    - §5.4 표의 모든 경우
    - 대화별 세션 분리, 유휴 정리, 상한, 재시작 지연
    - 취소 후 10초 안에 응답이 오면 유지, 안 오면 종료
    - 목록용 세션, 화면 읽기
  - **Calliope 실제 연결(호스트):** 위 설정으로 `open_game` → `ply` → `show_game`(화면) → `submit_commentary`. 두 대화의 예산이 따로인지, 데이터 폴더를 Calliope HTTP 서버와 같이 쓸 수 있는지(파일 잠금) 확인한다.

## 9. 남은 질문

1. **V3-059 개정 승인**(§6): MCP 커넥터가 `sandbox.api`를 import한다.
2. **네트워크가 필요한 stdio 서버**: V3-037(Confirmed) 개정과 "연결 허용" 프로파일. 이번 범위 밖이다.
3. **자원 제한**(Q-40): cgroup이나 rlimit. 프로세스가 오래 살게 되면서 중요도가 올라간다.
4. **Calliope 쪽 관찰**(HIMARI가 아니라 Calliope가 정할 것):
   - Calliope §9의 임시 파일 정리는 PID로 프로세스 생존을 판단한다. Sandbox 안의 PID는 별도 namespace라 바깥 HTTP 서버가 볼 때 다른 프로세스의 PID로 보일 수 있다. 그러면 크래시로 남은 임시 파일이 지워지지 않을 수 있다(해롭지는 않음).
   - Calliope HTTP 서버의 예시 포트 8765는 HIMARI 기본 포트와 겹친다.
