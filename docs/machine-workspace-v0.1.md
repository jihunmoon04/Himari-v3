# 머신 Workspace v0.1

> 2026-10-25 · **PROPOSED (V3-P74)** · rev. 1: 설계 초안. 소유자 결정 Q1~Q8 대기(§17).
>
> 소유자 방향(2026-10-24, 대화에서 정함):
> - 개발용 Workspace를 따로 만들지 않는다. 기본 Workspace에서 git·C·Python·Java 같은 일반 개발이 되게 한다.
> - Workspace에 제한이 적어야 한다. 금지된 위치만 빼면 어느 폴더든 Workspace가 될 수 있다. rclone으로 마운트한 Google Drive 폴더도 일반 Workspace처럼 쓴다.
> - **읽기는 기본적으로 넓게 허용하고, 쓸 수 있는 곳을 정하는 것이 Workspace다.**
> - 명령은 서버 사용자 `coder`의 권한으로 돈다(HIMARI 서비스는 `himari-v3`로 돈다).
> - 명령의 네트워크는 허용 목록(패키지 저장소·git 서버)만. git push 인증 정보는 Sandbox 밖에 두고, HIMARI의 Tool이 push한다(강제 push·브랜치 삭제 거부).
> - 이 방식은 소유자 principal에게만 준다. 다른 principal은 지금의 격리된 기본 Workspace를 쓴다.
> - 일반 승인 절차는 두지 않는다(Sandbox가 피해 범위를 막는다). 필요해지면 `REQUIRE_USER` 승인을 나중에 더한다.
>
> 앞선 결정:
> - V3-008~012·V3-021·V3-028: Environment가 Workspace 사용법을 정하고, Workspace는 ID로 식별하며, Registry와 Binding을 나눈다. Workspace 전용 모듈은 만들지 않는다.
> - V3-030·V3-033·V3-035: HIMARI가 주는 기본 Workspace, 사용자별 Root, 대화마다 기본 폴더.
> - V3-032·V3-034: `run_command`는 OS Sandbox 안에서만. 승인·복제 격리는 두지 않는다.
> - V3-036·V3-037: 명령의 네트워크는 기본 차단, 조건부 허용은 별도 정책 변경과 검증이 필요하다. **이 문서가 그 정책 변경이다**(§11).
> - V3-050: Sandbox는 시작 시 실제로 탈출을 시도해 보는 자체 검사로 켠다(§13에서 이 프로파일의 검사를 정한다).
> - V3-051: 명령·파일 Tool 한도.
> - V3-P40·V3-P41: Inside 파일 Tool과 Sandbox. 모델에게는 `/workspace/...`만 보이고, 빈 루트에 Workspace 하나를 붙인다.

## 1. 왜, 그리고 범위

지금 HIMARI의 Workspace는 사용자마다 하나, 서버의 HIMARI 데이터 폴더 안에 있다. 모델은 그 안만 보고 쓰며, 명령은 네트워크 없이 돈다. 그래서 다음이 안 된다.
- 소유자의 기존 폴더(프로젝트, 강의 자료, Drive)에서 작업하기
- `pip install`, `git clone`, Maven·Gradle 의존성 받기
- 명령을 실행하기 전에 폴더 전체를 훑는 검사(20만 항목 한도) 때문에 큰 프로젝트에서 명령을 돌리기

| 포함 (이 패킷) | 제외 (이후) |
| --- | --- |
| 새 Environment `machine`: `coder`로 도는 실행기(runner)와 HIMARI 사이의 계약(§4, §5) | 소유자 외 principal에게 열기 |
| 넓은 읽기 + 붙인 Workspace에만 쓰기 Sandbox 프로파일(§6), 숨김 목록(§7) | 사용자 PC 같은 다른 기계의 runner(같은 계약으로 나중에 가능, §4.4) |
| 파일 Tool·`run_command`·업로드·파일 보이기·문서·이미지를 runner로(§8, §9) | 오래 도는 프로세스(개발 서버, Q-11) |
| Workspace 등록·붙이기·떼기(Data v13, Web, CLI, Current State)(§10) | 모델이 Workspace를 붙여 달라고 요청하고 한 번에 승인하기(Q4) |
| 허용 목록 네트워크 프록시(§11) | 디스크 사용량 한도 |
| git push Tool(§12) | 기존 대화를 새 방식으로 옮기기(Q1) |
| 자원 한도(§6.5), 자체 검사(§13), Drive 마운트 다루기(§14) | 명령마다 따로 정하는 자원 한도 |

## 2. 다른 호스트 (2026년 기준, 제가 아는 범위)

| 호스트 | 읽기 | 쓰기 | 네트워크 | 인증 정보 | 승인 |
| --- | --- | --- | --- | --- | --- |
| **Codex CLI** (`workspace-write`) | 거의 전체 | 작업 폴더 + 추가한 폴더 + `/tmp` | 기본 차단 | 사용자 환경 | Sandbox 안은 묻지 않는 모드가 흔함 |
| **Claude Code** (Sandbox) | 거의 전체, 거부 목록 | 작업 폴더(+ 추가 폴더) | 밖의 프록시가 도메인 허용 목록으로 거름 | 거부 목록으로 가림 | Sandbox 안은 자동 허용 가능 |
| **Codex 클라우드·Copilot 코딩 에이전트** | 컨테이너 안 저장소 | 컨테이너 안 저장소 | 허용 목록 | push는 컨테이너 밖, 범위를 좁힌 토큰 | 결과는 PR로 |

