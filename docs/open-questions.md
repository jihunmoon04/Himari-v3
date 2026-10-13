# 미결정 사항 및 구현 선행조건

기준: **2026-10-13**. 확정된 방향은 [decisions.md](decisions.md), 전체 맥락은 [architecture.md](architecture.md) 참고.

각 항목의 **우선순위는 설계/구현 순서에 대한 제안**이며 사용자 최종 결정은 아니다.

## P0 — 첫 실행 가능한 v3 Skeleton 이전에 확정할 최소 계약

| ID | 주제 | 결정해야 할 내용 | 권장 결정 기준 |
| --- | --- | --- | --- |
| Q-01 | 공개 계약 | 최소 public port, DTO 소유 모듈, 의존성 방향, 오류 타입 | 실제 첫 Run 경로에 필요한 만큼만 정의 |
| Q-02 | Run / Control | Run의 입력/출력 경계, final/question/tool-call 분기, ToolResult의 다음 Run 편입 | Run = 1회 LLM 호출 원칙 유지 |
| Q-03 | Data 최소 모델 | Conversation event, Run, ToolCall/ToolResult, 최신 Snapshot의 저장 단위와 원자성 | 원본 exact 기록과 가공된 prompt 상태 분리 |
| Q-04 | Prompt / Provider | 모델 요청/응답 정규화, 모델/추론 설정, 지원하지 않는 옵션의 처리 | provider별 구현이 Control에 누출되지 않도록 |
| Q-05 | Tool 최소 계약 | Tool ID, namespace, 인자/결과, 실패 표현, Workspace 필요 메타데이터 | Workspace 비의존 Tool과 환경 Tool 모두 수용 |
| Q-06 | Dependency Enforcement | private import 차단 범위, 경계 검사 방식, 계약 테스트 책임 | 초기 변경 비용이 낮은 단순한 강제 방법 |

**P0의 목표**는 모든 기능을 설계하는 것이 아니라, Fake Tool/Provider와 단일 Run을 이용해 Control → Prompt → Provider → ToolResult → 다음 Run/종료 흐름을 안전하게 검증할 수 있게 하는 것이다.

## P1 — 첫 Inside Environment 연결 전 결정

| ID | 주제 | 세부 미결정 |
| --- | --- | --- |
| Q-07 | Workspace Registry | ID 발급·유일성, Workspace type 체계, 실체 주소/비밀 연결정보 처리, 자원 등록·삭제 주체 |
| Q-08 | Environment Instance | Conversation별 활성화 방식, 같은 종류의 복수 instance 허용 여부, 수명/상태 |
| Q-09 | Binding | 식별자, Conversation/Environment 스코프, 자원 공유, 중복 생성, 변경/철회, 재연결 상태 |
| Q-10 | Binding authority | Tool에 binding ID 또는 workspace ID를 노출할지, 스코프 검증 절차, 권한 정책 소유권 |
| Q-11 | Inside execution | host/local/remote 등 Workspace 종류별 명령 실행, 연결, 파일 도구, 실행 프로세스 상태 |
| Q-12 | Security | Sandbox 강제 수준, 경로/네트워크 접근, 사용자 승인 및 자동 승인, capability 변경 |
| Q-13 | Tool call concurrency | 한 Run의 복수 ToolCall, 병렬/순차 실행, 완료 순서 및 중복 실행 방지 |

**확정 전제:** Workspace 수명은 Environment에 종속되지 않으며, 같은 Workspace에 여러 Environment가 다른 방식으로 접근할 수 있다.

## P2 — 기능 확장에 맞춰 결정

| ID | 주제 | 세부 미결정 |
| --- | --- | --- |
| Q-14 | Prompt compaction | 압축 trigger, 요약 품질, context budget, Snapshot 교체/복구, exact tail 선정 |
| Q-15 | Reasoning / continuation | provider가 실제로 공개한 usage·reasoning continuation의 저장 범위, 지원 여부 및 비밀 처리 |
| Q-16 | Memory Tool | 읽기/쓰기 Tool 표면, Data 조회 방식, 기억의 선택·검증·삭제, 자동 컨텍스트 삽입 여부 |
| Q-17 | Plan Tool | `update_plan` 데이터 모델, 보여줄 상태, 저장/복구 정책, Run과의 관계 |
| Q-18 | Streaming / recovery | 출력 스트리밍 이벤트, 중단된 LLM 호출/미완료 Tool 처리, resume/cancel 경로 |
| Q-19 | Retention & privacy | Tool/Prompt 원문 보존 분류, 사용자 파일/비밀 정보, 삭제 및 감사 규칙 |
| Q-20 | v2 migration | 어떤 사용자 데이터와 구현을 가져올지, 마이그레이션 필요성 및 검증 기준 |

## 의도적으로 미리 고정하지 않는 사항

- 특정 Python 패키지 트리 또는 세부 함수 시그니처
- 범용 Workspace 생성/삭제 API의 단일 구현
- Inside의 명령 실행 및 자동 승인 정책
- Memory의 자동 업데이트 알고리즘
- DB 스키마를 곧바로 공개 API로 취급하는 방식
- 모든 Environment에 동일한 실행기와 권한 체계를 강요하는 방식

## 제안하는 다음 단계

1. **Q-01~06을 최소 단위로 설계**: public 계약과 Fake 구현만으로 Run loop 증명.
2. **Q-07~10의 Workspace/Binding 모델 확정**: 물리 Workspace와 권한 관계의 분리 검증.
3. **Inside (Q-11~12) 구현**: 첫 구체적인 Environment를 붙여 교체 테스트 수행.
4. Memory·Plan·압축과 Provider 다변화는 공개 계약을 유지하며 추가.

각 단계에서 모듈 구현은 계약 테스트를 통과해야 하며, 변경이 외부 모듈의 내부 수정까지 요구하는 경우에는 경계 설계를 재검토한다.
