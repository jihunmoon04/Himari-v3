# 먼저 말 걸기 간략화 (V3-P107)

상태: **Confirmed**(2026-11-24). 소유자 요청: "먼저 말걸기가 너무 복잡하니 알아서 줄여 달라"(작업 방식은 맡김). 이 문서는 [V3-P90 설계(proactive-v0.2.md)](proactive-v0.2.md)의 §2.4, §5, §6.2, §7.4, §8을 바꾼다. 자유 wake와 답 없는 wake는 작업 단계(H-81)가 다시 바꿨다(§8). 나머지(잠들기, 지금 기기, 돌아옴, 작업 끝남, 자유 wake의 pass와 숨김, 읽는 도구만)는 그대로다.

## 1. 무엇이 바뀌나

| | V3-P90 (지금까지) | V3-P107 |
| --- | --- | --- |
| 잠든 캐릭터와 알림 | `idle` 잠이면 깨워서 말하고 `told`(확인 전)로 둠. 같은 기기의 돌아옴이 10분 안이면 "봤음", 그 뒤면 `again`으로 다시 알림, 그 대화의 메시지도 "봤음" | **깨우지 않는다.** 어떤 잠이든 알림을 기다리게 하고, 사용자가 돌아오면(잠이 풀리면) 그때 말한다 |
| 알림 상태 | `scheduled → claimed → delivered / told / missed`, `told → claimed`, 되돌릴 때 `told`로 | `scheduled → claimed → delivered / missed`(V3-P87 그대로) |
| 조절 규칙 | wake마다 표(§5 of V3-P90)로 따로 | **세 단계**(§3) |
| 자유 wake 타이머 | 15 → +30 → +60분마다(`FREE_STEPS`) | **한 간격**, 기본 30분(`FREE_MINUTES`) |
| 자유 wake 하루 예산 | 말한 것 6, 연 것(pass 포함) 12 | **연 것(pass 포함) 하나**, 기본 10 |

줄어든 것: `told`·`again`·`asleep` 표시, `TOLD_SEEN`, `Told`, `seen_reminders`, Data의 `told_reminders`·`see_answered_reminders`·`see_reminders`, 스케줄러의 매 틱 "봤나" 확인 두 번, 설정 둘(`HIMARI_PROACTIVE_FREE_STEPS`, `HIMARI_PROACTIVE_FREE_ATTEMPTS`), 상태의 `attempts`·`free_attempts`. 서버 코드는 292줄을 지우고 102줄을 더해 약 190줄 줄었다(테스트는 273줄 삭제, 160줄 추가).

## 2. 알림: 잠든 캐릭터를 깨우지 않음

- `_blocked`가 알림도 잠(`sleep`이 `idle`이든 `display_off`든)으로 막는다(`asleep`). 기다리는 동안 상태는 `scheduled` 그대로다.
- 사용자가 PC로 돌아오면 앱이 깸을 알리고(`after`/`wake`), 잠이 풀리므로 다음 판단에서 말한다. 비운 시간이 길면 같은 wake에 돌아옴 인사와 합쳐진다(합치는 순서는 그대로: 알림이 먼저).
- 24시간 안에 돌아오지 않으면 지금처럼 그 뒤의 돌아옴에 늦은 알림(`late`, 7일까지), 그 뒤 `missed`.
- **바뀐 동작(받아들인 것):** 사용자가 자리에 없을 때 캐릭터 말풍선에 쪽지처럼 남겨 두는 일이 없어졌다. 그 대신 사용자가 돌아온 순간에 말한다. 사용자가 입력 없이 화면만 보고 있어 캐릭터가 잠든(`idle`) 동안에는 알림이 늦어진다(움직이면 바로 말함). 정확한 시각이 중요한 일은 휴대폰 알람이 맡는다고 본다.
- 그대로인 것: 알림은 사용자가 부탁한 것이므로 "조용히", 23–08시, 대화 중, 답하지 않은 wake, 간격, 하루 예산을 보지 않는다(V3-P87 Q8).