공통점은 셋이다. **읽기는 넓게, 쓰기는 작업 폴더만, 네트워크는 허용 목록, 인증 정보는 Sandbox 밖.** 승인 없이 돌리는 모드는 Sandbox가 피해 범위를 막을 때만 권한다. 승인도 Sandbox도 없는 모드는 버려도 되는 컨테이너나 VM에서만 쓰라고 안내한다.

HIMARI도 이 공통 방식을 따른다. 다른 점은 하나다. 원격 컨테이너가 아니라 **소유자 계정(`coder`)의 실제 파일**에서 일한다. 그래서 숨김 목록(§7)과 쓰기 범위가 Codex CLI·Claude Code와 같은 역할을 한다.

## 3. 개념: 읽기 범위와 Workspace

### 3.1 읽기 범위

- `coder`가 읽을 수 있는 곳은 **숨김 목록(§7)만 빼고 모두** 읽는다.
- 명령(`cat`, `grep`, 컴파일러)과 파일 Tool(`read_file`, `list_files`, `read_document`, `present_file`) 모두 같다.
- 경로는 **실제 경로**다(`/home/coder/projects/foo/main.c`). `/workspace` 같은 가상 경로는 이 Environment에 없다(§8.1).

### 3.2 Workspace

Workspace는 **대화가 쓸 수 있는 폴더**다. 대화에 붙인 Workspace가 정하는 것은 넷이다.

| 정하는 것 | 내용 |
| --- | --- |
| 쓰기 범위 | 붙인 Workspace 폴더 안에서만 파일을 만들고, 고치고, 지운다. 명령도 같다 |
| 작업 위치(cwd) | 명령이 시작되고, 파일 Tool의 상대 경로가 기준으로 삼는 폴더 |
| 모델에게 주는 정보 | Current State에 "이 대화의 Workspace"로 실린다(§10.5) |
| 동시 실행 단위 | 같은 Workspace에 쓰는 일(파일 쓰기·명령)은 한 번에 하나(지금의 `workspace_busy`) |

**대화 폴더**는 Workspace를 붙이지 않아도 늘 있다. 소유자의 HIMARI 폴더(Q2, 권고 `/home/coder/HIMARI`) 아래 `conversations/<conversation_id>/`이고, 지금의 대화별 기본 폴더와 같은 역할이다(V3-033). HIMARI 폴더 전체가 하나의 기본 Workspace로 모든 대화에 붙어 있다. 그래서 지금처럼 같은 사용자의 다른 대화 폴더에도 쓸 수 있다.

예:
- Workspace를 붙이지 않은 새 대화: 대화 폴더(HIMARI 폴더)에만 쓴다. 다른 곳은 읽기만 한다.
- `~/projects/foo`를 붙인 대화: 그 폴더에도 쓰고, 작업 위치가 그 폴더로 바뀐다(§10.4).
- Drive의 `대학/`까지 붙인 대화: 세 곳에 쓴다.

### 3.3 막지 않는 것, 막는 것

- **막지 않는다:** Workspace로 등록할 폴더(숨김 목록 안만 아니면 어디든), 명령 종류, Workspace 안의 심볼릭 링크·하드링크·마운트, 큰 폴더.
- **막는다:** 숨김 목록 읽기·쓰기, 붙이지 않은 폴더에 쓰기, 허용 목록 밖으로의 네트워크, 호스트의 소켓(사용자 systemd·D-Bus 등. 이것으로 Sandbox 밖에서 명령을 띄울 수 있다), HIMARI의 데이터와 비밀.

## 4. 구조

### 4.1 runner

```
브라우저/CLI ──▶ HIMARI 서버 (himari-v3)
                   │  Environment "machine" (클라이언트)
                   │  Unix 소켓, JSON-RPC 한 줄씩 + 파일 디스크립터 전달
                   ▼
                 himari-runner (coder, systemd)
                   ├─ 파일 연산: 숨김 목록·쓰기 범위 검사
                   ├─ 명령: bubblewrap + Landlock, 머신 프로파일 (§6)
                   ├─ 네트워크 프록시: 허용 목록 (§11)
                   └─ git push (§12)
```

- runner는 HIMARI 저장소의 새 최상위 모듈 `himari/runner/`다. 같은 release·venv에서 `python -m himari.runner`로 띄운다. `common`과 `sandbox`만 import한다(import 검사에 추가).
- systemd `himari-runner.service`, `User=coder`. 설정은 `/etc/himari-runner/runner.toml`(root 소유, coder 읽기 전용)이라 runner도 모델도 바꿀 수 없다.
- 소켓은 `/run/himari-runner/runner.sock`. 폴더는 `coder:himari-v3` 0750, 소켓 0660이다. runner는 연결마다 `SO_PEERCRED`로 상대 uid가 설정의 `client_uid`(himari-v3)인지 확인한다.
- runner는 대화나 principal을 모른다. HIMARI가 매 요청에 **이 호출의 쓰기 범위와 작업 위치**를 담아 보낸다. runner는 그 범위를 자기 설정과 다시 대조한다(§5.3).

### 4.2 왜 HIMARI를 coder로 돌리지 않나

HIMARI 서비스를 `coder`로 돌리면 runner가 필요 없다. 하지만 이렇게 된다.
- HIMARI의 DB·비밀·운영자 비밀값이 `coder` 소유가 된다. `coder` 계정에서 도는 다른 도구(개발 도구, 에이전트)가 그것을 읽게 된다.
- 소유자가 정한 지금의 배포 구조(서비스 사용자 분리)가 바뀐다.

runner로 나누면 HIMARI가 뚫려도 runner가 허락하는 일(숨김 목록 밖의 `coder` 권한)까지만 간다. runner가 뚫려도 HIMARI 데이터는 그대로다.

