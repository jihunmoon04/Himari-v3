# 서버 줄이기: runner와 stdio MCP 빼기 (V3-P109 설계 v0.1)

상태: **Confirmed**(2026-11-24). 소유자 결정 Q1~Q3(§2), 나머지는 소유자가 맡긴 구현자 판단(§3, "설계 후 구현까지 진행")이다. 구현은 §8.

> 소유자(2026-11-24), 서버를 Linux 밖(Windows PC에서 서버와 앱을 함께)에서도 돌릴 수 있게 하는 방향을 논의한 뒤:
> - 파일 Tool의 읽기 범위를 대화 폴더 안으로 좁혀도 된다(Q1).
> - Calliope는 쓰지 않으니 서버에서 지우는 편이 좋다. 날씨·지도 같은 것은 나중에 붙일 수 있다(Q2).
> - `read_document`는 있어야 한다(Q3).

앞선 결정
- **V3-P74** 머신 Workspace: 파일 연산은 사용자(`coder`)로 도는 `himari-runner`가 한다(Unix 소켓 + `SO_PEERCRED`, 파일은 디스크립터 전달). 읽기는 `coder`가 읽을 수 있는 곳 전부(숨김 목록 제외), 쓰기는 대화 폴더(`<runner의 himari_home>/conversations/<id>`).
- **V3-P100·V3-P106:** 붙인 Workspace 쓰기, `git_push`, `run_command`, 붙인·등록한 Workspace를 뺐다. runner에 남은 일은 파일 연산뿐이다(프로토콜 7).
- **V3-P59** stdio MCP: HIMARI가 stdio 서버를 server sandbox(bubblewrap + Landlock) 안에서 직접 띄운다.
- **V3-P66·V3-P70·V3-P101:** 업로드는 대화 폴더의 `uploads/`에 놓인다. `read_document`와 `present_file`은 `machine` Environment의 Tool이다.

## 1. 왜

V3-P106으로 HIMARI는 반려 에이전트가 됐지만, 서버에는 작업 에이전트 시절의 Linux 전용 장치가 남아 있다.

| 장치 | Linux에 묶인 것 | 반려에 필요한가 |
| --- | --- | --- |
| `himari-runner` | Unix 소켓, `SO_PEERCRED`, `SCM_RIGHTS`, `openat2`, `/proc/self/fd`, `renameat2`, `pwd`, systemd 단위 | 아니다. 서버의 다른 사용자 파일을 읽고 쓰려고 있던 것이다 |
| stdio MCP 실행 | server sandbox의 stdio 설정 | 지금 쓰는 서버가 없다(Calliope는 안 씀) |
| 문서·이미지 worker의 sandbox | bubblewrap + Landlock | 그대로 둔다(§4.3) |

runner를 빼면 대화 폴더는 HIMARI가 직접 소유하고, 파일 Tool은 프로세스 안에서 OS와 상관없는 코드로 돈다. 이것은 나중의 로컬(Windows) 구성으로 가는 첫 단계다(§7).

## 2. 소유자 결정

| | 질문 | 결정 |
| --- | --- | --- |
| Q1 | 파일 Tool이 읽는 범위 | **대화 폴더 안만.** 쓰기와 같다 |
| Q2 | stdio MCP | **뺀다.** HTTP MCP는 남긴다. 날씨·장소처럼 반려의 핵심이 될 기능은 MCP가 아니라 내장 Tool로 따로 설계한다(별도 패킷) |
| Q3 | `read_document` | **남긴다** |

## 3. 구현자 판단 (소유자가 맡김)