**저장된 것(스키마 v19):** v18 DB의 `told` 알림은 업그레이드 때 `delivered`가 된다(그 대화에 답이 기록되었으므로). 데이터만 바꾸는 업그레이드지만 지금 규칙대로 백업을 먼저 만든다. 저장된 wake 이유의 `asleep`·`again`은 읽을 때 무시한다(옛 기록은 그대로 그려지되 그 표시만 없음).

**Windows 앱(2026-11-24 정리):** 잠든 동안 온 wake에 잠깐 깨어 말하고 다시 잠드는 동작(V3-P90 §2.4)은 남긴다. 이 서버는 잠든 기기에 wake를 열지 않지만, wake를 연 직후 잠든 경우, 운영자의 `try`, 옛 서버의 알림에서 여전히 쓰인다. 알림 전용이던 이름(`reminderAwake`)은 `wakeAwake`로 바꿨다. 사용자가 깨울 때까지 그 말풍선을 남겨 두던 것(`bubbleUntilAwake`)은 없앴다. 이제 다른 말풍선처럼 정해진 시간 뒤에 닫힌다.

## 3. 조절 규칙: 세 단계

먼저 막는 규칙이 이유(`blocked`)가 된다. 위 단계의 규칙은 아래 단계에도 모두 적용된다.

| 단계 | wake | 막는 규칙 |
| --- | --- | --- |
| 1. 모든 wake | 알림 | 지금 기기 없음(`no_device`), 작업 중(`working`), 잠(`asleep`) |
| 2. 방금 일어난 일 (`EVENT_KINDS`) | 돌아옴, 작업 끝남 | + "조용히"(`quiet`), 대화 중 10분(`talking`) |
| 3. HIMARI가 먼저 꺼내는 말 | 열린 고리, 자유 wake | + 답하지 않은 wake(`unanswered`), 23–08시(`quiet_hours`), 간격 15분(`gap`), 자유 wake는 하루 예산(`daily`) |

- V3-P90 표와 다른 점은 둘뿐이다. 알림이 잠에 막히는 것(§2, 화면이 꺼져 막힐 때의 이유도 `display_off`에서 `asleep`으로)과, 돌아옴이 "대화 중"에도 막히게 된 것. 돌아옴은 "비운 동안 사용자 메시지가 없음"이 조건이라 비운 시간(1시간 이상)이 지난 뒤 10분 안의 메시지는 있을 수 없으므로 실제로 달라지는 경우는 없고, 스케줄러가 wake를 열기 직전 다시 보는 확인(`TALKING_KINDS`)도 "알림만 빼고 모두"로 한 줄이 되었다.
- 작업 끝남이 3분 뒤 버려지는 것, 고리가 24시간 기다리는 것, 늦은 알림 규칙은 그대로다.

## 4. 자유 wake: 한 간격, 하루 한 셈

- **간격:** 같은 작업이 `HIMARI_PROACTIVE_FREE_MINUTES`(기본 **30**, 0이면 자유 wake 없음, 최대 1440) 이어지면 연다. 그 뒤에는 그 wake(어느 종류든)부터 다시 같은 간격. 4시간 작업이면 30, 60, 90 … 분.
  - 단계(`step`)는 그대로 센다. 같은 작업에서 몇 번째 자유 wake인지가 열쇠(`free:<기기>:<작업 시작>:<단계>`)와 모델에게 가는 `step`에 들어간다. 바뀐 것은 단계마다 간격이 늘어나지 않는다는 것뿐이다.
  - 사용자 메시지는 단계도 기다림도 바꾸지 않고 10분 동안 막을 뿐이다(V3-P90 §17.8 그대로).
- **하루 예산:** `HIMARI_PROACTIVE_FREE_DAILY`(기본 **10**, 0–96)는 그날(지금 기기의 시간대 자정부터) **연** 자유 wake 수다. 말한 것과 pass한 것을 같이 센다. 둘 다 요청 하나의 비용이고, 사용자에게 들리는 횟수는 간격·대화 중·답하지 않은 wake 규칙이 이미 줄인다.
  - V3-P90은 "말한 것 6"과 "연 것 12"를 따로 셌다. 15분부터 시작하던 타이머가 30분 한 간격이 되면서 하루에 열리는 수 자체가 줄어 두 셈을 나눌 이유가 약해졌다.