### 4.3 왜 MCP 서버가 아닌가 (표준 우선 검토)

| | runner를 MCP 서버로 | 전용 계약 (권고) |
| --- | --- | --- |
| 표준 | MCP Tool로 보인다 | HIMARI 내부 계약 |
| Workspace 의미 | MCP Tool은 External Tool이라 Workspace가 없다(V3-054). 대화별 쓰기 범위를 넘길 표준 자리가 없다 | Binding·쓰기 범위·작업 위치를 그대로 넘긴다 |
| 파일 바이트 | 업로드 1 GiB, 이미지·문서·파일 보이기를 JSON으로 실어야 한다 | 디스크립터를 넘긴다(§5.2) |
| lock·취소·결과 불명 | MCP 취소는 알림이라 결과가 불명확하다 | 기존 Inside와 같은 규칙 |

Workspace는 HIMARI의 Environment 개념(V3-009)이라, 표준 Tool 프로토콜에 억지로 맞추면 V3-054의 경계를 깬다. 계약 모양은 JSON-RPC 2.0으로 두어 MCP와 비슷하게 읽히게 한다.

### 4.4 새 Environment kind `machine`

- `inside`는 그대로 남는다. 다른 principal과 기존 대화(Q1)가 쓴다.
- `machine`은 새 EnvironmentPlugin이다(`environment/machine/`). Tool 이름은 `machine/read_file`, `machine/run_command` 등이다. 경로 모양(실제 경로)과 범위가 Inside와 달라서, 같은 이름으로 두면 모델이 두 의미를 섞는다.
- 설정 `HIMARI_MACHINE_SOCKET`이 있고 대화의 principal이 `HIMARI_MACHINE_PRINCIPALS`(기본 `operator`)에 있으면, **새 대화**는 `inside` 대신 `machine`을 활성화한다(T0, V3-P43).
- runner가 꺼져 있으면 Tool은 "the machine runner is not reachable"로 실패한다. Inside로 몰래 바꾸지 않는다.
- 같은 계약으로 나중에 다른 기계(소유자의 PC)의 runner를 붙일 수 있다. 그때는 Environment Instance가 runner마다 생긴다(Q-08). 이 패킷은 이 서버의 runner 하나만 다룬다.

## 5. runner 계약

### 5.1 연결과 시작

- 연결 직후 `hello`: runner의 계약 버전, 상태(`ENFORCED` 등, V3-050), 설정의 요약(숨김 목록 개수, 허용 도메인 개수, HIMARI 폴더 경로, 자원 한도). HIMARI는 상태가 `ENFORCED`가 아니면 `machine` Tool을 "not available"로 실패시킨다.
- 요청 하나에 연결 하나를 쓴다. 명령이 도는 동안 연결이 끊기면(HIMARI 쪽 취소) runner는 명령 트리를 죽이고 회수한다.

### 5.2 바이트는 디스크립터로

Unix 소켓의 `SCM_RIGHTS`로 열린 파일을 넘긴다.
- **읽기:** runner가 검사(§8.2)를 통과한 파일을 읽기 전용으로 열어 넘긴다. HIMARI는 그 디스크립터로 읽는다. 텍스트 읽기, 이미지(V3-P67), 문서 읽기(V3-P70, 문서 Sandbox에 그대로 넘김), 파일 보이기(V3-P66)가 모두 이 길이다.
- **쓰기·업로드:** HIMARI가 Data의 업로드 보관본을 읽기 전용으로 열어 넘긴다. runner가 그 내용으로 임시 파일을 만들고 이름을 붙인다(V3-P66 §5.2의 단계: 복사 → 이름 고르기 → 기록 → 이름 붙이기를 그대로).

### 5.3 연산

| 연산 | 하는 일 | runner의 검사 |
| --- | --- | --- |
| `hello` | §5.1 | — |
| `check_workspace(path)` | 등록 전에 폴더 확인: 있음, 폴더임, `coder`가 쓸 수 있음, 숨김 목록 안이 아님, 파일시스템 종류(§14) | — |
| `list(path, limit)` | 폴더 목록 | 읽기 검사 |
| `open_read(path)` | 읽기 디스크립터 | 읽기 검사 |
| `stat(path)` | 크기·종류·식별자(편집 CAS) | 읽기 검사 |
| `write(path, fd or bytes, expected_identity?)` | 원자적 쓰기(임시 파일 + rename) | 쓰기 검사 |
| `delete(path)` | 파일 하나 지우기 | 쓰기 검사 |
| `place_upload(...)` 단계들 | §5.2 | 쓰기 검사(대화 폴더만) |
| `remove_conversation_folder(id)` | 대화 영구 삭제(V3-P65 §5) | HIMARI 폴더의 `conversations/<id>`만 |
| `run(argv, cwd, write_roots, timeout)` | 명령(§9) | 쓰기 범위·작업 위치 검사 |
| `git_push(...)` | §12 | §12 |

**쓰기 범위는 두 번 검사한다.** HIMARI가 대화의 Binding에서 고른 `write_roots`를 보낸다. runner는 그것이 설정의 `writable_under`(기본: `coder`의 홈, Drive 마운트, HIMARI 폴더) 안이고 숨김 목록과 겹치지 않는지 다시 본다. HIMARI가 뚫려도 숨김 목록과 `writable_under` 밖에는 쓸 수 없다.

## 6. Sandbox 머신 프로파일

`sandbox` 모듈의 세 번째 프로파일이다(Workspace·서버 프로파일 옆, V3-P59 §3). 같은 bubblewrap + Landlock 도우미를 쓴다.

### 6.1 배치

