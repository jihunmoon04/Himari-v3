# CurrentState Contract v0.1 — Draft for Review

> 작성: 2026-10-13 · 상태: **PROPOSED / NOT FROZEN**.
>
> 이미 확정된 원칙: **Current State는 Cache-first Prompt의 Conversation 뒤에 위치**하며, **활성 Workspace/Environment는 Artifact가 아닌 운영 상태**다. 아래 DTO, 기본 Binding 스코프, 합성 및 동시성 프로토콜은 검토용 권고안이다.
>
> 관련: [architecture](architecture.md) · [data-contract-v0.1](data-contract-v0.1.md) · [prompt-composition](prompt-composition.md) · [decisions](decisions.md).

## 1. 설계 목표와 책임 경계

**CurrentStateProjection은 읽기 전용으로 구성한 provider-neutral DTO**다. 지속 상태의 원본도 아니고, Workspace용 별도 모듈도 아니며, Tool 승인/권한을 증명하는 토큰도 아니다.

```text
Data Public Read Port
  ├── Conversation-scoped Environment Activations
  ├── Workspace Registry (현재 대화에 노출 가능 항목만)
  ├── Workspace Bindings / Default Selection
  └── Plan/Note Artifacts + revisions
              │
              ▼
      PersistedStateBasis (Data, consistent read)
              │
              ├── optional EnvironmentObservation Port
              │      └── 연결/가용성 관측(신선도 표시)
              ▼
      Prompt CurrentStateProjector (proposal)
              │
              ▼
      CurrentStateProjection (immutable DTO)
              │
              ├── Prompt context rendered AFTER exact Conversation
              └── Run.prompt_basis (version/refs)
```

- **Data** 소유: Registry, Activation, Binding, 기본 Workspace 선택, Artifact, 변경 revision 및 조회 권한.
- **Environment** 소유: 실제 Workspace에 대한 연결/실행/접근 검증, 지원되는 실행 기능에 대한 현장 관측.
- **Prompt** 소유: Data가 반환한 승인된 상태만 포함, 선택적인 Environment 관측 병합, 모델 입력용 직렬화.
- **Provider** 소유: provider-native 역할/메시지 인코딩 및 ToolCall/ToolResult와의 합법적 배치.
- **Control** 소유: 직렬화된 Conversation 실행 경계, Run 요청 시점의 Prompt 입력 확보 및 Run 기록/반복 제어.
- **Tool/Environment 실행** 소유: 실행 직전 권한·Binding 상태 재검사. CurrentStateProjection 또는 캐시한 PromptSnapshot으로 실행 승인을 대신하지 않는다.

**비목표:** CurrentState 전용 영속 테이블, Workspace 전용 소프트웨어 모듈, Tool 실행 권한의 프롬프트 위임, 매 Run 모든 로컬/원격 Workspace 강제 네트워크 프로브.

## 2. 제안하는 Data Read Contract

### 2.1 PersistentStateBasis

Data는 `read_current_state_basis(conversation_scope)`와 동등한 **하나의 일관된 조회**를 제공한다. `conversation_scope`는 신뢰된 호출자 문맥이며 모델이 임의 작성하는 문자열이 권한이 될 수 없다.

| 필드 | 의미 |
| --- | --- |
| `conversation_id` | 조회 대상 대화 |
| `basis` | 아래 2.2절의 데이터 읽기 기준/버전 |
| `environments[]` | 이 대화에서 활성인 Environment Instance 목록 |
| `bindings[]` | 해당 Environment와 대화 범위에서 허용된 Workspace Binding만 |
| `workspace_views[]` | 위 Binding에서 접근 가능한 Registry 항목의 공개 메타데이터만 |
| `default_binding_selection[]` | Environment별 암묵적 기본 대상 설정이 있는 경우 |
| `artifacts[]` | 이 대화에서 Prompt에 포함하도록 선택된 Plan/Note 등과 revision |

상태의 authoritative 값은 Data가 기록한 실제 activation/binding/artifact 상태다. 별도 외부 환경의 연결 관측은 이 API가 보장하는 DB snapshot에 포함하지 않는다.

**일관된 읽기:** Binding이 철회됐는데 선택/default 값만 오래 남아 있거나, Artifact 본문은 revision N인데 참조 revision만 N+1인 상태를 한 결과 안에서 만들지 않는다. 같은 DB의 원본들은 읽기 트랜잭션 같은 단일 논리적 snapshot으로 조회해야 한다.

### 2.2 StateBasis (제안)

```json
{
  "conversation_sequence": 42,
  "conversation_state_revision": 9,
  "workspace_versions": {"ws_01": 4},
  "artifact_revisions": {"plan_01": 7, "note_01": 2}
}
```

