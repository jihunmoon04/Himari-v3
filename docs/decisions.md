# 설계 결정 기록 (Decision Register)

기록 시점: **2026-10-13**. 이 문서는 지금까지의 대화에서 채택한 방향을 요약한다.

- **Confirmed**: 사용자가 명시적으로 결정하거나 동의한 아키텍처 원칙.
- **Proposed**: 해당 원칙을 구현하는 권고안. 명칭·데이터 모델까지 확정된 것은 아님.
- **Open**: 아직 선택하지 않았거나 구현 정책을 보류한 항목.

## A. 확정된 설계 원칙

| ID | 상태 | 결정 |
| --- | --- | --- |
| V3-001 | Confirmed | **Run = LLM 요청/응답 1회**. 여러 Run의 집합을 Task로 만드는 실행 기본 계층을 사용하지 않는다. |
| V3-002 | Confirmed | 별도 **Control** 모듈이 Run 반복을 제어하며, 모델의 final response 또는 question 이후 반복을 중단한다. |
| V3-003 | Confirmed | Data의 **Conversation 원본 입력/출력은 exact**. Prompt용 가공 데이터와 분리한다. |
| V3-004 | Confirmed | **Prompt**는 Data의 기록을 이용하여 다음 LLM 입력을 구성한다. 최신 Prompt Snapshot은 **1개**만 보존하며 압축 문맥 + 최근 exact 항목 방식이다. |
| V3-005 | Confirmed | Tool 실패는 **실패 ToolResult를 모델에 전달(A)**한다. Runtime이 의미론적 자동 복구를 대신하지 않는다. |
| V3-006 | Confirmed | reasoning 관련 설정/이용 가능한 provider 결과는 Data에서 관리하고 Prompt가 다음 요청에 반영하는 방향이다. provider 비공개 내부 추론 텍스트의 저장을 전제하지 않는다. |
| V3-007 | Confirmed | Memory는 **Tool을 통한 Data 접근/구성** 방향으로 처리한다. 세부 정책은 나중에 정한다. |
| V3-008 | Confirmed | Conversation에 **여러 Environment**가 동시에 활성화될 수 있다. 환경별 Tool 노출은 `environment/tool` 네임스페이스 형태를 사용한다. |
| V3-009 | Confirmed | **Environment가 Workspace의 사용 방법을 규정**한다. Workspace는 환경에 종속된 수명을 가지지 않는다. |
| V3-010 | Confirmed | Workspace는 기본 작업 공간/사용자 로컬 컴퓨터 등 다양한 형태이며, 동일 Workspace를 서로 다른 Environment가 다르게 사용할 수 있다. |
| V3-011 | Confirmed | Tool은 **Workspace 지정 필수/불필요**로 나뉜다. `run_command`는 전자, `update_plan`은 후자다. |
| V3-012 | Confirmed | **Workspace ID로 식별**하며 Workspace Registry와 Environment–Workspace **Binding을 구분**하는 설계 방향을 채택한다. |
| V3-013 | Confirmed | 모듈 내부 변경이 외부 계약을 지키는 한 다른 모듈의 변경을 요구하지 않도록 **계약 기반 모듈화**를 강제한다. |
| V3-014 | Confirmed | 먼저 구체화할 환경은 **Inside**. Inside의 실제 실행/승인 정책은 이 단계에서 결정하지 않는다. |

## B. 현재 채택을 권고하지만 세부 확정 전인 설계

| ID | 상태 | 권고안 |
| --- | --- | --- |
| V3-P01 | Proposed | `workspace_id`, `environment_instance_id`, `binding_id`를 구별하고 외부 노출 ID는 불투명한 값으로 표현한다. |
| V3-P02 | Proposed | Workspace 필요 Tool은 실제 자원 ID 대신 **Binding 참조**를 전달하고 서버가 Environment·Conversation 범위와 현재 권한을 검사한다. |
| V3-P03 | Proposed | Workspace는 Conversation 사이에서도 공유 가능하며, Binding은 Conversation 범위로 관리한다. 정확한 생성/공유/해제 규칙은 미결정. |
| V3-P04 | Proposed | Data는 Registry 및 Binding을 저장하지만 **Environment가 접근/실행 의미**를 정의한다. |
| V3-P05 | Proposed | public port, 불변 DTO, 명시적 오류/취소 계약, 외부 모듈의 내부 구현 import 금지 및 자동 의존성 검사를 채택한다. |
| V3-P06 | Proposed | 초기에는 한 프로세스 내 의존성 주입과 계약 테스트를 사용하며, 프로세스 수준 장애 격리는 필요시 확장한다. |
| V3-P07 | Proposed | Tool의 Workspace 요구 여부와 Environment 소속 여부를 **별개의 메타데이터**로 관리한다. |

## C. 명시적으로 미결정인 영역

- Data 물리 스키마, Run의 request/response/metadata 저장 범위와 영속성.
- 최신 Prompt Snapshot의 구체적 구조, 압축 시점, 예산, 원자적 갱신 규칙.
- Provider별 request/response 매핑, reasoning continuation 지원 범위, streaming.
- 복수 ToolCall 처리, 실행 순서/병렬성, 실패/취소/복구 세부 규칙.
- Environment 활성화 단위, Workspace 유형별 연결·권한·삭제 정책, Binding 수명/재사용 정책.
- **Inside** 명령 실행·Sandbox·승인·실행 프로세스 정책.
- **Memory Tool** 기능·자동화·권한·보존 정책.
- `update_plan`의 실질 데이터 구조·노출 방식·수명.
- v2 데이터/기능 이식의 범위와 절차.

각 항목의 우선순위와 완료 조건은 [미결정 항목](open-questions.md)에서 관리한다.

## D. v2에서 자동 이식하지 않는 구조

v3의 실행 기본 모델은 v2의 Task → ManagerTurn 계층을 승계하지 않는다. Worktree, Git 기반 작업 권한, Work Apply/Discard, 특수 HumanDirection을 초기 v3의 필수 구성으로 전제하지 않는다. 다만 v2의 개별 기능은 요구사항에 따라 독립적으로 재검토할 수 있다.

## E. 결정 변경 규칙

1. Confirmed 설계와 충돌하는 변경은 이 문서의 ID와 이유를 함께 개정한다.
2. Proposed 항목을 확정할 때는 **Confirmed**로 승격하고 구체적인 호환성 기준을 추가한다.
3. 코드 구현 결과가 설계를 소급해 확정하지 않는다. 설계와 구현 상태를 분리해 관리한다.
