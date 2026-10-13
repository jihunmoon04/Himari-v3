# 모듈 경계 및 계약 (Module Boundaries)

> 상태: **아키텍처 원칙은 확정**, 구체적인 함수 시그니처·패키지명·계약 버전 정책은 제안/미결정.
>
> 목표는 **공개 계약을 유지하는 모듈 내부 변경이 다른 모듈의 수정을 요구하지 않는 것**이다. 장애가 전체 프로세스에 절대 영향을 미치지 않는 물리적 격리는 초기 목표가 아니다.
>
> **공개 계약 메타데이터 표준과 모듈별 Owner/Provider/Consumer/Source of Truth/선언 위치는 [통합 계약 기준서](contracts.md)에서 관리한다.**

## 1. 독립성의 정의

1. **구현 독립성(필수):** 다른 모듈은 구현 클래스·SQL·OS 핸들·어댑터 내부에 접근하지 않는다.
2. **행동 계약 호환성(필수):** 동일한 공개 계약을 만족하는 대체 구현으로 교체할 수 있다. 성공뿐 아니라 에러 분류, 취소, 권한 검사, 이벤트 순서 등도 계약의 일부다.
3. **장애 격리(후속):** 모듈별 프로세스 분리나 서비스 격리까지 보장하는 것은 아니다.

출력 모델·압축 알고리즘을 바꾸면 시스템이 계약상 올바르게 동작해도 모델의 생성 내용은 달라질 수 있다. **호환성 ≠ 응답 동일성**.

## 2. 모듈 경계

- **Control**: 각 작업의 흐름을 관리하고 Prompt → Provider 1회 → 응답 분기 → Tool 실행 → 다음 Run을 조정. question용 `ask_user` Tool을 등록한다(V3-044).
- **Prompt**: 얇은 `PromptContextLoader`가 Data 읽기 모델·Tool catalog를 모으고, **순수 함수 `compose(PromptInputs)`**가 provider-neutral 요청을 구성한다(V3-043). 자체 영속 저장·전송 금지.
- **Provider**: `ProviderAdapter` SPI(`prepare_native_request`/`invoke_prepared`/`normalize`/`describe_capabilities`)로 모델별 통신, 지원 기능 협상, 응답 정규화. Run 반복 정책 금지.
- **Tool**: `ToolHandler` SPI 정의, 카탈로그 취합, 인자 검증, 디스패치, 충돌 안전성이 입증된 다중 ToolCall의 스케줄링, **`ToolOutcome`의 원자 커밋**(V3-042). Tool 구현 자체는 Internal 모듈이나 Environment 패키지에 있다.
- **Environment**: `EnvironmentPlugin` SPI. 특정 Workspace 연결/접근 방식, 정책 판정, 실행기 내부 구현, **자기 Tool(`tools()`) 제공**(V3-041). 다른 Environment의 구현을 직접 가져오지 않음.
- **Data**: Execution Records(원본 Provider 요청·응답 포함), Conversation Events, Artifact, Registry, Binding 및 최신 Prompt Snapshot의 저장/조회 계약. **공개 Port는 `ConversationLog`/`ArtifactStore`/`WorkspaceState`/`StateQuery`/(P2)`ControlState`로 분리**(V3-039). Prompt 정책이나 실제 Tool 실행 금지.
- **Workspace**: 독립 자원/식별 개념. **별도 소프트웨어 모듈을 만들지 않음**; Data Registry와 Environment 연결 계약으로 관리.
- **Memory Tool (후속)**: Data 조회/수정 계약을 사용하고 Tool 인터페이스에 결과 제공. 특수 직접 결합 금지.

## 3. 허용하는 의존성의 성격

```text
Composition Root (구현 조립/의존성 주입 + 플러그인 등록)
   ├── Control      → Prompt Port, ProviderAdapter, Tool Port, Data.ConversationLog
   ├── Prompt       → (loader만) Data.ConversationLog / StateQuery, Tool Catalog, optional Env observe
   │                  compose() 는 의존성 없음
   ├── Tool         → ToolHandler SPI 소유, Dispatcher → Data.ConversationLog(+ArtifactStore) 커밋
   │                  Scheduler → verified resource conflict metadata, never command-string equality alone
   ├── Internal Tools (memory/plan/note) → Tool SPI, Data.ArtifactStore 읽기
   ├── Environment  → Tool SPI(구현해서 제공), Data.WorkspaceState
   ├── Provider     → provider-specific SDK/adapter
   └── Data         → persistence backend

의존 방향: 플러그인(Environment 구현·Internal Tool·Provider Adapter) → 소유 모듈의 api.py.
소유 모듈은 플러그인 구현을 import하지 않는다. 연결은 Composition Root에서만.

공유: 공개된 작은 타입·프로토콜·에러 의미
금지: 다른 모듈의 private 구현, Repository/SQL 직접 호출, 역방향 호출·순환 import
```

모듈은 상대 모듈의 **public port**를 의존성 주입받는다. **Port는 Owner 모듈의 `api.py`에 `typing.Protocol` + frozen dataclass로 선언하고, 코드가 생기면 그것이 계약의 원본이다(V3-046).** 거대한 하나의 공용 DTO 파일에 모든 내부 모델을 밀어 넣지 않는다.

