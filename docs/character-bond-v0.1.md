# 관계: 관계 정사·기념일·관계 단계 v0.1 (캐릭터 로드맵 3단계, 2026-11-25)

[캐릭터 로드맵](character-architecture.md) 3단계. [삶 v0.1](character-life-v0.1.md)(1·2단계) 위에 만든다. 소유자 방침(2026-11-25)에 따라 짧게 정하고 써 보며 고친다. **2026-11-25 구현, 배포 전(H-78).** 구현에서 정한 것은 [§9](#9-구현에서-정한-것-2026-11-25).

## 1. 무엇을 만드나

- **관계 정사**(페르소나마다 따로, H-76 D1): 사용자와 HIMARI 사이에 **실제로 정해진 것**.
  - `name`: 호칭·별명("앞으로 대장이라고 부를게")
  - `promise`: 둘 사이의 약속("주말마다 영화 얘기 하기로 함"). 사용자 혼자의 일정은 지금처럼 열린 고리다.
  - `joke`: 둘만 아는 농담
  - `first`: 처음 함께 한 일("처음으로 같이 밤샘")
  - `date`: 기념일(월·일과 이름, "사용자 생일 3월 2일")
- **함께한 날:** 그 사용자의 첫 대화 날부터 센 날 수.
- **기념일 먼저 말 걸기:** 함께한 100일·200일·300일·1년(그 뒤 해마다), 그리고 `date` 항목의 날.
- **관계 단계:** `new`(처음) → `familiar`(익숙함) → `close`(가까움). 사건으로 넘어가고 내려가지 않는다.
- **Web:** Life 화면에 관계 정사와 **추억** 타임라인(일화 + 관계 정사, 날짜순).

## 2. 저장

- 삶 항목 표에 종류 `bond`를 더하고, 꼬리표(`name`·`promise`·`joke`·`first`·`date`)와 월·일(`date`만)을 둔다(스키마 v23). 글 300자, 페르소나마다 살아 있는 것 100개.
- 관계 단계: 주인 × 페르소나마다 단계와 넘어간 시각, 넘어간 까닭(항목 번호 또는 날 수).
- 쓴 이: `conversation`(기억 정리), `user`(Web). 작가는 관계를 쓰지 않는다(함께 한 일은 사실만, H-76).

## 3. 기억 정리 넓히기

- 입력에 지금 페르소나의 관계 정사를 더하고, 출력에 `bond`를 더한다: `[{"op": "add|update|retire", "id", "tag", "text", "month_day", "evidence": {"user": [seq], "himari": [seq]}}]`.
- **둘 사이에 정해진 것만:** 사용자가 말하거나 동의한 것이어야 한다. 사용자 메시지 근거가 하나 이상 있어야 한다. HIMARI 혼자의 제안이나 농담은 사용자가 받아야 정해진 것이다.
- `first`는 일화에 그 일이 있을 때만 쓴다.
- 사용자 생일 같은 사용자 사실은 지금처럼 사용자 기억에도 남는다. `date`는 그날을 챙기는 용도다.

## 4. 관계 단계

넘어가는 사건(먼저 맞는 것):

| 단계 | 넘어가는 때 |
| --- | --- |
| `new` → `familiar` | `name` 또는 `joke` 항목이 처음 생김, 또는 함께한 날 30일 |
| `familiar` → `close` | `familiar`이고, `promise` 또는 `first` 항목이 있고, 함께한 날 90일. 또는 Web에서 주인이 올림 |

- 내려가지 않는다. 주인이 Web에서 단계를 바꿀 수는 있다(되돌리기 포함).
- 페르소나가 관계를 정해 두었으면(예: "오래 알고 지낸 친구") **페르소나가 이긴다.** 단계는 그 안의 거리감만 더한다.

## 5. 프롬프트

- runtime state `bond`(반려 켬 + 삶 켬):
  - `stage`: 한 문장, 숫자 없음. 예: "familiar: you know each other's ways and talk at ease"
  - `together_days`, `since`
  - `entries`: 관계 정사, 꼬리표와 글, 1,000자까지, `name`·`promise` 먼저
  - `today`: 오늘이 기념일이면 그 이름
- 반려 절에 `BOND_RULE` 한 문장(요지):
  - 정해진 호칭과 농담을 자연스럽게 쓰고, 약속을 기억한다.
  - 거리감은 단계에 맞추되 페르소나가 정한 관계가 먼저다.
  - 기념일은 그날 한 번만 챙긴다.
  - 여기 없는 공동 경험을 지어내지 않는다.
- 원칙 문단에서 `bond`도 정보로 이름을 댄다.

## 6. 기념일 먼저 말 걸기

- 새 wake 이유 `anniversary`(이름과 날 수 또는 기념일 이름). 그날 처음 지금 기기가 있고 조용한 시간이 아닐 때 한 번만 연다.
- 세 단계 조절 규칙(H-56~)은 알림과 같은 쪽이다: 하루 상한에 들어가고, 자유 wake보다 먼저다.
- 모델은 그 이유로 짧게 챙긴다. 맞지 않으면 `[[pass]]`.

## 7. 하지 않는 것(v0.1)

- 관계 단계에 따라 숨긴 뒷이야기를 드러내는 것은 5단계다.
- 약속을 지켰는지 따지지 않는다.
- 지난 대화를 다시 읽어 관계 정사를 메우지 않는다.

## 8. 정한 것(권고안대로, 써 보며 바꿈)

| # | 정한 것 |
| --- | --- |
| B1 | 관계 정사는 삶 항목 표의 `bond` 종류로 둔다(페르소나별, 같은 화면·같은 물러남 규칙) |
| B2 | 관계로 남기려면 사용자 메시지 근거가 있어야 한다 |
| B3 | 단계 문턱: familiar = 호칭·농담 또는 30일, close = familiar + 약속·처음 + 90일 |
| B4 | 함께한 날 = 그 사용자의 첫 대화 날부터 |
| B5 | 기념일 wake는 하루 상한 안에서 알림과 같은 우선순위 |

## 9. 구현에서 정한 것 (2026-11-25)

설계대로 만들었고 2026-11-25 배포했다(H-78, [기록](deployments/2026-11-25-v3-character.md)). 아래는 만든 것과, 설계가 비워 둔 곳에서 기존 방식에 맞춰 정한 것이다(소유자 방침: 묻지 않고 권고안으로 정하고, 써 보며 고친다).

### 9.1 만든 것

| 부분 | 자리 |
| --- | --- |
| 계약 | `data/api.py`: `LifeKind.BOND`, `BondTag`, `LifeEntry.tag`·`month_day`, `BondStage`·`BondStanding`·`BondChange`·`BondSnapshot`·`Anniversary`, 순수 함수 `together_days`·`anniversaries_on`, Port `BondStore`(목록·사용자 항목 더하기·고치기, 기억 작업의 `apply_bond_changes`, `bond_stage`, `read_bond`, `set_bond_stage`). `WakeKind.ANNIVERSARY`와 `WakeReason.days`·`years`·`entry_id` |
| 저장 | 스키마 v23: `life_entries`에 `tag`·`month_day` 열, 새 표 `bond_stages`(주인·페르소나·단계·넘어간 시각·까닭·리비전). `data/sqlite.py`와 `data/memory.py` 둘 다, 공통 규칙(꼬리표·월일 검사, 순서, 상한, 단계 계산 `settle_stage`)은 `data/rules.py` |
| 기억 정리 | `memory/passes.py`: `BOND_INSTRUCTIONS`, `bond_entries` 입력, `plan_bond`. `memory/worker.py`: 지금 페르소나의 관계를 읽어 넣고, 작업이 적용된 뒤 `self` 다음에 `bond`를 적용 |
| 프롬프트 | `prompt/api.py`: `himari_bond_data`, `BOND_STAGE_WORDS`, `BOND_RULE`, `ANNIVERSARY_RULE`, `CurrentStateProjection.bond`·`PromptInputs.bond`, 기념일 wake 그리기. `prompt/system.py`: `LIFE_RULE` 뒤 `BOND_RULE`, 자유 wake 줄 뒤 `ANNIVERSARY_RULE`, 원칙 문단. Control은 턴 시작에 삶과 함께 관계를 읽는다(`driver._read_user`) |
| 먼저 말 걸기 | `proactive/rules.py`: `anniversary_candidates`, 3단계 조절과 하루 상한(`DAILY_KINDS`), 합치는 순서. `bootstrap/proactive.py`: 판단마다 지금 페르소나의 오늘 기념일을 읽음 |
| Web | `GET/POST /api/bond`, `PUT/DELETE /api/bond/{id}`, `PUT /api/bond/stage`(기기 토큰 403은 기본 규칙). Settings → Life 아래 **Bond** 섹션(`web/src/components/settings/Bond.tsx`) |
| 시험 | `tests/data/test_bond.py`(두 저장소), `tests/data/test_sqlite.py`(v22 → v23, `V23_UNDO`·`_ADDED_IN[23]`), `tests/memory/`(bond), `tests/prompt/test_bond_prompt.py`, `tests/proactive/test_rules.py`(기념일), `tests/bootstrap/test_bond_app.py`·`test_proactive_scheduler.py`, `tests/interfaces/test_web_bond.py`, `Bond.test.tsx`, 실제 모델 확인 `tests/live/test_bond_live.py` |

### 9.2 정한 것

1. **저장:** 관계 항목은 삶 항목 표의 `bond` 종류다(B1). 번호·리비전·지우기(`LifeStore.retire_life`, Web은 `DELETE /api/bond/{id}`)를 삶과 같이 쓴다. `list_life`는 관계를 내지 않고 `BondStore.list_bond`가 따로 낸다(이름 → 약속 → 나머지, 각각 최신순). 단계는 따로 둔 표 `bond_stages`이고 행이 없으면 `new`(리비전 0)다.
2. **상한:** 글 300자, 페르소나마다 살아 있는 항목 100개. 넘으면 대화가 쓴 것 중 가장 오래된 것(고친 시각)부터 물러난다. 모두 사용자 것이면 기억 정리의 새 항목은 건너뛰고(`full`) 사용자의 더하기는 거부한다(409 `bond_full`).
3. **같은 꼬리표·같은 글**(정규화 후)은 기억 정리가 다시 더하지 않는다(`duplicate`). 사용자가 직접 더하는 것은 막지 않는다.
4. **`date`는 MM-DD만**(연도 없음). `02-29`도 되고 윤년이 아닌 해에는 2월 28일에 챙긴다. 다른 꼬리표에는 월·일을 두지 않는다(주면 거부).
5. **함께한 날:** 그 사용자의 가장 이른 대화(`conversations.created_at`, 휴지통 포함)의 날을 1일로 센다. 한국식으로 100일째는 99일 뒤다. 날짜는 **서버 시간대**(`HIMARI_TIME_ZONE`, 운영은 Asia/Seoul)로 센다. 이야기 작가와 같고, 턴·먼저 말 걸기·Web이 같은 날을 본다.
6. **기념일:** 100·200·300일, 그리고 첫날과 같은 월·일(1년, 그 뒤 해마다). 400일 같은 것은 없다. `date` 항목은 해마다 그 월·일. 이름은 날 수면 영어 한 줄("100 days together", "2 years together"), 항목이면 그 글이다.
7. **단계는 읽을 때 정한다:** `read_bond`가 턴 시작, 먼저 말 걸기의 판단마다, Web 읽기에서 단계를 먼저 올리고 저장한다. 그래서 "항목이 생길 때"는 늦어도 다음 턴에 반영되고, 30·90일 문턱을 보는 주기 작업은 따로 두지 않았다. 계기가 항목이면 그 항목을 만든 시각이 넘어간 시각이고, 날 수면 읽은 시각이다. 까닭은 `b<번호>`, `30d`·`90d`, `user`.
8. **단계 계기**(B3)는 출처를 가리지 않는다. 사용자가 Web에서 더한 `name`·`joke`도 `familiar`로 올린다. 한 번 읽을 때 `new` → `familiar` → `close`까지 갈 수 있다.
9. **주인이 단계를 바꾸면**(Web, 리비전으로 충돌 막음) 그 시각 **뒤에 생긴 것만** 센다: 그 뒤에 만든 항목, 그 뒤 날에 닿은 날 수 문턱. 되돌린 단계가 다음 턴에 바로 다시 올라가지 않게 하려고 정했다. 그 밖에는 내려가지 않는다.
10. **프롬프트의 `bond`:** 반려와 삶이 켜지면 항목이 없어도 늘 넣는다(단계는 늘 있으므로). `stage`는 영어 한 문장(숫자 없음), `together_days`, `since`는 **첫 대화 날짜**(단계가 바뀐 시각이 아니다. 단계 시각은 숫자처럼 쓰일 수 있어 넣지 않았다), `entries`(꼬리표·글, `date`는 `date: MM-DD`, 1,000자까지), `today`(이름 목록, 없으면 뺌), `about` 한 줄. 첫 대화가 없으면 날 수와 `since`를 뺀다.
11. **`BOND_RULE`**은 `LIFE_RULE` 바로 뒤다. 기념일 wake에서 `[[pass]]`해도 된다는 말은 따로 한 줄(`ANNIVERSARY_RULE`)로, 자유 wake 줄 뒤에, 자유 wake가 확인된 모델에만 넣는다(그런 모델에만 기념일 wake가 열린다). 원칙 문단은 삶이 켜지면 `your bond with the user (bond)`도 정보로 이름 댄다. 따로 켜고 끄는 설정은 두지 않았다: `HIMARI_LIFE=0`이 관계도 끈다(저장된 것은 남음).
12. **기념일 wake:** `WakeKind.ANNIVERSARY`. 이유에는 `days`·`years`·`entry_id`만 남기고 글은 넣지 않는다(고리처럼, 그릴 때 지금 글을 읽고 지운 항목은 `deleted`). 키 `anniversary:<날짜>:<d100|y1|b7>`로 그날 하나에 한 번이다. 조절은 HIMARI가 먼저 꺼내는 말(3단계)과 같다: 지금 기기, 잠·작업 중 아님, "조용히"와 대화 중 아님, 답하지 않은 wake 없음, 23–08시 아님, 간격 15분. 그리고 하루 상한(`HIMARI_PROACTIVE_FREE_DAILY`, 기본 10)에 자유 wake와 함께 들어간다. 합치는 순서는 알림 바로 다음(B5)이고, 같이 있으면 자유 wake가 기다린다.
13. **pass:** 기념일만인 wake는 자유 wake와 같은 길이다(`free_wake`가 `ANNIVERSARY`도 받는다): 말하기 전까지 숨기고, `[[pass]]`면 보이지 않는다. 그래서 자유 wake가 확인된 모델에만 연다(`HIMARI_PROACTIVE_FREE_MINUTES=0`이어도 연다). 고리 같은 다른 이유와 합쳐지면 보통 wake라서 pass할 수 없다. 자유 wake의 단계 수는 기념일 wake를 세지 않는다.
14. **기억 정리:** 입력 `bond_entries`(3,000자까지), 출력 `bond`는 한 작업에 5개. 사용자 메시지 근거가 하나 이상 있어야 하고(B2) HIMARI 근거는 그 부분의 HIMARI 답만 된다. 근거는 둘을 합쳐 정렬해 남긴다. `first`는 그 작업에 일화가 있을 때만 **더한다**(고치기는 막지 않음). 항목 하나가 틀려도 그것만 버린다(스키마에서 `bond`는 느슨하고 `plan_bond`가 하나씩 본다). `self`처럼 작업이 적용된 뒤 따로 적용하고 live 작업만 한다. 사용자가 쓴(고친) 항목은 고치거나 물러나게 하지 않는다. 지시문은 관계 저장소가 있을 때만 붙인다(`pass_instructions(life, bond)`).
15. **영구 삭제:** 그 대화에서 나온 관계 항목은 메모처럼 지운다(사용자의 말에서 왔으므로, D5). 사용자가 Web에서 고친 것은 남기고 연결만 지운다. 삶의 정사는 전처럼 남긴다.
16. **Web:** Settings → Life 화면 아래에 **Bond** 섹션(같은 화면, 지금 페르소나). 함께한 날·오늘 기념일, 단계 고르기, 항목 목록·더하기(꼬리표, `date`면 MM-DD)·고치기·지우기, **Moments together**(관계 항목 + 최근 일화 60개, 최신순. 일화는 페르소나와 상관없이 함께 쓰는 것이라 다 보인다). 더하기와 단계 바꾸기는 화면이 보여 준 페르소나 번호를 보내고, 그사이 바뀌었으면 409 `persona_changed`.
17. **5단계용:** `BondStore.bond_stage(principal, persona_id)`가 저장된 단계를 올리지 않고 읽는다. `[familiar]`·`[close]` 줄을 숨기는 5단계가 이것을 쓴다.

### 9.3 시험

- 전체 pytest(WSL): **2893 passed, 85 skipped**(4분 4초. 삶 때보다 40개 늘고, 건너뛴 하나는 새 실제 모델 확인). `mypy --strict` 문제 없음(262 파일). `scripts/check_imports.py` OK.
- Web: `npx vitest run` 7파일 32개 통과(Bond 10개), `npm run typecheck`, `npm run lint` 통과.

### 9.4 실제 모델 확인 (2026-11-25)

`tests/live/test_bond_live.py`(`HIMARI_LIVE=1`)를 운영 키로 한 번 돌렸다. 모델 `deepseek/deepseek-v4.1-flash-1017`(기억 작업 모델, 운영 서버의 실제 값은 읽지 않음). 대화: 사용자가 "앞으로 나를 대장이라고 불러 줘", "너는 그냥 루시. 그리고 우리 주말마다 같이 본 영화 얘기 하자. 약속?", "이번 주말엔 회사 워크숍 가". 이미 있는 관계 항목은 농담 "감자 사건" 하나. **1 passed.**

- 입력 1,844·출력 944 토큰, 약 2,800토큰(1센트 아래, 키 사용량 숫자는 실행 직후 바뀌지 않았다).
- `bond` 3개가 모두 모양대로 왔고 `plan_bond`가 셋 다 받았다: `name` "사용자를 '대장'이라고 부른다."(user 1, himari 2), `name` "사용자는 HIMARI를 '루시'라고 부른다."(user 3, himari 4), `promise` "주말마다 함께 본 영화 이야기를 나누기로 약속했다."(user 3, himari 4). 있는 농담은 다시 쓰지 않았다. `self`는 비었다(HIMARI가 자기 삶을 말하지 않았으므로 맞다).
- 메모·일화는 예전처럼 왔다. 워크숍은 열린 고리로 갔다(약속으로 가지 않음).
- 본 것: "대장" 호칭이 사용자 메모(`preference`)와 관계 정사에 둘 다 남았다. 써 보며 겹침이 거슬리면 지시문을 좁힌다.

### 9.5 알려진 한계

- 날짜는 서버 시간대로 센다. 기기가 다른 시간대면 자정 근처에 하루 어긋날 수 있다.
- 모든 대화를 영구 삭제하면 함께한 날은 그 뒤 첫 대화부터 다시 센다.
- 기념일 wake는 그날 기기가 없거나, 조용한 시간·답하지 않은 wake·하루 상한에 막히면 놓친다(다음 날 다시 열지 않음). 그날 대화하면 턴의 `bond.today`로는 챙길 수 있다. 자유 wake가 확인되지 않은 모델에서는 열리지 않는다.
- 대화 도중 페르소나를 바꾸면 그 대화의 관계가 새 페르소나에 들어갈 수 있다(삶과 같음).
- 관계 항목은 Web 대화의 자동 기억 줄에 보이지 않는다(항목별 기록 없음). `bond` 적용은 작업 적용 뒤 따로라서 그 사이 멈추면 잃는다.
- 같은 호칭·생일이 사용자 메모와 관계 정사에 겹쳐 남을 수 있다(§9.4).
- Life 화면의 Bond 섹션을 브라우저로 직접 보지 않았다(컴포넌트 시험만).
