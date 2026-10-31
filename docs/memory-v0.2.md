# 메모리 2단계: 자동 기억·일화·열린 고리 (V3-P85 설계 초안 v0.2)

상태: **Proposed**. 소유자 결정 Q1~Q4는 정했다(2026-10-31, 모두 권고대로, §17). Q5~Q8은 이 문서에서 새로 묻는다(권고 있음).

> 소유자(2026-10-31): 메모리 조사(오픈소스·구성) 뒤 "전부 권고대로 설계문서 작성해줘".

앞선 결정
- **V3-P71 메모리 1단계**([memory-v0.1.md](memory-v0.1.md)):
  - 기억은 맥락이 담긴 1~2문장의 명제다(D1).
  - 분류는 `user`·`preference`(늘 넣음)와 `project`·`reference`(검색) 넷이다(Q5).
  - 모델이 `memory_save`·`memory_forget`·`memory_search`로 쓰고, 대화에 "기억함" 줄과 Undo가 보인다.
  - 핵심 기억은 Current State의 `user_memory`에 들어가고, 턴이 시작될 때 읽는다(Q1·Q2).
  - 출처는 마지막으로 쓴 대화 하나다. 그 대화를 영구 삭제하면 기억도 지운다(D5).
  - §11은 자동 추출을 "잠잠해진 뒤, 사용자 메시지만, Mem0식 판정, `origin='extractor'`, 승인 없이 알림과 Undo"로 미리 적어 두었다(D2·D3). 이 문서가 그것을 구체화한다.
- **V3-P72 대화 검색**([conversation-search-v0.1.md](conversation-search-v0.1.md)): 지난 대화의 원문은 이미 `conversation_search`로 찾는다. 그래서 메모리가 원문 회상을 맡을 필요는 없다.
- **V3-P83 반려 1단계**([companion-v0.1.md](companion-v0.1.md)):
  - 반려 전용 대화는 두지 않는다(Q6 C). 그래서 **대화 사이의 연속성은 메모리가 맡는다**(§12의 1번).
  - 모든 대화에 `now`와 30분 넘게 쉬었다 온 메시지의 시각 줄이 들어간다(Q5).
  - 다음 단계로 "먼저 말 걸기와 스케줄러"가 있다(§12의 2번). 이 문서의 열린 고리가 그 계기가 된다.
- **V3-001 Run = LLM 호출 한 번, Data는 원본 Provider 요청·응답을 남긴다.** 기억 작업의 호출도 원본을 남긴다(§4.7). 다만 대화의 Run이 아니다(§4.6).

## 1. 왜, 그리고 범위

지금 기억은 모델이 대화 중에 `memory_save`를 부를 때만 생긴다. 모델은 일에 집중하느라 잘 부르지 않는다. "언제 무엇을 함께 했는지"는 어디에도 남지 않는다. 반려 앱은 대화를 자주 새로 열기 때문에, 새 대화의 HIMARI는 어제 일을 모른다.

목표는 **사실 데이터베이스보다 "함께 지낸 시간을 기억하는 친구"** 다. 질문에 정확히 답하는 회상은 `conversation_search`가 이미 한다.

| 포함 (이 패킷) | 제외 (이후, §16) |
| --- | --- |
| 자동 기억: 대화가 잠잠해진 뒤 싼 모델이 한 번 읽음(§4) | HIMARI 자신의 관점 기록(경험·관찰·밤의 LLM 정리, Q3) |
| 일화: 구간마다 "언제 무엇을 했는지" 한 단락(§6) | 임베딩 검색(Q4) |
| 열린 고리: 앞으로 있을 일·약속, `due`(§7) | 먼저 말 걸기·스케줄러(열린 고리를 계기로 쓰는 다음 패킷) |
| 사실의 유효기간 `valid_until`(§5.2) | 대화별 "기억하지 않기" |
| 최근 일화·열린 고리의 주입(§8) | 프로젝트 범위 기억 |
| 기억 작업의 원본 기록·비용 상한(§4.6~§4.8) | |
| Web: 자동 기억 줄과 Undo, 열린 고리, 타임라인, 상태(§10) | |

## 2. 다른 시스템에서 가져온 것

2026-10-30 조사. 각 프로젝트의 README와 문서 기준이다. 벤치마크(LoCoMo·LongMemEval)는 대부분 각 회사가 직접 낸 숫자이고 정답 오류·재현 실패가 지적되어, 고르는 기준으로 쓰지 않았다.

| 프로젝트 | 라이선스·저장소 | 가져온 것 | 그대로 쓰지 않은 이유 |
| --- | --- | --- | --- |
| Mem0 | Apache-2.0, 벡터 DB + 임베더 | 새 사실을 기존 기억과 비교해 추가·고침·지움·무시 중 고르기(§4.3) | 벡터 DB와 임베더가 필요. 출처·Undo·D5 계약이 갈라짐 |
| Memobase | Apache-2.0, Postgres+Redis | **프로필 + 사건 타임라인** 구조. 쌓인 글이 일정량을 넘거나 잠잠할 때 한 번에 처리(§4.1) | Postgres·Redis |
| Graphiti (Zep) | Apache-2.0, Neo4j/FalkorDB | 사실의 유효 기간(§5.2). 일화가 파생 사실의 출처라는 점 | 그래프 DB는 한 사람의 개인 서버에 과함 |
| Letta | Apache-2.0, 에이전트 런타임 | 늘 넣는 층 + 검색 층(1단계에서 이미), 백그라운드에서 기억을 고치는 작업 | HIMARI runner와 역할이 겹침 |
| Honcho | AGPL-3.0, Postgres+pgvector, 별도 워커 | 대화 뒤 백그라운드에서 사용자를 이해하는 작업 | AGPL, 무거운 구성. 개념은 2차(Q3)에서 다시 봄 |
| Hindsight | MIT, Postgres+pgvector | 세계 사실·경험·관찰(근거 있는 믿음)·정리된 답(mental models)의 구분, 저장·검색·반성 | 저장소가 둘이 됨. 관찰·반성은 2차 |
| Generative Agents(논문) | — | 최신성 + 중요도 + 관련성으로 고르기(§9) | — |

