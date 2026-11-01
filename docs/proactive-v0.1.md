# 먼저 말 걸기와 스케줄러 (V3-P87 설계 초안 v0.1)

상태: **Proposed**, rev. 5. 소유자 결정 Q1~Q9 모두 권고대로(2026-11-01, §15).

**rev. 5(2026-11-01, 재검수 NOT_READY 반영, §18.1):**
- P1: 재시작으로 끊긴 wake의 **끝나지 않은 도구 묶음**이 남은 채 새 wake가 열리면, Driver의 기존 복구(`_resume_pending_group`)가 앞 wake의 도구를 새 wake 안에서 실행한다 → 새 wake는 **정리된 대화**에서만 연다(작업 없음, 끝나지 않은 도구 묶음 없음, 답을 기다리는 호출 없음, 복구가 필요한 상태 아님, 답하지 않은 계기 없음). 확인은 `accept_wake`가 Driver의 lock 안에서 한다. 끊긴 wake의 묶음은 재시작 때 **닫는다**(시작하지 않은 호출은 실행하지 않음, 이미 끝난 효과는 다시 실행하지 않음, 결과를 모르는 호출은 기존 복구 규칙)(§4.4, §6.1).
- P2: 알림을 `delivered`·`scheduled`로 바꾸는 갱신은 `remind_state = claimed`이고 `remind_wake = 그 wake`일 때만. 그 사이 고치거나 지운 알림은 건드리지 않는다(§5.3).

**rev. 4(2026-11-01, 외부 설계 검수 NOT_READY 반영, §18):**
- P1-1: 지난 wake의 그림은 고리를 고치면 길이가 바뀐다. 상태 전체를 다시 더하는 것으로는 여러 wake의 증가분을 덮는다고 보장할 수 없다 → wake가 가리키는 고리의 글을 **`context_key`**에 넣어, 바뀌면 요청 전체를 다시 잰다(§8.4).
- P1-2: 알림의 중복 막기 열쇠는 "전달했다"는 뜻이 아니다 → 알림은 고리 행에 **`scheduled → claimed → delivered / missed`** 상태를 두고, wake가 답 없이 취소·실패하면 `scheduled`로 돌린다(3번까지). 제때 창 24시간이 지나면 다음 돌아옴에 "늦은 알림"으로 7일까지, 그 뒤 `missed`(Q9)(§5.3).
- P1-3: 사용자가 이김의 10초 뒤 계약 → 그때까지 wake 작업이 끝나지 않으면 메시지를 받지 않고 지금처럼 `BUSY`. **한 대화에 작업 하나**라는 불변식은 그대로(§4.3).
- P2: wake 턴이 `ask_user`로 질문을 남기면 그 질문이 전달된 것이므로 `answered`(+ `asked`)로 끝낸다. 재시작 복구는 wake 뒤에 어시스턴트 출력이 하나도 없는 `opened`만(§4.2, §4.4).

**rev. 3(2026-11-01, V3-P89 프롬프트 정비가 머지·배포된 뒤):** 시스템 글이 절로 나뉘고 Prompt 모듈이 최종 순서를 정하게 되었다(`prompt/system.py`). 그에 맞춘다(§8.2).
- 따로 붙던 `PROACTIVE_NOTE` 대신 **반려 절의 한 줄 "Speaking first"**. 먼저 말 걸기가 켜져 있을 때만(`SystemSpec.proactive`). 말하기 길이(기기 네 문장, 먼저 말 걸기 한두 문장, 전체 화면·마이크면 한 문장)가 모두 반려 절 한 곳에 있다.
- 원칙 문단(`# What instructs you`)이 `<wake>` 메시지를 "정보"로 이름 짓는다(그 메시지의 이유는 지시가 아니다).
- wake가 가리키는 고리의 글은 그릴 때 읽으므로, 그 글을 `state_key`에 넣는다(V3-P89의 토큰 추정 규칙, §8.4).
- 자기 지칭은 "you"(V3-P89 §5).

