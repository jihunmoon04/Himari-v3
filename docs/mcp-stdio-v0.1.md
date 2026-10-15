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
- **import 규칙:** `sandbox`는 `common`만 쓴다(`ONLY_FROM`). Inside는 `sandbox.api`를 쓴다. Bootstrap이 백엔드를 만들어 넘기는 방식은 지금과 같다. **MCP 커넥터는 `sandbox`를 import하지 않는다**(§4.3, V3-059 그대로).
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

### 4.2 운영자가 적은 경로의 검사

**설정을 읽을 때** (실패하면 그 서버만 거부):

- 절대 경로이고, 존재하고, 마지막 구성 요소가 심볼릭 링크가 아니다.
- **HIMARI 데이터 폴더, Workspaces 폴더와 겹치면 거부한다.** 그 안에 있거나 그것들을 안에 품는 경로 모두다. 예를 들어 데이터 폴더가 홈 아래에 있으면 홈 전체를 붙일 수 없다.
- `writable`은 HIMARI 서버 사용자 소유의 폴더여야 한다.
- `command`는 `read_only` 경로 안이나 시스템 경로(`/usr` 등) 안에 있어야 한다. 밖에 있으면 Sandbox 안에서 보이지 않는다.
- 개수 한도: `read_only` 16개, `writable` 4개.

