# Data Contract v0.1 — Draft for Review

> 작성: 2026-10-13 · 상태: **PROPOSED / NOT FROZEN**. 이 문서의 타입, 필드 이름, 상태 전이 및 API는 검토 초안이다. 이미 확정된 원칙과 충돌할 경우 [decisions.md](decisions.md)를 우선한다.
>
> 관련 문서: [architecture](architecture.md) · [data-and-tools](data-and-tools.md) · [prompt-composition](prompt-composition.md) · [open-questions](open-questions.md).

## 0. 범위와 확정된 전제

이 문서는 **Data 모듈만의 영속 계약**을 구체화한다. Control의 분기 정책, Prompt 압축 전략, Provider wire 형식, Tool Scheduler의 충돌 알고리즘은 소유하지 않는다.

**이미 확정된 사항**:

1. Run = **LLM 호출 1회**, Run의 집합으로 Task를 정의하지 않음.
2. Conversation의 기록 대상 입력·출력은 **exact**, Prompt의 가공 문맥과 분리.
3. 최신 Prompt Snapshot은 Conversation당 **1개만 유지**(압축 문맥 + 최근 exact tail).
4. **모든 Run의 원본 Provider request / 실제로 받은 response 전부**를 기록.
5. 실제 Tool 실행 사실, Conversation에 표시/보존할 Event, Plan/Note 등 변경 가능한 Artifact는 **논리적으로 별도**.
6. Tool의 실패는 결과를 모델에 전달. 같은 Run의 복수 ToolCall은 독립적이면 병렬 실행 가능.
7. Prompt 순서는 **stable prefix → Conversation → current state**. Trigger는 이미 기록된 Event/완료 그룹의 참조이지 데이터 이동이 아님.
8. ToolCall–ToolResult는 provider가 요구하는 **완전한 네이티브 그룹**으로 전송.
9. Workspace Registry / Binding은 Data에 저장하고, Workspace는 독립적으로 존재하는 자원이며 전용 코드 모듈을 만들지 않음.

## 1. Data 모델 (제안)

모델 수는 최소화한다. 각각 논리적 데이터 모델이지 반드시 1개 테이블/1개 JSON 파일이라는 의미는 아니다.

### 1.1 ConversationEvent

| 필드 | 의미 |
| --- | --- |
| `event_id` | 불투명 고유 ID |
| `conversation_id` | 소속 대화 |
| `sequence` | 해당 Conversation의 **단조 증가 순번**; 순서의 유일한 기준 |
| `kind` | `user_message`, `assistant_message`, `assistant_tool_calls`, `tool_result`, `assistant_question` 등 정규화된 이벤트 종류 |
| `payload` | 이벤트의 **exact canonical payload** (커밋 후 불변) |
| `origin_run_id?` | Assistant/Tool 이벤트의 기원 Run |
| `call_id?` / `call_ids?` | Tool 호출/결과 상관관계 |
| `created_at` | 관측·표시용 시간; 순서 권위 아님 |

- `sequence`는 Conversation 안에서 유일해야 한다. 같은 번호의 의미 수정이나 중간 삽입은 하지 않는다.
- **Exact**는 실제로 보존하기로 한 이벤트의 canonical payload를 커밋 후 변형하지 않는다는 의미. Provider의 원래 wire 응답을 그대로 의미하지 않는다(원본은 RunRecord에 별도 보존).
- Assistant가 ToolCall을 여러 개 생성한 경우 **하나의 ordered call group**으로 표현할 수 있다. 각 call_id, 인자, 원래 호출 순서를 기록한다.
- 일부 ToolResult는 Conversation에 나타나지 않을 수 있다. **실행 자체가 없었다는 뜻은 아니다**.
- UI의 `Complete`, 축약, 접기, 아이콘은 원본 이벤트 변경이 아닌 **presentation projection**이다.

### 1.2 RunRecord

