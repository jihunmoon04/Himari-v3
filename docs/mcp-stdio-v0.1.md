# MCP stdio 지원과 Sandbox 모듈 분리 v0.1

> 2026-10-15 · **PROPOSED (V3-P59)** · 1단계(Sandbox 모듈 분리, PR #30), 2단계(서버 프로파일, PR #32), 3단계(클라이언트 세 층 PR #33, stdio 전송 PR #34) 구현됨. 구현 노트는 §10.
>
> **2026-10-16 개정 (PR #35; 사용자: 표준 방식과 범용성을 우선해 이전 결정을 고쳐도 된다). stdio 구현됨(§10.4), HTTP 취소 알림 구현됨(§10.5):** Stop은 MCP 표준대로 **최선 노력 취소**다. HTTP와 stdio에 같게 적용한다. stdio 프로세스는 취소 뒤 **`ping`에 답하지 않을 때만** 종료한다. stdin 쓰기는 하나의 순서 있는 큐로만 한다(§5.5, 이유와 근거는 §11).
>
> 기준 사양: MCP 2025-11-25 [Transports §stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [Lifecycle §Shutdown·Timeouts](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle), [Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation), [Ping](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/ping).
>
> 이미 정한 것(2026-10-15 사용자 결정):
> - 프로세스 단위는 **기본 서버당 하나**이고, **서버마다 운영자가 "대화별"을 고를 수 있다.** 처음에는 "대화당 하나"로 정했다가, "보편적이고 확장 가능하게"라는 목표에 따라 바꿨다(§5.3).
> - Sandbox를 쓸 수 없으면 **띄우지 않는다**.
> - ~~실행 중 Stop은 **취소 알림 후, 안 끝나면 프로세스를 종료**한다.~~ **2026-10-16 개정(사용자: 표준·범용 우선):** Stop은 취소 알림을 보내고 기다리기를 멈추는 최선 노력 취소다. 서버가 작업을 멈췄는지는 보장하지 않는다. 프로세스는 서버가 `ping`에 답하지 않을 때만 종료한다(§5.5·§11).
> - 서버가 세션에 기대지 않게 하는 쪽으로 간다. 첫 대상인 **Calliope도 그렇게 고친다**(§5.7, 소유자 허용).
>
> **방향 — MCP는 상태 없는 프로토콜로 가고 있다.** 공식 최신판 [2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/changelog)은 `initialize` 핸드셰이크와 프로토콜 세션을 없앴다. 여러 호출에 걸친 상태는 "서버가 발급한 핸들을 보통 Tool 인자로 주고받는 것"(SEP-2567)으로 다룬다. 이 설계는 그 방향을 따른다.
> - HIMARI는 서버가 연결 단위로 상태를 가진다고 가정하지 않는다.
> - 프로세스 단위는 호스트 내부의 일로 다룬다.
> - 클라이언트는 전송(HTTP/stdio)과 프로토콜 판(2025-11-25/2026-07-28)을 나눠 둔다. 그래서 최신 판 지원(V3-P61 예정)을 나중에 끼울 수 있다.
>
> 첫 대상: [Calliope](https://github.com/jihunmoon04/Calliope)의 MCP 서버(`python -m tools.mcp`, stdio 전용, AG-D 결정 O1).

## 1. 왜, 그리고 범위

MCP의 표준 전송은 stdio와 Streamable HTTP 두 가지이고, 사양은 "클라이언트는 가능하면 stdio를 지원해야 한다(SHOULD)"고 한다. 로컬 MCP 서버 대부분은 stdio로 배포되고, Calliope도 stdio만 연다. HIMARI는 지금 HTTP만 지원한다(V3-055).

stdio에서는 **클라이언트가 서버를 자기 자식 프로세스로 띄운다.** 따라서 HIMARI가 운영자가 적은 명령을 실행하게 된다. 이 설계의 중심은 그 프로세스를 HIMARI의 데이터와 비밀에서 떼어 놓는 것이다.

| 포함 | 제외 |
| --- | --- |
| Sandbox를 독립 모듈 `himari/sandbox/`로 분리, 프로파일로 정책을 나눔 | 네트워크가 필요한 stdio 서버 (V3-037 개정 필요, 별도 결정) |
| 오래 사는 격리 프로세스(stdin/stdout 파이프) | cgroup·rlimit 자원 제한 (Q-40, 그대로 미결) |
| stdio 전송, 프로세스 단위(서버당/대화별), 유휴 정리, 취소·종료 | MCP 서버의 안내문·프롬프트·리소스를 모델 문맥에 넣기 (별도 V3-P60) |
| 전송과 프로토콜 판을 나눈 클라이언트 구조 | 2026-07-28판 자체의 지원 (별도 V3-P61) |
| `mcp.json`의 stdio 항목, Calliope 설정 예 | MCP Apps 2단계 (화면의 Tool 호출) |

## 2. 사양에서 가져오는 규칙

- 클라이언트가 서버를 자식 프로세스로 띄운다. 서버는 stdin으로 받고 stdout으로 보낸다.
- 메시지는 JSON-RPC 요청·알림·응답 하나씩이고, **줄바꿈으로 구분하며 메시지 안에 줄바꿈이 있으면 안 된다(MUST NOT).**
- 서버는 stdout에 MCP 메시지가 아닌 것을 쓰면 안 된다(MUST NOT). 클라이언트도 stdin에 MCP 메시지가 아닌 것을 쓰면 안 된다.
- 서버는 stderr에 로그를 쓸 수 있다(MAY). 클라이언트는 그것을 오류로 여기면 안 된다(SHOULD NOT).
- **종료:** 클라이언트는 ① stdin을 닫고 ② 서버가 끝나길 기다리거나, 적당한 시간 뒤 `SIGTERM`을 보내고 ③ 그래도 안 끝나면 `SIGKILL`을 보낸다(SHOULD).
- **시간 제한:** 요청마다 시간 제한을 두고, 넘으면 취소 알림을 보내고 기다림을 멈춘다(SHOULD).
- **취소:** `notifications/cancelled {requestId, reason}`. 받는 쪽은 처리를 멈추고 **취소된 요청에 응답하지 않아야** 하며(SHOULD), 무시할 수도 있다(MAY). 보낸 쪽은 그 뒤에 오는 응답을 무시한다(SHOULD).
- **ping:** 받는 쪽은 빈 결과로 **즉시 답해야** 한다(MUST). 시간 안에 답이 없으면 보낸 쪽은 연결이 죽은 것으로 보고 끊을 수 있다(MAY).
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

- 절대 경로이고, 존재하는 **디렉터리**다. 파일 하나만 붙일 수는 없다. 파일이 필요하면 그 파일이 든 디렉터리를 붙인다(Calliope의 Stockfish는 `/home/coder/opt/stockfish`).
- **HIMARI 데이터 폴더, Workspaces 폴더와 문자열로 겹치면 거부한다.** 그 안에 있거나 그것들을 안에 품는 경로 모두다. 예를 들어 데이터 폴더가 홈 아래에 있으면 홈 전체를 붙일 수 없다. 이것은 **설정 실수를 일찍 알려 주는 검사일 뿐**이다. 보안 보장은 아래의 정체성 검사가 한다(심볼릭 링크나 bind mount 별칭은 문자열 비교로 잡히지 않는다).
- `writable`은 HIMARI 서버 사용자 소유의 폴더여야 한다.
- `command`는 `read_only` 경로 안이나 시스템 경로(`/usr` 등) 안에 있어야 한다. 밖에 있으면 Sandbox 안에서 보이지 않는다.
- 개수 한도: `read_only` 16개, `writable` 4개.

**정체성 검사 (PR #29 검수 B1·B5).** 경로 문자열이 겹치지 않아도 같은 파일·디렉터리에 닿는 길이 세 가지 있다.
- **하드링크**
- **조상 경로의 심볼릭 링크**: 예를 들어 `/tmp/alias -> /srv/himari`이면 `/tmp/alias/control`
- **bind mount 별칭**: 디렉터리를, 또는 파일 하나를 다른 곳에 다시 마운트한 것

그래서 **세션 프로세스를 띄울 때마다**, 실제로 붙일 fd를 연 뒤 그 fd가 가리키는 대상의 정체성 (st_dev, st_ino)으로 아래 규칙을 확인한다. 경로가 어떻게 적혔든(심볼릭 링크를 거치든) 이 검사는 실제 대상을 본다. 하나라도 어기면 띄우지 않는다("실행 안 됨").

- **보호 집합 P:** HIMARI 데이터 폴더(`himari.db`·운영자 비밀·`mcp.json`·Workspaces 전체) 아래의 **모든 디렉터리**(루트 포함)와, **링크 수가 2 이상인 모든 파일**의 정체성. 띄울 때마다 새로 모은다.
- **M0 (하위 마운트 없음):** 붙일 트리 안에 다른 마운트 지점이 있으면 거부한다. 마운트 정보(mountinfo)와 st_dev 비교로 찾고, Workspace admission(V3-P41)과 같은 계산이다. bwrap의 bind는 재귀적이라 하위 마운트까지 따라 들어가기 때문이고, **파일 하나를 bind mount한 별칭도 하위 마운트라서 여기서 막힌다.** bind mount는 링크 수를 늘리지 않으므로 R1만으로는 잡히지 않는다.
- **R0 (디렉터리 별칭):** 붙일 트리의 루트와 그 안의 모든 디렉터리가 P에 없다.
  - 보호 트리 안의 디렉터리를 붙이려는 경우(심볼릭 링크로든 bind mount로든 경로를 바꿔 적어도)는 루트가 P에 걸린다.
  - 보호 트리를 품는 경우는 트리를 내려가다 보호 루트를 만나서 걸린다.
  - 디렉터리는 하드링크될 수 없으므로 이 둘이 디렉터리로 닿는 길의 전부다.
- **R1 (파일 하드링크):** 붙일 트리 안의 링크 수 2 이상인 파일이 P에 없다. 링크 수가 1인 파일은 다른 이름이 없다. 그런 파일이 보호 트리 안에 있다면 그것이 든 디렉터리가 R0에 걸리고, 파일 bind mount 별칭이라면 M0에 걸린다.
- **R2 (`writable` 트리):** 쓰기 트리의 파일은 트리 밖에 다른 이름을 가지면 안 된다. 트리 안에서 찾은 이름 수가 링크 수와 같아야 한다. Workspace admission과 같은 계산이고, 이것이 없으면 서버가 쓰기 범위 밖의 파일을 고칠 수 있다.
- **`read_only` 트리의 다른 외부 링크는 허용한다.** 읽기 전용이라 고칠 수 없고, R1이 HIMARI의 것은 이미 막는다. 실제로 Calliope 트리의 파일 5,249개 중 136개가 링크 수 2 이상인데, `uv`가 패키지 캐시에서 venv로 하드링크한 것이다. 이를 막으면 Calliope를 띄울 수 없다. Calliope의 쓰기 폴더(`~/.local/share/calliope`, 파일 152개)에는 링크 수 2 이상인 파일이 없다.
- **순서와 경쟁:** fd를 먼저 열고, 검사도 바인드도 그 fd로 한다(`--ro-bind-fd`·`--bind-fd`). 검사와 바인드 사이에 누가 경로를 바꿔도 붙는 것은 검사한 그 대상이다.
- **한도:** P를 모으는 것과 붙일 트리를 내려가는 것을 합쳐 20만 항목(admission의 `MAX_ADMISSION_ENTRIES`)이다. 넘으면 띄우지 않는다. P는 디렉터리와 다중 링크 파일만 모으므로, 보통 파일이 많은 Workspace여도 대개 작다. 그래도 세지 않고 지나가는 항목은 없다.

**보장의 한계.** 검사는 세션을 띄우는 시점의 것이다.

- **Sandbox 안의 서버는 새 링크로 트리를 넘을 수 없다.** `link(2)`는 같은 파일시스템이어도 마운트 지점이 다르면 실패한다(EXDEV). 서버가 볼 수 있는 트리는 각각 별도 bind mount다.
- **Sandbox 밖에서 같은 사용자로 도는 프로세스는 세션 도중에 링크나 별칭을 만들 수 있다.** 예를 들어 Calliope HTTP 서버는 쓰기 폴더를 같이 쓰고 격리 없이 돈다. 새 하드링크는 다음 세션을 띄울 때 잡힌다. 이미 마운트된 Sandbox 안으로 새 마운트가 들어가지는 않는다(Sandbox는 자기 mount namespace를 가진다). 그 사이의 노출을 막으려면 그런 프로세스를 다른 사용자로 돌려야 한다. 이 문서는 그것을 운영 권고로 둔다.
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
  "processes": {"scope": "shared", "idle_seconds": 1800, "max": 4},
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
- `processes.scope`: `"shared"`(기본)는 서버당 프로세스 하나를 모든 대화가 같이 쓴다. `"per_conversation"`은 대화마다 따로 띄운다(§5.3).
- `processes.idle_seconds`(기본 1800, 60~86400): 이만큼 쓰지 않은 프로세스는 종료한다. `shared`에서는 서버 전체 기준, `per_conversation`에서는 대화 기준이다.
- `processes.max`(기본 4, 1~16): `per_conversation`에서 그 서버에 동시에 살아 있을 수 있는 프로세스 수. `shared`에서는 1이다.
- `token_env`는 stdio에 쓸 수 없다. `ui`·`tools`·`default`·`timeout_seconds`는 HTTP와 같다.
- 효과(`effect`)는 지금처럼 운영자가 정한다. **`read`의 뜻을 분명히 한다(PR #29 검수 B2):** `read`는 "다시 실행해도, 실행됐는지 몰라도 아무것도 달라지지 않는다"이다. 여기에는 외부 파일뿐 아니라 **서버의 세션 상태(발급한 id, 예산, 세션 트리)**도 들어간다.
  - 이것이 중요한 이유: Dispatcher는 `read` 호출을 claim하지 않는다. 그래서 요청이 서버에 간 뒤 Stop이 와도 그 호출을 "실행 안 됨"(CANCELLED)으로 보고한다. 그 보고가 해롭지 않은 Tool에만 `read`를 줄 수 있다. 이 정의는 HTTP 서버에도 같다. README의 운영 규칙에도 적는다.
  - Calliope(코드 확인, 2026-10-15)는 아래와 같다. `scoped_write`는 실행 전에 claim되므로, 중간에 끊기면 "결과 불명, 다시 확인할 것"으로 보고되고 다시 실행되지 않는다. §5.7의 Calliope 변경 뒤에도 분석·탐색은 예산과 캐시를 바꾸므로 분류는 같다.

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
- **세 층으로 나눈다 (확장성):**
  - **전송**(`HttpTransport`·`StdioTransport`): 메시지 하나를 보내고 받는 방법.
  - **프로토콜 판**(`Revision2025_11_25`; 나중에 `Revision2026_07_28`): 시작 절차(`initialize`나 `server/discover`), 요청마다 붙일 것(`_meta`), 결과 해석(`resultType`).
  - **JSON-RPC 처리**: id, 짝 맞추기, 결과 확실성.

  이번에 구현하는 판은 2025-11-25 하나다. 2026-07-28은 판만 더하면 되게 둔다(V3-P61). Calliope의 MCP SDK 2.3.0은 두 판을 모두 말하므로(`LATEST_PROTOCOL_VERSION = 2026-07-28`, `server/discover` 처리 있음) 지금 판으로 바로 붙는다.

### 5.3 프로세스 단위 (사용자 결정)

- **`shared`(기본):** 서버당 프로세스 하나를 모든 대화가 같이 쓴다. 다른 MCP 호스트들이 보통 이렇게 하고, 서버도 그것을 전제로 만들어진다. 최신 사양처럼 서버가 연결에 상태를 두지 않으면 이것으로 충분하다.
- **`per_conversation`(서버마다 선택):** 키가 (서버, 대화)다. 대화에서 그 서버의 Tool을 처음 부를 때 띄우고, 그 뒤로는 같은 프로세스를 쓴다. 연결 단위로 상태를 가진 옛 방식의 서버를 대화끼리 섞이지 않게 할 때 쓴다.
- **공통:**
  - 여러 호출은 한 프로세스로 동시에 보낸다(JSON-RPC는 id로 짝을 맞춘다). 서버가 하나씩 처리하는 것은 서버의 일이다.
  - `idle_seconds` 동안 진행 중인 호출이 없으면 종료한다(§5.5). 다음 호출이 새로 띄운다.
  - **프로세스가 죽으면:** 진행 중이던 호출은 결과 불명이다(§5.4). 다음 호출이 새로 띄운다. 연속으로 실패하면 띄우기를 늦춘다(1·2·4초…, 최대 60초).
- **`per_conversation` 상한:** 살아 있는 프로세스가 `max`개면 가장 오래 쉰 것을 종료하고 띄운다. 모두 호출 중이면 그 호출을 "실행하지 않음"으로 거절한다(`FAILED`, "this server has no free session; try again").
- **`shared`에서 취소 뒤 확인의 영향 (2026-10-16 개정):** 취소 뒤 서버가 `ping`에 답하는 동안(`CHECKING`, 보통 수 밀리초) 모든 대화의 새 호출은 기다린다. 서버가 10초 안에 답하지 않아 프로세스를 끝내면(§5.5), 그 프로세스에 걸려 있던 **다른 대화의 호출도** 결과 불명이 된다. 살아 있는 서버라면 이런 종료는 일어나지 않는다.
- **목록과 화면은 따로 읽는다:** HIMARI가 시작할 때 서버마다 **목록용 프로세스**를 한 번 띄워 `initialize`·`tools/list`·화면 읽기(`resources/read`, V3-P58)를 하고 종료한다. Tool 정의와 화면은 이것으로 정한다(최신 사양: 목록은 연결마다 달라지지 않는다). 다시 읽는 것은 재시작이다.
- **기록하지 않는다:** 프로세스 상태는 HIMARI의 Data에 남기지 않는다. HIMARI를 재시작하면 모든 프로세스가 새로 시작된다.
- **새로 띄운 뒤의 옛 id (PR #29 검수 B4).** 이전 프로세스에서 받은 id가 새 프로세스에서 무엇을 가리키는지 HIMARI는 알 수 없다. 그래서 두 가지를 둔다.
  - **서버 쪽이 원칙이다.** id는 어느 프로세스·연결에서 받든 같은 대상을 가리키거나, 모르면 분명히 거절해야 한다(최신 사양 SEP-2567의 방향). Calliope는 이렇게 고친다(§5.7).
  - **HIMARI 쪽 안전장치(모든 stdio 서버):** 어떤 대화가 쓰던 프로세스가 새로 시작되면(유휴 정리, 상한 교체, 프로세스 종료, 취소 후 종료), 그 대화가 새 프로세스에서 받는 첫 결과 앞에 모델용 안내를 붙인다: "This server was restarted: identifiers from its earlier answers may no longer apply; check them before relying on them." 서버가 이미 원칙을 지키면 해가 없고, 옛 서버에서는 모델이 알 수 있다.

### 5.4 결과의 확실성 (V3-055의 stdio판)

| 언제 | 판정 |
| --- | --- |
| 프로세스를 띄우지 못함, Sandbox가 거부, `initialize` 실패, 상한 초과 — **요청을 쓰기 전** | 실행 안 됨 (`McpRefused` → `FAILED`) |
| JSON-RPC parse / invalid request / method not found / invalid params 오류 | 실행 안 됨 |
| 쓰기 직전에 이미 죽어 있음 (returncode 있음) | 실행 안 됨 |
| 요청을 쓴 뒤: 프로세스 종료, 시간 초과, 사양 위반 출력, 취소, 그 밖의 JSON-RPC 오류 | **결과 불명** (`McpUncertain` → claim된 호출은 UNKNOWN, 재실행 안 함, V3-047) |
| 쓰다가 실패(BrokenPipe)했는데 직전까지 살아 있었음 | 결과 불명 (일부를 읽었을 수 있음) |
| `isError: true` | 서버가 실행해서 실패한 것 (`FAILED`, 지금과 같음) |

### 5.5 취소와 종료 (2026-10-16 개정)

**Stop의 뜻 — 최선 노력 취소 (MCP 표준, HTTP·stdio 공통).**
- Stop이나 시간 초과가 나면 HIMARI는 서버에 `notifications/cancelled`를 보내고 그 호출을 기다리기를 멈춘다. 그게 전부다.
  - 사양상 서버는 처리를 멈춰야 하지만(SHOULD), 무시할 수도 있다(MAY: 이미 끝났거나 멈출 수 없는 작업).
  - 그래서 HIMARI는 **서버가 작업을 멈췄다고 보장하지 않는다.** 취소된 호출의 효과(파일, 캐시, 예산 등)는 늦게라도 생길 수 있다.
- 결과는 지금의 확실성 계약 그대로다.
  - claim된 호출(`scoped_write`, `unknown`)은 결과 불명이고 다시 실행하지 않는다(V3-057).
  - `read` 호출은 "실행 안 됨"으로 보고된다. `read`는 정의상 다시 실행해도, 실행됐는지 몰라도 아무것도 달라지지 않는 Tool이다(§5.1). 늦게 끝나도 해가 없는 Tool에만 `read`를 준다는 운영 규칙이 바로 이 경우를 위한 것이다.
- **HTTP 서버에도 같다.** 사양상 연결이 끊긴 것을 취소로 보면 안 되고(SHOULD NOT), 취소하려면 취소 알림을 명시적으로 보내야 한다(SHOULD). 지금 HTTP 채널은 연결만 끊으므로, 취소 알림을 보내도록 고친다. 계약은 다음과 같다(PR #35 재검수 N2).
  - **무엇을 취소하나:** 요청의 POST가 실제로 시작된 것(연결이 이루어진 것)만 취소한다. 보내지 못한 요청은 서버가 모르므로 알림을 보내지 않는다. `initialize`는 어느 전송에서든 취소하지 않는다(MUST NOT). 알림은 알림이므로 취소 대상이 아니다.
  - **무엇을 담나:** 요청을 보낼 때 그 요청의 JSON-RPC id, 세션 id(`MCP-Session-Id`), 프로토콜 버전을 함께 기억해 둔다. 알림은 그 값들로 보낸다. 그사이 세션이 갱신됐어도 원래 요청의 세션으로 보낸다. 그 세션이 이미 없으면 서버가 404로 답할 것이고, 그것으로 끝이다.
  - **어떻게 보내나:** 취소된 호출 안에서 기다리지 않는다. 별도의 백그라운드 작업이 보내고, 그 작업에는 따로 시간 제한(5초)이 있다. Stop은 알림 전송을 기다리지 않고 바로 끝난다. HIMARI가 끝날 때 남은 알림 작업은 그 시간 제한 안에서 마치거나 버린다.
  - **실패해도:** 알림 전송의 성공이나 실패, 서버의 답(202, 404, 오류)은 원래 호출의 판정을 바꾸지 않는다. 판정은 §5.4 그대로다(최선 노력).
  - HTTP 서버의 프로세스는 HIMARI가 관리하지 않으므로, 아래의 확인과 종료는 stdio에만 해당한다.
- **stdio의 취소 대상도 같다:** 큐에 들어가 승인된 요청만 취소 알림을 보낸다. 승인되기 전에 멈춘 호출은 서버가 모르므로 보내지 않는다. `initialize`는 취소하지 않는다(§5.5 아래, C2).

**stdio: 프로세스 상태.** `STARTING` → `READY` ⇄ `CHECKING` → `CLOSING` → (끝).

**stdin은 하나의 순서 있는 큐로만 쓴다 (PR #35 검수 B2·B3).**
- 세션마다 쓰기 큐가 하나 있다. 요청, 취소 알림, `ping`, 서버의 `ping`에 대한 답이 모두 이 큐를 거친다. 큐에 들어간 순서대로 한 줄씩 쓴다.
- **요청의 승인:** 요청은 세션이 `READY`인 순간에 큐에 들어갈 때만 승인된다. 상태 확인과 큐 넣기 사이에는 다른 작업이 끼어들 틈이 없다(같은 동기 구간).
  - 승인된 요청은 그 뒤 세션이 `CHECKING`이 되어도 순서대로 쓰인다. 이미 보낸 요청과 똑같이 "진행 중"으로 다룬다(확인이 실패해 종료되면 결과 불명).
  - `CHECKING` 중에 온 요청은 승인되지 않고 기다린다.
- **취소의 순서:** 한 번의 취소는 같은 동기 구간에서 다음을 한다.
  1. 그 요청을 기다리는 목록에서 뺀다.
  2. 취소 알림을 큐에 넣는다.
  3. `ping`을 큐에 넣는다.
  4. 세션을 `CHECKING`으로 두고, **이번 확인의 마감**을 지금(큐에 넣은 때) + 10초로 정한다.

  `CHECKING` 중에 다시 취소하면 1~3을 한다(새 알림과 새 `ping`). **마감은 늘리지 않는다**(PR #35 재검수 N1). 확인 하나는 처음 들어간 때부터 10초를 넘지 않는다. 확인을 기다려야 할 가장 최근 `ping`만 새 것으로 바뀐다.
- **`READY`로 돌아가는 조건:** 가장 최근에 넣은 `ping`에 응답이 오면 돌아간다. 이전 `ping`의 응답은 무시한다.
  - **오류 응답도 받아들인다(호환 정책).** 사양은 `ping`에 빈 결과로 답하라고 한다(MUST). 그래도 오류로 답하는 서버가 있을 수 있다(예: `ping`을 구현하지 않아 `-32601`). 그 서버도 stdin을 읽고 순서대로 답하고 있으므로, 이 확인이 알고 싶은 "살아 있는가"에는 "예"다. 그래서 오류도 `READY`로 본다.
  - 큐가 순서대로 쓰고 서버는 stdin을 순서대로 읽으므로, 이 응답은 그 앞의 모든 취소 알림이 서버에 전달됐고 서버가 응답한다는 뜻이다.
  - 그래서 `READY` 뒤에 승인되는 요청은 어떤 취소 알림보다도 뒤에 쓰인다.
- **마감까지 가장 최근 `ping`의 응답이 없으면** `CLOSING`이 되고 종료 절차를 밟는다(ping 사양: 연결이 죽은 것으로 볼 수 있다, MAY).
  - 마감은 확인에 처음 들어간 때부터 세므로, 파이프가 막혀 알림이나 `ping`을 쓰지 못해도 10초면 끝난다.
  - 마감 직전에 또 취소하면 그 `ping`에 남는 시간이 짧다. 살아 있는 서버는 수 밀리초 안에 답하므로 실제로 문제가 되지 않는다. 확인 시간에 상한을 두기 위해 받아들이는 대가다.
- **알림이나 `ping`을 쓰다가 실패하면** 곧바로 종료한다.
- **취소된 요청의 응답**(취소와 완료가 엇갈린 경우 등)은 버린다(사양 6항). 모르는 id의 응답과 같다. 따로 기억하지 않으므로, 응답하지 않는 것이 정상인 취소가 쌓여도 메모리가 늘지 않는다.
- **`CHECKING` 중에 새 호출이 오면 (B3 유지):** 승인하지 않고 확인이 끝날 때까지 기다린다. 살아 있는 서버라면 `ping` 왕복 시간만큼이고, 응답이 없으면 최대 10초 + 종료 약 4초다. `shared`이면 모든 대화의 새 호출이 기다린다.
  - `READY`로 돌아가면 그 세션에서 승인된다.
  - 종료되면 새 프로세스를 띄운다. 재시작 안내(§5.3)가 붙는다.
  - 기다리는 동안 Stop이 오면 그 호출은 아직 승인되지 않았으므로 "실행 안 됨"이다.
- **이 확인이 잡는 것과 못 잡는 것.**
  - 잡는 것: `ping`에도 답하지 않는 서버(멈춘 프로세스, 막힌 stdin). EOF(죽은 프로세스)와 사양 위반 출력은 지금처럼 바로 드러난다.
  - **못 잡는 것:** `ping`에는 답하면서 Tool 작업만 멈춘 서버. 그런 서버에서는 호출마다 시간 초과가 나고 결과 불명이 되지만, 프로세스를 다시 띄우지는 않는다. 어디서부터 "고장"으로 볼지는 서버마다 다르므로(Calliope의 긴 분석처럼 정상일 수 있음) 이번에는 정하지 않는다(§9).
- **기다리는 동안 출처가 꺼지면 (C1):** 실행 권한의 결정 시점은 지금처럼 **Dispatcher의 마지막 확인**이다(V3-P53 동시성 계약: 인자 검증 뒤, claim·실행 직전). 그 확인을 통과한 호출은 시작된 것으로 보고, `CHECKING` 대기는 그 실행의 일부다. HTTP 호출이 네트워크를 기다리는 것과 같다. 그래서 대기 중에 출처를 꺼도 그 호출은 멈추지 않는다. 대기는 최대 약 14초다.
  - 다시 확인하지 않는 이유는 두 가지다. 첫째, claim된 호출(`scoped_write`)을 그 시점에 멈추면 결과가 이미 불명으로 기록된 뒤라 "실행 안 됨"으로 바꿀 수 없다. 둘째, 같은 규칙을 HTTP와 stdio에 똑같이 적용하는 쪽이 계약이 단순하다.
  - 꺼진 뒤 Dispatcher가 새로 받는 호출은 지금처럼 `DENIED`다.
- **프로세스를 종료하면:** 그 프로세스에 쓰고 아직 응답을 받지 못한 요청은 **모두** 결과 불명이다. 동시에 보냈던 다른 호출도 포함하고, `shared`이면 다른 대화의 호출도 포함한다. 그 호출들의 handler는 `McpUncertain`을 받는다.
- **`initialize`는 취소하지 않는다 (C2):** 사양이 금지한다(MUST NOT). 세션을 띄우는 중(`STARTING`)의 시간 초과(15초)나 Stop은 취소 알림 없이 바로 종료 절차로 간다. 이때 기다리던 호출은 아직 쓰지 않았으므로 "실행 안 됨"이다. 목록용 세션(§5.3)도 같다.
- **종료 절차(사양):** stdin을 닫고 2초 기다리고, `SIGTERM`(프로세스 그룹)을 보내고 2초 기다리고, `SIGKILL`을 보낸다. bwrap이 PID namespace를 가지므로 그룹을 죽이면 서버가 띄운 하위 프로세스(Calliope의 Stockfish)도 함께 끝난다.
- **HIMARI가 끝날 때:** 모든 세션에 종료 절차를 동시에 적용한다(합쳐서 최대 약 4초).

### 5.6 화면(V3-P58)과의 관계

화면은 목록용 세션에서 읽은 것을 쓴다(§5.3). 그 밖에 바뀌는 것은 없다. Calliope의 `show_game`은 `ui://calliope/viewer.html` 화면을 가지므로 1단계 표시는 그대로 동작한다. 뷰어의 "해설 새로고침"(화면의 `tools/call`)은 2단계 전까지 거절된다.

### 5.7 Calliope 쪽 변경 (Calliope 저장소, 소유자 허용 2026-10-15)

Calliope를 어느 호스트에서도, 어떤 프로세스 단위로도 같게 동작하게 한다. Calliope의 설계 절차(설계 → 독립 검수 → 구현)로 Calliope 저장소에서 진행하고, 이 문서는 HIMARI가 기대는 결과만 적는다. 설계 묶음: Calliope [AG4-D](https://github.com/jihunmoon04/Calliope/pull/101)(`docs/design/agent-ag4-design.md`). 게임 id는 `g-` + `game_key` 앞 16자리이고, `<data>/agent/games/`에 한 번만 쓰는 기록이 있다. 예산은 프로세스의 15분 이동 창(권고)이다.

- **게임 id = 내용 기반.** 지금은 세션마다 `g1`, `g2`처럼 다시 센다. 바꾸면 게임 키(`game_key`, sha256)에서 만든 id가 된다. 같은 게임은 언제 어느 프로세스에서 열어도 같은 id를 받는다. `open_game`은 같은 게임에 같은 id를 돌려준다.
- **id만으로 다시 열 수 있게.** 연 게임의 이동 기록을 데이터 폴더에 남긴다(파일은 한 번 쓰고 고치지 않음, Calliope §9의 쓰기 규칙). 다른 프로세스에서 받은 id도 그 기록으로 찾는다. 기록이 없는 id는 분명히 거절한다.
- **예산은 연결 단위에서 뗀다.** 지금의 세션 누적 예산은 호스트마다 다르게 동작한다. 서버당 하나를 쓰는 호스트에서는 금방 바닥나고, 대화별로 띄우면 대화마다 새로 생긴다. 호출 단위 상한과 프로세스 공통의 시간 창 예산 같은 모양으로 바꾸는 안을 Calliope 설계에서 정한다.
- **인용 핸들은 그대로 둔다.** 이미 프로세스마다 무작위 토큰을 붙여(`c3.4f2a1b`) 다른 프로세스의 핸들을 분명히 거절한다. 세션 트리를 가리키므로 프로세스를 넘어 살리지는 않는다(이번 범위 밖).
- **HIMARI가 Calliope를 연결하기 전 조건**은 게임 id 변경이다. 예산 변경 전에는 `per_conversation`으로 연결할 수 있다.

## 6. 계약 변경 (Proposed)

| 계약 | 변경 | 비고 |
| --- | --- | --- |
| 새 모듈 `sandbox` | `api.py`: `SandboxState`, `SandboxUnavailableError`, `SandboxSetupError`, `CommandResult`, `WorkspaceProfile`, `ServerProfile`, `ProcessLauncher`, `IsolatedProcess` | `ONLY_FROM["sandbox"] = {"common"}`. 층은 common 바로 위 |
| ENV-04/05 | Sandbox의 소유가 Environment에서 `sandbox` 모듈로 옮겨진다. Inside는 소비자가 된다 | 동작 변경 없음 (1단계) |
| V3-059 | **바뀌지 않는다** (PR #29 검수 C1): Bootstrap이 서버별 실행기를 주입하고, MCP 커넥터는 자기 Protocol로 받는다(§4.3) | 초안의 "MCP가 `sandbox.api`를 import" 안은 철회 |
| MCP 커넥터 | stdio 전송, stdio 프로세스 관리자(`shared`/`per_conversation`), `mcp.json`의 stdio 키 | 공개 계약 아님 (V3-059) |
| Bootstrap | 서버 프로파일 자체 검사, 서버별 launcher를 만들어 `mcp.connect`에 주입, 종료 시 세션 정리 | |
| 바뀌지 않음 | Data, Control, Prompt, Provider, Tool 계약, Web·CLI API | 세션 상태는 표시 대상이 아니다. 시작 문제는 지금처럼 `SourceStatus.problems`에 보인다 |

## 7. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 운영자가 적은 명령이 HIMARI 데이터·비밀에 닿음 | 서버 프로파일 Sandbox: 데이터 폴더·Workspaces는 마운트 자체가 없고, 겹치는 경로는 설정에서 거부 |
| 하드링크로 HIMARI 파일에 닿거나 쓰기 범위 밖을 고침 | 세션마다 R1(데이터 폴더와 inode 공유 금지)·R2(쓰기 트리의 외부 링크 금지). 서버는 새 교차 링크를 만들 수 없음(EXDEV). 같은 사용자의 격리 안 된 프로세스가 만드는 링크는 다음 세션에서 잡힘(§4.2 한계) |
| 프로세스가 바뀐 뒤 옛 id가 다른 대상을 가리킴 | 서버의 id를 프로세스와 무관하게(Calliope: 내용 기반 게임 id), HIMARI는 재시작 안내를 모델에게 |
| 취소 뒤 확인 실패에 따른 종료가 새 호출을 함께 끝냄 | `CHECKING` 중에는 새 요청을 승인하지 않음. 승인은 `READY`인 순간 큐에 넣을 때만 |
| 취소된 작업이 서버에서 계속되어 늦게 효과를 냄 | 최선 노력 취소(사양). claim된 호출은 결과 불명, `read`는 정의상 효과 없음(§5.5) |
| HIMARI의 API 키·토큰이 서버로 샘 | 환경 변수는 정해진 것과 운영자가 적은 것만 (`env_from`은 이름을 콕 집어 복사) |
| 서버가 네트워크로 무언가를 보냄 | 네트워크 없음 (V3-037). 필요한 서버는 이 설계로는 쓸 수 없다 |
| 한 대화의 서버 상태가 다른 대화로 샘 | 서버가 연결에 상태를 두지 않는 것이 원칙(§5.7), 그렇지 않은 서버는 `per_conversation` |
| 서버가 멈추거나 출력을 쏟아냄 | 시간 제한·줄 한도·stderr 링버퍼. 취소 뒤 `ping`에 10초 안에 답하지 않으면 종료(`ping`에는 답하면서 Tool만 멈춘 서버는 못 잡음, §9) |
| 프로세스가 쌓임 | 서버당 `max`, 유휴 정리, 종료 시 프로세스 그룹 전체 종료 |
| 메모리·CPU 과다 사용 | **대응 없음 (Q-40).** 프로세스 수로만 묶는다 |
| Sandbox가 없는 호스트 | stdio 서버를 띄우지 않음 (사용자 결정) |

## 8. 검증 계획

- **1단계 (분리):** 기존 단위·호스트 테스트를 import 경로만 바꿔 그대로 통과한다. import 검사에 `sandbox` 규칙을 추가한다. 전체 pytest를 돌린다(계약 위치가 바뀌므로).
- **2단계 (서버 프로파일):**
  - 단위: bwrap 인자(같은 경로 fd 붙이기, 네트워크 없음, `--bind`류 없음), 경로 검사(데이터 폴더와 겹침·심볼릭 링크·소유자), 종료 절차 순서(가짜 프로세스).
  - 호스트(`HIMARI_SANDBOX_HOST=1`): §4.4의 모든 항목, 그리고 하위 프로세스까지 종료되는지.
  - 정체성 검사. 각 경우가 거부되는지 확인한다:
    - 조상 경로의 심볼릭 링크로 데이터 폴더 안을 가리키는 경로(`/tmp/alias/control`): R0
    - 데이터 폴더의 디렉터리를 bind mount한 별칭: R0
    - 붙일 트리 안에 파일 bind mount: M0
    - 데이터 폴더를 품는 경로: R0
    - 데이터 폴더 파일의 하드링크: R1
    - `writable` 트리 파일의 바깥 링크: R2
    - 20만 항목 초과

    허용되는 것: `read_only` 트리의 무해한 외부 링크(uv 캐시). 그리고 Sandbox 안에서 트리를 넘는 `link`는 EXDEV다. bind mount가 필요한 경우는 호스트 테스트(`HIMARI_SANDBOX_HOST=1`)에서만 돌린다.
- **3단계 (stdio):**
  - 가짜 stdio MCP 서버(Python 스크립트)로 다음을 확인한다:
    - 한 줄 메시지, 응답 짝 맞추기, 알림 무시, `ping` 응답
    - 비 JSON 줄과 한도 초과 줄은 종료와 결과 불명
    - §5.4 표의 모든 경우
    - `shared`·`per_conversation` 각각: 프로세스 수, 유휴 정리, 상한, 재시작 지연, 확인 실패에 따른 종료가 다른 대화 호출에 주는 영향(`shared`)
    - (2026-10-16 개정, §11.4) 취소된 요청에 응답하지 않는 서버: `ping`에 답하면 같은 프로세스로 `READY`. `ping`에도 답하지 않으면 10초 뒤 종료
    - `CHECKING` 중의 새 호출은 승인되지 않고 기다림. 유지되면 같은 프로세스, 종료되면 새 프로세스와 안내
    - 쓰기 순서: 취소 알림 → `ping`, 추가 취소마다 알림과 새 `ping`, 가장 최근 `ping`의 응답으로만 `READY`. 승인된 요청은 그 뒤 취소가 와도 순서대로 쓰임
    - 대기 중 출처를 끄면 그 호출은 계속되고, 그 뒤의 새 호출은 `DENIED`
    - 종료 시 같은 프로세스의 다른 미응답 요청도 결과 불명
    - `initialize` 중 시간 초과와 Stop에는 취소 알림 없이 종료하고, 기다리던 호출은 실행 안 됨
    - 새로 띄운 프로세스의 첫 결과에 안내가 붙음(대화마다 한 번)
    - 목록용 세션, 화면 읽기
  - **Calliope 실제 연결(호스트) — Calliope AG4 구현 후 (C3):** 3단계 구현과 그 테스트는 가짜 stdio 서버로 끝낼 수 있다. Calliope 연결 수락 테스트는 Calliope의 게임 id 변경(AG4)이 병합된 뒤에 한다. 위 설정으로 `open_game` → `ply` → `show_game`(화면) → `submit_commentary`. 확인할 것:
    - Calliope 변경 뒤: 두 대화가 같은 게임을 열면 같은 id, 다른 프로세스에서 받은 게임 id도 통하는지
    - 데이터 폴더를 Calliope HTTP 서버와 같이 쓸 수 있는지(파일 잠금)
    - 하드링크가 있는 venv가 R1·R2를 통과하는지
    - 기록이 없는 게임 id는 분명히 거절되는지

## 9. 남은 질문

1. **Calliope 설계**(§5.7): AG4-D로 정해졌다(Calliope PR #101 병합, 구현 PR #102).
2. **`ping`에는 답하면서 Tool만 멈춘 stdio 서버**(§5.5): 연속 시간 초과 횟수 같은 기준으로 다시 띄울지. 서버마다 정상적인 작업 시간이 달라 이번에는 정하지 않았다.
3. **2026-07-28판 지원**(V3-P61): 핸드셰이크 없이 요청마다 `_meta`, `server/discover`, `resultType`, 목록 캐시(`ttlMs`). 클라이언트의 판 층(§5.2)에 더한다.
4. **네트워크가 필요한 stdio 서버**: V3-037(Confirmed) 개정과 "연결 허용" 프로파일. 이번 범위 밖이다.
5. **자원 제한**(Q-40): cgroup이나 rlimit. 프로세스가 오래 살게 되면서 중요도가 올라간다.
6. **운영 권고**(§4.2 한계): 쓰기 폴더를 같이 쓰는, 격리되지 않은 프로세스(Calliope HTTP 서버 등)를 다른 사용자로 돌릴지.
7. **Calliope 쪽 관찰**:
   - Calliope §9의 임시 파일 정리는 PID로 프로세스 생존을 판단한다. Sandbox 안의 PID는 별도 namespace라, 크래시로 남은 임시 파일이 지워지지 않을 수 있다(해롭지는 않음).
   - Calliope HTTP 서버의 예시 포트 8765는 HIMARI 기본 포트와 겹친다.

## 10. 구현 노트 (2026-10-15)

### 10.1 1단계 — Sandbox 모듈 분리

설계대로 옮겼고, 동작은 바뀌지 않았다. 다음만 구체화했다.

- **위치:** `environment/inside/sandbox/`의 `backend.py`·`landlock_helper.py`·`self_test.py`를 `src/himari/sandbox/`로 옮겼다. 단위 테스트도 `tests/sandbox/`로 옮겼다. 테스트 내용은 import 경로만 바뀌었다.
- **`sandbox.api`:** 값 타입과 계약만 둔다.
  - `SandboxState`, `SandboxUnavailableError`, `SandboxSetupError`, `CommandResult`;
  - V3-051 한도 상수와 `validate_argv`, `WORKSPACE_MOUNT`;
  - **`WorkspaceSandbox` Protocol** (`state`, `start`, `run`): Workspace 프로파일의 계약이다. Inside는 이것만 본다.
- **§6의 `WorkspaceProfile`:** 1단계에서는 위 Protocol과 상수로 나타냈다. 프로파일을 값으로 나누는 일은 두 번째 프로파일이 생기는 2단계에서 `ServerProfile`과 함께 한다.
- **하위 호환:** `sandbox.backend`는 옮겨 간 이름(`SandboxState`, `validate_argv`, 한도 상수 등)을 그대로 다시 내보낸다. 그래서 기존 테스트가 쓰던 `backend.X`가 그대로 동작한다.
- **import 검사:** `LAYERS["sandbox"] = 1`, `ONLY_FROM["sandbox"] = {"common"}`. Environment는 `sandbox.api`만 쓸 수 있고(`sandbox.backend`는 거부), MCP 커넥터는 `sandbox`를 아예 쓸 수 없다(V3-059). 이 규칙들의 테스트를 `tests/scripts/test_check_imports.py`에 더했다.
- **Bootstrap:** `SandboxBackend`를 `himari.sandbox.backend`에서 만들어 Inside에 넘긴다. 하는 일은 전과 같다.

### 10.2 2단계 — 서버 프로파일

§4대로 구현했다. 새 파일은 `sandbox/identity.py`(정체성 검사), `sandbox/server.py`(실행기와 프로세스), `sandbox/server_self_test.py`(자체 검사)이다. `sandbox.api`에는 `ServerProfile`, `IsolatedProcess`·`ProcessLauncher` Protocol, `ProcessGone`, `SandboxAdmissionError`, `SandboxProfileError`와 한도(`MAX_LINE_BYTES` 4 MiB, `STDERR_TAIL_BYTES` 64 KiB, `MAX_READ_ONLY` 16, `MAX_WRITABLE` 4, `MAX_IDENTITY_ENTRIES` 20만)를 더했다. Bootstrap 연결과 MCP 쪽 주입은 3단계에서 한다.

**Workspace 경로는 그대로다.** 두 프로파일이 같은 bwrap 구성 함수(`sandbox_arguments`)를 쓰도록 나눴고, `run_command`의 bwrap 인자는 나누기 전과 바이트 단위로 같다(확인함).

**구체화하거나 더한 것:**
- **상태:** 서버 프로파일은 `ServerSandbox`라는 별도 객체다. 호스트 능력(bwrap·Landlock)은 Workspace와 같은 것을 쓰고, 상태와 자체 검사는 따로 가진다. 실행 중 Sandbox 구성이 실패하면(`SandboxSetupError`) Workspace처럼 SETUP_FAILED로 굳는다.
- **보호 루트는 Sandbox가 쥔다 (PR #32 검수 B1):** `ServerSandbox(capability, protected)`는 만들 때 보호 루트(적어도 HIMARI 데이터 폴더)를 받아야 하며, 빈 목록이면 거부한다. 이 루트는 자체 검사와 모든 launcher의 설정 검사, 모든 실행의 정체성 검사에 항상 적용된다. 프로파일의 `protected`는 추가분일 뿐이다. 그래서 Bootstrap이 프로파일에 보호 경로를 빠뜨려도 데이터 폴더는 붙지 않는다.
- **붙일 트리끼리의 정체성 (B3):** 실행할 때 연 fd로 확인한다. 두 트리가 같은 디렉터리이거나, 한 트리가 다른 트리의 루트를 품으면(심볼릭 링크·bind mount 별칭으로 철자만 달라도) 거부한다. 같은 디렉터리가 읽기 전용과 쓰기로 동시에 보이지 않게 하기 위해서다. 쓰기 폴더의 소유자도 붙일 fd로 다시 확인한다.
- **굳지 않는 거부:** 설정 검사 실패(`SandboxProfileError`), 정체성 검사 실패, 붙일 폴더를 열 수 없음(`SandboxAdmissionError`)은 그 실행만 거부한다. 폴더 하나가 사라졌다고 다른 서버까지 막히지 않게 하기 위해서다.
- **설정 검사에 더한 것:**
  - 붙일 폴더끼리 겹치면 거부한다.
  - **Sandbox가 직접 놓는 곳 (B2):** `/proc`, `/dev`, `/sys`, `/run/himari`(helper), `/tmp/home`, 시스템 뷰(`/usr`, `/bin`·`/lib` 등, `/etc`의 세 파일). 그 자체, 그 안, **그것을 덮는 상위 경로**(예: `/run`, `/etc`) 모두 거부한다. bwrap은 나중에 붙인 것이 앞의 것을 가리기 때문이다.
  - **`/tmp` (B2):** `/tmp` 자체나 그 상위는 Sandbox의 tmpfs를 호스트의 `/tmp`로 바꿔 버리므로 거부한다. `/tmp` 아래의 폴더는 tmpfs 위에 붙으므로 허용한다.
  - 작업 위치는 붙인 폴더 안이거나 `/tmp` 자체여야 한다. `/tmp` 아래의 다른 경로는 Sandbox 안에 없기 때문이다.
  - 운영자 환경 변수는 고정 변수(`PATH`·`HOME` 등)와 `PWD`를 덮어쓸 수 없고, 64개·각 4,096자까지다.
- **종료의 SIGTERM:** 프로세스 구조는 bwrap(HIMARI의 자식) → PID namespace의 init인 bwrap → 서버다.
  - bwrap 그룹에 보낸 SIGTERM은 bwrap만 죽이고, 서버는 `--die-with-parent`로 SIGKILL을 받는다. 실제로 확인했다.
  - 그래서 SIGTERM은 서버 프로세스에 직접 보낸다. pidfd로 열고 부모가 init인지 확인하므로 pid 재사용으로 엉뚱한 프로세스에 가지 않는다.
  - 찾지 못하면 그룹에 보낸다. SIGKILL은 그룹에 보내고, Sandbox 안의 모든 프로세스가 함께 끝난다.
- **한 줄 한도:** asyncio 읽기 한도를 `MAX_LINE_BYTES`로 둔다. 4 MiB 줄은 읽고, 1바이트라도 넘으면 거부한 뒤 프로세스를 종료한다(양쪽 경계를 테스트함).
- **자체 검사의 임시 폴더**는 `TMPDIR`와 상관없이 `/tmp` 아래에 만든다. 홈 같은 경로가 필요 없는 구성이어야 하기 때문이다.

**실측 (이 호스트, 2026-10-15):** Calliope MCP 서버를 이 프로파일로 띄웠다.
- 설정: `read_only` `/home/coder/Calliope`·`/home/coder/opt/stockfish`, `writable` `~/.local/share/calliope`, 보호 경로는 임시 데이터 폴더.
- 정체성 검사를 포함한 실행 0.07초. Calliope 트리의 uv 하드링크는 `read_only`라서 허용된다.
- stdio로 `initialize`·`tools/list`(Tool 9개)·`open_game`이 오갔다.
- `ply`는 Sandbox 안의 Stockfish로 3.7초에 분석했고, 결과는 쓰기 폴더의 캐시에 저장됐다.
- stdin을 닫은 뒤 0.2초에 정상 종료했다.

### 10.3 3단계 — stdio 전송

§5대로 구현했다. 두 PR로 나눴다.
- **3a (PR #33):** 클라이언트를 세 층으로 나눴다. 동작 변경은 없다.
  - JSON-RPC: `mcp/jsonrpc.py`
  - 프로토콜 판: `mcp/revision.py`
  - 채널: `Channel` Protocol과 `HttpChannel`
- **3b:** `mcp/stdio.py`, `mcp.json`의 stdio 키, Bootstrap 연결.

**구체화하거나 정한 것:**
- **주입:** Bootstrap은 stdio 서버가 하나라도 있을 때만 `ServerSandbox.discover([데이터 폴더])`를 만들고 자체 검사를 돌린다. 서버마다 `launcher_for(source, stdio)`로 실행기를 준다.
  - ENFORCED가 아니면 그 서버는 이유와 함께 빠진다.
  - 프로파일 검사 실패(`SandboxProfileError`, 예: 쓰기 폴더가 데이터 폴더 안)도 그 서버만 뺀다.
  - MCP 패키지는 실행기의 모양을 자기 Protocol(`stdio.ProcessLauncher`)로 선언하고 `sandbox`를 import하지 않는다(V3-059). 실행기가 던지는 어떤 예외든 "실행 안 됨"이다. `sandbox=False`이면 stdio 서버는 띄우지 않는다.
- **목록 세션:** 시작할 때 목록용 프로세스로 `initialize`·`tools/list`·화면(`resources/read`)을 읽고 닫는다. 호출은 다른 프로세스로 간다.
- **대화 키:** `McpTool.run`은 대화 id를 키로 넘긴다(`call_tool(..., key=)`). `per_conversation`은 이 키로 프로세스를 고른다. HTTP 채널은 키를 쓰지 않는다.
- **재시작 안내의 기준:** 대화가 **요청을 보낸** 프로세스를 기억한다. 답을 받은 프로세스가 아니다. 그래서 응답 전에 프로세스가 끝난 대화도 다음 첫 결과에서 안내를 받는다(테스트 중 발견). 안내는 한 번만 붙고, 실행 기록에 `"restarted": true`를 남긴다.
- **시작 중의 Stop:** 프로세스를 띄우는 일(`STARTING`)은 서버 관리자의 작업이다. 기다리던 호출의 Stop은 그 기다림만 끝내고, 시작은 계속된다(`shared`에서 다른 대화가 같은 시작을 기다릴 수 있음). `initialize`는 취소하지 않고, 15초 안에 답이 없으면 종료한다.
- **자리 할당은 한 번에 하나 (PR #34 검수 B1):** 서버마다 할당 잠금을 둔다. 잠금 안에서 상한을 확인하고, 필요하면 유휴 세션을 닫고, 다시 확인한 뒤, 시작 작업을 등록해 자리를 잡는다. 닫을 세션은 `close`를 부르는 순간 CLOSING이 되므로, 닫히는 동안 새 요청이 들어가지 않는다. 두 대화가 동시에 같은 유휴 세션을 닫고 둘 다 프로세스를 띄우는 일이 없다(수정 전 코드에서 상한 1에 프로세스 2개를 재현함).
- **종료와 할당 (B3):** `aclose()`도 할당 잠금 아래에서 종료 상태로 바꾸고, 그 시점의 시작 작업을 모두 기다린 뒤 모든 세션을 닫는다. 그래서 그 뒤에는 어떤 시작도 등록되지 않는다.
  - `_session()`은 잠금 안에서 종료 여부를 다시 확인한다.
  - 띄우는 도중에 종료가 시작됐으면 띄운 프로세스를 바로 닫는다.
  - 수정 전 코드에서 종료 뒤 살아 남은 프로세스를 재현했다.
- **쓰기도 시간 제한 안 (B2):** 요청 쓰기와 응답 대기를 하나의 `timeout_seconds`로 묶는다. 서버가 stdin을 읽지 않아 파이프가 차면 쓰기가 끝없이 기다릴 수 있기 때문이다(수정 전 코드에서 실제 파이프로 재현함: 멈춤).
  - 쓰는 중에 시간이 다 되거나 Stop이 오면 결과 불명이다. 일부를 읽었을 수 있다.
  - 그 세션은 닫는다. 스트림에 요청의 나머지가 남았을 수 있어 다시 쓸 수 없고, 취소 알림도 보내지 않는다.
- **연속 실패:** 시작에 연속으로 실패하면 1·2·4…60초 동안 새 시작을 하지 않고 바로 "실행 안 됨"으로 거절한다(기다리게 하지 않음).
- **`per_conversation` 상한:** 자리가 없으면 가장 오래 쉰 유휴 프로세스를 닫는다. 모두 호출 중이면 "this server has no free session; try again"(`FAILED`, 실행 안 됨)이다.
- **취소된 요청의 응답**은 버린다. 서버가 보낸 `ping`에는 `{}`로, 다른 요청에는 `-32601`로 답하고, 알림은 무시한다.
- **사양 위반:** JSON이 아닌 줄, JSON-RPC가 아닌 메시지, 보내지 않은 id에 대한 응답은 그 프로세스를 닫는다. 진행 중이던 호출은 결과 불명이다.

**검증:**
- 가짜 stdio 서버(프로세스 없이)로 34개 테스트.
- 실제 자식 프로세스로 Application 전체 경로 3개.
- 실제 server sandbox 안에서 Application 전체 경로(호스트 게이트) 2개: 서버가 본 환경 변수, 종료 후 프로세스가 남지 않음, 데이터 폴더를 쓰기 폴더로 지정하면 거부.
- conformance 게이트는 HTTP만 본다(§2.3). stdio는 위 테스트가 맡는다.

**Calliope 실측 (이 호스트, 2026-10-15; AG4 전 Calliope, `per_conversation`):**
- §5.1의 `mcp.json`으로 `Application.open(sandbox=True)`를 했다. 자체 검사와 목록 세션을 포함해 1.7초에 Tool 9개를 등록했고, `show_game` 화면도 읽었다.
- 각본 모델의 `open_game` → `ply`가 Dispatcher를 거쳐 둘 다 SUCCEEDED였다(3.4초).
- 닫는 데 0.25초 걸렸다.
- Calliope의 게임 id 변경(AG4)이 병합되면 `shared`로 수락 테스트를 한다(§8).

### 10.4 개정 구현 — stdio의 쓰기 큐와 `ping` 확인 (2026-10-16)

§5.5 개정과 §11.4대로 구현했다(`mcp/stdio.py`).
- **쓰기 큐:** 세션마다 `asyncio.Queue` 하나와 쓰기 작업 하나를 둔다. 요청, 알림, `ping`, 서버의 `ping`에 대한 답, `initialize`까지 모두 이 큐로 쓴다.
- **승인:** `READY` 확인과 큐 넣기는 한 동기 구간이다. 승인된 요청은 Stop이 와도 순서대로 쓰이고 취소 알림이 그 뒤에 간다.
  - 쓰기가 시간 제한 안에 끝나지 않으면(파이프가 막힘) 세션을 닫는다(PR #34 B2 그대로).
- **확인:** 취소는 알림과 `ping`을 차례로 큐에 넣고 `CHECKING`으로 둔다. 마감(`CHECK_SECONDS`, 10초)은 처음 들어간 때부터 세고 늘지 않는다.
  - 가장 최근 `ping`의 응답(오류 포함)으로 `READY`가 된다. 그때 답 없는 이전 `ping`은 잊는다. 그래서 `busy`가 남지 않고, 유휴 정리를 막지도 않는다.
  - 취소된 요청이나 늦은 `ping`의 응답은 기다리는 것이 없으므로 버린다.
  - **마감 판정은 응답을 읽은 시각으로 한다(PR #36 검수 B1).** 리더가 `ping` 응답을 읽은 시각을 기록하고, 확인 작업은 그 시각이 마감 안일 때만 `READY`로 둔다. 확인 작업이 늦게 돌더라도 마감 뒤에 읽힌 응답으로는 살아나지 않는다.
- **가짜 서버 테스트(새로 쓴 것):**
  - 취소 → 알림 → `ping` 순서와 프로세스 유지
  - 늦은 응답 버림, `ping` 무응답 시 종료와 다른 대화 호출의 결과 불명, 오류 `ping` 응답도 살아 있음
  - `CHECKING` 중 새 호출 대기, 추가 취소의 새 `ping`과 최신 것만 인정, 마감이 늘지 않음
  - 느린 파이프에서 승인된 요청의 순서, 막힌 파이프에서 마감 종료, 승인 전에 멈춘 호출은 아무것도 보내지 않음
- **실측(이 호스트, 실제 server sandbox의 Calliope, `shared`):**
  - 분석 중인 `ply`를 Stop하자 `ping` 응답으로 51 ms 만에 `READY`가 됐다.
  - 다음 호출은 같은 프로세스에서 성공했다(재시작 안내 없음). Calliope가 취소된 분석을 끝낸 뒤 처리해서 7.9초 걸렸다.
  - 다른 대화의 진행 중 호출도 정상으로 끝났다.
  - 처음 설계라면 이 프로세스는 10초 뒤 종료되고 두 호출 모두 결과 불명이었다.

### 10.5 개정 구현 — HTTP 취소 알림 (2026-10-16)

§5.5의 HTTP 계약대로 구현했다(`mcp/client.py`의 `HttpChannel`).
- **언제 보내나:** 요청을 보낸 뒤 Stop(작업 취소)이나 읽기·쓰기 시간 초과(`ReadTimeout`, `WriteTimeout`)로 기다리기를 멈추면 보낸다.
- **언제 보내지 않나:** `initialize`, 그리고 **보내지 않은 요청**(PR #37 검수 B1).
  - httpx의 전송(httpcore)은 요청 헤더를 보내기 시작할 때 trace 이벤트(`…send_request_headers.started`)를 낸다. 그 전(연결 풀 대기, TCP 연결, TLS)에 Stop하면 알림을 보내지 않는다.
  - 이런 이벤트를 내지 않는 전송(테스트처럼 직접 주입한 전송)에서는 보냈는지 알 수 없다. 그럴 때는 보낸 것으로 본다. 받는 쪽은 모르는 id의 취소를 무시하게 되어 있다(사양 Error Handling).
- **`PoolTimeout`은 "실행 안 됨"이다(C1).** 연결 풀 자리를 얻지 못해 요청이 나가지 않았으므로 결과 불명이 아니다. 예전에는 결과 불명으로 분류했다.
- **무엇을 담나:** 요청을 보낼 때의 id, 세션 id, 프로토콜 버전을 그대로 담는다.
- **어떻게 보내나:** 백그라운드 작업이 `CANCEL_SECONDS`(5초) 안에서 보내고 응답 본문은 읽지 않는다. 어떤 결과든 판정에 영향이 없다. 채널을 닫을 때는 남은 알림을 그 제한 안에서 기다린다.
- **테스트(가짜 HTTP 서버 8개, 실제 소켓 서버 1개):**
  - Stop 때 그 요청의 id·원래 세션·판 헤더로 알림이 간다.
  - 읽기 시간 초과 때 "timed out" 알림이 간다.
  - 세션이 갱신돼도 원래 세션으로 간다.
  - Stop은 느린 알림을 기다리지 않고, 닫기는 제한 시간까지만 기다린다.
  - 알림이 실패해도 판정이 같다.
  - `initialize`와 연결하지 못한 요청은 취소하지 않는다.
  - 실제 소켓의 서버(연결 1개 제한)에서, 연결 풀을 기다리던 요청의 Stop은 알림이 없고, 보낸 요청의 Stop은 알림이 간다.
  - `PoolTimeout`은 "실행 안 됨"이다.
- **공식 SDK와의 실측 (이 호스트, MCP Python SDK 2.3의 Streamable HTTP 서버, 느린 Tool):**
  - HIMARI가 Stop하자 서버의 Tool 작업이 취소됐다(`CancelledError`).
  - 같은 실험을 알림 없이(변경 전처럼 연결만 끊고) 하면 서버는 Tool을 끝까지 실행했다.
  - 사양대로, 연결이 끊긴 것은 취소가 아니다.

## 11. 개정: Stop은 최선 노력 취소, 확인은 `ping`으로 (2026-10-16, PR #35 병합)

**사용자 결정(2026-10-16):** 표준 방식과 범용성을 우선한다. 그러기 위해 이전 결정을 고쳐도 된다.

### 11.1 무엇이 잘못됐나

§5.5의 처음 설계(PR #29 검수 B3·B6)는 이랬다.
- 취소한 요청마다 "응답이 오기를" 10초 기다린다.
- 모두 응답하면 `READY`로 돌아가고, 하나라도 응답하지 않으면 프로세스를 종료한다.
- 그 바탕에는 2026-10-15 결정 "취소 알림 후, 안 끝나면 프로세스를 종료"가 있었다.

그런데 사양은 반대로 정한다.
- 취소 알림을 받은 쪽은 **취소된 요청에 응답하지 않아야 한다**(Cancellation 4항, SHOULD).
- 보낸 쪽은 그 뒤에 오는 응답을 **무시해야 한다**(6항, SHOULD).
- 받은 쪽은 이미 끝났거나 멈출 수 없는 작업이면 **취소를 무시할 수 있다**(5항, MAY).

그러니 클라이언트는 취소된 작업이 정말 끝났는지 알 수 없다. 사양을 지키는 서버에서는 "모두 응답하면 `READY`"가 일어나지 않고, **Stop이나 시간 초과가 날 때마다 10초 뒤 프로세스가 종료된다.**

HTTP 쪽도 표준과 어긋나 있었다. HIMARI는 Stop 때 연결만 끊는데, 사양은 "연결이 끊긴 것을 취소로 보지 말라(SHOULD NOT), 취소하려면 취소 알림을 보내라(SHOULD)"고 한다(Transports §Streamable HTTP).

### 11.2 확인한 것 (2026-10-16, 이 호스트)

- **Calliope(MCP Python SDK 2.3)에 `ply`를 보내고 0.5초 뒤 취소했다.**
  - 15초 동안 그 요청에 대한 응답이 없었다.
  - 이어서 보낸 `ping`에는 바로 답했다.
  - 처음 설계라면 이 프로세스는 10초 뒤 종료된다. `shared`라면 그 순간 다른 대화의 진행 중 호출도 결과 불명이 되고, 그사이 새 호출은 약 14초 멈춘다.
- **Calliope는 Tool을 멈출 수 없는 작업 스레드에서 하나씩 실행한다.** 그래서 취소된 분석은 끝까지 돈다. 사양 5항의 "멈출 수 없는 작업"이고, 그 결과가 캐시에 저장되고 예산도 쓴다. 이것이 최선 노력 취소의 현실이다.
- **같은 실행에서 HIMARI와 Calliope의 수락 시나리오는 모두 통과했다**(취소가 없는 경로).
  - shared·per_conversation 두 모드에서, 다른 대화와 다른 프로세스가 같은 게임 id를 썼다.
  - 재시작 뒤에도 같은 id가 통했다.
  - 옛 `g1`은 거부됐다.

### 11.3 고른 방법과 버린 방법

**Stop의 뜻.** 두 가지 중에서 골라야 했다(PR #35 검수 B1).

| | (A) 최선 노력 취소 (고름) | (B) 작업 종료 보장 |
| --- | --- | --- |
| 하는 일 | 취소 알림을 보내고 기다리기를 멈춤 | 취소 뒤 서버를 확실히 멈춤 |
| 사양 | MCP의 취소 그대로 | 사양에 그런 수단이 없음. 프로세스를 죽이는 것이 유일한 방법 |
| HTTP 서버 | 같게 적용 | 적용할 수 없음(남의 서버 프로세스) |
| 이미 생긴 효과 | 되돌릴 수 없음 (둘 다 같음) | 되돌릴 수 없음 |
| `shared`에서 | 다른 대화에 영향 없음 | Stop 한 번이 다른 대화의 호출을 결과 불명으로 만듦 |
| 확실성 계약 | V3-057 그대로(claim된 호출은 결과 불명) | 같음 |

**(A)를 고른다.**
- 다른 MCP 호스트들도 취소 알림을 보내고 기다리기를 멈출 뿐, Stop마다 서버를 다시 띄우지 않는다.
- (B)도 "이미 생긴 효과"는 막지 못한다. 그래서 결과 불명이라는 사실은 바뀌지 않고, 대가만 크다.
- HIMARI의 확실성 계약은 처음부터 (A)를 전제로 맞춰져 있다. 취소된 claim 호출은 결과 불명이고, `read`는 늦게 끝나도 해가 없는 Tool에만 준다.

**stdio에서 언제 프로세스를 끝내나.**

| 방법 | 사양 | 응답 없는 서버를 잡는가 | 정상 서버에서 Stop의 대가 |
| --- | --- | --- | --- |
| (가) 처음 설계: 취소한 요청의 응답을 기다림 | 4항과 어긋남 | 잡음 | **매번 종료·재시작** |
| (나) 취소 즉시 `READY`, 확인 없음 | 맞음 | **못 잡음.** `ping`에도 답하지 않는 서버가 계속 쓰임 | 없음 |
| (다) 취소 즉시 종료 | 맞음 | 잡음 | 매번 종료·재시작 |
| **(라) 취소 뒤 `ping` (고름)** | 맞음 (ping 사양 그대로) | **`ping`에도 답하지 않는 서버**를 잡음 | `ping` 왕복 시간만큼 새 호출이 기다림 |

**(라)를 고른다.** `ping`은 사양이 "상대가 아직 응답하는지 확인하는" 용도로 둔 것이다. 받는 쪽은 즉시 답해야 하고(MUST), 답이 없으면 연결을 끊어도 된다(MAY).
- (라)가 잡는 것은 "`ping`에 답하지 않는 서버"까지다.
- `ping`에는 답하면서 Tool 작업만 멈춘 서버는 못 잡는다. 이 경우는 §9에 남은 질문으로 둔다.

**쓰기 순서 (검수 B2·B3).**
- 승인과 상태 전환이 엇갈리거나, 알림과 `ping`의 순서가 뒤바뀌는 일을 막는다. 방법은 세션의 stdin 쓰기를 **하나의 순서 있는 큐**로만 하는 것이다. 승인은 `READY`인 순간 큐에 넣을 때만 한다.
- 이것은 HIMARI 안의 일이라 서버에 따라 달라지지 않는다. stdin이 한 줄씩 순서대로 읽힌다는 사양(stdio 전송)에만 기댄다.

**두지 않는 것:** 유휴 중의 주기적 `ping`은 두지 않는다. 사양은 주기적 `ping`을 권하지만(SHOULD) 주기와 시간 제한은 구현에 맡긴다.
- 쓰이지 않는 프로세스는 유휴 정리로 닫힌다.
- 쓰이는 프로세스가 응답하지 않으면 그 호출이 시간 초과로 취소되고, 이어서 이 확인이 돈다.
- 죽은 프로세스는 stdout EOF로 바로 드러난다.

### 11.4 구현과 검증 (다음 PR)

**구현:**
- **`mcp/stdio.py`:**
  - 세션의 쓰기 큐(하나의 쓰기 작업이 순서대로 씀)를 둔다.
  - 승인은 `READY`인 동기 구간에서만 한다.
  - `DRAINING`을 `CHECKING`으로 바꾸고, 취소 목록 대신 "가장 최근 `ping`과 그 마감"을 둔다.
  - 취소된 요청의 늦은 응답은 버린다.
  - B2의 쓰기 시간 제한은 "승인된 요청이 큐를 지나 다 쓰일 때까지"에 그대로 적용한다.
- **`mcp/client.py`(HTTP):** §5.5의 계약대로 한다. 보낼 때 id·세션 id·프로토콜 버전을 기억하고, Stop이나 시간 초과 때 시간 제한(5초)이 있는 백그라운드 작업으로 `notifications/cancelled`를 POST한다. 결과는 판정에 영향이 없다. `initialize`와 보내지 못한 요청은 취소하지 않는다.
- **README:** 운영자 안내를 고친다. Stop은 최선 노력 취소라 서버에서 작업이 끝날 수 있다는 것, 프로세스는 응답하지 않을 때만 다시 띄운다는 것.
- **그대로인 것:** B1 할당 잠금, B3 종료 동기화, `initialize`는 취소하지 않음.

**테스트 (모두 결정적으로):**
- **가짜 서버(취소된 요청에 응답하지 않음 — 사양의 기본):** 취소 → `ping` 응답 → 같은 프로세스로 `READY`. 다음 호출은 같은 프로세스에 쓰이고 재시작 안내가 없다.
- **가짜 서버(`ping`에도 답하지 않음):** 마감(테스트에서는 줄임) 뒤 종료. 같은 프로세스의 다른 미응답 호출은 결과 불명이고, 다음 호출은 새 프로세스와 안내로 간다.
- **가짜 서버(취소된 요청에도 늦게 응답함):** 그 응답은 버리고, `ping`으로 `READY`.
- **순서:**
  - 서버가 받은 줄의 순서가 "요청 … 취소 알림 → `ping`"이다.
  - 추가 취소는 "알림 → 새 `ping`"이고, 이전 `ping`의 응답으로는 `READY`가 되지 않는다.
  - 쓰기가 느린 서버에서 승인된 요청이 쓰이는 도중에 다른 요청을 취소하면, 승인된 요청은 그대로 쓰이고(진행 중으로 다룸) 새 요청은 `READY`까지 승인되지 않는다.
  - 알림이나 `ping` 쓰기가 실패하거나 막히면 종료한다.
- **HTTP:**
  - Stop과 시간 초과 때 가짜 HTTP 서버가 `notifications/cancelled`를 받는다. 그 안에 원래 요청의 id, 원래 세션 id, 프로토콜 버전이 들어 있다. 세션이 그사이 갱신돼도 원래 세션 id로 간다.
  - Stop은 알림 전송을 기다리지 않는다(느린 서버에서도 바로 끝남).
  - 알림이 실패하거나 404를 받아도 호출 결과는 같다.
  - 연결 전에 실패한 요청과 `initialize`에는 알림이 없다.
- **고정 마감:** `CHECKING` 중의 추가 취소가 마감을 늘리지 않는다. 처음부터 10초(테스트에서는 줄임)에 응답이 없으면 종료한다.
- **오류 `ping` 응답:** `-32601`로 답하는 서버도 `READY`로 돌아간다.
- **실제 Calliope(호스트):** `ply`를 분석하는 중에 Stop한다. 그러면:
  - 프로세스가 유지되고(재시작 안내 없음),
  - 다음 호출이 같은 프로세스에서 성공한다(앞의 분석이 끝나기를 서버 안에서 기다릴 수 있음),
  - `shared`에서 다른 대화의 진행 중 호출이 결과 불명이 되지 않는다.