| 위치 | 안에서 보이는 모습 |
| --- | --- |
| `/` | 호스트 루트를 **읽기 전용**으로(`--ro-bind / /`, 하위 마운트 포함) |
| 숨김 목록(§7) | 폴더는 빈 tmpfs, 파일은 빈 파일로 가림 |
| 붙인 Workspace들 | 같은 경로에 **읽기·쓰기**로 다시 묶음(확인한 디스크립터로 `--bind-fd`) |
| `/tmp` | 새 tmpfs |
| `/run`, `/var/run` | 새 tmpfs(사용자 systemd·D-Bus·에이전트 소켓을 가림) |
| `/dev`, `/proc` | 새 것 |
| 도구 캐시 | runner의 캐시 폴더를 읽기·쓰기로(§6.3) |

- 사용자 namespace 안에서도 uid는 `coder` 그대로라, 만든 파일의 소유자는 `coder`다.
- 지금 Workspace 프로파일의 "빈 루트 + 폴더 하나"와 달리, 실행 전에 폴더 전체를 훑는 검사(admission, V3-P41·V3-P59 §4.2)를 하지 않는다. 경계는 마운트 namespace(가림과 읽기 전용) + Landlock(쓰기 범위) + `coder`의 OS 권한이다. Workspace 안의 하드링크·마운트는 막지 않는다(§15의 남는 위험).

### 6.2 Landlock

- 파일시스템: 읽기 `/`, 쓰기는 붙인 Workspace들 + `/tmp` + 도구 캐시. 마운트 namespace와 같은 범위를 두 번째 층으로 건다.
- 네트워크: Sandbox의 network namespace에는 loopback만 있다. 밖으로는 프록시 다리(§11.2)로만 나간다. **namespace 안의 loopback TCP는 허용한다**(Q8). 테스트가 로컬 서버를 띄우는 일(Java·Python 테스트)이 흔하기 때문이다. 지금 Workspace 프로파일은 Landlock으로 TCP를 전부 막는다.
- 추상 Unix 소켓·시그널 범위 제한(ABI 6)은 그대로. 이 서버의 Landlock ABI는 8이다(2026-10-24 확인).

### 6.3 환경 변수와 캐시

- `--clearenv` 뒤에 정한 값만 넣는다: `PATH`(`~/.local/bin` 포함), `HOME=/home/coder`, `LANG`, `TERM=dumb`, 프록시 변수(§11.3), 문서 도구 변수(V3-P70, 지금과 같음).
- 홈은 읽기 전용이다(붙이지 않았다면). 도구가 쓰려는 캐시는 runner의 캐시 폴더(`~/.cache/himari-runner/`, 숨김 목록 예외)로 돌린다: `XDG_CACHE_HOME`, `PIP_CACHE_DIR`, `GRADLE_USER_HOME`, `MAVEN_OPTS=-Dmaven.repo.local=…`, `npm_config_cache`. 그래서 의존성은 한 번만 받는다.
- `pip install --user`는 홈에 쓰므로 안 된다. 프로젝트 안의 venv를 쓰라고 Tool 설명에 적는다.

### 6.4 시간과 출력

V3-051 그대로다(기본 120초, 최대 600초, 출력 256 KiB 수집, 모델에 약 64 KiB). Gradle·Maven 첫 빌드도 대개 10분 안이다. 모자라면 이후 값을 바꾼다.

### 6.5 자원 한도 (Q5)

이 서버: 메모리 11 GiB, CPU 2개, cgroup v2(cpu·memory·pids 위임 가능), systemd 255(2026-10-24 확인).
- **서비스 전체 한도(systemd):** `MemoryMax=4G`, `MemorySwapMax=0`, `TasksMax=1024`, `CPUQuota=150%`, `CPUWeight=50`. 모든 명령을 합친 값이라, 명령이 넘쳐도 HIMARI·Calliope·Caddy는 영향을 받지 않는다.
- runner는 명령 프로세스의 `oom_score_adj`를 1000으로 올려 띄운다. 한도에 걸리면 runner가 아니라 명령이 죽는다. 결과에 "killed (out of memory)"를 적는다.
- 명령마다 따로 정하는 한도(위임 cgroup)와 디스크 한도는 이후다.

## 7. 숨김 목록 (Q3)

숨김 목록은 runner 설정에 있고, 모델·HIMARI가 바꾸지 못한다. 읽기와 쓰기 모두 막는다.

**기본값(권고):**
- `coder` 홈의 **점(.)으로 시작하는 항목은 기본으로 숨기고**, 아래만 보인다: `.gitconfig`, `.profile`, `.bashrc`, `.local/bin`, `.local/lib`. 인증 정보 대부분이 점 폴더에 있다(이 서버에서 확인: `.ssh`, `.claude`, `.claude.json`, `.config/gh`, `.copilot`, `.cline`, `.vscode-server`, `.pki`, `.bash_history`).
- HIMARI·runner: `/srv/himari-v3`, `/etc/himari-v3`, `/etc/himari-runner`, `/opt/himari-v3`의 deployment 기록, `/run/himari-runner`, runner의 캐시 밖 상태 폴더.
- Calliope 데이터 `/var/lib/calliope`(이미 `coder`가 못 읽음, 방어용).
- 다른 사용자의 홈(`/home/opc` 등, 이미 대부분 못 읽음).
- `/etc/shadow` 같은 것은 OS 권한이 이미 막는다.

**어디서 막나:**
- 명령: 마운트 namespace에서 가린다(§6.1).
- 파일 Tool: 연 파일의 실제 경로(`/proc/self/fd/N`)를 숨김 목록과 대조한다(§8.2).
- Workspace 등록: 숨김 목록 안의 폴더는 등록할 수 없다. 숨김 목록을 **품은** 폴더(예: 홈 전체)는 등록할 수 있고, 그 안의 숨김 항목은 여전히 가려진다.