| | 정한 것 | 이유 |
| --- | --- | --- |
| D1 | 대화 폴더는 **`<HIMARI_DATA_ROOT>/conversations/<conversation_id>`**. 폴더는 0700, 새 파일은 0600(POSIX) | HIMARI 사용자만 닿는다. 데이터 폴더는 이미 0700이다 |
| D2 | Tool id(`machine/*`)와 Environment 이름(`machine`), Binding의 `cwd_relative`(`conversations/<id>`)는 그대로 | 옛 대화의 기록·Binding이 그대로 맞는다. 스키마 변경 없음 |
| D3 | 모델이 보는 경로는 **실제 절대 경로**(대화 폴더 아래). 상대 경로는 대화 폴더 기준. **`~`는 없앤다**(사용자 홈이라는 개념이 없어짐) | 지금처럼 Current State의 작업 위치와 결과의 경로가 같다 |
| D4 | 대화 폴더 밖의 경로는 읽기·쓰기 모두 **DENIED**(`outside`: "outside this conversation's folder: you can read and write only there") | Q1 |
| D5 | 대화 폴더 안의 **링크(심볼릭 링크·Windows 재분석 지점)는 따라가지 않고 거부**한다(`links`). 경로의 각 부분을 `lstat`으로 확인 | 폴더에 쓰는 것은 HIMARI뿐이고, 링크를 만드는 Tool은 없다. 그래도 남은 링크가 밖을 가리키지 못하게 한다 |
| D6 | 쓰기는 지금처럼 **임시 파일 + 이름 바꾸기**(원자적), `edit_file`은 읽었을 때의 정체(장치·inode·크기·수정 시각)를 쓰기 직전에 다시 확인(`changed`) | 계약 유지 |
| D7 | 업로드의 "덮어쓰지 않는 이름 바꾸기": POSIX는 `link` 뒤 임시 파일 지우기, Windows는 `rename`(대상이 있으면 실패) | `renameat2`(Linux 전용) 대신. 두 경우 모두 폴더 잠금 안에서 한다 |
| D8 | 블로킹 파일 연산은 worker 스레드에서 돌고, **취소돼도 스레드가 끝날 때까지 잠금을 놓지 않는다**(지금의 `run_blocking`) | runner의 "취소된 쓰기는 끝난 뒤에 닫는다"(PR #87 C1)와 같은 보장 |
| D9 | `HIMARI_MACHINE_SOCKET`은 **없앤다**. 설정돼 있으면 시작 로그에 "더 이상 쓰지 않음"만 남긴다. `HIMARI_MACHINE_PRINCIPALS`(누구의 새 대화가 폴더를 갖는지)는 남는다. 빈 목록이면 지금의 "소켓 없음"처럼 대화만 한다 | 설정 하나가 빠지고, 끄는 방법은 남는다 |
| D10 | `mcp.json`에 `command`(stdio) 서버가 남아 있어도 **시작은 실패하지 않는다.** 그 서버만 빠지고 상태에 이유("stdio servers are not run since V3-P109; serve it over HTTP")가 보인다 | 설정 하나 때문에 반려가 멈추지 않게 |
| D11 | 이미지·문서 worker의 server sandbox는 **그대로**(Linux 서버 구성). sandbox 모듈의 일반 server profile(`read_only`·`writable`)은 남기고, stdio를 설명하던 문장만 고친다 | Q3, 이 패킷은 줄이기만 한다 |

## 4. 바뀌는 것

### 4.1 `machine` Environment

- `MachineEnvironment(root, files=..., documents=...)`: `root`는 `<data_root>/conversations`. runner 클라이언트 대신 프로세스 안의 **`ConversationFolders`**(새 모듈 `environment/machine/folders.py`)를 쓴다.
- `observe`는 늘 ONLINE이고, `readable`은 "only this conversation's folder"다. runner에 묻던 `hello`는 없다.
- `MachineUnavailable`은 폴더를 만들거나 열 수 없을 때(디스크 오류)만 난다.
- 오류 분류(`not_found`·`outside`·`is_directory`·`not_directory`·`not_regular`·`exists`·`access`·`too_large`·`changed`·`invalid`·`links`·`other`)는 `environment/machine/errors.py`로 옮긴다. `outside`와 `access`는 DENIED, 나머지는 FAILED(지금과 같음). runner의 `timed_out`(느린 마운트)은 없어진다.

### 4.2 Tool (모델에게 보이는 계약 변경)

| Tool | 지금 | 바뀐 뒤 |
| --- | --- | --- |
| `machine/list_files` | 서버의 어느 폴더든(숨김 제외) | 대화 폴더 안만 |
| `machine/read_file` | `coder`가 읽을 수 있는 곳 전부 | 대화 폴더 안만 |
| `machine/write_file`·`edit_file`·`delete_file` | 대화 폴더 안만 | 그대로 |
| `machine/present_file` | 읽을 수 있는 파일 전부 | 대화 폴더 안의 파일 |
| `machine/read_document` | 읽을 수 있는 파일 전부 | 대화 폴더 안의 파일 |

설명 글에서 "on this server", "~ (the user's home)", "hidden locations"를 빼고, "Paths are absolute inside this conversation's folder, or relative to it"으로 바꾼다. 결과 형식(목록·읽기 부분·크기 한도)은 그대로다.

### 4.3 MCP

- `mcp/stdio.py`와 stdio 실행(`StdioServer`·`StdioChannel`·`ListingChannel`·`LauncherFor`), Bootstrap의 `_server_launchers`, `Application.open(mcp_launcher_for=…)`를 지운다.
- `mcp.json`의 stdio 키(`command`·`args`·`cwd`·`env`·`env_from`·`read_only`·`writable`·`processes`)는 읽기만 하고 그 서버를 빼는 이유로 쓴다(D10).
- HTTP MCP(Tool·프롬프트·리소스, 취소, 대화마다 켬/끔)는 그대로다. `Channel`의 `take_restart`·`release`는 일반 계약으로 남는다(HTTP는 늘 False·아무것도 안 함).
- CI의 `mcp-conformance` 워크플로에서 stdio 경로를 뺀다.

### 4.4 지우는 것

- `src/himari/runner/`(`himari-runner` 진입점 포함), `environment/machine/client.py`
- `pyproject.toml`의 `himari-runner` 스크립트
- 테스트: `tests/runner/`, `tests/runnerkit.py`, `tests/mcp/test_mcp_stdio.py`, `tests/bootstrap/test_stdio_application.py`, `tests/mcp_stdio_fake.py`, `tests/stdio_server/`, `tests/sandbox_host/test_stdio_host.py`
- `scripts/check_imports.py`의 runner 규칙

## 5. 운영 서버 옮기기 (배포 때)

이 패킷의 배포는 따로 지시받아 한다. 그때의 절차:

1. 멈추기 전 확인: 진행 중인 Run 0, `/home/coder/HIMARI/conversations`의 폴더 수와 크기.
2. `himari-v3`·`himari-runner`를 멈춘다.
3. `/home/coder/HIMARI/conversations/*`를 `/srv/himari-v3/conversations/`로 복사한다(`rsync -a --no-links`, 소유자 HIMARI 서비스 사용자, 폴더 0700·파일 0600). 링크는 옮기지 않는다(D5). 원본은 지우지 않고 남긴다(되돌리기용, 소유자가 나중에 지움).
4. `web.env`에서 `HIMARI_MACHINE_SOCKET`을 지운다. `mcp.json`에서 `calliope`를 지운다.
5. `himari-runner.service`·`himari-runner.socket`을 끄고(`disable --now`) 단위 파일과 `/etc/himari-runner`를 백업한 뒤 지운다.
6. 새 판으로 `himari-v3`를 시작한다. 확인: 시작 로그의 대화 폴더 줄, 업로드 하나, `machine/read_file`·`present_file` 하나, Tool servers 화면에 Calliope 없음.

알려진 영향:
- 옛 기록의 경로(`/home/coder/HIMARI/conversations/<id>/…`)를 모델이 다시 쓰면 `outside`로 거부된다. Current State의 작업 위치가 새 경로라 모델은 다음 호출에서 고친다.
- 업로드 기록의 `workspace_path`가 옛 경로면, 다음에 그 파일을 첨부할 때 새 폴더에 다시 놓인다(`holds`가 거짓 → 새 이름, 기록 갱신). 스키마·데이터 변경 없음.
- 사용자가 SSH로 `~/HIMARI`에서 보던 파일은 이제 Web에서 내려받는다(`present_file`).

## 6. 시험

계약 변경(모델에게 보이는 Tool 범위, MCP 설정 의미, Bootstrap 인자)이므로 **전체 pytest**(V3-038).

- `tests/environment/test_machine.py`를 새 구현으로 다시 쓴다: 대화 폴더 밖 읽기·쓰기 거부, `..`·절대 경로 탈출, 링크 거부(중간·마지막 부분), 원자적 쓰기와 권한, `edit_file`의 `changed`, 목록 정렬·잘림, 업로드 단계(복사·빈 이름·덮어쓰지 않는 확정·버리기·`holds`), 폴더 지우기(안의 링크는 따라가지 않음), 취소된 쓰기가 끝날 때까지 잠금 유지.
- runner를 쓰던 Bootstrap·Web 시험(`test_files`·`test_trash`·`test_machine_app`·`test_documents_app`·`test_images_app`·`test_screen_app`·`test_web*`)은 데이터 폴더 안의 대화 폴더로 바꾼다. "runner 없이 열기"를 보던 시험은 `machine_principals=()`로 바꾼다.
- MCP: `command` 서버가 시작을 막지 않고 이유와 함께 빠지는지.
- import 경계 검사(`scripts/check_imports.py`), mypy, Web 빌드는 그대로 통과해야 한다.

## 7. 남는 Linux 의존과 다음 단계

이 패킷 뒤에 서버에 남는 Linux 의존:
- `data/sqlite.py`·`data/blobs.py`·`bootstrap/operator.py`·`mcp/config.py`·`documents/__init__.py`의 파일 잠금(`fcntl`)과 소유자·권한 검사(`geteuid`, 0600/0700), `O_NOFOLLOW`·`O_DIRECTORY`
- 문서·이미지·Tool 스키마 worker의 `resource.setrlimit`
- 문서·이미지 worker의 server sandbox(bubblewrap + Landlock)

다음 패킷(로컬 구성)에서 이것들을 플랫폼 계층 하나로 모으고, Windows에서는 Job Object로 worker를 묶는다. 새 `ConversationFolders`는 처음부터 `os`의 이식 가능한 호출만 쓴다(`os.pread`·`dir_fd`·`/proc` 없음).

## 8. 구현

- 새 모듈: `environment/machine/folders.py`(`ConversationFolders`), `environment/machine/errors.py`(오류 분류). `MachineEnvironment(root)`·`machine/*` Tool은 이것을 `run_blocking`으로 부른다.
- Bootstrap: `CONVERSATIONS_NAME = "conversations"`, `machine_principals`가 비어 있지 않으면 `MachineEnvironment(<data>/conversations)`. `machine_socket`·`mcp_launcher_for`·`_server_launchers` 삭제.
- Web 설정: `HIMARI_MACHINE_SOCKET`은 `machine_socket_ignored`로만 읽어 시작 때 알림.
- MCP: `McpServerConfig.refused`(stdio 항목), `RESTART_NOTICE`는 `client.py`로.
- 시험: `tests/environment/test_machine.py` 다시 씀(경로 탈출·링크·권한·업로드·취소된 쓰기), Bootstrap·Web 시험은 데이터 폴더 안의 대화 폴더로, MCP의 stdio 시험은 지우거나 HTTP로(프롬프트 목록 시간 초과, stdio 항목이 빠지는 것).
- 확인(WSL, Python 3.12): 전체 pytest 2779 통과·77 건너뜀, `mypy`(strict, src+tests) 통과, `scripts/check_imports.py` 통과. 실호스트 sandbox 게이트와 운영 배포는 하지 않았다.
- **Codex 검수(2026-11-24) 반영:** P1 링크된 루트를 따라 영구 삭제하던 것(루트를 먼저 `lstat`), P1 Windows에서 호출자 철자를 그대로 쓰던 것(신뢰하는 폴더 경로 + 검사한 부분으로 다시 만듦, 드라이브·스트림 콜론·끝의 점과 공백·장치 이름 거부, 대화 id는 소문자만), P2 폴더 사용자를 비우면 영구 삭제가 폴더를 남기던 것(`Application.folders`는 늘 있음), P2 업로드 이름·임시 파일 검사(정상 `.tmp` 파일만 확정), P2 덮어쓸 때 umask가 권한을 깎던 것(`fchmod`), P3 옛 설계 문서의 안내. 시험을 더했고, Windows 기본 Python에서 `folders.py`만 따로 돌린 스모크(쓰기·읽기·업로드·junction 거부·삭제)도 통과했다.