| 필드 | 의미 |
| --- | --- |
| `run_id` | LLM 호출 1회를 식별하는 고유 ID |
| `conversation_id` | 이 호출이 속한 대화 |
| `trigger_refs` | Run을 시작시킨 기존 Event ID 또는 완결된 Tool 실행 참조 그룹 |
| `prompt_basis` | 요청 구성 시 참조한 Conversation cursor / Prompt Snapshot revision / 관련 상태 버전 (정확한 필드는 미정) |
| `provider_key`, `model_id` | 호출에 사용한 Provider/모델 |
| `raw_request` | **실제 Provider 요청 본문**(인증/전송 비밀 헤더 제외), 원본 형태로 불변 보존 |
| `raw_response?` | 실제 수신한 Provider 응답 본문. 수신하지 못했을 때는 없음 |
| `normalized_output?` | Provider 변환 후 의미를 가진 결과; 재구성 가능한 파생 기록 |
| `usage?` | Provider에서 보고한 사용량(추론 토큰 등) |
| `state` | 아래 호출 상태 |
| `error?`, `timestamps` | 수신 여부, 실패/중단의 진단과 관측값 |

권고 상태: `PREPARED` (요청 본문 영속화) → `IN_FLIGHT` (호출 시작 시도) → `RECEIVED` (응답 영속화). 실패 시 `FAILED`, 송신/수신 여부가 확실하지 않을 때 `OUTCOME_UNKNOWN` 별도 처리. **구체 상태 전이는 미확정**.

- **Run 1개 = Provider 호출 시도 1회**로 제안한다. Provider가 내부에서 자동 재시도하여 2회 이상 전송하지 못하게 하고, 별도 재요청 시 독립된 새 Run을 작성한다. 호출 실패도 Run 기록으로 남는다.
- Provider 호출은 외부 부작용이므로 `IN_FLIGHT` 표시만으로 실제 송신 성공을 증명하지 않는다.
- Raw 응답을 수신한 경우에는 **정규화/Conversation projection 이전에 영속화**하는 순서를 권고한다. 이후 정규화가 실패해도 원본 증거가 남는다.
- 요청에 비밀이 들어갈 수 있으므로 원본 본문은 강한 접근 제어/암호화를 전제로 한다. **Authorization 헤더 등 전송용 credential은 원본 요청 본문에 포함하지 않는다.** 정확한 민감 정보 보호·삭제·보존 정책은 별도 결정.

### 1.3 ToolExecutionRecord

