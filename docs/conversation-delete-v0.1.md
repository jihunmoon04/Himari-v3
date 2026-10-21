# 대화 삭제와 업그레이드 백업 v0.1

> 2026-10-21 · **PROPOSED (V3-P65)** · rev. 3 — 검수 NOT_READY(B1~B3; C1) 반영(§14), 재검수 NOT_READY(B4; C2·C3) 반영(§15). 구현 없음.
>
> 소유자 결정(2026-10-21): **Q1~Q3 모두 권고대로**(§11). 휴지통 30일(운영자가 바꿈), 휴지통은 목록만, 업그레이드 백업은 자동으로 지우지 않음.
>
> 소유자 방향(2026-10-21):
> - 대화 삭제와 DB 업그레이드 안전장치(업그레이드 전 백업, 되돌리기 절차)를 한 묶음으로 만든다. 지금 DB에는 중요한 것이 없으므로 첫 v7 업그레이드가 백업을 시험하기에 좋다.
> - 휴지통을 거친 뒤 N일이 지나면 영구 삭제한다.
> - 대화의 Workspace 폴더는 그 대화용으로 만든 것이므로 영구 삭제 때 같이 지운다. 나중의 실제 사용자 폴더는 대화와 따로 다룬다.
> - 외부 저장소: HIMARI가 소유하거나 그 대화를 위해 연 것만 지운다. 외부 서버가 자기 쪽에 저장한 데이터는 지우지 않고, 삭제 확인 창에서 알린다. 실행 중인 대화는 먼저 취소한 뒤 휴지통으로 옮긴다.
> - 파일 업로드·다운로드(FILE-04)보다 먼저 한다. 대화 삭제 시 파일 정리(Q-33)는 이 설계를 쓴다.

## 1. 왜, 그리고 범위

지금 HIMARI에는 대화 삭제가 없다. Data·Web·CLI 어디에도 없고, 문서에는 "삭제는 아직 없다(Q-19). 생기면 `external_settings`와 함께 지운다"는 메모만 있다([conversation-model-v0.1.md](conversation-model-v0.1.md) §4.1).

대화 하나에 딸린 것은 다음과 같다.

| 위치 | 대화에 딸린 것 | 이 설계에서 |
| --- | --- | --- |
| Data (SQLite) | `conversations`, `events`, `idempotency`, `runs`(요청·응답 원문 포함), `compaction_runs`, `tool_executions`, `artifacts`, `activations`, `bindings`, `external_settings`, `model_settings` | 영구 삭제 때 한 트랜잭션으로 모두 지운다 |
| Data (SQLite) | `ui_resources` — MCP 뷰 HTML. 내용 주소로 저장하고 모든 대화가 함께 쓴다(V3-P58) | 영구 삭제 때 **더는 어느 호출도 참조하지 않는 것만** 지운다 |
| Inside Workspace | 사용자별 Root 아래 대화 폴더 `conversations/<id>` (기본 Binding의 cwd) | 영구 삭제 때 지운다(§5) |
| MCP stdio `per_conversation` | 대화마다 뜨는 서버 프로세스 | 휴지통으로 옮길 때 종료한다(§6) |
| MCP stdio `shared`, MCP HTTP | 없음: 세션 하나를 모든 대화가 함께 쓴다(`mcp/client.py` HTTP는 `key`를 쓰지 않음) | 아무것도 하지 않는다 |
| 외부 서버의 저장소 (예: Calliope `/var/lib/calliope`) | HIMARI는 모른다: MCP 서버에 대화 id를 보내지 않는다 | 지우지 않는다. 확인 창에서 알린다(§6) |
| Runner (메모리) | 작업 상태 `JobStatus` | 휴지통으로 옮길 때 버린다 |

| 포함 | 제외 (이후) |
| --- | --- |
| 휴지통으로 옮기기·복원·영구 삭제, 보관 기간이 지나면 자동 영구 삭제 | 휴지통에 있는 대화 내용 보기(Q2) |
| Web 대화 메뉴의 삭제와 휴지통 화면, CLI 명령 | 여러 대화 한꺼번에 삭제, 휴지통 비우기 |
| 대화 폴더와 그 대화만 쓰던 뷰 HTML 정리 | 실제 사용자 폴더(대화와 별개, 나중) |
| `per_conversation` MCP 프로세스 종료 | 외부 서버에 삭제를 알리는 장치(운영자 opt-in 훅, §6.2) |
| 스키마 업그레이드 전 자동 백업과 되돌리기 절차 | 정기 백업, 원격 백업, 암호화 |

## 2. 다른 호스트들

대화형 LLM 호스트는 대체로 같은 모양이다.
- 대화 메뉴에 "삭제"가 있고 확인을 한 번 묻는다.
- 지운 대화는 목록에서 바로 사라진다. 복구 기간을 두는 곳도 있고(휴지통·보관 30일 정도), 바로 영구 삭제하는 곳도 있다.
- 지운 대화와 함께 그 대화에 올린 파일도 지워진다. 연결된 외부 서비스에 저장된 것은 그 서비스가 다룬다.

이 설계는 복구 기간을 두는 쪽을 따른다.

## 3. 상태와 전이

```
          trash (실행 중이면 먼저 취소)        begin_purge              purge
 ACTIVE ─────────────────────────────▶ TRASHED ────────────▶ PURGING ──────────▶ (없음)
        ◀───────────────────────────── 
                   restore
```

