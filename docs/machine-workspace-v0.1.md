# 머신 Workspace v0.1

> 2026-10-25 · **PROPOSED (V3-P74)** · rev. 4 — 외부 재검수 2(HEAD ea6e534) 반영: N4 호스트의 경로 Unix 소켓을 seccomp로 막음(§6.6, Q9), N5 runner 소켓은 systemd 소켓 단위가 그룹 `himari-v3`로 만듦(§4.1), N6 Q5 문구를 §6.5에 맞춤(§17). 소유자 결정 Q1~Q9 대기(§17). rev. 3 — 외부 재검수(HEAD a7eca31) 반영: N1 bundle fetch에만 `file` 전송 허용(§12), N2 숨김은 마지막에 가리고 실행 직전에 확인하는 불변 조건(§6.1), N3 runner 생존은 명령 OOM 격리까지만 보장(§6.5). rev. 2 — 외부 검수(HEAD 61200df) 반영: B1 기본 쓰기 범위를 대화 폴더로, 겹치는 Workspace는 한 충돌 영역(§3.2, §9.1), B2 파일 Tool은 디스크 파일시스템만·보이는 점 항목 줄임(§7, §8.2), B3 git push의 bundle 전달·대상·lock(§12), P2 등록 범위(§5.3)·링크 풀기(§8.3)·OOM(§6.5)·프록시 DNS(§11.3). 소유자 결정 Q1~Q8 대기(§17). rev. 1: 설계 초안.
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
| 동시 실행 단위 | 겹치는 Workspace에 쓰는 일(파일 쓰기·명령)은 한 번에 하나(지금의 `workspace_busy`, §9.1) |

**대화 폴더**는 Workspace를 붙이지 않아도 늘 있다. 소유자의 HIMARI 폴더(Q2, 권고 `/home/coder/HIMARI`) 아래 `conversations/<conversation_id>/`이고, 지금의 대화별 기본 폴더와 같은 역할이다. **대화 폴더가 그 대화의 기본 Workspace**다. 대화마다 따로라서, 서로 다른 대화의 명령은 서로 기다리지 않는다(검수 B1).

다른 대화의 폴더는 읽을 수 있지만, 쓰려면 그 폴더나 HIMARI 폴더 전체를 Workspace로 붙여야 한다. 지금 Inside의 "같은 사용자의 다른 대화 폴더에도 쓴다"(V3-033)를 `machine`에서는 이렇게 바꾼다(§16). HIMARI 폴더 전체를 늘 붙여 두면 모든 대화가 한 lock을 나눠 쓰게 되기 때문이다.

예:
- Workspace를 붙이지 않은 새 대화: 대화 폴더에만 쓴다. 다른 곳은 읽기만 한다.
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
- 소켓은 **systemd 소켓 단위**(`himari-runner.socket`)가 만든다: `ListenStream=/run/himari-runner/runner.sock`, `SocketUser=coder`, `SocketGroup=himari-v3`, `SocketMode=0660`, `DirectoryMode=0750`. runner는 받은 소켓을 `LISTEN_FDS`로 쓴다(검수 N5). runner가 직접 만들면 소켓 그룹이 `coder`가 되고, `coder`는 `himari-v3` 그룹의 구성원이 아니라 `chgrp`도 할 수 없다(이 서버: `coder`의 그룹은 `coder`, `users`, `himari-project`, 2026-10-25 확인). root인 systemd가 만들면 그룹을 정확히 줄 수 있다.
- runner는 연결마다 `SO_PEERCRED`로 상대 uid가 설정의 `client_uid`(himari-v3)인지 다시 확인한다(파일 권한이 잘못되어도 다른 사용자는 거부).
- 배포 시험: `himari-v3`로 연결 → `hello` 성공, `coder`가 아닌 다른 사용자(예: `nobody`)로 연결 → 권한 거부, 소켓의 소유·그룹·모드 확인.
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

**쓰기 범위는 두 번 검사한다.** HIMARI가 대화의 Binding에서 고른 `write_roots`를 보낸다. runner는 각 루트가 숨김 목록 안이 아니고, 특수 파일시스템(§8.2)이 아니며, 설정의 `writable_under` 안인지 다시 본다.

`writable_under`의 기본값은 `/`다. 소유자 방향("금지된 위치 외에는 어디든")대로 **등록 범위는 숨김 목록과 OS 권한이 정한다**(검수 P2). 운영자가 범위를 좁히고 싶으면 이 설정을 바꾼다(예: 홈과 Drive만). 어느 쪽이든 HIMARI가 뚫려도 숨김 목록 안에는 쓸 수 없다.

## 6. Sandbox 머신 프로파일

`sandbox` 모듈의 세 번째 프로파일이다(Workspace·서버 프로파일 옆, V3-P59 §3). 같은 bubblewrap + Landlock 도우미를 쓴다.

### 6.1 배치

마운트는 아래 **순서대로** 만든다. 순서가 계약의 일부다(검수 N2).