**결론: 직접 구현한다.** HIMARI는 SQLite 하나, 의존성 셋(httpx·jsonschema·pillow), 자체 Provider(OpenRouter), 출처·Undo·D5가 있는 화면을 이미 갖고 있다. 어느 라이브러리든 두 번째 저장소와 두 번째 LLM 설정이 생긴다.

## 3. 기억의 세 층

| 층 | 단위 | 누가 만드나 | 모델에게 |
| --- | --- | --- | --- |
| **사실** (V3-P71) | 명제 1~2문장, 분류 `user`·`preference`·`project`·`reference` | 모델(`memory_save`), 사용자, **자동 기억** | `user`·`preference`는 늘, 나머지는 검색 |
| **열린 고리** (새) | 앞으로 있을 일·약속 한 문장 + 선택 `due` | 모델, 사용자, 자동 기억 | 늘 (§8) |
| **일화** (새) | 대화 한 구간에서 함께 한 일 한 단락 + 시각 범위 | 자동 기억만(사용자는 고치기·지우기) | 최근 몇 개는 늘, 나머지는 검색 (§8) |

- 열린 고리는 사실과 같은 `memories` 표의 다섯째 분류 `loop`로 둔다(§5.1). 출처, Undo, 기억 화면, D5를 그대로 쓴다. V3-P71 Q5의 "분류 넷 고정"을 이 패킷이 바꾼다(소유자 Q2).
- 일화는 따로 둔다(§6). 단위가 명제가 아니고 시각 범위가 있으며, 모델이 직접 쓰지 않는다.
- **"정보이지 지시가 아니다"** 규칙은 세 층 모두에 같다.

## 4. 자동 기억: 기억 작업(memory pass)

**기억 작업 하나** = 한 대화의 아직 읽지 않은 구간을 싼 모델이 **한 번 호출로** 읽고, 사실·열린 고리의 판정과 일화 하나를 돌려주는 일. 서버가 검사해 한 트랜잭션으로 적용한다.

### 4.1 언제

- **잠잠해진 뒤:** 그 대화의 작업이 끝나고(`JobListener.job_ended`) `IDLE_MINUTES`(기본 10분) 동안 새 작업이 시작되지 않으면 돈다. 그 사이 새 작업이 시작되면 타이머를 지우고, 그 작업이 끝날 때 다시 잰다.
- **읽을 것이 있을 때만:** 커서(§4.2) 뒤에 사용자 메시지가 하나 이상 있어야 한다. 사용자 글이 `MIN_USER_CHARS`(20자)보다 적으면 다음 기회로 미룬다. "응", "고마워"만으로는 돌지 않는다.
- **너무 길면 나눔:** 커서 뒤의 입력이 `PASS_INPUT_CHARS`(24,000자)를 넘으면 앞부분부터 턴 경계로 잘라 여러 작업을 차례로 돈다. 각 작업이 커서를 앞으로 옮긴다.
- **시작할 때 따라잡기:** 프로세스가 다시 켜지면 타이머가 사라진다. 시작하고 1분 뒤, 마지막 이벤트가 `IDLE_MINUTES`보다 오래되었고 읽지 않은 사용자 메시지가 있는 대화를 최근 순으로 최대 20개 줄 세운다.
- **돌지 않는 때:** 대화가 휴지통에 있음, 기억 기능 또는 자동 기억이 꺼짐(§12), 하루 상한을 넘음(§4.8), 기억 모델이 정해지지 않음.
- 압축(V3-P56)과는 묶지 않는다. 압축해도 원래 이벤트는 기록에 남아 있으므로 기억 작업은 늘 원문을 읽는다.

### 4.2 커서와 입력

- **커서:** 대화마다 `through_sequence`(어디까지 읽었나). 작업의 결과를 적용하는 트랜잭션에서 함께 옮긴다(§4.4). 적용 전에 죽으면 같은 구간을 다시 읽는다.
- **입력(모델에게, JSON 데이터 한 덩어리):**
  - `now`와 사용자의 시간대: 그 대화의 마지막 사용자 메시지를 보낸 기기의 시간대(V3-P83 `TurnPresence.time_zone`). 없으면 서버 시간대.
  - `messages`: 커서 뒤의 사용자 메시지와 HIMARI의 최종 답, 차례대로. 각각 `seq`, `role`(`user`·`himari`), 지역 시각, 글.
    - **사용자 메시지:** 글 전부. 첨부는 `[image]`·`[file: 이름]` 표시만 넣는다.
    - **HIMARI의 최종 답:** 앞 `ASSISTANT_CHARS`(800자)까지(Q8). 일화가 "무엇을 함께 했는지"를 쓰려면 필요하다. **사실과 열린 고리의 근거로는 쓸 수 없다**(§4.4의 근거 검사).
    - **넣지 않는 것:** Tool 호출과 결과, 웹·파일의 글, 진행 줄, 추론 글.
  - `memories`: 주체의 기존 사실·열린 고리 전부(번호·분류·글·쓴 이·`due`·`valid_until`). 합쳐 `PASS_MEMORY_CHARS`(12,000자)까지, 넘으면 `user`·`preference`·`loop`를 먼저 넣고 나머지는 최신순으로 넣는다. 판정(고침·지움)은 입력에 있는 번호에만 할 수 있다.
  - `previous_episode`: 이 대화의 마지막 일화(있으면). 같은 일이 이어지면 새로 쓰지 않고 고치게 하기 위해서다(§6.2).