- **휴지통(TRASHED)** 상태는 `conversations.trashed_at`(UTC 시각) 하나로 나타낸다. 다른 행은 그대로 둔다. 그래서 복원은 이 값을 지우는 것뿐이다.
- 휴지통의 대화는 **아무도 쓸 수 없다**. 목록(제목·미리보기·옮긴 시각), 복원, 영구 삭제만 된다. 다른 모든 Data 포트는 그 대화를 없는 대화처럼 다룬다(NOT_AUTHORIZED, §4.2). 그래서 휴지통으로 옮긴 뒤 늦게 도착한 쓰기는 기록되지 않고 실패한다.
- **영구 삭제 중(PURGING)** 은 `conversations.purge_started_at`으로 나타낸다. 폴더를 지우기 전에 기록한다(§7.2). 이 상태에서는 복원할 수 없고, 남은 일은 영구 삭제를 끝내는 것뿐이다. 서버가 폴더를 지운 직후 죽어도, 재시작 뒤 폴더 없는 대화가 복원되지 않는다.
- **영구 삭제(purge)** 는 휴지통에 있는 대화만 된다. 활성 대화를 바로 영구 삭제하려면 휴지통을 거친다(Web·CLI의 "지금 영구 삭제"는 두 단계를 차례로 부른다).
- 보관 기간: 운영자 설정 `HIMARI_TRASH_DAYS`(기본 30, 1~365; Q1). 서버가 시작할 때와 그 뒤 1시간마다, 보관 기간이 지난 대화와 영구 삭제가 중간에 끊긴 대화(PURGING)를 영구 삭제한다.
- 세 전이(trash·restore·purge)는 대화마다 하나씩만 돈다(§7.3).

## 4. Data

### 4.1 포트 (DATA-12 확장)

```python
@dataclass(frozen=True)
class ConversationSummary(DeepFrozen):
    ...                                   # 지금과 같음
    trashed_at: datetime | None = None    # 휴지통에 옮긴 시각

@dataclass(frozen=True)
class TrashedConversation(DeepFrozen):
    summary: ConversationSummary
    workspace: ConversationWorkspace | None   # 기본 Binding의 Workspace와 cwd (§5)
    tool_ids: frozenset[ToolId]               # 이 대화에서 부른 Tool (§6.1)

class ConversationCatalog(Protocol):
    async def list_conversations(self, principal, *, limit=50, offset=0,
                                 trashed: bool = False) -> Sequence[ConversationSummary]:
        """trashed=False: 활성 대화만(지금과 같음). True: 휴지통만, 옮긴 시각이 최근인 것부터."""
    async def trash_conversation(self, scope: TrustedScope) -> datetime:
        """ACTIVE → TRASHED. 옮긴 시각을 돌려준다. 이미 휴지통이면 INVALID_STATE_TRANSITION."""
    async def restore_conversation(self, scope: TrustedScope) -> None:
        """TRASHED → ACTIVE. TRASHED가 아니면(ACTIVE, PURGING) INVALID_STATE_TRANSITION."""
    async def begin_purge(self, scope: TrustedScope) -> TrashedConversation:
        """TRASHED → PURGING(purge_started_at 기록), 또는 이미 PURGING이면 그대로.
        영구 삭제 전에 정리할 것을 돌려준다. ACTIVE면 INVALID_STATE_TRANSITION."""
    async def purge_conversation(self, scope: TrustedScope) -> None:
        """PURGING → 없음. 한 트랜잭션(§4.3). PURGING이 아니면 INVALID_STATE_TRANSITION."""
    async def used_tool_ids(self, scope: TrustedScope) -> frozenset[ToolId]:
        """활성 대화에서 부른 Tool id들(삭제 확인 창, §6.1). 다른 읽기와 같은 권한 검사."""

class ConversationRetention(Protocol):
    """Bootstrap 전용(Web·Control에 주지 않음): 모든 principal에 걸친 정리."""
    async def due_purges(self, cutoff: datetime) -> Sequence[TrustedScope]:
        """cutoff 전에 휴지통에 옮긴 대화와, PURGING인 대화 전부."""
```

- 이 포트들은 휴지통의 대화에 대해 **owner 검사만** 한다. 다른 사용자의 대화와 없는 대화는 지금처럼 같은 NOT_AUTHORIZED다.
- 상태 전이는 Data가 한 트랜잭션 안에서 검사한다. 그래서 Application의 잠금(§7.3)이 없더라도 잘못된 전이가 기록되지는 않는다. 잠금이 막는 것은 Data 밖의 일(폴더 삭제, Runner)과 전이 사이의 경쟁이다.
- `ConversationRetention`은 principal 범위를 넘는 유일한 읽기다. 그래서 공개 포트 묶음과 분리하고, 조립(Bootstrap)만 받는다. 돌려주는 것은 id뿐이고 내용이 아니다.

### 4.2 휴지통의 대화는 없는 대화처럼

SQLite의 모든 포트는 `_Tx.authorized(scope)`를 거친다(인메모리도 같은 자리가 있다). 여기에 `trashed_at IS NULL`을 더한다. 휴지통의 대화는 다른 사용자의 대화, 없는 대화와 **같은 오류**를 낸다(구별 불가 원칙 그대로). 위 §4.1의 휴지통 포트만 owner만 보는 검사(`owned`)를 쓴다.

결과:
- 휴지통으로 옮긴 뒤 시작하려던 Run·Tool 기록, 늦은 MCP 뷰 조회, 다른 탭의 메시지 전송은 모두 실패하고 아무것도 쓰지 않는다.
- `recover_interrupted`도 휴지통의 대화에는 돌지 않는다. 복원한 뒤 첫 실행에서 지금처럼 돈다.

### 4.3 영구 삭제 트랜잭션

