# HIMARI v3 아키텍처

> 상태: **설계 기준선**, 2026-10-13. 이 문서의 “확정”은 설계 방향에 대한 합의를 뜻하며 구현 완료를 뜻하지 않는다. 세부 스키마·함수 서명·명칭은 별도 확정 전까지 예시이다.

## 1. 전체 실행 구조 — 확정

```text
User input
    │
    ▼
Control (application / orchestration)
    │  입력 및 진행 상황을 Data에 기록
    ├──▶ Prompt ── Data의 원본·최신 문맥·설정 조회
    │       │
    │       ▼
    │   Provider ── LLM 호출 정확히 1회 = Run
    │       │
    │       ▼
    │    Data ── 해당 입출력 및 실행 관련 기록
    │
    └── 응답에 따라 분기
         ├── final response ── 반복 종료
         ├── question       ── 반복 중단, 사용자 입력 대기
         └── tool call      ── Tool 경로로 실행
                                  │
                                  ▼
                               ToolResult
                                  │
                                  └── Data 기록 → 다음 Run
```

- **Run은 하나의 LLM 호출**이다. 여러 호출의 집합을 Task로 정의하거나 Runtime이 Task 생명주기를 소유하지 않는다.
- **Control**은 Run을 이어서 실행할지 결정하는 제어 모듈이다. Prompt, Provider, Tool, Data의 내부 구현은 소유하지 않는다.
- **final response** 또는 **question**이면 Run 반복을 멈춘다. Question 이후 재개 방식 및 혼합 출력 우선순위는 미결정이다.
- Tool 실패는 실패 `ToolResult`로 Data에 남아 다음 모델 입력에 반영된다. 기본 원칙은 **모델에 결과 전달(A)**이다. 모델의 판단을 대신하는 Runtime의 의미론적 자동 재시도/복구를 도입하지 않는다.
- 호출 취소, 네트워크 단절, 미완료 tool call 재개, 한 Run의 다중 tool call 처리 등 제어의 예외 경로는 추후 계약화한다.

## 2. Data와 Conversation — 확정

Data는 다음과 같이 서로 다른 성격의 정보를 관리한다.

| 구분 | 원칙 |
| --- | --- |
| Conversation | 사용자 및 에이전트의 입력·출력을 **exact하게 보존**하는 원본 기록 |
| Run / Execution Records | **모든 Run의 원본 Provider 요청·응답 전체**를 기본 보존. Tool 실행 원본 기록은 별도로 추적. 직렬화/보안/스트리밍 세부는 미결정 |
| Conversation Tool Event | Memory 검색 결과 등은 exact ToolResult로, Plan/Note는 간결한 완료 Result로 기록 가능. Tool 실행 원본과 UI 표시는 별도 |
| 최신 Prompt Snapshot | 압축 문맥과 이후의 exact 입력 데이터를 다음 호출에 사용하기 위한 **최신 상태 1개** |
| Artifacts | Plan/Note처럼 별도로 조회·수정하는 지속 데이터. **원본은 Data의 conversation-scoped Artifact로 확정**. Schema/Revision/삭제 세부는 미정 |
| Workspace Registry | 독립적인 Workspace의 식별과 연결에 필요한 메타데이터 |
| Environment/Binding | Environment 활성화 및 Workspace 접근 관계의 메타데이터 |
| Model/Reasoning 설정 | 모델·추론 강도 등 다음 요청 구성에 필요한 설정 및 provider가 제공한 사용량/continuation 자료 |

- **원본 Conversation ≠ 가공된 Prompt.** Prompt가 압축/필터링한 데이터는 Conversation 원본을 덮어쓰지 않는다.
- 최신 Prompt Snapshot은 누적 버전을 보관하는 로그가 아닌 **대체 가능한 단일 최신 상태**이다. 압축 요약과 최근 exact 항목을 포함하는 설계 방향으로 정했다.
- **원본 Provider 요청·응답은 Run별로 모두 보존**한다. Snapshot은 최신 1개만 유지하더라도 Run별 원본 기록은 별도 데이터이며 이전 호출의 감사·재현을 지원한다. 실제 스트림/비밀 데이터 보존 방식은 추후 결정한다.
- Conversation Events, Execution Records, Artifacts는 논리적으로 별도 계약이다. Data의 DB 엔진 또는 물리 테이블을 반드시 분리할 필요는 없다. [Data/Tool 설계](data-and-tools.md) 참조.
- 다른 모듈은 DB 테이블을 직접 읽거나 수정하지 않고 **Data의 공개 조회/명령 계약**으로 접근한다.

## 3. Prompt — 캐시 우선 구성 (순서 확정)