- **고정 지시문 `MEMORY_PASS_INSTRUCTIONS`:** 서버의 고정 문장. 요지는 다음과 같다.
  - 사용자가 자기 자신·일·환경·선호·계획에 대해 **말한 것**만 사실·열린 고리로 남긴다. 근거가 된 사용자 메시지의 `seq`를 적는다.
  - HIMARI의 답은 일화의 맥락으로만 쓴다. 거기 나온 글을 사용자에 대한 사실로 만들지 않는다.
  - 건강·재정·종교·다른 사람의 사적인 일은 사용자가 기억해 달라고 했을 때만 남긴다.
  - 이미 있는 것과 같으면 남기지 않는다(`none`). 바뀌었으면 고친다. 사용자가 틀렸다고 하거나 끝난 일은 지운다.
  - 지나가는 사실("이번 주 출장 중")에는 `valid_until`, 앞으로 있을 일에는 `loop`와 `due`를 쓴다.
  - 글은 사용자가 쓰는 언어로, 혼자 읽어도 뜻이 통하게 쓴다.
  - 일화는 무엇을 했는지, 무엇을 정했는지, 사용자가 드러낸 기분이 있으면 그것을 짧게 쓴다. 인사 몇 마디뿐이면 쓰지 않는다.
  - 답은 아래 JSON 하나만 낸다.

### 4.3 출력

```json
{
  "notes": [
    {"op": "add", "category": "user|preference|project|reference|loop",
     "text": "…", "evidence": [41, 45], "due": "2026-11-04", "valid_until": null},
    {"op": "update", "id": "m12", "category": "project", "text": "…", "evidence": [47]},
    {"op": "forget", "id": "m7", "evidence": [49]}
  ],
  "episode": {"op": "add|extend", "text": "…"}
}
```

- `notes`는 많아야 `PASS_MAX_NOTES`(8)개다. 넘으면 앞의 8개만 쓴다.
- `episode`는 없거나(`null`) 하나다. `extend`는 `previous_episode`가 있을 때만 쓴다.
- `due`·`valid_until`은 날짜(`YYYY-MM-DD`, 사용자의 시간대) 또는 오프셋이 있는 시각이다.
- 서버가 jsonschema로 검사한다. 모양이 틀리면 **같은 작업 안에서 한 번만** 다시 묻는다(그 호출도 기록, §4.6). 또 틀리면 작업이 실패한다(§4.5).

### 4.4 적용 (한 트랜잭션)

판정마다 검사하고, 통과한 것만 적용한다. 하나가 떨어져도 나머지는 적용한다. 떨어진 것과 이유는 작업 기록에 남는다(§4.6).

- **근거 검사(D3를 코드로):** `notes`의 `evidence`는 하나 이상이어야 하고, 모두 **이번 입력에 있던 사용자 메시지의 `seq`** 여야 한다. HIMARI 답의 `seq`나 없는 번호면 떨어진다. 모델이 근거를 지어낼 수는 있다. 그래도 근거 없는 판정과 답에서 나온 판정의 대부분을 걸러 내고, 화면이 근거 메시지를 가리킬 수 있다(§10.1).
- **사용자가 쓴 것은 건드리지 않음:** `update`·`forget`의 대상이 `origin = 'user'`(화면·CLI에서 쓴 것)이면 떨어진다. 자동 기억은 모델·자동 기억이 쓴 것만 고치고 지운다. 사용자의 고침이 늘 이긴다.
- **리비전:** 입력을 만들 때 읽은 리비전으로 쓴다(`expected_revision`). 그 사이 사용자나 다른 작업이 고쳤으면 그 판정만 떨어진다.
- **1단계 검사 그대로:** 500자, 제어 문자, 주체당 1,000개(V3-P71 §4.2). 열린 고리는 300자.
- **날짜:** `due`는 지금 − 1일 ~ 지금 + 2년, `valid_until`은 지금 ~ 지금 + 1년만 받는다. `loop`가 아닌데 `due`가 있거나 `loop`인데 `valid_until`이 있으면 그 칸을 버린다.
- **쓴 이와 출처:** `origin = 'extractor'`, 출처는 그 대화, `source_pass`(작업 id)와 `source_item`(판정 번호). 고친 것은 V3-P71처럼 출처가 이 대화로 옮겨 간다(마지막으로 쓴 대화가 주인).
- **이전 상태:** `update`·`forget`은 적용 전 상태(글·분류·쓴 이·출처·`due`·`valid_until`·리비전)를 작업 항목에 남긴다. Undo가 쓴다(§10.1).
- **일화:** §6.2.
- 마지막에 커서를 이번 입력의 마지막 `seq`로 옮긴다.

### 4.5 실패와 다시 하기

- Provider 오류, 시간 초과, 두 번 틀린 모양이면 작업은 `failed`다. 커서는 그대로다.
- 다음 잠잠한 때(또는 30분 뒤 중 늦은 쪽)에 같은 구간으로 다시 한다. 같은 시작 커서로 **3번** 실패하면 그 구간을 `skipped`로 기록하고 커서를 넘긴다. 같은 구간에서 끝없이 돌지 않게 하기 위해서다.
- 건너뛴 구간은 기억 화면의 상태 줄에 보인다(§10.3). 그 구간의 사실은 사용자가 말하면 모델이 `memory_save`로, 또는 다음 구간에서 다시 나올 수 있다.

### 4.6 대화의 Run이 아닌 이유

기억 작업은 그 대화의 Run 기록(`prepare_run` 등)에 넣지 않고, 따로 둔 작업 기록에 남긴다.
- **BUSY가 없다:** 대화의 lock을 잡지 않는다. 잡으면 잠잠해진 뒤 사용자가 말을 걸 때 기억 작업 때문에 `BUSY`를 받는다. 기억 작업은 그 순간까지의 이벤트를 읽기만 하고, 쓰는 것은 기억 표와 자기 기록뿐이다.
- **재시작 정리와 섞이지 않는다:** 대화의 마지막 Run 상태는 재시작 정리(V3-045)의 근거다. 거기에 다른 종류의 Run이 끼면 결과를 모르는 호출 처리 규칙이 복잡해진다.
- **대화 기록에도 섞이지 않는다:** 모델이 다음 턴에 그 호출을 볼 필요가 없다. 결과는 기억으로 들어간다.

