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
| Run | 모델 호출 1회의 식별 및 실행 기록; 상세 저장 범위는 미결정 |
| Tool 기록 | Conversation에 포함되는 이벤트와 별도 실행 기록이 존재할 수 있음. 어떤 Tool을 어디에 기록할지는 별도 정의 |
| 최신 Prompt Snapshot | 압축 문맥과 이후의 exact 입력 데이터를 다음 호출에 사용하기 위한 **최신 상태 1개** |
| Workspace Registry | 독립적인 Workspace의 식별과 연결에 필요한 메타데이터 |
| Environment/Binding | Environment 활성화 및 Workspace 접근 관계의 메타데이터 |
| Model/Reasoning 설정 | 모델·추론 강도 등 다음 요청 구성에 필요한 설정 및 provider가 제공한 사용량/continuation 자료 |

- **원본 Conversation ≠ 가공된 Prompt.** Prompt가 압축/필터링한 데이터는 Conversation 원본을 덮어쓰지 않는다.
- 최신 Prompt Snapshot은 누적 버전을 보관하는 로그가 아닌 **대체 가능한 단일 최신 상태**이다. 압축 요약과 최근 exact 항목을 포함하는 설계 방향으로 정했다.
- 모든 과거 Provider 요청을 최신 Snapshot만으로 완전 재현할 수 있다고 가정하지 않는다. 추가적인 감사/재현 자료 보존 범위는 미결정이다.
- 다른 모듈은 DB 테이블을 직접 읽거나 수정하지 않고 **Data의 공개 조회/명령 계약**으로 접근한다.

## 3. Prompt — 확정된 책임, 미정인 세부 계약

Prompt는 Data의 원본 대화, 최신 Prompt Snapshot, 설정 및 노출 가능한 Tool/Environment 정보를 활용하여 **다음 LLM 요청을 구성**한다.

```text
Conversation exact history ──┐
Latest Prompt Snapshot ──────┤
Model / reasoning config ────┼──▶ Prompt ▶ provider request
Exposed tool & env metadata ─┘
```

- 압축 및 컨텍스트 예산 전략은 Prompt의 내부 구현으로 캡슐화한다.
- 최신 Prompt Snapshot을 갱신할 때 원본 Conversation은 보존한다.
- 모델별 요청 필드(예: reasoning effort, token budget)는 지원 가능한 Provider 인터페이스를 통해 구성한다. 모든 모델이 같은 옵션을 지원한다고 가정하지 않는다.
- **Reasoning 구분:** (1) 요청 설정값, (2) provider가 공개하는 reasoning token 사용량, (3) provider가 반환·재사용을 허용하는 opaque reasoning/continuation 자료를 각각 별도로 다룬다. **비공개 내부 추론 원문은 획득·보존 가능하다고 전제하지 않는다.** 실제 보존/전달 가능 범위는 provider 계약에 따른다.

## 4. Module 개요 — 확정된 책임 분리

| 모듈 | 소유한 책임 | 소유하지 않는 책임 |
| --- | --- | --- |
| Control | Run 반복·종료 분기·Tool 호출 오케스트레이션 | Provider 세부 API, DB 스키마, 환경별 실행 메커니즘 |
| Data | 영속 상태·원본 기록·조회/변경 계약 | 프롬프트 정책, 실제 명령 실행 |
| Prompt | 모델 요청 입력 조립·컨텍스트 압축 | Conversation 원본 변경, Provider 전송 |
| Provider | 모델 API 통신·응답 정규화 | Tool의 환경별 실행, Run 반복 정책 |
| Tool | 노출된 도구의 등록/호출 계약·결과 형태 | 환경별 Workspace 수명·정책 |
| Environment | Workspace 접근 방식·실행 정책·환경 전용 Tool 처리 | Workspace 자체의 수명 소유 |
| Workspace (개념/리소스) | 작업 대상의 지속적인 정체성과 자원 | 특정 Environment 구현 |

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