| 필드 | 의미 |
| --- | --- |
| `execution_id` | 내부 Tool 실행 레코드 ID |
| `origin_run_id` / `call_id` | 어느 Run의 어떤 ToolCall인지; **`(run_id, call_id)` 유일** |
| `ordinal` | 같은 Run 안에서 모델이 요청한 원래 호출 순서 |
| `tool_id`, `arguments` | 선택된 Tool 및 exact canonical 호출 인자 |
| `workspace_binding_ref?` | 필요할 경우 요청한 Binding 참조 (ID 자체는 권한 증명이 아님) |
| `state` | `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `DENIED`, `CANCELLED`, `UNKNOWN` 등 |
| `raw_execution_result?` | 실제 Tool 구현/Environment가 생성한 실행 결과 (저장 정책에 따른 원본) |
| `model_result?` | 모델에 반환할 ToolResult; 동일 Raw 결과일 필요 없음 |
| `conversation_event_id?` | Conversation에 기록한 결과 Event의 참조; 없는 경우 null |
| `artifact_refs?`, `timing?` | 상태 변경 출처 및 관측 자료 |

- ToolCall은 모델 응답을 먼저 기록한 뒤 실행한다. call_id, ordinal 및 exact 인자는 **실행 전 영속화**한다.
- Model-visible ToolResult는 Tool 구현의 계약에 맞게 생산된다. Plan/Note는 `complete` + artifact revision처럼 축약된 **실제 반환 데이터**를 만들 수 있다.
- Conversation에 결과를 보존하기로 했다면 **해당 model_result의 exact 값**을 기록한다. UI의 `Complete` 요약과 혼동하지 않는다.
- 외부 효과가 발생할 수 있는 Tool은 중단 이후 완료 여부 불명(`UNKNOWN`)을 허용하며 **자동 재실행하지 않는다**.
- Tool이 UI에서 숨겨지는지와 Provider가 필요로 하는 tool_result/history 결합은 별도의 의미이다.

### 1.4 PromptSnapshot

| 필드 | 의미 |
| --- | --- |
| `conversation_id` | Snapshot의 단일 소유 대화; **고유 인덱스** |
| `revision` | Snapshot 갱신 revision |
| `prepared_prompt` | **가장 최근 구성된 전체 provider-neutral 프롬프트**: Stable Prefix + compressed history + exact Conversation tail + Current State |
| `compressed_context` | `prepared_prompt` 내부에 포함된 압축된 과거 문맥; 구조화된 부분으로 보존 가능 |
| `exact_tail_refs` | `prepared_prompt`에 포함한 exact Event/Tool 그룹 참조(누락·중복 검증) |
| `based_on_run_id?` | 어느 Run의 프롬프트를 마지막으로 구성했는지 추적 (아직 Run 미실행 시 null) |
| `covers_through_sequence` | 압축이 반영한 Conversation Event 순번 |
| `replay_coverage?` | Conversation에 생략한 ToolResult를 Execution Record와 함께 재구성할 때 필요한 참조/경계 |
| `format_version` | 향후 내용 형식 변경 대비 |
| `updated_at` | 관측용 |

- 최신 **한 row/한 논리 객체**만 교체해 보존. 옛 Snapshot 사본은 유지하지 않는다.
- **Snapshot은 요약만이 아니라 마지막으로 구성한 프롬프트 전체의 materialized snapshot**이다. 압축된 부분과 exact 부분을 함께 포함하고, 현재 상태 및 고정 부분도 그 시점의 실제 구성에 대응한다. 원본 Run별 provider wire 요청은 RunRecord가 별도로 보존한다.
- Snapshot은 **원본이 아니고 가공된 컨텍스트**. Coverage에는 완결되지 않은 ToolCall–ToolResult 그룹을 넣지 않는다.
- 다음 Run에서 Prompt는 마지막 Snapshot의 **안정적 역사 문맥 부분**을 재사용할 수 있으나, `covers` 이후 exact Event/필요한 Execution Records를 반영하고 **Current State는 최신 값으로 교체**한다. 이때 이미 포함된 `exact_tail_refs`를 중복 편입하지 않는다.
- 프롬프트를 새로 구성한 경우 최신 Snapshot **하나를 그 프롬프트로 교체**하는 방식이 기본 제안이다. 압축 계산은 매 Run 필수가 아니지만 Snapshot이 단순한 오래된 요약만 저장해서는 안 된다.
- `replace_snapshot(expected_revision, expected_conversation_boundary, new_content)` 같은 compare-and-swap을 권고한다.
- 각 Run이 실제 전송한 provider-specific 요청은 RunRecord의 **raw_request**에 모두 보존된다. 최신 PromptSnapshot은 provider-neutral 구성 상태이므로 두 자료는 목적이 다르다. Snapshot 교체 후에도 과거 Run 요청은 소실되지 않는다.
- Prompt의 내부 압축 전략/품질/예산과 Snapshot을 작성할 시점은 **Prompt 모듈** 소유이며 Data는 정합성과 보존을 책임진다.

### 1.5 Artifact (Plan/Note 등) — 별도 현재 상태

| 필드 | 의미 |
| --- | --- |
| `artifact_id`, `conversation_id` | 식별자/소속 |
| `kind`, `content_schema` | Plan/Note 등의 유형 및 스키마 버전 |
| `revision`, `content` | 현재 상태와 변경 버전 |
| `origin_execution_id?` | 마지막 변경을 유발한 Tool 실행 |

- Artifact는 Conversation Event가 아니다. 수정 가능한 **작업 결과·사용자/에이전트 작성 콘텐츠**다. **활성 Workspace/Environment 및 Binding은 Artifact가 아니다.**
- **확정:** Plan/Note의 원본은 Data의 **conversation-scoped Artifact**로 관리한다. Outside의 기본 Workspace에는 원본을 중복 소유하지 않고 필요시 projection/export를 제공한다.
- 데이터 접근은 범용 Artifact Read/Write 계약, 의미론적 편집은 각 Internal Tool이 맡는다.
- Revision CAS 및 동일 Artifact의 수정 충돌 거부를 권고한다.
- 변경 이력의 보존 정책은 추후 결정. 현재 Revision과 Run/Tool 실행 provenance는 최소 보존을 권고한다.

### 1.6 Operational State와 CurrentStateProjection — 원칙 확정, 필드는 제안

**Artifact는 에이전트가 작성·수정하는 Plan/Note 같은 콘텐츠**다. Workspace의 활성화·연결 관계는 Artifact와 의미와 권한 경계가 다른 **운영 상태(operational state)**다.

| 원천 | Data/Environment 책임 | Prompt로 투영할 정보 |
| --- | --- | --- |
| Workspace Registry | Data: 고유 Workspace ID, 유형, 등록 상태, 참조 메타데이터. Workspace 수명은 Environment에 종속되지 않음 | 표시 가능한 이름/ID/유형만 |
| Environment Activation / Instance | Data: Conversation에서 활성화한 Environment 인스턴스와 설정(정확한 스키마 미결정) | 활성 Environment 식별 및 지원 기능 |
| Workspace Binding | Data: Environment Instance + Workspace + Conversation의 관계, 활성/철회 상태, 권한 정책 참조 | 사용 가능한 Workspace Binding 식별과 범위 |
| Default Workspace Selection | Data: 해당 Conversation/Environment의 기본 Binding 선택(스코프 미결정) | 별도 명시가 없을 때 기본 대상 안내 |
| Conversation Working Directory | Data: 각 Conversation → 기본 Workspace 내 논리적 상대 cwd 매핑(세부 저장 방식 제안) | Sandbox 내부의 기본 경로, 허용된 다른 대화 폴더로의 접근 범위 |
| Artifact | Data: Conversation-scoped Plan/Note 본문과 revision | 현재 Plan/Note 중 Prompt에 필요한 항목 |
| Live Connectivity | Environment: 대상 Workspace의 온라인/연결/실행 가능 여부 관측 | 필요할 때만 최신 관측을 반영; 저장된 활성 상태로 온라인을 추정 금지 |

`CurrentStateProjection`은 **새로운 authoritative 원본/Workspace 전용 모듈이 아니라 여러 공개 Port의 합성된 읽기 DTO**다. 상세 Port, 필드/상태 범위 및 기본 Binding 정책 초안은 [CurrentState Contract v0.1](current-state-contract-v0.1.md)을 참고한다. 누가 합성을 수행할지는 Composition Root/Prompt 경계 구현 시 결정하되, 각 원본 소유권은 이전 표에 따른다.

```text
Data: Registry + Activations + Bindings + Settings + Artifacts
          \
           --> CurrentStateProjection --> Prompt --> Provider
          /