| 순서 | 위치 | 안에서 보이는 모습 |
| --- | --- | --- |
| 1 | `/` | 호스트 루트를 **읽기 전용**으로(`--ro-bind / /`, 하위 마운트 포함) |
| 1 | `/tmp` | 새 tmpfs |
| 1 | `/run`, `/var/run` | 새 tmpfs(사용자 systemd·D-Bus·에이전트 소켓을 가림) |
| 1 | `/dev`, `/proc` | 새 것 |
| 2 | 붙인 Workspace들 | 같은 경로에 **읽기·쓰기**로 다시 묶음(확인한 디스크립터로 `--bind-fd`). 얕은 경로부터 |
| 3 | 도구 캐시 | runner의 캐시 폴더를 읽기·쓰기로(§6.3) |
| 4 | 숨김 목록(§7) | **마지막에** 가린다. 폴더는 빈 tmpfs를 올리고 바로 읽기 전용으로 다시 마운트, 파일은 `/dev/null`을 읽기 전용으로 묶음 |

**불변 조건: 최종 마운트 트리에서 숨김 경로는 모두 가려져 있다.**
- 숨김을 먼저 가리고 그 위 폴더를 Workspace로 다시 묶으면 가린 것이 사라진다. 이 서버에서 bwrap 0.9.0으로 재현했다(2026-10-25): `--tmpfs D/hidden --bind D D` 순서면 `D/hidden/token`이 읽히고, `--bind D D --tmpfs D/hidden` 순서면 없다. 그래서 숨김은 Workspace·캐시보다 **뒤에** 가린다. `/home/coder` 전체를 Workspace로 붙여도 `.ssh`는 가려진다.
- 가림은 **읽기 전용**이다. tmpfs를 쓰기 가능하게 두면 Landlock의 쓰기 루트(`/home/coder`)가 그 아래 마운트도 허락해 모델이 거기에 쓴 줄 안다(사라지는 쓰기). 읽기 전용이면 "Read-only file system"으로 바로 실패한다.
- 숨김은 **항목 단위**로 가린다. 홈의 점 항목 규칙(§7)은 명령마다 홈을 나열해 예외가 아닌 점 항목을 하나씩 가린다(예: `.local`은 `bin`·`lib` 외의 자식을, `.cache`는 `himari-runner` 외의 자식을). 예외는 가리지 않을 뿐 따로 묶지 않는다.
- **실행 직전에 확인한다.** Landlock 도우미가 명령을 실행하기 전에 runner가 넘긴 숨김 목록의 각 경로를 본다. 폴더는 비어 있고 읽기 전용이며 장치가 호스트 원본과 달라야 하고, 파일은 크기 0이어야 한다. 하나라도 어긋나면 실행하지 않고 설정 실패로 보고한다(지금의 sync 보고와 같은 길). 숨김 경로는 수십 개라 비용이 작다.
- 명령이 도는 동안 홈에 새로 생긴 점 항목은 그 명령에서는 가려지지 않는다. 다음 명령부터 가려진다(§15).

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
- **서비스 전체 한도(systemd):** `MemoryMax=4608M`, `MemorySwapMax=0`, `TasksMax=1100`, `CPUQuota=150%`, `CPUWeight=50`. 명령이 넘쳐도 HIMARI·Calliope·Caddy는 영향을 받지 않는다.
- **명령은 runner와 다른 cgroup에 둔다(검수 P2).** 서비스는 `Delegate=yes`로 하위 cgroup을 위임받는다. runner는 시작할 때 자기를 `runner` 잎으로 옮기고, 명령은 `commands` 잎에 넣는다(bwrap을 띄우기 전에 그 cgroup으로 들어감). `commands`에 `memory.max=4G`, `pids.max=1024`를 건다. 메모리가 넘치면 커널은 `commands` 안에서만 프로세스를 죽인다. runner의 생존은 `oom_score_adj` 같은 유도가 아니라 cgroup 경계로 보장한다.
- **보장하는 것과 아닌 것(검수 N3).** 명령의 메모리 초과는 `commands` 안에서 처리되어 runner에 닿지 않는다. 하지만 서비스 전체 한도(4.5 GiB)는 runner·프록시·명령을 합친 값이라, runner가 0.5 GiB 넘게 쓰는 순간에 명령이 4 GiB 가까이 쓰면 서비스 전체 OOM이 날 수 있다. 그래서 runner의 생존은 **보장하지 않고**, 서비스 OOM과 runner 종료를 정상 경로로 다룬다.
  - `runner` 잎에 `memory.low=256M`을 걸어 서비스 안에서는 runner의 메모리를 먼저 지킨다(유도이지 보장은 아님).
  - runner가 죽으면 `Restart=always`로 다시 뜬다. 그 사이의 호출은 HIMARI에서 결과 불명(`UNKNOWN`)이고(§9.2), 다시 뜬 runner에는 이전 명령이 없다(`--die-with-parent`). HIMARI는 다음 호출 때 다시 연결하고 `hello`로 상태를 확인한다.
  - 서비스 OOM은 runner 로그와 HIMARI의 `UNKNOWN` 결과 문구("the machine runner restarted")로 알린다.
- 결과에 "killed (out of memory)"를 적는다(`commands`의 `memory.events`의 `oom_kill` 증가로 안다).
- 명령마다 따로 정하는 한도와 디스크 한도는 이후다.

### 6.6 호스트의 경로 Unix 소켓 (검수 N4, Q9)

**문제.** 읽기 전용으로 보이는 호스트 루트에도 경로 Unix 소켓(소켓 파일)이 있다. 소켓 연결은 파일 쓰기가 아니라서 읽기 전용 마운트가 막지 않고, `coder`에게 쓰기 권한이 있으면 연결된다. Landlock ABI 6의 scope는 **추상** 소켓만 막는다. `/run`·`/tmp`를 새로 만드는 것은 그 두 곳만 해결한다.