- `conversation_sequence`는 **대화 이벤트의 시퀀스**이고 현재 운영 상태 Revision과 다르다.
- `conversation_state_revision`은 Activation/Binding/기본 선택/해당 대화에 속하는 상태가 바뀔 때 증가하는 버전 후보.
- Workspace Registry는 Conversation 사이에서 공유될 수 있으므로 `conversation_state_revision`만으로 Registry 변경을 포착하지 못한다. `workspace_versions` 등 별도 버전 확인을 요구한다.
- Artifact는 콘텐츠의 `revision`으로 검사한다.
- 이 필드는 **버전 벡터 예시**이며 정확한 타입/해시·CAS 구현은 확정 전이다. 최소 계약은 *조립에 사용된 원본 값과 해당 버전을 같이 반환*한다는 것이다.
- Data의 권한 필터링은 immutable DTO 생성 **이전**에 실시한다. Prompt가 모든 Workspace Registry를 받아 필터링하는 구조를 피한다.

## 3. 제안하는 CurrentStateProjection (provider-neutral)

```json
{
  "schema_version": "current_state.v1",
  "conversation_id": "conv_01",
  "basis": {
    "conversation_sequence": 42,
    "conversation_state_revision": 9,
    "workspace_versions": {"ws_01": 4},
    "artifact_revisions": {"plan_01": 7}
  },
  "environments": [
    {
      "environment_instance_id": "env_inside_01",
      "kind": "inside",
      "label": "Inside",
      "default_binding_id": "bind_01",
      "bindings": [
        {
          "binding_id": "bind_01",
          "workspace_id": "ws_01",
          "workspace_label": "Project Workspace",
          "workspace_kind": "managed",
          "binding_state": "active",
          "connection": {"status": "unknown"}
        }
      ]
    }
  ],
  "artifacts": [
    {
      "artifact_id": "plan_01",
      "kind": "plan",
      "revision": 7,
      "content": {"title": "Implement API", "steps": []}
    }
  ]
}
```

**예시이며 실제 API/JSON 스키마는 미확정**이다. 특히 default 선택/표시 label, plan content 전체/요약 선택, 여러 같은 유형의 Environment instance 이름 충돌 처리는 후속 계약으로 남는다.

**필드 해석 규칙:**

- `environment_instance_id`는 대화 중 활성화된 **구체 인스턴스**의 정체성이다. `kind`는 구현 유형이고, 여러 인스턴스가 같은 `kind`를 가질 수 있다.
- `binding_id`는 **Environment와 Workspace 사이 접근 관계**의 식별자다. 모델에게 공개해도 비밀 bearer capability가 아니다.
- `binding_state`는 저장된 관계가 active/disabled/revoked 중 어떤 상태인가를 의미. **`connection.status`와 혼동하지 않는다.**
- **추천:** 기본 Binding 선택은 `(conversation, environment_instance)`당 0~1개로 두고, 반드시 해당 Environment 소유의 활성 Binding을 가리키게 한다. 대화 전역의 한 기본 Workspace를 가정하지 않는다. 해당 스코프 선택은 확정 전이다.
- `connection`의 값은 `online`, `offline`, `unknown` 또는 명시적 관측 오류를 고려한다. 관측값이 있다면 `observed_at`과 **근거/신선도**를 함께 보고한다. 초깃값 `unknown`은 연결 불가능을 의미하지 않는다.
- `artifacts[]`의 포함 범위는 Prompt 문맥 정책에 따라 결정한다. 없는 Artifact와 내용을 생략한 Artifact를 혼동하지 않도록 `omitted`/명시적 존재 정보 또는 선택 규칙이 필요하다.
- 표시 가능한 capability나 Tool 이름을 포함할 경우 Tool Registry와 Environment가 제공한 **허용된 기능**만 투영한다. 이 데이터가 실제 Tool 실행 권한을 부여하지 않는다.
- 경로, 인증 토큰, 로컬 Agent 비밀, root directory, 연결 문자열, 내부 policy 레코드 등은 **Projection에 포함하지 않는다**.

## 4. EnvironmentObservation Port (선택)

`observe_bindings(environment_instance_id, allowed_binding_ids)`와 같은 **read-only 관측 계약**을 고려한다.

권장 속성:

1. **Best effort:** Provider 요청마다 Workspace 접속에 실패한다고 해서 무조건 Prompt 생성 전체를 막지 않는다. 관측 실패/미수행은 `unknown`으로 표시하고, 미검증 성공을 보장하지 않는다.
2. **Side-effect free:** 단순 상태 관측에 실제 명령 실행, Workspace 생성, 승인 획득을 결합하지 않는다.
3. **Bounded:** 명시적 타임아웃/동시성 상한과 자원 제한 필요. 값/정책은 미정.
4. **Freshness:** 오래된 관측을 현재 사실처럼 말하지 않는다. Connection status와 관측 시점을 함께 보존.
5. **Not authority:** 실제 Tool 실행 시 Environment는 현재 Binding, 연결, 접근/승인 정책을 다시 검증한다.

실시간 관측과 Data 영속 상태를 **완벽한 단일 트랜잭션 시점**에 묶을 수 없다. 이 한계를 타입과 에러 의미에 드러내야 한다. 연결 변화는 Run을 구성한 뒤에도 발생할 수 있다.

## 5. Default Workspace Selection 규칙 — 제안

기존 확정 원칙: Tool의 Workspace 요구 여부와 Environment 소속 여부는 독립적이다.

| workspace_requirement | 현재 상태를 이용한 동작 |
| --- | --- |
| `None` | Binding 입력 불필요. `update_plan` 등 Internal Tool에 적합 |
| `Required` | 호출이 명시적으로 **현재 허용된 Binding**을 지정해야 함. Default를 자동 대입하지 않는다 |
| `Default` | 해당 Environment에 유효한 default Binding이 있으면 후보 선택. 부재/철회/모호함은 명시적 실패 |

- **현재 상태를 보고 모델이 선택하는 것과 런타임이 무언가를 암묵 선택하는 것을 분리**한다.
- **Binding ID는 권한 증명이 아니다**: 이름만 맞는 다른 Conversation의 Binding을 모델이 넣어도 Tool/Environment가 실행 시 거부해야 한다.
- Default 대상 변경은 Prompt 생성용 Data state revision을 증가시키는 것이 권고다.
- 현재 활성 환경이 없으면 빈 `environments[]`를 반환한다. 임의의 Workspace를 자동으로 연결하지 않는다.
- **Plan/Note Internal Tool은 Workspace를 필요로 하지 않는다.** Data의 Artifact를 사용하므로 Outside가 활성화되어 있지 않아도 실행된다.

## 6. Cache-first Prompt 및 Snapshot과의 관계

확정된 순서: `Stable Prefix → Conversation → Current State`.

1. Data의 **coherent PersistentStateBasis**와 필요한 Conversation/Artifact를 조회한다.
2. Prompt는 exact chronological history를 재구성하고 **현재 상태를 대화 뒤**에 추가한다.
3. Provider Adapter는 native ToolCall/ToolResult 연결을 보존한 합법적인 Provider 요청을 만든다.
4. **Run.raw_request**에 실제로 전송한 provider-specific 요청을 전부 보존한다.
5. **Latest PromptSnapshot (1개)**에는 마지막으로 구성한 전체 provider-neutral 프롬프트와 상태 기준 정보를 보존한다.
6. 다음 Run에서는 과거 Snapshot의 CurrentState를 **다시 authoritative 원본으로 사용하지 않고**, 최신 상태를 조회하여 뒤쪽 Current State만 재구성한다.

**제안하는 일관성 검증:** Raw Run request를 저장하는 `prepare_run` 경계에서 Data StateBasis가 여전히 유효한지 확인한다. 그 사이 변경이 있다면 stale basis 오류를 반환하여 재조회/재구성한다. 네트워크 관측의 최신성은 동일 CAS로 보장하지 않는다.

**주의:** Provider별 규칙상 tool_result 바로 뒤에 임의 메시지를 추가하면 불법적인 protocol 구조가 될 수 있다. Provider Adapter는 native form으로 허용되는 현재 상태 메시지 표현을 선택하거나 요청을 생성하지 않는다. 논리적 cache-first 순서는 잘못된 wire request를 보내는 근거가 아니다.

Provider 캐시 효율을 위해 Projection은 안정적인 필드 순서/정렬을 사용하고, 실제 관측 없이 `observed_at=now`처럼 매 Run 변화하는 값을 생성하지 않는 것이 좋다. 그러나 **캐시를 위해 오래된 권한/활성 상태를 고정해서는 안 된다.**

## 7. Authority, 실패, 동시성