소유자 파일 안의 비밀(프로젝트의 `.env` 등)은 숨김 목록이 모른다. 필요하면 설정에 경로를 더한다.

## 8. 파일 Tool

### 8.1 경로

- 절대 경로(실제 경로) 또는 작업 위치 기준 상대 경로. `~`는 `coder`의 홈으로 푼다.
- 결과에는 실제 경로가 나온다. Inside처럼 숨길 호스트 경로가 없다. 숨김 목록과 다른 사용자 홈은 경로상 "없음"으로 답한다(있는지 알려 주지 않음).

### 8.2 읽기 검사

1. 경로를 연다(심볼릭 링크는 따라간다. 소유자 폴더에는 정상적인 링크가 많다).
2. 연 디스크립터의 실제 경로를 `/proc/self/fd`에서 읽어 숨김 목록과 대조한다. 걸리면 닫고 "no such file"로 답한다.
3. 일반 파일만 읽는다(FIFO·장치는 거부, 지금과 같음). **하드링크가 여럿인 파일도 읽는다.** git의 로컬 clone이 하드링크를 쓰기 때문이다.

### 8.3 쓰기 검사

1. 대상을 담은 Workspace를 고른다(붙인 Workspace의 실제 경로 중 가장 긴 접두어).
2. 그 Workspace 루트 디스크립터에서 `openat2(RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS)`로 연다. 루트 아래로 풀리는 심볼릭 링크와 하위 마운트는 따라가고, 밖으로 나가는 것은 거부한다.
3. 연 부모 폴더의 실제 경로를 숨김 목록과 대조한다.
4. 쓰기는 지금처럼 같은 폴더의 `O_EXCL` 임시 파일 후 rename이다. 하드링크된 파일을 고치면 링크가 끊길 뿐, 다른 이름의 내용은 바뀌지 않는다.
5. `edit_file`의 CAS(식별자 비교, `CHANGED`)는 그대로다. 소유자가 다른 곳에서 동시에 고친 경우도 이것이 잡는다.

### 8.4 그 밖

- `list_files`·`delete_file`·`present_file`·`read_document`·이미지 읽기는 Inside와 같은 동작이고, 경로 검사만 위를 따른다.
- 업로드는 대화 폴더의 `uploads/`에 둔다(지금과 같음, runner를 거쳐서).
- 파일 Tool의 `workspace_binding_id` 인자는 `machine`에 두지 않는다. 경로가 실제 경로라서 Workspace를 경로로 고른다(V3-P02를 이 Environment에서 바꿈, §16).

## 9. `run_command`

- 작업 위치: 대화의 작업 위치(§10.4). 인자 `cwd`로 다른 폴더를 줄 수 있다(읽을 수 있는 곳이면 어디든. 쓰기 범위는 그대로).
- 쓰기 범위: 대화에 붙인 모든 Workspace. 명령이 무엇을 바꿀지 모르므로 **붙인 모든 Workspace의 lock을 경로 순서로 잡는다**(교착 방지). 파일 Tool은 그 Workspace의 lock 하나만 잡는다.
- 취소·결과 불명: 지금 Inside와 같다(V3-P41, V3-047). runner와의 연결이 명령 도중 끊기면 결과 불명(`UNKNOWN`)이다.
- runner가 다시 시작하면 그 아래 명령은 모두 죽는다(`--die-with-parent`).

## 10. Workspace 등록과 붙이기 (Q-07, Q-09, Q-10)

### 10.1 Data (SQLite v13)

```sql
CREATE TABLE workspaces (
    workspace_id     TEXT PRIMARY KEY,      -- Data가 만든다
    owner            TEXT NOT NULL,         -- principal
    environment_kind TEXT NOT NULL,         -- 'machine'
    location         TEXT NOT NULL,         -- 실제 폴더 경로
    label            TEXT NOT NULL,
    note             TEXT,                  -- 모델에게 보일 짧은 메모(§14의 자동 메모 포함)
    created_at       TEXT NOT NULL,
    UNIQUE (owner, environment_kind, location)
);
```

- 대화에 붙이기는 기존 `bindings` 표에 `machine` Binding 행을 더하는 것이다(Binding은 대화 범위, V3-P03). 떼기는 행을 지운다.
- HIMARI 폴더(기본 Workspace)는 소유자마다 하나 자동으로 등록되고, 새 대화가 만들어질 때 붙는다(T0). 이것은 뗄 수 없다. 1단계(표가 없을 때)는 설정의 경로를 쓰는 Binding 하나다.
- 등록 취소: 어느 대화에도 붙어 있지 않을 때만. 폴더 자체는 지우지 않는다.
- Workspace의 `location`은 Data에만 있고, Tool 인자에서 오지 않는다(지금의 `host_root`와 같은 원칙).

### 10.2 누가 붙이나 (Q4)

**사용자만** Web이나 CLI에서 붙인다. 모델이 붙일 수 있으면 쓰기 범위가 의미를 잃는다. 대화에서 "이 폴더에서 작업해" 하면 모델은 붙여 달라고 안내한다. 모델이 요청하고 사용자가 한 번에 승인하는 카드는 이후에 둔다.

### 10.3 Web과 CLI

- 대화 화면에 **Workspace 줄**: 붙인 폴더 목록, 작업 위치 표시, "붙이기"(등록된 목록에서 고르거나 새 경로 입력 → runner `check_workspace` → 등록 + 붙이기), "떼기".
- 설정의 **Workspace 목록**: 등록·이름·메모·등록 취소.
- 명령이 도는 중에 붙이거나 떼면 다음 Tool 호출부터 적용한다(지금 도는 명령의 범위는 바뀌지 않음).
- CLI: `himari workspace list|add|remove`, `himari attach <path>`, `himari detach <path>`.

