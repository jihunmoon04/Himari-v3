# Data, Tool Execution, and Artifacts

> 2026-10-13. **확정된 방향과 세부 권고안을 구분**한다. 이는 구현된 스키마가 아니라 논리적 계약 설계다.
>
> 구체적인 구조·저장 순서·공개 Data Port 초안: [Data Contract v0.1 — PROPOSED](data-contract-v0.1.md). 이 초안은 아직 확정된 사양이 아니다.

## 1. 세 기록의 책임 — 확정 방향

| 논리 영역 | 목적 | 갱신 성격 |
| --- | --- | --- |
| Execution Records | Run별 Provider **원본 요청·응답 전체**와 실제 Tool 호출/실행 사실 | 기록 불변; 진행 상태 전이 가능 |
| Conversation Events | 대화 입력/출력의 **exact** 기록 및 노출 대상으로 선택한 Tool 이벤트 | 순서 있는 append-only 이벤트 |
| Artifacts | Plan, Note 등 독립적으로 열람·수정하는 현재 상태 | 내용/버전 갱신 |
| Latest Prompt Snapshot | 압축 문맥 및 최근 exact 입력으로 만든 최신 모델 문맥 | Conversation별 최신 **1개** 교체 |

- 기본적으로 **모든 Run의 원본 Provider request/response를 보존**한다. 저장 방식(원본 wire JSON, 스트리밍 chunk, redaction)은 세부 설계로 남긴다. 비밀 인증 정보/헤더를 그대로 보존한다는 의미가 아니다. 응답을 못 받은 시도에는 존재하지 않는 응답을 만들어 넣지 않는다.
- Conversation의 정확한 기록과 사용자 UI 표시, 다음 Run의 Provider 입력은 **서로 다른 차원**이다.
- 원본 Conversation을 가공된 Prompt로 덮어쓰거나, Prompt Snapshot의 압축 결과를 원본으로 간주하지 않는다.

## 2. ToolResult 저장/노출 정책 — 확정된 사례

| 도구/종류 | Execution | Conversation | 별도 상태 |
| --- | --- | --- | --- |
| `memory_search` | 원본 호출/결과 | 검색 결과를 ToolResult로 보존 | Memory 자체는 Data가 보유 |
| `update_plan` | 원본 호출/결과 | `complete` 등 간결한 ToolResult | Plan Artifact (저장 위치는 제안) |
| `update_note` | 원본 호출/결과 | 간결한 ToolResult | Note Artifact (저장 위치는 제안) |
| 일반 Environment Tool | 원본 호출/결과 | 도구별 정책에 따름 | 선택적 |

- **모델에 실제로 반환되는 ToolResult**, **Conversation에 무엇을 저장하는지**, **UI에 어떻게 표시하는지**를 동일한 값으로 강제하지 않는다. 단, Conversation에 저장하기로 한 값은 정확하게 기록한다.
- ToolCall의 의미 있는 provenance는 `call_id` 등 안정적인 참조로 묶는다.
- Conversation에서 생략해도 Execution Records에서 정확한 이벤트/결과를 추적할 수 있어야 한다. 다음 모델 입력에 필요한 native Tool 그룹을 재구성할 책임은 Prompt + Provider 경계에 있다.
- **확정: Plan/Note의 원본 저장소는 Data의 conversation-scoped Artifact**다. Outside Workspace에는 필요시 projection/export한다. 원본을 Workspace로 옮기지 않으며, Outside 기본 Workspace를 사용해 별도 파일을 수정하는 Tool은 `None`이 아니라 `Default` Workspace 정책을 따른다.

## 3. Artifact 계약 — 제안

- Artifact ID, owner/scope, kind, content schema, current revision, provenance (originating call/run).
- **Artifact는 Plan/Note 등의 콘텐츠이며, 활성 Workspace/Environment/Binding은 Artifact가 아니다.** Workspace Registry·Environment Activation·Workspace Binding·기본 선택 설정은 Data의 별도 운영 상태이고, 현재 연결 가능 여부는 Environment에서 확인한다.
- Artifact 도메인 의미(PlanStep, Note 형식)는 각 Internal Tool이 담당. Data의 범용 저장 API는 내용을 의미론적으로 해석하지 않는다.
- 같은 Artifact를 동시에 수정하면 revision compare-and-swap으로 충돌을 보고; 서로 다른 Artifact는 별도 부작용이 없을 때 병렬 처리 가능.
- 가능한 Internal Tool의 **Artifact 변경 + Tool 완료 기록 + 필요한 Conversation Event 기록**은 같은 Data 트랜잭션으로 원자적 커밋하도록 권고.
- 외부 파일/명령 부작용은 DB 트랜잭션으로 롤백할 수 없으며 미확인 완료 시 자동 재실행하지 않는 계약이 필요.
- 이벤트 / run / artifact 사이의 참조 및 대화 삭제와 Artifact 보존/삭제 정책은 세부 결정 전.