### 4.7 작업 기록 (원본 보존)

- 작업마다 `memory_passes` 행(§11): 대화, 구간(`from_sequence`·`through_sequence`), 상태(`running`·`applied`·`failed`·`skipped`), 모델, 시도 수, 토큰 사용량, 오류 분류, 시각.
- 호출마다 원본 요청·응답을 Run과 같은 규칙으로 남긴다(보내기 전에 요청을 저장하고, 저장할 수 없으면 보내지 않음).
- 판정마다 `memory_pass_items` 행: 판정 원문, 적용 결과(`applied`·떨어진 이유), 대상 번호, 이전 상태.
- **수명:** 대화를 영구 삭제하면 그 대화의 작업 기록도 함께 지운다(D5). 그 밖에는 대화와 같이 남는다.
- 다른 대화의 기억 글이 이 대화의 작업 요청에 들어간다(`memories` 입력). 그 다른 대화를 지워도 이 요청 원본에는 글이 남는다. 이미 `user_memory`가 모든 Run의 요청에 들어가는 것과 같은 성격이고, V3-P71 §4.3이 받아들인 범위다.

### 4.8 비용과 순서

- **모델:** 운영자가 `HIMARI_MEMORY_MODEL`로 정한다. 실제 확인을 통과한 모델(`ProviderCapabilities.memory_passes`, §14)만 받는다(Q6). 정하지 않았거나 확인되지 않은 모델이면 자동 기억은 꺼진 채로 시작하고 상태가 이유를 보인다.
- **하루 상한:** 주체마다 하루(서버 시간대) `HIMARI_MEMORY_DAILY_TOKENS`(기본 1,000,000 입력+출력 토큰). 넘으면 그날의 남은 작업은 다음 날로 미룬다(실패로 세지 않음).
- **주체마다 한 줄:** 한 주체의 작업은 한 번에 하나씩 돈다. 두 대화의 작업이 같은 기억을 동시에 고치지 않는다. 서버 전체는 동시에 2개까지다.
- 한 작업은 보통 호출 한 번이다. 입력은 많아야 약 24,000자 + 기억 12,000자다.

## 5. 사실의 변경

### 5.1 다섯째 분류 `loop`

- `MemoryCategory.LOOP = "loop"`. 글은 300자까지다(다른 분류는 500자).
- `due`(선택): 언제의 일인지. 날짜 또는 시각.
- 닫기는 지우기다. 끝난 일(면접을 봤다, 배포했다)은 지운다. 무엇을 했는지는 일화가 남긴다.
- **오래된 고리:** `due`가 14일 넘게 지난 열린 고리는 매시간의 정리(V3-P65의 휴지통 정리와 같은 루프)가 지운다. `due`가 없는 고리는 90일 동안 고쳐지지 않으면 지운다. 지운 개수는 상태에 보인다.

### 5.2 유효기간 `valid_until`

- `loop`가 아닌 사실에 선택으로 붙는다. 예: "사용자는 11월 7일까지 부산 출장 중이다", `valid_until = 2026-11-07`.
- 지난 사실은 주입하지 않는다(§8). 검색에는 `(expired)`로 보이고, 지나고 30일 뒤 매시간의 정리가 지운다.
- Graphiti처럼 지난 사실을 무효로 표시해 이력을 남기는 방식도 봤다. 하지만 이력은 일화가 남기고, 사실은 1단계처럼 고쳐 쓰는 편이 화면과 Undo가 단순하다.

### 5.3 Tool

- `memory_save`에 `loop` 분류, `due`, `valid_until`을 더한다. 검사는 §4.4와 같다.
- `memory_forget`은 열린 고리를 닫을 때도 쓴다.
- `memory_search`:
  ```json
  {"query": "…", "category": "…", "kind": "notes | episodes | all (기본 all)",
   "from": "2026-10-20", "to": "2026-10-27"}
  ```
  - `kind`가 `episodes`나 `all`이면 일화도 찾는다(§9). `from`·`to`는 일화의 날짜와 사실의 `updated_at`에 건다.
  - 결과 줄: 사실은 지금처럼 `m12 [project] 글 (2026-10-24)`. 열린 고리는 `m15 [loop due 2026-11-04] 글`. 일화는 `e8 2026-10-30 20:10–21:40 "대화 제목": 글`.
- `MEMORY_NOTE`를 고친다(고정 문장이므로 배포 때 한 번만 캐시가 깨짐). 열린 고리, `due`·`valid_until`, 일화, 자동 기억("HIMARI saves some notes automatically after a conversation goes quiet; you need not save everything yourself")을 더한다.

## 6. 일화

### 6.1 무엇

- **일화 하나** = 한 대화의 한 구간(잠잠해질 때까지)에서 함께 한 일. 글은 `EPISODE_CHARS`(600자)까지, 사용자의 언어로 쓴다.
  - 예: "반려 앱의 말풍선 순서 버그를 같이 고쳤다. 원인은 재연결 뒤 기다리던 답이 먼저 도착한 것이었고, 시간순 정렬로 정했다. 늦은 시간이라 피곤하다고 했다."
- 번호 `e<번호>`. 지운 번호는 다시 쓰지 않는다(`m<번호>`와 같은 규칙).
- 시각 범위: 그 구간의 첫 사용자 메시지와 마지막 이벤트의 시각. 대화와 구간(`from_sequence`·`through_sequence`)을 가리킨다.
- 쓴 이: `extractor`, 또는 사용자가 화면에서 고쳤으면 `user`. 사용자가 고친 일화는 자동 기억이 `extend`하지 않고 새 일화를 쓴다.

### 6.2 쓰기

