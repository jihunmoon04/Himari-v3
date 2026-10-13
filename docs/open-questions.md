# 남은 미결정 사항과 구현 선행조건

기준: **2026-10-13**. 이 문서는 구현된 범위와 남은 미결정 계약을 구분해 관리한다. 이미 합의한 방향은 [Decision Register](decisions.md)에 둔다.

연관: [Prompt Composition](prompt-composition.md) · [Data / Tools / Artifacts](data-and-tools.md) · [Data Contract v0.2 — 검수 후보](data-contract-v0.2.md) · [CurrentState Contract v0.1 — 검토 초안](current-state-contract-v0.1.md) · [Module Boundaries](module-boundaries.md)

Data [v0.2 구현 가능 후보](data-contract-v0.2.md)에 **DR-01~DR-09 독립 검수 항목**, 이전 [v0.1](data-contract-v0.1.md)에 DC-01~DC-10 설계 쟁점을 정리했다. [Control–Prompt–Provider v0.1](control-prompt-provider-v0.1.md)에 **CP-01~CP-07 경계 검수 항목**이 있다. 기존 Q-01~Q-06은 아직 완료가 아닌 미결정으로 유지한다.

**[통합 모듈 계약 기준서](contracts.md)**에 Owner / Provider / Consumer / Source of Truth / Contract Location 메타데이터가 채워진 **검토용 공개 Port 인벤토리**를 작성했다. **메타데이터 작성 표준 자체는 V3-029로 확정**되었지만 각 함수/DTO 시그니처는 아직 확정되지 않았다.

**테스트 범위 정책 V3-038 확정:** 공개 계약 변경 때 전체 pytest, 내부 구현 변경 시 해당 모듈 pytest. 실제 `tests/`와 정적 CI가 존재하며 OpenRouter 라이브 테스트는 opt-in이다([Testing Policy](testing-policy.md)).

## 이미 해결된 설계 방향

- **Run = 모델 호출 1회**, Control이 final/question에서 반복 종료.
- **원본 Conversation exact 보존**, 최신 Prompt Snapshot은 1개.
- **모든 Run의 Raw Provider request/response 보존**(비밀/직렬화 세부 예외는 추가 설계).
- **Prompt 순서는 Stable Prefix → Conversation → Current State**, Provider 설정 및 Reasoning은 네이티브 API 필드로 전달.
- **Trigger는 기존 Event의 참조**이며 Run 간 이동하거나 복제하지 않음.
- **ToolCall ↔ ToolResult는 native group으로 연결**하고 호출 ID를 보존.
- **충돌이 없는 복수 ToolCall 병렬 실행 허용**. 명령 문자열 같음/다름은 충돌 증명이 아님.
- **Workspace 전용 코드 모듈 없음**. Workspace ID + Environment Binding, Environment별 접근 계약.
- **활성 Workspace/Environment는 Artifact가 아닌 Data 운영 상태**(Registry/Activation/Binding/Settings). 실제 온라인/접근 가능 여부는 Environment에서 검증하고, Prompt는 CurrentStateProjection으로 읽는다.
- **동일 Conversation의 실행 흐름/Run 직렬화**(내부 독립 ToolCall 병렬 허용; 구체 lease/queue 방식만 미정).
- **ConversationEvent는 kind별 불변 canonical payload**(정확한 enum/schema는 미정).
- **Prompt가 모델 문맥에 필요한 ToolResult를 Execution Records에서 선택하고 정합성을 검증하며, Provider가 native 구조로 직렬화**한다.
- **Plan/Note 원본은 Data의 conversation-scoped Artifact**에 저장한다; Outside의 기본 Workspace는 필요시 projection/export.
- Tool 실패는 결과로 모델에 반환. Memory는 Data 공개 API를 이용하는 Tool 방향; Plan/Note는 간결한 결과 + 별도 상태.

## P0 — 현재 구현 현황과 잔여 계약

**PR #1~#9 병합 완료(2026-10-13), 영속 Data 검수 중:** `api.py` 공개 계약, 인메모리 Data, Control Run 반복·`ask_user`·`resume`, Tool Registry/Dispatcher, T3/T6 claim·UNKNOWN 모델 보고, 실제 OpenRouter Provider(wire-exact) 및 턴별 reasoning continuation이 구현됐다. Inside 파일 Tool·Sandbox `run_command`(PR #8~#9), SQLite 영속 Data(V3-P42)도 구현됐다. `tests/`·정적 CI도 있다. **Web/CLI는 미구현**이다.