`BEGIN IMMEDIATE` 하나에서, 자식부터:

1. `tool_executions`에서 이 대화가 참조하는 `ui_digest` 목록을 읽어 둔다.
2. `compaction_runs`(이 대화의 Run), `tool_executions`, `runs`, `events`, `idempotency`, `artifacts`, `bindings`, `activations`, `external_settings`, `model_settings`, `conversations` 순서로 지운다.
3. 1의 digest 중 **다른 호출이 참조하지 않는 것**만 `ui_resources`에서 지운다.

- 외래 키(`foreign_keys=ON`)가 순서를 검사한다. 빠뜨린 테이블이 있으면 트랜잭션이 실패해 아무것도 지워지지 않는다. 다음 버전에서 대화 테이블을 더할 때 이 목록에 넣지 않으면 테스트가 실패하도록, 테스트는 `sqlite_master`에서 `conversation_id` 열이 있는 모든 테이블을 찾아 비었는지 본다.
- SQLite는 지운 페이지를 바로 덮어쓰지 않는다. `PRAGMA secure_delete=ON`을 켜서 지운 내용이 파일에 남지 않게 한다(쓰기가 조금 늘어난다). WAL 파일에 남은 이전 페이지는 다음 체크포인트에서 덮인다.

### 4.4 SQLite v7

```sql
ALTER TABLE conversations ADD COLUMN trashed_at TEXT;
ALTER TABLE conversations ADD COLUMN purge_started_at TEXT;  -- NULL이 아니면 trashed_at도 NULL이 아니다
```

v6 → v7 제자리 업그레이드(지금 방식과 같음). 업그레이드 전에 백업한다(§8).

## 5. Workspace 폴더

**지우는 것은 그 대화의 기본 폴더 하나뿐이다.**
- 대화의 기본 Binding의 cwd가 정확히 `conversations/<그 대화 id>`일 때만 지운다. 그 폴더는 대화를 만들 때 Data가 정한 이름이다(`sqlite.py` activate의 기본값).
- cwd가 다르면(앞으로 다른 폴더를 Binding하는 기능이 생기면) 지우지 않는다. 그 폴더는 대화의 것이 아니다.
- 모델이 Root의 다른 곳에 쓴 파일은 남는다. 어느 대화의 것인지 알 수 없다.
- 같은 사용자의 다른 대화가 이 폴더의 파일을 쓰고 있었다면, 그 대화에서는 파일이 사라진다(V3-030의 교차 접근). 이 폴더는 그 대화용이라는 소유자 방향대로다.

**지우는 방법 (Inside Environment):** `InsideEnvironment.remove_conversation_folder(workspace_id, cwd_relative)`.
- 그 Workspace의 쓰기 잠금(`write_lock`)을 잡는다. 같은 Root에서 도는 Inside Tool·명령과 겹치지 않는다(Q-40의 전제: Workspace 변경은 이 잠금 아래에서만).
- `fs.remove_tree(root_fd, relative)`(새 함수): 지금 fs처럼 Root의 디렉터리 fd에서 `openat2`(`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV`)로 내려가며 `unlinkat`한다.
  - 심볼릭 링크는 따라가지 않고 링크 자체를 지운다. 링크가 가리키는 대상은 남는다.
  - 다른 파일시스템(마운트)으로 넘어가지 않는다. 넘어가야 하면 멈추고 실패한다.
  - 쓰기 권한이 없는 디렉터리(명령이 `chmod 500`으로 만든 것)는 자기 소유일 때만 권한을 고쳐 안을 지운다.
- 이 일은 워커 스레드에서 돈다(`run_blocking`). 아주 큰 트리도 이벤트 루프를 막지 않는다.
- 폴더가 이미 없으면 성공이다(멱등).

## 6. 외부 저장소

### 6.1 알리기

삭제 확인 창은 이 대화가 쓴 외부 서버를 보여 준다.
- `Data.used_tool_ids(scope)`로 이 대화에서 부른 Tool id들을 읽는다. MCP Tool id는 `<서버 이름>/<이름>`이다(`mcp/config.py`). 그래서 **기록된 id의 앞부분이 곧 서버 이름이다.** 지금 설정이나 연결 상태와 대조하지 않는다. 대화에서 쓴 뒤 운영자가 그 서버를 설정에서 빼도 이름은 남는다(검수 C1).
- HIMARI 자체 Tool은 뺀다: `ask_user`(앞부분 없음)와 `inside/…`(Inside Environment).
- 그 이름이 지금 설정에 없으면 "(지금은 설정에 없음)"을 붙인다.
- 문구: "이 대화에서 쓴 외부 서버(Calliope)에 저장된 데이터는 지워지지 않습니다."
- 외부 Tool을 쓴 기록이 없을 때도 짧은 일반 문구는 늘 보인다: "외부 서비스에 저장된 데이터는 지워지지 않습니다." Tool 기록 밖의 경로(앞으로 생길 연결 방식 등)로 남은 데이터가 있을 수 있기 때문이다.

### 6.2 지우지 않는 이유와 나중의 훅

- HIMARI는 MCP 서버에 대화 id를 보내지 않는다. `shared` stdio와 HTTP는 세션 하나를 모든 대화가 쓴다. 그래서 서버 쪽 데이터 중 어느 것이 이 대화의 것인지 HIMARI는 모른다.
- MCP 사양에는 "대화가 지워졌다"를 서버에 알리는 방법이 없다. HTTP의 세션 종료(`DELETE` + `Mcp-Session-Id`)는 세션 단위이고, 우리 세션은 모든 대화가 함께 쓴다.
- 서버별 비표준 "삭제 Tool"을 HIMARI가 자동으로 부르는 것은 하지 않는다. 표준 우선 원칙에 맞지 않고, 부작용이 있는 Tool을 사용자의 지시 없이 실행하게 된다.
- **미결(나중):** 필요해지면 운영자가 `mcp.json`에 서버별로 "대화를 영구 삭제할 때 이 Tool을 부른다"를 명시하는 opt-in 훅을 검토한다. 그때는 서버에 어떤 대화 식별자를 보낼지도 함께 정해야 한다.

