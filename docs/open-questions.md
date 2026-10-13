# 남은 미결정 사항과 구현 선행조건

기준: **2026-10-13**. 이 문서는 아직 확정되지 않은 계약만 관리한다. 이미 합의한 방향은 [Decision Register](decisions.md)에 둔다.

연관: [Prompt Composition](prompt-composition.md) · [Data / Tools / Artifacts](data-and-tools.md) · [Data Contract v0.2 — 검수 후보](data-contract-v0.2.md) · [CurrentState Contract v0.1 — 검토 초안](current-state-contract-v0.1.md) · [Module Boundaries](module-boundaries.md)

Data [v0.2 구현 가능 후보](data-contract-v0.2.md)에 **DR-01~DR-09 독립 검수 항목**, 이전 [v0.1](data-contract-v0.1.md)에 DC-01~DC-10 설계 쟁점을 정리했다. [Control–Prompt–Provider v0.1](control-prompt-provider-v0.1.md)에 **CP-01~CP-07 경계 검수 항목**이 있다. 기존 Q-01~Q-06은 아직 완료가 아닌 미결정으로 유지한다.

**[통합 모듈 계약 기준서](contracts.md)**에 Owner / Provider / Consumer / Source of Truth / Contract Location 메타데이터가 채워진 **검토용 공개 Port 인벤토리**를 작성했다. **메타데이터 작성 표준 자체는 V3-029로 확정**되었지만 각 함수/DTO 시그니처는 아직 확정되지 않았다.

**테스트 범위 정책 V3-038 확정:** 계약 변경 때만 전체 pytest, 그 외 수정된 모듈 pytest만 수행한다. 테스트 구조/명령 예시는 [Testing Policy](testing-policy.md). 실제 Python 테스트 디렉터리는 아직 생성되지 않았다.

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

## P0 — 최소 v3 Skeleton 및 첫 실제 Run 이전

| ID | 주제 | 아직 확정해야 할 계약 | 권장 순서 |
| --- | --- | --- | --- |
| Q-01 | Public Ports / DTO | **방향 확정(V3-039/040/046)**: Data sub-port 분리, SPI 3종(`ToolHandler`/`ProviderAdapter`/`EnvironmentPlugin`) 우선, `api.py` 코드 선언. 남은 것: 실제 시그니처·오류 모델 | **1** |
| Q-02 | Control / Run | **동일 Conversation 직렬화는 확정, P0는 프로세스 내 lock(V3-045)**. question은 `ask_user`(V3-044)로 정규화가 `FINAL \| TOOL_CALLS`로 단순화. 남은 것: 복수 호출 종료 배리어·`ask_user` 혼합(V3-P15) | **2** |
| Q-03 | Data schema | kind별 불변 Payload 원칙 확정. **[Data v0.2](data-contract-v0.2.md)**에 DTO/state/트랜잭션/Port 후보 구체화; DR-01~09 검수·freeze 필요 | **1** |
| Q-04 | Prompt / Provider boundary | **[Control–Prompt–Provider v0.1](control-prompt-provider-v0.1.md)**에 중립 입력/native prepare/send/normalize 초안. Provider별 role/SDK exact raw capture 검증 및 CP-01~07 확정 필요 | **2** |
| Q-05 | Tool API | **방향 확정(V3-041/042)**: Environment가 `tools()`로 자기 Tool 제공, Tool 구현은 `ToolOutcome` 반환·Dispatcher가 커밋. 남은 것: `ToolDefinition`/`ToolContext`/`ToolOutcome` 필드, 네임스페이스, 결과 기록 정책 | **1** |
| Q-06 | Contract enforcement | import-linter + SPI conformance suite + Fake 교체 테스트를 첫 Skeleton부터 도입(V3-046). 남은 것: 도구 선택·최소 실패 시나리오 | **2** |
| Q-19a | Raw retention security | 저장될 원본의 인증/비밀 정보 제외, 접근 권한, 안전한 실패 처리 | **1** |
| Q-21 | Trigger inclusion | **Prompt의 기록 선택·검증과 Provider의 native 직렬화 소유권은 확정**. ToolCall 그룹 결손 검사·오류 형식, Conversation 밖의 결과 포함 시점 | **2** |
| Q-22 | Prompt cursor | Latest Snapshot의 coverage/cursor와 Conversation 최신 위치 및 Artifact Revision 정합성 | **3** |