### 10.4 작업 위치

- Workspace를 붙이지 않은 대화: 대화 폴더.
- 붙이면 **마지막으로 붙인 Workspace**가 작업 위치가 된다. Workspace 줄에서 바꿀 수 있다. 떼면 남은 것 중 마지막 것, 없으면 대화 폴더로 돌아간다.

### 10.5 모델에게 주는 정보 (Current State)

```json
"machine": {
  "read": "everything the user can read, except hidden locations",
  "workspaces": [
    {"path": "/home/coder/HIMARI", "label": "HIMARI", "writable": true},
    {"path": "/home/coder/projects/foo", "label": "foo", "writable": true, "note": "…"}
  ],
  "working_directory": "/home/coder/projects/foo",
  "network": "package registries and git hosts only, through a proxy"
}
```

Tool 설명에 고정 문장으로 쓰기 범위·숨김·네트워크·push 방법을 적는다. 붙이지 않은 곳에 쓰려 하면 거부 결과에 "ask the user to attach the folder as a workspace"를 적는다.

## 11. 네트워크 (Q6)

### 11.1 왜 프록시인가

명령의 network namespace는 그대로 분리한다. 밖으로 나가는 유일한 길은 runner의 프록시다. 프록시는 도메인 허용 목록으로 거른다. Claude Code Sandbox와 같은 방식이다. Landlock의 TCP 규칙은 포트만 알아 도메인을 거를 수 없다.

### 11.2 다리

- runner가 프록시를 Unix 소켓으로 연다. 그 소켓을 Sandbox 안 `/run/himari/proxy.sock`에 묶는다.
- Landlock 도우미가 명령을 실행하기 전에 Sandbox 안 `127.0.0.1:3128`에서 그 소켓으로 잇는 작은 다리를 띄운다. 명령보다 먼저 포트를 잡으므로 명령이 가로챌 수 없다.

### 11.3 프록시 규칙

- HTTP `CONNECT`(HTTPS)와 일반 HTTP를 받는다. 목적지는 **이름**으로 받고, 프록시가 DNS를 푼다.
- 허용 목록(권고 기본값): `pypi.org`, `files.pythonhosted.org`, `repo.maven.apache.org`, `repo1.maven.org`, `plugins.gradle.org`, `services.gradle.org`, `downloads.gradle.org`, `github.com`, `codeload.github.com`, `objects.githubusercontent.com`, `raw.githubusercontent.com`, `registry.npmjs.org`.
- 이름이 허용되어도 **풀린 주소가 loopback·사설·링크 로컬·메타데이터 주소면 거부**한다(이 서버의 HIMARI·Calliope·클라우드 메타데이터에 닿지 않게).
- 거부는 HTTP 403과 이유 한 줄. 프록시 로그에 도메인·바이트·결과를 남긴다(내용은 남기지 않음).
- 환경 변수: `HTTP_PROXY`·`HTTPS_PROXY`·`ALL_PROXY`(대소문자 둘 다), `NO_PROXY=localhost,127.0.0.1`, `JAVA_TOOL_OPTIONS=-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=3128 -Dhttp.proxyHost=…`(Maven·Gradle·Java가 읽음), git은 `HTTPS_PROXY`를 읽는다.
- 안 되는 것: SSH git(`git@github.com:`, HTTPS를 쓰라고 Tool 설명에 적음), `apt`(root 필요. 시스템 패키지는 운영자가 설치, §19), 허용 목록 밖의 웹(웹 읽기는 Exa Tool로).

## 12. git push (Q7)

push는 인증 정보가 필요하고 밖에 영향을 주는 일이라 Sandbox 밖의 runner가 한다. 그래서 **모델이 만든 것을 Sandbox 밖에서 실행하지 않는 것**이 핵심이다. 저장소의 `.git/hooks`, `.git/config`(`core.fsmonitor`, `core.sshCommand`, `credential.helper`, `include.path` 등)는 모두 모델이 쓸 수 있다.

Tool `machine/git_push(repository, branch)`:
1. **Sandbox 안에서** `git bundle create`로 그 브랜치의 bundle을 runner의 임시 폴더에 만든다(저장소 설정과 hook이 실행되어도 Sandbox 안이다).
2. runner가 **자기 소유의 빈 bare 저장소**에 그 bundle을 fetch한다(bundle은 데이터일 뿐이다). 저장소의 설정은 읽지 않는다.
3. 원격 URL은 저장소 설정에서 읽되 HTTPS만, 그리고 설정의 `push_repos`(예: `github.com/jihunmoon04/*`)에 맞을 때만 쓴다.
4. runner의 bare 저장소에서 `refs/heads/B:refs/heads/B`로 push한다. **`+`(강제)와 삭제는 만들지 않는다.** fast-forward가 아니면 원격이 거부하고, 그 결과를 모델에 돌려준다.
5. 인증: runner 설정의 **전용 fine-grained GitHub 토큰**(고른 저장소만, Contents 쓰기만), `GIT_ASKPASS`로 넘긴다. `coder`의 `~/.config/gh` 토큰은 쓰지 않는다.

effect는 `EXTERNAL`이다. 결과에 원격 URL·브랜치·이전과 새 커밋을 적는다.

## 13. 자체 검사 (V3-050 개정)