### 6.3 `per_conversation` 프로세스

휴지통으로 옮길 때 그 대화의 프로세스를 닫는다. `McpConnection.release(key)`(새 메서드): stdio `per_conversation`이면 그 key의 세션을 지금의 닫기 절차(stdin 닫기 → SIGTERM → SIGKILL)로 닫는다. 다른 경우는 아무것도 하지 않는다.
- 휴지통의 대화는 Tool을 부르지 않으므로 그 자리를 다른 대화에 바로 내준다.
- 복원한 뒤 첫 호출은 새 프로세스로 가고, 지금처럼 `RESTART_NOTICE`가 붙는다.

## 7. Application·Control

### 7.1 휴지통으로 옮기기

`Application.trash(scope) -> datetime`. 대화의 수명주기 잠금(§7.3)을 잡은 채로:

0. **먼저 권한을 확인한다**(검수 B4): Data `read_conversation(scope)`. 다른 사용자의 대화나 없는 대화면 NOT_AUTHORIZED(Web 404)로 끝나고, **Runner에는 아무것도 하지 않는다.**
1. **Runner에서 그 대화를 닫는다**(`BackgroundRunner.close(scope, timeout)`, 새 메서드, 아래 계약).
2. `close`가 상한(30초) 안에 끝나지 않으면 닫힘을 되돌리고(`reopen`) 거부한다("아직 멈추는 중입니다. 잠시 후 다시 시도하세요"). Sandbox가 멈춘 경우 등이다(Q-40).
3. Data `trash_conversation`.
4. MCP `release(key)`(§6.3). 실패해도 휴지통 이동은 유지하고 로그만 남긴다. 프로세스는 어차피 idle 종료된다.
5. Runner의 작업 상태(`JobStatus`)를 지운다. 닫힘은 휴지통에 있는 동안 유지한다(복원 때 `reopen`, 영구 삭제 때 `forget`). 3이 실패하면 `reopen`한다.

서버를 다시 시작하면 Runner의 닫힘은 메모리에서 사라진다. 그래도 휴지통의 대화에 대한 `accept`는 Data가 NOT_AUTHORIZED로 거부하므로(§4.2), 작업이 시작되지 않는다.

열린 질문(`ask_user`)이나 멈춘 턴(PAUSED)은 그대로 남는다. 복원하면 그 상태 그대로 돌아온다.

**`BackgroundRunner.close`의 계약 (검수 B2).** 지금 `submit`은 "실행 중인지 검사 → `await driver.accept()` → `_start()`" 순서다. `accept`가 기다리는 동안 닫힐 수 있으므로, 입구에서 한 번 검사하는 것으로는 부족하다. 그래서 Runner는 대화마다 다음 두 가지를 둔다.
- `_closed`: 닫힌 대화.
- `_admitting`: `accept`에 들어갔지만 아직 끝나지 않은 접수 수.

규칙은 다음과 같다.
- `submit`은 들어올 때 닫혔으면 `ControlError(CLOSED)`로 거부한다. 아니면 `_admitting`을 하나 올리고 `accept`를 부르고, 끝나면(성공이든 실패든) 내린다.
- **`_start`는 항상 `_closed`를 다시 확인한다**(`submit`, `submit_compaction`, `submit_cancel` 모두). 닫혔으면 작업을 시작하지 않고 `ControlError(CLOSED)`를 낸다. `_start`는 `await`가 없는 동기 함수라, 이 확인과 시작 사이에 닫힘이 끼어들 수 없다.
- `close(scope, timeout)`는 다음을 차례로 한다. 끝나면 그 대화에는 접수 중인 요청도 실행 중인 작업도 없고, 이후의 어떤 `_start`도 성공하지 않는다.
  1. `_closed`에 넣는다.
  2. `_admitting`이 0이 될 때까지 기다린다.
  3. 실행 중인 작업이 있으면 Cancel 신호를 보내고(V3-P55: 진행 중인 호출을 끊는다) 끝나기를 기다린다.
  4. 2와 3을 합쳐 `timeout` 안에 끝나지 않으면 `TimeoutError`를 낸다(닫힘은 유지되고, Application이 `reopen`한다).
- 닫히기 직전에 `accept`가 끝난 메시지는 이미 기록되어 있다(CTRL-06: 기록된 뒤에만 접수를 알린다). 그 요청은 `CLOSED` 오류를 받는다. 메시지는 대화에 남고, 복원하면 답이 없는 메시지로 보여 Continue로 이어 간다. 이미 휴지통이면 `accept` 자체가 NOT_AUTHORIZED로 실패한다(§4.2).
- `ControlErrorCode.CLOSED`(새 값)는 Web에서 409이고, 지금의 오류 형식을 따른다: `{"error": {"code": "conversation_closed", "message": "This conversation is being deleted."}}`(검수 C2).
- **Runner는 권한을 검사하지 않는다**(지금도 그렇다). `_closed`·`_admitting`은 지금의 `_running_in`처럼 **대화 id**로 묶는다. 대화 id는 Data가 만들고 주인이 하나뿐이다. 그래서 `close`·`reopen`·`forget`은 Application이 그 대화의 권한을 확인한 뒤에만 부른다: `trash`는 0단계, `restore`는 Data `restore_conversation`이 성공한 뒤, `purge`는 Data `begin_purge`가 성공한 뒤.