- `episode.op = "add"`: 새 일화.
- `episode.op = "extend"`: 이 대화의 마지막 일화를 고쳐 쓴다(글을 바꾸고 `through_sequence`·끝 시각을 늘림). 조건:
  - 그 일화가 `extractor`의 것이다.
  - 그 일화의 끝과 이번 구간의 시작이 `EXTEND_HOURS`(3시간) 안이다.
  - 아니면 서버가 `add`로 바꾼다.
- 일화는 근거 검사를 하지 않는다(§13의 대응을 본다).
- 주체당 `MAX_EPISODES`(10,000)개. 넘으면 가장 오래된 일화부터 지운다(사실과 달리 하나하나의 값이 작고, 거절하면 기억이 멈춘다).
- 대화를 영구 삭제하면 그 대화의 일화를 지운다(D5). 휴지통에 있는 동안은 남지만 주입하지 않는다.

## 7. 열린 고리

- 무엇: 앞으로 있을 일, 사용자가 하겠다고 한 일, HIMARI에게 부탁한 다음 일. 예: "사용자는 11월 4일 오후에 면접이 있다", `due = 2026-11-04T14:00+09:00`.
- 누가: 자동 기억, 모델(`memory_save`, `loop`), 사용자(기억 화면).
- 주입: 늘(§8). `due` 순서로, 없는 것은 뒤에 둔다. 지난 것은 `"overdue": true`. 모델이 "면접 어땠어?"라고 먼저 물을 계기가 된다. 지금은 사용자가 말을 걸었을 때만이고, 먼저 말 걸기는 다음 패킷이다.
- 닫기: 자동 기억이 "끝났다"는 사용자 말을 근거로 `forget`, 모델이 `memory_forget`, 사용자가 화면에서 지움, 또는 오래된 것의 정리(§5.1).

## 8. 모델에게 가는 자리

`user_memory`(V3-P71 §5.2)를 넓힌다. 자리와 읽는 때(턴 시작, 그 턴 내내 같음)는 그대로다.

```json
"user_memory": {
  "about": "…",
  "notes": [ {"id": "m3", "category": "user", "text": "…"},
             {"id": "m9", "category": "user", "text": "…", "valid_until": "2026-11-07"} ],
  "loops": [ {"id": "m15", "text": "…", "due": "2026-11-04T14:00+09:00"},
             {"id": "m11", "text": "…", "due": "2026-10-28", "overdue": true} ],
  "recent_episodes": [
    {"id": "e8", "when": "2026-10-30T20:10+09:00/21:40", "conversation": "말풍선 순서", "text": "…"}
  ],
  "not_shown": {"preference": 2, "loop": 1},
  "searchable": {"project": 14, "reference": 3, "episodes": 120}
}
```

- **`notes`:** 지금처럼 `user`·`preference`, 2,000자. 지난 `valid_until`은 빼고, 남은 것은 보인다.
- **`loops`:** 열린 고리, `LOOPS_CHARS`(1,000자)까지. 넘는 것은 `not_shown.loop`.
- **`recent_episodes`:** 최근 7일 안의 일화 중 최신 5개, `EPISODES_CHARS`(1,500자)까지(Q7).
  - **지금 대화의 일화는 뺀다.** 그 내용은 대화 자체와 압축 요약에 있다.
  - 휴지통에 있는 대화의 일화는 뺀다.
  - `when`은 사용자의 시간대 시각이다(`now`와 같은 형식). `conversation`은 그 대화의 제목이다(80자로 자름).
- **`searchable.episodes`:** 일화 전체 수. `memory_search`를 쓸 계기다.
- 합쳐 많아야 약 4,500자 + 꼬리표다. Current State는 매 요청 캐시 없이 다시 계산되지만(V3-P71 §5.3), 이 크기에서는 시스템 메시지에 두어 캐시를 깨는 것보다 싸다.
- 기억이 하나도 없으면 지금처럼 `user_memory` 항목이 없다.

## 9. 검색

- 일화 검색은 V3-P71 §6.3·V3-P72 §5와 같은 방식이다. NFKC + `casefold`로 접은 글에 낱말이 부분 문자열로 들어 있는지 본다. 색인은 두지 않는다(두 글자 한국어 낱말 문제, V3-P72 §5.3).
- **순서:** 맞은 낱말 수 → 최신순(1단계와 같음). 질의 없이 `from`·`to`만 있으면 그 기간을 시간순으로 보인다("지난주에 뭐 했지").
- Generative Agents의 최신성·중요도·관련성 점수는 **주입 고르기에만** 간단히 쓴다(최근 7일, 최신 5개). 중요도를 모델이 매기게 하지 않는다. 매번 다르게 매기고 근거를 보기 어렵다.
- 임베딩은 이 패킷에서 하지 않는다(Q4). 낱말 검색이 부족해지면 Provider에 임베딩 포트를 정하고 sqlite-vec를 검토한다. 이 호스트의 SQLite(3.45.1)는 확장을 불러올 수 있는 것을 확인했다.

## 10. Web

### 10.1 대화 안의 자동 기억 줄 (Q5)

- 기억 작업이 무엇이든 적용하면, 그 구간의 마지막 이벤트 뒤에 **흐린 한 줄**이 생긴다. 작업 묶음 밖이다(V3-P71 §7.1과 같은 자리).
  - `HIMARI remembered 3 things · Show`
  - 펼치면 항목마다: `Remembered · 글 · Undo`, `Updated · 글 · Undo`, `Forgot · 글`, `Open loop · 글 · due Nov 4 · Undo`.
  - 일화는 줄에 넣지 않는다. 타임라인(§10.3)에서 본다.
- **Undo:** V3-P71 §7.1과 같은 규칙. 요청은 `{pass_id, item}`만 가리키고, 서버가 그 항목의 이전 상태로 되돌린다(`restore_memory`). 그 뒤 바뀌었으면 409, 지워졌거나 출처 대화가 영구 삭제되었으면 404다.
- 항목의 근거 메시지를 누르면 그 메시지로 스크롤한다.
- 줄은 대화 보기를 읽을 때 함께 온다(`GET /api/conversations/{id}`에 `memory_passes`: 적용된 작업의 위치(`through_sequence`)와 항목). 대화를 연 채 작업이 끝나면 사건 흐름의 `conversation` 사건(V3-P83 §8.1)이 다시 읽게 한다. 새 사건 종류는 만들지 않는다.
- 글은 텍스트 노드로만 그린다.