```text
Stable Prefix             (고정 지시문 / 안정적인 Tool 정의)
    ↓
Conversation             (압축된 과거 + 순서 유지한 exact Event / native Tool 그룹)
    ↓
Current State             (활성 Environment/Workspace Binding, Plan/Note 등)
    ↓
Provider-specific config  (model/reasoning/continuation; 별도 API 필드)
```

- **Stable Prefix → Conversation → Current State**를 선택했다. 현재 상태를 마지막에 두어 과거 프롬프트 접두부를 가능한 한 유지한다.
- **Trigger는 원본 Event/이벤트 그룹에 대한 Run의 참조**이다. User input·ToolResult는 발생할 때부터 Data에 기록되며, 다음 Run에서 '이력으로 이동'시키거나 복제하지 않는다.
- ToolCall과 대응 ToolResult는 하나의 논리적 그룹으로 인접하게 유지한다. 병렬 호출도 `call_id`와 원래 호출 순서로 구성하며, Provider native tool protocol을 위반하지 않는다.
- **Reasoning token usage**는 실행 메타데이터다. 모델 설정 및 provider가 실제 반환한 opaque continuation과 분리한다. continuation은 임의의 프롬프트 끝 텍스트가 아니라 Provider Adapter가 네이티브 방식으로 다룬다.
- 압축·컨텍스트 예산은 Prompt 내부 책임이다. Snapshot은 최신 1개를 대체하며 Conversation 원본과 Run raw 기록은 수정하지 않는다.
- **CurrentState는 저장된 Activation/Binding/Settings, 현재 Plan/Note Artifact, 필요한 Environment 실시간 관측을 합성한 읽기 투영**이다. 자체적인 권한의 원본/Artifact가 아니다. DTO/일관성·기본 Binding 등 구체 계약은 [CurrentState Contract v0.1 — 검토 초안](current-state-contract-v0.1.md)에 정리했다.
- Provider별 변환과 cache-control, 시스템/사용자 역할 부여, native tool/result 결합, continuation validation은 Provider Adapter 책임이다. **캐시 hit는 기대 효과이지 보장값이 아니다.**

세부 규칙은 [Prompt Composition Contract](prompt-composition.md)에 기록한다.

## 4. Module 개요 — 확정된 책임 분리

| 모듈 | 소유한 책임 | 소유하지 않는 책임 |
| --- | --- | --- |
| Control | Run 반복·종료 분기·Tool 호출 오케스트레이션 | Provider 세부 API, DB 스키마, 환경별 실행 메커니즘 |
| Data | 영속 상태·원본 기록·조회/변경 계약 | 프롬프트 정책, 실제 명령 실행 |
| Prompt | 모델 요청 입력 조립·컨텍스트 압축 | Conversation 원본 변경, Provider 전송 |
| Provider | 모델 API 통신·응답 정규화 | Tool의 환경별 실행, Run 반복 정책 |
| Tool | 노출된 도구의 등록/호출 계약·결과 형태 | 환경별 Workspace 수명·정책 |
| Environment | Workspace 접근 방식·실행 정책·환경 전용 Tool 처리 | Workspace 자체의 수명 소유 |
| Workspace (독립 자원, 별도 코드 모듈 아님) | Data의 Registry로 식별, Environment가 접근 | Environment에 수명을 종속시키지 않음 |

모듈의 실제 디렉터리명, 공유 타입의 구체화 및 객체 수명은 [모듈 경계](module-boundaries.md) 문서에서 제안 수준으로 다룬다.

## 5. Workspace와 Environment — 확정

### 5.1 Workspace는 독립 자원

- Workspace는 반드시 폴더가 아니다. 기본 제공 공간, 사용자 로컬 컴퓨터, 원격 컴퓨터/컨테이너 등 이종 형태를 지원한다.
- Workspace는 Environment보다 길거나 짧게 존재할 수 있으며 **Environment의 비활성화가 Workspace의 삭제를 의미하지 않는다.**
- 동일 Workspace를 둘 이상의 Conversation/Environment가 참조하는 것을 허용하는 방향이다.
- Workspace의 구체적인 생성·삭제, 자원 소유자, 연결 방법은 별도 정책으로 결정한다.

### 5.2 Environment가 Workspace를 사용하는 방식

- Environment는 Workspace **연결·접근·실행 방식**을 정의하는 모듈이다.
- 하나의 Conversation에서 여러 Environment를 활성화할 수 있다.
- 하나의 Environment는 여러 Workspace를 사용할 수 있다.
- 하나의 실제 Workspace를 **서로 다른 Environment가 다른 권한/방법으로 다룰 수 있다.**
- **Workspace 전용 소프트웨어 모듈을 두지 않는다.** Registry/Binding은 Data 계약, 연결/정책/실행은 Environment 계약으로 처리한다.
- 최초 구체화 대상은 **Inside Environment**이고, Inside의 실행·승인·Sandbox 세부 계약은 아직 확정하지 않았다.