이 서버에서 확인했다(2026-10-25). `/run`·`/tmp`·`/dev` 밖에서 `coder`가 볼 수 있는 소켓 파일은 셋이고, 그중 하나에 실제로 연결된다.
- `/home/coder/.local/share/code-server/code-server-ipc.sock`: **연결됨**(code-server IPC). §7의 점 항목 규칙이 가리지만, 숨김 목록이 놓친 소켓 하나가 Sandbox 밖 실행이 될 수 있다는 증거다.
- `/var/lib/oracle-cloud-agent/tmp/plugin*`, `/etc/unified-monitoring-agenttmp/*`: 권한 거부.

그래서 숨김 목록에 기대지 않고 **소켓을 만드는 것 자체**를 막는다.

**계약.**
- **seccomp(이 서버, Landlock ABI 8):** Landlock 도우미가 명령을 실행하기 직전(no_new_privs 뒤)에 seccomp 필터를 건다.
  - `socket(AF_UNIX, …)` → `EACCES`. 경로 소켓에 연결하려면 이 호출이 먼저 있어야 하므로, 어느 경로의 소켓이든 연결할 수 없다.
  - `socketpair(AF_UNIX, …)`는 허용한다. 이름 없는 한 쌍이라 호스트에 닿지 않고, Python asyncio·Node의 자식 IPC가 쓴다.
  - `io_uring_setup`·`io_uring_enter`·`io_uring_register` → `ENOSYS`. io_uring의 소켓 연산은 `socket` 호출을 거치지 않기 때문이다.
  - 아키텍처가 이 서버의 것(aarch64, 필터가 아는 것)이 아니면 프로세스를 끝낸다(다른 아키텍처의 호출 번호로 우회 방지).
  - 필터는 BPF를 stdlib `ctypes`로 만들어 `prctl(PR_SET_SECCOMP)`로 건다(Landlock과 같은 방식, 새 의존성 없음).
- **Landlock에 경로 소켓 제한이 생기면:** 검수에 따르면 ABI 9에 경로 소켓 연결 제한(`LANDLOCK_ACCESS_FS_RESOLVE_UNIX`)이 있다. 구현 때 커널 문서로 확인한다. 커널이 그것을 지원하면 Landlock으로 "Sandbox 안 `/tmp`의 소켓만 연결 허용"으로 바꾸고, seccomp의 `socket(AF_UNIX)` 규칙은 뺀다(호환성이 좋아짐). io_uring 규칙은 남긴다.
- **프록시 다리(§11.2)는 예외가 아니다.** 다리는 도우미가 seccomp를 걸기 **전에** 따로 띄우는 프로세스다. 다리는 `PR_SET_DUMPABLE=0`으로 자기를 감춘다. 명령은 같은 uid이지만 능력(capability)이 없어 다리에 `ptrace`·`pidfd_getfd`·`/proc/<pid>/mem` 접근을 할 수 없다. 다리가 여는 경로는 코드에 고정된 `/run/himari/proxy.sock` 하나다.
- 명령이 다리를 죽이면 네트워크만 끊긴다.

**호환성 비용(이 서버에서).** 스스로 Unix 소켓을 만드는 프로그램은 `EACCES`로 실패한다.
- Python 3.14 이상의 `multiprocessing` 기본 시작 방식(`forkserver`), `multiprocessing.Manager`, tmux·screen, ssh-agent, Docker CLI.
- 이 서버의 Python은 3.12라 기본이 `fork`이고 영향이 없다. pip·Maven·Gradle(데몬은 loopback TCP)·javac·gcc·make·git·pytest·npm은 영향이 없다고 본다. 구현에서 각각 시험한다(§18).
- `run_command` 설명에 "Unix domain sockets cannot be created; use TCP on 127.0.0.1 or `multiprocessing.set_start_method('fork')`"를 적는다.

## 7. 숨김 목록 (Q3)

숨김 목록은 runner 설정에 있고, 모델·HIMARI가 바꾸지 못한다. 읽기와 쓰기 모두 막는다.

**기본값(권고):**
- `coder` 홈의 **점(.)으로 시작하는 항목은 기본으로 숨기고**, 아래만 보인다: `.gitconfig`(커밋 작성자), `.local/bin`, `.local/lib`(사용자가 설치한 도구). 인증 정보 대부분이 점 폴더에 있다(이 서버에서 확인: `.ssh`, `.claude`, `.claude.json`, `.config/gh`, `.copilot`, `.cline`, `.vscode-server`, `.pki`, `.bash_history`).
- **보이는 예외도 비밀이 없다고 가정하지 않는다(검수 B2).** `.bashrc`·`.profile`은 토큰을 `export`하는 일이 흔해 rev. 1의 예외에서 뺐다(명령은 비대화형이라 필요 없음). `.gitconfig`는 이 서버에서 비밀 같은 줄이 없고 `credential` 절은 `gh`를 부르는 도우미뿐이다(2026-10-25 확인. Sandbox 안에서는 `~/.config/gh`가 가려져 실패한다). 예외 목록은 운영자가 설정에서 바꾸고, 배포 스크립트가 예외 파일에 비밀 같은 줄(`token`, `password`, `https://…@`)이 있으면 경고한다.
- HIMARI·runner: `/srv/himari-v3`, `/etc/himari-v3`, `/etc/himari-runner`, `/opt/himari-v3`의 deployment 기록, `/run/himari-runner`, runner의 캐시 밖 상태 폴더.
- 특수 파일시스템: 파일 Tool은 디스크 파일시스템만 연다(§8.2). 명령의 `/proc`·`/dev`·`/run`은 새로 만든 것이라 runner나 호스트의 것이 보이지 않는다(§6.1).
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

