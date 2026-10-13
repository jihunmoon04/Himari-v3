# Prompt Composition Contract

> 2026-10-13. **프롬프트의 논리적 순서와 Trigger 처리 규칙은 확정**. Provider별 메시지/캐시 구현과 압축 알고리즘은 미결정.
>
> [Architecture](architecture.md) · [Decisions](decisions.md) · [Open questions](open-questions.md)

## 1. 확정된 논리적 순서: cache-first

```text
Stable Prefix
  ├── Fixed Instructions / trusted policies
  └── Stable Tool Definitions (provider가 요구하는 별도 tool 필드를 포함)
      ↓
Conversation
  ├── Older compressed context (있을 경우)
  └── Exact chronological tail
       ├── User / Assistant messages
       └── native ToolCall + ToolResult group
      ↓
Current State
  ├── Active Environment and accessible Workspace bindings
  ├── Current Plan / Note and other selected Artifacts
  └── Other current, non-historical operational state
      ↓
Provider-specific request parameters / continuation
  (메시지 꼬리에 임의 텍스트를 붙인다는 의미가 아님)
```

**최종 선택은 Stable Prefix → Conversation → Current State.** 고정 접두부 및 기존 Conversation을 가능한 한 그대로 두고, 새로운 Conversation 이벤트를 뒤에 추가하여 prefix caching 기회를 키운다.

- Tool 목록/스키마, 지시문의 버전이나 선택이 달라지면 Stable Prefix가 달라질 수 있다. 안정성은 허용된 기능을 숨기거나 과장할 이유가 아니다.
- Conversation의 과거 항목을 프롬프트 최적화를 이유로 임의 재정렬하지 않는다. 압축 구간만 명시적으로 대체하고, 최근 exact tail은 시간 순서를 보존한다.
- Current State는 **과거 사실의 수정이 아니라 최신 상태의 별도 투영**이다. 사용자 데이터/Plan/Memory 등 신뢰도가 낮은 내용은 높은 우선순위의 지시문으로 승격하지 않는다. Provider 역할(role) 배정과 메시지 순서는 별개의 문제다.
- 최종 State 메시지는 반드시 Provider 프로토콜과 호환되어야 한다. ToolCall ↔ ToolResult 결합을 방해하면 허용되는 provider-native 블록/역할로 표현하거나, 호환 불가 시 요청을 거부한다. 그럴듯한 가짜 Tool 메시지를 삽입하지 않는다.

## 2. Trigger는 Data Event의 참조 — 별도 저장소나 이동 없음

- 사용자 입력과 실제 Tool 결과는 발생 시점부터 Data의 원본 Event/Execution 기록에 저장한다.
- **Trigger는 다음 Run을 시작하게 한 Event(또는 Event 그룹)를 지칭하는 논리적 역할**이다. 원본 Event를 별도로 복제·이동하지 않는다.
- 한 Run이 ToolCall 여러 개를 낸 경우, 그 호출 그룹 및 대응하는 결과 그룹이 다음 Run을 촉발할 수 있다. Trigger가 단일 Event라고 고정하지 않는다.
- 새로운 Run에서 이전 Trigger는 그저 이어진 Conversation의 과거 항목이 된다. Provider request를 구성할 때는 **같은 원본 Event를 한 번만** 전송한다.
- 일부 ToolResult는 Conversation에 기록되지 않을 수 있다. 다음 모델 입력에 반드시 필요한 경우에는 Execution Records를 통해 참조하되, 중복·순서 오류 없이 provider-native history를 완전하게 재구성해야 한다. 재구성 불가능하면 fail closed 한다.

예시:

```text
Data:
  Event 101 User input
  Run 1 → ToolCall A, ToolCall B
  ToolResult A, ToolResult B recorded against matching call_ids
  Run 2 triggers = {ToolResult A, ToolResult B}

Run 2 Provider input:
  stable prefix + previous Conversation (including A/B group, exactly once)
  + latest Current State
```

