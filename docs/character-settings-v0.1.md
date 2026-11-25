# 설정 다시 나누기: "HIMARI"와 "You and HIMARI" v0.1 (캐릭터 로드맵 8단계, 2026-11-25)

[캐릭터 로드맵](character-architecture.md) 8단계. 저장하는 것은 거의 그대로 두고 **Web 설정 화면**을 기억의 출처에 따라 다시 나눈다. 소유자 방침(2026-11-25)에 따라 짧게 정하고 써 보며 고친다.

## 1. 왜

소유자가 써 본 뒤(2026-11-25):

- Memory의 **Timeline**(일화)과 Bond의 **Moments together**가 겹친다. Moments together가 일화와 관계 기록을 이미 함께 보여 준다.
- 사용자에 대한 **메모**는 "실제 사람은 대화 상대에 대해 적어 두지 않는다"는 위화감이 있다. 하지만 메모는 HIMARI가 사용자를 기억하는 장치이고 **알림과 다시 챙길 일이 메모로 돈다.** 그래서 없애지 않고 자리와 이름을 바꾼다.
- HIMARI의 기억은 **사용자와의 대화에서 생긴 것**과 **대화 밖에서 생긴 것**으로 나뉜다. 저장된 데이터도 그대로 이렇게 나뉜다.

| 나뉨 | 데이터 |
| --- | --- |
| 대화 밖(HIMARI 자신) | 상태(기분, 지금 하는 일, 날씨·화제), 작업(프로젝트, 조각), 정사, 일정, 일기 |
| 대화에서(너와 나) | 관계 단계, 둘 사이에 정해진 것, 일화, 사용자에 대한 메모, 메모 제안 |

## 2. 새 설정 순서

1. **Instructions**: 그대로.
2. **Personas**: 그대로(7단계의 칸).
3. **HIMARI**(지금의 Life에서 Bond를 뺀 것): 상태 줄, Work(6단계), Arcs, Plans, Facts, Diary. 지금 페르소나의 것.
4. **You and HIMARI**(새로 묶음):
   - Together N days와 Stage(지금 Bond의 맨 위)
   - Settled between you(지금 그대로)
   - **What HIMARI remembers about you**: 지금의 Memory 메모(종류, Valid until, 날짜 있는 loop) + Suggestions. 페르소나와 상관없이 함께 쓴다는 한 줄 안내.
   - **Moments together**: 일화와 관계 기록의 타임라인. 일화는 여기서 고치고 지운다(지금 Timeline에서 하던 것).
5. **Devices**, 6. **Tool servers**: 그대로.

- **Memory 섹션과 Timeline은 없어진다**(모든 기능이 You and HIMARI로 옮겨 간다).
- 옛 주소(`/settings/memory`, `/settings/life`)로 오면 새 자리로 보낸다.

## 3. 서버

- 저장소와 API는 그대로 둔다. Web이 이미 있는 `/api/memories`, `/api/episodes`, `/api/life`, `/api/bond`를 새 화면에서 다시 쓴다.
- 일화 고치기·지우기가 Moments together에서 되도록 API가 모자라면 그만큼만 더한다.

## 4. 정한 것(권고안대로, 써 보며 바꿈)

| # | 정한 것 |
| --- | --- |
| S1 | 설정을 HIMARI(대화 밖)와 You and HIMARI(대화에서)로 나눈다 |
| S2 | 메모는 없애지 않고 You and HIMARI의 "What HIMARI remembers about you"로 옮긴다 |
| S3 | Timeline은 Moments together에 합친다 |