1. 경로를 연다(심볼릭 링크는 절대·상대 모두 따라간다. 소유자 폴더에는 정상적인 링크가 많다). `O_NONBLOCK|O_NOCTTY`로 열어 FIFO·장치에서 멈추지 않는다.
2. **파일시스템 종류를 본다(검수 B2).** 연 디스크립터에 `fstatfs`를 불러 `f_type`이 디스크 파일시스템 허용 목록(ext2/3/4, xfs, btrfs, f2fs, zfs, FUSE, overlay, NFS, SMB, vfat, exfat, ntfs3)에 있을 때만 계속한다. `proc`·`sysfs`·`devtmpfs`·`tmpfs`·`cgroup2`·`debugfs`·`tracefs`·`securityfs`·`bpf`·`configfs`·`pstore`·`efivarfs` 등은 "not a regular file"로 거부한다. 그래서 `/proc/self/environ`(runner의 환경)이나 `/sys`의 장치 파일처럼 일반 파일로 보이는 특수 인터페이스를 읽을 수 없다. 경로 문자열 검사(`/proc`, `/sys`, `/dev`, `/run`으로 시작하면 거부)를 첫 번째 층으로 둔다.
3. 연 디스크립터의 실제 경로를 `/proc/self/fd`에서 읽어 숨김 목록과 대조한다. 걸리면 닫고 "no such file"로 답한다.
4. 일반 파일만 읽는다(`fstat`, 지금과 같음). **하드링크가 여럿인 파일도 읽는다.** git의 로컬 clone이 하드링크를 쓰기 때문이다.
5. `list_files`도 같다: 폴더 디스크립터에 2·3을 적용한다.

runner 자신은 비밀을 환경 변수에 두지 않는다(push 토큰은 설정 파일에서 읽어 push 때만 넘김, §12). 그래도 runner의 `/proc`가 열리지 않는 것은 위의 2가 보장한다.

### 8.3 쓰기 검사

rev. 1은 Workspace 루트에서 `RESOLVE_BENEATH`로 열자고 했지만, 그 방식은 절대 경로 심볼릭 링크를 늘 거부한다(Workspace 안을 가리켜도). 그래서 **풀고 나서 검사한다**(검수 P2).

1. **부모 폴더를 연다.** 경로의 부모를 일반 경로 풀기로(절대·상대 링크 모두 따라감) `O_PATH|O_DIRECTORY`로 연다. `RESOLVE_NO_MAGICLINKS`는 둔다(`/proc/*/fd` 같은 마법 링크는 따라가지 않음).
2. **풀린 위치를 검사한다.** 그 디스크립터의 실제 경로(`/proc/self/fd`)가 붙인 Workspace의 실제 경로 중 하나의 안이어야 하고(경로 성분 단위 접두어), 숨김 목록 안이 아니어야 하며, 파일시스템이 §8.2의 허용 목록이어야 한다.
3. **마지막 성분.** 이름이 심볼릭 링크면 그 대상에 대해 1·2를 다시 한다(링크 단계는 최대 40). 대상이 Workspace 밖이면 거부한다. 링크 자체를 바꾸지 않고 대상 파일을 고친다(편집기와 같음).
4. **그 디스크립터 기준으로만 쓴다.** 검사한 부모 디스크립터에 대해 `O_EXCL` 임시 파일 → `renameat`이다. 검사 뒤 경로가 바뀌어도 쓰는 곳은 검사한 폴더다. 하드링크된 파일을 고치면 링크가 끊길 뿐, 다른 이름의 내용은 바뀌지 않는다.
5. `edit_file`의 CAS(식별자 비교, `CHANGED`)는 그대로다. 소유자가 다른 곳에서 동시에 고친 경우도 이것이 잡는다.
6. `delete_file`도 1~2를 거친 부모 디스크립터에 대해 `unlinkat`이다(링크는 링크만 지움).

시험: 절대 링크(안→안 허용, 안→밖 거부, 안→숨김 거부), 상대 링크로 `..` 탈출, 링크 고리, 마지막 성분 링크, 하위 마운트(허용), 검사와 쓰기 사이에 부모를 링크로 바꾸기(검사한 폴더에 씀).

### 8.4 그 밖

- `list_files`·`delete_file`·`present_file`·`read_document`·이미지 읽기는 Inside와 같은 동작이고, 경로 검사만 위를 따른다.
- 업로드는 대화 폴더의 `uploads/`에 둔다(지금과 같음, runner를 거쳐서).
- 파일 Tool의 `workspace_binding_id` 인자는 `machine`에 두지 않는다. 경로가 실제 경로라서 Workspace를 경로로 고른다(V3-P02를 이 Environment에서 바꿈, §16).

## 9. `run_command`

- 작업 위치: 대화의 작업 위치(§10.4). 인자 `cwd`로 다른 폴더를 줄 수 있다(읽을 수 있는 곳이면 어디든. 쓰기 범위는 그대로).
### 9.1 lock: 겹치는 경로가 한 충돌 영역 (검수 B1)