Environment: optionally observed live connectivity/capabilities
```

- **Data persisted activation ≠ 실제 온라인 상태 ≠ 현재 실행 권한**이다. Tool을 호출할 때 Environment는 현재 Binding·권한·연결 상태를 별도로 다시 검증한다. Projection의 표시용 정보 또는 Binding ID는 권한 증명이 아니다.
- Conversation 밖에서도 Workspace 자체는 지속할 수 있지만, 그 Workspace에 접근하는 활성 Binding은 Conversation/Environment 범위에서 관리한다.
- **V3-032~036 확정:** 기본 Workspace Root는 **신뢰된 사용자/principal별**로 소유한다. Conversation별 기본 cwd와 같은 사용자에게 허용된 다른 Conversation 디렉터리 접근이 가능하다. `inside/run_command`는 OS Sandbox 실행을 요구하며 **네트워크 기본 차단**이다. WorkingDirectory는 자원 식별과 다른 Data 설정이고 Conversation 폴더는 OS 보안 격리가 아니다. 사용자 인증·Root 매핑/지원 여부는 상세 설계 대상. 제안: [Default Workspace v0.2](default-workspace-files-v0.2.md), [Sandbox Execution](sandbox-execution-v0.1.md).
- CurrentState는 Event나 Artifact로 복제할 필요가 없다. 마지막 Run에서 구성된 **전체 프롬프트는 최신 PromptSnapshot**, 실제 송신된 provider-specific 요청은 **각 Run의 raw_request**로 보존한다.
- 현재 상태의 투영 값이 언제나 원본과 실시간 동기라는 보장은 없다. 요청 생성 시 읽기 버전/기준과 Tool 실행 시 재검증이 필요하다.

## 2. 관계와 불변식

```text
Conversation(id)
 ├── ConversationEvent(seq=1...N, exact canonical)
 ├── RunRecord(run_id, raw request/response, trigger_refs)
 │     └── ToolExecutionRecord(call_id, ordinal, result)
 ├── Artifact(artifact_id, revision, content)
 ├── EnvironmentActivation / WorkspaceBinding / DefaultSelection
 │      └── Workspace Registry (자원 정체성은 독립)
 └── Latest PromptSnapshot(revision, covers_through_sequence)