### 7.2 복원·영구 삭제

- `Application.restore(scope)`: Data `restore_conversation`(권한·상태 검사), 그것이 성공한 뒤에만 Runner의 닫힘을 푼다(`reopen`).
- `Application.purge(scope)`:
  1. Data `begin_purge`: PURGING으로 바꾸고 폴더(§5)를 얻는다. 이 순간부터 복원은 거부된다.
  2. 폴더를 지운다.
  3. Data `purge_conversation`.
  4. Runner의 그 대화 기록(닫힘·상태)을 지운다(`forget`).

  **폴더를 먼저 지운다.** 2와 3 사이에 서버가 죽으면 대화는 PURGING으로 남고 폴더만 없다. 재시작 뒤 복원은 거부되고(1의 기록), 다음 정리가 다시 돌아 끝낸다(1과 2는 멱등). 반대 순서면 Data 기록 없이 폴더만 남아 아무도 찾지 못한다.
- **자동 정리:** 시작할 때와 1시간마다 `due_purges(now − HIMARI_TRASH_DAYS)`의 대화를 하나씩 `purge`한다. 하나가 실패하면(폴더 삭제 실패 등) 로그를 남기고 다음 대화로 넘어간다. 다음 차례에 다시 시도한다.

### 7.3 전이는 대화마다 하나씩 (검수 B1)

`trash`, `restore`, `purge`(자동 정리 포함)는 **대화마다 하나인 수명주기 잠금**(`Application._lifecycle[conversation_id]`, `asyncio.Lock`)을 잡고 돈다. `purge`는 1~4 동안 잠금을 놓지 않는다.

이 잠금이 없으면 다음 일이 생긴다. 자동 정리가 휴지통 상태를 확인한 뒤 사용자가 복원하고, 그 뒤 정리가 폴더를 지운다. 대화는 복원되었는데 파일이 없다.

- P0는 서버 프로세스가 하나다(V3-045, SQLite 단일 writer 잠금). 그래서 프로세스 안의 잠금으로 충분하다. 다중 worker(P2)에서는 lease와 함께 다시 설계한다.
- 서버가 죽는 경우는 잠금이 아니라 PURGING 기록이 막는다(§3).
- 잠금은 대화 하나의 전이끼리만 막는다. 다른 대화와 다른 작업(메시지, 읽기)은 막지 않는다. 메시지와의 경쟁은 Runner의 닫힘(§7.1)과 Data의 상태 검사(§4.2)가 맡는다.
- 다 쓴 잠금(그 대화를 영구 삭제함)은 사전에서 지운다.

## 8. 업그레이드 전 백업과 되돌리기

### 8.1 자동 백업

`SqliteStore`가 DB를 열 때 **업그레이드가 필요하면**(이전 버전이고 그 스키마와 정확히 같을 때, 지금 규칙) 업그레이드 트랜잭션 전에 백업을 만든다.
- 파일: DB와 같은 디렉터리의 `himari.db.v<이전 버전>-<UTC 시각>.bak`.
- 단일 writer 잠금(`flock`)을 잡은 뒤에 만든다. 그래서 복사 중에 DB가 바뀌지 않는다.

**업그레이드 트랜잭션에 들어가기 전에 다음이 모두 끝나야 한다(검수 B3).** 하나라도 실패하면 **업그레이드하지 않고 시작을 거부한다.** 오류에 경로와 이유를 담고, DB는 이전 버전 그대로다. 실패하면 만든 파일을 지운다. 1~5에서 실패하면 `.partial`을 지운다. 6에서 실패하면 이미 이름이 바뀌었으므로 최종 이름의 파일을 지운다(검수 C3). DB가 업그레이드되지 않았으니 그 백업은 필요 없고, 다음 시작에서 새 시각의 이름으로 다시 만든다. 지우기마저 실패하면 남은 파일의 경로를 오류에 함께 적는다.
1. 임시 이름 `<백업 이름>.partial`을 지금의 비공개 파일 규칙으로 새로 만든다(0600, 이 사용자 소유, `O_EXCL | O_NOFOLLOW`).
2. SQLite 온라인 백업 API(`Connection.backup`)로 그 파일에 복사하고 닫는다.
3. 그 파일을 읽기 전용으로 다시 연다. 그리고 다음을 확인한다: `PRAGMA integrity_check`가 `ok`, `application_id`가 HIMARI, `user_version`이 이전 버전, 스키마가 이전 버전과 정확히 같음(지금의 서명 검사).
4. 파일을 `fsync`한다.
5. 최종 이름으로 `rename`한다. 같은 이름이 이미 있으면 덮어쓰지 않고 실패한다(`link` 후 `unlink`, 또는 `renameat2(RENAME_NOREPLACE)`).
6. **부모 디렉터리를 `fsync`한다.** 파일 내용의 `fsync`만으로는 새 디렉터리 항목이 전원 차단 뒤에 남는다고 보장되지 않는다.

- 백업 연결이 만드는 `-journal` 파일도 지금 규칙대로 비공개인지 확인한다.
- 새 DB(빈 파일)나 같은 버전이면 백업하지 않는다.
- 백업은 자동으로 지우지 않는다(Q3). 시작 로그에 경로를 남긴다.
- **백업에는 그 시점의 모든 대화가 들어 있다.** 그 뒤에 영구 삭제한 대화도 백업에는 남는다. 운영 문서에 이 사실과 함께, 되돌릴 필요가 없어지면 백업을 지우라고 적는다(검수 Q3 의견).