| 시나리오 | 권장 의미 |
| --- | --- |
| 다른 Conversation의 Workspace가 Registry에 있음 | 활성·승인된 Binding 없으면 Projection에 노출하지 않음 |
| Binding이 조회 후 철회됨 | Tool 실행 단계에서 거부. 이전 Prompt의 ID는 실행 권한이 아님 |
| 로컬 PC가 offline이나 관측 실패 | `offline`은 실제로 관측했을 때만, 그 외 `unknown`. Tool 실행 단계에서 판단 |
| Plan 내용은 revision 7, revision 메타데이터는 8 | Data coherent read 위반: 읽기 실패 처리 |
| Plan 변경과 Binding 변경이 조회 중 발생 | 동일 DB snapshot으로 한 기준에서 읽음; 기준이 달라지면 Run 준비 경계에서 재검증 |
| 환경 관측이 지연 또는 중단됨 | 예산 내 관측, 미확인 상태로 표시; 필수 관측인지 여부는 Tool 정책에 따름 |
| CurrentState에 악의적인 Workspace label/Note 내용 존재 | 신뢰되지 않은 데이터로 명시적 표시, system 지시문으로 승격하지 않음 |
| 기본 Binding이 다른 Environment 인스턴스를 가리킴 | Default 설정을 유효하지 않은 관계로 거부 |
| 다중 Conversation이 동일 Workspace를 가리킴 | Data projection은 각 Conversation의 Binding만 반환. 실제 리소스 충돌 관리는 Tool Scheduler에서 처리 |
| Provider가 State block을 Tool 결과 뒤에 수용하지 못함 | Provider Adapter가 지원되는 native 표현 사용; 불가능하면 명시적 구성 실패 |

## 8. Public Port 경계 및 최소 테스트 — 제안

**Data:** `read_current_state_basis(scope) -> PersistentStateBasis` (DB 내부 테이블/Repository 누출 금지)

**Environment:** `observe_bindings(instance, bindings) -> Observations` (optional, read-only); `authorize_and_execute`는 실제 Tool 실행 계약이며 관측과 별개

**Prompt:** `project_current_state(basis, observations?) -> CurrentStateProjection`; Prompt가 전체 입력을 구성하며 Provider의 raw JSON은 생성하지 않음

**Provider:** `render_current_state(projection, native_context) -> provider-native representation` — native tool result 불변식 준수

계약 테스트 최소 항목:

1. 활성 Environment가 없을 때 빈 목록, 불필요한 Workspace 노출 0.
2. 동일 Workspace가 두 Environment에 Binding되어도 동일 `workspace_id`를 보존하고 `binding_id`는 구분.
3. 같은 `kind`의 서로 다른 Environment 인스턴스에서 Default Binding 충돌 없음.
4. Binding revoke 뒤 Projector가 허용되지 않은 Workspace를 노출하지 않음.
5. 변경 중의 상태를 조회해도 참조/Artifact revision이 일관된 한 시점에서 생성.
6. offline/unknown 차이, 관측 시각 없을 때 허위 online 표시 금지.
7. Model이 과거 Binding ID로 Tool을 요청하더라도 실제 실행 단계에서 재검증.
8. Data에서 생 Workspace 비밀 경로/토큰이 투영 결과에 나타나지 않음.
9. Prompt의 Stable Prefix + exact Conversation 순서는 유지되고 CurrentState만 갱신 가능.
10. Provider Tool 그룹 인접성 위반 없이 CurrentState가 native request에 배치되거나 명시적 거부.
11. Fake Data/Environment 관측 객체로 Prompt 구현을 바꿀 필요 없이 같은 Contract 통과.

## 9. 독립 검토 후 결정할 세부 쟁점

| ID | 항목 | 권고 |
| --- | --- | --- |
| CS-01 | Projection을 조립하는 모듈/Port 소유자 | 별도 모듈 없이 Prompt가 Data + 선택적 Environment 관측 Port 결합 |
| CS-02 | Default Workspace 선택 스코프 | `(conversation, environment_instance)`당 0~1 Binding |
| CS-03 | 읽기 일관성/Run 준비 시 검증 | Data 한 snapshot + source revisions, prepare_run에서 검증 |
| CS-04 | Workspace 연결 상태의 자동 관측 | 초기에는 unknown 허용, 별도 관측은 best-effort·bounded |
| CS-05 | 모델에 포함할 Plan/Note 범위 및 노출 형식 | Prompt budget/선택 정책, 임의 truncated 상태는 명시 |
| CS-06 | Provider native CurrentState 인코딩 | tool/result 그룹 구조를 훼손하지 않는 provider-specific 표현 |
| CS-07 | 사용 가능한 Tool 목록과 Projected capability 표현 | Tool Registry 소유의 schema/권한표면을 중복 생성하지 않음 |
| CS-08 | StateBasis 실제 버전/해시 알고리즘 | Conversation-local revision + 공유 Workspace별 버전 + Artifact revision |

**검토 초안**이다. 이미 채택한 결정과 구분하여 `decisions.md`에 새로운 Confirmed 항목으로 기록하지 않는다.