**하드링크 (PR #29 검수 B1).** 경로가 겹치지 않아도, 같은 파일시스템이면 하드링크로 같은 파일에 닿을 수 있다. 그래서 **세션 프로세스를 띄울 때마다**, 붙일 fd를 연 뒤 그 fd 기준으로 다음 두 규칙을 확인한다. 하나라도 어기면 띄우지 않는다("실행 안 됨").

- **R1 (모든 트리):** 붙일 트리 안의 어떤 파일도 HIMARI 데이터 폴더(`himari.db`·운영자 비밀·`mcp.json`·Workspaces 전체) 아래의 파일과 inode를 공유하지 않는다. 계산은 두 쪽에서 링크 수가 2 이상인 파일의 (st_dev, st_ino) 집합을 모아 교집합이 비어 있는지 보는 것이다. 링크 수가 1인 파일은 다른 이름이 없으므로 볼 필요가 없다.
- **R2 (`writable` 트리):** 쓰기 트리의 파일은 트리 밖에 다른 이름을 가지면 안 된다. 트리 안에서 찾은 이름 수가 링크 수와 같아야 한다. Workspace admission(V3-P41)과 같은 계산이고, 이것이 없으면 서버가 쓰기 범위 밖의 파일을 고칠 수 있다.
- **`read_only` 트리의 다른 외부 링크는 허용한다.** 읽기 전용이라 고칠 수 없고, R1이 HIMARI의 것은 이미 막는다. 실제로 Calliope 트리의 파일 5,249개 중 136개가 링크 수 2 이상인데, `uv`가 패키지 캐시에서 venv로 하드링크한 것이다. 이를 막으면 Calliope를 띄울 수 없다. Calliope의 쓰기 폴더(`~/.local/share/calliope`, 파일 152개)에는 링크 수 2 이상인 파일이 없다.
- **한도:** 두 쪽 합쳐 20만 항목(admission의 `MAX_ADMISSION_ENTRIES`)이다. 넘으면 띄우지 않는다.

**보장의 한계.** 검사는 세션을 띄우는 시점의 것이다.

- **Sandbox 안의 서버는 새 링크로 트리를 넘을 수 없다.** `link(2)`는 같은 파일시스템이어도 마운트 지점이 다르면 실패한다(EXDEV). 서버가 볼 수 있는 트리는 각각 별도 bind mount다.
- **Sandbox 밖에서 같은 사용자로 도는 프로세스는 세션 도중에 링크를 만들 수 있다.** 예를 들어 Calliope HTTP 서버는 쓰기 폴더를 같이 쓰고 격리 없이 돈다. 이런 링크는 다음 세션을 띄울 때 잡힌다. 그 사이의 노출을 막으려면 그런 프로세스를 다른 사용자로 돌려야 한다. 이 문서는 그것을 운영 권고로 둔다.
- 심볼릭 링크는 문제가 아니다. Sandbox 안에서는 붙인 트리와 시스템 경로만 보이므로, 밖을 가리키는 심볼릭 링크는 끊긴 링크가 된다.

### 4.3 오래 사는 프로세스 API (`sandbox.api`)와 주입

```python
class IsolatedProcess(Protocol):
    async def write_line(self, data: bytes) -> None          # stdin; 끝났으면 ProcessGone
    async def read_line(self) -> bytes                       # stdout 한 줄 (최대 MAX_LINE_BYTES), EOF면 ProcessGone
    @property
    def returncode(self) -> int | None
    def stderr_tail(self) -> bytes                           # 최근 stderr (최대 64 KiB, 나머지는 버림)
    async def shutdown(self, grace: float) -> None           # stdin 닫기 → 대기 → SIGTERM → 대기 → SIGKILL (프로세스 그룹 전체)

class ProcessLauncher(Protocol):
    async def launch(self, argv, env, cwd) -> IsolatedProcess   # 한 서버 프로파일에 묶인 실행기. ENFORCED가 아니면 SandboxUnavailableError,
                                                                # §4.2 R1·R2를 어기면 SandboxAdmissionError (둘 다 "실행 안 됨")
```

- **주입 (PR #29 검수 C1):** Bootstrap이 서버마다 `sandbox.server_launcher(ServerProfile(read_only, writable, protected=data root))`를 만들어 `mcp.connect(..., launchers={source: launcher})`로 넘긴다. MCP 커넥터는 자기 패키지 안에 같은 모양의 작은 Protocol(`launch`, `write_line`, `read_line`, `returncode`, `stderr_tail`, `shutdown`)을 선언해서 구조적으로 받는다. **`sandbox.api`를 import하지 않으므로 V3-059(Confirmed)는 바뀌지 않는다.** 스키마 검사 주입(V3-059)과 같은 방식이다. 프로파일과 경로 검사는 `sandbox` 모듈의 일이고, MCP는 실행기만 받는다.

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
  "tools": {"definitions": {"effect": "read"}, "commentary": {"effect": "read"}, "show_game": {"effect": "read"},
            "open_game": {"effect": "scoped_write"}, "ply": {"effect": "scoped_write"},
            "judge_move": {"effect": "scoped_write"}, "facts": {"effect": "scoped_write"},
            "explore": {"effect": "scoped_write"}, "submit_commentary": {"effect": "scoped_write"}},
  "ui": {}
}}}
```

- `args`는 64개, 각 4,096자까지다(`run_command`와 같음). `env` 값은 문자열이고 32개까지다.
- `env_from: ["NAME"]`은 HIMARI 환경에서 그 변수만 복사한다. 토큰을 넘길 때 쓰고, `token_env`(HTTP)와 같은 역할이다. 이름이 설정되지 않았으면 거부한다.
- `sessions.idle_seconds`(기본 1800, 60~86400): 이만큼 쓰지 않은 대화의 프로세스는 종료한다.
- `sessions.max`(기본 4, 1~16): 그 서버에 동시에 살아 있을 수 있는 프로세스 수.
- `token_env`는 stdio에 쓸 수 없다. `ui`·`tools`·`default`·`timeout_seconds`는 HTTP와 같다.
- 효과(`effect`)는 지금처럼 운영자가 정한다. **`read`의 뜻을 분명히 한다(PR #29 검수 B2):** `read`는 "다시 실행해도, 실행됐는지 몰라도 아무것도 달라지지 않는다"이다. 여기에는 외부 파일뿐 아니라 **서버의 세션 상태(발급한 id, 예산, 세션 트리)**도 들어간다.
  - 이것이 중요한 이유: Dispatcher는 `read` 호출을 claim하지 않는다. 그래서 요청이 서버에 간 뒤 Stop이 와도 그 호출을 "실행 안 됨"(CANCELLED)으로 보고한다. 그 보고가 해롭지 않은 Tool에만 `read`를 줄 수 있다. 이 정의는 HTTP 서버에도 같다. README의 운영 규칙에도 적는다.
  - Calliope(코드 확인, 2026-10-15)는 아래와 같다. `scoped_write`는 실행 전에 claim되므로, 중간에 끊기면 "결과 불명, 다시 확인할 것"으로 보고되고 다시 실행되지 않는다.

    | Tool | 효과 | 이유 |
    | --- | --- | --- |
    | `definitions`, `commentary` | `read` | 등록부·저장된 해설을 읽기만 함 |
    | `show_game` | `read` | 이미 분석된(캐시된) 수만 보이고 예산을 쓰지 않음 |
    | `open_game` | `scoped_write` | 세션에 게임을 등록하고 id를 발급함. `plies`를 주면 분석하고 예산을 씀 |
    | `ply`, `judge_move` | `scoped_write` | 캐시에 없으면 분석하고 예산을 씀, 분석을 캐시에 씀 |
    | `facts`, `explore` | `scoped_write` | 세션 트리를 키움(노드 예산), `explore`는 탐색 예산을 씀 |
    | `submit_commentary` | `scoped_write` | 부를 때마다 새 해설 파일을 남김 |

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
- **유휴 정리:** `idle_seconds` 동안 진행 중인 호출이 없으면 종료한다(§5.5). 다음 호출은 새 세션이다.
- **세션이 바뀐 뒤 옛 id (PR #29 검수 B4).** stdio에는 세션 정체성이 없어서, 옛 세션에서 받은 id가 새 세션에서 무엇을 가리키는지 HIMARI는 알 수 없다.
  - Calliope(코드 확인)의 인용 핸들은 이미 세션마다 무작위 토큰을 붙여서(`c3.4f2a1b`) 다른 세션의 것을 거절한다. 그런데 **게임 id는 `g1`, `g2`처럼 세션마다 처음부터 다시 센다.** 새 세션에서 연 게임이 옛 대화의 `g1`과 같은 id를 받으면, 모델이 옛 `g1`(다른 게임)에 대해 물어도 새 게임의 분석이 정상처럼 나온다.
  - **Calliope 쪽 변경 (Calliope 소유자 결정 필요):** 게임 id에도 핸들과 같은 세션 토큰을 붙인다(`g1.4f2a1b`). 옛 세션의 게임 id는 "no game … in this session"으로 거절된다. HIMARI가 Calliope를 연결하기 전 조건으로 둔다.
  - **HIMARI 쪽 (모든 stdio 서버):** 대화의 세션이 새로 시작되면(유휴 정리, 상한 교체, 프로세스 종료, 취소 후 종료), 그 세션의 첫 결과 앞에 모델용 안내를 붙인다: "This server started a new session: identifiers from its earlier answers (games, handles) no longer apply; open them again." 서버가 id를 세션마다 고유하게 만들지 않더라도 모델이 알 수 있게 하는 장치다. 그래도 서버 쪽 고유 id를 대신하지는 못한다.
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

**세션 상태:** `STARTING` → `READY` ⇄ `DRAINING` → `CLOSING` → (끝). `READY`일 때만 새 요청을 쓴다.

- **Stop(Cancel)이나 시간 초과로 호출을 기다리지 않게 되면:**
  1. 그 요청 id로 `notifications/cancelled`를 보낸다(사유: "cancelled by the user" 또는 "timed out").
  2. 세션은 **`DRAINING`**이 된다(PR #29 검수 B3).
  3. **10초 안에 그 요청의 응답이 오면** `READY`로 돌아간다. 응답은 사양대로 무시한다.
  4. 안 오면 `CLOSING`이 되고 종료 절차를 밟는다.

  이 기다림은 취소된 handler가 아니라 세션 관리자(pool)의 백그라운드 작업이 한다. Cancel이 Tool 호출을 바로 멈춘다는 V3-057의 의미는 그대로다. 호출의 결과는 V3-057대로다(claim된 호출은 결과 불명).
- **`DRAINING` 중에 같은 대화의 새 호출이 오면 (B3):** 요청을 쓰지 않고 그 세션이 정해질 때까지 기다린다(최대 10초 + 종료 약 4초).
  - `READY`로 돌아가면 그 세션에 쓴다.
  - 종료되면 새 세션을 띄워 거기에 쓴다. 새 세션 안내(§5.3)가 붙는다.
  - 기다리는 동안 Stop이 오면 그 호출은 아직 쓰지 않았으므로 "실행 안 됨"이다.
  - 새 호출이 이전 취소의 종료에 휩쓸리는 일은 없다. 종료할 프로세스에는 더 이상 쓰지 않기 때문이다.
- **프로세스를 종료하면:** 그 프로세스에 쓰고 아직 응답을 받지 못한 요청은 **모두** 결과 불명이다. 취소된 것만이 아니라 같은 대화에서 동시에 보냈던 다른 호출도 포함한다. 그 호출들의 handler는 `McpUncertain`을 받는다.
- **`initialize`는 취소하지 않는다 (C2):** 사양이 금지한다(MUST NOT). 세션을 띄우는 중(`STARTING`)의 시간 초과(15초)나 Stop은 취소 알림 없이 바로 종료 절차로 간다. 이때 기다리던 호출은 아직 쓰지 않았으므로 "실행 안 됨"이다. 목록용 세션(§5.3)도 같다.
- **종료 절차(사양):** stdin을 닫고 2초 기다리고, `SIGTERM`(프로세스 그룹)을 보내고 2초 기다리고, `SIGKILL`을 보낸다. bwrap이 PID namespace를 가지므로 그룹을 죽이면 서버가 띄운 하위 프로세스(Calliope의 Stockfish)도 함께 끝난다.
- **HIMARI가 끝날 때:** 모든 세션에 종료 절차를 동시에 적용한다(합쳐서 최대 약 4초).

### 5.6 화면(V3-P58)과의 관계

화면은 목록용 세션에서 읽은 것을 쓴다(§5.3). 그 밖에 바뀌는 것은 없다. Calliope의 `show_game`은 `ui://calliope/viewer.html` 화면을 가지므로 1단계 표시는 그대로 동작한다. 뷰어의 "해설 새로고침"(화면의 `tools/call`)은 2단계 전까지 거절된다.