- 상태(`himari proactive`, `/api/proactive`)의 `today`는 오늘 연 자유 wake 수, `daily`는 그 한도다. `attempts`·`free_attempts`와 막힘 이유 `attempts`는 없앴다.
- 옛 설정 `HIMARI_PROACTIVE_FREE_STEPS`·`HIMARI_PROACTIVE_FREE_ATTEMPTS`는 더 읽지 않는다(값이 있어도 오류 없이 무시). 단, V3-P90에서 자유 wake를 끄던 방법(`HIMARI_PROACTIVE_FREE_STEPS=` 빈 값)은 `HIMARI_PROACTIVE_FREE_MINUTES`가 없을 때 계속 끈다. 끈 기능이 업그레이드로 조용히 켜지지 않게 하기 위해서다(검수 P2).

## 5. 줄이지 않은 것

| 후보 | 남긴 이유 |
| --- | --- |
| 알림과 열린 고리를 한 종류로 합치기 | 둘은 메모의 다른 필드(`remind_at`·`due_at`)와 Web 기억 화면, 기억 정리(V3-P85)의 규칙에 걸쳐 있다. 합치면 먼저 말 걸기 밖의 계약이 바뀐다 |
| 열쇠의 ±1분 별칭 | 작업 시작·끝 시각을 "지금 − 지속 시간"으로 다시 셈하는 데서 온다. 앱이 사건 id를 보내야 없앨 수 있어 앱 계약이 바뀐다 |
| `focus`·`game_ended` enum | 옛 wake 기록을 읽고 그리는 데 쓴다. 지우려면 기록을 옮기는 마이그레이션이 필요한데 얻는 것이 enum 두 줄뿐이다 |
| `RETURN_WAIT`(20초) | `sleep`을 보내지 않는 옛 앱의 돌아옴에만 쓰인다 |
| 자유 wake의 숨김과 pass | 다른 반려 프로젝트에 없는 HIMARI의 장점이다(2026-11-23 비교). 상태 기계(`opened`·`speaking`·`passed`·`dropped`)는 그대로 둔다 |

## 6. 계약 변경

- `WakeReason`: `asleep`, `again` 필드 삭제(Data 공개 DTO). 저장된 JSON의 두 키는 읽을 때 무시.
- `WakeStore`: `told_reminders`, `see_answered_reminders`, `see_reminders` 삭제. `begin_wake`는 `scheduled`만 `claimed`로, `end_wake`는 `delivered` 또는 `scheduled`(+1회)/`missed`로만, `miss_reminders`는 `scheduled`만 본다. `PENDING_REMINDERS`는 `scheduled`·`claimed`.
- SQLite 스키마 v19(데이터만): `told → delivered`.
- `himari.proactive`: `Told`, `seen_reminders`, `TOLD_SEEN`, `Situation.told`, `Decision.seen` 삭제. `EVENT_KINDS`, `free_today` 추가. `ProactiveSettings`: `attempts`·`free_steps` 삭제, `free_minutes` 추가, `daily` 기본 10.
- 설정: `HIMARI_PROACTIVE_FREE_MINUTES` 추가, `HIMARI_PROACTIVE_FREE_STEPS`(빈 값의 "끔"만 남음, §4)·`HIMARI_PROACTIVE_FREE_ATTEMPTS` 삭제, `HIMARI_PROACTIVE_FREE_DAILY`의 뜻과 기본값(6 → 10, 범위 0–96) 바뀜.
- 상태의 막힘 이유(`last_look.outcome`): 알림이 꺼진 화면에 막히면 `display_off` 대신 `asleep`, `attempts`는 없음. 읽는 곳은 CLI의 출력 줄뿐이다.
- 프롬프트: wake 이유에 `"again"`이 더 나오지 않는다. 실제 모델 확인 장면 `reminder_again`을 뺐다(`tests/live/test_proactive_live.py`).
- Web 대화 기록의 wake 줄과 wake 이유(`event_view`의 `wake.reasons`): `again` 삭제(옛 기록도 표시하지 않음).

공개 계약이 바뀌므로 전체 pytest를 돌린다(V3-038).

## 7. 검증

