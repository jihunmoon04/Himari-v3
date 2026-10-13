# 설계 결정 기록 (Decision Register)

기록 시점: **2026-10-13**. 이 문서는 지금까지의 대화에서 채택한 방향을 요약한다.

- **Confirmed**: 사용자가 명시적으로 결정하거나 동의한 아키텍처 원칙.
- **Proposed**: 해당 원칙을 구현하는 권고안. 명칭·데이터 모델까지 확정된 것은 아님.
- **Open**: 아직 선택하지 않았거나 구현 정책을 보류한 항목.

## A. 확정된 설계 원칙

| ID | 상태 | 결정 |
| --- | --- | --- |
| V3-001 | Confirmed | **Run = LLM 요청/응답 1회**. 여러 Run의 집합을 Task로 만드는 실행 기본 계층을 사용하지 않는다. |
| V3-002 | Confirmed | 별도 **Control** 모듈이 Run 반복을 제어하며, 모델의 final response 또는 question 이후 반복을 중단한다. question은 `ask_user` Tool 호출로 표현한다(V3-044). |
| V3-003 | Confirmed | Data의 **Conversation 원본 입력/출력은 exact**. Prompt용 가공 데이터와 분리한다. |
| V3-004 | Confirmed | **Prompt**는 Data의 기록을 이용하여 다음 LLM 입력을 구성한다. 최신 Prompt Snapshot은 **1개**만 보존하며 압축 문맥 + 최근 exact 항목 방식이다. |
| V3-005 | Confirmed | Tool 실패는 **실패 ToolResult를 모델에 전달(A)**한다. Runtime이 의미론적 자동 복구를 대신하지 않는다. |
| V3-006 | Confirmed | reasoning 관련 설정/이용 가능한 provider 결과는 Data에서 관리하고 Prompt가 다음 요청에 반영하는 방향이다. provider 비공개 내부 추론 텍스트의 저장을 전제하지 않는다. |
| V3-007 | Confirmed | Memory는 **Tool을 통한 Data 접근/구성** 방향으로 처리한다. 세부 정책은 나중에 정한다. |
| V3-008 | Confirmed | Conversation에 **여러 Environment**가 동시에 활성화될 수 있다. 환경별 Tool 노출은 `environment/tool` 네임스페이스 형태를 사용한다. |
| V3-009 | Confirmed | **Environment가 Workspace의 사용 방법을 규정**한다. Workspace는 환경에 종속된 수명을 가지지 않는다. |
| V3-010 | Confirmed | Workspace는 기본 작업 공간/사용자 로컬 컴퓨터 등 다양한 형태이며, 동일 Workspace를 서로 다른 Environment가 다르게 사용할 수 있다. |
| V3-011 | Confirmed | Tool은 **Workspace 지정 필수/불필요**로 나뉜다. `run_command`는 전자, `update_plan`은 후자다. 기본 Workspace를 암묵적으로 선택하는 유형은 별도 정책으로 고려한다. |
| V3-012 | Confirmed | **Workspace ID로 식별**하며 Workspace Registry와 Environment–Workspace **Binding을 구분**하는 설계 방향을 채택한다. |
| V3-013 | Confirmed | 모듈 내부 변경이 외부 계약을 지키는 한 다른 모듈의 변경을 요구하지 않도록 **계약 기반 모듈화**를 강제한다. |
| V3-014 | Confirmed | 먼저 구체화할 환경은 **Inside**. Inside의 실제 실행/승인 정책은 이 단계에서 결정하지 않는다. |
| V3-015 | Confirmed | **Prompt 순서는 Stable Prefix → Conversation(압축 이력 + 최근 exact) → Current State**로 정한다. Provider-specific 설정 및 Reasoning continuation은 메시지 꼬리의 임의 텍스트가 아니라 전용 API 형식이다. |
| V3-016 | Confirmed | 다음 Run의 **Trigger는 기존 Event 또는 Event 그룹의 참조**다. 발생 시점부터 Data에 보존되고 이후에 이력으로 이동/복사하지 않는다. |
| V3-017 | Confirmed | **ToolCall과 대응 ToolResult는 논리적으로 인접한 그룹**을 유지하고 call_id로 연결한다. Provider-native 재구성 규칙을 따른다. |
| V3-018 | Confirmed | **모든 Run의 원본 Provider request/response를 기본적으로 전부 보존**한다. 비밀 헤더, 미수신 응답, 스트리밍의 정확한 캡처 방식은 별도 정책. |
| V3-019 | Confirmed | **Execution Records / exact Conversation Events / Artifacts**를 서로 다른 논리 데이터 계약으로 구분한다. Prompt Snapshot은 이들과 분리된 최신 1개이다. |
| V3-020 | Confirmed | **Memory 검색 ToolResult는 Conversation에 내용 기록**하고, **Plan/Note Tool은 간결한 완료 Result를 Conversation에 남기고 상태를 별도 관리**할 수 있다. 모델 입력/Conversation 기록/UI 표시는 분리한다. |
| V3-021 | Confirmed | Workspace는 독립 자원이지만 **전용 소프트웨어 모듈은 만들지 않는다**. Registry/Binding은 Data가 보유하고 연결·실행 의미는 Environment가 규정한다. |
| V3-022 | Confirmed | **독립적인 복수 ToolCall은 병렬 실행을 허용**한다. 같은 명령 문자열인지 여부로 독립성을 판정하지 않고 실제 공유 자원/부작용 충돌을 기준으로 한다. |
| V3-023 | Confirmed | 모델이 가진 의미적 실행 정책과 **Raw Provider 기록을 Prompt 조립 정책과 분리**한다. Provider가 지원하는 reasoning/continuation은 Provider Adapter가 네이티브 배치 규칙을 따른다. |
| V3-024 | Confirmed | **동일 Conversation에서 Control 실행 흐름 및 Run은 직렬화**한다. 같은 실행 흐름의 독립적인 여러 ToolCall은 병렬 처리 가능하며, 다른 Conversation도 병렬 실행 가능하다. 정확한 lease/queue 구현은 미결정. |
| V3-025 | Confirmed | **ConversationEvent는 kind별 불변 canonical payload**를 사용한다. 구체 kind 목록과 call-group serialization은 미정. |
| V3-026 | Confirmed | Conversation에 기록하지 않은 ToolResult 등은 **Prompt가 원본 Execution 기록에서 선택·정합성을 검증**하고, **Provider가 native wire 구조로 직렬화**한다. 불완전한 호출 그룹을 날조하지 않는다. |
| V3-027 | Confirmed | **Plan/Note 원본은 Data의 conversation-scoped Artifact**에 보존한다. Outside의 기본 Workspace에 중복 원본을 두지 않으며 외부 노출/export는 별도 계약. |
| V3-028 | Confirmed | **활성 Workspace 및 Environment는 Artifact가 아니라 운영 상태**다. Workspace Registry/Environment Activation/Workspace Binding 및 설정은 Data에서 관리하고, 실시간 연결 가능 여부는 Environment에서 관측한다. Prompt의 Current State는 이를 합성한 읽기 투영이다. |
| V3-029 | Confirmed | 모든 모듈의 공개 계약은 **Contract ID/Version/Status, Owner, Provider, Consumer, Source of Truth, Contract Location**을 추적한다. 실제 Input/Output, Guarantees/Errors/Effects, Dependencies/Tests도 계약 의미에 맞게 기록한다. Owner의 공개 Port만 사용하고 현재 미구현 코드 위치는 planned로 구분한다. **통합 인벤토리는 [contracts.md](contracts.md)**에서 관리한다. |
| V3-030 | Confirmed | **HIMARI 제공 기본 Workspace를 v3 첫 파일 작업 기반으로 우선 구축**한다. 여기에서 파일 첨부, 대화 중 파일 생성·수정·제공 기능을 먼저 설계한다. v2 Phase 4A의 Task/Work 의존 구조를 필수 전제로 이식하지 않는다. |
| V3-031 | Superseded by V3-032~034 | **과거 결정:** 초기 기본 Workspace는 Approval·Worktree 복제 격리·OS Sandbox 없이 파일 Tool만 제공. **2026-10-13 개정:** Sandbox 및 run_command를 포함하며, Approval 미도입과 Worktree 미사용은 유지. |
| V3-032 | Confirmed | 초기 기본 Workspace에서 **`inside/run_command` 지원 및 OS Sandbox 적용**을 채택한다. 임의 명령을 Sandbox 없이 실행하는 것은 이 계약을 충족하지 않는다. Sandbox의 기술·기동 게이트·리소스 제한 세부는 미결정. |
| V3-033 | Confirmed | 기본 Workspace는 **Conversation마다 기본 작업 폴더**를 사용하되 **같은 사용자에게 허용된 다른 Conversation 폴더에도 접근**할 수 있게 설계한다. Conversation 폴더는 기본 cwd/정리 단위이고 보안 격리 단위가 아니다. 사용자별 root·세부 ACL·공유 영역 이름은 추후 결정. |
| V3-034 | Confirmed | 초기 기본 Workspace에서 별도의 **사용자 Approval·Worktree/Workspace 복제 격리를 요구하지 않는 결정은 유지**한다. 그러나 Sandbox를 통한 호스트/사용자 간 격리 및 파일 접근·실행 자원 검증은 유지한다. |
| V3-035 | Confirmed | 기본 Workspace의 **소유·공유 범위는 사용자/principal별**로 한다. 동일 사용자에 속한 각 Conversation은 별도 기본 cwd 폴더를 갖되 같은 사용자에게 허용된 다른 Conversation 폴더에 접근할 수 있다. 실제 principal 식별/인증/Root 매핑은 별도 계약. |
| V3-036 | Confirmed | **기본 Workspace Sandbox 안의 네트워크 접근은 기본 차단**한다. 향후 Web Search는 명령 Sandbox와 별도의 Tool/신뢰된 네트워크 경계로 구현할 수 있으며, 이를 이유로 run_command의 네트워크 권한을 자동 부여하지 않는다. |
| V3-037 | Confirmed | **초기 Sandbox는 네트워크 차단 전용 단일 프로파일**만 제공한다. `connected`/도메인 allowlist/프록시 등 조건부 허용 모드는 초기 범위에서 제외한다. 네트워크 namespace 격리를 검증하지 못하면 `run_command` 실행 거부. 호스트 서비스로 연결되는 Unix 소켓 등 우회 경로도 차단한다. 향후 네트워크 허용은 별도 정책 변경·검증을 요구한다. |
| V3-038 | Confirmed | **공개 모듈 계약을 생성·변경·제거할 때만 전체 pytest를 실행**한다. 공개 계약을 유지하는 내부 구현 변경·버그 수정·리팩터링에서는 변경된 **모듈별 pytest만 실행**한다(복수 모듈 변경이면 해당 모듈들만). 서명뿐 아니라 오류, 권한, 부작용, 호출 순서·취소 의미 등 계약 보장 변화도 공개 계약 변경이다. Sandbox 실제 호스트 격리 검증은 pytest 범위와 별도의 보안 게이트다. [testing-policy.md](testing-policy.md). |
| V3-039 | Confirmed | **Data 공개 Port를 Consumer별로 분리**한다: `ConversationLog`(Event·Run·ToolExecution·PromptSnapshot), `ArtifactStore`(Plan/Note), `WorkspaceState`(Registry·Activation·Binding·cwd 매핑·FileMetadata), `ControlState`(P2). 저장 백엔드·트랜잭션은 하나로 공유할 수 있으나 **계약 변경의 단위는 sub-port**다. 하나의 거대 Data Port를 모든 모듈이 의존하는 구조를 피한다. |
| V3-040 | Confirmed | **확장 지점 SPI를 내부 프로토콜보다 먼저, 가장 작게 확정**한다. 대상은 `ToolHandler`, `ProviderAdapter`, `EnvironmentPlugin` 세 가지다. 새 Tool·Provider·Environment 추가가 다른 모듈의 수정 없이 패키지 하나로 끝나는 것을 목표로 한다. |
| V3-041 | Confirmed | **Environment가 자신의 Tool을 직접 제공**한다(`EnvironmentPlugin.tools()`). Tool 모듈은 카탈로그 취합·인자 검증·디스패치·스케줄링·결과 커밋만 담당한다. `inside/run_command`, Inside 파일 Tool은 Inside Environment 패키지가 소유한다. |
| V3-042 | Confirmed | **Tool 구현은 실행 원장(ledger)을 알지 않는다.** `ToolHandler.run()`은 `ToolOutcome(model_result, artifact_mutations, …)`을 반환하고, Artifact 변경 + ToolExecution 종결 + Conversation Event의 원자 커밋은 Tool Dispatcher가 Data Port로 수행한다. Tool 구현은 `run_id`/`call_id`로 트랜잭션을 직접 다루지 않는다. |
| V3-043 | Confirmed | **Prompt 핵심은 I/O 없는 순수 함수** `compose(PromptInputs) -> ModelInput`이다. Data·Tool catalog·Environment 관측 수집은 Prompt 모듈 안의 얇은 `PromptContextLoader`가 담당한다. 압축·캐시 실험은 Fake 없이 입력 데이터만으로 테스트할 수 있어야 한다. |
| V3-044 | Confirmed | **question은 `ask_user` Control Tool로 표현**한다. 정규화 출력은 `FINAL \| TOOL_CALLS` 두 가지다. `ask_user`는 즉시 완료 Tool과 구분되는 **suspending Tool**이다. 호출 그룹을 커밋한 뒤 Control은 `WAITING_FOR_USER`로 멈추고 **Conversation lock을 해제**한다. 사용자 응답이 오면 **원래 `call_id`에 대응하는 ToolResult를 커밋한 뒤** 새 Run을 시작한다. 재시작 후에도 대기 중인 호출을 Data의 영속 기록만으로 식별할 수 있어야 하며, 새 Task 계층을 도입하지 않는다. 텍스트 휴리스틱으로 question을 추정하지 않는다. *(2026-10-13 독립 검수 B2 반영)* |
| V3-045 | Confirmed | **P0는 단일 프로세스·단일 사용자 규모로 단순화**하되, **크래시 후 실행 중복 방지는 lock이 아니라 영속 상태로 보장**한다. ① 같은 Conversation 직렬화(V3-024)는 프로세스 내 Conversation별 lock(예: `asyncio.Lock`)으로 실현한다. 이 lock은 **살아 있는 프로세스 안의 동시 실행만** 막으며, 크래시 후 중복 송신·중복 부작용은 막지 못한다. ② **Provider 송신 직전(`DISPATCH_CLAIMED`)과 부작용 있는 Tool 실행 직전(`EXECUTION_CLAIMED`)의 최소 영속 표시는 P0에 둔다.** ③ 표시 후 결과 기록 전에 크래시하면 재시작 시 `OUTCOME_UNKNOWN`(Run) / `UNKNOWN`(Tool)으로 처리하고 **자동 재실행하지 않는다**. ④ 분산 lease, `control_generation` fencing, 다중 worker 경합, UNKNOWN 정밀 reconcile 절차, 복잡한 CAS는 P2로 연기한다. Run별 원본 요청·응답 보존(V3-018)은 유지한다. *(2026-10-13 독립 검수 B1 반영)* |
| V3-046 | Confirmed | **계약은 코드로 선언**한다. 각 Owner 모듈의 `api.py`(`typing.Protocol` + frozen dataclass)가 생기면 그것이 계약의 원본이고, 문서는 설계 근거·결정 이력을 담는다. 모듈 간 import 방향은 import-linter 등 정적 검사로 강제한다. 문서 간 이름 불일치는 코드 선언 시점에 해소한다. |
| V3-047 | Confirmed | **UNKNOWN 효과는 모델에게 알리고 모델이 확인한다(Q-37).** 실행 기록(`EXECUTION_CLAIMED`)을 남긴 뒤 효과가 불확실해지면(실행 중 예외, 결과 기록 전 크래시) Runtime은 재실행하지 않고, 그 호출에 **"결과 불명, 재시도 전 상태를 확인할 것"이라는 결과를 붙여** 대화를 이어간다. 모델이 다른 Tool로 상태를 확인하고, 다시 실행할지는 모델이 새 호출로 결정한다. 상태는 **UNKNOWN 그대로** 유지해 성공·실패로 꾸미지 않는다. 다른 Tool로 확인할 수 없는 효과를 가진 Tool은 정의에서 `unknown_outcome = REQUIRE_USER`를 지정하고, 이때만 사용자 해소 전까지 대화를 멈춘다(P0에는 해당 Tool 없음). 사용자 선언 절차는 두지 않는다. *(2026-10-13 사용자 승인: "모델 자동 확인 + Tool별 require_user 예외 + 상태 UNKNOWN 유지")* |
| V3-048 | Confirmed | **Provider 원본 요청은 wire-exact로 보존한다(V3-P14 결정).** Adapter가 `prepare_native_request`에서 요청 본문 bytes를 직접 만들고, Data가 송신 전에 그 bytes를 저장하며, `invoke_prepared`는 **저장된 bytes를 그대로** 전송한다. 공식 SDK의 재직렬화·자동 재시도에 기대지 않는다(HTTP 클라이언트 직접 사용). 인증 키·헤더는 저장하지 않는다. RunRecord의 `raw_representation`은 `WIRE_EXACT`. *(2026-10-13 사용자 승인)* |
| V3-049 | Confirmed | **reasoning의 책임 분담.** ① 추론 강도 설정: 중립 `RequestOptions.reasoning_effort`(Data 설정 → Prompt), Provider가 네이티브 필드로 변환하고 미지원이면 송신 전 거부. ② 추론 토큰 사용량: Provider가 응답에서 정규화하고 Data가 기록하며, 프롬프트에 넣지 않는다. ③ continuation(불투명한 추론 블록·서명): Data가 그것을 낳은 assistant 턴에 붙여 저장하고, Prompt는 이력의 해당 턴에 불투명한 값 그대로 싣는다. **실제 요청에 넣는 것은 Provider가 `prepare_native_request` 단계에서** 한다(wire-exact 유지). **Provider는 Data를 직접 조회하지 않고** `ModelInput`으로 받은 것만 쓰며, provider·모델 호환성과 네이티브 배치는 Provider가 판단한다. **Prompt는 모델을 알지 않으며** 어느 턴을 포함할지(압축 시 해당 턴의 continuation도 함께 제외)만 정한다. *(2026-10-13 사용자 승인)* |