### 8.2 되돌리기 절차 (운영자)

이전 릴리스는 새 스키마를 거부한다(`unsupported schema version`). 그래서 되돌리려면 DB도 함께 되돌린다.

```bash
sudo systemctl stop himari-v3
# 이전 릴리스로 바꾼다 (WorkingDirectory·ExecStart를 이전 <sha8>로)
sudo -u himari-v3 mv /srv/himari-v3/himari.db /srv/himari-v3/himari.db.v7-failed
sudo -u himari-v3 rm -f /srv/himari-v3/himari.db-wal /srv/himari-v3/himari.db-shm
sudo -u himari-v3 cp --preserve=mode /srv/himari-v3/himari.db.v6-<시각>.bak /srv/himari-v3/himari.db
sudo systemctl start himari-v3
```

- 업그레이드 뒤에 생긴 대화와 메시지는 잃는다. 이 절차는 업그레이드 직후 문제를 발견했을 때 쓴다.
- 정확한 경로는 배포 기록(`/opt/himari-v3/deployment.json`)의 `data_root`를 따른다. 구현 PR에서 이 절차를 운영 문서에 넣고, 이번 v7 배포 때 실제로 한 번 되돌려 본다(지금 DB는 잃어도 되므로).

## 9. Web·CLI

**Web API**

| 요청 | 동작 |
| --- | --- |
| `GET /api/conversations/{id}/delete-notice` | `{servers: [이름…]}` (§6.1) |
| `POST /api/conversations/{id}/trash` | 휴지통으로(§7.1). 200 `{trashed_at, purge_after}`. 멈추는 중이면 409 |
| `POST /api/conversations/{id}/restore` | 복원. 휴지통이 아니면 409 |
| `DELETE /api/conversations/{id}` | 영구 삭제. 휴지통이 아니면 409. 204 |
| `GET /api/conversations?trashed=1` | 휴지통 목록 |

휴지통의 대화에 대한 다른 요청은 모두 404다(Data의 NOT_AUTHORIZED와 같음).

**Web 화면**
- 대화 메뉴에 "대화 삭제"를 둔다. 확인 창에는 "휴지통으로 옮깁니다. N일 뒤 영구 삭제됩니다", 외부 서버 문구(§6.1), 실행 중이면 "진행 중인 작업을 멈춥니다"를 보여 준다.
- 옮긴 뒤에는 대화 목록으로 돌아간다.
- 사이드바 아래에 "휴지통"을 둔다. 목록에는 제목·옮긴 시각·영구 삭제 예정일을 보이고, 항목마다 "복원"과 "지금 영구 삭제"(확인 한 번 더)를 둔다.

**CLI**
- `himari delete ID [--now]`: 휴지통으로. `--now`면 이어서 영구 삭제. 둘 다 확인 질문(y/N)을 하고, `--yes`로 생략한다.
- `himari trash`: 휴지통 목록. `himari restore ID`: 복원.
- 대화 안에서 `/delete`: 확인 뒤 휴지통으로 옮기고 대화를 끝낸다.

## 10. 계약 변경 (Proposed)

| 모듈 | 변경 |
| --- | --- |
| Data | `ConversationSummary.trashed_at`, `TrashedConversation`, `ConversationCatalog`의 trash·restore·begin_purge·purge·used_tool_ids, `list_conversations(trashed=)`, Bootstrap 전용 `ConversationRetention.due_purges`. 휴지통 대화는 다른 포트에서 NOT_AUTHORIZED. SQLite v7 `trashed_at`·`purge_started_at`, `secure_delete`, 검증·`fsync`한 업그레이드 전 백업 |
| Environment (Inside) | `remove_conversation_folder`, `fs.remove_tree` |
| MCP | `McpConnection.release(key)` |
| Control | `BackgroundRunner.close(scope, timeout)`·`reopen`·`forget`(§7.1 계약), `ControlErrorCode.CLOSED` |
| Bootstrap | `HIMARI_TRASH_DAYS`, `Application.trash`·`restore`·`purge`·`delete_notice`, 대화별 수명주기 잠금(§7.3), 자동 정리 작업 |
| Web·CLI | §9 |
| Prompt·Provider·Tool | 없음 |

## 11. 소유자 결정

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 휴지통 보관 기간 | (a) 30일, 운영자가 `HIMARI_TRASH_DAYS`로 바꿈 (b) 7일 (c) 기간 없음(사용자가 직접 비울 때까지) | **(a)** — 다른 호스트의 흔한 값. 원문 Provider 기록이 들어 있으므로 무기한(c)은 두지 않는다 |
| Q2 | 휴지통의 대화 보기 | (a) 목록만(제목·미리보기·시각). 내용은 복원해서 본다 (b) 읽기 전용으로 내용까지 | **(a)** — 휴지통 대화를 모든 포트에서 없는 대화로 다루는 단순한 규칙(§4.2)을 지킨다. (b)는 읽기 포트마다 예외가 필요하다 |
| Q3 | 업그레이드 백업 보관 | (a) 자동으로 지우지 않음(운영자가 지움) (b) 최근 3개만 남기고 자동 삭제 | **(a)** — 업그레이드는 드물고, 백업을 자동으로 지우는 장치가 오히려 위험하다. 경로는 시작 로그에 남긴다 |

**소유자 결정(2026-10-21): Q1 (a), Q2 (a), Q3 (a) — 모두 권고대로.**