- lock은 Workspace id가 아니라 **실제 경로**에 건다. 두 쓰기 작업은 잡으려는 루트 중 하나라도 **같거나 한쪽이 다른 쪽의 조상**이면 충돌한다. `/home/coder/HIMARI`를 붙인 대화의 명령과 `/home/coder/HIMARI/conversations/c_1`의 파일 쓰기는 서로 기다린다. `~/projects/x`와 `~/projects/y`는 기다리지 않는다.
- `run_command`는 대화에 붙인 모든 Workspace(대화 폴더 포함)를 한 번에 잡는다. 파일 Tool은 대상을 담은 Workspace 하나를 잡는다. 하나의 lock 관리자가 "잡으려는 경로 집합이 지금 잡힌 것과 겹치지 않으면 한꺼번에 준다"로 동작한다. 한꺼번에 잡으므로 교착이 없다. 요청은 지금 잡힌 것과도, **먼저 와서 기다리는 요청**과도 겹치지 않을 때 받는다. 그래서 겹치는 요청끼리는 도착 순이고(큰 명령이 계속 밀리지 않음), 겹치지 않는 요청은 앞의 대기와 상관없이 바로 받는다.
- 비교하는 경로는 등록 때 정한 실제 경로(realpath)다. 같은 폴더가 bind mount로 두 경로에 보이는 경우는 등록 때 루트의 (장치, inode)가 이미 등록된 루트와 같으면 거부해 막는다. 하위 폴더까지 bind mount로 겹치는 경우는 찾지 않는다(§15).
- 읽기는 lock을 잡지 않는다(지금과 같음).
- lock은 HIMARI 프로세스 안에 있다. runner를 부르는 것은 HIMARI 하나뿐이라 충분하다(P0 단일 서버, V3-045).

### 9.2 그 밖

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
- 대화 폴더(기본 Workspace)는 새 대화가 만들어질 때 붙는다(T0). 이것은 뗄 수 없다. HIMARI 폴더 전체는 소유자마다 하나 자동으로 등록만 되고, 붙이는 것은 사용자가 고른다(§3.2). 1단계(표가 없을 때)는 대화 폴더 Binding 하나다.
- 등록할 때 실제 경로(realpath)로 바꿔 저장하고, 루트의 (장치, inode)가 이미 등록된 루트와 같으면 거부한다(§9.1).
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
    {"path": "/home/coder/HIMARI/conversations/c_1", "label": "this conversation", "writable": true},
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
- 다리는 seccomp를 걸기 전에 갈라져 나오고, 숨겨진(`PR_SET_DUMPABLE=0`) 프로세스다(§6.6).

### 11.3 프록시 규칙

- HTTP `CONNECT`(HTTPS)와 일반 HTTP를 받는다. 목적지는 **이름**으로 받고, 프록시가 DNS를 푼다.
- **이름 정규화:** 소문자, 끝의 `.` 제거, IDNA(A-label)로 바꾼 뒤 비교한다. IP 리터럴(`1.2.3.4`, `[::1]`, 십진·8진 표기 포함)은 받지 않는다. 허용 목록은 정확한 이름이고, 하위 도메인은 `*.example.com`처럼 적은 것만 허용한다.
- **포트:** `CONNECT`는 443, 일반 HTTP는 80만.
- **풀고, 모두 검사하고, 고정한다(검수 P2):** 이름을 한 번 풀어 **돌아온 주소를 모두** 검사한다. 하나라도 금지 범위면 거부한다. 연결은 검사한 주소로 직접 한다(다시 풀지 않음). 그래서 DNS 재바인딩(검사 때와 연결 때 다른 주소)이 통하지 않는다.
- **일반 HTTP의 연결 재사용:** 요청마다 `Host`를 다시 검사하고, 다른 이름이면 새 연결을 연다.
- 허용 목록(권고 기본값): `pypi.org`, `files.pythonhosted.org`, `repo.maven.apache.org`, `repo1.maven.org`, `plugins.gradle.org`, `services.gradle.org`, `downloads.gradle.org`, `github.com`, `codeload.github.com`, `objects.githubusercontent.com`, `raw.githubusercontent.com`, `registry.npmjs.org`.
- 이름이 허용되어도 **풀린 주소가 금지 범위면 거부**한다: loopback, 사설(RFC 1918, `fc00::/7`), 링크 로컬(`169.254.0.0/16`, 클라우드 메타데이터 포함, `fe80::/10`), CGNAT(`100.64.0.0/10`), `0.0.0.0/8`, 멀티캐스트, 예약, IPv4-mapped IPv6(풀어서 다시 검사). 이 서버의 HIMARI·Calliope·VPN 주소에 닿지 않게 한다.
- 거부는 HTTP 403과 이유 한 줄. 프록시 로그에 도메인·바이트·결과를 남긴다(내용은 남기지 않음).
- 환경 변수: `HTTP_PROXY`·`HTTPS_PROXY`·`ALL_PROXY`(대소문자 둘 다), `NO_PROXY=localhost,127.0.0.1`, `JAVA_TOOL_OPTIONS=-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=3128 -Dhttp.proxyHost=…`(Maven·Gradle·Java가 읽음), git은 `HTTPS_PROXY`를 읽는다.
- 안 되는 것: SSH git(`git@github.com:`, HTTPS를 쓰라고 Tool 설명에 적음), `apt`(root 필요. 시스템 패키지는 운영자가 설치, §19), 허용 목록 밖의 웹(웹 읽기는 Exa Tool로).

