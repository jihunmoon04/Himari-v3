# 대화 삭제와 업그레이드 백업 v0.1

> 2026-10-21 · **PROPOSED (V3-P65)** · rev. 1 — 설계 초안. 구현 없음.
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
          trash (실행 중이면 먼저 취소)          purge (지금 / 보관 기간 지남)
 ACTIVE ───────────────────────────────▶ TRASHED ───────────────────────────────▶ (없음)
        ◀─────────────────────────────── 
                    restore
```

- **휴지통(TRASHED)** 상태는 `conversations.trashed_at`(UTC 시각) 하나로 나타낸다. 다른 행은 그대로 둔다. 그래서 복원은 이 값을 지우는 것뿐이다.
- 휴지통의 대화는 **아무도 쓸 수 없다**. 목록(제목·미리보기·옮긴 시각), 복원, 영구 삭제만 된다. 다른 모든 Data 포트는 그 대화를 없는 대화처럼 다룬다(NOT_AUTHORIZED, §4.2). 그래서 휴지통으로 옮긴 뒤 늦게 도착한 쓰기는 기록되지 않고 실패한다.
- **영구 삭제(purge)** 는 휴지통에 있는 대화만 된다. 활성 대화를 바로 영구 삭제하려면 휴지통을 거친다(Web·CLI의 "지금 영구 삭제"는 두 단계를 차례로 부른다).
- 보관 기간: 운영자 설정 `HIMARI_TRASH_DAYS`(기본 30, 1~365; Q1). 서버가 시작할 때와 그 뒤 1시간마다, 보관 기간이 지난 대화를 영구 삭제한다.

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
        """TRASHED → ACTIVE. 휴지통이 아니면 INVALID_STATE_TRANSITION."""
    async def read_trashed(self, scope: TrustedScope) -> TrashedConversation:
        """영구 삭제 전에 정리할 것. 휴지통이 아니면 INVALID_STATE_TRANSITION."""
    async def purge_conversation(self, scope: TrustedScope) -> None:
        """TRASHED → 없음. 한 트랜잭션(§4.3). 휴지통이 아니면 INVALID_STATE_TRANSITION."""
    async def used_tool_ids(self, scope: TrustedScope) -> frozenset[ToolId]:
        """활성 대화에서 부른 Tool id들(삭제 확인 창, §6.1). 다른 읽기와 같은 권한 검사."""

class ConversationRetention(Protocol):
    """Bootstrap 전용(Web·Control에 주지 않음): 모든 principal에 걸친 정리."""
    async def trashed_before(self, cutoff: datetime) -> Sequence[TrustedScope]: ...
```

- 이 포트들은 휴지통의 대화에 대해 **owner 검사만** 한다. 다른 사용자의 대화와 없는 대화는 지금처럼 같은 NOT_AUTHORIZED다.
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
- `Data.used_tool_ids(scope)`로 이 대화에서 부른 Tool id들을 읽고, 시작 때 연결한 서버별 Tool 목록(`SourceStatus.tool_ids`)과 맞춰 서버 이름을 얻는다(Application).
- 문구: "이 대화에서 쓴 외부 서버(Calliope)에 저장된 데이터는 지워지지 않습니다."
- 쓴 서버가 없으면 이 문구는 없다.

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

`Application.trash(scope) -> datetime`:

1. **Runner에서 그 대화를 닫는다**(`BackgroundRunner.close(scope)`, 새 메서드). 그 뒤의 `submit`·`submit_compaction`·`submit_cancel`은 BUSY로 거부된다. 그래서 2와 4 사이에 다른 탭에서 새 작업이 시작되지 않는다.
2. 실행 중인 작업이 있으면 Cancel 신호를 보내고(V3-P55: 진행 중인 호출을 끊는다) 그 작업이 끝나기를 기다린다. 상한은 30초다. 넘으면 1을 되돌리고 거부한다("아직 멈추는 중입니다. 잠시 후 다시 시도하세요"). Sandbox가 멈춘 경우 등이다(Q-40).
3. Data `trash_conversation`.
4. MCP `release(key)`(§6.3). 실패해도 휴지통 이동은 유지하고 로그만 남긴다. 프로세스는 어차피 idle 종료된다.
5. Runner의 그 대화 상태를 지운다. 3이 실패하면 1을 되돌린다.

열린 질문(`ask_user`)이나 멈춘 턴(PAUSED)은 그대로 남는다. 복원하면 그 상태 그대로 돌아온다.