**이미 정해진 것(2026-10-21, 소유자):** 휴지통 → N일 뒤 영구 삭제. 대화 폴더는 같이 지움. 외부 서버 데이터는 지우지 않고 알림. 실행 중이면 먼저 취소. 삭제와 백업을 한 묶음으로.

## 12. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 다른 사용자의 대화를 지움 | 모든 휴지통 포트가 owner를 검사하고, 다른 사용자·없는 대화는 같은 NOT_AUTHORIZED |
| 휴지통으로 옮기는 중에 다른 탭이 새 작업을 시작 | Runner를 먼저 닫는다. `accept` 중이던 요청은 끝날 때까지 기다리고, `_start`가 닫힘을 다시 확인한다(§7.1 계약, B2). 그래도 늦게 온 쓰기는 Data가 거부(§4.2) |
| 자동 정리와 복원이 겹쳐 복원된 대화의 폴더가 지워짐 | 대화별 수명주기 잠금으로 전이를 하나씩(§7.3, B1). 폴더를 지우기 전에 PURGING을 기록해 복원 거부(§3) |
| 폴더를 지운 직후 서버가 죽고, 재시작 뒤 그 대화를 복원 | PURGING이면 복원 거부, 다음 정리가 끝냄(§7.2) |
| 업그레이드는 끝났는데 전원 차단으로 백업 파일이 사라짐 | 내용·디렉터리 모두 `fsync`하고 검증한 뒤에만 업그레이드(§8.1, B3) |
| 다른 사용자가 남의 대화 id로 삭제를 요청해 주인의 작업 접수를 막음 | 수명주기 잠금 뒤 Runner를 건드리기 전에 Data로 권한 확인(§7.1의 0, B4) |
| 쓴 서버가 설정에서 빠져 외부 데이터 안내가 사라짐 | 기록된 Tool id의 앞부분(서버 이름)으로 알리고, 일반 문구는 늘 보임(§6.1, C1) |
| 진행 중인 Tool이 취소를 무시해 영원히 기다림 | 30초 상한 뒤 거부, 상태는 그대로(§7.1의 2) |
| 폴더 안의 심볼릭 링크를 따라가 Root 밖이나 다른 대화의 파일을 지움 | `RESOLVE_BENEATH`·`NO_SYMLINKS`·`NO_XDEV`, 링크는 링크만 지움(§5) |
| 같은 Root의 명령이 실행 중인 폴더를 지움 | Workspace 쓰기 잠금 아래에서만 지움(§5) |
| 다른 대화가 여전히 보는 MCP 뷰 HTML을 지움 | 참조가 없는 digest만, 같은 트랜잭션에서 확인(§4.3) |
| 대화 테이블을 더하고 삭제 목록에 빠뜨림 | 외래 키로 트랜잭션 실패 + `conversation_id` 열이 있는 모든 테이블을 확인하는 테스트(§4.3) |
| 영구 삭제 중 서버가 죽음 | 폴더 먼저, Data 나중. 다음 정리 때 다시 돈다(§7.2) |
| 지운 내용이 DB 파일에 남음 | `secure_delete=ON`(§4.3). 백업 파일에는 남는다. 백업은 업그레이드 시점의 것이고 운영자가 지운다(Q3, 운영 문서에 명시) |
| 업그레이드가 중간에 실패해 DB가 망가짐 | 업그레이드는 지금도 한 트랜잭션. 그 앞에 백업, 백업 실패면 시작 거부(§8.1) |
| 되돌린 이전 릴리스가 새 DB를 열지 못함 | 백업으로 DB도 되돌리는 절차(§8.2), 이번 배포에서 실제로 시험 |

## 13. 검증 계획

- **Data (인메모리·SQLite 양쪽):**
  - 상태 전이: trash·restore·begin_purge·purge, 잘못된 전이의 INVALID_STATE_TRANSITION. PURGING이면 복원 거부, `begin_purge`는 다시 불러도 같음.
  - 휴지통 대화에 대한 모든 다른 포트의 NOT_AUTHORIZED(포트 목록을 돌며).
  - purge 뒤 `conversation_id` 열이 있는 모든 테이블에 그 대화 행이 없음.
  - 공유 `ui_digest`는 다른 대화가 참조하면 남고, 아무도 참조하지 않으면 지워짐.
  - 각 쓰기 직후 오류를 주입하면 purge 전체가 롤백.
  - `due_purges`: 기간이 지난 휴지통 대화와 PURGING 대화 전부, 다른 사용자의 것도 포함.
  - `used_tool_ids`: 서버를 설정에서 뺀 뒤에도 그 이름이 나옴.
- **SQLite 업그레이드:** v6 → v7. 백업 파일이 원본과 같은 내용이고 0600이다. 백업이 실패하면(쓰기 금지 디렉터리, 검증 실패, 같은 이름의 파일이 이미 있음) 시작을 거부하고, DB는 v6 그대로이며 `.partial`이 남지 않는다. `fsync`가 파일과 부모 디렉터리에 불리는지 확인한다(`os.fsync`를 감싸 기록). 부모 디렉터리 `fsync` 실패를 주입하면 최종 이름의 백업도 남지 않는다(C3). 같은 버전과 새 DB에서는 백업하지 않는다.
- **fs.remove_tree:**
  - Root 밖을 가리키는 링크와 다른 대화 폴더를 가리키는 링크는 대상이 남음.
  - 깊은 트리, 권한 500 디렉터리, 이미 없는 폴더.
  - 마운트 경계(`HIMARI_SANDBOX_HOST=1` 게이트에서).