## 12. git push (Q7)

push는 인증 정보가 필요하고 밖에 영향을 주는 일이라 Sandbox 밖의 runner가 한다. 그래서 **모델이 만든 것을 Sandbox 밖에서 실행하지 않는 것**이 핵심이다. 저장소의 `.git/hooks`, `.git/config`(`core.fsmonitor`, `core.sshCommand`, `credential.helper`, `include.path` 등)는 모두 모델이 쓸 수 있다.

Tool `machine/git_push(repository, branch)`:

**대상 검사 (검수 B3)**
- `repository`(저장소 폴더)는 **이 대화에 붙인 Workspace 안**이어야 한다(§8.3의 풀고 나서 검사). 붙이지 않은 저장소는 push하지 않는다.
- `branch`는 `git check-ref-format --branch` 규칙에 맞고 `-`로 시작하지 않아야 한다.
- push 동안 그 Workspace의 lock을 잡는다(§9.1). bundle을 만드는 사이에 다른 명령이 저장소를 바꾸지 못한다.

**1. Sandbox 안에서 꺼내기.** 머신 프로파일로 저장소 폴더에서 두 명령을 돌린다(네트워크 없음).
- `git remote get-url --push origin` → 원격 URL(글자로만 받음).
- `git bundle create - refs/heads/<branch>` → bundle을 **stdout으로** 낸다. runner는 이 stdout을 일반 출력 한도(256 KiB)가 아니라 자기 소유의 새 임시 파일(`0600`, runner 상태 폴더)로 받는다. 한도는 2 GiB이고 넘으면 멈추고 거부한다. 따로 묶는 폴더가 없어서 Sandbox와 runner가 공유하는 쓰기 위치가 생기지 않는다.
- 저장소의 설정·hook(`core.fsmonitor`, `include.path` 등)이 이 단계에서 실행되어도 Sandbox 안이다.