runner가 시작할 때 머신 프로파일로 실제 명령을 띄워 다음을 확인한다. 하나라도 어긋나면 `SETUP_FAILED`이고 명령을 돌리지 않는다(파일 Tool도 막는다).

| 검사 | 기대 |
| --- | --- |
| 숨김 목록의 시험 파일 읽기·나열 | 안 보임 |
| 붙인 시험 Workspace에 쓰기 | 됨 |
| 붙이지 않은 `coder` 폴더, 홈, `/usr`, `/`에 쓰기 | 거부 |
| `/run/user/<uid>/bus`, `/run/dbus/system_bus_socket`, runner 소켓 연결 | 안 보임 |
| 호스트 `127.0.0.1`의 시험 리스너(TCP) | 닿지 않음 |
| 프록시로 허용 밖 도메인, 허용 도메인이 사설 주소로 풀리는 경우 | 403 |
| Sandbox 안 loopback 리스너 | 됨(Q8) |
| `oom_score_adj` | 1000 |
| 남은 디스크립터 | 정한 것만 |

HIMARI는 `hello`의 상태로 따르고, 따로 탈출 시험을 하지 않는다(runner의 검사가 그 프로파일의 검사다). 운영자는 `HIMARI_SANDBOX_HOST=1 pytest tests/sandbox_host`에 머신 프로파일 시험을 더해 배포 전에 돌린다.

## 14. Drive 마운트

이 서버(2026-10-24 확인):
- `~ubuntu/cloud_storage/02_구글_일반`, `03_구글_암호화`. `fuse.rclone`, `allow_other`, `--umask 000`, `--vfs-cache-mode full`, `--poll-interval 15m`.
- 파일은 `rw-rw-rw-`, 소유자 `ubuntu`로 보이고, chmod는 무시된다.
- `coder`는 상위 폴더 ACL로 들어갈 수 있다(`/home/coder/cloud_storage`가 링크). `himari-v3`는 못 들어간다. runner가 `coder`로 돌기 때문에 문제없다.

다루기:
- 일반 Workspace처럼 등록하고 붙인다. 막지 않는다.
- `check_workspace`가 파일시스템 종류가 `fuse.rclone`이면 Workspace에 **자동 메모**를 단다: "Cloud-synced folder: files have no execute bit, changes from other devices can take up to 15 minutes to appear, keep git repositories and build output on local disk." 모델은 Current State에서 이것을 본다.
- 지운 파일은 rclone 기본 설정에서 Drive 휴지통으로 간다(되살릴 수 있음).
- FUSE `allow_other` 마운트는 Sandbox의 사용자 namespace 안에서도 접근된다(커널이 `allow_other`면 하위 namespace를 허락). 구현에서 자체 검사로 확인한다.

참고(이 패킷 밖): 지금 Drive 마운트는 `umask 000` + `allow_other`라 이 서버의 모든 사용자가 읽고 쓸 수 있고, 상위 폴더 ACL 하나가 막고 있다. 운영 쪽에서 `--umask 007`과 그룹으로 좁히는 것을 권한다.

## 15. 위협과 대응

| 위협 | 대응 | 남는 것 |
| --- | --- | --- |
| 웹 내용(Exa)의 주입으로 파일을 읽어 밖으로 보냄 | 숨김 목록, 네트워크 허용 목록, push는 정한 저장소만 | 허용 도메인으로 보내는 경로(예: 공개 저장소의 이슈)는 인증이 없어 막히지만, 모든 경로를 증명하지는 못한다 |
| 숨김 위치를 링크로 우회 | 명령: 마운트 namespace에서 가림. 파일 Tool: 실제 경로 대조 | 숨김 파일을 가리키는 **이미 있던** 하드링크(새로 만들 수는 없음: 원본 이름이 안 보임) |
| 붙이지 않은 폴더 쓰기 | 마운트 읽기 전용 + Landlock + runner 검사 | Workspace 안의 이미 있던 하드링크로 밖의 파일 내용 바꾸기(명령의 직접 쓰기. 파일 Tool은 rename이라 해당 없음) |
| 호스트 소켓으로 Sandbox 밖 실행 | `/run`·`/tmp` 새로, 네트워크 namespace, Landlock scope | 자체 검사가 알려진 소켓을 확인 |
| push가 모델의 설정·hook을 실행 | bundle은 Sandbox 안, push는 runner의 빈 저장소에서 | — |
| 강제 push·브랜치 삭제 | refspec에 `+`와 삭제를 만들지 않음 | GitHub 브랜치 보호를 함께 권함 |
| 메모리·프로세스 폭주 | 서비스 한도, `oom_score_adj` | 디스크 채우기(한도 없음) |
| 실수로 지우기 | git, Drive 휴지통 | git 밖 로컬 파일은 되돌릴 수 없음 |
| HIMARI가 뚫림 | runner가 쓰기 범위를 자기 설정과 다시 대조, 숨김 목록은 runner 설정 | `coder` 권한 안의 숨김 아닌 파일 |
| runner가 뚫림 | HIMARI 데이터는 `himari-v3` 소유, runner는 `coder` 권한만 | `coder` 계정의 모든 것 |

## 16. 바뀌는 결정