## 3. ToolCall/ToolResult chronology

- ToolCall과 그 대응 결과는 같은 **논리적 호출 그룹**으로 유지한다. 사이에 Current State/Plan을 끼워 넣지 않는다.
- 같은 Run에서 나온 다중 ToolCall은 provider-specific native 형태로 전송한다. 일반적인 wire 예는 **모든 calls → 모든 corresponding results**, call_id로 정확히 연결한다.
- Tool 실행 완료 시각은 원래 호출 순서와 다를 수 있다. Run의 요청 순서, 완료 순서, 결과 매핑을 구분해 원본에 보존한다.
- Provider마다 tool-use와 result 인접성/역할/Reasoning Item 배치 요구가 다르므로 최종 직렬화는 **Provider Adapter 책임**이다.
- Result 본문이 UI에서 Compact로 표시되거나 별도 Artifact에 저장되는지는 프로토콜상 필요한 모델 입력을 변경하지 않는다.

## 4. Reasoning은 메시지 본문의 마지막 단락이 아니다

구분하여 다룬다:

1. **요청 설정**: model, reasoning effort, token budget 등 Provider 파라미터.
2. **실행 측정**: Provider가 보고한 reasoning-token 사용량 등의 Run 메타데이터.
3. **재사용 가능한 continuation**: Provider가 실제로 돌려준 opaque/encrypted reasoning·continuation 표현.

(2)의 수치를 prompt 텍스트에 붙여 이전 추론을 복원할 수는 없다. (3)은 해당 Provider가 명시적으로 지원하는 API 항목으로 **원래 생성 이력과 호환되는 위치**에 배치한다. Provider가 공개하지 않은 사적 추론 원문은 수집/재현을 전제하지 않는다.

## 5. Prefix cache 실무 제약

- 프롬프트 앞부분이 바이트/토큰 단위에서 얼마나 같은지, tool 스키마와 모델이 일치하는지 등이 캐시 재사용에 영향을 준다. **캐시 hit는 보장값이 아니다.**
- 현재 상태가 매 Run 달라져도 이전의 Fixed + Conversation prefix는 동일하게 유지될 수 있다.
- 기존 메시지의 'Trigger → History' 논리적 분류는 storage migration이 아니다. Provider로 보낸 직전 prefix를 재작성하거나 이전 Event를 두 번 보내면 캐시·정확성이 깨진다.
- 압축 Snapshot이 교체되면 과거 문맥 구간의 프롬프트가 달라져 일부 캐시가 무효화된다.
- Provider 전용 cache-control 마커/최소 길이·TTL·지원 기능은 Provider Adapter 계약에서 판단한다. Prompt는 provider-neutral ordering 및 정합성을 소유한다.

## 6. v2와의 관계

HIMARI-v2의 OpenAI adapter는 이미 `instructions → chronological history → optional objective/memory → runtime context → current plan → optional directive/steering/cue` 순서를 사용하고, 다중 ToolCall을 native group으로 재구성하며, opaque reasoning continuation을 기존 anchor 위치에 replay한다.

v3는 **기본 배치의 대부분을 계승**하되 Task/ManagerTurn-specific channel을 일반화하고 Prompt 모듈에 문맥 조립 정책을 모은다. Provider Adapter는 wire protocol 변환과 continuation 유효성 판단을 맡는다.

## 7. 미결정

- Stable Prefix 내용/버전, Tool 정의의 안정적 집합 유지 방식.
- 현재 State를 Tool-result-only 최신 요청 뒤에 provider별 어떤 role/블록으로 보낼지.
- 압축 정책, exact tail 범위, Snapshot cursor / revision 일치 검사 및 단일 Snapshot 갱신 트랜잭션.
- Conversation에 포함하지 않는 결과의 모델 입력 포함 기준, Provider별 replay-safe 판정.
- Provider별 cache-control/continuation 제한과 검증 지표(실제 cache hits, 토큰 비용, 품질).