```

1. **I-01:** RunRecord 1개는 LLM 호출 1회만 의미한다. Tool 실행과 다른 Run의 호출은 포함하지 않는다.
2. **I-02:** Conversation Event의 `sequence`는 대화 내 유일·단조 증가. 타임스탬프는 정렬 기준이 아니다.
3. **I-03:** 기록된 Event의 canonical payload와 Run의 수신/송신 Raw 본문은 사후 불변이다. 상태는 허용된 전이로만 바꾼다.
4. **I-04:** 모든 ToolExecution은 원래 Run의 call_id와 ordinal로 역추적 가능하다. 같은 Run/call_id를 두 번 실행 성공 처리하지 않는다.
5. **I-05:** Conversation에 ToolResult가 없어도 Execution Records의 결과/위치로 필요한 Native Provider Tool 그룹을 재구성해야 한다. 불가능하면 실패를 명시적으로 반환하고 **조용히 누락/가짜 결과 생성 금지**.
6. **I-06:** Prompt Snapshot은 Conversation당 최신 1개이고, 압축 범위/cursor는 이미 보존된 순서 및 완전한 Tool 그룹과 일치해야 한다.
7. **I-07:** Run의 Trigger는 기존 Event/Execution 참조다. 다음 Run을 위해 원본을 이동·복제하지 않는다.
8. **I-08:** Conversation 삭제/비밀 자료 접근은 별도 정책으로 관리한다. 이 문서의 불변은 정상 생애 중 무단 변경 금지이지 영구 삭제 금지가 아니다.
9. **I-09:** 활성 Workspace/Environment의 authoritative 저장 영역은 Artifact가 아니라 Registry/Activation/Binding/Settings다. 마지막 PromptSnapshot이나 UI projection은 권한 원본이 아니다.
10. **I-10:** 같은 Conversation에서 Control의 **실행 흐름은 하나만 활성**이고, LLM Run은 동시에 하나만 실행한다. 다만 그 실행 흐름 내 ToolCall 여러 개는 Scheduler가 독립성을 검증하면 병렬로 실행될 수 있다. 다른 Conversation의 실행 흐름은 병렬 가능하다. 구체적인 동기화/lease 메커니즘은 미결정.

## 3. 영속화 및 원자성 경계 (제안)

### 3.1 사용자 입력 → Provider 응답

```text
User input received
  ├── T1: append exact User ConversationEvent
  ↓
Prompt builds request at stable read boundary
  ├── T2: create RunRecord(raw_request, trigger_refs, input basis) BEFORE send
  ↓
Provider call (external; T2와 원자 트랜잭션 불가능)
  ├── T3: persist raw_response or typed failure/unknown
  ├── T4: normalize response + append Assistant output / call group
  ↓
If tool calls: persist pending ToolExecutionRecords BEFORE tool execution
```

- T3/T4는 별도 트랜잭션일 수 있으나 **T3의 성공 이후 정규화 실패를 복구할 수 있어야** 한다.
- Call group Event와 그 pending ToolExecutionRecords는 **동일 논리 결과에 기반하고 누락 없이 결합**한다(하나의 DB 트랜잭션 권고).
- Provider 요청을 기록하는 것과 실제 네트워크 송신은 하나의 DB 트랜잭션으로 보장할 수 없다. crash/retry 정책은 Control + Provider 계약에 남긴다.
- **확정:** 같은 Conversation에서는 Run/Control 실행 흐름을 직렬화한다. 한 Run이 ToolCalls를 낸 후 여러 Tool이 병렬 실행되는 동안에도 동일 Conversation에서 다른 Run 흐름을 임의로 시작하지 않는다. 이를 구현하는 lease/lock/queue와 중단 후 소유권 회복 방식은 별도 설계한다.

### 3.2 Internal Tool의 Artifact 변경

```text
Internal Tool computes intended mutation(expected revision)
  ↓