**2. runner 밖에서 검사.**
- 원격 URL: `https://`만, 사용자 정보(`user@`)·포트·쿼리 없음, 호스트와 경로가 설정의 `push_repos`(예: `github.com/jihunmoon04/*`)에 맞아야 한다. 경로 끝의 `.git`은 정규화한다.
- bundle: runner 소유의 **빈 bare 저장소**(매번 새로 만들고 끝나면 지움)에서 `git bundle list-heads`로 ref가 정확히 `refs/heads/<branch>` 하나인지 확인하고, `git fetch <bundle> refs/heads/<branch>:refs/heads/<branch>`로 받는다. 원본 저장소의 디렉터리와 설정은 이 단계부터 열지 않는다.
- runner의 git 실행은 고정 환경이다: `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `HOME`은 빈 임시 폴더, `core.hooksPath=/dev/null`, `credential.helper=` 비움, `GIT_TERMINAL_PROMPT=0`, `protocol.allow=never`. 허용하는 전송은 **단계마다 따로** 연다(검수 N1).
  - bundle 받기(`list-heads`, `fetch <bundle>`): `protocol.file.allow=always`만. `protocol.allow=never`만 두면 `fatal: transport 'file' not allowed`로 실패한다(이 서버의 git 2.55.0으로 재현, 2026-10-25). bundle 경로는 runner가 만든 임시 파일의 절대 경로이고, 저장소 설정을 읽지 않는 빈 bare 저장소에서 받는다.
  - push: `protocol.https.allow=always`만. `file`은 열지 않는다.

**3. push.**
- 확인한 URL로 `refs/heads/<branch>:refs/heads/<branch>`를 push한다. **`+`(강제)와 삭제는 만들지 않는다.** fast-forward가 아니면 원격이 거부하고, 그 결과를 모델에 돌려준다.
- 인증: runner 설정의 **전용 fine-grained GitHub 토큰**(고른 저장소만, Contents 쓰기만)을 이 git 실행에만 `GIT_ASKPASS`로 넘긴다. 토큰은 runner의 환경 변수에 두지 않는다. `coder`의 `~/.config/gh` 토큰은 쓰지 않는다.
- 프록시 허용 목록(§11)은 Sandbox 명령용이라 push에는 쓰이지 않는다. push는 `push_repos`의 호스트로만 직접 연결한다.

effect는 `EXTERNAL`이다. 결과에 원격 URL·브랜치·이전과 새 커밋을 적는다.

## 13. 자체 검사 (V3-050 개정)

runner가 시작할 때 머신 프로파일로 실제 명령을 띄워 다음을 확인한다. 하나라도 어긋나면 `SETUP_FAILED`이고 명령을 돌리지 않는다(파일 Tool도 막는다).

| 검사 | 기대 |
| --- | --- |
| 숨김 목록의 시험 파일 읽기·나열 | 안 보임 |
| 숨김 경로를 품은 폴더(시험용 `home`)를 Workspace로 붙였을 때 그 안의 숨김 파일 읽기·쓰기 | 안 보임, 쓰기는 "Read-only file system"(N2) |
| 실행 직전 숨김 확인을 일부러 어긋나게(가림 하나 빼기) | 설정 실패, 명령 실행 안 됨 |
| 붙인 시험 Workspace에 쓰기 | 됨 |
| 붙이지 않은 `coder` 폴더, 홈, `/usr`, `/`에 쓰기 | 거부 |
| `/run/user/<uid>/bus`, `/run/dbus/system_bus_socket`, runner 소켓 연결 | 안 보임 |
| 숨기지 않은 읽기 가능 폴더에 둔 시험용 호스트 소켓(`coder` 쓰기 가능)에 연결 | `socket(AF_UNIX)`에서 `EACCES`(N4) |
| io_uring으로 소켓 만들기 | `ENOSYS` |
| `socketpair(AF_UNIX)`, Python asyncio 이벤트 루프 | 됨 |
| 다리 프로세스에 `ptrace` 붙기 | 거부 |
| 호스트 `127.0.0.1`의 시험 리스너(TCP) | 닿지 않음 |
| 프록시로 허용 밖 도메인, 허용 도메인이 사설 주소로 풀리는 경우 | 403 |
| Sandbox 안 loopback 리스너 | 됨(Q8) |
| 명령의 cgroup | `commands` 잎(runner와 다름), `memory.max` 적용 |
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
| 상위 Workspace 마운트가 숨김을 다시 드러냄 | 숨김을 마지막에 읽기 전용으로 가림 + 실행 직전 확인(§6.1) | 명령 도중 새로 생긴 점 항목 |
| 숨김 위치를 링크로 우회 | 명령: 마운트 namespace에서 가림. 파일 Tool: 실제 경로 대조 | 숨김 파일을 가리키는 **이미 있던** 하드링크(새로 만들 수는 없음: 원본 이름이 안 보임) |
| 붙이지 않은 폴더 쓰기 | 마운트 읽기 전용 + Landlock + runner 검사 | Workspace 안의 이미 있던 하드링크로 밖의 파일 내용 바꾸기(명령의 직접 쓰기. 파일 Tool은 rename이라 해당 없음) |
| 호스트 소켓으로 Sandbox 밖 실행 | `/run`·`/tmp` 새로, 네트워크 namespace(추상 소켓), Landlock scope, **seccomp로 `socket(AF_UNIX)`·io_uring 거부**(§6.6) | 스스로 Unix 소켓을 만드는 프로그램이 실패(호환성 비용) |
| push가 모델의 설정·hook을 실행 | bundle은 Sandbox 안, push는 runner의 빈 저장소에서 | — |
| 강제 push·브랜치 삭제 | refspec에 `+`와 삭제를 만들지 않음 | GitHub 브랜치 보호를 함께 권함 |
| 메모리·프로세스 폭주 | 서비스 한도, 명령 전용 cgroup(§6.5) | 서비스 전체 OOM 때 runner 재시작(진행 중 호출은 `UNKNOWN`), 디스크 채우기(한도 없음) |
| 실수로 지우기 | git, Drive 휴지통 | git 밖 로컬 파일은 되돌릴 수 없음 |
| 파일 Tool로 특수 파일 읽기(`/proc/self/environ` 등) | 디스크 파일시스템 허용 목록(`fstatfs`) + 경로 문자열 검사(§8.2) | — |
| 겹치는 Workspace의 동시 쓰기 | 경로 기준 충돌 영역(§9.1) | 하위 폴더 단위의 bind mount 별칭 |
| 프록시 DNS 재바인딩 | 모든 응답 검사 + 검사한 주소로 연결 고정(§11.3) | — |
| HIMARI가 뚫림 | runner가 쓰기 범위를 숨김 목록·특수 파일시스템·`writable_under`와 다시 대조 | `coder` 권한 안의 숨김 아닌 파일 |
| runner가 뚫림 | HIMARI 데이터는 `himari-v3` 소유, runner는 `coder` 권한만 | `coder` 계정의 모든 것 |

## 16. 바뀌는 결정

| 결정 | 바뀌는 것 |
| --- | --- |
| V3-036·V3-037 | `machine`에서는 명령 네트워크를 허용 목록 프록시로 연다(§11). Inside는 그대로 차단 |
| V3-050 | `machine`의 자체 검사 항목을 §13으로 정한다(빈 루트 증명 대신 숨김·쓰기 범위·소켓·프록시 증명) |
| V3-033 | `machine`에서 다른 대화 폴더는 읽기만. 쓰려면 붙인다(§3.2) |
| V3-P40 | `machine`은 실제 경로를 보이고, 읽기는 Workspace 밖도 된다. 하드링크 파일 읽기 허용, 링크는 풀고 나서 검사(§8.3) |
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
| Q5 | 자원 한도? | **명령 전용 cgroup에 메모리 4 GiB·프로세스 1,024개, 서비스 전체 4.5 GiB·CPU 1.5개.** 명령의 메모리 초과는 명령 cgroup 안에서 끝난다. 서비스 전체가 넘치면 runner도 죽을 수 있고, 그때는 다시 뜨며 진행 중 호출은 `UNKNOWN`이다(§6.5, 검수 N6) |
| Q6 | 네트워크 허용 목록? | **§11.3의 기본 목록 + 사설·loopback 주소 거부.** 운영자가 설정에서 더한다 |
| Q7 | push 인증? | **고른 저장소만 쓰는 전용 fine-grained 토큰을 runner 설정에.** `coder`의 gh 토큰은 계정 전체 권한이라 쓰지 않는다 |
| Q8 | Sandbox 안 loopback TCP? | **허용.** namespace 안이라 밖에 닿지 않고, 테스트에 필요하다 |
| Q9 | 호스트 경로 Unix 소켓을 어떻게 막나? | **seccomp로 `socket(AF_UNIX)`와 io_uring을 거부**(§6.6). 커널이 Landlock 경로 소켓 제한을 지원하면 그쪽으로 바꾼다. 대안인 "알려진 소켓 위치만 가리기"는 새로 생기는 소켓을 놓친다. 비용: 스스로 Unix 소켓을 만드는 프로그램(Python 3.14+ `forkserver`, tmux 등)이 이 서버에서는 실패한다 |

## 18. 검증 계획

- **단위:** 숨김 목록 판정(점 항목 규칙, 예외, 품은 폴더), 쓰기 범위 판정(§8.3의 링크 시험 전부), 파일시스템 종류 거부(`/proc/self/environ`, `/sys`, tmpfs), 경로 lock 충돌(같음·조상·형제, FIFO, 한꺼번에 잡기), 작업 위치 규칙(§10.4), Current State 모양, refspec 생성(강제·삭제 없음), push URL 검사, 프록시 이름 정규화·주소 판정(사설·loopback·CGNAT·IPv6·IPv4-mapped·IP 리터럴 표기).
- **프록시:** 허용 이름이 사설 주소로 풀림, 응답 중 하나만 사설, 검사 뒤 DNS 답이 바뀜(재바인딩: 연결은 검사한 주소로), 443 외 포트, keep-alive 중 `Host` 바꾸기.
- **runner 계약:** `SO_PEERCRED` 거부, 디스크립터 전달, 연결 끊김 → 명령 트리 종료, runner 재시작 → 결과 불명.
- **호스트 시험(`HIMARI_SANDBOX_HOST=1`):** §13의 표 전부 + Drive 형태의 FUSE 마운트(가능하면 실제 rclone) 읽기·쓰기, Workspace 안 심볼릭 링크가 숨김 위치를 가리킬 때 명령·파일 Tool 모두 막힘.
- **Unix 소켓(§6.6):** 자체 검사 항목 + 호환성 시험(pip, `python -m pytest`, asyncio, Python 3.12 `multiprocessing` 기본, Maven·Gradle wrapper 빌드, gcc·make, git, npm). 실패하는 것(forkserver 강제, tmux)은 결과 문구 확인.
- **runner 소켓(§4.1):** `himari-v3`로 연결 성공, 다른 사용자 거부, `SO_PEERCRED` 거부(권한을 일부러 넓힌 경우).
- **마운트 순서:** 숨김을 품은 Workspace(홈 전체), 숨김 안의 예외(`.local/bin`), 중첩 Workspace 둘, 숨김 파일(폴더가 아닌 것)에서 최종 트리 확인. bwrap 인자 순서를 고정하는 단위 시험.
- **git push:** bundle `fetch`가 `file` 허용으로 성공하고 push 단계에서는 `file` 원격이 거부됨, 악성 `.git/config`(`core.fsmonitor`, `core.hooksPath`, `include.path`, `credential.helper`, `core.sshCommand`)와 `pre-push` hook을 둔 저장소에서 push해도 Sandbox 밖에서 아무것도 실행되지 않음(표시 파일로 확인). 붙이지 않은 저장소 거부, 허용 밖·`user@`·`http://`·`ssh` 원격 거부, ref가 둘 이상인 bundle 거부, 2 GiB 넘는 bundle 거부, 강제 push가 필요한 상황에서 거부 결과, push 중 같은 Workspace의 명령이 기다림.
- **실제 개발 흐름(라이브, 선택):** 새 대화에서 Python 프로젝트 `pip install` + 테스트, C `make`, Java Gradle wrapper 빌드, git clone → 수정 → commit → push(시험 저장소).
- **회귀:** Inside 대화와 다른 principal은 그대로(전체 pytest, 두 Data 백엔드).