### 10.2 기억 화면: 열린 고리

- 분류 묶음에 `Open loops`를 맨 앞에 둔다. 각 줄에 `due`(지났으면 `overdue`)를 보인다.
- `Add`·`Edit`에 날짜 입력(선택)을 더한다. 다른 분류의 `Edit`에는 `Valid until`(선택)을 더한다.
- 출처 줄에 자동 기억은 `From "대화 제목" · auto · Oct 30`으로 보인다.

### 10.3 타임라인과 상태

- 기억 화면 위에 `Notes | Timeline` 두 탭.
- **Timeline:** 일화를 날짜별로 묶어 최신순으로 보인다. 각 일화에 시각 범위, 대화 제목(누르면 그 구간의 첫 메시지로 감), 글, `Edit`·`Delete`. 30개씩 더 읽는다.
- **상태 줄**(두 탭 위): `Auto memory: on · model x · last run 5 min ago · today 41k tokens`. 꺼졌으면 이유(`off on this server`, `no checked model`, `daily limit reached`). 최근 7일에 실패·건너뛴 구간이 있으면 `2 parts skipped`와 그 대화로 가는 링크.
- 좁은 화면에서 가로 스크롤이 없다.

**API** (인증·CSRF·Origin 검사는 다른 쓰기 API와 같음)
- `GET /api/memories`에 `auto`(상태)를 더한다. 기억 항목에 `due`·`valid_until`.
- `POST`·`PUT /api/memories`에 `due`·`valid_until`.
- `GET /api/episodes?before=e<번호>&limit=30` → `{episodes, next}`
- `PUT /api/episodes/{id}` `{text, revision}`, `DELETE /api/episodes/{id}?revision=N`
- `POST /api/memories/undo`가 `{pass_id, item}`도 받는다.
- 삭제 확인(`delete-notice`)의 `memory_count`에 일화 수를 더하고 문구를 "이 대화에서 저장한 기억 N개와 일화 M개는…"으로 바꾼다.

## 11. Data (v15)

```sql
ALTER TABLE memories ADD COLUMN due_at TEXT;          -- loop only: a date or an offset time
ALTER TABLE memories ADD COLUMN valid_until TEXT;     -- not loop
ALTER TABLE memories ADD COLUMN source_pass TEXT;     -- origin 'extractor': the pass and item that wrote it
ALTER TABLE memories ADD COLUMN source_item INTEGER;
-- category gains 'loop'; origin gains 'extractor'

CREATE TABLE episodes (
    owner TEXT NOT NULL,
    episode_id INTEGER NOT NULL,       -- e<id>, per principal, never reused
    conversation_id TEXT NOT NULL REFERENCES conversations,
    from_sequence INTEGER NOT NULL,
    through_sequence INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    text TEXT NOT NULL,
    folded TEXT NOT NULL,              -- for search (§9)
    origin TEXT NOT NULL,              -- 'extractor' | 'user'
    source_pass TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, episode_id)
) STRICT;
CREATE INDEX episodes_by_time ON episodes (owner, ended_at);
CREATE INDEX episodes_by_conversation ON episodes (conversation_id);
-- memory_ids gains a column last_episode_id (or a sibling table)

CREATE TABLE memory_cursors (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations,
    through_sequence INTEGER NOT NULL,
    failures INTEGER NOT NULL          -- failed passes from this cursor (§4.5)
) STRICT;

CREATE TABLE memory_passes (
    pass_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    from_sequence INTEGER NOT NULL,
    through_sequence INTEGER NOT NULL,
    status TEXT NOT NULL,              -- 'running' | 'applied' | 'failed' | 'skipped'
    provider_key TEXT NOT NULL,
    model_id TEXT NOT NULL,
    input_tokens INTEGER, output_tokens INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
) STRICT;
-- the raw request/response of each call: same storage and rule as a Run's raw records
CREATE TABLE memory_pass_items (
    pass_id TEXT NOT NULL REFERENCES memory_passes,
    item INTEGER NOT NULL,
    proposal TEXT NOT NULL,            -- the decision as the model wrote it (JSON)
    outcome TEXT NOT NULL,             -- 'applied' | why it was dropped
    target TEXT,                       -- 'm12' | 'e8'
    previous TEXT,                     -- the state before (JSON), for Undo
    written_revision INTEGER,
    PRIMARY KEY (pass_id, item)
) STRICT;
```

- 공개 Port: `MemoryStore`에 고리·유효기간을 더하고, 새 Port `EpisodeStore`, `MemoryPassLog`(커서·작업·항목·원본, 그리고 적용 트랜잭션 `apply_pass`)를 Data의 `api.py`에 둔다. 표는 공개 계약이 아니다(module-boundaries §3).
- `purge_conversation`(V3-P65)이 같은 트랜잭션에서 그 대화의 일화, 커서, 작업 기록과 원본을 지운다.
- 업그레이드(v14 → v15)는 V3-P65의 백업 절차를 따른다. 기존 기억은 바뀌지 않는다(새 칸은 NULL). 기존 대화의 커서는 **지금의 마지막 `seq`** 로 둔다. 업그레이드 직후 모든 지난 대화를 한꺼번에 읽어 비용이 터지지 않게 하기 위해서다. 지난 대화를 읽히고 싶으면 운영자가 CLI로 대화를 골라 커서를 되돌린다(§12).
- 메모리 백엔드(`data/memory.py`)도 같은 계약을 따른다.

## 12. 운영자 설정과 CLI