P0 완료 목표: **Fake Data/Provider/Tool로 모델 호출 1회 + tool group + 다음 Run + final/question** 경로를 검증하고, 저장된 Raw 요청/응답 및 정확한 Conversation을 확인한다. 현재 코드 Skeleton은 아직 생성되지 않았다.

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
| Q-36 | Control state authority | **lease/fence·다중 worker 크래시 복구는 P2(V3-045)**. P0 남은 것: cancel / final / `ask_user` 대기 우선순위 |

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

P1은 Workspace 전용 소프트웨어 모듈을 다시 도입하는 단계가 아니다. Data Registry + Environment 접근 계약을 사용한다.

## P2 — 이후 기능 확장에 맞춰 결정

| ID | 주제 | 아직 확정해야 할 계약 |
| --- | --- | --- |
| Q-14 | Compaction | 압축 시점, 문맥 예산, 요약 품질, exact tail 경계, Snapshot 교체/복구 |
| Q-15 | Reasoning / continuation | provider별 opaque 자료 보존/앵커 재구성/교차 모델 호환 및 cache-control |
| Q-16 | Memory Tool | 기억 생성·조회·삭제 정책, 자동 삽입/검증/권한, Data 읽기 경계 |
| Q-17 | Plan / Note Artifact | **원본은 Data conversation-scoped Artifact로 확정**. Revision/CAS, schema, 삭제/복구·표시, Outside 노출/export |
| Q-18 | Streaming / recovery | chunk-level 원본 기록, 스트리밍 UI, 중단된 모델 호출, resume/cancel |
| Q-19 | Lifecycle/privacy | 보존 기간, 개인 정보 삭제, Conversation·Run·Artifact 연쇄 제거, 접근 감사 |
| Q-20 | v2 migration | 데이터/기능 마이그레이션 범위, v2 검증된 Provider replay 규칙 선택적 재사용 |
| Q-25 | Cache performance | tool schema 안정성, provider별 marker/TTL/히트 관측, 배치 순서에 따른 비용·품질 비교 |
| Q-26 | Artifact origin atomicity | Internal Tool의 Artifact 변경 + ToolResult/Event 원자 트랜잭션, revision 중복 방지 |
| Q-27 | CurrentStateProjection | **CS-01~08 초안 작성됨**. 합성 소유 위치, 기본 Binding 선택 스코프, Data consistency/version token, Environment 연결 관측, Provider-native 표현의 최종 검수·확정 필요 |
| Q-28 | Conversation serialization | **P0는 프로세스 내 lock(V3-045)**. P2: lease/queue 범위, 취소/중단 후 ownership 회수. 신규 사용자 입력 정책은 P0에서 결정 |

## 다음 설계 작업 제안 (2026-10-13 개정: V3-039~046 반영)

문서를 더 늘리기보다 **최소 코드 Skeleton으로 계약을 검증**하는 단계로 넘어간다.

1. **SPI·Port를 코드로 선언 (Q-01, Q-05, Q-06)** — `tool/api.py`(`ToolHandler`, `ToolOutcome`), `provider/api.py`(`ProviderAdapter`), `environment/api.py`(`EnvironmentPlugin`), `prompt/api.py`(`PromptInputs`, `compose`), `data/api.py`(`ConversationLog`, `ArtifactStore`, `WorkspaceState`, `StateQuery`). import-linter 설정 포함.
2. **Fake로 핵심 경로 검증 (P0 완료 목표)** — Fake Provider/Tool + 인메모리 Data로 `User → Run → ToolCall 그룹 → Run → FINAL`, `ask_user → WAITING_FOR_USER → 응답 → Run` 경로.
3. **Data v0.2·CPP v0.1 잔여 검수** — P0 범위(V3-045)로 줄어든 DR/CP 항목만 확정. fencing/lease/claim CAS는 P2.
4. **Inside Environment + Sandbox(v2 이식) (P0-F, Q-29~33)** — `EnvironmentPlugin`으로 구현.
5. Pause/Resume(P0-C)은 P0 lock 위에서 최소 형태로, 영속 ControlState는 P2. Artifact·Memory·Compaction·캐시 튜닝은 각 기능 단계에서 결정.

실제 구현 전까지 '정책 방향 확정'과 '구체 알고리즘·스키마 미결정'을 혼동하지 않는다.