| 결정 | 바뀌는 것 |
| --- | --- |
| V3-036·V3-037 | `machine`에서는 명령 네트워크를 허용 목록 프록시로 연다(§11). Inside는 그대로 차단 |
| V3-050 | `machine`의 자체 검사 항목을 §13으로 정한다(빈 루트 증명 대신 숨김·쓰기 범위·소켓·프록시 증명) |
| V3-P40 | `machine`은 실제 경로를 보이고, 읽기는 Workspace 밖도 된다. 하드링크 파일 읽기 허용 |
| V3-P41 | `machine`은 읽기 전용 루트 + 가림 + 쓰기 묶음, 실행 전 전체 검사 없음, Sandbox 안 loopback TCP 허용 |
| V3-P02 | `machine` Tool은 Binding id 대신 경로로 Workspace를 고른다(runner가 다시 검사) |
| V3-P13 | `machine` Tool은 `workspace_requirement = DEFAULT`(대화의 Binding들이 쓰기 범위) |
| Q-07·Q-09·Q-10 | `workspaces` 표, Binding = 대화의 쓰기 범위, Tool에는 경로만(§10) |
| Q-24 | Environment 안의 실행이 다른 사용자(runner)로 넘어가는 첫 사례 |
| Q-40 | 자원 한도의 서비스 단위 해결(§6.5). 명령 단위는 남음 |

## 17. 소유자 결정

| # | 질문 | 권고 |
| --- | --- | --- |
| Q1 | 기존 대화는? | **새 대화부터 `machine`.** 기존 대화는 `inside`로 남는다. 옮기려면 대화 폴더 복사, 파일 기록의 `/workspace` 경로 바꾸기, 기록 속 옛 경로가 필요해 따로 다룬다 |
| Q2 | HIMARI 폴더 위치? | **`/home/coder/HIMARI`**(`conversations/<id>/`). 소유자가 터미널·편집기로 바로 열 수 있다 |
| Q3 | 숨김 목록 방식? | **홈의 점 항목은 기본으로 숨기고 필요한 것만 보이기**(§7). 알려진 인증 위치만 나열하는 방식은 새 도구의 토큰을 놓친다 |
| Q4 | Workspace를 누가 붙이나? | **사용자만(Web·CLI).** 모델 요청 + 한 번 승인은 이후 |
| Q5 | 자원 한도? | **서비스 전체 메모리 4 GiB, 프로세스 1,024개, CPU 1.5개, 명령이 먼저 죽음**(§6.5) |
| Q6 | 네트워크 허용 목록? | **§11.3의 기본 목록 + 사설·loopback 주소 거부.** 운영자가 설정에서 더한다 |
| Q7 | push 인증? | **고른 저장소만 쓰는 전용 fine-grained 토큰을 runner 설정에.** `coder`의 gh 토큰은 계정 전체 권한이라 쓰지 않는다 |
| Q8 | Sandbox 안 loopback TCP? | **허용.** namespace 안이라 밖에 닿지 않고, 테스트에 필요하다 |

## 18. 검증 계획

- **단위:** 숨김 목록 판정(점 항목 규칙, 예외, 품은 폴더), 쓰기 범위 판정(가장 긴 접두어, 링크로 나가기, 숨김과 겹침), 작업 위치 규칙(§10.4), Current State 모양, refspec 생성(강제·삭제 없음), 프록시 주소 판정(사설·loopback·IPv6·IPv4-mapped).
- **runner 계약:** `SO_PEERCRED` 거부, 디스크립터 전달, 연결 끊김 → 명령 트리 종료, runner 재시작 → 결과 불명.
- **호스트 시험(`HIMARI_SANDBOX_HOST=1`):** §13의 표 전부 + Drive 형태의 FUSE 마운트(가능하면 실제 rclone) 읽기·쓰기, Workspace 안 심볼릭 링크가 숨김 위치를 가리킬 때 명령·파일 Tool 모두 막힘.
- **git push:** 악성 `.git/config`(`core.fsmonitor`, `core.hooksPath`, `include.path`, `credential.helper`, `core.sshCommand`)와 `pre-push` hook을 둔 저장소에서 push해도 Sandbox 밖에서 아무것도 실행되지 않음(표시 파일로 확인). 허용 밖 원격 거부. 강제 push가 필요한 상황에서 거부 결과.
- **실제 개발 흐름(라이브, 선택):** 새 대화에서 Python 프로젝트 `pip install` + 테스트, C `make`, Java Gradle wrapper 빌드, git clone → 수정 → commit → push(시험 저장소).
- **회귀:** Inside 대화와 다른 principal은 그대로(전체 pytest, 두 Data 백엔드).

## 19. 구현 단계와 배포

1. **1단계: runner와 머신 프로파일.** `himari/runner/`, 계약(§5), 머신 프로파일(§6, 네트워크 없이), 숨김 목록(§7), 자체 검사(§13), `machine` Environment의 파일 Tool·`run_command`·업로드·파일 보이기·문서·이미지(§8, §9), HIMARI 폴더 하나만(붙이기 없음), 새 대화 활성화(Q1).
2. **2단계: Workspace 등록과 붙이기.** Data v13(§10.1), Web Workspace 줄과 설정 목록, CLI, Current State, Drive 자동 메모(§14).
3. **3단계: 네트워크.** 프록시와 다리(§11), 캐시 환경 변수(§6.3).
4. **4단계: git push.** §12.

배포(소유자 sudo, 단계마다 스크립트로 준비):
- `himari-runner.service`(User=coder, §6.5의 한도), `/etc/himari-runner/runner.toml`, `/run/himari-runner`(RuntimeDirectory).
- 개발 도구: `openjdk-21-jdk`, `maven`, `cmake`(이 서버에 gcc·make·python3·git은 있음, Java·cmake는 없음, 2026-10-24 확인). Gradle은 프로젝트의 wrapper가 받는다(허용 목록).
- web.env: `HIMARI_MACHINE_SOCKET=/run/himari-runner/runner.sock`. 지우면 새 대화가 다시 `inside`를 쓴다(되돌리기).
- 1단계는 DB 변경이 없고, 2단계에서 v12 → v13(업그레이드 전 백업, V3-P65).