**V3-039~046 승인 근거 (2026-10-13):** 독립 검수 C3("승인 근거가 대화만으로 확인되지 않음")에 따라 사용자에게 항목별 상태를 다시 물었고, 사용자가 **V3-039~046을 Confirmed로 명시 승인**했다(Claude Code 세션, PR #1 병합 직전). 그 이전 근거: 모듈 설계 검토 직후 "이 방향으로 문서 수정해서 push" 요청, 독립 검수 후 "V3-039~046의 단순화 방향은 유지" 지시. **Confirmed 범위는 각 항목의 방향과 원칙**이다. 함수 시그니처·DTO 필드·구체 메커니즘(ask_user 대기 판정, 답변 재생 규칙, Environment 등록 방식, raw 보존 기준 등)은 V3-P14~P19로 따로 관리하며 Confirmed가 아니다. V3-044·045 본문은 검수 B1·B2에서 사용자가 지시한 요구사항으로 개정했다.

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
| V3-P08 | Refined by V3-041/042 | Tool Dispatcher는 **Internal (Data 공개 API) / Environment (Workspace 실행 Port)**를 분리한다. → Environment Tool은 해당 Environment 패키지가 `ToolHandler`로 제공하고, Internal Tool도 같은 `ToolHandler` SPI를 구현한다. 원자 커밋은 Dispatcher가 수행한다. 필요시 외부 서비스 Connector 유형을 추가한다. |
| V3-P09 | Superseded by V3-027 | Plan/Note 원본을 Data에 저장하는 결정으로 승격. Revision/삭제·Outside projection 구현은 여전히 미정. |
| V3-P10 | Proposed | 같은 Workspace의 범위 불명 `run_command`는 기본 직렬, 서로 다른 검증된 파일 범위 수정은 조건부 병렬. 다른 Conversation의 같은 자원 충돌도 검증한다. |
| V3-P11 | Proposed | Artifact Revision(CAS) 및 Data 트랜잭션으로 상태 변경 + Tool 완료 기록 + 필요한 Conversation Event를 원자적으로 커밋한다. |
| V3-P12 | Proposed | 같은 Run의 여러 ToolCall이 모두 terminal 상태에 도달한 뒤 다음 Run을 시작하며, 입력의 원래 Call 순서와 완료 순서를 별도 보존한다. |
| V3-P13 | Proposed | `workspace_requirement`는 `None / Required / Default`로 구분한다. `Default`는 기본 Workspace 사용으로 명시적 권한 검증이 필요하다. |
| V3-P14 | Superseded by V3-048 | P0 Provider raw 요청 보존 기준: wire-exact(A)와 adapter-serialized(B) 중 선택 → **V3-048로 wire-exact 확정**. |
| V3-P15 | Proposed | `ask_user`는 P0에서 **ToolCall 그룹의 유일한 호출**일 때만 허용하고, 다른 호출과 섞이면 `INVALID_MODEL_OUTPUT`으로 거부(raw 보존, 효과 없음)한다. 혼합 허용은 후속 검토. |
| V3-P16 | Proposed | Tool 노출 텍스트(commentary)와 ToolCall이 함께 오면 `TOOL_CALLS`로 정규화하고 텍스트는 같은 assistant Event의 블록으로 보존한다. final 신호로 해석하지 않는다. |
| V3-P17 | Proposed | **ask_user 대기의 영속 근거:** 최신 `PROJECTED` Run의 ToolCall 그룹이 단일 `ask_user` 호출이고 그 ToolExecution이 `PENDING`이면 Conversation은 `WAITING_FOR_USER`다. 상태 값·테이블을 추가하지 않는 **파생 상태**다. 답변은 `user_message` Event(`answers_call` 포함) + ToolExecution `PENDING → SUCCEEDED` + `tool_result` Event를 한 트랜잭션(T7a)으로 커밋한다. |
| V3-P18 | Proposed | **답변 재생 규칙:** `answers_call`이 있는 `user_message`는 native 이력에서 **해당 call의 ToolResult로 한 번만** 렌더링하고 독립 user 턴으로 다시 보내지 않는다. 원본 Event 두 개는 모두 보존한다. ToolCall 그룹은 원래 ordinal 순서로 assistant 호출 바로 뒤에 결과를 붙인다. |
| V3-P19 | Proposed | **Environment 등록:** `EnvironmentPlugin`은 kind당 하나를 bootstrap에서 등록하고, `tools()`는 kind 수준의 고정 ToolHandler 목록을 반환한다. Conversation의 Tool 카탈로그는 Internal Tool + `ask_user` + **그 Conversation에 활성 Instance가 있는 kind의 Tool**이다. 대상 Instance·Workspace는 호출 인자의 `workspace_binding_id`(또는 `Default` 규칙)로 Dispatcher가 해석해 `ToolContext`에 넣고, Environment는 실행 시 다시 검증한다. P0는 Conversation당 kind별 활성 Instance 최대 1개. |
| V3-P20 | Proposed | **모듈 layer 순서:** `common < data < tool < environment < prompt < provider < control < bootstrap`. 하위 layer만 import할 수 있고, 다른 모듈은 `himari.<module>.api`와 `himari.common`만 import한다(`scripts/check_imports.py`). Provider는 Prompt의 `ModelInput`을, Environment는 Tool의 `ToolHandler`를 소비하므로 이 순서가 된다. bootstrap(Composition Root)만 예외다. |
| V3-P21 | Proposed | **`himari.common`:** 여러 계약이 공유하는 식별자(NewType)·값 타입(`TrustedScope`, `CallRef`, `ToolCall`, `ContentBlock`, `ModelToolResult`, `TokenUsage`, `RawRepresentation`, `ASK_USER_TOOL_ID`)만 둔다. 행위·모듈 고유 DTO는 두지 않는다(거대 공용 계약 패키지 금지 원칙 유지). |
| V3-P22 | Proposed | **코드 선언 시 이름 정리:** Tool 완료는 DATA-08 `commit_tool_outcome` 하나로 통합(구 `complete_tool_execution`). 정규화 공통 규칙 `validate_tool_calls`(빈 그룹·중복 call_id/ordinal·`ask_user` 혼합 거부)는 `provider/api.py`에 두어 모든 Adapter가 공유한다. |
| V3-P23 | Proposed | **drive당 Run 예산:** `max_runs_per_drive`(기본 16)에 도달하면 `RUN_LIMIT_REACHED`로 멈춘다. 의미론적 복구가 아니라 무한 루프 방지 예산이다. 다음 사용자 입력에서 이어간다. |
| V3-P24 | Proposed | **동시 drive:** 같은 Conversation을 이미 drive 중이면 대기열에 넣지 않고 `ControlError(BUSY)`로 즉시 거부한다(CP-05의 P0 답). |
| V3-P25 | Proposed | **P0 Tool 실행 순서:** Dispatcher는 그룹을 원래 ordinal 순서로 **순차 실행**한다. 병렬 스케줄링(TOOL-03)은 포트 변경 없이 나중에 추가한다. |
| V3-P26 | Proposed | **`ToolCompletion.record_event`:** Data가 Tool의 `conversation_record` 정책을 알 수 있도록 Dispatcher가 커밋 시 전달한다(FULL → True). |
| V3-P27 | Superseded by V3-047 | **불확실한 효과의 판정:** claim된 Environment Tool의 handler 예외는 FAILED로 만들지 않는다. → V3-047에 따라 `REPORT_TO_MODEL`이면 UNKNOWN + "결과 불명" 결과로 종결해 모델에 전달하고, `REQUIRE_USER`면 `EXECUTION_CLAIMED`로 남겨 그룹을 BLOCKED로 둔다. claim이 없는 Tool의 예외는 아무 효과도 없었으므로 FAILED 결과로 모델에 전달한다. |
| V3-P28 | Proposed | **미완결 그룹 재개 시점:** `drive`와 `resume` 시작 시(재시작 정리와 저장 응답 투영 직후, 사용자 입력 기록 전) 최신 그룹의 claim 전 `PENDING` 호출을 먼저 실행한다(D20). |
| V3-P29 | Proposed | **공개 DTO의 깊은 불변성:** 모든 공개 DTO는 `frozen=True` + `DeepFrozen`을 상속한다. 생성 시 모든 필드를 깊게 복사·불변화한다(Mapping → `FrozenMap`, list/tuple → tuple, set → frozenset). `FrozenMap`은 **dict를 상속하지 않는** 불변 Mapping이라 `dict.__setitem__` 같은 dict API로도 바꿀 수 없다. JSON 직렬화는 독립 사본을 만드는 `thaw()`로 한다. 그래서 Data는 저장 객체를 그대로 공유·반환해도 된다. `object.__setattr__` 같은 의도적 저수준 조작은 Python에서 막을 수 없으므로 범위 밖이다(영속 DB 백엔드는 직렬화로 자연히 격리됨). *(독립 검수 B2-1, 재검수 B1)* |
| V3-P30 | Proposed | **ToolCall ordinal 규칙:** 한 그룹의 ordinal은 나열 순서대로 정확히 `0..n-1`이다. 음수·빈 번호·순서 불일치는 `INVALID_MODEL_OUTPUT`으로 거부한다(`validate_tool_calls`). |
| V3-P31 | Proposed | **저장된 응답의 재시작 투영:** `recover_interrupted`는 `RECEIVED`(투영 전) Run을 상태 변경 없이 보고하고, Control은 새 Run 전에 `read_raw_response`로 저장 응답을 다시 정규화·투영한다(Provider 재호출 없음). 정규화 실패면 `PROJECTION_FAILED`, 저장 당시와 다른 Provider면 `RECOVERY_REQUIRED`(입력은 기록). *(독립 검수 B3-1)* |
| V3-P32 | Proposed | **Environment 활성화 실행 게이트:** Dispatcher는 모든 Environment Tool 호출에 대해 현재 Conversation에서 그 kind가 활성인지(`read_activation`) 확인하고, Binding이 활성 Instance 소속인지 검사한다. Workspace가 필요 없는 Tool도 예외가 아니다. 카탈로그 노출은 권한이 아니다. *(독립 검수 B3-2)* |
| V3-P33 | Proposed | **Tool 인자 검증:** `input_schema`는 지원 부분집합(`type/properties/required/additionalProperties/items/enum` + `description/title/default`)만 쓸 수 있고, 등록 시 **모든 키워드의 값 형식까지 검사해 fail-closed로 거부**한다(예: `required`는 properties에 선언된 이름의 중복 없는 문자열 배열, object 전용 키워드는 `type: object`에서만, `default`는 자기 스키마를 만족). Tool 입력 스키마의 최상위는 `type: object`여야 한다. 실행 전 모델 인자를 검증해 실패하면 handler 없이 DENIED. `enum`은 JSON 의미의 동등성으로 비교한다(불리언 ≠ 숫자, `1 == 1.0`, 객체·배열은 구조 비교). P0에서 suspending Tool은 `ask_user`만 등록 가능하다. *(재검수 B3)* |
| V3-P34 | Proposed | **`resume(scope)`:** 사용자 메시지 없이 이어 가는 Control 진입점이다. 정리·투영·미실행 호출 처리 후, 영속된 마지막 Event로 다음 trigger를 정한다(FINAL이면 `IDLE`, 대기 중이면 `WAITING_FOR_USER`). 합성 사용자 메시지를 만들지 않는다. |
| V3-P35 | Proposed | **입력 멱등성:** 같은 `idempotency_key`에 같은 메시지면 기존 Event를 반환하고, 다른 메시지면 `DUPLICATE_IDENTITY`로 거부한다. |
| V3-P36 | Proposed | **Tool 원본 결과 보존:** `ToolExecutionRecord.raw_result`에 Tool이 반환한 원본 실행 결과를 `model_result`와 따로 저장한다(Data v0.2 §6의 `raw_execution_result`). 모델에는 보내지 않는다. *(재검수 B2)* |
| V3-P37 | Proposed | **V3-047 구현 방식:** `ToolDefinition.unknown_outcome`(기본 `REPORT_TO_MODEL`)은 **claim 시점에 `ToolExecutionRecord.unknown_outcome`으로 영속**하고, 결과를 Conversation Event로 남길지(`conversation_record`)도 `record_event`로 함께 영속한다. 복구·보고는 현재 Registry가 아니라 이 값들로 판단한다. 값이 없는 불확실 기록은 `REQUIRE_USER`로 취급한다(자동 완화 금지). Data `commit_unknown_outcome`도 영속 정책이 `REPORT_TO_MODEL`이 아니면 거부한다(이중 방어). `REQUIRE_USER` 불확실성이 생기면 **같은 그룹의 남은 호출을 실행하지 않고** 멈춘다(실행 중·재시작 모두). 실행 중 예외는 같은 `drive`에서, 재시작 정리로 생긴 UNKNOWN은 다음 `drive`/`resume`의 그룹 마무리에서 처리한다. 안내 문구는 예외 메시지를 넣지 않은 고정 문구(`UNKNOWN_OUTCOME_RESULT`)다. 모델이 재시도 전에 실제로 상태를 확인하는지는 **기계적으로 보장하지 않는 모델 재량**이다(V3-005). **P0에는 `REQUIRE_USER` 해소 API가 없으므로 `ToolRegistry`가 `REQUIRE_USER` Tool 등록을 기본 거부한다**(`allow_require_user`, bootstrap 기본값 꺼짐). 해소 API를 구현할 때 함께 허용한다. *(PR #4 검수 B1·B2, 후속 C1·C2)* |
| V3-P38 | Proposed | **OpenRouter Adapter(첫 실제 Provider):** OpenAI 호환 Chat Completions(`/api/v1/chat/completions`)를 httpx로 직접 호출한다. Tool id의 `/`는 `__`로 가역 인코딩한다(`__`를 포함하거나 64자를 넘는 id는 송신 전 거부). 재시작 후 저장 응답 재정규화에 Adapter 상태가 필요 없게 하기 위해서다. Current State는 마지막 Tool 그룹 뒤의 user-role `<runtime_state>` 메시지로 넣고, 비어 있으면 생략한다. 첨부는 아직 미지원(송신 전 거부). HTTP 비-200·200 본문의 `error`는 확정 실패(`NO_RESPONSE`), 연결 실패는 미송신 확정, 연결 이후 전송 오류(타임아웃 등)는 `UNCERTAIN`. 자동 재시도 없음. 라이브 검증 모델: `openai/gpt-5-nano`, `anthropic/claude-haiku-5.5`. |

## C. 명시적으로 미결정인 영역

- Data 물리 스키마, Run 및 Conversation Event 식별자·cursor·raw 직렬화/스트리밍 보존 방식. **Run별 request/response 전체 보존 범위의 방향 자체는 확정**.
- 최신 Prompt Snapshot의 구체적 구조, 압축 시점, 예산, 원자적 갱신 규칙.
- Provider별 request/response 매핑, reasoning continuation 지원 범위, streaming.
- **병렬 실행 가능 원칙 자체는 확정**. Scheduler의 실제 충돌 판별 알고리즘, 한 Run의 종결 배리어·취소/복구·중복 실행 정책은 미결정.
- Environment 활성화 단위, Workspace 유형별 연결·권한·삭제 정책, Binding 수명/재사용 정책.
- **기본 Workspace에서 `run_command` + OS Sandbox를 함께 도입**한다. 초기 Approval과 Worktree 복제 격리는 미도입이며, Sandbox 백엔드/필수 권한/실행 제한과 장기 ManagedProcess는 별도 설계.
- **사용자별 기본 Workspace Root + Conversation별 기본 cwd + 같은 사용자 내 교차 접근**은 확정. 실제 principal 인증/ACL·폴더 수명·공유 기록 정책은 미결정.
- **Sandbox 네트워크 기본 차단**은 확정. v2는 네트워크 격리를 제공하지 않았으므로 별도 Sandbox 검증이 필요하다. 향후 별도 Web Search Tool은 가능하지만 현재 구현 결정은 아니다.
- **Memory Tool** 기능·자동화·권한·보존 정책.
- Plan/Note **원본 저장 위치는 확정**. Artifact의 schema/Revision/삭제·수명/Outside export, `update_plan` 세부 스키마는 미결정.
- **활성 Workspace/Environment 저장 영역과 Artifact의 구분은 확정**. 실제 현재 상태 조회 DTO, 연결상태 갱신 주기, 바인딩 선택 및 기본 Workspace 정책은 미결정.
- 기본 Workspace 파일 자체의 저장 형태, 파일 첨부/생성 metadata, 버전·다운로드·삭제·안전한 경로 API·업로드 크기 제한(현행 초안: [default-workspace-files-v0.2.md](default-workspace-files-v0.2.md)).
- Sandbox는 v2 Phase 3.5의 Bubblewrap/Landlock·기동 인증·워크스페이스 루트 보호·프로세스 제한 설계를 **재사용 대상으로 검토**한다. v3의 Work 부재/사용자별 Root·Conversation cwd·네트워크 차단에 맞춘 수정은 [Sandbox Execution v0.1 초안](sandbox-execution-v0.1.md)에 남긴다.
- **Sandbox Environment 구현 세부 계약은 [Sandbox Implementation v0.2 검토 초안](sandbox-implementation-v0.2.md)**에 정리한다. V3-032~037 정책 외의 DTO, Admission, Activation, 실행 한도와 Linux 검증 방식은 아직 확정 전이다.
- Pause/Resume를 위한 ConversationControlState·안전 경계·미완결 ToolCall 복구는 [control-pause-v0.1.md](control-pause-v0.1.md) 검토 초안이며 **현재 확정 정책이 아니다**.
- v2 데이터/기능 이식의 범위와 절차.
- **SPI 세부 시그니처(V3-040)**: `ToolHandler`/`ToolContext`/`ToolOutcome`, `ProviderAdapter`, `EnvironmentPlugin`의 필드·오류 분류는 첫 코드 Skeleton에서 `api.py`로 확정한다.
- **P0에 남는 내구성 장치(V3-045)**: 송신 직전 `DISPATCH_CLAIMED`, 부작용 Tool 실행 직전 `EXECUTION_CLAIMED`, 재시작 시 `OUTCOME_UNKNOWN`/`UNKNOWN` 처리, 자동 재실행 금지. UNKNOWN Tool은 V3-047에 따라 모델에게 "결과 불명"으로 알린다(`REQUIRE_USER` Tool만 대화를 멈춤).
- **P2로 연기된 내구성 장치(V3-045)**: 분산 lease, `control_generation` fencing, 다중 worker claim 경합, UNKNOWN 정밀 reconcile 절차. 연기일 뿐 폐기가 아니며 도입 시 공개 계약 변경으로 취급한다.

각 항목의 우선순위와 완료 조건은 [미결정 항목](open-questions.md)에서 관리한다. 공개 계약의 Owner·Consumer·데이터 출처 지도는 [contracts.md](contracts.md)에서 관리한다. 상세 흐름은 [Prompt Composition](prompt-composition.md), [Data and Tools](data-and-tools.md), [Data Contract v0.2](data-contract-v0.2.md) 참조.

## D. v2에서 자동 이식하지 않는 구조

v3의 실행 기본 모델은 v2의 Task → ManagerTurn 계층을 승계하지 않는다. Worktree, Git 기반 작업 권한, Work Apply/Discard, 특수 HumanDirection을 초기 v3의 필수 구성으로 전제하지 않는다. 다만 v2의 개별 기능은 요구사항에 따라 독립적으로 재검토할 수 있다.

## E. 결정 변경 규칙

1. Confirmed 설계와 충돌하는 변경은 이 문서의 ID와 이유를 함께 개정한다.
2. Proposed 항목을 확정할 때는 **Confirmed**로 승격하고 구체적인 호환성 기준을 추가한다.
3. 코드 구현 결과가 설계를 소급해 확정하지 않는다. 설계와 구현 상태를 분리해 관리한다.