- 규칙(`tests/proactive/test_rules.py`): 어떤 잠이든 알림을 막음, 잠에서 깬 돌아옴과 알림이 한 wake로 합쳐짐, `scheduled`가 아닌 알림은 말하지 않음, 첫 자유 wake는 30분, 간격이 늘지 않음, 대화 중 타임라인(30분 단계 1 → 59분 메시지 → 60분 막힘 → 69분 단계 2 → 99분 단계 3), 하루 10개(말함·pass 같이), 어제 것은 세지 않음.
- Data(`tests/data/test_wakes.py`, 두 저장소): 남은 `told`는 claim도 miss도 되지 않음, 전에 전달된 뒤 다시 정한 알림이 실패하면 `scheduled`로, 옛 이유의 `asleep`·`again`은 무시.
- 업그레이드(`tests/data/test_sqlite.py`): v2~v18 모두 v19로, v18의 `told` 알림은 `delivered`가 되고 나머지는 그대로.
- 스케줄러(`tests/bootstrap/test_proactive_scheduler.py`): 잠든 동안 알림이 `asleep`으로 막히고 `scheduled`로 남았다가, 깨면 `asleep`/`again` 표시 없이 말하고 `delivered`.
- 설정(`tests/interfaces/test_web_devices.py`): 기본값, 잘못된 값, 옛 변수 무시.

## 8. 작업 단계에서 바꾼 것 (character-work, 2026-11-25, 배포 전)

[작업 설계 §7](character-work-v0.1.md#7-먼저-말-걸기)(H-81)이 이 문서의 §3·§4를 다시 바꾼다. 자세한 것은 [구현에서 정한 것](character-work-v0.1.md#11-구현에서-정한-것-2026-11-25) 15~17번.

| | V3-P107 (지금까지) | 작업 단계 |
| --- | --- | --- |
| 자유 wake가 열리는 때 | 같은 작업(프로그램 묶음)이 30분 이어짐. 작업이 바뀌면 처음부터 | **컴퓨터 앞**(지금 기기가 연결·잠기지 않음·잠 아님, 입력 10분 안)이고 **마지막 wake나 사용자의 마지막 메시지에서 30분**. 작업 종류는 묶지 않는다(있으면 wake 글에 `group`·`minutes`) |
| 자유 wake 열쇠·단계 | `free:<기기>:<작업 시작>:<단계>`, 같은 작업의 몇 번째 | `free:<기기>:<기다림 시작 분>`(기다림마다 한 번), `step`은 사용자의 마지막 메시지 뒤 몇 번째 |
| 답 없는 wake | 사용자가 답할 때까지 HIMARI가 꺼내는 wake를 모두 멈춤 | 사용자의 마지막 메시지 뒤 보인 wake가 **하나면 2시간**, **둘 이상이면** 말할 때까지 |
| 계기 | 알림, 고리, 돌아옴, 작업 끝남, 자유, 기념일 | + **`work_done`**: 작업자가 조각을 남긴 뒤 3시간, 조각마다 한 번(`work:<항목 번호>`), 3단계(HIMARI가 꺼내는 것), 하루 상한 안, `[[pass]]` 가능, 합치는 순서는 기념일 다음·고리 앞 |

- 그대로인 것: 하루 10번(pass 포함), 간격 15분, 23–08시, 대화 중 10분, "조용히", 자유 wake는 혼자 간다(다른 wake가 있으면 기다림). 돌아옴·작업 끝남·알림은 답 없는 wake 규칙을 따르지 않는다.
- 자유 wake는 마지막 메시지에서 30분을 기다리므로 "대화 중 10분"에 막히는 일이 없어졌다.
- 스케줄러는 wake 기록을 사용자의 마지막 메시지까지(7일 안) 읽는다(단계와 답 없는 wake를 세려고). 작업 시작까지 거슬러 읽던 것은 없앴다.
- 프롬프트: `FREE_RULE` 첫 문장이 "사용자가 컴퓨터 앞에 있고 한동안 이야기하지 않았다(group이 있으면 그것이 하는 일)"로 바뀌었고, 기념일 줄 다음에 `WORK_RULE`이 붙는다(자유 wake용 모델만).
- 근거 기록(2026-11-11~25 운영): wake 9번 중 7번이 돌아옴, 자유 wake는 1번(단계 1만), 11-06 뒤로 단계 2에 간 자유 wake가 없었다.