Data를 통한 간접 결합에도 주의한다. 테이블/컬럼을 공개 계약으로 삼지 않고, 의미가 안정적인 읽기 모델/명령 API만 노출한다.

## 4. 기본 계약에 포함할 항목

모든 public port는 구현 전 다음을 정의해야 한다.

| 항목 | 검증해야 할 내용 |
| --- | --- |
| 식별자·스코프 | conversation_id, run_id, workspace_id, binding_id의 의미와 참조 권한 |
| 입력·출력 | 불변 DTO, 필수 필드, 지원하지 않는 기능의 명시적 실패 |
| 오류 | 도구 실패 vs 전송 실패 vs 권한 실패 vs 취소 구분 |
| 순서 | 원본 기록 시점, ToolCall → ToolResult 순서, terminal 처리 |
| 부작용 | 파일 변경 등 외부 효과, 재호출 가능 여부, 중복 실행 금지 |
| 취소·타임아웃 | 취소 후 결과·잔여 프로세스·다음 Run 처리 |
| 권한 | ID는 참조일 뿐 증명이 아님. 실행 시 실제 Conversation·Environment·Binding 권한 검증 |
| 버전 호환 | 계약 변경 시 변경 지점과 소비자 호환 테스트 |

필요한 경우에만 프로토콜 확장을 사용하고, 초기에 불필요한 plugin 시스템·마이크로서비스·분산 이벤트 버스는 넣지 않는다.

## 5. 테스트로 경계 강제

- **의존성 규칙:** import-linter(또는 동등 도구)로 import graph를 검사해 다른 모듈 private 심볼(`api.py` 외)/역방향 참조/순환 의존을 차단. 첫 Skeleton부터 CI에 넣는다(V3-046).
- **제공자 계약 테스트:** 모든 Provider Adapter가 동일한 공통 성공/오류/취소 계약에 맞게 정규화되는지 검증.
- **Environment 계약 테스트:** Fake Workspace에 연결한 Inside 구현에서 권한, 실패, 취소, 작업 결과를 검증.
- **Data 계약 테스트:** 저장 구현을 교체해도 동일한 exact history, snapshot, registry 조회 의미를 유지.
- **Control 통합 테스트:** Fake Prompt/Provider/Tool/Data로 final, question, tool-result→next run, tool failure 경로를 검증.
- **Prompt 계약 테스트:** Stable Prefix → exact Conversation → Current State 순서, Trigger 무복제, native ToolCall/Result 그룹 인접성과 Provider별 합법적인 state/continuation 직렬화를 검증.
- **Scheduler 교체 테스트:** 동일 Workspace 명령 충돌, 서로 다른 검증된 파일 수정, 다른 Conversation의 동일 자원 충돌을 검증.
- **교체 테스트:** 모듈의 구체 구현을 최소 Fake/대체 어댑터로 바꿔도 나머지 모듈 코드 수정 없이 핵심 경로가 작동하는지 검증.
- **SPI 적합성 테스트:** `ToolHandler`·`EnvironmentPlugin`·`ProviderAdapter`마다 재사용 가능한 conformance suite를 두고, 새 플러그인은 그 suite만 통과하면 등록 가능하게 한다(V3-040).

테스트는 공통 contract suite를 구현체별로 재사용한다. v2처럼 검증을 위한 중복 계층과 과도한 전체 테스트를 늘리지 않는 것을 목표로 한다.

**확정된 pytest 실행 규칙(V3-038):** 모듈의 공개 계약이 추가·변경·제거되는 경우에만 **전체 pytest**를 실행한다. 그렇지 않으면 **수정된 모듈의 pytest만** 실행한다(복수 모듈이면 각각). 공개 계약에는 API 시그니처뿐 아니라 DTO, 오류, 권한/격리, 순서·취소·부작용 등의 보장도 포함된다. Sandbox 실호스트 검증은 별도의 필수 보안 게이트이며 full pytest 여부와는 독립이다. [Testing Policy](testing-policy.md) 참조.

## 6. 예시 디렉터리 구조 (제안 — 아직 미생성)

```text
src/himari/
    control/        api.py   # drive/resume, ask_user ToolHandler
    data/           api.py   # ConversationLog, ArtifactStore, WorkspaceState, StateQuery (P2 ControlState)
    prompt/         api.py   # PromptInputs, compose(), loader
    provider/       api.py   # ProviderAdapter SPI
        openai/              # Adapter 구현 (플러그인)
    tool/           api.py   # ToolHandler SPI, ToolOutcome, Dispatcher/Scheduler
        internal/            # memory/plan/note ToolHandler (플러그인)
    environment/    api.py   # EnvironmentPlugin SPI
        inside/              # Inside 구현 + run_command/파일 ToolHandler + sandbox (플러그인)
    # workspace/ 전용 패키지 없음: Data.WorkspaceState + EnvironmentPlugin 사용
    # contracts/ 공용 패키지 없음: 각 Owner의 api.py
    bootstrap/               # Composition Root
```

정확한 경로와 패키지 경계는 첫 최소 실행 경로를 구현하기 전에 확정한다. 문서를 작성했다고 코드 Skeleton이 구현된 것은 아니다.