## 4. Internal Tool과 Environment Tool — 설계 방향

```text
Control
   ↓
Tool Registry / Dispatcher / Scheduler
   ├── Internal Tool ── Data public API (Memory, Plan, Note)
   └── Environment Tool ── Environment public execution API ── Workspace
```

- **Workspace 전용 소프트웨어 모듈을 만들지 않는다.** Data는 Workspace Registry/Binding을 보관하고 Environment는 연결/실행을 책임진다.
- Memory는 **Internal Tool → Data 공개 API**로 접근한다. Outside/Inside를 의무적으로 통과하지 않는다. Data 내부 SQL/Repository 직접 접근은 금지한다.
- Workspace 요구 여부는 Environment 소속 여부와 독립이다.
- Workspace 필요 Tool은 권한 검증 가능한 binding/workspace 참조를 요구한다. Workspace 비필요 Tool은 대상 없이 실행한다. 기본 Workspace 암묵 선택은 `Default`라는 **별도 정책**이어야 한다.
- External Service Tool을 나중에 별도 유형으로 나눌지, 등록 시그니처와 결과 형식은 미정이다.

## 5. 복수 ToolCall과 동시성 — 확정 방향 / 정책 권고

**확정 방향:** 서로 충돌하지 않는 ToolCall은 병렬 실행할 수 있다. **명령 문자열의 동일성 여부는 안전성의 충분한 기준이 아니다.**

초기 권고 정책:

| 충돌 후보 | 기본 처리 |
| --- | --- |
| 읽기만 수행하는 서로 독립적인 작업 | 병렬 |
| 검증된 서로 다른 파일에 대한 한정 수정 | 다른 공유 부작용이 없으면 병렬 |
| 같은 파일에 읽기/쓰기 또는 쓰기/쓰기 | 직렬 |
| **같은 Workspace의 범위를 알 수 없는 `run_command`** 여러 개 | 기본 직렬 |
| 서로 다른 Workspace의 작업 | 공유된 실체 자원이 없다고 확인되면 병렬 |
| 동일 Workspace를 사용하는 **서로 다른 Conversation** | 동일 충돌 규칙 적용 |

Tool/Environment가 검증 가능한 자원 접근 범위(읽기/쓰기/불명)를 제공한다. LLM이 주장하는 경로나 명령 문자열만으로 실제 영향 범위를 증명하지 않는다.

Scheduler는 Binding ID만 다르더라도 **실제 Workspace/공유 자원 정체성**으로 충돌을 판단해야 한다. 이종 Workspace 별칭/공유 디렉터리/외부 DB가 존재할 수 있으므로 완벽한 자동 충돌 판정은 전제하지 않는다.

복수 ToolCall의 결과는 각 `call_id`에 귀속한다. 실제 완료 순서와 원래 호출 순서를 보존하고, Provider가 요구하는 native group으로 정규화한다. **확정: 같은 Conversation의 Control/Run 실행 흐름은 직렬화**한다. 하나의 실행 흐름에서 별도로 독립성이 검증된 ToolCall들은 병렬로 실행할 수 있다. ToolCall의 완료 배리어, lease/queue 구현은 여전히 미결정.

## 6. 추가로 확정할 계약

- Run/request/response raw serialization 및 실패 전·후 영속 기록 원자성.
- `ExecutionRecord`와 `ConversationEvent` 간 이벤트 ID, sequence/cursor, 출력 누락과 조회 정책.
- **확정: Prompt는 Conversation과 Execution 기록을 선택하고 완결된 호출 그룹인지 검증, Provider는 native wire 직렬화**한다. 구체 Tool 그룹 데이터 구조와 불완전한 결과의 오류 코드는 미결정.
- **확정: Event는 kind별 불변 canonical payload**다. 종류별 세부 schema는 미결정.
- **확정: Plan/Note 원본은 Data conversation-scoped Artifact**. Artifact 삭제/공유 범위와 revision 이력 보존은 미결정.
- CurrentStateProjection의 데이터 합성 범위, 현재 활성 Environment/Binding 설정의 보존 및 Environment의 실제 온라인 상태 반영 규칙.
- Data 트랜잭션과 외부 효과의 중복 실행 방지/중단 복구.
- Tool Scheduler의 충돌 스코프, 읽기와 쓰기의 구별, 동일 자원 alias 검증 및 동시성 한도.
- Inside의 보호 실행/사용자 승인/Workspace Binding 보안 계약.