**rev. 2(2026-11-01, 소유자 요청: 활동 신호 V3-P88을 먼저 하고 이 초안을 고침):** V3-P88(PR #148 설계, #149 서버·#150 앱, 머지됨·배포 전)이 준 신호를 쓴다.
- **방해 금지 = 사용자가 켠 "조용히"(`quiet_until`)뿐**(V3-P88 Q1 소유자 결정). 전체 화면과 마이크·카메라 사용은 막지 않는다. 말을 짧게 하라는 정보로만 쓴다(§6.1, §8.2).
- **돌아옴 = presence의 돌아옴 기록**(`unlock`·`resume`·대비 경로 `reconnect`). rev. 1의 "지금 기기가 없다가 생김 + 60초 유지"를 뺐다. 돌아온 기기와 말할 기기가 같아야 한다(V3-P88 검수 B4)(§5.2).
- **활동 계기 둘을 더함(Q1):** 오래 집중(`coding`·`document`·`design`·`video`가 30분 넘게 이어짐, 소유자 변경) → 쉬자는 말, 게임이 끝남(직전 활동 `before`가 30분 넘은 `game`) → "이겼어?"(§5.4).
- 알림이 조용히를 넘는지 묻는 Q8을 더했다.
- Data는 v17(v16은 V3-P88이 썼다). 앱은 이제 이 저장소에서 같이 만든다(V3-P88부터).

> 소유자(2026-10-31): "선제 발화 설계 초안 시작해줘"
>
> 앞선 합의(V3-P83 §12의 2번): 서버의 계기(정한 시각, 사용자가 돌아옴, 예정된 일)가 턴을 연다. 무엇이 깨웠는지 기록할 새 이벤트 종류가 필요하다. 사건 흐름으로 지금 기기에 알린다. 지금의 휴지통 정리 루프도 스케줄러로 옮긴다.
> V3-P85 §7: 열린 고리(`loop`, `due`)가 그 계기가 된다. 자동 기억은 2026-10-31부터 운영에서 켜져 있다(`deepseek/deepseek-v4.1-flash`).

앞선 결정
- **V3-001: Run = LLM 호출 한 번.** 모든 Run에는 계기(`TriggerRef`)가 있다. 지금 계기는 사용자 메시지(`EventRef`)와 끝난 도구 묶음(`CompletedGroupRef`)뿐이다(`driver._next_triggers`).
- **V3-P83 Q6:** 반려 전용 대화는 없다. 앱은 최근 대화(`most_recent_user_conversation`)를 따라간다(§8.2).
- **V3-P83 Q4:** 답은 메시지를 **보낸 기기**에서 캐릭터가 말한다. 앱은 `job` 사건의 보낸 곳이 자기일 때 `live`를 연다(§8.4).
- **V3-P83 §6.1:** 지금 기기는 연결되어 있고 10분 안에 입력이 있었던 기기다. 없으면 "자리 비움"이다. presence는 이것을 메모리에만 둔다.
- **V3-P85 §7, §8:** 열린 고리는 `user_memory.loops`로 매 턴 들어간다. 지난 것에는 `"overdue": true`가 붙는다. 지금은 사용자가 말을 걸었을 때만 모델이 고리를 본다.
- **V3-P62:** 아무도 읽지 않으면 작업은 20 Run 뒤 멈춘다. 기기의 `live` 흐름이 열려 있으면 `seen`이다(V3-P83 §8.3).
- **V3-P88 활동 신호:** presence가 기기마다 세션(잠금), 지금 하는 일(분류·프로그램·이어 한 시간·전체 화면), 직전 활동 하나(30분), 재생 중인 미디어, 마이크·카메라를 메모리에 둔다. 모델은 Current State `presence.now_doing`으로 본다. 잠금 해제·깨어남은 60초 흔들림 막기를 넘어 바로 지금 기기가 되고 **돌아옴 기록** `Returned(device_id, at, via)`로 남는다. 사용자의 **조용히**(`quiet_until`, 주체 단위, Data v16)가 유일한 방해 금지다.
- **V3-P85 §4.6:** 기억 작업은 대화의 Run이 아니다(lock을 잡지 않아 `BUSY`를 만들지 않는다). 이 패킷의 먼저 말 걸기는 반대로 **대화의 Run**이다. 사용자가 보는 대화에 HIMARI의 말이 남아야 하기 때문이다.

## 1. 왜, 그리고 범위

반려 에이전트는 사용자가 말을 걸 때만 움직인다. 열린 고리("11월 4일 오후에 면접")를 기억해도, 사용자가 먼저 말하기 전에는 "면접 어땠어?"라고 묻지 못한다. 이 패킷은 **서버가 턴을 여는 길**을 만들고, 그 길을 쓰는 계기들을 붙인다. V3-P88의 활동 신호 덕분에 "언제"를 사용자가 실제로 무엇을 하는지에 맞출 수 있다.

**이 패킷에서 한다**
1. **새 이벤트 `wake`:** 서버가 연 턴의 계기. 무엇이 깨웠는지 담는다(§3).
2. **턴 열기:** `wake`를 받아 그 대화의 작업을 시작하고, 답을 지금 기기에서 말하게 한다(§4).
3. **계기(Q1):**
   - **열린 고리의 날짜가 지남:** "면접 어땠어?"(§5.1)
   - **사용자가 돌아옴:** 화면 잠금을 풀거나 PC를 깨워, 오래 비웠던 자리로 돌아오면 인사한다(§5.2)
   - **알림:** 사용자가 "30분 뒤에 알려 줘"라고 부탁한 것(§5.3)
   - **활동:** 오래 쉬지 않고 집중함, 게임이 끝남(§5.4)
4. **조절 규칙:** 언제 말하지 않을지. 사용자의 조용히, 조용한 시간, 간격, 하루 상한, 답 없는 말 위에 또 걸지 않기(§6).
5. **스케줄러:** 계기를 살피는 주기 작업. 휴지통 정리 루프도 여기로 옮긴다(§7).
6. **화면:** Web의 표시, 앱 계약의 바뀐 부분(§9, §10).

**하지 않는다(뒤 단계, §14)**
- 화면을 보고 반응해 말 걸기(V3-P84 2단계의 모델이 직접 하는 화면 캡처가 필요하다). 활동 신호(분류·미디어)로 할 수 있는 것만 이번에 한다.
- 이유 없이 심심해서 하는 잡담
- 작업 중 메시지(`BUSY` 대신 받아 두기). 이 패킷은 먼저 말 거는 턴에 대해서만 "사용자가 이긴다"(§4.3)를 둔다.
- Windows 알림 센터(토스트), 앱이 꺼져 있을 때 알리기
- Web만 쓰는 사용자에게 먼저 말 걸기(Web에는 지금 기기 개념이 없다)

## 2. 구조

```
   presence (V3-P88)                       Data
     지금 기기, 활동 신호, 직전 활동,          열린 고리, 알림, wake 기록,
     돌아옴 기록                               조용히(v16), 마지막 입력(v17)
              │                                  │
              └──────────┬───────────────────────┘
                         ▼
        bootstrap 스케줄러: 30초마다 + 돌아옴 기록이 생기면 바로
           │  himari.proactive (순수 함수): 계기 모으기 → 조절 규칙 → 고르기
           ▼
        Control: runner.submit_wake(scope, wake)
           │  Data: wake 이벤트 + wake 기록 + 중복 막기 열쇠 (한 트랜잭션)
           ▼
        Driver: 보통의 턴 (계기 = wake 이벤트), Run 상한 3
           │
           ▼
        사건 흐름 job {origin: wake, device_id} → 그 기기의 앱이 live를 열고 캐릭터가 말함
```

- **서버가 "언제"를, 모델이 "무엇을"** 정한다. 말할지 말지는 규칙이 정한다(결정적이고 시험할 수 있다). 모델은 말하는 내용만 정한다(Q7).
- 대화에 남는 것은 `wake` 이벤트 하나와 HIMARI의 답이다. 나중에 사용자가 답하면 보통의 대화로 이어진다.

## 3. `wake` 이벤트 (Data v17)

### 3.1 왜 새 이벤트 종류인가

사용자 메시지에 "서버가 보냄" 표시를 붙이는 길도 있다. 그러지 않는다.
- 기억 작업은 사용자 메시지를 **사용자의 말(근거)**로 읽는다. 서버가 쓴 글이 사용자의 말이 되면 안 된다.
- `sent_from`, `most_recent_user_conversation`, 대화 검색, 시각 줄의 간격이 모두 "사용자 메시지"를 센다. 하나라도 빠뜨리면 조용히 틀린다.
- 새 종류면 모든 소비자가 **명시적으로** 다룬다. 다루지 않은 곳은 시험에서 드러난다.

### 3.2 모양

```python
class EventKind(Enum):
    ...
    WAKE = "wake"


@dataclass(frozen=True)
class WakeReason(DeepFrozen):
    kind: WakeKind              # LOOP_DUE | RETURNED | REMINDER | FOCUS | GAME_ENDED | TRY
    memory_id: int | None = None  # LOOP_DUE, REMINDER
    away_minutes: int | None = None  # RETURNED
    via: str | None = None        # RETURNED: "unlock" | "resume" | "reconnect"
    activity: str | None = None   # FOCUS: the category ("coding", "document", "design")
    minutes: int | None = None    # FOCUS: how long it has gone on; GAME_ENDED: how long the game lasted
    late: bool = False            # REMINDER only: past its 24-hour window, told at a later return (§5.3)


@dataclass(frozen=True)
class Wake(DeepFrozen):
    wake_id: str
    reasons: tuple[WakeReason, ...]  # 하나 이상. 같은 순간의 계기는 한 wake로 합친다(§6.4)
    device_id: str                    # 말할 기기(지금 기기)
```

- 고리의 글과 날짜는 이벤트에 **복사하지 않는다.** 번호만 둔다. 모델에게 그릴 때 그 순간의 기억을 읽는다(§8.1). 고리가 지워졌으면 "지워진 고리"로 그린다.
  - 이유: 기억은 사용자가 지울 수 있어야 한다(V3-P71 D5). 이벤트에 글을 복사하면 지운 기억이 대화 기록에 남는다.
- `TRY`는 운영자가 시험으로 연 것이다(§11).
- `wake`는 Run의 계기가 된다. `_next_triggers`는 마지막 어시스턴트 출력 뒤의 `wake`도 `EventRef`로 모은다.

### 3.3 Data v17

- `events.kind`에 `wake`.
- `memories`에 `remind_at`(쓴 그대로), `remind_instant`, `remind_state`(`scheduled`·`claimed`·`delivered`·`missed`), `remind_wake`, `remind_attempts`, `reminded_at`(§5.3). 고리(`loop`)에만.
- `devices`에 `last_input_at`. `last_seen_at`과 같이 10분에 한 번, 그리고 잠기거나 자리 비움이 될 때 쓴다(§5.2: 자리 비움 길이를 재시작에도 잰다).
- 새 표 `wakes`:

| 열 | 뜻 |
| --- | --- |
| `wake_id` | `w<숫자>` |
| `principal_id`, `conversation_id`, `event_sequence` | 어느 대화의 어느 이벤트 |
| `reasons` | JSON(§3.2) |
| `device_id` | 말한 기기 |
| `status` | `opened` → `answered` / `cancelled` / `failed` (§4.2) |
| `asked` | `answered`가 `ask_user` 질문으로 끝났으면 1 |
| `created_at`, `ended_at` | |

- 새 표 `wake_keys`(주체, 열쇠) → `wake_id` 또는 `expired`. **기본 키가 중복을 막는다.** 열쇠는 §5의 각 계기가 정한다. **알림은 열쇠를 쓰지 않고** 고리 행의 전달 상태로 따라간다(§5.3, 검수 P1-2).
- **`begin_wake`(한 트랜잭션):** 열쇠를 넣고(이미 있으면 `DUPLICATE`), 실은 알림을 `scheduled → claimed`로 바꾸고(아니면 `DUPLICATE`), `wake` 이벤트를 붙이고, `wakes` 줄을 쓴다. 대화가 휴지통·삭제 중이면 거절. 그래서 재시작이 끼어도 같은 고리로 두 번 말하지 않는다.
- v16 → v17 업그레이드는 앞선 업그레이드처럼 백업 후 한다.

## 4. 턴 열기

### 4.1 `submit_wake`

`BackgroundRunner.submit_wake(scope, wake)`:
1. 대화가 닫혔거나(`CLOSED`) 붙잡혀 있거나 작업 중이면(`BUSY`) 거절한다. 스케줄러는 이번 틱을 건너뛴다.
2. `driver.accept_wake`가 `begin_wake`를 부른다(사용자 메시지의 `accept`와 같은 자리, 같은 lock).
3. `_start(..., "wake", origin=MessageOrigin(WAKE, device_id))`. `JobListener.job_started`가 사건 흐름에 `job`을 낸다.

`OriginKind`에 `WAKE`를 더한다. 사용자 메시지의 보낸 곳에는 쓰지 않고, 작업의 보낸 곳에만 쓴다.

### 4.2 턴의 모양

- 모델: 그 대화의 모델(V3-P64). 따로 고르지 않는다.
- **Run 상한 3**(그 턴만). 먼저 말 거는 말은 짧아야 하고, 도구를 오래 쓰면 사용자 모르게 비용이 든다. 도구는 그대로 쓸 수 있다(예: 날씨 검색).
- `ask_user`는 쓸 수 있다. 답을 기다리는 대화가 되면 그 대화에는 다음 wake를 걸지 않는다(§6.1).
- 끝나면 `wakes.status`를 바꾼다(검수 P2):
  - `answered`: 최종 답이 생김, **또는** `ask_user`로 질문을 남기고 답을 기다리게 됨(`asked = 1`을 함께 씀). 말을 건 것은 전달되었다. 그 뒤 기다림은 지금의 `ask_user` 대기(V3-P80의 waiting 대화) 그대로이고, §6.1이 그 대화에 다음 wake를 막는다.
  - `cancelled`: 어시스턴트 출력 없이 취소됨(사용자가 이김, §4.3).
  - `failed`: 오류, 또는 재시작 뒤 이어 가지 않음(§4.4).

### 4.3 사용자가 이긴다 (Q6)

wake 작업이 도는 중에 그 대화에 사용자 메시지가 오면, 지금은 `BUSY`가 된다. HIMARI가 먼저 말을 꺼냈다고 사용자가 말을 못 하는 것은 거꾸로다.

- `submit`이 돌고 있는 작업이 `wake`이면: 그 작업에 취소를 요청하고(`StopSignal.cancel`, 진행 중인 공급자 호출과 도구는 Driver의 `_interruptible`이 바로 끊는다) **끝나기를 10초까지 기다린다.**
  - 끝났으면 메시지를 받고 새 작업을 시작한다.
  - **10초 안에 끝나지 않으면 메시지를 받지 않고 지금처럼 `BUSY`를 돌려준다**(검수 P1-3). 메시지는 저장되지 않는다. 앱과 Web은 지금 `BUSY`를 다루는 그대로(앱: 입력 유지, "이 대화는 작업 중입니다"). wake 작업은 취소 요청을 받은 채 곧 끝난다.
  - **불변식: 한 대화에서 작업은 하나다.** 새 작업은 앞 작업이 끝난 것을 확인한 뒤에만 시작한다. 그래서 wake 작업의 최종 답이 사용자 메시지 뒤에 기록되는 일은 없다(받아들임은 Driver의 lock 안에서 앞 작업이 끝난 뒤).
- 취소된 wake의 답이 이미 확정됐으면 그대로 남는다. 확정 전이면 wake 이벤트만 남고, 다음 Run의 계기에 wake와 사용자 메시지가 함께 들어간다. 반려 절의 "Speaking first" 줄이 "사용자가 그 뒤에 말했으면 사용자에게 답하라"고 말한다(§8.2).
- 다른 작업(사용자가 시킨 일)은 지금처럼 `BUSY`다. 일반적인 해결은 "작업 중 메시지" 패킷이다.

### 4.4 재시작

- `wake` 이벤트는 남고 작업은 사라진다. 시작할 때 스케줄러가 `opened`인 wake를 본다.
  - **그 wake 뒤에 어시스턴트 출력(최종 답, 도구 호출, `ask_user`)이 이미 있으면** 이어 가지 않는다. 최종 답이나 질문이 있으면 `answered`로 적는다(검수 P2: 답을 기다리는 질문을 다시 열지 않는다).
  - **끝나지 않은 도구 묶음이 있으면(재검수 P1)** 스케줄러가 Driver에게 그 묶음을 **닫게** 한다(`_close_latest_group`, 이유 "the server restarted while HIMARI was speaking first"). 그 뒤 wake를 `failed`로 적는다.
    - 시작하지 않은 호출(`PENDING`)은 **실행하지 않는다**. 실패한 먼저 말 걸기의 도구가 나중에 다른 턴 안에서 실행되지 않게 하려는 것이다.
    - 이미 끝난 호출의 효과와 결과는 그대로 남고 **다시 실행하지 않는다**(실행 기록이 있음).
    - 결과를 모르는 호출(시작했지만 결과가 없음)은 지금의 규칙대로(V3-047) 모른다고 보고되고, 필요하면 대화가 복구 필요 상태(`RECOVERY_REQUIRED`)가 된다. 그 상태가 풀리기 전에는 그 대화에 새 wake를 열지 않는다(§6.1).
  - 출력이 하나도 없고, 10분 안에 열렸고, 조절 규칙이 아직 통과하면 이어 간다(`submit(None)`, 계기는 그 wake).
  - 아니면 `failed`로 바꾸고 둔다. 사용자가 다음에 말하면 그 wake도 계기에 들어가고, 모델은 사용자에게 답한다(§8.2).
  - 알림을 실은 wake가 `failed`·`cancelled`가 되면 그 알림은 §5.3대로 다시 `scheduled`가 된다.

## 5. 계기

모든 계기는 **후보**를 낸다. 후보는 조절 규칙(§6)을 지나야 wake가 된다.

### 5.1 열린 고리의 날짜가 지남

- 대상: 물러나지 않은 `loop` 기억 가운데 `due_instant`가 있는 것. 제안(아직 고르지 않은 것)은 기억이 아니므로 빠진다.
- **말할 시각(Q4):**
  - 시각까지 있는 고리(`2026-11-04T14:00+09:00`): `due + 1시간`. 일이 끝났을 때쯤 "어땠어?"
  - 날짜만 있는 고리(`2026-11-04`): 그날 18:00(그 고리를 쓸 때의 시간대).
- **창:** 말할 시각에서 24시간 안에만 후보다. 넘으면 열쇠를 `expired`로 넣는다. 배포 직후 지난 고리가 한꺼번에 깨어나지 않는다.
- **열쇠:** `loop:m<번호>:<due_at>`. 날짜를 고치면 열쇠가 바뀌므로 다시 한 번 말할 수 있다.
- 고리를 닫는 것은 지금처럼 기억 작업과 모델이다. 사용자가 "잘 봤어"라고 답하면 기억 작업이 근거와 함께 `forget`한다.

### 5.2 사용자가 돌아옴

- **돌아옴:** presence의 돌아옴 기록 `Returned(device_id, at, via)`(V3-P88 §3). 잠금 해제(`unlock`)나 깨어남(`resume`)으로 그 기기가 지금 기기가 될 때, 또는 사라졌던 기기가 다시 이어져 지금 기기가 될 때(`reconnect`, 앱의 표시를 잃었을 때의 대비) 생긴다. 앱의 신호가 사건 흐름보다 먼저 와도 기록된다(V3-P88 구현 검수 B2).
- **자리 비움 길이:** `at` − **돌아오기 전 모든 기기의 마지막 입력 중 가장 늦은 것**(Data의 `devices.last_input_at`, v17). presence의 메모리가 아니라 Data에서 재므로, 서버를 재시작해 기기들이 다시 붙은 `reconnect`는 짧게 재져 인사가 되지 않는다. 10분 단위로 쓰므로 많아야 10분 짧게 잰다.
- **조건(Q3):**
  - 자리 비움이 `RETURN_HOURS`(4시간) 이상
  - 그동안 어느 대화에도 사용자 메시지가 없었음(Web이나 CLI로 말했으면 이미 돌아온 것)
  - 기록이 10분 안에 생겼음
  - **돌아온 기기 = 그 순간의 지금 기기**(V3-P88 검수 B4). 그 사이 다른 PC로 옮겼으면 버린다.
  - 기록이 생긴 뒤 **20초**를 기다린다. 잠금을 풀자마자 사용자가 먼저 앱에 말을 걸면(그 20초 안의 사용자 메시지) 인사하지 않는다. rev. 1의 "60초 유지 + 입력" 규칙은 필요 없다(잠금 해제는 사람이 앉았다는 분명한 행동이다).
- **열쇠:** `return:<기기>:<기록의 at, 분>`. 같은 돌아옴에 두 번 인사하지 않는다.

### 5.3 알림

사용자가 시각을 정해 부탁한 것: "30분 뒤에 알려 줘", "내일 9시에 약 먹으라고 해 줘".

- **고리에 붙인다:** 알림은 "HIMARI에게 부탁한 다음 일"이라서 이미 열린 고리의 뜻 안에 있다. 새 저장소를 만들지 않고 `loop` 기억에 `remind_at`을 더한다.
  - 기억 화면의 고리 목록에서 보이고, 고치고, 지울 수 있다(V3-P85 §10.2를 넓힘).
  - `due`(일이 있는 때)와 `remind_at`(말해 줄 때)은 다르다. 둘 다 있을 수 있다.
- **누가 쓰나:** 모델의 `memory_save`(`loop`, 새 인자 `remind_at`)와 사용자(기억 화면). **자동 기억은 쓰지 않는다.** 알림은 사용자가 그 턴에서 부탁한 것이어야 한다. `memory_save` 설명에 "remind_at only when the user asked to be reminded at a time"을 더한다(V3-P89 뒤로 저장 규칙은 도구 설명에 있다. 기억 절에는 출처 규칙만).
  - 상대 시각("30분 뒤")은 모델이 `now`(V3-P83 §6.4)로 계산해 절대 시각으로 쓴다. 날짜 규칙은 V3-P85 §5.3과 같다(시간대 없는 시각은 서버 시간대).
- **말할 시각:** `remind_at` 그대로.
- **조절 규칙의 예외:** 사용자가 정한 시각이므로 조용한 시간, 간격, 하루 상한을 보지 않는다. 사용자의 조용히를 넘을지는 Q8. **"사용자가 대화 중"(§6.1, 10분)도 보지 않는다**: 대화하던 중이면 그 작업이 끝난 뒤 바로 말한다("참, 9시에 약 드시라고 하셨죠"). 작업 중이면 기다린다. **지금 기기는 필요하다.**
- **전달 상태(검수 P1-2):** 알림은 `wake_keys`(중복 막기)가 아니라 고리 행의 상태로 따라간다. "wake를 열었다"와 "전달했다"는 다르기 때문이다.

| 상태 | 뜻 | 다음 |
| --- | --- | --- |
| `scheduled` | 말할 때를 기다림 | `remind_at`이 지나고 조건이 맞으면 `begin_wake`가 같은 트랜잭션에서 `claimed`로(`state = scheduled`일 때만, 그래서 두 wake가 같은 알림을 싣지 않는다) |
| `claimed` | 이 wake(`remind_wake`)가 싣고 있음 | wake가 `answered` → `delivered`(`reminded_at`). `cancelled`·`failed` → `scheduled`로 되돌리고 `remind_attempts + 1`. 3번이 되면 `missed` |
| `delivered` | 전달함 | 끝. 고리는 남는다(일이 끝났는지는 모름) |
| `missed` | 전달하지 못함 | 끝. 기억 화면의 고리에 "Missed reminder at …"으로 보인다 |

  - `remind_at`을 고치면 상태는 `scheduled`, 시도 횟수는 0으로 돌아간다(그리고 `remind_wake`를 비운다). 지우면 상태도 없어진다.
  - **경합(재검수 P2):** wake가 끝날 때의 갱신(`delivered` 또는 `scheduled`로 되돌림)은 **`remind_state = claimed AND remind_wake = 끝나는 wake`**일 때만 한다. 그 사이 사용자가 알림을 고쳤거나(`scheduled`, `remind_wake` 비움) 고리를 지웠으면 그 갱신은 아무것도 바꾸지 않는다. 그래서 고친 알림이 옛 wake 때문에 `delivered`가 되지 않는다.
  - **제때와 늦은 알림(Q9):** `remind_at`에서 24시간 안이면 제때 말한다. 그 안에 지금 기기가 없어서 말하지 못했으면, 7일 안의 **다음 돌아옴 기록**에서 "늦은 알림"으로 말한다(이유에 `"late": true`, 모델은 "어제 9시에 알려 달라던 거, 그때 못 전했어"처럼). 7일이 지나면 `missed`. 늦은 알림도 조절 규칙의 예외는 같다.
  - 서버가 `claimed`인 채로 재시작되면, 그 wake의 상태(§4.4)를 따라 `delivered` 또는 `scheduled`로 정리한다.
  - **`delivered`의 뜻(재검수 의견, 이 패킷의 한계):** wake 턴의 최종 답이나 질문이 **대화에 기록되었다**는 뜻이다. 앱이 그 말풍선을 실제로 띄웠는지(또는 뒤에 음성으로 말했는지)는 확인하지 않는다. 답이 만들어지는 중에 기기 연결이 끊기면 알림은 `delivered`로 적히지만 사용자는 못 볼 수 있다. 앱은 다시 연결되면 지금처럼 자기 기기의 끝난 작업 답을 복구해 띄운다(V3-P83 §9, 앱의 "늦은 답 복구"). 표시 확인(앱이 "봤다"를 알림)은 뒤 단계다(§14).

### 5.4 활동 (V3-P88 신호, Q1)

지금 기기의 신호(`DeviceState`의 `activity`·`before`)를 본다. 신호가 없으면(옛 앱, 앱 설정에서 끔) 이 계기는 없다.

- **오래 집중함(`FOCUS`):** 지금 활동이 `coding`·`document`·`design`·**`video`**(소유자 변경, 2026-11-01)이고 `for_seconds`가 `FOCUS_MINUTES`(30분, 소유자 변경 2026-11-01. 처음 권고 120분) 이상. 앱의 60초 규칙 덕분에 메신저를 잠깐 본 것으로는 끊기지 않는다(V3-P88 §4.1).
  - 영상도 오래 이어 보면 계기가 된다. 이유의 `activity`가 `video`이므로 모델은 "일"이 아니라 "오래 봤다"는 쪽으로 말한다(예: "한참 보고 있네, 눈 좀 쉬어?"). 영상이 전체 화면이면 "Speaking first" 줄대로 한 문장이다(§8.2). 방해 금지는 아니다(V3-P88 소유자 결정 그대로).
  - 열쇠: `focus:<기기>:<그 활동이 시작된 시각, 분>`(= 지금 − `for_seconds`). 한 번 이어지는 집중에 한 번만(30분째에 한 번 말하고, 그 집중이 계속되어도 다시 말하지 않음). 쉬었다(분류가 바뀌어) 다시 30분이면 새 열쇠다.
  - 모델에게: `{"kind": "focus", "activity": "coding", "minutes": 34}`. 쉬자는 말 한마디. 같은 턴의 Current State `presence.now_doing`에도 지금 활동과 `for_minutes`가 있다(V3-P88 §7).
- **게임이 끝남(`GAME_ENDED`):** 직전 활동 `before`가 `game`이고 `lasted_seconds`가 `GAME_MINUTES`(30분) 이상이며, 끝난 지 10분 안.
  - 열쇠: `game:<기기>:<끝난 시각, 분>`.
  - 모델에게: `{"kind": "game_ended", "minutes": 95}`. "이겼어?" 같은 한마디.
- 둘 다 알림이 아닌 계기라서 조절 규칙(§6)을 모두 따른다. 창은 FOCUS는 그 집중이 이어지는 동안, GAME_ENDED는 끝난 뒤 10분.
- **하지 않는 것:** 같은 노래 반복(V3-P88 §4.4에서 뺌), 창 제목·미디어 제목의 내용으로 계기를 만드는 것. 제목은 제3자의 글이라 계기로 쓰지 않는다(V3-P88 §7).

## 6. 조절 규칙 (Q5)

### 6.1 말하지 않는 때 (모든 계기)

- `HIMARI_PROACTIVE`가 꺼져 있음(기본 꺼짐). `HIMARI_COMPANION`도 켜져 있어야 한다.
- **지금 기기가 없음.** 앱이 없거나 자리 비움(잠긴 기기는 지금 기기가 아니다, V3-P88 §3).
- **사용자의 조용히**(`quiet_until`이 지금보다 뒤). 알림은 Q8을 따른다.
- **막지 않는 것(V3-P88 소유자 결정):** 전체 화면(영상·게임·작업)과 마이크·카메라 사용. 영상을 보며, 전체 화면 작업 중에 대화할 수 있다. 이때는 말을 더 짧게 한다(§8.2).
- **사용자가 대화 중:** 어느 대화든 마지막 사용자 메시지가 10분 안. 그때는 고리가 이미 `user_memory`에 있으므로 모델이 대화 안에서 자연스럽게 꺼낼 수 있다. **알림은 예외**(§5.3, 작업이 끝난 뒤 바로).
- **어느 대화든 작업 중**(주체의 작업이 하나라도 돎). 일하는 중에 끼어들지 않는다.
- **고른 대화(§6.3)가 정리되지 않음(재검수 P1):** 답을 기다리는 호출(`ask_user`)이 있음, 끝나지 않은 도구 묶음이 있음(재시작으로 끊긴 것 포함), 복구가 필요한 상태(`RECOVERY_REQUIRED`, 결과를 모르는 호출), 답하지 않은 계기(사용자 메시지나 wake)가 있음, 붙잡혀 있음, 휴지통에 있음.
  - 이 확인은 스케줄러가 미리 한 번, 그리고 **`accept_wake`가 Driver의 lock 안에서 다시 한 번** 한다(그 사이 사용자가 말하거나 복구가 생겨도 새 wake가 섞이지 않게). 정리되지 않았으면 `begin_wake`를 부르지 않고 `NOT_SETTLED`로 돌려준다. 스케줄러는 이번 틱을 건너뛴다(열쇠·알림 상태를 건드리지 않음).
- **답 없는 말 위에 또 걸지 않는다:** 고른 대화의 마지막 이벤트가 wake 턴의 최종 답이면(사용자가 아직 답하지 않음), 알림이 아닌 계기는 기다린다.

### 6.2 알림이 아닌 계기에만

- **조용한 시간:** 기본 23:00–08:00, 지금 기기의 시간대(없으면 `HIMARI_TIME_ZONE`). 조용한 시간에 생긴 후보는 창 안에 있으면 끝난 뒤에 말한다.
- **간격:** 앞의 wake(알림 포함)에서 `GAP_MINUTES`(30분, 소유자 변경 2026-11-01) 안이면 기다린다.
- **하루 상한:** 지금 기기의 시간대로 하루 `DAILY`(4번). 알림은 세지 않는다.

### 6.3 어느 대화에서 (Q2)

- **최근 대화**(`most_recent_user_conversation`). 앱이 따라가는 대화와 같으므로, 사용자가 앱을 열었을 때 보던 곳에 말이 이어진다.
- 그 대화가 §6.1에 걸리면 이번 틱은 건너뛴다. 다른 대화로 옮기지 않는다(앱이 보는 곳과 어긋난다).

### 6.4 합치기와 순서

- 같은 틱에 후보가 여럿이면 **한 wake로 합친다**(돌아옴 + 지난 고리 둘 → 인사 한 번에 둘을 꺼냄). 따로 말하면 연달아 세 번 말을 건다.
- 한 wake의 이유는 4개까지. 알림 → 고리(말할 시각 순) → 활동 → 돌아옴 순서. 넘는 것은 다음 기회로 넘긴다(창 안이면).

## 7. 스케줄러

- **자리:** `bootstrap/schedule.py`의 `Ticker`. 이름 붙인 주기 작업을 돌린다.
  - `purge`: 지금의 `_purge_periodically`(1시간)를 옮긴다. 동작은 같다.
  - `proactive`: 30초마다. 그리고 presence가 돌아옴 기록을 만들면 바로 한 번(§5.2의 20초 대기를 시작하려고). presence에 `on_returned` 콜백을 더한다(bootstrap이 잇는다).
  - 기억 작업자(V3-P85)는 사건으로 움직이고 자기 스캔이 있으므로 옮기지 않는다.
- **한 작업이 실패해도** 다른 작업과 다음 틱은 돈다(지금의 정리 루프와 같음). 실패는 로그에 남는다.
- **순수 함수와 바깥:** 계기 모으기, 조절 규칙, 고르기, 합치기는 `himari.proactive`의 순수 함수다(시계와 상태를 인자로 받음). `Ticker`가 Data·presence·runner를 읽고 결과를 실행한다. 시험은 시계를 바꿔 가며 순수 함수를 돌린다.
- **주체:** 지금은 `operator` 하나지만, 함수는 주체 단위로 쓴다.

## 8. 모델에게 가는 것

### 8.1 wake를 그리는 법

`wake`는 사용자 역할의 메시지로 그린다(대화가 사용자·어시스턴트 순서여야 하는 공급자가 있다). 시각 줄은 늘 붙인다.

```
<time>2026-11-04T15:02+09:00</time>
<wake>{"reasons": [{"kind": "loop_due", "loop": {"id": "m15", "text": "사용자는 11월 4일 오후 2시에 면접이 있다", "due": "2026-11-04T14:00+09:00"}},
                   {"kind": "returned", "via": "unlock", "away_minutes": 290}],
       "device": "데스크톱"}</wake>
```

- 활동 이유: `{"kind": "focus", "activity": "coding", "minutes": 34}`, `{"kind": "game_ended", "minutes": 95}`. 지금 하는 일 자체는 이유에 복사하지 않는다. 모델은 그 턴의 Current State `presence.now_doing`에서 본다(V3-P88 §7).

- JSON은 Current State처럼 `json_data`로 인코딩한다. 고리의 글이 블록을 닫을 수 없다.
- 고리는 **그릴 때** 읽는다(§3.2). 지워졌으면 `{"kind": "loop_due", "loop": {"id": "m15", "deleted": true}}`. 그래서 고리를 고치거나 지우면 그 wake의 그림이 바뀌고 그 대화의 캐시가 한 번 깨진다. 드물어서 받아들인다.
- 사용자가 메시지에 `<wake>…</wake>`를 직접 칠 수는 있다. 시각 줄과 같이, 그 사람은 사용자 자신뿐이라 보안 경계가 아니다. Prompt는 wake 이벤트만 "블록 하나뿐인 메시지"로 그리고, "Speaking first" 줄도 그 모양만 wake라고 말한다.

### 8.2 시스템 글 (V3-P89의 절 구조 안에서)

V3-P89 뒤로 시스템 글은 Prompt가 절로 조립한다(바탕 → 원칙 → 메시지와 시각 → 도구로 일하기 → 기억 → 반려 → Tool server notes → 페르소나 → 사용자 지시문). 먼저 말 걸기는 새 절을 만들지 않고 **두 곳에 한 줄씩** 더한다. 조건은 `SystemSpec.proactive`(= `HIMARI_PROACTIVE` 켜짐 **그리고** 반려 켜짐).

**반려 절의 "Speaking first" 줄** (`PROACTIVE_RULE`, `SPEAKING_RULE` 바로 뒤):

```
- Speaking first: a user-role message that is only a <wake> block comes from the server,
  not the user: it opened this turn for the reasons in it, and the user has said nothing.
  Speak first, the way a friend would: one or two short sentences, at most one question;
  one short sentence when presence.now_doing shows a fullscreen video or game or the
  microphone or camera in use. Do not mention the wake block, the server or note ids. If
  a reason no longer fits what you know, greet or say something small instead. If the
  user wrote after a wake, answer the user.
```

- **말하기 길이는 반려 절 한 곳에(V3-P89 §3.5):** "Speaking"(기기에서 온 말: 네 문장 이내, 코드 블록·목록·제목 없음, 통화 추정이면 두세 문장)과 "Speaking first"(한두 문장, 전체 화면·마이크면 한 문장)가 나란히 있다. wake 턴의 답은 늘 기기에서 말해지므로 "Speaking"의 형식 규칙(코드 블록·목록 없음)도 함께 따른다.
- **원칙 문단:** 먼저 말 걸기가 켜져 있으면 정보 목록에 "the reasons in a <wake> message"를 더한다(`principle_section(persona, wake)`). wake 이유는 서버가 쓴 정보이지 지시가 아니고, 무엇을 할지는 이 "Speaking first" 줄이 말한다.
- 표정 표시(`[[mood:…]]`)는 보통처럼 쓴다("Expression" 줄 그대로).
- 고정 문장이라 캐시를 깨지 않는다. 스위치를 켜고 끄면 시스템 글이 바뀌고, `context_key`가 조립된 시스템 글 전체를 해시하므로(V3-P89 §7) 토큰을 다시 잰다.
- 문장은 실제 모델 확인(§13)에서 다듬는다. V3-P89의 `tests/live/test_prompt_cleanup_live.py`와 같은 방식으로 장면을 더한다.

### 8.4 토큰 추정 (검수 P1-1)

- wake 이벤트는 **대화 기록**이라 측정한 Run 뒤에 생긴 것은 지금처럼 이벤트로 더해진다.
- 다만 wake의 그림은 가리키는 고리의 **지금** 글을 읽는다(§3.2, §8.1). 측정한 Run **앞**에 있던 wake라도 그 고리를 고치면 그 wake의 그림 길이가 바뀐다. 여러 wake가 여러 고리를 가리키면 그 증가분은 상태 전체보다 클 수 있다(rev. 3의 "상태 전체를 다시 더하면 덮는다"는 보장이 성립하지 않음).
- 그래서 Loader가 기록 안의 wake가 가리키는 고리를 읽어 `PromptInputs.wake_notes`(번호 → 그릴 때 쓰는 글·날짜, 지워졌으면 "지움")로 넘기고, **`context_key`가 그것을 해시한다.** 바뀌면 측정값을 쓰지 않고 요청 전체를 다시 잰다(V3-P89 `Compactor.estimate`의 첫 갈래). 고리를 고치는 일은 드물어서 비용이 작다.
- 압축 요약 뒤로 접힌 wake는 요약 안의 글이라 해당 없다. `wake_notes`는 지금 입력에 남은 wake의 것만이다.

### 8.3 다른 소비자

| 소비자 | wake를 |
| --- | --- |
| 시각 줄의 간격(V3-P83 §6.4) | 대상이 아님(사용자 메시지 간격만 셈). wake 자체에는 늘 붙임 |
| `sent_from` | 건너뜀 |
| `most_recent_user_conversation` | 세지 않음 |
| 기억 작업(V3-P85) | 문맥 한 줄 `[HIMARI spoke first: loop m15 due, user returned]`로 그림. 번호(`seq`)는 있지만 **사용자 근거가 될 수 없다**(역할이 사용자 메시지가 아니므로 지금 검사로 떨어짐). 시험으로 지킨다 |
| 대화 검색(V3-P72) | 글이 없음(검색되지 않음) |
| 압축 요약 | 문맥 한 줄로 그림 |
| Web·CLI 기록 | §9 |

## 9. Web과 CLI

- **대화 화면:** wake 이벤트는 말풍선이 아니라 흐린 한 줄로 그린다. UI는 영어를 유지한다.
  - `HIMARI reached out · open loop: 면접 · you were away 4h 50m`
  - 고리 이름을 누르면 기억 화면의 그 고리로 간다.
- **기억 화면 > Loops:** 고리마다 "Checks in after Nov 4, 15:00" / "Reminds you at …"을 보이고, 알림 시각을 넣고 지울 수 있다. 이미 말했으면 "Checked in Nov 4".
- **설정 > Devices 아래:** `Reaching out: on · quiet 23:00–08:00 · today 1 of 4`. 꺼져 있으면 이유(`off (HIMARI_PROACTIVE=0)`).
- **CLI:** wake를 `· HIMARI reached out (…)`로 찍는다. `himari proactive status`, `himari proactive try`(§11).

## 10. 앱 계약 (바뀐 부분, V3-P88처럼 이 저장소에서 같이 만든다)

- `job` 사건의 `origin`에 새 모양: `{"kind": "wake", "device_id": "…"}`.
- **`device_id`가 자기이면:**
  - 지금 보는 대화가 그 대화가 아니면 그 대화로 옮긴다. 사용자가 입력 중이면 옮기지 않고, 말풍선만 띄운다(누르면 옮김).
  - `live`를 열고 캐릭터가 말한다(지금 자기가 보낸 작업과 같게).
- 자기가 아니면 기록만 갱신한다(V3-P83 Q4와 같은 규칙).
- `hello`의 활동에도 같은 `origin`이 실린다.
- 사용자가 wake의 말에 답하면 보통의 메시지다. 앱이 따로 할 일은 없다.
- 서버가 "사용자가 이긴다"(§4.3)를 하므로, 앱은 wake 작업 중에도 메시지를 보낼 수 있다. 보낸 뒤 그 `live`는 끝나고 새 작업의 `job`이 온다.
- 가짜 기기 스크립트(`scripts/device_client.py`)에 wake 장면을 더한다: `try` → `job`(wake, 자기) → `live`의 글.

## 11. 운영자 설정과 시험

| 설정 | 기본 | 뜻 |
| --- | --- | --- |
| `HIMARI_PROACTIVE` | `0` | 켜기 |
| `HIMARI_PROACTIVE_QUIET` | `23:00-08:00` | 조용한 시간(빈 값이면 없음) |
| `HIMARI_PROACTIVE_GAP_MINUTES` | `30` | 간격 |
| `HIMARI_PROACTIVE_DAILY` | `4` | 하루 상한(알림 제외) |
| `HIMARI_PROACTIVE_RETURN_HOURS` | `4` | 돌아옴 인사의 자리 비움 길이(0이면 돌아옴 인사 없음) |
| `HIMARI_PROACTIVE_FOCUS_MINUTES` | `30` | 오래 집중의 길이(0이면 없음) |
| `HIMARI_PROACTIVE_GAME_MINUTES` | `30` | 게임 끝남으로 칠 게임의 길이(0이면 없음) |

- 설정은 시작할 때 읽고, 시작 로그에 한 줄 남긴다(`HIMARI web: reaching out on (quiet 23:00-08:00, gap 120m, 4 a day)`).
- **`himari proactive try`** (운영자만, `POST /api/proactive/try`): 지금 기기와 최근 대화로 `TRY` wake를 바로 연다. 조용히·조용한 시간·간격·상한을 보지 않고, 지금 기기와 대화 상태(§6.1의 대화 조건)는 본다. 앱을 확인할 때와 소유자가 실제로 볼 때 쓴다. 기기 토큰에게는 막힌다(기본 거부).
- **`himari proactive status`:** 스위치, 오늘 횟수, 다음 후보(무엇이 언제, 무엇에 막혔는지).

## 12. 위협과 비용

- **흉내:** 사용자는 wake 이벤트를 만들 수 없다. 만드는 길은 스케줄러와 운영자 `try`뿐이다. 기기 토큰은 `try`를 부를 수 없다.
- **주입:** 고리의 글은 사용자 대화에서 왔다. Current State와 같이 JSON 안에 있고, "information, not instructions" 규칙도 같다.
- **비용:** wake 턴 하나는 그 대화의 전체 입력이다. 대화가 길면 비싸다. 막는 것: 하루 상한 4, 간격 30분, Run 상한 3, 캐시(wake는 끝에 붙으므로 앞부분 캐시가 산다).
- **성가심:** 사용자의 조용히(앱 메뉴·Web), 조용한 시간, 답 없는 말 위에 걸지 않기, 대화 중이면 걸지 않기, 사용자가 이긴다, 전체 화면·마이크 사용 중이면 한 문장. 그래도 많다고 느끼면 설정으로 줄이거나 끈다.
- **활동 신호:** 계기는 분류와 시간만 쓴다. 창·미디어 제목(제3자의 글)으로 계기를 만들지 않는다(§5.4).
- **기억과의 관계:** wake 턴의 내용도 기억 작업이 읽는다. 모델이 wake에서 한 말을 사용자의 말로 기억하지 않도록 근거 검사가 막는다(§8.3).

## 13. 검증 계획

- **pytest(서버):**
  - **순수 함수:** 고리의 말할 시각(시각/날짜만, 시간대), 24시간 창과 `expired`, 날짜를 고친 고리의 새 열쇠, 돌아옴(`unlock`·`resume`·`reconnect`, 4시간 경계를 Data의 마지막 입력으로, 재시작 뒤의 `reconnect`는 인사 안 함, 10분 지난 기록, 돌아온 기기 ≠ 지금 기기, 20초 안의 메시지로 취소, 다른 대화의 메시지), 활동(집중 2시간 경계·분류·같은 집중에 한 번·쉬고 다시, 게임 30분 경계·끝난 지 10분·신호 없음), 알림(조용한 시간·상한 무시, Q8대로 조용히, 자리 비움이면 다음 돌아옴에), 사용자의 조용히, 전체 화면·마이크는 막지 않음, 조용한 시간(자정을 넘는 범위, 기기 시간대), 간격, 하루 상한(시간대의 하루), 답 없는 말, 작업 중, `ask_user` 대기, 합치기와 4개 상한·순서.
  - **Data:** v16 → v17 업그레이드와 백업, `begin_wake`의 원자성(열쇠 중복이면 이벤트도 없음), 휴지통 대화 거절, SQLite·메모리 백엔드 일치, `remind_at` 쓰기·고치기·지우기, `last_input_at`.
  - **Control:** `submit_wake`의 `BUSY`·`CLOSED`, 계기에 wake가 들어감, Run 상한 3, 사용자가 이김(확정 전 취소 → 다음 계기에 둘 다, 확정 뒤 → 답 남음), 재시작 뒤 10분 안 wake 이어 가기와 그 밖 `failed`.
  - **Prompt:** wake 그리기(지운 고리, JSON 인코딩, 시각 줄), "Speaking first" 줄과 원칙의 `<wake>`는 `SystemSpec.proactive`일 때만(반려 꺼짐이면 없음), 반려 절 안에서 "Speaking" 바로 뒤, 대화 프롬프트에 3인칭 HIMARI 없음, 시각 줄 간격에서 빠짐, `sent_from`에서 빠짐. **`context_key`가 wake가 가리키는 고리의 글을 따름**(측정 전 wake의 고리를 고치거나 지우면 측정값을 쓰지 않음, 검수 P1-1), 스위치를 켜고 끄면 `context_key`가 바뀜.
  - **알림 상태(검수 P1-2):** `begin_wake`가 `scheduled`만 `claimed`로(동시에 두 wake 불가), wake `answered` → `delivered`, 답 없이 `cancelled`·`failed` → `scheduled`와 시도 +1, 3번이면 `missed`, `remind_at` 고침이 상태를 되돌림, 24시간 뒤 다음 돌아옴에 `late`, 7일 뒤 `missed`, 재시작 뒤 `claimed` 정리, 대화 중이어도 작업이 끝나면 말함.
  - **정리된 대화에서만(재검수 P1):** wake A가 도구 호출을 기록한 뒤 재시작 → A의 묶음을 닫음(시작 안 한 호출은 실행 안 됨, 끝난 효과는 다시 실행 안 됨) → A `failed` → 알림 `scheduled` → 새 wake B는 묶음이 닫힌 뒤에만 열림. 결과를 모르는 호출이 남아 복구가 필요하면 B를 열지 않음. 스케줄러가 본 뒤 lock 안에서 사용자 메시지가 들어오면 `NOT_SETTLED`이고 열쇠·알림 상태는 그대로.
  - **알림 경합(재검수 P2):** `claimed`인 알림을 고친 뒤 옛 wake가 `answered`로 끝나도 새 알림은 `scheduled` 그대로, 지운 뒤 끝나도 아무것도 바뀌지 않음.
  - **사용자가 이김(검수 P1-3):** 취소가 10초 안에 끝나면 메시지 받음, 안 끝나면 `BUSY`이고 메시지 저장 안 됨, 어떤 경우에도 두 작업이 동시에 돌지 않고 wake의 답이 사용자 메시지 뒤에 기록되지 않음.
  - **`ask_user`(검수 P2):** wake 턴이 질문을 남기면 `answered`+`asked`, 그 알림은 `delivered`, 재시작 뒤 그 질문을 다시 열지 않음.
  - **기억 작업:** wake의 `seq`를 근거로 댄 기억이 떨어짐, 문맥 줄로 그려짐.
  - **사건 흐름:** `job`의 wake `origin`, `hello`.
  - **스케줄러:** 한 작업의 예외가 다른 작업을 멈추지 않음, `purge`가 옮긴 뒤에도 같은 주기.
  - **import 규칙:** `himari.proactive`의 계층과 `ONLY_FROM`.
- **가짜 기기 스크립트:** wake 장면(§10)이 CI에서 돈다.
- **ui_smoke:** wake 줄, 기억 화면의 알림 시각, 설정의 상태 줄.
- **실제 모델 확인**(V3-P85처럼 `tests/live/`, 결과는 `docs/live-results/`): 검사한 모델들에 wake 장면 몇 가지(지난 고리, 돌아옴, 알림, 오래 집중, 게임 끝남, 전체 화면 영상 중, 지운 고리, wake 뒤 사용자 메시지)를 주고 잰다. 짧음(두 문장 이하), 질문 하나 이하, 사용자 언어, wake·서버·번호를 말하지 않음, 표정 표시 문법. 통과한 모델에만 먼저 말 걸기를 허락할지는 결과를 보고 정한다.
- **소유자:** 실제 PC에서 `try`, 그리고 하루 써 보기.

## 14. 뒤 단계 (방향, 이 패킷에서 구현하지 않음)

1. **작업 중 메시지:** `BUSY` 대신 받아 두었다가 다음 Run 사이에 넣는다. 그러면 §4.3의 특례가 일반 규칙이 된다.
2. **화면을 보고 반응:** 화면을 보는 Tool(V3-P84 2단계)이 생기면 활동 신호보다 자세한 계기를 더한다. 캐릭터 반응(팝콘 등, V3-P86 갈래 2)은 앱이 같은 신호로 혼자 한다.
3. **모델이 말할지 정하기:** 싼 모델로 "지금 말할 만한가"를 먼저 묻고, 아니면 턴을 열지 않는다(Q7의 B를 대화 밖에서).
4. **알림 센터와 표시 확인:** 앱이 꺼져 있거나 다른 창이 전체 화면일 때 Windows 알림으로. 앱이 알림 말풍선을 실제로 띄웠음을 서버에 알려 `delivered`를 "보였음"으로 좁히기(§5.3의 한계).
5. **여러 주체.**

## 15. 소유자 질문

**소유자 결정(2026-11-01): Q1~Q9 모두 권고대로(A).** Q6은 설명을 듣고 확인함(사용자가 이김, 10초 안에 끝나지 않으면 `BUSY`).
**소유자 변경(같은 날):** Q5의 간격을 **2시간 → 30분**, 오래 집중의 길이를 **2시간 → 30분**(하루 4번·조용한 시간·게임 30분은 그대로). Q1의 오래 집중에 **영상(`video`)도 포함**.

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 이번 패킷의 계기 | **A. 지난 고리 + 돌아옴 + 알림 + 활동(오래 집중·게임 끝남)** / B. 활동 빼고(rev. 1의 셋) / C. 지난 고리 + 돌아옴 | **A**. 활동 신호를 먼저 만든 이유가 이것이다. 활동 계기는 분류와 시간만 쓰므로 작다. 알림은 고리에 시각 하나를 더하는 것이고, 돌아옴 인사는 반려의 "곁에 있음"을 가장 쉽게 느끼게 한다 |
| Q2 | 어느 대화에서 말하나 | **A. 최근 대화** / B. 매번 새 대화 / C. 고리를 쓴 대화(없으면 최근 대화) | **A**. 앱이 따라가는 곳과 같다. B는 대화 목록을 어지럽히고 앞뒤 맥락이 없다. C는 오래된 대화로 앱이 갑자기 옮겨 간다 |
| Q3 | 돌아옴 인사 | **A. 4시간 넘게 비웠다가 잠금을 풀거나 PC를 깨우면 늘** / B. 할 말(지난 고리·알림)이 있을 때만 / C. 하지 않음 | **A**. 아침에 PC를 켰을 때 인사가 반려답다. 하루 상한과 간격이 지나치지 않게 막는다. 할 말이 있으면 합쳐서 한 번에 말한다(§6.4) |
| Q4 | 고리의 말할 시각 | **A. 뒤에만: 시각이 있으면 1시간 뒤, 날짜만이면 그날 18시** / B. 앞(1시간 전·그날 아침)과 뒤 둘 다 / C. 앞에만 | **A**. "어땠어?"가 고리의 주된 쓰임이다. 앞에 알려 주는 것은 사용자가 원할 때 알림(§5.3)으로 한다. B는 고리 하나에 두 번 말을 건다 |
| Q5 | 조절 수치 | **A. 조용한 시간 23–08시, 간격 2시간, 하루 4번, 집중 2시간, 게임 30분** / 다른 수치 | **A**. 처음에는 적게 시작하고 써 보며 늘린다. 모두 설정으로 바꿀 수 있다 |
| Q6 | wake 작업 중 사용자가 말하면 | **A. 사용자가 이김(wake 작업 취소)** / B. 지금처럼 `BUSY` | **A**. HIMARI가 먼저 말을 꺼냈다는 이유로 사용자가 말을 못 하면 안 된다. wake 작업은 짧아서 잃는 것이 적다 |
| Q7 | 모델이 말하지 않기로 할 수 있나 | **A. 아니오: 말할지는 서버 규칙이 정함** / B. 예: 답 전체가 `[[pass]]`면 숨김 | **A**. B는 숨긴 답이 대화 기록과 다음 입력에 남아 비용과 혼란이 생긴다. 필요해지면 대화 밖의 싼 판단으로 한다(§14의 3) |
| Q9 | 제때(24시간) 전하지 못한 알림 | **A. 7일 안의 다음 돌아옴에 "늦은 알림"으로, 그 뒤 `missed`** / B. 24시간이 지나면 바로 `missed` / C. 언제든 다음 돌아옴에 | **A**. 주말에 PC를 안 켜서 놓친 "월요일 9시 약속" 알림은 화요일에라도 듣는 편이 낫다. 한 달 지난 알림은 소음이라 7일에서 끊는다. `missed`는 기억 화면에 보인다 |
| Q8 | 알림이 사용자의 조용히를 넘나 | **A. 넘는다(사용자가 그 시각을 정해 부탁함)** / B. 조용히가 끝난 뒤에 말한다 | **A**. 조용히는 "먼저 말 걸지 마"이고, 알림은 사용자가 "그때 말해 줘"라고 따로 부탁한 것이다. 회의 중에 알림이 걱정되면 알림 시각을 바꾸거나 지우면 된다. B면 "9시에 약 먹으라고 해 줘"가 조용히 때문에 늦는다 |

## 16. 모듈 경계

| 모듈 | 바뀌는 것 |
| --- | --- |
| `data` | v17: `EventKind.WAKE`, `Wake`·`WakeReason`, `wakes`·`wake_keys`, `begin_wake`, `memories.remind_at`·`reminded_at`, `devices.last_input_at`, `OriginKind.WAKE` |
| `presence` | 마지막 입력을 Data에 쓰기(10분, 잠기거나 자리 비움이 될 때), 돌아옴 기록이 생길 때의 `on_returned` 콜백 |
| `proactive` (새, 계층 3) | 계기·조절 규칙·고르기·합치기 순수 함수. 입력은 `DeviceState`(활동 신호)·`Returned`·조용히·고리·wake 기록. `common`, `data.api`, `presence.api`만 |
| `prompt` | wake 그리기, `system.py`의 `PROACTIVE_RULE`(반려 절)과 원칙의 `<wake>`, `SystemSpec.proactive`, `PromptInputs.wake_notes`와 그것을 해시하는 `context_key`(§8.4), 시각 줄 간격에서 빼기, `MEMORY_SECTION`·`memory_save` 설명의 `remind_at` |
| `control` | `submit_wake`, `accept_wake`, wake 작업의 Run 상한, 사용자가 이김, `_next_triggers`의 wake, `sent_from`에서 빼기 |
| `memory` | 입력에서 wake를 문맥 줄로 |
| `tool`(기억 도구) | `memory_save`의 `remind_at` |
| `bootstrap` | `Ticker`(정리 루프를 옮김), proactive 작업, 설정, 재시작의 wake 처리 |
| `interfaces/web` | wake 줄, 기억 화면의 알림, 설정 상태 줄, `POST /api/proactive/try`, 기기 허용 목록은 그대로 |
| `interfaces/cli` | wake 출력, `proactive status`·`try` |
| `clients/windows` | §10 |

- Provider, Environment, runner, sandbox, MCP는 바뀌지 않는다.

## 17. 구현 단계

1. **1부: wake의 길.** Data v17(`wake`, `wakes`·`wake_keys`, `begin_wake`), `submit_wake`·사용자가 이김·Run 상한, 모든 소비자의 wake 처리(Prompt·기억 작업·검색·압축·Web·CLI), 반려 절의 "Speaking first"와 원칙의 `<wake>`, `wake_notes`와 `context_key`, `try`와 `status`, 사건 흐름의 `origin`, 가짜 기기 스크립트. 이것만으로 `try`로 앱에서 볼 수 있다.
2. **2부: 계기와 스케줄러.** `himari.proactive` 순수 함수, `Ticker`(정리 루프 옮김), 고리·돌아옴·알림·활동, `remind_at`(도구·기억 화면), `last_input_at`, `on_returned`, 설정 상태 줄, 실제 모델 확인.
3. **앱:** §10(이 저장소에서, 1부와 함께 또는 뒤에).

배포: V3-P88(v16)이 먼저 배포되어 있어야 한다(돌아옴 기록과 활동 신호). 1·2부를 함께 해도 되고 1부만 먼저 해도 된다(스위치가 꺼져 있으면 `try`로만 열림). 1부를 배포할 때 v17로 올린다.

## 18. 외부 설계 검수 (PR #147, `80168b2`): NOT_READY → rev. 4

소유자가 붙여 준 정적 검수(2026-11-01). 코드와 대조했다: `BackgroundRunner.cancel()`은 협력적 취소 요청이다(Driver의 `_interruptible`이 진행 중인 호출을 끊지만 끝나는 시각은 보장하지 않음). 지적은 모두 맞다.

| # | 지적 | 반영 |
| --- | --- | --- |
| P1-1 | 지난 wake의 그림 길이 변화는 상태 전체를 다시 더하는 것으로 덮을 수 없음 | `wake_notes`를 `context_key`에 넣어 바뀌면 요청 전체를 다시 잼(§8.4) |
| P1-2 | 알림의 열쇠를 잡은 것 ≠ 전달. 취소되면 잃음. 24시간 창과 "다음 돌아옴"이 모순 | 고리 행의 `scheduled → claimed → delivered / missed`, 답 없는 wake면 되돌림(3번까지), 제때 24시간·늦은 알림 7일·그 뒤 missed(Q9), 알림은 "대화 중" 규칙의 예외(§5.3, §6.1) |
| P1-3 | 10초 뒤에도 wake 작업이 끝나지 않을 때의 계약 없음 | 그때는 `BUSY`(메시지 저장 안 함), 한 대화에 작업 하나라는 불변식 유지(§4.3) |
| P2 | `ask_user`로 질문을 남긴 wake의 상태와 재시작 | `answered`+`asked`로 끝냄, 재시작 복구는 출력이 하나도 없는 `opened`만(§4.2, §4.4) |

검수자 의견: Q8(알림이 조용히를 넘나)은 사용자 경험에 직접 닿으므로 구현 전에 정해야 한다.

### 18.1 재검수 (`826b338`): NOT_READY → rev. 5

이전 넷은 해결 확인. 코드와 대조했다: `_resume_locked`는 대화의 마지막 묶음이 끝나지 않았으면 그 묶음을 어느 턴의 것이든 이어서 실행한다(`_resume_pending_group`). 맞다.

| # | 지적 | 반영 |
| --- | --- | --- |
| P1 | 재시작으로 끊긴 wake의 끝나지 않은 도구 묶음이 남은 채 새 wake가 열리면 두 wake의 도구와 경계가 섞임 | 새 wake는 정리된 대화에서만(lock 안에서 다시 확인, `NOT_SETTLED`), 끊긴 wake의 묶음은 재시작 때 닫음(시작 안 한 호출 실행 안 함, 끝난 효과 다시 실행 안 함, 결과 모르는 호출은 기존 복구), 복구가 필요하면 새 wake 안 엶(§4.4, §6.1) |
| P2 | 알림을 고친 뒤 옛 wake가 끝나면 고친 알림까지 `delivered`가 될 수 있음 | 끝날 때의 갱신은 `claimed`이고 `remind_wake`가 그 wake일 때만(§5.3) |

검수자 의견: P1이 반영되면 READY.

### 18.2 재검수 (`85385ac`): READY_WITH_CORRECTIONS → 고침

P1·P2 해결 확인. 문서 정합성 둘과 구현 주의 하나:
- `WakeReason`에 `late: bool = False`(알림만, §3.2).
- §16·§17의 "`wake_notes`와 `state_key`"를 `context_key`로 통일(§8.4와 같게).
- `delivered`는 "대화에 기록됨"이지 앱이 실제로 띄웠다는 뜻이 아님을 §5.3에 적고, 표시 확인은 뒤 단계(§14의 4)로.

검수자 의견: 이 셋을 반영하면 READY.

## 19. 구현 메모

### 19.1 1부: wake의 길 (`feat/proactive-part1`)

- **Data v17.** `memories`에 알림 열(`remind_at`·`remind_instant`·`remind_state`·`remind_wake`·`remind_attempts`·`reminded_at`, 2부가 씀), `devices.last_input_at`(2부), 표 `wakes`·`wake_keys`. `begin_wake`는 한 트랜잭션에서 키 확인(하나라도 있으면 `DUPLICATE_IDENTITY`, 아무것도 남기지 않음) → 이벤트 → `wakes` 행 → 키 → 알림 `scheduled → claimed`(`AND remind_state='scheduled'`가 아니면 `INVALID_STATE_TRANSITION`). `end_wake`는 한 번만(`opened`일 때만 바뀌고 그다음은 그대로 돌려줌); 알림은 `remind_wake`가 이 wake일 때만 `delivered`(answered) 또는 `scheduled`로 되돌림(시도 3번째면 `missed`). 버림(purge)은 그 대화의 `wakes`를 지우고 `wake_keys`는 남김(같은 날 같은 계기가 다시 열리지 않게). 메모리 백엔드도 같음.
- **Control.** `ConversationDriver.accept_wake`는 드라이버 lock 안에서 `_prepare` 뒤 정리됐는지 다시 보고(멈춘 턴·답 기다리는 호출·끝나지 않은 도구 묶음·답 안 한 계기면 `NOT_SETTLED`) `begin_wake`. 웨이크 이벤트는 `_next_triggers`의 계기(`resume`이 그 뒤로 이어 감). `resume(max_runs=)`로 이번 턴만 Run 상한을 더 좁힘(`WAKE_RUNS = 3`). `close_cut_group`은 끝나지 않은 묶음을 닫음(시작 안 한 호출은 실행 안 함). `BackgroundRunner.submit_wake`는 작업 중·입력 받는 중이면 `BUSY`, 받으면 `accepted`를 알리고 `"wake"` 작업을 `MessageOrigin(WAKE, device_id)`로 시작, `JobStatus.wake_id`. **사용자가 이김:** 메시지가 든 `submit`은 지금 작업이 wake 작업이면 취소를 요청하고 `WAKE_YIELD_SECONDS = 10`초까지 기다림; 그래도 돌면 `BUSY`이고 메시지는 저장하지 않음. 보통 작업은 메시지로 취소하지 않음(예전과 같음).
- **Bootstrap.** `bootstrap/proactive.py`: `ProactiveSettings`(§11 값, 간격 30분·하루 4번·돌아옴 4시간·집중 30분·게임 30분·조용한 시간 23–08), `WakeEvents`(Runner 듣는 이: 작업이 끝나면 wake 이벤트 뒤를 읽어 `answered`(마지막 답) / `answered`+`asked`(기다리는 질문) / `cancelled`(아무 말 전에 취소) / `failed`), `settle_cut_wakes`(시작할 때 `opened`인 것: 답·질문이 뒤에 있으면 `answered`, 아니면 끊긴 묶음을 닫고 `failed` — 1부는 다시 이어 가지 않음). `Application.try_wake`(스위치·지금 기기·가장 최근 대화가 있어야 하고, 주체의 작업이 하나라도 돌면 `busy`, 그 대화가 정리되지 않았으면 `not_settled`; 조용히·조용한 시간·간격·상한·"메시지 뒤 10분"·"답 없는 wake 위"는 보지 않음 — 확인용이라. `ProactiveRefused` 코드 `proactive_off`·`no_device`·`no_conversation`·`busy`·`closed`·`not_settled`), `proactive_status`. `SystemSpec.proactive = companion and enabled`.
- **Prompt.** `UserTurn.wake`; 웨이크는 `<time>` 줄(언제나, 사이 간격 계산에는 안 들어감) + `<wake>{JSON}</wake>`(`json_data`라 노트 글이 태그를 닫지 못함). 고리는 `PromptInputs.wake_notes`(ContextLoader가 그 턴에 읽음, 지워진 것은 `{"id","deleted":true}`), `context_key`에 `wake_notes`. `PROACTIVE_RULE`은 반려 절의 `SPEAKING_RULE` 바로 뒤, 원칙 문단은 "the reasons in a <wake> message"를 정보로 이름 지음 — 둘 다 `proactive`이고 반려가 켜졌을 때만. 압축 입력에서는 `role: "wake"`.
- **기억 작업.** 웨이크는 `context` 줄(`[HIMARI spoke first: loop due m15, …]`): 보이지만 사용자 근거가 아니고, 파트를 시작하지 않고, 사용자 글자 수에도 안 셈. 대화 검색은 `SEARCH_KINDS`(사용자·답)뿐이라 웨이크를 찾지 않음.
- **Web·CLI.** 이벤트 보기 `kind: "wake"`, `wake: {id, device_id, reasons}`, `text`는 서버가 만든 한 줄("HIMARI reached out · reminder m7 (late) · video for 1h 35m"); Web은 흐린 가운데 줄(`.wake-line`), CLI는 `· …` 한 줄. `GET /api/proactive`, `POST /api/proactive/try`(202, 거절은 409와 코드) — 기기 토큰은 기본 거절(`device_scope`). `himari proactive [status|try]`. 사건 흐름의 `job` origin은 `{"kind":"wake","device_id":…}`(origin_view 그대로).
- **설정.** `HIMARI_PROACTIVE`(기본 끔), `HIMARI_PROACTIVE_QUIET`(`HH:MM-HH:MM`, 비우면 없음), `_GAP_MINUTES`·`_DAILY`·`_RETURN_HOURS`·`_FOCUS_MINUTES`·`_GAME_MINUTES`. 시작 줄 `HIMARI web: reaching out …`. 1부의 숫자는 아직 `try`에 쓰이지 않음(2부의 규칙이 씀).
- **가짜 기기.** `scripts/device_client.py --self-test`가 답 뒤에 `try`로 wake를 열고, 기기가 origin `wake`인 `job`을 보고 그 `live`를 끝까지 따라감(표시 글에 표시 기호 없음).
- **배포:** v17 업그레이드(백업 먼저). 스위치를 켜지 않으면 바뀌는 것은 표·열뿐이고 모델 입력도 같다(`proactive=false`면 새 줄 없음).

### 19.2 검수 (PR #153, `82cbd53`): READY_WITH_CORRECTIONS → 고침

- **P2-1 재시작 통합 테스트:** `tests/integration/test_wake_restart.py`(메모리·SQLite 둘 다). wake 턴의 묶음에서 첫 호출은 성공, 둘째는 claim 전에 죽고, 셋째는 시작 안 함 → 재시작 → `settle_cut_wakes`: 첫 호출 `SUCCEEDED` 그대로(다시 실행 안 함), 둘째·셋째 `CANCELLED`(`CUT_REASON`), 모델 호출 없음, wake `failed`, 새 wake가 열리고 셋째는 끝까지 실행되지 않음. `REQUIRE_USER`의 결과 모르는 호출이면 다시 실행하지 않고 새 wake는 `NOT_SETTLED`.
  - 이 테스트가 찾은 것: 닫힌 묶음은 모델이 아직 답하지 않은 계기(`CompletedGroupRef`)라 `accept_wake`가 새 wake를 계속 막았다(사용자가 말할 때까지). §6.1의 "답하지 않은 계기"는 사용자 메시지나 wake이고 §13은 "묶음이 닫힌 뒤에 새 wake"이므로, **끝난 wake의 턴이 남긴 묶음**(재시작으로 닫힘, Run 상한에서 멈춤)은 막지 않는다 — 새 wake 턴이 그 결과를 함께 본다. 사용자의 턴이 남긴 묶음(Run 사이에서 멈춤 등)은 사용자의 일이라 계속 막는다(`Driver._left_by_a_wake`: 마지막 사용자 메시지·wake 이벤트가 wake이고 그 기록이 `opened`가 아님).
- **P2-2 `begin_wake` 인자:** `rules.check_wake`가 두 백엔드에서 똑같이 거절(`INVALID_ARGUMENT`, 아무것도 쓰지 않음): 같은 열쇠 두 번·빈 열쇠, `reminders`가 REMINDER 이유들의 노트와 정확히 같지 않음(이유 없는 알림, 알림을 싣지 않은 REMINDER 이유, 두 번, 다른 노트, 같은 노트의 이유 두 번, 노트 없는 REMINDER 이유).

### 19.3 2부의 첫 PR(2a): 계기와 스케줄러 (`feat/proactive-part2`)

알림(§5.3의 `remind_at`·도구·기억 화면·늦은 알림), 설정 화면의 상태 줄, 실제 모델 확인은 다음 PR(2b)에서 한다. 규칙 함수는 알림의 예외를 이미 안다.

- **`himari.proactive`(새, 계층 3, `common`·`data`·`presence`만):** `ProactiveSettings`(bootstrap에서 옮김), `Situation`(시계와 읽은 상태) → `decide(situation, taken)` → `Decision`(이유·열쇠·알림·`expired` 열쇠·`blocked`·`held`·`next_at`).
  - 고리: `loop_moment` — 시각이 있으면 +1시간, 날짜만이면 그날 18:00(날짜의 `due_instant`는 그 시간대의 하루 끝이라 다음 자정 − 6시간). 24시간 창, 넘으면 `expired` 열쇠. 열쇠 `loop:m<번호>:<due_at>`.
  - 돌아옴: 4시간 이상, 기록 뒤 20초~10분, 돌아온 기기 = 지금 기기, 자리 비움 시작 뒤(그리고 돌아온 뒤 20초 안) 사용자 메시지 없음. 마지막 입력을 모르면(처음 쓰는 기기) 인사하지 않음. 열쇠 `return:<기기>:<분>`.
  - 활동: FOCUS(`coding`·`document`·`design`·`video`, 30분), GAME_ENDED(30분 이상, 끝난 지 10분 안). 열쇠의 시작·끝 시각은 길이에서 거꾸로 세므로 틱마다 1초쯤 흔들려 분 경계를 넘을 수 있다 → 앞뒤 1분 열쇠도 함께 확인(`aliases`).
  - 규칙 순서: 지금 기기 없음 → 작업 중 → (여기까지 알림도 막힘) → 조용히 → 대화 중(10분) → 답 없는 wake → 조용한 시간(기기 시간대, 자정 넘김) → 간격 → 하루 상한(기기 시간대의 하루, 알림만인 wake는 안 셈). **운영자의 `try`는 간격과 하루 상한에 세지 않는다**(시험이라).
  - 합치기: 알림 → 고리(말할 시각 순) → 활동 → 돌아옴, 4개까지, 나머지는 `held`(다음 틱).
- **`bootstrap/schedule.py` `Ticker`:** 이름 붙인 주기 작업. `purge`(옛 정리 루프, 같은 주기, 시작할 때 바로 한 번), `proactive`(30초, 돌아옴 기록 뒤 21초에 한 번 더 `poke`). 한 작업의 예외는 로그만 남고 다른 작업과 다음 차례는 돈다.
- **마지막 입력(`Inputs`):** presence의 `on_input`(신호마다, 벽시계로 바꾼 입력 시각과 잠김 여부)을 받아, 이 프로세스 안에서는 모두 기억하고 Data(`devices.last_input_at`, 뒤로 가지 않음)에는 10분에 한 번, 잠기면 바로 쓴다. 반려 기능이 켜져 있으면 먼저 말 걸기가 꺼져 있어도 쓴다.
- **돌아옴(`on_returned`):** presence가 돌아옴 기록을 만들 때(그 신호의 입력보다 먼저) 부른다. 스케줄러가 그때까지 아는 가장 늦은 입력(이 프로세스의 것 + Data의 것)으로 자리 비움 길이를 잰다. 서버가 시작된 뒤 처음 듣는 주체면 Data를 먼저 읽고 잰다(그 읽기는 이 신호의 입력이 쓰이기 전). 그래서 재시작 뒤의 `reconnect`는 짧게 재진다.
- **스케줄러(`ProactiveScheduler`):** 지금 기기가 있는 주체마다 `Situation`을 읽는다(최근 대화, 그 끝 40개 이벤트로 "답 없는 wake", 기억의 고리, 26시간의 wake, 조용히, 마지막 사용자 메시지, 작업 중). 후보의 열쇠를 한 번에 물어(`taken_keys`) `decide`, `expired`를 쓰고(`expire_keys`), 이유가 있으면 `submit_wake`. `BUSY`·`NOT_SETTLED`·`CLOSED`·열쇠 경합은 그 틱을 건너뛰고 상태에 남긴다.
- **상태:** `GET /api/proactive`와 `himari proactive status`에 오늘 횟수/상한, 마지막 wake, 다음 고리 시각, 마지막 판단(결과, 막힌 이유로 미룬 이유들)을 더함.
- **재시작(§4.4와 다름):** 이어 가지 않는다. 시작할 때는 기기가 아직 다시 붙지 않아 §6.1(지금 기기)을 통과할 수 없으므로 설계의 "이어 간다" 갈래는 열리지 않는다. 대신 **아무 말도 하기 전에 끊긴 wake**(wake 뒤에 이벤트가 없음)는 `failed`로 적고 열쇠를 돌려준다(`release_keys`). 계기가 아직 창 안이면 스케줄러가 새 wake로 다시 말한다. 한마디라도 한 wake(도구 묶음 포함)는 열쇠를 지킨다.
- **Data(스키마 변경 없음, v17 그대로):** `wakes_since`, `taken_keys`, `expire_keys`(`wake_id` 없이), `release_keys`, `record_input`·`read_inputs`, `last_user_message_at`. 두 백엔드 같음.

### 19.4 검수 (PR #156, `dd6376a`): NOT_READY → 고침

- **P1 말하기 전 끊긴 wake가 다음 wake를 막음:** 열쇠를 돌려줘도 그 wake 이벤트가 답하지 않은 계기(`EventRef`)로 남아 `accept_wake`가 `NOT_SETTLED`를 냈다. 이제 `Driver._left_by_ended_wakes`가 계기를 하나씩 본다: 사용자 메시지, **아직 열린** wake, 사용자 턴이 남긴 묶음은 막고, **끝난** wake(재시작으로 `failed`, 사용자가 이겨 `cancelled`)의 계기와 끝난 wake가 남긴 묶음은 막지 않는다. 다음 턴은 그것들을 다 그리고 새 wake를 마지막에 둔다(앞 wake는 같은 이유의 `<wake>` 줄로 그대로 보임 — 기록을 숨기지 않음).
  - 같이 고친 것: 말하기 전 끊긴 wake가 **간격**에 걸려 30분 동안 다시 말하지 못했다. 간격과 하루 상한은 이제 **말했거나 말하는 중인 wake**(`opened`·`answered`)만 센다(`proactive.spoke`). 실행 중 실패한 wake는 열쇠를 지키므로 같은 계기를 되풀이하지 않는다.
  - 시험: 두 백엔드에서 재시작 → `failed` → 열쇠 반환 → 새 wake → 모델 답(두 `<wake>`가 그려짐, 열쇠 다시 잡힘), 실제 앱의 스케줄러로 같은 흐름, 열린 wake·사용자 메시지는 계속 막음.
- **P2 읽은 뒤 바뀐 것:** 스케줄러가 wake를 열기 직전에 마지막 사용자 메시지(알림만이 아니면 "대화 중")와 지금 기기를 다시 보고, `submit_wake(alone=True)`가 그 주체의 **어느 대화에서든** 작업이 돌면 `BUSY`. 다시 본 것과 Runner의 검사 사이에 `await`가 없어 함께 성립한다. 남는 틈은 `accept_wake` 안의 기다림뿐이고, 그 대화의 메시지는 Driver lock이 막는다.

### 19.5 재검수 (PR #156, `0ed2f28`): READY_WITH_CORRECTIONS → 고침

- **P2-1 `alone` 검사 뒤의 틈:** `accept_wake`를 기다리는 동안 다른 대화에서 작업이 시작될 수 있었다. 이제 `submit_wake`가 기다림 **뒤에** 한 번 더 본다: 그 주체의 작업이 돌면(또는 이 대화가 닫히는 중이면) wake 작업을 시작하지 않고 `Driver.withdraw_wake`로 거둔다(`cancelled`, 알림은 되돌림, 열쇠 반환 — 한마디도 안 했으므로). 이 확인에서 `_start`까지 `await`가 없어 wake 작업이 다른 작업 곁에서 도는 일은 없다. 사용자가 이긴다(사용자의 작업은 막지 않음). 시험: `accept_wake` 안에서 실제로 기다리는 사이 다른 대화의 작업을 시작하는 끼어들기.
- **P2-2 출력이 있는 `failed`:** `spoke`가 `failed`라도 그 턴이 출력(최종 답, 진행 글, 도구 호출)을 남겼으면 센다. 스케줄러가 26시간 안의 `failed` wake마다 그 wake 뒤를 다음 메시지·wake까지 읽어(`Situation.said`) 넘긴다. 읽지 못하면 말한 것으로 센다. `wake_turn`은 다음 wake에서도 끊는다(뒤 wake의 출력이 앞 wake 몫이 되지 않게; 작업 종료·재시작 정리에서는 열린 wake 뒤에 다른 wake가 올 수 없어 바뀌는 것이 없다).

### 19.6 2부의 둘째 PR(2b): 알림, 상태 줄, 실제 모델 확인 (`feat/proactive-part2b`)

- **Data(스키마 그대로, v17):** `Memory`에 `remind_at`·`remind_instant`·`remind_state`·`reminded_at`, `MemoryChange`·`NoteDates`에 `remind_at`·`remind_instant`. 모든 노트 쓰기가 한 곳(SQLite `_write_memory`, 메모리 `_put_memory`)을 지난다: 고리가 아니면 알림을 지움, 알림이 **새로 쓰이면**(다른 시각이거나 지워짐) `scheduled`(없으면 상태 없음)·시도 0·`remind_wake` 비움·`reminded_at` 비움, **그대로면** 전달 상태를 지킴(그래서 자동 기억이 글을 고쳐도, 사용자가 다른 날짜를 고쳐도 `delivered`가 유지됨). 자동 기억은 `remind_at`을 쓰지 않는다(그 경로에 인자가 없음). 저장·복원은 다시 읽은 노트(상태 포함)를 돌려준다. `miss_reminders(principal, before)`: 7일 지난 `scheduled` → `missed`. 되돌리기(Undo)는 알림도 되살린다.
- **`memory_save`:** `remind_at`(고리만, 오프셋 있는 시각만 — 날짜만이면 거절, 5분 전~1년 뒤). 설명에 "Give a loop remind_at only when the user asked to be reminded at a time: the exact time with its UTC offset, worked out from now for "in 30 minutes"". 다시 쓰기에서 주지 않으면 지킴, `null`이면 지움. `memory_search` 줄에 `remind …`(아직 전하지 않은 것만).
- **계기(`reminder_candidates`):** `scheduled`이고 시각이 지난 고리. 24시간 안이면 제때(Q8대로 조용히·대화 중·답 없는 wake·조용한 시간·간격·하루 상한을 넘음, 지금 기기·작업 중은 기다림). 그 뒤 7일 안이면 **제때 창이 끝난 뒤 생긴** 새 돌아옴 기록(길이는 상관없음)에서 `late: true`로. 열쇠 없음(전달 상태가 막음). 합칠 때 맨 앞. 스케줄러가 판단할 때마다 `miss_reminders(now − 7일)`.
- **모델에게:** 기억의 `loops`에 아직 전하지 않은 알림의 `remind_at`, 알림 wake의 `loop`에 `remind_at`(고리 확인 wake에는 없음). `context_key`의 `wake_notes`에 `remind_at`.
- **Web:** 기억 화면의 고리 줄 — "Reminds you at …" / "Reminded …" / "Missed reminder at …", 없으면 아직 오지 않은 "Checks in after …"(설계의 "Checked in Nov 4"는 하지 않음: 열쇠가 wake인지 만료인지 화면에서 알 길이 없어서). 편집 폼에 고리일 때만 보이는 `datetime-local` "Remind me at"(브라우저 시간대로 보냄, 서버가 그 시간대로 읽음). 편집에서 이름을 대지 않은 날짜는 노트의 것을 지킴(전에는 고리의 `due`만 고쳐도 다른 날짜가 비었을 수 있음). Settings > Devices 아래 "Reaching out: on · quiet 23:00–08:00 · today 1 of 4"(꺼져 있으면 이유). `GET /api/proactive`에 `quiet_hours`·`next_reminder_at`; CLI `Next reminder`.
- **실제 모델 확인**(`tests/live/test_proactive_live.py`, 결과 `docs/live-results/proactive-2026-11-01.jsonl`): 9장면(지난 고리, 돌아옴, 알림, 늦은 알림, 오래 집중, 게임 끝남, 전체 화면 영상, 지운 고리, wake 뒤 사용자 메시지) × 2번 × 5모델. 판정: 두 문장 이하(전체 화면은 한 문장), 질문 하나 이하, 한국어, wake·서버·번호를 말하지 않음, 표정 표시 문법.
  - "Speaking first" 줄을 두 번 다듬음: 70/90 → 77/90 → **80/90**(gpt-6-luna 18, deepseek-v4.1-flash 17, claude-haiku-5.5 16, mimo-v2.6-pro 16, minimax-m3 13). 바뀐 말: "about those reasons only: at most two short sentences in all, and only one of them a question; when … fullscreen … one short sentence and no question".
  - 남은 실패는 대부분 가벼움(짧은 질문 둘 "다녀오셨어요? 어떠셨어요?", 전체 화면에서 두 문장). 모델 밖의 실패: deepseek가 도구 없이 도구 호출 문법을 지어냄(2회), mimo 빈 응답(1회). **minimax-m3만 80% 아래.** 통과한 모델에만 먼저 말 걸기를 허락할지는 소유자가 정한다(§13).

### 19.7 검수 (PR #157, `a661e60`): NOT_READY → 고침

- **P1-1 정리 작업이 기다리는 알림의 고리를 지움:** 날짜 없는 고리는 90일, 날짜 지난 고리는 14일이면 지워졌다(알림은 1년 뒤까지 받음). 이제 알림이 `scheduled`·`claimed`인 고리는 그 정리에서 빠진다(`PENDING_REMINDERS`); 전한 뒤(`delivered`)나 놓친 뒤(`missed`)는 원래 규칙. 두 백엔드.
- **P1-2 자동 기억 수정의 Undo가 알림을 잃음:** Undo가 읽는 기록(`rules.memory_json`)에 `remind_at`·`remind_instant`를 넣고 `memory_from_record`가 되살린다(알림 전의 기록은 알림 없이 그대로 읽힘). 같은 알림으로 되돌리면 전달 상태도 지켜진다.
- **P2-1 retire 뒤 복구의 상태가 백엔드마다 다름:** 메모리 백엔드가 retire 기록을 먼저 지우고 쓰는 바람에 알림을 새로 쓴 것으로 보고 `scheduled`로 되돌렸다. 이제 retire 기록이 있는 동안 쓰고 지운다 — SQLite처럼 `delivered`가 남는다.
- 시험(두 백엔드): 1년 안 알림의 고리가 정리 시각을 지나도 남고, 알림을 전한 뒤에 정리됨; 자동 수정 → Undo → 알림·상태 유지(옛 기록도 읽힘); 전달 → retire → 복구 → `delivered`. 고치기 전 코드에서는 P1 둘이 두 백엔드에서, P2-1이 메모리 백엔드에서 실패하는 것을 확인함.

### 19.8 재검수 (PR #157, `e52f458`): READY_WITH_CORRECTIONS → 고침

- **P2-1 같은 문구의 고리에 새 알림:** `memory_save`의 "Already saved" 검사가 알림을 보지 않아, 이미 있는 고리를 같은 문구와 `remind_at`으로 다시 저장하면 알림이 붙지 않았다. 이제 알림이 주어졌고 그 고리의 것과 다르면 그 고리의 수정으로 쓴다(글은 그 고리의 것, `due`는 주지 않으면 그 고리의 것, "Updated m1"). 알림 없이 같은 문구면 예전처럼 "Already saved".
- **P2-2 자동 기억이 기다리는 알림을 지움:** 알림이 `scheduled`·`claimed`인 고리는 자동 기억이 **잊거나(`FORGET`) 고리 아닌 것으로 바꾸지(`UPDATE`의 다른 범주)** 못한다 — Data의 `decide_item`이 `reminder_pending`으로 거절하고(권위), 계획 단계도 `forget`을 미리 떨어뜨린다. 고리인 채로 글만 고치는 것은 된다(알림은 지켜짐). 사용자와 모델의 `memory_forget`은 그대로 지울 수 있다. 알림을 전한 뒤에는 예전 규칙.
  - 같이 고친 것: 메모리 백엔드가 자동 기억을 적용할 때 노트를 전달 상태 없이 읽고 있었다(SQLite는 읽음) — 이제 같은 상태로 판정한다.

### 19.9 소유자 결정 (2026-11-01): 기준에 못 미친 모델은 먼저 말 걸지 않음

- `ProviderCapabilities.speaks_first`: 실제 모델 확인(§19.6)에서 80% 이상 통과한 모델만 — gpt-6-luna(18/18), deepseek-v4.1-flash(17), claude-haiku-5.5(16), mimo-v2.6-pro(16). minimax-m3(13/18)은 빠짐. 확인하지 않은 모델(gpt-5-nano, gpt-6.1-sol, claude-sonnet-5.5, glm-5.3)도 다른 능력과 같은 원칙으로 꺼져 있다(확인하면 켤 수 있음).
- 그 대화의 모델이 `speaks_first`가 아니면: `accept_wake`가 `MODEL_UNCHECKED`(스케줄러는 그 틱을 건너뜀, `try`는 409 `model_unchecked`), 시스템 글의 "Speaking first" 줄과 원칙의 `<wake>`도 넣지 않는다(`RequestOptions.speaks_first`). 사용자가 대화의 모델을 바꾸면 그 다음 판단부터 따른다.
- **검수(PR #158, `5c9b8de`) P2-1 받은 뒤 모델이 바뀜:** `accept_wake`는 모델 A로 확인하지만 작업의 턴은 모델을 다시 고른다. 이제 wake 작업은 `Driver.resume_wake`로 돈다: 그 턴의 모델이 `speaks_first`가 아니면 Run 없이 wake를 거둔다(`cancelled`, 열쇠 반환, 결과 `CANCELLED`/`model_unchecked`). 시험: `try`가 받은 뒤 첫 Run 전에 모델의 능력을 바꿈 → Run 없음, wake `cancelled`.

### 19.10 앱 (`feat/proactive-app`, §10)

- `JobOrigin`(새, WPF 밖이라 ContractTests가 Linux에서 시험): `IsOwn(origin, device)` — `device`나 **`wake`**이고 이 기기를 이름 지음; `IsWake`; `Follow(current, conversation, draft)` — 지금 보는 대화가 아니고 입력 중이 아닐 때만 옮김.
- `Streams.cs`: `job started`가 이 기기의 wake이고 다른 대화면 `Follow`일 때 `RefreshConversations(그 대화)`로 옮기고, 아니면 상태 줄("히마리가 다른 대화에서 말을 걸었어요. 대화 목록에서 열 수 있어요."). 그다음은 자기 작업과 같은 길(`RecoverMoveJob`, `StartLive` → 말풍선·표정·이동). 재연결의 `hello`/`activity`도 `IsOwn`으로 wake 작업의 `live`를 다시 연다.
- `HistoryReader`: wake 이벤트를 "(히마리가 먼저 말을 걸었어요)" 한 줄로.
- 설계의 "입력 중이면 말풍선만 띄우고 누르면 옮김"은 상태 줄 알림으로 줄였다(말풍선은 그 대화의 `live`에 묶여 있어 다른 대화의 말을 띄우면 지금 대화의 말풍선과 섞인다). 사용자가 그 대화를 열면 이어지는 `live`나 기록으로 본다.
- 확인: 앱·UiTests·ContractTests가 Linux의 .NET 10 SDK로 경고 없이 빌드(기존 `confirmResend` 경고 하나만), ContractTests 통과. UiTests와 실제 PC 확인은 Windows에서(소유자).
- **검수(PR #159, `6fddcc2`) P1 옮기는 도중의 입력:** `Follow` 확인과 실제 전환 사이에 대화 목록을 읽는 HTTP 기다림이 있어, 그동안 A에 쓰기 시작한 글이 B로 옮겨질 수 있었다. 이제 `FollowWake`가 목록을 읽은 **뒤** 다시 본다(`JobOrigin.StillFollow`: 같은 대화, 입력 리비전 그대로 — 글을 고치거나 대화를 바꾸면 오름 —, 입력창이 빔). 그 확인에서 전환(`ShowConversations`, 동기)까지 `await`가 없다. 바뀌었으면 옮기지 않고 상태 줄. `RefreshConversations`는 읽기(`LoadConversations`)와 보이기(`ShowConversations`)로 나눔.
- **P2-1 재연결 뒤의 wake:** `hello`/`activity`로 다시 찾은 wake 작업도 `RecoverAndStartLive(..., wake: true)`가 먼저 `FollowWake`를 거친다(같은 확인).
- 시험: ContractTests에 `StillFollow`; UiTests에 느린 목록 응답(`SlowListHandler`) 중 아무것도 안 함 → B로 옮김, 입력함 → A에 남고 글 유지·상태 줄, 다른 대화를 고름 → 그 대화에 남음(빌드 확인, 실행은 Windows).
- **재검수(PR #159, `ed9cbe3`) P2 알림이 바로 지워짐:** 입력 중이라 옮기지 않고 띄운 알림을 곧이은 대화 읽기(`hello`·`activity`)가 "연결됨"으로 덮었다. 이제 앱이 그 대화를 기억하고(`wakeElsewhere`) 상태 줄을 쓰는 곳이 `JobOrigin.Status`를 거친다: 그 대화를 열거나 그 wake 작업이 끝날 때까지 알림이 남는다. 시험: ContractTests(`Status`), UiTests(입력 중 → 알림 → 연결된 채 지금 대화를 읽어도 알림 유지 → 그 대화를 열면 끝).