### 7.2 복원·영구 삭제

- `Application.restore(scope)`: Data `restore_conversation`, 그리고 Runner의 닫힘을 푼다.
- `Application.purge(scope)`:
  1. Data `read_trashed`로 폴더(§5)를 얻는다.
  2. 폴더를 지운다.
  3. Data `purge_conversation`.

  **폴더를 먼저 지운다.** 2와 3 사이에 서버가 죽으면 대화는 휴지통에 남고 폴더만 없다. 다음 정리 때 다시 돌아 끝난다(2는 멱등). 반대 순서면 Data 기록 없이 폴더만 남아 아무도 찾지 못한다.
- **자동 정리:** 시작할 때와 1시간마다 `trashed_before(now − HIMARI_TRASH_DAYS)`의 대화를 하나씩 `purge`한다. 하나가 실패하면(폴더 삭제 실패 등) 로그를 남기고 다음 대화로 넘어간다. 다음 차례에 다시 시도한다.

## 8. 업그레이드 전 백업과 되돌리기

### 8.1 자동 백업

`SqliteStore`가 DB를 열 때 **업그레이드가 필요하면**(이전 버전이고 그 스키마와 정확히 같을 때, 지금 규칙) 업그레이드 트랜잭션 전에 백업을 만든다.
- 파일: DB와 같은 디렉터리의 `himari.db.v<이전 버전>-<UTC 시각>.bak`. 지금의 비공개 파일 규칙(0600, 이 사용자 소유, 링크 따라가지 않음)으로 새로 만든다.
- 방법: SQLite 온라인 백업 API(`Connection.backup`)로 그 파일에 복사한 뒤 `fsync`한다. 단일 writer 잠금(`flock`)을 잡은 뒤라 복사 중에 바뀌지 않는다.
- 백업이 실패하면(디스크 부족 등) **업그레이드하지 않고 시작을 거부한다**. 오류에 경로와 이유를 담는다. DB는 이전 버전 그대로다.
- 새 DB(빈 파일)나 같은 버전이면 백업하지 않는다.
- 백업은 자동으로 지우지 않는다(Q3). 시작 로그에 경로를 남긴다.

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
| Data | `ConversationSummary.trashed_at`, `TrashedConversation`, `ConversationCatalog`의 trash·restore·read_trashed·purge·used_tool_ids, `list_conversations(trashed=)`, Bootstrap 전용 `ConversationRetention`. 휴지통 대화는 다른 포트에서 NOT_AUTHORIZED. SQLite v7 `trashed_at`, `secure_delete`, 업그레이드 전 백업 |
| Environment (Inside) | `remove_conversation_folder`, `fs.remove_tree` |
| MCP | `McpConnection.release(key)` |
| Control | `BackgroundRunner.close`·`reopen`·`forget` |
| Bootstrap | `HIMARI_TRASH_DAYS`, `Application.trash`·`restore`·`purge`·`delete_notice`, 자동 정리 작업 |
| Web·CLI | §9 |
| Prompt·Provider·Tool | 없음 |

## 11. 소유자 결정

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 휴지통 보관 기간 | (a) 30일, 운영자가 `HIMARI_TRASH_DAYS`로 바꿈 (b) 7일 (c) 기간 없음(사용자가 직접 비울 때까지) | **(a)** — 다른 호스트의 흔한 값. 원문 Provider 기록이 들어 있으므로 무기한(c)은 두지 않는다 |
| Q2 | 휴지통의 대화 보기 | (a) 목록만(제목·미리보기·시각). 내용은 복원해서 본다 (b) 읽기 전용으로 내용까지 | **(a)** — 휴지통 대화를 모든 포트에서 없는 대화로 다루는 단순한 규칙(§4.2)을 지킨다. (b)는 읽기 포트마다 예외가 필요하다 |
| Q3 | 업그레이드 백업 보관 | (a) 자동으로 지우지 않음(운영자가 지움) (b) 최근 3개만 남기고 자동 삭제 | **(a)** — 업그레이드는 드물고, 백업을 자동으로 지우는 장치가 오히려 위험하다. 경로는 시작 로그에 남긴다 |

**이미 정해진 것(2026-10-21, 소유자):** 휴지통 → N일 뒤 영구 삭제. 대화 폴더는 같이 지움. 외부 서버 데이터는 지우지 않고 알림. 실행 중이면 먼저 취소. 삭제와 백업을 한 묶음으로.