## 19. 구현 단계와 배포

1. **1단계: runner와 머신 프로파일.** `himari/runner/`, 계약(§5), 머신 프로파일(§6, 네트워크 없이), 숨김 목록(§7), 자체 검사(§13), `machine` Environment의 파일 Tool·`run_command`·업로드·파일 보이기·문서·이미지(§8, §9), 대화 폴더만(붙이기 없음), 새 대화 활성화(Q1).
2. **2단계: Workspace 등록과 붙이기.** Data v13(§10.1), Web Workspace 줄과 설정 목록, CLI, Current State, Drive 자동 메모(§14).
3. **3단계: 네트워크.** 프록시와 다리(§11), 캐시 환경 변수(§6.3).
4. **4단계: git push.** §12.

배포(소유자 sudo, 단계마다 스크립트로 준비):
- `himari-runner.socket`(§4.1: `SocketUser=coder`, `SocketGroup=himari-v3`, `SocketMode=0660`)과 `himari-runner.service`(User=coder, `Delegate=yes`, §6.5의 한도), `/etc/himari-runner/runner.toml`. 소켓 권한 배포 시험(§4.1).
- 개발 도구: `openjdk-21-jdk`, `maven`, `cmake`(이 서버에 gcc·make·python3·git은 있음, Java·cmake는 없음, 2026-10-24 확인). Gradle은 프로젝트의 wrapper가 받는다(허용 목록).
- web.env: `HIMARI_MACHINE_SOCKET=/run/himari-runner/runner.sock`. 지우면 새 대화가 다시 `inside`를 쓴다(되돌리기).
- 1단계는 DB 변경이 없고, 2단계에서 v12 → v13(업그레이드 전 백업, V3-P65).