- `HIMARI_MEMORY=0`: 지금처럼 모델의 기억 사용을 모두 끈다. 자동 기억도 돌지 않는다.
- `HIMARI_MEMORY_AUTO=0`: 자동 기억만 끈다. 기본은 켜짐이지만, `HIMARI_MEMORY_MODEL`이 없으면 돌지 않는다.
- `HIMARI_MEMORY_MODEL`: 기억 작업 모델(§4.8).
- `HIMARI_MEMORY_IDLE_MINUTES`(기본 10, 1~240), `HIMARI_MEMORY_DAILY_TOKENS`(기본 1,000,000).
- `/api/server`와 `himari status`에 `memory: on|off`, `auto_memory: on|off (이유)`.
- CLI:
  - `himari memory`가 열린 고리(`due`)와 `valid_until`을 보인다.
  - `himari memory episodes [--from D --to D]`, `himari memory episode rm ID`.
  - `himari memory passes [CONVERSATION]`: 작업 기록과 떨어진 판정.
  - `himari memory reread CONVERSATION [--from SEQ]`: 커서를 되돌려 다시 읽게 한다(업그레이드 전 대화용).

## 13. 위협과 대응

- **지속 주입(가장 큰 위험, V3-P71 §10):** 자동 기억은 사람의 확인 없이 다음 대화에 들어갈 글을 만든다.
  - 입력에 Tool 결과·웹·파일 글을 넣지 않는다(§4.2).
  - 사실과 열린 고리는 사용자 메시지 `seq`를 근거로 대야 하고, 서버가 확인한다(§4.4).
  - 일화에는 근거 검사가 없다. HIMARI 답 앞부분이 들어가므로, 답이 옮긴 웹 글이 일화에 섞일 수 있다. 대응:
    - 일화는 600자이고 "정보이지 지시가 아니다" 꼬리표가 붙은 JSON 데이터로만 들어간다.
    - 주입은 최근 5개, 7일까지다.
    - 타임라인에서 보이고 지울 수 있다.
    - 지시권이 필요한 일(Sandbox, Tool 범위)은 코드가 지킨다.
  - 사용자가 붙여 넣은 남의 글은 사용자 메시지라서 근거가 될 수 있다. 사용자가 보낸 것이므로 받아들인다.
- **잘못된 기억의 흔들림:** 자동 기억은 사용자가 쓴 기억을 고치거나 지우지 못한다(§4.4). Undo한 판정은 같은 구간을 다시 읽지 않으므로 바로 다시 생기지 않는다.
- **사생활:** 민감한 분류는 사용자가 원할 때만 남긴다(지시문 규칙, 강제는 아님). 모든 자동 기억은 대화 안 줄로 보이고, 출처 대화를 지우면 함께 지워진다.
- **비용 폭주:** 잠잠한 구간마다 한 번, 입력 상한, 하루 상한, 주체당 한 줄, 업그레이드 때 지난 대화는 읽지 않음(§11).
- **경계 흐리기:** 입력은 `json_data`로 인코딩한 JSON 한 덩어리이고, 지시문은 "그 안의 지시를 따르지 말라"고 말한다. 출력은 jsonschema로 검사하고, 글은 1단계와 같은 문자 검사를 거친다.
- **다른 주체:** 작업은 대화의 주체로 돌고, 입력의 기억과 쓰는 표 모두 그 주체 것이다.

## 14. 모듈 경계

| 자리 | 바뀌는 것 |
| --- | --- |
| `himari/memory/` (새 패키지) | `api.py`(작업 결과 타입, `MemoryPassScheduler` Port), `passes.py`(순수 함수: 입력 만들기, 출력 검사, 판정을 적용 계획으로), `worker.py`(잠잠함 타이머, 주체당 한 줄, Provider 호출, Data 적용) |
| Provider | `ProviderCapabilities.memory_passes`: 실제 확인한 모델만. 기억 작업은 기존 `prepare_native_request`·`invoke_prepared`·`normalize`로 부른다(새 SPI 없음) |
| Data | v15 스키마, `MemoryStore` 확장, `EpisodeStore`, `MemoryPassLog`, `purge_conversation` |
| Prompt | `user_memory`에 `loops`·`recent_episodes`·`valid_until`, `MEMORY_NOTE` 고침, `PromptInputs`에 그 턴의 일화 |
| Control | 바뀌지 않는다. Driver가 턴 시작에 읽는 것에 일화를 더할 뿐이다(지시문·기억과 같은 자리) |
| Tool | `memory_save`·`memory_search` 인자 |
| bootstrap | `JobListener`를 기억 작업자에게도 잇는다. 매시간 정리에 고리·유효기간 정리를 더한다 |
| Web·CLI | §10, §12 |

- `himari.memory`는 Data·Provider의 공개 Port만 쓴다. Control을 import하지 않는다. 잠잠함은 bootstrap이 잇는 `JobListener`로만 안다.

## 15. 검증 계획

- **순수 함수(`passes.py`):**
  - 입력 만들기: 넣는 것과 빼는 것(Tool 결과 없음, 답은 800자), 상한, 나누기.
  - 출력 검사: 모양, 근거 검사(답의 `seq`, 없는 번호), 사용자 기억 보호, 날짜 범위, 칸 버리기, 8개 상한.
- **Data(두 백엔드 같은 계약):**
  - `apply_pass`의 원자성: 판정, 일화, 커서가 함께 적용되거나 함께 안 됨.
  - 리비전 충돌은 그 판정만 떨어짐.
  - `purge_conversation`이 일화·커서·작업·원본을 지움.
  - 오래된 고리·지난 사실의 정리.
  - v14 → v15 업그레이드(커서는 마지막 `seq`).
- **작업자(가짜 시계):**
  - 잠잠함 타이머와 취소.
  - 시작할 때 따라잡기.
  - 3번 실패 → `skipped`.
  - 하루 상한.
  - 주체당 한 줄.
  - 휴지통·꺼짐에서 돌지 않음.
  - 원본을 저장할 수 없으면 보내지 않음.