## 12. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 다른 사용자의 대화를 지움 | 모든 휴지통 포트가 owner를 검사하고, 다른 사용자·없는 대화는 같은 NOT_AUTHORIZED |
| 휴지통으로 옮기는 중에 다른 탭이 새 작업을 시작 | Runner를 먼저 닫는다(§7.1의 1). 그래도 늦게 온 쓰기는 Data가 거부(§4.2) |
| 진행 중인 Tool이 취소를 무시해 영원히 기다림 | 30초 상한 뒤 거부, 상태는 그대로(§7.1의 2) |
| 폴더 안의 심볼릭 링크를 따라가 Root 밖이나 다른 대화의 파일을 지움 | `RESOLVE_BENEATH`·`NO_SYMLINKS`·`NO_XDEV`, 링크는 링크만 지움(§5) |
| 같은 Root의 명령이 실행 중인 폴더를 지움 | Workspace 쓰기 잠금 아래에서만 지움(§5) |
| 다른 대화가 여전히 보는 MCP 뷰 HTML을 지움 | 참조가 없는 digest만, 같은 트랜잭션에서 확인(§4.3) |
| 대화 테이블을 더하고 삭제 목록에 빠뜨림 | 외래 키로 트랜잭션 실패 + `conversation_id` 열이 있는 모든 테이블을 확인하는 테스트(§4.3) |
| 영구 삭제 중 서버가 죽음 | 폴더 먼저, Data 나중. 다음 정리 때 다시 돈다(§7.2) |
| 지운 내용이 DB 파일에 남음 | `secure_delete=ON`(§4.3). 백업 파일에는 남는다. 백업은 업그레이드 시점의 것이고 운영자가 지운다(Q3) |
| 업그레이드가 중간에 실패해 DB가 망가짐 | 업그레이드는 지금도 한 트랜잭션. 그 앞에 백업, 백업 실패면 시작 거부(§8.1) |
| 되돌린 이전 릴리스가 새 DB를 열지 못함 | 백업으로 DB도 되돌리는 절차(§8.2), 이번 배포에서 실제로 시험 |

## 13. 검증 계획

- **Data (인메모리·SQLite 양쪽):**
  - 상태 전이: trash·restore·purge, 잘못된 전이의 INVALID_STATE_TRANSITION.
  - 휴지통 대화에 대한 모든 다른 포트의 NOT_AUTHORIZED(포트 목록을 돌며).
  - purge 뒤 `conversation_id` 열이 있는 모든 테이블에 그 대화 행이 없음.
  - 공유 `ui_digest`는 다른 대화가 참조하면 남고, 아무도 참조하지 않으면 지워짐.
  - 각 쓰기 직후 오류를 주입하면 purge 전체가 롤백.
  - `trashed_before`.
- **SQLite 업그레이드:** v6 → v7. 백업 파일이 원본과 같은 내용이고 0600이다. 백업 실패(쓰기 금지 디렉터리 등)면 시작을 거부하고 DB는 v6 그대로다. 같은 버전과 새 DB에서는 백업하지 않는다.
- **fs.remove_tree:**
  - Root 밖을 가리키는 링크와 다른 대화 폴더를 가리키는 링크는 대상이 남음.
  - 깊은 트리, 권한 500 디렉터리, 이미 없는 폴더.
  - 마운트 경계(`HIMARI_SANDBOX_HOST=1` 게이트에서).
- **Application·Control:**
  - 실행 중인 대화를 휴지통으로 옮기면 작업이 취소되고 Run·Tool 기록이 종결된 뒤 옮겨짐.
  - 옮기는 중의 submit은 BUSY.
  - 30초 상한.
  - purge의 순서와 중간 실패 뒤 재시도.
  - 자동 정리.
  - `per_conversation` 프로세스가 종료되고, 복원 뒤 새 프로세스와 `RESTART_NOTICE`.
- **Web·CLI:** API 계약과 권한(다른 사용자, 휴지통 대화에 대한 다른 요청 404). 브라우저 스모크: 삭제 확인 창(외부 서버 문구) → 목록에서 사라짐 → 휴지통에서 복원 → 다시 삭제 → 지금 영구 삭제.
- **배포:** v7 배포 때 백업 생성 확인 → 되돌리기 절차를 한 번 실제로 수행 → 다시 v7로.