- **Application·Control:**
  - 실행 중인 대화를 휴지통으로 옮기면 작업이 취소되고 Run·Tool 기록이 종결된 뒤 옮겨짐.
  - **B4 회귀:** 다른 사용자가 실행 중인 대화를 휴지통으로 옮기려 하면 404다. 원래 주인의 작업은 계속 돌고, 그 사이 주인의 submit도 CLOSED를 받지 않으며, Runner 상태(`JobStatus`, 닫힘, 접수 수)가 그대로다. 복원과 영구 삭제도 같은 방식으로 확인한다.
  - 옮기는 중의 submit은 CLOSED이고, 응답은 지금의 오류 형식이다.
  - **B2 회귀:** `driver.accept`를 멈춰 둔 채 `trash`를 부른다. `trash`는 `accept`가 끝날 때까지 기다리고, 그 submit은 작업을 시작하지 않고 CLOSED를 받는다. 메시지는 기록되어 있고, 복원 뒤 Continue로 이어진다.
  - `submit_compaction`·`submit_cancel`도 닫힌 뒤에는 시작되지 않음.
  - **B1 회귀:** 폴더 삭제를 멈춰 둔 채 `restore`를 부르면, `restore`는 잠금을 기다린 뒤 거부된다(대화는 이미 PURGING). 반대로 `restore`가 먼저 잡으면 `purge`는 `begin_purge`에서 거부되고 폴더는 남는다.
  - 폴더 삭제 뒤 `purge_conversation` 전에 실패를 주입하면, 재시작 뒤 복원은 거부되고 다음 정리가 끝낸다.
  - 30초 상한.
  - purge의 순서와 중간 실패 뒤 재시도.
  - 자동 정리.
  - `per_conversation` 프로세스가 종료되고, 복원 뒤 새 프로세스와 `RESTART_NOTICE`.
- **Web·CLI:** API 계약과 권한(다른 사용자, 휴지통 대화에 대한 다른 요청 404). 브라우저 스모크: 삭제 확인 창(외부 서버 문구) → 목록에서 사라짐 → 휴지통에서 복원 → 다시 삭제 → 지금 영구 삭제.
- **배포:** v7 배포 때 백업 생성 확인 → 되돌리기 절차를 한 번 실제로 수행 → 다시 v7로.

## 14. 검수 처리 — rev. 1(`14f3d79`), 독립 검수: NOT_READY

| # | 지적 | 처리 |
| --- | --- | --- |
| B1 | 영구 삭제가 휴지통 상태를 확인한 뒤 복원이 끼어들면, 복원된 대화의 폴더가 지워진다 | 대화별 수명주기 잠금으로 trash·restore·purge를 하나씩 돌리고, purge는 끝까지 잠금을 쥔다(§7.3). 서버가 죽는 경우를 위해 폴더 삭제 전에 PURGING(`purge_started_at`)을 기록하고, 그 상태에서는 복원을 거부한다(§3, §4.1, §7.2) |
| B2 | `submit`의 `await driver.accept()` 동안 Runner가 닫히면, 닫힌 뒤에 `_start`가 돈다 | `close`의 계약을 정했다: 접수 중인 요청 수를 세어 끝나기를 기다리고, `_start`가 매번 닫힘을 다시 확인한다(동기 함수라 끼어들 틈이 없음). 닫히기 직전에 기록된 메시지는 남고, 그 요청은 CLOSED를 받는다(§7.1). 회귀 테스트 추가(§13) |
| B3 | 백업 파일의 `fsync`만으로는 디렉터리 항목이 남는다고 보장되지 않는다 | 업그레이드 전의 완료 조건을 정했다: `.partial`에 쓰기 → 다시 열어 검증 → 파일 `fsync` → 덮어쓰지 않는 `rename` → 부모 디렉터리 `fsync`. 하나라도 실패하면 업그레이드하지 않는다(§8.1) |
| C1 | 지금 연결된 서버 목록과만 대조하면, 설정에서 빠진 서버의 안내가 사라진다 | 기록된 Tool id의 앞부분(`<서버>/…`)을 서버 이름으로 쓰고 지금 설정과 대조하지 않는다. 일반 문구는 늘 보인다(§6.1) |
| Q3 의견 | 영구 삭제한 대화가 이전 백업에는 남는다 | §8.1과 §12에 적고, 운영 문서에 넣는다 |

## 15. 재검수 — rev. 2(`8e4c920`): NOT_READY

B1~B3, C1은 PASS다. 새로 나온 지적과 처리는 다음과 같다.

| # | 지적 | 처리 |
| --- | --- | --- |
| B4 | `trash`가 Data로 권한을 확인하기 전에 Runner를 닫는다. Runner는 대화 id로 묶으므로, 남의 대화 id로 온 요청이 주인의 접수를 막을 수 있다 | 수명주기 잠금 → Data `read_conversation`으로 권한 확인 → Runner `close` → Data `trash_conversation` 순서(§7.1의 0). Runner는 권한을 검사하지 않고, Application이 권한을 확인한 뒤에만 `close`·`reopen`·`forget`을 부른다고 계약에 적었다(복원·영구 삭제도). 회귀 테스트 추가(§13) |
| C2 | `CLOSED`의 오류 예시가 지금의 Web 오류 형식과 다르다 | `{"error": {"code": "conversation_closed", "message": …}}`로 통일(§7.1) |
| C3 | 부모 디렉터리 `fsync`가 실패할 때는 이미 최종 이름이다 | 그 단계에서 실패하면 최종 이름의 파일을 지우고, 지우기도 실패하면 경로를 오류에 적는다(§8.1). 테스트 추가(§13) |