Data atomic commit:
   check (run_id, call_id) not already committed
   compare artifact revision
   update artifact
   append ToolResult execution record / finish state
   append exact Conversation ToolResult Event if configured
  ↓
Committed result returned to Control
```

- **Artifact 변경과 완료 결과 기록을 함께 commit**하는 복합 Data 명령을 권고한다. Tool 구현이 먼저 Artifact를 따로 저장하고 이후 결과만 쓰면 중단 시 불일치할 수 있다.
- ToolCall ID를 멱등성 식별자로 사용하되, 중복 충돌과 이미 완료된 동일 요청을 분리하여 반환한다.
- External Tool은 실제 파일/프로세스/네트워크 변경에 Data DB 트랜잭션을 적용할 수 없다. **실행 의도 선기록 + 실제 완료 결과 기록 + 결과 불명 처리**를 따로 규정한다.

### 3.3 Snapshot 갱신

- Prompt 모듈이 새 **전체 Prompt 구성**과 압축 대상의 **완전한 기록 prefix**를 결정한다.
- Data는 `covers_through_sequence`의 유효성, 적절한 Snapshot revision 및 조회 기준의 변화를 비교한다.
- 정상 커밋 시 같은 Conversation의 **기존 Snapshot을 원자적으로 대체**한다. 전체 구성 입력과 그에 대응하는 coverage 및 참조 정보가 일관되어야 한다.
- 압축 중 추가된 Conversation Event를 무조건 삭제/덮어쓰지 않으며 다음 Prompt는 갱신된 Snapshot의 cursor 이후 exact tail을 읽는다.

## 4. Data 공개 Port (함수명은 예시)

| Port 기능 | 예상 입력 | 보장되는 결과 |
| --- | --- | --- |
| `append_event` | conversation + exact event + causal refs | immutable event ID 및 monotonic sequence |
| `read_events` | conversation + range/cursor | ordered exact events |
| `prepare_run` | conversation + trigger refs + raw request + prompt basis | persist-before-send Run ID |
| `record_run_outcome` | run ID + raw response 또는 typed failure | irreversible raw outcome 및 상태 전이 |
| `commit_model_output` | run ID + normalized assistant output + ordered calls | assistant event와 pending tool executions의 정합성 |
| `commit_tool_outcome` | execution ID + actual/model result + optional Conversation event | 결과 및 연결 관계 원자 커밋 |
| `commit_internal_tool_mutation` | execution + artifact CAS operations + model result + event policy | Artifact / Tool execution / Conversation all-or-none |
| `read_prompt_basis` | conversation + cursor/boundary + needed artifact refs | 동일한 읽기 기준으로 coherent context input |
| `read_current_state` (개념적 조회 계약) | conversation + active environment scope | Data의 Registry/Binding/Settings/Artifact 버전과 접근 가능한 상태. Live 관측은 Environment 계약에서 별도 합성 |
| `get_latest_snapshot` / `replace_latest_snapshot` | conversation + expected revision + coverage + derived content | 최신 1개, conflict 시 명시적 실패 |
| `get_artifact` / `list_artifacts` | conversation + type/id | current revision + data |
| `get_workspace_binding` | environment instance + conversation scope + binding | 접근 관계만 조회; **실제 실행 권한 판정은 Environment** |

Port의 소비자는 DTO/오류 의미만 알아야 하며 내부 SQL·테이블·저장 경로는 알아서는 안 된다.

## 5. 오류 및 실패 시나리오

| 상황 | Data가 보장할 동작 |
| --- | --- |
| Run 준비 후 provider 송신 전에 crash | 요청 기록 존재; 송신 여부를 확인할 수 없으면 미확인 상태. 무조건 자동 재송신하지 않음 |
| Provider 응답 저장 후 정규화 오류 | Raw 응답 유지; 정상 Assistant Event를 날조하지 않고 오류 추적 가능 |
| 두 Tool이 같은 Artifact revision을 수정 | 하나만 성공, 나머지는 CAS Conflict |
| Artifact 수정 직후 ToolResult 영속화 실패 | 같은 Data transaction이면 전체 rollback |
| 외부 명령 성공 직후 DB 기록 실패 | 외부 부작용은 지속할 수 있음; 확인 불명으로 보고 자동 재실행하지 않음 |
| Conversation에서 ToolResult를 숨김 | Execution 기록은 보존; 모델 재생용 참조가 남아야 함 |
| Snapshot cursor가 미완결 Tool 그룹을 포함 | Snapshot 갱신 거부 |
| 활성 Workspace Binding이 철회됨 | Data는 변경된 Binding을 반환; 실제 Tool 실행 Environment가 재검증 |
| Event와 Run 사이 부정합 참조 | 저장 거부, 기존 원본 변경하지 않음 |
| Raw provider 요청에 비밀 데이터 포함 | 권한 제한 저장 정책 필요; 단순 무분별 로깅 금지 |

## 6. 최소 Contract Test (제안)

1. User Event 삽입 시 exact payload 불변 + 순번 증가.
2. Raw request 준비 후 Provider 1회 호출, 수신 raw response 정확 보존.
3. Raw response 정상화 실패 시에도 raw를 재조회 가능.
4. 같은 Run의 2개 ToolCall 병렬 완료 순서가 달라도 ordinal/call_id가 정확함.
5. Conversation에 결과를 보존한 Memory Tool과 완료만 보존한 Plan Tool 각각의 native replay.
6. Internal Plan 변경/완료 기록 원자성 + 중복 call_id 방지 + CAS conflict.
7. Latest 전체 Prompt Snapshot 교체와 tail selection이 같은 Event를 중복/누락하지 않음.
8. Tool 그룹이 불완전할 때 provider request 재구성 실패 (가짜 결과 없이).
9. Fake Data 구현으로 Control/Prompt 코드 변경 없이 public port 준수.
10. 다른 Conversation/Environment에서 같은 Workspace를 참조해도 ID/Biding 참조 혼동 없음 (권한 집행은 Environment 테스트에서 확인).

## 7. 독립 리뷰 후 확정할 쟁점

| ID | 결정 필요 | 초기 추천 |
| --- | --- | --- |
| DC-01 | **확정: 이벤트 유형별 불변 canonical payload**. 구체 event kind enumeration / ToolCall 그룹 wire 표현은 미정 | ordered call group과 call_id 참조를 권고 |
| DC-02 | **확정: 동일 Conversation의 실행 흐름과 Run은 직렬화**. lease/queue와 재개 구현만 미정 | 한 흐름 내 독립 ToolCall 병렬 실행은 허용 |
| DC-03 | Raw Provider 요청/응답 저장의 실제 직렬화·비밀·stream 범위 | 원본 body exact, credential 제외, streaming 세부 형식 별도 |
| DC-04 | **확정: Prompt가 실행 기록에서 소스 선택 및 정합성 확인, Provider가 native wire 직렬화**. 부분 누락·unpaired 결과의 구체 실패 계약 미정 | 불완전 history는 fail closed |
| DC-05 | **최신 전체 Prompt Snapshot**의 저장 형식, exact coverage/Execution 연결/cursor 원자성 | 압축 + exact tail + current state를 포함한 가장 최근 구성 입력 1개; 완전한 Tool 그룹 경계로 coverage, CAS 갱신 |
| DC-06 | **확정: Plan/Note 원본은 Data conversation-scoped Artifact**. 삭제·Revision 이력과 Outside export 정책은 미정 | Outside는 원본과 별도 copy of record가 되지 않음 |
| DC-07 | Run/Tool 결과 불명, 재시도, 취소 및 recovery 상태 전이 | 모호한 외부 효과는 UNKNOWN; 자동 재실행하지 않음 |
| DC-08 | Public Port granularity와 모듈별 소유 DTO | 도메인별 작은 API 묶음, 내부 repository 직접 참조 불허 |
| DC-09 | **CurrentStateProjection DTO**의 소유 위치와 갱신 기준 | 원본은 Data Registry/Activation/Binding/Artifact + Environment의 live 관측, 조회모델은 Prompt 입력용 |
| DC-10 | 동일 Conversation의 **Control lease/queue, crash 후 회수 규칙** | Run 직렬화는 확정, 실행 방식은 별도 결정 |

**기존 확정 원칙 및 DC-01/02/04/06의 명시적으로 채택된 부분 외에**, 이 문서의 타입 이름·실제 DB 스키마·원자성 구현·동기화 메커니즘은 여전히 검토 초안이다.