## 6. 계약 변경 (Proposed)

| 계약 | 변경 | 비고 |
| --- | --- | --- |
| 새 모듈 `sandbox` | `api.py`: `SandboxState`, `SandboxUnavailableError`, `SandboxSetupError`, `CommandResult`, `WorkspaceProfile`, `ServerProfile`, `ProcessLauncher`, `IsolatedProcess` | `ONLY_FROM["sandbox"] = {"common"}`. 층은 common 바로 위 |
| ENV-04/05 | Sandbox의 소유가 Environment에서 `sandbox` 모듈로 옮겨진다. Inside는 소비자가 된다 | 동작 변경 없음 (1단계) |
| V3-059 | **바뀌지 않는다** (PR #29 검수 C1): Bootstrap이 서버별 실행기를 주입하고, MCP 커넥터는 자기 Protocol로 받는다(§4.3) | 초안의 "MCP가 `sandbox.api`를 import" 안은 철회 |
| MCP 커넥터 | stdio 전송, 대화별 세션 관리자, `mcp.json`의 stdio 키 | 공개 계약 아님 (V3-059) |
| Bootstrap | 서버 프로파일 자체 검사, 서버별 launcher를 만들어 `mcp.connect`에 주입, 종료 시 세션 정리 | |
| 바뀌지 않음 | Data, Control, Prompt, Provider, Tool 계약, Web·CLI API | 세션 상태는 표시 대상이 아니다. 시작 문제는 지금처럼 `SourceStatus.problems`에 보인다 |

## 7. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 운영자가 적은 명령이 HIMARI 데이터·비밀에 닿음 | 서버 프로파일 Sandbox: 데이터 폴더·Workspaces는 마운트 자체가 없고, 겹치는 경로는 설정에서 거부 |
| 하드링크로 HIMARI 파일에 닿거나 쓰기 범위 밖을 고침 | 세션마다 R1(데이터 폴더와 inode 공유 금지)·R2(쓰기 트리의 외부 링크 금지). 서버는 새 교차 링크를 만들 수 없음(EXDEV). 같은 사용자의 격리 안 된 프로세스가 만드는 링크는 다음 세션에서 잡힘(§4.2 한계) |
| 세션이 바뀐 뒤 옛 id가 다른 대상을 가리킴 | 서버가 id를 세션마다 고유하게(Calliope 변경), HIMARI는 새 세션 안내를 모델에게 |
| 취소 정리가 새 호출을 함께 끝냄 | `DRAINING` 중에는 새 요청을 쓰지 않음 |
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
  - 하드링크: 데이터 폴더 파일의 링크를 `read_only` 트리에 두면 거부(R1). `writable` 트리 파일의 바깥 링크도 거부(R2). `read_only` 트리의 무해한 외부 링크(uv 캐시 같은 것)는 허용. Sandbox 안에서 트리를 넘는 `link`는 EXDEV. 20만 항목을 넘으면 거부.
- **3단계 (stdio):**
  - 가짜 stdio MCP 서버(Python 스크립트)로 다음을 확인한다:
    - 한 줄 메시지, 응답 짝 맞추기, 알림 무시, `ping` 응답
    - 비 JSON 줄과 한도 초과 줄은 종료와 결과 불명
    - §5.4 표의 모든 경우
    - 대화별 세션 분리, 유휴 정리, 상한, 재시작 지연
    - 취소 후 10초 안에 응답이 오면 유지, 안 오면 종료
    - `DRAINING` 중의 새 호출은 쓰지 않고 기다림. 유지되면 같은 세션, 종료되면 새 세션과 안내
    - 종료 시 같은 프로세스의 다른 미응답 요청도 결과 불명
    - `initialize` 중 시간 초과와 Stop에는 취소 알림 없이 종료하고, 기다리던 호출은 실행 안 됨
    - 새 세션의 첫 결과에 안내가 붙음
    - 목록용 세션, 화면 읽기
  - **Calliope 실제 연결(호스트):** 위 설정으로 `open_game` → `ply` → `show_game`(화면) → `submit_commentary`. 확인할 것:
    - 두 대화의 예산이 따로인지
    - 데이터 폴더를 Calliope HTTP 서버와 같이 쓸 수 있는지(파일 잠금)
    - 하드링크가 있는 venv가 R1·R2를 통과하는지
    - 세션을 바꾼 뒤 옛 게임 id가 거절되는지(Calliope 변경 후)

## 9. 남은 질문

1. **Calliope 변경 (소유자 결정):** 게임 id에 세션 토큰을 붙인다(§5.3, B4). HIMARI가 Calliope를 연결하기 전 조건이다.
2. **네트워크가 필요한 stdio 서버**: V3-037(Confirmed) 개정과 "연결 허용" 프로파일. 이번 범위 밖이다.
3. **자원 제한**(Q-40): cgroup이나 rlimit. 프로세스가 오래 살게 되면서 중요도가 올라간다.
4. **운영 권고**(§4.2 한계): 쓰기 폴더를 같이 쓰는, 격리되지 않은 프로세스(Calliope HTTP 서버 등)를 다른 사용자로 돌릴지.
5. **Calliope 쪽 관찰**(HIMARI가 아니라 Calliope가 정할 것):
   - Calliope §9의 임시 파일 정리는 PID로 프로세스 생존을 판단한다. Sandbox 안의 PID는 별도 namespace라 바깥 HTTP 서버가 볼 때 다른 프로세스의 PID로 보일 수 있다. 그러면 크래시로 남은 임시 파일이 지워지지 않을 수 있다(해롭지는 않음).
   - Calliope HTTP 서버의 예시 포트 8765는 HIMARI 기본 포트와 겹친다.
