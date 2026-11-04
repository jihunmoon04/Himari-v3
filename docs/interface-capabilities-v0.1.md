# 인터페이스 능력과 반려 파일 수신 (V3-P93 설계 초안 v0.1)

> 2026-11-04 · **설계 병합 / 1단계 구현**, rev. 3 · 설계 기준 `18e945a`(#179 병합).
>
> 소유자 요청: 반려 대화에서 파일을 받을 수 없는 현재 제한을 모델에게 알리고,
> 이후 Windows 파일 수신을 지원한다. 시스템 프롬프트에는 해석 규칙을 고정하고
> 현재 상태는 runtime state로 전달해 인터페이스 전환에 따른 접두부 변경을 줄인다.
> Workspace 탐색은 후속 범위로 둔다. 1단계는 턴 스냅샷과 프롬프트를 구현한다.
> Windows 파일 API·연결 세대 협상·저장 UI는 2단계 예정이며 아직 없다.

관련: [반려 앱 V3-P83](companion-v0.1.md), [파일 V3-P66](file-upload-download-v0.1.md),
[프롬프트 정비 V3-P89](prompt-cleanup-v0.1.md), [Current State](current-state-contract-v0.1.md),
[Prompt Composition](prompt-composition.md), [테스트 정책](testing-policy.md).

## 1. 현재 문제와 목표

현재 `SPEAKING_RULE`은 기기 발화에서 긴 자료를 파일로 주거나 Web에서 이어가라고 한다.
문서 생성 안내도 `present_file`을 권하고, `PresentFile.description`은 대화에 다운로드
카드가 생긴다고 설명한다. 하지만 파일 다운로드 경로는 `principal()`을 사용하므로
기기 토큰을 403 `device_scope`로 거부하며 Windows 앱에는 파일 카드/저장 기능이 없다.

이미 있는 기반은 다음과 같다.

- `UserMessage.origin`, `Wake.device_id`, `TurnSource`: 이번 턴의 출처와 기기.
- `presence.sent_from`: 모델에게 보이는 출처. 현재 활동 기기와는 별도다.
- `inside/present_file`, `machine/present_file`: 파일의 당시 바이트를 불변 보관소에 저장.
- `ToolResult.presented_files`, `FileRecord(file_id, version)`: 대화에 귀속된 파일 기록.
- `GET /api/conversations/{id}/events`: 기기도 읽으며 `presented_files` 메타데이터를 포함.
- Stable Prefix → Conversation → runtime state 순서. OpenRouter 캐시 표시는 상태보다 앞이다.

목표는 **모델의 파일 작업 능력과 사용자가 현재 화면에서 받을 수 있는 능력을 구분**하는
것이다. 모델의 Workspace가 있어도 앱의 Workspace 탐색 UI는 없을 수 있다.

## 2. 범위와 전환의 의미

| 이번 설계에 포함 | 후속 범위 |
| --- | --- |
| 인터페이스 능력의 신뢰 경계·턴별 상태·복구 | Windows Workspace/서버 파일 탐색기 |
| 고정 프롬프트와 캐시 검증 기준 | Windows 임의 파일 업로드·편집·동기화 |
| 기존 파일 제공 도구를 이용한 Windows 파일 카드·저장 | 공개 다운로드 링크·외부 공유 |
| 기기 토큰의 제한된 제공 파일 조회·다운로드 | 자동 저장·자동 실행·서버에 저장 완료 영수증 기록 |

이 문서의 **인터페이스 전환**은 반려 기능이 켜진 같은 서버/대화에서 Web·CLI·Windows가
번갈아 메시지를 보내는 경우다. `HIMARI_COMPANION`은 기기 인증/기능을 허용하는 서버
설정이며 이번 턴의 UI 선택과 다르다. 인터페이스를 바꿨다는 이유로 이 설정을 바꾸지 않는다.

V3-P89의 persona 범위 A(반려 기능이 켜져 있으면 모든 대화)는 유지한다. Web↔Windows
전환만으로 persona를 넣고 빼지 않는다. 서버 반려 기능 자체를 끄고 켜는 설정 변경,
persona 편집, 모델 변경까지 캐시가 유지된다는 보장은 이 설계의 범위가 아니다.
전역 스위치를 꺼도 persona를 공급자에게 계속 보내는 변경은 제안하지 않는다.

## 3. runtime state의 인터페이스

### 3.1 모델에게 보이는 꼴

```json
{
  "interface": {
    "kind": "windows_companion",
    "capabilities": {
      "file_download": false,
      "workspace_browser": false
    }
  }
}
```

- `kind`: `web`, `cli`, `windows_companion`, `unknown`. 현재의 기기 메시지/wake는
  Windows 반려 인터페이스로 매핑한다. 다른 기기 인터페이스를 지원할 때는 명시적으로 확장한다.
- `file_download`: 이 인터페이스가 허용된 대화 파일을 내려받는 기능을 지원한다는 뜻이다.
  특정 파일이 존재함/만료되지 않음/이미 저장됨을 뜻하지 않는다.
- `workspace_browser`: 사용자가 이 인터페이스로 작업 파일을 탐색할 수 있다는 뜻이다.
  모델의 Workspace Binding, 파일 Tool의 사용 가능 여부나 실행 권한을 뜻하지 않는다.
- URL, 토큰, 로컬 저장 경로, 문장 형태의 지시, 파일별 전달 상태는 이 객체에 넣지 않는다.
  기기 ID는 기존 원본 출처에 남기며 이 모델 표시 객체에 중복하지 않는다.

기본 정책은 다음과 같다. `true`는 해당 기능과 인증 권한을 서버가 실제로 제공할 때만 가능하다.

| 접수한 인터페이스 | 1단계 `file_download` | 2단계 `file_download` | `workspace_browser` |
| --- | --- | --- | --- |
| 서버가 지원하는 Web | 파일 서비스/인증 허용 시 true | 동일 | Web 탐색기와 권한이 있을 때만 true |
| 서버가 지원하는 CLI | 기존 download 명령/서비스 허용 시 true | 동일 | 이번 범위에서는 false |
| Windows 앱 | false | 서버·앱 협상과 권한을 모두 만족하면 true | false |
| 출처/지원 여부 불명 | false | false | false |

기존 기록의 출처가 없다고 Web으로 추정하거나, 다른 접속 기기의 기능을 빌려 true로 만들지 않는다.

### 3.2 값의 원본과 소유권

유효 능력은 **서버 기능 ∩ 클라이언트 지원 ∩ 인증 권한**이다.

- Web/CLI는 서버가 지원하는 각 인터페이스의 기본 계약과 해당 요청의 인증으로 결정한다.
- Windows 사용자 입력은 그 요청의 `client_features` 선언과 현재 연결 세대의 일치를 검사하고,
  wake는 해당 기기의 **현재 Subscription에 귀속된** 확인 값을 사용한다(§6.1).
  `device_id`만 같은 지연 요청은 현재 세대의 선언으로 취급하지 않는다.
  앱의 선언은 지원 여부의 입력일 뿐 권한이 아니다.
- `kind`와 기기 선택은 인증된 `origin` 또는 wake의 기기로 서버가 정한다. 메시지 본문,
  모델 인자, 활동 중인 앱 이름, `presence.current_device`로 바꾸지 않는다.
- Bootstrap이 인증/기기 협상을 합성하고 Control에 `InterfaceSource`(제안)를 제공한다.
  Prompt는 결과를 읽기 전용으로 표시하며 presence 내부 구현을 참조하지 않는다.
- 모델 능력인 `ProviderCapabilities`에 UI 능력을 추가하지 않는다. Tool/Environment는
  Bootstrap이나 Windows 구현을 import하지 않는다.

### 3.3 턴 시작, 저장, 재개

1. 입력 접수 또는 wake 승인 시 서버가 인터페이스 스냅샷을 만든다. 인증·권한 검사를 먼저 한다.
2. 스냅샷은 원본 `UserMessage`/`Wake`와 원자적으로 저장한다. Data 소유의 frozen DTO
   `InterfaceSnapshot(version=1, kind, capabilities)`와 두 payload의
   `interface: InterfaceSnapshot | None = None` 선택 필드를 제안한다.
   HTTP 본문에 이 서버 소유 필드를 직접 받지 않는다.
3. Control은 이 스냅샷을 턴의 모든 Run에 고정하고 Prompt의 `PromptInputs.interface`와
   `CurrentStateProjection.interface`로 투영한다. 스냅샷 종류와 origin/wake 기기의 매핑이
   맞지 않으면 접수를 거부하며 임의로 다른 기기를 택하지 않는다.
   Tool 라운드의 재개·Run 한도·서버 재시작·압축 뒤에도 같은 출처를 복원한다.
4. 새 사용자 메시지나 `ask_user` 답변을 다른 인터페이스에서 접수하면 새 턴에는 새 스냅샷을 쓴다.
   입력 없이 기존 턴을 resume한 요청은 접속한 화면만으로 전달 대상을 바꾸지 않는다.
5. 옛 기록에 스냅샷이 없으면 저장된 origin/wake로 종류만 복원하고 능력은 false로 둔다.
   현재 활성 기기나 과거 대화 문장으로 능력을 추측하지 않는다.

복원 기준은 **해당 턴을 연 원본 입력 이벤트의 sequence**다. 일반 입력과 `answers_call`이
있는 `UserMessage`를 모두 포함해, 처리할 턴의 경계 안에서 가장 최근에 접수한 입력을 택한다.
서버가 연 턴은 그 턴의 `Wake` 이벤트를 기준으로 한다. 뒤에 열린 다른 턴의 입력을 빌리지 않는다.
이 기준 이벤트의 origin과 interface를 한 쌍으로 읽고, 같은 턴의 ToolGroup/압축 뒤에도 유지한다.
Composer의 `UserTurn` 목록이나 화면에 보이는 마지막 사용자 말풍선을 역탐색하지 않는다.
`ask_user` 답변은 Tool 그룹 결과 안에 렌더링되더라도 원본 Data의 입력 sequence로 선택한다.

1단계는 두 Data 백엔드의 입력·wake 트랜잭션에 선택 필드를 함께 기록한다.
SQLite payload JSON에만 추가하며 테이블·`payload_schema_version`·DB v18은 유지한다.
옛 기록의 선택 필드 누락은 `None`으로 읽는다. 병합 기준 `18e945a`의 실제 코덱으로 새
`UserMessage`/`Wake`를 읽어 추가 메타데이터를 무시하는 롤백 읽기 호환도 확인했다.
구버전 서버는 이 스냅샷을 활용하지 않으므로 새 인터페이스 설명은 제공하지 않는다.
동시 Web/기기 턴의 스냅샷은 대화/턴별로 격리한다.

스냅샷은 접수 당시의 설명이다. 도중에 토큰이 철회되거나 서비스가 꺼져도 실행 권한을
보존해 주지 않는다. 실제 파일 조회·다운로드는 매 요청의 현재 권한을 다시 검사한다.

## 4. 시스템 프롬프트와 캐시 경계

### 4.1 고정된 해석 규칙

인터페이스 능력의 의미와 다음 조건부 규칙은 시스템 프롬프트에 고정한다.

> 현재 답변 화면과 지원 기능은 runtime_state.interface를 따른다. 기능이 불명확하면
> 지원한다고 추정하지 않는다. file_download가 false이면 이 화면에서 파일을 받을 수
> 있다고 말하지 않는다. 파일을 대화에 준비한 것과 사용자가 로컬에 저장한 것을 구분한다.
> 필요하면 같은 대화를 Web에서 열어 받을 수 있다고 안내한다. 실제 도구 성공 없이
> 파일을 준비했다고 말하지 않는다.

Web/일반 답변과 반려 말풍선의 표현 규칙은 함께 설명하고 현재 종류로 선택한다.
사용자 정의 지시문/persona보다 상위인 능력 제한을 유지한다. JSON 상태에는 사실만 넣고
새 지시문을 주입하지 않는다. 기존 `SPEAKING_RULE` 및 문서 생성 안내의 무조건적인
파일 수신 약속도 이 규칙에 맞춘다.

### 4.2 같은 인터페이스 전환이 바꾸지 않는 것

같은 모델·공급자·서버 설정·persona·도구 설정을 고정한 경우:

- Web↔Windows 전환은 시스템 텍스트, 도구 ID/설명/스키마/순서를 바꾸지 않는다.
- `interface`는 Conversation 뒤의 runtime state에만 들어간다. 과거 메시지나 도구 결과를
  현재 인터페이스에 맞춰 다시 쓰지 않는다. 과거 스냅샷을 대화 본문에 반복해서 그리지 않는다.
- 기존 `present_file` 도구는 인터페이스별로 켜고 끄지 않는다. 실제 서버 기능·모델 능력·권한이
  바뀌어 도구 구성이 달라지는 것은 별개이며 캐시를 위해 기능을 과장하지 않는다.
- `interface`는 상태 크기/측정에 반영하고 `state_key`에 포함한다. 인터페이스 값 자체를
  `context_key`의 고정 접두부 구성 요소로 추가하지 않는다. 기존 키의 다른 구성은 유지한다.
- 기존 압축·기억 추출 프롬프트는 바꾸지 않는다. 새 스냅샷 메타데이터를 과거 대화나
  요약/추출 입력에 별도 문장으로 추가하지 않으며, 요약·메모리를 현재 능력의 원본으로
  읽지 않는다. 현재 상태는 Data의 턴 스냅샷에서 복구한다.

### 4.3 검증 범위

실제 캐시 손실 원인을 시스템 프롬프트 하나로 미리 단정하지 않는다. 구현 전후 동일한
조건에서 `model_id`, 공급자, 시스템 직렬화 해시, 도구 카탈로그 해시와
`cached_input_tokens`/입력 토큰 수를 비교한다. 해시만 남기며 persona·토큰을 로그에 풀지 않는다.

오프라인 기준은 **interface만 바꾼 요청에서 시스템/도구 바이트가 같고, 상태와 state_key는
달라지는 것**이다. Provider의 캐시 표시는 상태보다 앞에 유지한다. 캐시 적중률의 수치는
공급자 라우팅·이력·시간·모델 변경에도 좌우되므로 100% 적중이나 모델 간 공유를 약속하지 않는다.
실제 API 비교를 실행하지 않았으면 오프라인 불변성 검증과 구분해 미실행으로 보고한다.

## 5. 파일 제공은 기존 도구 하나의 의미를 유지

`inside/present_file`과 `machine/present_file`은 각각 기존 Environment 파일 제공 도구로
유지한다. Windows 전용 복제 도구나 모드별 도구 on/off는 추가하지 않는다.

```text
Workspace의 파일
  → 기존 present_file
  → 불변 보관본 + 대화의 FileRecord(file_id, version) + ToolResult.presented_files
  → Web 다운로드 카드 / 지원하는 Windows 앱의 저장 카드
```

- 도구 성공은 **그 버전의 파일을 대화에서 받을 수 있도록 준비했다**는 뜻이다. 카드가 실제로
  표시됐거나 다운로드/로컬 저장이 끝났다는 뜻은 아니다. 모델 결과 문구와 설명도 이 의미로 고친다.
- `file_download=false`인 앱에서도 사용자가 원하면 파일을 준비하고 Web 수신 경로를 안내할 수 있다.
  파일 생성/Workspace 작업 자체를 금지하는 플래그가 아니다.
- 바이트와 FileRecord/ToolResult의 원자성·버전·용량·만료·대화 삭제는 V3-P66을 따른다.
- 특정 기기로 파일 바이트를 자동 전송하거나 자동 저장하지 않는다. 모델이 받은 출처는 이번 턴의
  응답 안내 기준이며, 파일 소유권은 계속 대화/주체 기준이다.
- 기존 wake/읽기 전용 Tool 제한을 UI 능력으로 확장하지 않는다.

## 6. Windows 수신 최소 범위

### 6.1 협상과 점진 배포

#### 6.1.1 연결 세대의 원본과 수명

현재 `PresenceHub.subscribe()`는 같은 기기의 SSE를 `replaced` 처리하지만,
`POST /api/device/presence`는 기기 토큰과 device_id만으로 `signal()`을 호출한다.
따라서 현재 API 그대로는 요청이 어느 연결에서 생성됐는지 확인할 수 없다.
다음 **연결 세대 계약을 새로 구현**해야 한다.

- 기기 SSE의 `subscribe()`가 성공할 때마다 서버가 `connection_generation`을 새로 만든다.
  적어도 128비트의 예측 불가능한 임의 식별자로, 단순한 기기별 카운터나 `signals_revision`이 아니다.
  서버 전체의 `signals_epoch`도 연결 교체를 구분하지 못하므로 대용하지 않는다.
  재연결·5분 스트림 갱신·같은 토큰의 앱 교체·서버 재시작에도 재사용하지 않는다.
- 이 값과 `file_download` 확인 상태는 `(principal, device_id, Subscription 객체)`에 귀속한다.
  새 Subscription을 현재 연결로 교체하는 것과 확인 값을 **즉시 false로 초기화**하는 것은
  하나의 Hub 임계 구간에서 일어난다. 이전 연결의 `_Device` 기능 값을 복사하지 않는다.
- 이 연결에만 보내는 기기 SSE `hello.connection_generation`으로 식별자를 전달한다.
  `Subscription`의 읽기 전용 속성으로 API가 그 연결의 값을 얻도록 EventHub 계약을 확장한다.
  hello 작성 중 다른 연결이 들어와도 다른 Subscription의 식별자를 대신 넣지 않는다.
- `GET /api/device`와 공통 `device_state()`에는 현재 세대 값을 공개하지 않는다.
  Web hello·주체 전체 방송·다른 기기에도 보내지 않는다. 원본 기기 토큰의 인증을 대신하거나
  다운로드 권한을 부여하는 값은 아니며, 모델 runtime state나 Data 턴 스냅샷에도 넣지 않는다.
- 현재 Subscription이 닫힘/철회/교체/큐 초과/서버 종료로 끝나면 그 세대는 즉시 무효이고
  wake용 능력은 false다. 일반 presence의 `REOPEN_SECONDS` 유예로 true를 유지하지 않는다.
  오래된 Subscription의 늦은 unsubscribe/finally는 현재 객체·세대와 일치할 때만 정리한다.
  즉, 교체된 연결의 정리가 새 연결의 false/true 상태를 지워서는 안 된다.

#### 6.1.2 HTTP 요청의 귀속

서버는 `GET /api/device` 및 hello의 `features.file_download: 1`과
`features.connection_generation: 1`을 함께 알린다(제안). 새 앱은 두 표시와 자신의 기기
hello를 확인한 뒤에만 다음 요청을 보낸다.

```text
POST /api/device/presence
Authorization: Bearer <device token>
X-Himari-Connection-Generation: <이 SSE hello에서 받은 값>

{..., "features": {"file_download": true}}
```

메시지/질문 답변의 선택 필드 `client_features: {"file_download": true}`도 같은 헤더로 묶는다.
서버는 클라이언트 선언과 서버 소유 `interface`를 구분하고 권한 교집합을 적용한다.
아래 표는 기기 Caller의 presence 및 메시지/질문 답변 접수에 적용한다.

| 요청 | 처리 |
| --- | --- |
| 세대 헤더가 있고 현재 활성 Subscription과 일치 | 정상 처리. presence의 파일 선언은 그 Subscription에만 기록하고, 입력의 능력은 그 요청의 client_features로 결정 |
| 세대 헤더가 있으나 이전/종료/다른 기기 세대이거나 현재 연결이 없음 | 409 `stale_connection_generation`. 일반 신호·파일 능력·입력 이벤트를 기록하기 전에 거부 |
| 헤더 없이 새 file_download 선언 또는 client_features 필드를 보냄 | 409 `connection_generation_required`. 기능 확인이나 입력 기록을 하지 않음 |
| 헤더도 새 기능 선언도 없는 구형 presence | 기존 활동 신호만 처리. 현재 Subscription의 새 파일 능력을 true로도 false로도 변경하지 않음 |
| 새 필드 없는 구형 메시지/질문 답변 | 기존 텍스트 입력 허용. 그 입력의 파일 능력은 false이며 이전 presence의 true를 빌리지 않음 |

세대 오류에는 현재 식별자를 돌려주지 않는다. 토큰 인증은 먼저 수행하며 기존 401/403을 유지한다.
새 선언의 존재는 실제 전송 필드로 판단하며, 파서가 보충한 기본 false를 구형 앱의 선언으로 오인하지 않는다.
일치하는 세대의 presence에서 file_download가 빠지면 그 세대의 파일 확인 값은 false다.
이전 세대의 true뿐 아니라 false도 새 세대를 덮어쓸 수 없다. 구형 신호의 생략 필드를
기존 전체-snapshot 처리에 섞어 새 파일 값을 기본 false로 덮어쓰는 구현도 금지한다.
이 문서는 구형 활동/화면 신호 전체를 세대 프로토콜로 소급 변경하지 않는다.

인증 후 세대 검증과 기능 적용은 한 Hub 임계 구간에서 처리하고 그 사이에 await하지 않는다.
기기 ID만 검사한 뒤 나중에 현재 세대에 쓰는 두 단계 처리는 허용하지 않는다.
입력/wake 스냅샷도 최종 접수 검사에서 현재 Subscription과 세대를 한 번에 읽는다.
새 선언을 포함한 입력은 HTTP 진입 검사와 별개로 이 시점에도 요청 세대를 재검증한다.
일치할 때 입력은 그 요청의 client_features를, wake는 현재 세대의 presence 확인 값을 사용한다.
이 캡처가 턴 시작 상태의 기준이며 원본 입력/wake와 원자적으로 기록한다. 캡처 이후의
연결 교체가 이미 접수한 스냅샷을 소급 변경하지는 않지만, 다음 새 턴은 새 세대를 읽는다.

#### 6.1.3 앱과 서버의 교체 처리

- Presence 루프는 HTTP 작업을 만들 때 `(ServerClient/기기, SSE 세대, 신호 본문)`을 함께
  캡처한다. 재연결 후 오래된 본문에 새 헤더를 뒤늦게 붙이지 않는다.
- SSE가 끝나거나 replaced를 받으면 로컬 세대와 확인 상태를 비운다. 다음 hello 전에는
  새 파일 능력 선언을 보내지 않는다. 새 세대를 받으면 새 presence 전체 선언을 즉시 예약한다.
- 오래된 HTTP 응답/409는 그 요청의 세대에만 적용한다. 새 hello를 이미 받은 상태에서
  이전 요청의 실패가 돌아와도 새 세대나 전송 스케줄을 지우지 않는다.
- 세대 오류로 거부된 메시지는 미접수로 처리해 초안을 보존한다. 다른 세대로 자동 재전송하지
  않는다. 기존 `replaced`의 종료 정책을 따르며 다른 연결의 세대를 조회해 가로채지 않는다.
- 구형 서버에는 헤더·새 presence/메시지 필드를 보내지 않는다. 새 서버의 구형 앱은
  일반 활동/텍스트 기능을 유지하지만 새 세대의 파일 능력을 확인할 수 없어 false다.
  Web/CLI에는 기기 연결 세대를 요구하지 않는다.

서버는 현재 세대에 유효한 선언이 없으면 wake의 파일 능력을 false로 둔다.
파일 API 인증은 별도로 매번 수행한다. 세대가 일치해도 권한이 늘어나지 않으며, 앱 업데이트나
새 협상이 이미 접수된 턴의 스냅샷을 소급 변경하지 않는다.

### 6.2 기기 토큰의 작은 허용 경로

아래 두 경로만 새로 허용한다. 이름과 응답 꼴은 이 설계의 제안이며 아직 구현되지 않았다.

| 메서드·경로 | 역할 |
| --- | --- |
| `GET /api/device/conversations/{cid}/presented-files?limit=50&offset=0` | 준비된 파일 카드 목록 |
| `GET /api/device/conversations/{cid}/presented-files/{file_id}/{version}` | 해당 불변 버전의 바이트 |

권한과 데이터 조건:

- 켜진 반려 기능과 현재 유효한 기기 토큰을 요구한다. 매 요청에서 기기 철회와 대화 소유권을 검사한다.
- 같은 주체가 소유한 활성 대화의 `origin=PRESENTED`이며 성공한 **공개된** ToolResult에
  연결된 파일만 허용한다. 숨겨진/무시된 wake와 `read_events()`에서 숨긴 결과는 파일 목록·
  바이트 경로에서도 노출하지 않는다. BlobStore를 열기 전에 이 조건을 확인한다.
  같은 사용자의 다른 짝지은 기기도 이 조건으로 읽을 수 있다. 별도 기기 전용 파일 소유권은 만들지 않는다.
- 업로드 원본(`UPLOAD`), Tool 이미지(`TOOL`), 미연결 파일, 다른 대화/다른 주체의 파일과
  휴지통/삭제된 대화는 이 경로에서 404다. 조회가 BlobStore를 직접 여는 권한이 되지 않는다.
- 파일 목록은 위 필터를 **페이지를 나누기 전에** 적용한다. 기존 FILE-02 조회의 필터 확장 또는
  전용 공개 읽기 포트로 구현하고, 전체 목록을 읽어 UI에서만 거르는 방식은 사용하지 않는다.
- 목록은 생성 시각 내림차순(동률은 file_id·version 내림차순), 모든 제공 버전을 대상으로
  `limit` 1..200과 `offset >= 0`, `next_offset`을 제공한다. 만료된 기록도 만료 표시로 남긴다.
- 카드 데이터는 `file_id`, `version`, `name`, `size`, `media_type`, `sha256`, `created_at`, `expired`다.
  `workspace_path`, blob_key, 서버 경로와 인증이 든 URL은 주지 않는다.
- 만료된 보관본은 기존 410 `file_expired`, 사라진 보관본은 410 `file_missing`을 사용한다.
  실제 읽기 직전에도 권한/버전/만료를 확인한다.
- 다운로드는 attachment/octet-stream·파일명 정제 등 기존 FILE-04 보호를 재사용한다.
  HTML/SVG 미리보기 경로나 임의 경로 다운로드를 열지 않는다.
- 기존 일반 파일 경로의 `principal()` 기본 거부는 유지한다. 기기 업로드·references·Workspace
  탐색·지시문/설정 변경 등 다른 경로를 함께 허용하지 않는다.

### 6.3 카드, 복구, 사용자 저장

1. 기존 대화 변경 알림/`presented_files`를 새 파일의 힌트로 사용한다. 실제 카드 목록은
   허용된 목록 API로 확인하고 페이지를 끝까지 읽는다. 재연결/앱 재시작/대화 열기에도 복구한다.
2. 현재 열어 둔 대화에 파일명·크기·버전·**저장** 버튼을 표시한다. 다른 대화/기기에서 생긴 파일
   때문에 현재 초안이나 대화를 자동 전환하지 않는다. 카드 키는 서버·기기 자격 증명 식별자·
   conversation_id·file_id·version이며, 재조회로 같은 카드를 중복 만들지 않는다.
3. 저장은 사용자가 누를 때만 네이티브 인증 요청으로 시작한다. 저장 대화상자의 제안 파일명도
   Windows 규칙으로 정제한다. 서버 파일명/모델 인자를 로컬 경로로 그대로 사용하지 않는다.
4. 선택한 폴더의 임시 파일로 한도 내 스트리밍하고 크기·SHA-256을 검증한 뒤 최종 파일로
   옮긴다. 기존 파일 덮어쓰기는 사용자 확인을 받는다. 실패/취소 시 이 요청의 임시 파일만 정리한다.
   서버/기기 전환 시 진행 중인 저장을 취소하고 새 자격 증명으로 이전 응답을 재사용하지 않는다.
5. 앱은 **저장 완료**를 실제 로컬 저장 성공 뒤에만 표시한다. 자동 열기/실행은 하지 않는다.
   이 단계는 저장 영수증을 모델이나 서버 Data에 기록하지 않으며, 앱 재시작 뒤에도 모델이
   과거 저장 완료를 추정하지 않는다. 재시도도 사용자가 눌러 시작한다.

## 7. 구현 패킷과 수용 기준

### 구현 PR 1 — 인터페이스 상태 + 프롬프트 (구현)

대상: Bootstrap/Control의 인터페이스 해석, Data 입력·wake 스냅샷, Prompt DTO/렌더링/해시,
고정 시스템/도구 설명. Windows 다운로드 능력은 아직 false다.

실제 포트는 Control의 `InterfaceSource.read(scope, origin)`이며 Bootstrap의
`ConfiguredInterfaces`를 주입한다. Web/CLI의 파일 수신은 서버의 기존 불변 보관본 다운로드
서비스를 따른다. 이 서비스는 Application이 열리면 제공되며 업로드 배치 검사
(`uploads_work`/`renameat2`)의 성공 여부와 독립적이다. 배치가 불가능해도 이미 제공한
파일은 다운로드할 수 있고, 모델이 읽을 수 있는 파일을 `present_file`로 준비할 수도 있다.
Web 탐색은 Machine 구성과 해당 사용자의 Machine 허용 목록을 따른다. 기기는 협상 구현 전까지 두 능력이 false다.
Control은 원본 전체 로그에서 최신 입력(질문 답변 포함)을 읽어 압축된 Prompt tail과
독립적으로 복원한다. 입력 없는 resume에서는 능력 소스를 다시 조회하지 않는다.
기존 presence는 현재 활동을 설명하며 인터페이스를 선택하지 않는다.

필수 검증:

- Web/CLI/Windows/불명 출처와 서버 파일 기능 on/off, 구형 기기 필드 누락의 유효 능력.
- 기기 A의 턴 중 현재 기기가 B로 바뀌어도 A의 스냅샷을 유지. 병렬 대화, wake, Web에서 답한
  ask_user, Run 한도 재개, 압축/재시작, 옛 기록 및 두 Data 백엔드의 원자성·읽기/롤백 호환.
- `file_download=false`에서 앱 수신을 약속하지 않는 고정 규칙과 도구 결과 의미.
- 같은 모델/설정을 고정한 인터페이스 전환에서 시스템/도구 직렬화 동일, state_key 변경,
  상태 토큰 추정·캐시 표시 위치 유지. persona의 기존 전 대화 범위도 확인.

### 구현 PR 2 — 제공 파일 읽기 권한 + Windows 저장

대상: 제한된 파일 API/조회, 기능 협상, 카드·저장·복구. `present_file` 파이프라인을 재사용한다.
Presence의 Subscription/subscribe/signal 경계와 Windows의 SSE별 HTTP 귀속도 이 PR에 포함한다.

필수 검증:

- 정상 제공 버전과 업로드/Tool 이미지/다른 대화/다른 주체/철회된 기기/휴지통/미연결/만료 파일.
  숨긴 wake/결과, 필터 후 페이지 완전성, 파일 버전 고정, 삭제·만료와 다운로드의 경합.
- 새/구형 서버·앱 조합에서 협상 없는 true 금지, 접수 뒤 권한 철회 시 다운로드 거부.
- 같은 토큰의 **구형→신형**, **신형→구형** SSE 교체를 모두 검사한다. subscribe 직후 false,
  현 세대의 첫 확인, 이전 세대의 지연 true/false 및 세대 없는 구형 신호 뒤에도 새 값이
  유지되는지 확인한다. 구형이 현재 연결이면 이전 신형의 true가 도착해도 false여야 한다.
- 교체된 연결의 늦은 unsubscribe, 일반 스트림 만료/재연결·큐 초과·기기 철회·서버 재시작,
  hello 작성 중 재교체, 다른 기기 세대 사용, 세대 검증/적용과 subscribe의 경합을 검사한다.
- 새 hello 뒤 이전 HTTP 응답/409가 도착한 경우, 이전 본문을 새 세대로 보내지 않는 경우,
  세대 없는 입력은 보수적 스냅샷을 쓰고 세대 오류 입력은 원자적으로 미기록/초안 보존되는 경우.
- 재연결·대화 변경·중복 알림·앱 재시작 시 카드 복구/중복 방지, 다른 대화 초안 보존.
- 저장 대화상자 취소, 파일명·경로 정제, 덮어쓰기 확인, 중간 실패·잘린 응답·해시 불일치,
  서버/기기 전환 시 임시 파일 정리, 실제 저장 뒤에만 완료 표시.
- 기존 파일/화면/기기 권한 및 Windows 포커스·질문·말풍선 회귀.

두 구현 PR은 공개 DTO/포트 또는 인증 계약을 바꾸므로 각각 전체 pytest와 관련 Windows
계약/UI 검사를 수행한다. 설계 PR #179은 문서만 바꾸므로 pytest 범위는 NONE이었다.
1단계는 Windows 코드·프로토콜을 변경하지 않으므로 Windows 검사는 해당하지 않으며,
기기 클라이언트 자체 통합 테스트로 기존 연결/대화 경로를 확인한다.
실제 모델/Provider 캐시 수치 확인은 오프라인 검사와 구분해 보고한다.

## 8. 설계 검수에서 확인할 선택

아래 선택을 담은 설계 PR #179이 병합되었다. 1단계 구현과 2단계 예정 범위는 §7로 구분한다.

| 선택 | 권고안 |
| --- | --- |
| 캐시 유지 범위 | 같은 모델·서버 설정에서 인터페이스 전환. 전역 반려 스위치·persona 편집은 별도 |
| 능력 원본 | 인증된 턴 출처 + 서버/앱/권한 교집합, Data에 턴 스냅샷 저장 |
| 파일 제공 방식 | 기존 Environment 도구와 불변 보관본 재사용 |
| 기기 파일 권한 | 소유한 활성 대화의 성공적으로 제공된 버전만, 좁은 별도 API |
| 로컬 저장 | 사용자가 선택, 자동 저장/실행·서버 수신 영수증은 후속 |
| Workspace | 모델의 기존 작업은 유지, Windows 탐색/편집 UI는 후속 |