| ID | 주제 | 현재 상태 / 남은 검수 |
| --- | --- | --- |
| Q-01 | Public Ports / DTO | `api.py`·불변 DTO 구현(PR #2), 코드가 원본 |
| Q-02 | Control / Run | Run·`ask_user`·`resume`·UNKNOWN 처리 P0 구현(PR #3~#4). 정식 Pause/Cancel·lease 미구현 |
| Q-03 | Data schema | Event/Run/Tool/Artifact 인메모리 + SQLite 스키마 v1 구현(V3-P42). 마이그레이션 정책 미정 |
| Q-04 | Prompt / Provider | Composer, Tool 그룹 재생, OpenRouter wire-exact·continuation 구현(PR #3/#5/#6). Streaming/타 Provider는 후속 |
| Q-05 | Tool API | ToolHandler·Registry·Dispatcher·ToolOutcome 구현. 파일/명령 및 Scheduler 후속 |
| Q-06 | Contract enforcement | `tests/`, `scripts/check_imports.py`, mypy·정적 CI·opt-in 라이브 테스트 구현 |
| Q-19a | Raw retention security | 본문/인증 헤더 분리 구현. SQLite 파일은 0600(소유자만). 저장 데이터 암호화·보존·삭제 정책 미결정 |
| Q-21 | Trigger inclusion | 기본 Run/Event 참조와 Tool 그룹 검증 구현. 압축/외부 결과 포함 정책 후속 |
| Q-22 | Prompt cursor | 최신 Snapshot Port 존재. coverage/cursor 검증·실질 압축 사용 미구현 |

**검증 경계:** Data·통합 테스트는 인메모리와 SQLite 양쪽에서 실행된다. SQLite 재시작은 DB 파일을 닫고 다시 여는 것이며, 실제 `SIGKILL` 크래시 시험이 있다(Q-41). 전원 차단은 직접 시험하지 않았다.

## P0-F — 기본 Workspace 파일 기능 (이번에 우선 진행)

**현행 확정 (V3-030 + V3-032~036):** 기본 Workspace에 파일 첨부·생성·수정·다운로드 및 `inside/run_command` 포함. **사용자/principal별 공유 Workspace Root** 아래 Conversation별 cwd, 허용된 같은 사용자 다른 Conversation 폴더 접근. **OS Sandbox + 초기 차단 전용 네트워크 프로파일(V3-037)**. 인간 Approval 및 Worktree 복제 격리 미도입. V3-031은 Superseded.

**[Default Workspace & Files v0.2](default-workspace-files-v0.2.md) 제안에서 아직 결정할 것:**

| ID | 항목 | 미결정 |
| --- | --- | --- |
| Q-29 | 기본 Workspace 스코프 | **사용자별 공유 Root + Conversation별 기본 폴더 + 같은 사용자 간 교차 접근 확정**. 정확한 principal 인증/경로 매핑·Binding 자동 등록 구현 미결정 |
| Q-30 | 파일 바이트/메타데이터 | FileAssetRecord, upload original, version/digest, Workspace 파일과 Data 참조 정합성 |
| Q-31 | 첨부·다운로드 | 스트리밍 제한, file_id 기반 다운로드, 접근 권한, Prompt file manifest 및 미디어 처리 |
| Q-32 | 파일 I/O 계약 | 파일 Tool·`inside/run_command`, 사용자별 Root/cwd에 맞춘 v2 Sandbox 수정, symlink/hardlink/mount 별칭 차단, 명령 효과 충돌, 실행 제한 |
| Q-33 | 파일 생명주기 | Conversation 삭제가 공유 Workspace 파일을 어떻게 참조 해제·GC할지 |

**권고:** 기본 파일 Tool과 bounded `inside/run_command`를 함께 개발하되, 장기 ManagedProcess는 별도 수명/복구 계약으로 미룬다. **네트워크 차단 전용 프로파일**과 Sandbox 없는 실행 fallback 금지는 필수 방향. [Sandbox Execution v0.1](sandbox-execution-v0.1.md)은 정책 원칙, [Sandbox Implementation Candidate v0.2](sandbox-implementation-v0.2.md)는 v2 코드별 이식 경계·기동 검증·실행 결과 계약을 담은 상세 초안이다.

## P0-C — Control Pause/Resume (설계 초안)

**[Control Pause/Resume v0.1](control-pause-v0.1.md) 제안:** Run raw I/O 보존만으로 정지·재개는 충분하지 않다. Conversation에 `ControlExecutionState` 및 execution cursor를 보존해 PAUSE_REQUESTED → PAUSED의 안전 경계, 미실행 ToolCall 및 부분 완료 결과를 구분해야 한다. **Pause 실행 정책은 아직 확정하지 않았다.**

| ID | 항목 | 미결정 |
| --- | --- | --- |
| Q-34 | Pause semantics | Provider in-flight soft pause 후 Run 응답 저장, ToolCall pending 처리, 요청 지연 한계 |
| Q-35 | Resume cursor | pending/group-complete 여부, 중복 Tool/Run 재실행 방지, 사용자 입력 큐 |
| Q-36 | Control state authority | **lease/fence·다중 worker 경합은 P2(V3-045)**. P0 남은 것: cancel / final / `ask_user` 대기 우선순위 |
| Q-37 | ~~UNKNOWN 해소 (P0)~~ | **해결: V3-047 (2026-10-13).** 모델에게 "결과 불명"으로 알리고 모델이 확인한다. `REQUIRE_USER` Tool의 사용자 해소 UI·API는 그런 Tool이 처음 생길 때 정의 |
| Q-38 | ask_user 세부 | 대기 취소·만료, 답변 첨부파일의 Provider별 ToolResult 표현, `ask_user` 혼합 허용(V3-P15) |

## P1 — 첫 Inside Environment 및 실질 Tool 실행 전

| ID | 주제 | 아직 확정해야 할 계약 |
| --- | --- | --- |
| Q-07 | Workspace Registry | ID 발급·유일성, 이종 Workspace 메타데이터/비밀 연결정보, 생성/삭제 소유자 |
| Q-08 | Environment Instance | Conversation 활성화/비활성화, 종류당 여러 Instance 허용 여부, 연결 수명 |
| Q-09 | Binding | Binding ID·스코프·철회/재연결·공유, Conversation 간 같은 Workspace 참조 |
| Q-10 | Binding authority | Tool에서 Binding/Workspace 중 어느 ID를 노출할지, 실행 시 권한 검증 |
| Q-11 | Inside Execution | **기본 파일 I/O와 bounded `run_command`는 P0-F로 선행**. 이후 ManagedProcess 수명/영속 프로세스 복구, 환경 접속 실패 처리 |
| Q-12 | Safety/Approval | **Sandbox + 네트워크 차단 전용 프로파일 확정**. [v0.2 구현 계약 초안](sandbox-implementation-v0.2.md)의 host gate/원본 Root·cwd 권한/네트워크·Unix socket 테스트 검수 및 실행 제한 값 확정 필요 |
| Q-13 | Scheduler 상세 | verified read/write footprint, 같은 Workspace 명령 기본 직렬, 다른 Conversation 충돌, 락/종결 배리어/최대 병렬수 |
| Q-23 | Tool execution durability | Tool 의도 저장·시작/완료·취소, DB 밖에서 발생한 부작용과 중단 후 결과 불명 처리 |
| Q-24 | Internal vs Environment routing | 실행 유형 등록 계약, `None/Required/Default` Workspace 정책, 외부 서비스 Tool 범주 필요 여부 |
| Q-39 | ~~Tool error disclosure~~ | **해결 (PR C1):** claim 없는 Tool의 예외는 모델에게 "The tool failed with an internal error."만 전달하고, 예외 타입·원문은 `raw_result`에만 남긴다. Inside 파일 Tool은 오류를 분류된 안전한 문구로만 돌려준다. |
| Q-40 | 실제 OS 권한 검증 | **부분 해결 (PR C2):** 시작 시 실호스트 자체 검사(V3-050/P41)와 선택 실행 호스트 게이트(`HIMARI_SANDBOX_HOST=1 pytest tests/sandbox_host`, 29개)를 이 서버에서 통과. 남은 것: 다른 배포 호스트에서의 게이트 실행, 권한이 필요한 마운트 넘기(`NO_XDEV`) 검증, CPU·메모리·프로세스 수 한도(cgroup/rlimit), Sandbox 생성이 멈췄을 때의 운영자 복구 절차(취소된 호출은 fail closed로 Workspace lock을 계속 잡고 있음), Workspace admission이 시점 검사라는 한계(P0는 단일 서버 프로세스이고 Workspace 변경이 Inside Tool과 같은 Workspace lock 아래에서만 일어난다고 전제한다. 다중 worker, 업로드 경로, 호스트 측 쓰기를 추가하면 같은 lock을 쓰게 하거나 검사를 다시 설계해야 함) |
| Q-41 | 영속 DB 크래시 내구성 | **부분 해결 (V3-P42):** SQLite 백엔드로 T1~T9·T7a·T-R을 포트 호출당 트랜잭션 1개로 구현. Data·통합 테스트를 두 백엔드로 실행(재시작 = DB 다시 열기), 모든 다중 쓰기 연산에 대해 각 쓰기 직후 오류 주입 시 전부 롤백, 실제 `SIGKILL` 시험(송신 중 → `OUTCOME_UNKNOWN`·재송신 없음, 부작용 중 → `UNKNOWN`·재실행 없음, 트랜잭션 도중 → 이전 상태, 커밋 직후 → 커밋 유지). 남은 것: 전원 차단 내구성은 SQLite·파일시스템 fsync 보장에 의존(직접 시험 불가), 스키마 마이그레이션, 백업, 다중 프로세스(P2 lease) |
| Q-42 | Continuation 일괄 조회 | Prompt loader가 assistant 턴마다 `read_run`을 호출한다. 영속 DB 도입 시 Run continuation을 한 번에 읽는 내부 최적화를 둔다(공개 계약 변경 없이 가능). *(PR #6 검수 C1)* |
| Q-43 | Continuation 크기 예산 | 암호화 reasoning은 큰 불투명 payload일 수 있고 지금은 모든 과거 턴의 continuation을 재전송한다. 압축(Q-14) 도입 시 턴과 그 continuation을 한 단위로 제외하는 예산 정책을 정한다. *(PR #6 검수 C2)* |

P1은 Workspace 전용 소프트웨어 모듈을 다시 도입하는 단계가 아니다. Data Registry + Environment 접근 계약을 사용한다.

## P2 — 이후 기능 확장에 맞춰 결정

| ID | 주제 | 아직 확정해야 할 계약 |
| --- | --- | --- |
| Q-14 | Compaction | 압축 시점, 문맥 예산, 요약 품질, exact tail 경계, Snapshot 교체/복구 |
| Q-15 | Reasoning / continuation 확장 | **기본 턴별 저장·재생은 PR #6으로 구현(V3-049)**. 남은 것: 타 모델 지원, 압축 시 제외, cache-control·예산(Q-43) |
| Q-16 | Memory Tool | 기억 생성·조회·삭제 정책, 자동 삽입/검증/권한, Data 읽기 경계 |
| Q-17 | Plan / Note Artifact | **원본은 Data conversation-scoped Artifact로 확정**. Revision/CAS, schema, 삭제/복구·표시, Outside 노출/export |
| Q-18 | Streaming / recovery | chunk-level 원본 기록, 스트리밍 UI, 중단된 모델 호출, resume/cancel |
| Q-19 | Lifecycle/privacy | 보존 기간, 개인 정보 삭제, Conversation·Run·Artifact 연쇄 제거, 접근 감사 |
| Q-20 | v2 migration | 데이터/기능 마이그레이션 범위, v2 검증된 Provider replay 규칙 선택적 재사용 |
| Q-25 | Cache performance | tool schema 안정성, provider별 marker/TTL/히트 관측, 배치 순서에 따른 비용·품질 비교 |
| Q-26 | Artifact origin atomicity | Internal Tool의 Artifact 변경 + ToolResult/Event 원자 트랜잭션, revision 중복 방지 |
| Q-27 | CurrentStateProjection | **CS-01~08 초안 작성됨**. 합성 소유 위치, 기본 Binding 선택 스코프, Data consistency/version token, Environment 연결 관측, Provider-native 표현의 최종 검수·확정 필요 |
| Q-28 | Conversation serialization | **P0는 프로세스 내 lock(V3-045)**. lock은 크래시 후 중복을 막지 않으며 그것은 Data T3/T6/T-R 담당. P2: lease/queue 범위, 취소/중단 후 ownership 회수. 신규 사용자 입력 정책은 P0에서 결정 |

## 다음 구현 작업 제안 (2026-10-13, PR #1~#6 반영)

1. **영속 Data** — SQLite 등으로 Event/Run/ToolExecution/Artifact/Continuation 저장, T2~T7a·T-R 원자성 및 실제 crash 복구 검증(Q-41).
2. **Inside Environment + Sandbox** — 사용자별 Root와 Conversation cwd, 파일 Tool·bounded `inside/run_command`, Bubblewrap/Landlock·네트워크 차단 실호스트 증거(Q-29~33/Q-39~40).
3. **최소 CLI** — 실 Provider와 실제 Workspace를 연결한 단대단 사용 경로 검증.
4. **Context 압축·예산 및 최신 Snapshot 활용** — exact tail, continuation 일괄 조회/크기 예산(Q-14/Q-22/Q-42/Q-43).
5. **후속 기능** — 정식 Pause/Cancel, Streaming, Web UI, Memory·Plan Tool, 충돌 Scheduler.

위 순서는 제안이며 새로운 확정 결정이 아니다. **설계 방향의 확정과 구현·실호스트 검증 완료는 구별한다.**
