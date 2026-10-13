# 남은 미결정 사항과 구현 선행조건

기준: **2026-10-13**. 이 문서는 아직 확정되지 않은 계약만 관리한다. 이미 합의한 방향은 [Decision Register](decisions.md)에 둔다.

연관: [Prompt Composition](prompt-composition.md) · [Data / Tools / Artifacts](data-and-tools.md) · [Data Contract v0.1 — 검토 초안](data-contract-v0.1.md) · [CurrentState Contract v0.1 — 검토 초안](current-state-contract-v0.1.md) · [Module Boundaries](module-boundaries.md)

Data 계약 초안에 **DC-01~DC-10 검토 쟁점**을 정리했다. 기존 Q-01~Q-06은 아직 완료가 아닌 미결정으로 유지한다.

**[통합 모듈 계약 기준서](contracts.md)**에 Owner / Provider / Consumer / Source of Truth / Contract Location 메타데이터가 채워진 **검토용 공개 Port 인벤토리**를 작성했다. **메타데이터 작성 표준 자체는 V3-029로 확정**되었지만 각 함수/DTO 시그니처는 아직 확정되지 않았다.

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
| Q-01 | Public Ports / DTO | 모듈별 public API 소유, 정규화된 입력/출력, 오류 모델, 의존성 주입 위치 | **1** |
| Q-02 | Control / Run | **동일 Conversation 직렬화는 확정**. final/question/tool-call 정규화, 복수 호출 종료 배리어·다음 Run 경계, 사용자 재개 방식과 lock/lease 복구 구현 | **2** |
| Q-03 | Data schema | **kind별 불변 Payload는 확정**. 종류별 구체 schema, Run·Execution·Event의 원자적 기록 순서, ID/cursor, raw 저장 형식, 조회 API | **1** |
| Q-04 | Prompt / Provider boundary | provider-neutral 메시지 표현, Current State의 provider별 안전한 role/위치, 누락/불호환 기능 처리 | **2** |
| Q-05 | Tool API | 스키마/네임스페이스, Internal/Environment 디스패치, Workspace 요구 플래그, 결과 표준형/기록 정책 | **2** |
| Q-06 | Contract enforcement | Private import 차단, 모듈 교체/계약 테스트, 최소 필수 실패 시나리오 | **3** |
| Q-19a | Raw retention security | 저장될 원본의 인증/비밀 정보 제외, 접근 권한, 안전한 실패 처리 | **1** |
| Q-21 | Trigger inclusion | **Prompt의 기록 선택·검증과 Provider의 native 직렬화 소유권은 확정**. ToolCall 그룹 결손 검사·오류 형식, Conversation 밖의 결과 포함 시점 | **2** |
| Q-22 | Prompt cursor | Latest Snapshot의 coverage/cursor와 Conversation 최신 위치 및 Artifact Revision 정합성 | **3** |

P0 완료 목표: **Fake Data/Provider/Tool로 모델 호출 1회 + tool group + 다음 Run + final/question** 경로를 검증하고, 저장된 Raw 요청/응답 및 정확한 Conversation을 확인한다. 현재 코드 Skeleton은 아직 생성되지 않았다.

## P1 — 첫 Inside Environment 및 실질 Tool 실행 전

| ID | 주제 | 아직 확정해야 할 계약 |
| --- | --- | --- |
| Q-07 | Workspace Registry | ID 발급·유일성, 이종 Workspace 메타데이터/비밀 연결정보, 생성/삭제 소유자 |
| Q-08 | Environment Instance | Conversation 활성화/비활성화, 종류당 여러 Instance 허용 여부, 연결 수명 |
| Q-09 | Binding | Binding ID·스코프·철회/재연결·공유, Conversation 간 같은 Workspace 참조 |
| Q-10 | Binding authority | Tool에서 Binding/Workspace 중 어느 ID를 노출할지, 실행 시 권한 검증 |
| Q-11 | Inside Execution | workspace type별 파일/명령 실행, 프로세스 수명, 연결 실패 처리 |
| Q-12 | Safety/Approval | OS sandbox, 파일·네트워크 정책, 승인/자동 승인, 실패/권한 우회 차단 |
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
| Q-28 | Conversation serialization | 단일 Control 흐름/Run 직렬화를 구현할 lease/queue의 범위, 취소/중단 후 ownership 회수 및 신규 사용자 입력 정책 |

## 다음 설계 작업 제안

1. **Data Event/Run 원본 계약과 최소 Public Port (Q-01, Q-03, Q-19a)**. 이미 확정된 네 데이터 원칙과 [Data Contract v0.1](data-contract-v0.1.md)의 미정 필드를 구분한다.
2. **Prompt → Provider 표준 입력 계약 (Q-04, Q-21, Q-22, Q-27)** — [CurrentState 초안](current-state-contract-v0.1.md)의 CS-01~08 검토를 전제로 Provider-native 호출의 합법성 검증.
3. **Control/Tool 계약 (Q-02, Q-05, Q-28)** — 동일 Conversation의 직렬화는 지키되 Tool 병렬성·호출 그룹 종료·오류·실행 기록의 일관성 확인.
4. 그다음 **Workspace Binding + Inside Scheduler (Q-07~13, Q-23~24)**.
5. Artifact·Memory·Plan·Compaction과 실제 Provider 캐시 튜닝은 각 기능의 상세 설계 단계에서 결정.

실제 구현 전까지 '정책 방향 확정'과 '구체 알고리즘·스키마 미결정'을 혼동하지 않는다.