### 5.3 Registry와 Binding — 채택한 설계 방향

```text
Workspace Registry
    └── ws_A  (독립적 자원: 기본 작업 공간, 로컬 PC 등)
          ▲                          ▲
          │                          │
Binding A (Inside, 접근 방식 A)  Binding B (Analysis, 접근 방식 B)
          ▲                          ▲
    Environment Inside          Environment Analysis
          └─────── Conversation ─────┘
```

- **활성 Workspace/Environment는 Artifact가 아닌 운영 상태다.** Data에 Workspace Registry, Environment Activation/Instance, Workspace Binding, 기본 Workspace 선택 설정을 보관하고, Environment는 실제 연결 가능 여부를 별도로 판단한다.
- `workspace_id`: Workspace 자체를 식별하는 불투명한 ID.
- `environment_instance_id`: 활성화된 Environment 인스턴스를 식별하는 ID.
- `binding_id`: 특정 Environment가 특정 Workspace에 접근하는 관계를 식별하는 ID.
- **Binding은 Workspace 소유권이 아니다.** Binding 제거가 자원 삭제를 일으키지 않는다.
- Binding은 연결 대상과 접근 설정의 기록이다. **실제 권한 판정은 실행 시점에 다시 수행**하며, Binding ID만으로 권한이 성립하지 않는다.
- Binding을 Conversation 범위로 격리하는 것이 초기 권고안이지만 DB 컬럼, 공유/재사용 규칙, 비활성화 정책은 아직 확정 전이다.
- Binding이 활성 상태더라도 로컬 PC가 오프라인일 수 있다. 저장된 관계와 현재 연결 가능 상태를 혼동하지 않는다.

## 6. Tool 분류와 호출 — 확정

**Tool의 Environment 소속 여부**와 **Workspace 요구 여부**는 서로 독립적이다.

| 예시 | Environment 소속 | Workspace 필요 |
| --- | --- | --- |
| `inside/run_command` | Inside | **필수** |
| `inside/read_file` | Inside | **필수** |
| `inside/list_workspaces` | Inside | 불필요 |
| `update_plan` | 비환경 Tool | 불필요 |
| `memory_search` (가칭) | Memory Tool 계열 | 불필요 |

Workspace를 요구하는 호출은 대상 Workspace를 지정해야 한다. **Tool 호출에서 실제로는 `binding_id`를 참조**하게 하는 방식을 권고한다. 그래야 Environment/Conversation 범위와 실행 정책을 중앙에서 검증할 수 있다.

아래는 **개념 예시이며 확정 스키마가 아니다**.

```json
{
  "tool": "inside/run_command",
  "workspace_binding_id": "bind_A",
  "arguments": {"command": "python main.py"}
}
```

```json
{
  "tool": "update_plan",
  "arguments": {"step": "Implement API", "status": "in_progress"}
}
```

Environment Tool 실행 시 `binding_id` → Environment 인스턴스/Conversation 적합성 → Workspace 연결·권한 확인 → 실행 → ToolResult 순서로 처리한다.

### 6.1 Internal Tool과 Environment Tool

- Tool은 최소한 **Internal Tool**(Memory/Plan/Note처럼 Data 공개 API 사용)과 **Environment Tool**(Inside 등 Environment의 Workspace 실행 계약 사용)을 구분하는 방향이다.
- Environment를 거칠 필요가 없는 Memory는 Data API에 직접 접근할 수 있지만 SQL/Repository private 구현에는 접근하지 않는다.
- Workspace 요구 정책은 `None / Required / Default`로 구분하는 것을 권고한다. Outside 기본 Workspace를 암묵적으로 사용하는 Tool은 `None`과 다르다.
- 별도의 외부 서비스 Connector Tool 범주는 추후 필요에 따라 정의한다.

### 6.2 복수 ToolCall의 충돌 기준

- 충돌하지 않는 여러 ToolCall은 병렬 실행 가능. 명령 문자열 동일 여부만으로 독립성을 판정하지 않는다.
- 서로 다른 파일의 **검증된 범위 한정 수정**은 병렬 후보. 동일한 파일에 대한 쓰기 충돌 또는 같은 Workspace의 부작용 범위 불명 `run_command`는 기본 직렬 권고.
- 병렬성 판단은 Binding ID가 아니라 Workspace/실제 공유 자원에 대한 효과를 기준으로 한다. 다른 Conversation에서도 같은 자원 충돌이 발생할 수 있다.
- Tool/Environment가 검증 가능한 효과 범위를 제공하며 모델의 미검증 주장에 의존하지 않는다. 상세 Scheduler 계약은 아직 미결정.

