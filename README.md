# HIMARI v3

HIMARI v3의 **아키텍처 결정 기록 및 설계 기준선**입니다. 현재는 설계 단계이며, 이 저장소의 문서는 구현 완료를 의미하지 않습니다.

> 설계 원칙: **Contract-based Modular Architecture** — 모듈은 서로의 내부 구현에 의존하지 않고, 명시된 계약을 유지하는 범위에서 독립적으로 수정·교체할 수 있어야 합니다.

## 결정된 핵심 방향

- **Run = LLM 호출 1회**. Control이 Run 반복을 조정하며, 모델의 final response 또는 question에서 반복을 중단합니다. v2의 Task/ManagerTurn 계층을 v3의 실행 기본 단위로 가져오지 않습니다.
- **Data**는 Conversation의 원본 입력·출력을 exact하게 보존합니다. Prompt는 Data를 가공해 다음 모델 입력을 만들고, **최신 Prompt Snapshot 1개**(압축 문맥 + 최근 exact 입력 데이터)만 유지합니다.
- **Tool 실패**는 ToolResult로 전달하며, 그 이후의 대응은 다음 Run에서 모델이 판단합니다. Runtime의 숨은 의미론적 자동 복구는 기본 동작이 아닙니다.
- **Workspace**는 독립적인 수명을 가진 이종 작업 대상입니다(기본 제공 작업 공간, 사용자 로컬 PC, 원격 환경 등). Conversation 사이에서도 참조할 수 있습니다.
- **Environment**는 Workspace의 연결·접근·실행 방식을 규정합니다. 한 Conversation에서 여러 Environment가 활성화될 수 있고, 동일한 Workspace를 Environment마다 다르게 다룰 수 있습니다.
- **Workspace Registry와 Binding**을 구분합니다. Tool은 Workspace Binding이 필요한 도구와 필요하지 않은 도구로 나뉩니다. 환경 도구의 이름은 `environment/tool` 형태를 고려합니다.
- **Memory**는 별도 특권 계층이 아니라 Tool 형태로 처리할 방향이며, 세부 설계는 보류합니다.
- **Provider reasoning** 설정과 사용량/공개된 continuation 정보는 Data에 보존할 수 있고 Prompt가 이를 사용해 모델 요청을 구성합니다. 비공개 내부 추론 원문은 이용 가능하다고 전제하지 않습니다.

## 문서

- [아키텍처](docs/architecture.md) — 역할, 데이터 흐름, Run·Prompt·Tool·Environment·Workspace 구조
- [모듈 경계 및 계약](docs/module-boundaries.md) — 의존성 규칙, 인터페이스, 계약 검증
- [결정 기록](docs/decisions.md) — 현재까지 합의한 내용과 설계상 권고 사항
- [미결정 사항](docs/open-questions.md) — 초기 구현에 앞서 확정할 계약과 후속 결정

## 상태 및 규칙

이 문서는 v2 코드와 호환성을 보장하는 명세가 아닙니다. v2의 구성을 무조건 이식하지 않고, v3 결정과 모듈 경계에 따라 새로 설계합니다.

**확정(Confirmed)**, **권고(Proposed)**, **미결정(Open)**을 구분합니다. 설계 예시(JSON, ID 이름, 디렉터리 등)는 명시적으로 확정하지 않는 한 구현 계약이 아닙니다.

기록 기준: 2026-10-13.