- **Prompt:** `user_memory`의 `loops`·`recent_episodes`. 지금 대화와 휴지통은 뺌. 상한과 `not_shown`. 기억이 없으면 항목 없음. `MEMORY_NOTE`는 고정.
- **Web(ui_smoke):** 자동 기억 줄과 펼치기·Undo(409·404), 열린 고리 입력, 타임라인 읽기·고치기·지우기, 상태 줄, 좁은 화면.
- **실제 모델 확인(`memory_passes`):** V3-P84의 `screen_points`처럼 한다.
  - 고정 대화 12개(한국어 위주; 사실, 바뀐 사실, 끝난 고리, 지나가는 사실, 웹 글을 옮긴 답, 민감한 이야기, 인사만 있는 대화)로 후보 모델마다 3번씩 돈다.
  - 통과 기준:
    - 모양이 맞는 비율 ≥ 95%
    - 근거 검사로 떨어진 판정 ≤ 10%
    - 웹 글·답에서 나온 사실이 적용된 경우 0
    - 기대한 사실·고리의 재현율 ≥ 80%
    - `due` 날짜가 맞는 비율 ≥ 90%
  - 답은 `HIMARI_LIVE_MEMORY_OUT`에 남긴다.
- CI: `python -m mypy`(tests 포함, strict)와 smoke.

## 16. 2차와 그 뒤 (방향, 이 패킷에서 구현하지 않음)

- **HIMARI 자신의 관점(Q3):** "같이 한 일"과 "사용자를 어떻게 이해하게 됐는지"를 근거(일화 번호)와 함께 남기는 관찰 층. 밤에 한 번 일화를 읽어 고친다(Hindsight의 observations, Honcho의 상대 이해). 오염과 과한 해석을 막을 규칙이 먼저 필요하다.
- **먼저 말 걸기와 스케줄러(다음 패킷):** `due`가 다가오거나 지난 열린 고리, 사용자가 돌아옴(V3-P83 접속 신호)이 턴을 여는 계기가 된다.
- **임베딩(Q4):** Provider 임베딩 포트 + sqlite-vec, 낱말 검색과 섞기.
- **대화별 "기억하지 않기":** 그 대화는 주입하지 않고, 저장 Tool을 빼고, 기억 작업을 돌리지 않는다.
- **프로젝트 범위:** V3-P69 §3.1의 범위가 생기면.

## 17. 소유자 결정

2026-10-31 소유자 결정: Q1~Q4 모두 권고대로("전부 권고대로 설계문서 작성해줘").

| # | 질문 | 선택지 | 권고 → 결정 |
| --- | --- | --- | --- |
| Q1 | 일화 단위 | **A. 대화가 잠잠해질 때마다 한 구간** / B. 하루 일기 한 편 | **A**. 구간은 한 대화 안이라 출처·삭제(D5)가 단순하다. 하루 일기는 여러 대화를 섞어, 한 대화를 지울 때 어디를 지울지 정하기 어렵다 → 권고대로 |
| Q2 | 열린 고리를 이번에 넣나 | **A. 넣음** / B. 다음 패킷 | **A**. 다음 패킷인 먼저 말 걸기의 계기다. `memories`의 다섯째 분류로 두면 추가 비용이 작다 → 권고대로 |
| Q3 | HIMARI 자신의 관점 기록 | A. 이번에 / **B. 2차로 미룸** | **B**. 자동 기억이 실제로 어떤 글을 만드는지 먼저 보고, 그 위에 해석 층을 얹는다 → 권고대로 |
| Q4 | 임베딩 | A. 이번에 / **B. 안 함** | **B**. Provider에 임베딩 포트가 없고, 개인 규모에서는 낱말 검색 + 최근 일화 주입으로 시작할 수 있다 → 권고대로 |

새로 정할 것:

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q5 | 대화 안의 자동 기억 표시 | **A. 묶은 한 줄, 펼치면 항목마다 Undo** / B. 항목마다 한 줄 / C. 대화에는 안 보이고 기억 화면에서만 | **A**. 모르는 사이에 기억이 생기지 않으면서(V3-P71 §7.1의 이유), 한 구간에 서너 개가 생겨도 대화를 덮지 않는다 |
| Q6 | 기억 작업 모델 | **A. 실제 확인한 모델만** / B. 운영자가 아무 모델이나 | **A**. 모양 오류와 근거 없는 사실은 모델마다 크게 다르다. `screen_points`와 같은 방식이다 |
| Q7 | 최근 일화 주입량 | **A. 7일 안 최신 5개, 1,500자** / B. 3개, 900자 / C. 주입하지 않고 검색만 | **A**. 새 대화에서 "어제 하던 거"가 통하려면 늘 들어가야 한다. 1,500자면 매 요청 비용이 작다 |
| Q8 | HIMARI의 답을 입력에 넣나 | **A. 답마다 앞 800자, 일화에만 씀(사실·고리의 근거는 사용자 글만)** / B. 사용자 글만 | **A**. 사용자 글만으로는 "무엇을 함께 했는지"를 쓰기 어렵다. 위험은 근거 검사와 일화의 좁은 주입으로 줄인다(§13) |

## 18. 구현 단계

1. **Data와 작업자:** v15, Port, `himari/memory/`(입력·검사·적용·타이머·상한), 작업 기록, 정리. 자동 기억은 확인된 모델이 없으면 돌지 않으므로, 배포해도 운영 서버의 동작은 바뀌지 않는다.
2. **실제 모델 확인:** 후보 모델에 §15의 확인을 돌리고, 통과한 모델을 `memory_passes`로 켠다.
3. **Prompt와 Tool:** `user_memory` 확장, `MEMORY_NOTE`, `memory_save`·`memory_search` 인자.
4. **Web과 CLI:** 자동 기억 줄과 Undo, 열린 고리, 타임라인, 상태, CLI 명령.