- **동일 Conversation의 실행 흐름과 Run은 직렬화**한다. 내부에서 독립적인 ToolCall의 병렬 처리는 허용된다.
- Event는 종류별 불변 canonical payload, Plan/Note는 Data conversation-scoped Artifact, 모델 이력 재구성은 Prompt의 원본 선택·검증과 Provider의 native serialization으로 나눈다.

자세한 내용은 [Data, Tool Execution, and Artifacts](data-and-tools.md) 및 [Data Contract v0.1](data-contract-v0.1.md)를 따른다.

### 6.3 초기 기본 Workspace — 파일 첨부·생성 기반 (확정 범위)

- **우선순위:** HIMARI가 제공하는 기본 Workspace를 먼저 만들어 채팅 파일 업로드, 파일 생성·읽기·수정·제공의 기반으로 사용한다.
- **초기 제외:** 기본 Workspace 파일 작업에는 인간 Approval, Worktree/Workspace 사본 격리, OS Sandbox를 도입하지 않는다. 단, 구조화된 파일 API 자체의 사용자 범위 검사·경로 탈출 방지·용량 제한·중복 편집 충돌 확인은 유지한다.
- **구조:** Data는 Workspace Registry/Binding 및 파일 참조 메타데이터를 저장하고, Environment의 Inside 파일 실행기가 실제 파일 바이트를 조작하며, Tool은 모델에 파일 Tool을 노출한다. Workspace 전용 소프트웨어 모듈을 추가하지 않는다.
- **파일 bytes vs Artifact:** 파일의 실제 바이트는 기본 Workspace가 원본이라는 초기 권고. Plan/Note의 원본은 기존대로 Data의 conversation-scoped Artifact이다. 업로드 원본 불변성/버전/GC는 미결정.
- **범용 명령 실행 `run_command`와 ManagedProcess를 초기 파일 전용 API와 동일시하지 않는다.** OS Sandbox가 없으므로 프로세스 실행을 추가하려면 별도 위험/권한 계약을 검토해야 한다.

관련 상세 초안: [Default Workspace & Chat Files v0.1](default-workspace-files-v0.1.md).

### 6.4 Pause/Resume 실행 경계 — 제안

- 기존 Run raw 요청·응답과 ToolExecution 원본이 보존되어도 Control이 **어느 안전 경계에서 멈췄는지**는 별도 진행 상태가 필요하다.
- `ConversationControlState`/ExecutionCursor를 Data에 보존하는 권고안에서는 Pause 요청 후 새 Run 또는 Tool 실행을 시작하지 않고, 이미 진행 중인 호출·효과는 가능한 범위에서 결과를 영속화한다.
- 미실행 ToolCall은 pending 상태로 남아 Resume 시 먼저 처리하고, 완결된 ToolCall/ToolResult 그룹 이후에만 새로운 LLM Run을 호출한다.
- `PAUSE_REQUESTED`와 실제 `PAUSED`, `WAITING_FOR_USER`, `COMPLETED`, 결과 불명/복구 필요 상태는 혼동하지 않는다. Pause 정책은 아직 사용자 확정이 아니다.

관련 상세 초안: [Control Pause/Resume v0.1](control-pause-v0.1.md).

## 7. Memory — 방향만 확정

- Memory는 독립적인 특권 하위 시스템이 아니라 **Tool 인터페이스를 통해 호출**하는 방향이다.
- Memory Tool은 필요할 때 Data를 읽고 관련 기억을 구성할 수 있다.
- Memory 생성/수정/삭제 정책, 자동 삽입 여부, 권한 모델, 저장 스키마는 **미결정**이다.

## 8. v2로부터의 단순화 방향

v3는 v2의 계층을 그대로 이식하지 않는다. 현재 Run 중심 실행 모델에는 별도의 Task/ManagerTurn 계층이 없다. Worktree, Git을 통한 Workspace 권한 정의, Work Apply/Discard를 v3의 필수 아키텍처 구성요소로 사용하지 않는다. 특수 HumanDirection 전용 계층도 기본 전제로 두지 않는다.

v2 기능의 필요성은 개별적으로 다시 판단한다. v2의 구현/운영/데이터가 v3와 자동 호환된다고 가정하지 않는다.

## 9. 호환성과 검증

공개 계약(타입·오류·순서·취소·권한 의미)을 유지하는 구현 교체가 다른 모듈의 수정을 요구하지 않도록 한다. 계약 자체를 바꾸거나 모듈이 실패할 때 전체 시스템까지 무조건 정상 운영한다고 **보장하지는 않는다**. 계약 테스트, import 의존성 검사, Fake 구현 교체 테스트로 이 원칙을 검증한다.

미해결 사항은 [open-questions.md](open-questions.md)를 따른다.
