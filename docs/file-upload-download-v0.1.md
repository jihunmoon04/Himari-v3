# 파일 업로드·다운로드 v0.1

> 2026-10-22 · **PROPOSED (V3-P66)** · rev. 2 — 소유자 결정 반영(§12): Q1 불변 보관소, 보관소 1 GiB, Q5 넘을 때와 30일 뒤 자동 만료(§8.5). Q2·Q4와 Q3의 나머지는 권고대로.
>
> 소유자 방향(2026-10-22):
> - HIMARI의 기본 에이전트 기능을 먼저 갖춘다. 후보는 고정 프롬프트, comment, plan·note, 메모리, 이미지, 파일 업로드·다운로드다. **파일부터 하고, 이미지는 그다음에 한다.**
> - 이미지는 "첨부 파일이 모델 입력으로 들어가는 경우"이므로 이 설계의 첨부 기록 위에 얹는다(§14).
>
> 앞선 결정: Workspace 배치와 교차 접근(V3-030·V3-035), 파일 Tool(V3-P40), 대화별 모델(V3-P64, 첨부의 미디어 처리가 모델 기능에 달려 먼저 함), 대화 삭제(V3-P65, 파일 정리 Q-33은 그 영구 삭제를 쓴다). 이 문서는 [default-workspace-files-v0.2.md](default-workspace-files-v0.2.md) §4의 권고(작업 파일과 불변 보관본 분리)를 구체화한다.

## 1. 왜, 그리고 범위

지금은 사용자가 대화에 파일을 줄 수도, 모델이 만든 파일을 받을 수도 없다.
- 모델은 Workspace 안에서 파일 Tool(`inside/read_file` 등)과 `inside/run_command`로 파일을 다룬다. 하지만 그 파일이 Web·CLI 밖으로 나오는 길이 없다.
- 내용 모델에는 첨부 참조 `AttachmentRef(file_id, version)`가 이미 있다(`common.py`). 하지만 그것을 만드는 곳이 없다. OpenRouter Provider는 받으면 `UNSUPPORTED_CAPABILITY`로 거부한다(`provider/openrouter.py` `_text`).
- 계약 지도에는 FILE-02(파일 기록), FILE-04(업로드·다운로드), FILE-05(모델에게 보이는 첨부 목록)가 PROPOSED로만 있다.

| 포함 | 제외 (이후) |
| --- | --- |
| 메시지에 파일 첨부(Web 버튼·끌어 놓기, CLI) | 이미지·PDF를 모델이 직접 보기(이미지 묶음, §14) |
| 첨부한 파일을 대화 폴더에 놓아 모델이 Tool로 읽게 함 | 대화 폴더 파일 탐색기(Q2 (b)를 고르면 포함) |
| 모델이 Workspace 파일을 사용자에게 건넴(`inside/present_file`) | 사용자 사이 파일 공유, 공개 링크 |
| 첨부·건넨 파일의 내려받기(Web·CLI) | 브라우저 안 미리보기(이미지 묶음에서 이미지부터) |
| 파일 기록과 불변 보관본, 대화 영구 삭제 때 정리 | 원격 저장소(S3 등), 암호화 |
| `ask_user`의 답에도 첨부(Q-38 일부) | MCP 리소스를 골라 첨부하기([mcp-context-v0.1.md](mcp-context-v0.1.md)의 이후 항목) |

## 2. 다른 호스트들

코드 실행 환경이 있는 대화형 호스트는 대체로 같은 모양이다.
- **올리기:** 사용자가 메시지에 파일을 붙이면 호스트가 실행 환경의 정해진 폴더에 복사하고, 모델에게는 파일 이름과 경로를 알린다. 모델은 코드나 도구로 그 파일을 읽는다. 모델이 볼 수 있는 이미지·PDF는 따로 모델 입력에도 넣는다.
- **내려받기:** 모델이 만든 파일은 모델이 명시적으로 내보낸 것(전용 도구, 또는 실행 환경 경로의 링크)만 대화에 다운로드 카드로 보인다. 일부 호스트는 실행 환경 폴더를 보는 파일 패널도 둔다.
- 올린 파일은 대화에 붙어 있고, 대화를 지우면 같이 지워진다.

API 수준에서도 같다(OpenAI Files, Anthropic Files): 파일을 먼저 올려 id를 받고, 메시지는 그 id를 참조한다. 이 설계는 이 두 단계(올리기 → 참조)와 "모델이 내보낸 것만 카드로"를 따른다.

## 3. 작업 파일과 보관본

```text
 사용자가 올림                                     모델이 건넴 (inside/present_file)
      │                                                     ▲
      ▼                                                     │ 그 순간의 바이트를 복사
 ┌────────────────────────────┐   메시지 보낼 때 복사   ┌──────────────────────────────┐
 │ 보관본 (불변, 내용 주소)     │ ─────────────────────▶ │ 작업 파일 (Workspace)          │
 │ <data_root>/files/…        │                        │ conversations/<id>/uploads/… │
 │ Sandbox·모델이 못 봄         │ ◀───────────────────── │ 모델·명령이 고치고 지울 수 있음  │
 └────────────────────────────┘                        └──────────────────────────────┘
      │  + Data 파일 기록 (files)
      ▼
 Web·CLI 내려받기, 메시지·Tool 결과의 첨부 표시, 모델에게 보이는 첨부 목록
```

- **작업 파일**은 Workspace에 있고 원본은 파일시스템이다(FILE-01). 모델과 명령이 마음대로 바꾼다.
- **보관본**은 한 시점의 바이트다. 한 번 쓰면 바뀌지 않는다. 올린 원본과 모델이 건넨 파일이 여기에 남는다.
- 둘을 나누는 이유:
  - 대화 기록은 "이 메시지에 이 파일이 붙어 있었다"를 그대로 보여야 한다. 모델이 작업 파일을 고쳐도 첨부는 바뀌지 않아야 한다.
  - 내려받기가 쓰는 도중의 파일을 주지 않는다(v0.2 수용 기준 7).
  - **이미지 묶음의 전제다.** 이미지를 모델 입력에 넣으면 매 요청마다 같은 바이트를 다시 보낸다. 기록의 재생과 프롬프트 캐시는 같은 입력을 전제로 한다(V3-P62 B2). 작업 파일을 다시 읽으면 그 사이 바뀔 수 있다.
- 비용은 저장 공간이다. 올린 파일은 보관본과 작업 파일, 두 벌이 된다. 한 벌로 줄이는 방법은 이 배포에서 쓸 수 없다. 하드링크는 Sandbox의 명령이 보관본의 inode를 고칠 수 있고 파일 Tool이 거부하며(`HARDLINK`), copy-on-write 복사(reflink)는 이 서버의 ext4가 지원하지 않는다.
- 그래서 보관소는 작게 두고 오래된 것부터 만료시킨다(§8.5, 소유자 결정). 작업 파일이 Workspace에 남아 있으므로 보관본이 만료돼도 모델의 작업은 이어진다. 잃는 것은 "그때의 원본을 내려받기"뿐이다.

## 4. Data

### 4.1 파일 기록 (FILE-02)

```python
FileId = NewType("FileId", str)

class FileOrigin(Enum):
    UPLOAD = "upload"      # 사용자가 올림
    PRESENTED = "presented"  # 모델이 inside/present_file로 건넴

@dataclass(frozen=True)
class FileRecord(DeepFrozen):
    file_id: FileId
    version: int            # 업로드는 늘 1. 건넨 파일은 같은 경로를 다시 건네면 +1
    origin: FileOrigin
    name: str               # 보이는 이름(§5.3 규칙을 거친 것)
    media_type: str         # 표시·모델 안내용. 신뢰하지 않음(§7.2)
    size: int
    sha256: str             # 보관본의 주소
    created_at: datetime
    workspace_path: str | None  # 업로드: 놓은 곳, 건넴: 읽은 곳. "/workspace/…" 형태
    attached: bool          # 메시지·Tool 결과가 참조함(§4.2)
    expired: bool           # 보관본이 만료되어 바이트가 없음(§8.5). 기록은 남음
```

- 파일 하나(`file_id`)는 대화 하나에 속한다. 다른 대화에서 같은 `file_id`를 참조할 수 없다.
- `version`: `AttachmentRef`에 이미 있는 칸이다. 업로드는 올릴 때마다 새 `file_id`다. 모델이 같은 Workspace 경로를 다시 건네면 같은 `file_id`의 다음 버전이 된다. 화면은 "report.pdf (v2)"처럼 보인다.
- 기록은 대화 소유다. 권한 검사는 다른 포트와 같다(`authorized(scope)`, 휴지통의 대화는 없는 대화).

### 4.2 상태: 대기(STAGED) → 첨부(ATTACHED)

업로드는 메시지보다 먼저 온다(§5.1). 그래서 기록에는 두 상태가 있다.
- **대기:** 올렸지만 아직 어느 메시지도 참조하지 않음. 24시간 안에 참조되지 않으면 정리가 지운다(§8.2).
- **첨부:** 메시지(T1·T7a) 또는 Tool 결과(T7)가 참조함. 대화를 영구 삭제할 때까지 남는다.

**참조는 Data가 그 커밋 안에서 확인한다.** `append_event(UserMessage)`(T1)와 `answer_pending_call`(T7a)은 블록의 모든 `AttachmentRef`에 대해 같은 트랜잭션에서 다음을 확인한다. 하나라도 어긋나면 `INVALID_ARGUMENT`로 아무것도 쓰지 않는다.
- 같은 대화의 파일이다.
- 그 `version`이 있다.
- 업로드면 아직 대기 상태다. 업로드 하나는 메시지 하나에만 붙는다.
- 보관본이 만료되지 않았다(§8.5). 만료됐으면 다시 올려야 한다.

확인이 끝나면 같은 트랜잭션에서 첨부 상태로 바꾼다. 그래서 "메시지는 기록됐는데 파일은 대기로 남아 지워짐"이나 "다른 대화의 파일을 참조함"이 생기지 않는다.

### 4.3 포트

```python
class FileCatalog(Protocol):
    async def stage_upload(self, scope: TrustedScope, blob: StoredBlob, name: str,
                           media_type: str) -> FileRecord:
        """대기 상태 업로드 기록 하나. 대화의 대기 기록이 MAX_STAGED(50)개면 LIMIT_EXCEEDED(새 코드)."""
    async def set_placement(self, scope: TrustedScope, file_id: FileId, workspace_path: str) -> None:
        """대기 중인 업로드를 Workspace에 놓은 곳(§5.2). 첨부된 뒤에는 INVALID_STATE_TRANSITION."""
    async def read_files(self, scope: TrustedScope, file_ids: Sequence[FileId] | None = None
                         ) -> Sequence[FileRecord]:
        """None이면 그 대화의 첨부된 파일 전부(버전마다 하나), 최근 것부터."""
    async def read_file_version(self, scope: TrustedScope, file_id: FileId, version: int) -> FileRecord: ...

@dataclass(frozen=True)
class StoredBlob(DeepFrozen):
    sha256: str
    size: int

class BlobStore(Protocol):
    """보관본 바이트. 내용 주소, 쓰기는 한 번(§4.5). Data 소유."""
    async def put(self, chunks: AsyncIterator[bytes], max_bytes: int) -> StoredBlob: ...
    def open(self, blob: StoredBlob) -> BinaryIO:
        """읽기 전용. 없거나 크기가 다르면 BLOB_MISSING(새 코드)."""
```

- 건넨 파일은 따로 포트를 두지 않는다. Tool 결과와 같은 트랜잭션에서 기록한다(§7.1): `ToolCompletion.presented_files`. MCP 화면을 결과와 함께 저장하는 `app_view`(V3-P58)와 같은 방식이다.
- 모델 입력을 만드는 Prompt loader는 `read_files(scope, ids)`로 기록에 있는 첨부의 이름·경로·크기를 읽는다(§6).

### 4.4 SQLite v8

```sql
CREATE TABLE blobs (                  -- 보관본 하나에 한 행. 모든 대화가 함께 씀(ui_resources처럼)
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    last_used_at TEXT NOT NULL,        -- 마지막 put: 올리기·건네기·같은 내용 다시 저장
    expired_at TEXT                    -- NULL이 아니면 바이트를 지움(§8.5)
) STRICT;
CREATE TABLE files (
    seq INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations,
    file_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    origin TEXT NOT NULL,              -- 'upload' | 'presented'
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL REFERENCES blobs,
    created_at TEXT NOT NULL,
    workspace_path TEXT,
    attached_at TEXT,                  -- NULL: 대기
    run_id TEXT REFERENCES runs (run_id),  -- 건넨 파일: 그 호출
    call_id TEXT,
    UNIQUE (file_id, version)
) STRICT;
CREATE INDEX files_by_conversation ON files (conversation_id, seq);
CREATE INDEX files_by_digest ON files (sha256);
```

- v7 → v8 제자리 업그레이드. V3-P65의 업그레이드 전 백업이 그대로 돈다.
- 영구 삭제 트랜잭션(V3-P65 §4.3)의 삭제 목록에 `files`를 더한다. `conversation_id` 열이 있는 모든 테이블을 확인하는 기존 테스트가 빠뜨림을 잡는다.
- `file_id`는 기존 `new_id("file")`로 만든다.
- `blobs`에는 `conversation_id`가 없다. 영구 삭제는 `files` 행을 지운 뒤 참조가 없어진 `blobs`를 §8.2 규칙으로 다룬다.
- 보관소 전체 크기는 `SUM(size) WHERE expired_at IS NULL`이다.

### 4.5 보관소 배치

```text
<data_root>/files/                0700
    blobs/<sha256 앞 2자>/<sha256>  0600, 쓰기 한 번
    staging/<임의>.partial          0600, 올리는 중
```

- 데이터 루트 안이므로 Sandbox와 stdio MCP 서버는 볼 수 없다(데이터 루트는 Sandbox 자체 검사의 보호 경로, V3-P41·V3-P59).
- `put`: `staging/`에 `O_EXCL | O_NOFOLLOW`로 만들고, 받으면서 SHA-256과 크기를 잰다. 한도를 넘으면 그 자리에서 멈추고 지운다. 다 받으면 `fsync`한 뒤 **보관본 잠금** 아래에서:
  1. 같은 주소가 살아 있으면(`blobs` 행이 있고 만료 아님) 새 파일을 지운다(같은 내용).
  2. 아니면 보관소 크기를 확인한다. 넘으면 만료(§8.5)로 자리를 만들고, 그래도 넘으면 `STORE_FULL`(새 코드)로 새 파일을 지우고 끝낸다. 자리가 있으면 `blobs/<aa>/<digest>`로 덮어쓰지 않는 이름 변경(`link` 후 `unlink`, V3-P65 백업과 같음) → 부모 디렉터리 `fsync`.
  3. `blobs` 행을 쓴다: `last_used_at`=지금, `expired_at`=NULL(만료됐던 같은 내용이면 되살아남).
- **보관본 잠금**은 BlobStore의 프로세스 안 잠금 하나다. `put`의 위 1~3과 모든 보관본 삭제(§8.2·§8.5)의 "확인 → `blobs` 갱신 → 파일 지우기"가 이 잠금 아래에서 돈다. 그래서 삭제가 조건을 확인한 뒤 같은 내용의 새 `put`이 끼어드는 일이 없다(P0는 서버 프로세스 하나, V3-045).
- 같은 내용은 한 벌만 저장한다. 다른 사용자의 파일과 주소가 같아도 API로는 알 수 없다. 내려받기는 그 대화의 파일 기록을 거쳐야 하기 때문이다.
- 바이트와 `blobs` 행을 먼저 쓰고 파일 기록(`files`)을 나중에 쓴다. 그 사이 서버가 죽으면 참조 없는 보관본이 남고, 정리가 지운다(§8.2). 이름 변경과 `blobs` 행 사이에 죽으면 행 없는 파일이 남고, 이것도 정리가 지운다.
- SQLite BLOB에 넣지 않는 이유: 파일은 수십 MB까지 되고(§8.3), DB가 커지면 업그레이드 전 백업(V3-P65)과 `secure_delete`의 비용이 함께 커진다.

## 5. 올리기

### 5.1 흐름

```
 Web/CLI                      Application                        Data / Inside
   │ POST …/uploads?name=…  (바이트)
   │ ───────────────────────▶ 대화 권한 확인 ─────────────────────▶ read_conversation
   │                          BlobStore.put (한도까지 받으며 해시)
   │                          stage_upload ───────────────────────▶ files (대기)
   │ ◀── 201 {file_id, name, size, media_type}
   │
   │ POST …/messages {text, attachments: [file_id…]}
   │ ───────────────────────▶ 1 대화 권한, 파일들이 이 대화의 대기 업로드인지
   │                          2 실행 중이면 409 (Runner 상태)
   │                          3 아직 놓지 않은 파일을 Workspace에 놓음 ───▶ Inside place_upload
   │                            set_placement ─────────────────────▶ files.workspace_path
   │                          4 runner.submit(UserMessage(블록)) ──▶ T1/T7a + 첨부 확인(§4.2)
   │ ◀── 202 (지금과 같음)
```

- 업로드 하나에 요청 하나다. 화면은 파일마다 진행률을 보이고, 실패한 것만 다시 올린다.
- 첨부만 있고 글이 없는 메시지도 된다. 블록 순서는 글 → 첨부(올린 순서)다.
- 2와 4 사이에 다른 탭이 작업을 시작하면 4가 지금처럼 409(`busy`)다. 3에서 놓은 파일은 남는다. 다시 보내면 이미 놓은 파일은 다시 놓지 않는다(`workspace_path`가 있고 그 경로에 파일이 있으면 그대로 씀). 그래서 다시 시도해도 `report (2).pdf`가 생기지 않는다.
- 4의 첨부 확인이 실패하면(그 사이 정리가 지운 대기 업로드 등) 메시지는 기록되지 않고 400이다.

### 5.2 Workspace에 놓기

`InsideEnvironment.place_upload(workspace_id, cwd_relative, name, source: BinaryIO) -> str`
- 놓는 곳: 대화 폴더의 `uploads/<name>`. 모델에게는 `/workspace/conversations/<id>/uploads/<name>`로 보인다.
- Workspace 쓰기 잠금(`write_lock`) 아래에서 한다. 같은 Root의 명령·쓰기 Tool과 겹치지 않는다.
- 지금의 `fs`처럼 Root fd에서 `openat2`(`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV`)로 `uploads/`를 만들고 연다. `uploads`가 링크이거나 디렉터리가 아니면 거부한다.
- 같은 이름이 있으면 덮어쓰지 않는다. `name (2).ext`, `name (3).ext`… 중 처음 비어 있는 것을 `O_CREAT | O_EXCL`로 만든다(이름 고르기와 만들기 사이에 끼어들 틈이 없음). 100번 안에 못 찾으면 실패한다.
- 보관본에서 나눠 읽어 쓰고 `fsync`한다. 큰 파일도 메모리에 다 올리지 않는다. 워커 스레드에서 돈다(`run_blocking`).
- 놓은 파일은 모델 것이다. 모델이 고치거나 지워도 보관본과 기록은 그대로다.
- Inside Environment가 없는 배포에서는 올리기를 받지 않는다(409 `files_unavailable`). 파일을 둘 곳이 없으면 모델이 읽을 방법이 없기 때문이다.

### 5.3 이름

사용자가 보낸 이름은 신뢰하지 않는다. 기록과 Workspace에는 다음을 거친 이름만 쓴다.
- Unicode NFC로 맞춘다. `/`, `\`, NUL, 제어 문자(C0·C1·DEL)는 `_`로 바꾼다.
- 앞뒤 공백과 끝의 `.`을 지운다. `.`과 `..`, 빈 이름은 `file`로 한다.
- `.himari-`로 시작하면 앞에 `_`를 붙인다(쓰기 Tool의 임시 파일 접두사).
- UTF-8로 200바이트를 넘으면 확장자를 남기고 줄인다(문자를 자르지 않음).
- 이름은 보이는 것일 뿐 경로가 아니다. 경로는 늘 `uploads/` 아래 한 단계다.

## 6. 모델에게 보이는 것 (FILE-05)

첨부 블록(`AttachmentRef`)은 모델 입력에서 **첨부 목록 한 줄**이 된다. 내용은 넣지 않는다(Q4).

```text
<attachment>{"name": "report.pdf", "path": "/workspace/conversations/c_12/uploads/report.pdf", "media_type": "application/pdf", "bytes": 1234567}</attachment>
```

- 값은 JSON으로 이스케이프한다(`json_data`, `<runtime_state>`와 같은 규칙). 파일 이름은 사용자가 정하므로 `</attachment>` 같은 이름이 경계를 닫지 못한다.
- 고정 지시문(Bootstrap이 Prompt에 주는 `instructions`, 지금은 `DEFAULT_INSTRUCTIONS`)에 문장 하나를 더한다. Inside Environment가 있는 배포에서만이다: "Attached files are copied into the workspace at the given path when the message is sent; read them with the workspace tools. The workspace copy may have changed since." 문장은 늘 같으므로 캐시를 깨지 않는다.
- **같은 입력은 같은 글이 된다.** 목록 줄은 불변 기록(이름·놓은 곳·크기)으로만 만든다. 지금 파일이 있는지 보지 않는다. 그래서 기록을 다시 보낼 때마다 같은 바이트이고, 프롬프트 캐시가 유지된다.
- **어디서 바꾸나:** Prompt loader가 모델 입력을 만들 때 그 입력에 든 첨부의 기록을 한 번에 읽어 `ModelInput.attachments: Mapping[tuple[FileId, int], AttachmentInfo]`에 담는다. 바꾸기는 Provider가 한다. 이 묶음에서는 모든 Provider가 위 글 한 줄로 바꾼다. 이미지 묶음에서는 이미지 입력을 받는 모델에게만 이미지 블록을 더한다(§14). 그래서 Provider의 지금 거부(`attachments are not supported yet`)는 없어진다.
- 기록을 읽지 못한 첨부(이론상 없음: 첨부 상태는 영구 삭제 때까지 남음)는 `{"name": "(unavailable)"}`로 바꾸고, 모델 입력 만들기를 실패시키지 않는다.
- **`ask_user`의 답:** 답 메시지(T7a)에도 첨부할 수 있다. 답은 Tool 결과로 모델에게 가므로, Tool 결과 안에서도 같은 한 줄이 된다.
- **압축:** 요약 입력의 첨부는 지금 `[attachment <id> v<n>]`이다(`compaction.py` `_blocks_text`). 같은 한 줄로 바꿔 요약이 파일 이름과 경로를 알게 한다. 압축되지 않은 꼬리는 원래 블록 그대로다.

## 7. 모델이 건네기, 그리고 내려받기

### 7.1 `inside/present_file`

모델이 Workspace 파일을 사용자에게 건네는 Inside Tool이다(FILE-03 확장).

```json
{"name": "present_file",
 "description": "Give a workspace file to the user: they get a download card in the conversation. The file's bytes at this moment are kept; later edits need another call.",
 "properties": {"path": {"type": "string"}, "workspace_binding_id": {"type": "string"}},
 "required": ["path"]}
```

- 경로 규칙은 다른 파일 Tool과 같다(`to_relative`, 링크·마운트·하드링크 거부, 같은 사용자의 다른 대화 폴더 허용).
- 일반 파일만 된다. 크기 한도는 업로드와 같다(§8.3). 넘으면 FAILED이고, 모델에게 크기와 한도를 알린다.
- 읽기는 쓰기 잠금 없이 한다(지금의 읽기 Tool과 같음). 다만 읽기 전과 뒤의 파일 정체(`Identity`: 장치·inode·크기·mtime)가 다르면 "읽는 도중 바뀜"으로 FAILED다. 바뀌는 중의 파일을 건네지 않는다.
- 바이트는 `BlobStore.put`으로 보관본이 된다. 결과는 `ToolOutcome.presented_files`에 담긴다.

```python
@dataclass(frozen=True)
class PresentedFile(DeepFrozen):
    blob: StoredBlob
    name: str               # 경로의 마지막 부분, §5.3 규칙
    media_type: str         # 확장자로 추정(mimetypes), 모르면 application/octet-stream
    workspace_path: str     # "/workspace/…"
```

- Dispatcher는 이것을 `ToolCompletion.presented_files`로 넘기고, **Data가 Tool 결과와 같은 트랜잭션에서** 파일 기록을 만든다(첨부 상태, `run_id`·`call_id`). 버전은 그 트랜잭션에서 정한다: 같은 대화에서 같은 `workspace_path`를 건넨 적이 있으면 그 `file_id`의 다음 버전, 없으면 새 `file_id`.
- SUCCEEDED일 때만 받는다. 다른 상태에 붙어 오면 Data가 `INVALID_ARGUMENT`이고, Dispatcher는 파일 없이 결과만 기록한다(`app_view`가 거부될 때와 같음).
- 모델에게 가는 결과: `Presented report.pdf to the user (v2, 1.2 MB).`
- 효과 분류는 `READ`다. Workspace를 바꾸지 않고, 결과가 기록되기 전에 서버가 죽어 다시 돌아도 해가 없다(같은 내용이면 같은 보관본이고, 새 버전 하나가 더 생길 뿐).

### 7.2 내려받기

`GET /api/conversations/{id}/files/{file_id}/{version}`
- 대화 권한 확인 → 기록 → 보관본을 나눠 보냄(스트리밍). 다른 사용자의 대화, 없는 파일, 휴지통의 대화는 같은 404다.
- 응답 머리글은 늘 같다. 기록의 `media_type`은 쓰지 않는다:
  - `Content-Type: application/octet-stream`
  - `Content-Disposition: attachment; filename="<ASCII 대체 이름>"; filename*=UTF-8''<퍼센트 인코딩>`
  - `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox`, `Cache-Control: private, no-store`
  - 그래서 HTML·SVG를 올리거나 모델이 건네도 이 출처에서 열리지 않는다. 브라우저 안 미리보기는 이미지 묶음에서 따로 정한다.
- 만료된 파일은 410 `file_expired`다(§8.5).
- 만료 표시가 없는데 보관본이 없거나 크기가 다르면 410 `file_missing`이고 서버 로그에 남긴다(백업으로 DB를 되돌린 경우 등, §8.4).
- `GET`은 부작용이 없다. 쿠키 인증(`SameSite=Strict`)으로 같은 출처의 `<a download>`가 그대로 된다. CLI는 Bearer로 받는다.
- 대기 중인 업로드도 올린 사람은 내려받을 수 있다(같은 대화 권한). 목록에는 나오지 않는다.

### 7.3 목록

`GET /api/conversations/{id}/files` — 그 대화의 첨부된 파일, 버전마다 한 항목: `{file_id, version, origin, name, media_type, size, created_at, workspace_path, expired}`. 최근 것부터, `limit`·`offset`.

이벤트 응답에도 붙인다.
- 사용자 메시지: `attachments: [{file_id, version, name, size, media_type, expired}]`
- Tool 결과: 건넨 파일이 있으면 `presented_files: [...]`(같은 모양)

## 8. 수명주기와 한도

### 8.1 대화 삭제 (Q-33)

- **휴지통:** 다른 포트처럼 없는 파일이다. 내려받기도 404다. 아무것도 지우지 않는다.
- **영구 삭제:** V3-P65 §4.3의 트랜잭션에서 `files`의 그 대화 행을 지운다. 트랜잭션 안에서 그 행들의 `sha256` 목록을 읽어 두고, **커밋한 뒤에** 보관본을 지운다. 커밋 전에 지우면 롤백된 대화의 파일이 사라진다. 지우는 조건은 정리와 같다(§8.2의 삭제 규칙). 오래전에 쓰인 보관본은 바로 지워지고, 지난 1시간 안에 같은 내용이 다시 `put`된 보관본만 다음 정리로 미뤄진다. 이미 만료된 보관본은 `blobs` 행만 지운다.
- 커밋과 보관본 삭제 사이에 서버가 죽으면 참조 없는 보관본이 남는다. 정리가 지운다(§8.2).
- Workspace의 작업 파일(`uploads/` 포함)은 대화 폴더와 함께 지워진다(V3-P65 §5, 이미 구현됨).

### 8.2 정리

V3-P65의 자동 정리(시작 때와 1시간마다)에 셋을 더한다.
1. 24시간이 지난 대기 업로드 기록을 지운다. Workspace에 놓은 작업 파일은 지우지 않는다. 놓은 뒤 보내지 못한 경우이고, 이미 모델이 볼 수 있는 곳에 있다.
2. 참조 없는 보관본을 지운다(아래 규칙). `blobs/`에서 행이 없는 파일, `staging/`의 파일도 1시간이 지났으면 지운다.
3. 30일 만료(§8.5).

**참조 없는 보관본의 삭제 규칙**(정리와 영구 삭제가 같이 씀): 보관본 잠금(§4.5) 아래에서, 그 주소를 참조하는 `files` 행이 없고 `last_used_at`이 1시간보다 오래됐을 때만 `blobs` 행과 파일을 지운다.
- 나이 조건은 `put`은 끝났지만 아직 기록(`stage_upload`, Tool 결과)이 커밋되지 않은 보관본을 지키기 위해서다. `put`이 같은 내용을 만나면 `last_used_at`을 갱신하므로, 오래된 보관본을 다시 쓰는 경우도 지켜진다.
- 1시간 안에 기록이 커밋되지 않으면(올리기 요청이 그만큼 멈춤) 보관본이 지워질 수 있다. 그때 `stage_upload`는 `blobs` 행이 살아 있는지 같은 트랜잭션에서 확인하고, 없으면 실패한다(업로드 500, 다시 올림). Tool 결과의 `presented_files`도 같다(파일 없이 결과만 기록, §7.1).

### 8.3 한도 (Q3)

| 설정 | 기본 | 넘으면 |
| --- | --- | --- |
| `HIMARI_FILE_MAX_BYTES` 파일 하나 | 32 MiB | 업로드 413 `file_too_large`(받는 중에 끊음), `present_file`은 FAILED |
| 메시지당 첨부 수 | 10 | 400 |
| 대화당 대기 업로드 | 50 | 409 `too_many_pending` |
| `HIMARI_FILE_STORE_MAX_BYTES` 보관소 전체 | **1 GiB**(소유자) | 오래된 것부터 만료(§8.5). 그래도 자리가 없으면 업로드 507 `storage_full`, `present_file`은 FAILED |
| `HIMARI_FILE_KEEP_DAYS` 마지막 사용 뒤 보관 기간 | **30일**(소유자, 1~365) | 만료(§8.5) |

- 전체 크기는 만료되지 않은 서로 다른 보관본 크기의 합이다(`blobs`, §4.4). Workspace의 작업 파일은 세지 않는다.
- 업로드 경로는 지금의 요청 본문 한도(256 KiB, `BodyLimitMiddleware`)에서 뺀다. 대신 그 경로만 `HIMARI_FILE_MAX_BYTES`를 받으며 센다(`Content-Length`가 한도를 넘으면 읽기 전에 거부).
- Workspace 자체의 용량 한도는 이 묶음 밖이다(명령도 Workspace를 채울 수 있음, Q-40).

### 8.4 백업과 되돌리기

- 업그레이드 전 백업(V3-P65 §8)은 DB만 복사한다. 보관본은 쓰기 한 번이라 백업 시점의 기록이 가리키는 보관본은 그대로 남는다. **예외:** 업그레이드 뒤 영구 삭제나 정리가 지운 보관본은, DB를 되돌리면 기록은 있는데 바이트가 없다. 내려받기는 410이고, 모델 목록 줄은 그대로다(§6).
- 이전 릴리스(v7)로 되돌리면 `files`를 모르므로 보관소는 쓰이지 않고 남는다. 다시 v8로 올리면 정리가 참조 없는 것을 지운다.
- 운영 문서에 이 두 가지를 적는다.

### 8.5 만료 (소유자 결정 Q5)

보관소에는 최근에 쓰인 것만 남긴다. **바이트만 지우고 기록은 남긴다.**

**언제**
- **30일:** `last_used_at`이 `HIMARI_FILE_KEEP_DAYS`(30)일보다 오래된 보관본. 시작 때와 1시간마다 정리가 본다.
- **용량:** `put`이 새 보관본을 넣으면 1 GiB를 넘을 때. `last_used_at`이 오래된 것부터, 넣을 자리가 날 때까지.
- 둘 다 **지난 1시간 안에 쓰인 보관본은 만료시키지 않는다.** 방금 올렸거나 보내는 중인 파일이 사라지지 않게 하기 위해서다. 1시간 안의 것만으로 1 GiB가 차면 새 파일을 받지 않는다(`storage_full`).
- "쓰였다"는 `put`이다: 올리기, 건네기, 같은 내용을 다시 저장하기. 내려받기는 넣지 않는다. 읽을 때마다 쓰기가 생기고, 내려받는다고 보관 기간이 늘어나는 것은 이 결정의 뜻("오래된 것을 줄인다")과 맞지 않는다.

**어떻게** (보관본 잠금 아래, §4.5)
1. `blobs.expired_at`=지금으로 쓰고 커밋한다.
2. 파일을 지운다. 1과 2 사이에 서버가 죽으면 파일이 남지만 만료된 것으로 다뤄지고, 정리가 지운다(만료된 행의 파일은 늘 지움).

**만료된 뒤**
- 화면: 첨부 칩과 다운로드 카드에 "만료됨"이 보이고 누를 수 없다. CLI는 `(expired)`.
- 내려받기: 410 `file_expired`.
- 모델 입력의 첨부 한 줄(§6)은 **바꾸지 않는다.** 기록에서 만드는 글이고, 바꾸면 그 지점부터 프롬프트 캐시가 깨진다. 작업 파일이 Workspace에 있으면 모델은 계속 읽는다.
- 같은 내용을 다시 올리거나 건네면 그 보관본이 되살아난다(§4.5의 3). 그 내용을 참조하던 예전 기록들도 다시 내려받을 수 있다.
- 업로드가 대기 중에 만료되면(1시간 넘게 보내지 않았고 그 사이 보관소가 참) 메시지 보내기가 거부된다(§4.2). 화면은 그 칩을 "만료됨, 다시 올리세요"로 바꾼다.
- 이미지 묶음에서: 만료된 이미지는 모델 입력에서 이미지 블록 대신 글 한 줄이 된다. 그때 캐시가 한 번 깨진다(§14).

## 9. Web·CLI

**Web API**

| 요청 | 동작 |
| --- | --- |
| `POST /api/conversations/{id}/uploads?name=<이름>` | 본문 = 파일 바이트, `Content-Type` = 선언된 형식. 201 `{file_id, version, name, size, media_type}` |
| `POST /api/conversations/{id}/messages` | `{text, attachments?: [file_id…]}`. `text`는 첨부가 있으면 비어도 됨 |
| `GET /api/conversations/{id}/files` | 목록(§7.3) |
| `GET /api/conversations/{id}/files/{file_id}/{version}` | 내려받기(§7.2) |

- 업로드는 `POST`이므로 지금의 Origin 검사(OriginGuard)를 받는다.
- 오류는 지금의 형식이다(`{"error": {"code", "message"}}`).

**Web 화면**
- 입력란 옆 클립 버튼과 끌어 놓기로 고른다. 고르면 바로 올리고, 입력란 위에 칩(이름·크기·진행률·빼기)을 보인다. 올리는 중에는 보내기를 막는다. 빼기는 화면에서만 뺀다(대기 기록은 정리가 지움).
- 보낸 메시지 아래에 첨부 칩을 보이고, 누르면 내려받는다. 만료된 파일은 "만료됨"으로 보이고 누를 수 없다(§8.5).
- 건넨 파일은 그 Tool 호출 자리에 다운로드 카드(이름·크기·버전)로 보인다.
- 오래된 응답 규칙은 지금과 같다: 업로드가 끝났을 때 사용자가 다른 대화로 갔으면 칩을 붙이지 않는다.

**CLI**
- 대화 안에서 `/attach PATH…`: 올리고 다음 메시지에 붙인다. `/attach`만 치면 붙일 목록을 보이고, `/detach`는 비운다.
- `himari send ID TEXT --attach PATH`(여러 번 가능).
- `himari files ID`: 목록. `himari download ID FILE_ID [--version N] [-o PATH]`: 내려받기. 기본 이름은 기록의 이름이고, 같은 이름의 파일이 있으면 덮어쓰지 않고 실패한다(`--force`로 덮어씀).
- 이벤트 출력에 첨부와 건넨 파일을 `[file] report.pdf (1.2 MB) — himari download c_12 file_40`처럼 보인다.

## 10. 계약 변경 (Proposed)

| 모듈 | 변경 |
| --- | --- |
| common | `FileId`. `AttachmentRef`는 그대로 |
| Data | `DataErrorCode.LIMIT_EXCEEDED`·`BLOB_MISSING`·`STORE_FULL`, `FileRecord`(`expired` 포함)·`FileOrigin`·`StoredBlob`, `FileCatalog`(stage_upload·set_placement·read_files·read_file_version), `BlobStore`. T1·T7a의 첨부 확인과 상태 변경(§4.2). `ToolCompletion.presented_files`(T7에서 기록·버전). SQLite v8 `blobs`·`files`. 영구 삭제 목록에 `files`, 커밋 뒤 보관본 삭제. 정리(대기 기록, 참조 없는 보관본), 만료(30일, 용량; §8.5) |
| Tool | `ToolOutcome.presented_files`, Dispatcher가 `ToolCompletion`으로 넘김 |
| Environment (Inside) | `place_upload`, `inside/present_file`(BlobStore를 Bootstrap이 넣어 줌) |
| Prompt | loader가 첨부 기록을 읽어 `ModelInput.attachments`. 압축 입력의 첨부 줄 |
| Provider | `AttachmentRef`를 첨부 목록 한 줄로(§6). 거부 없앰 |
| Bootstrap | `HIMARI_FILE_MAX_BYTES`·`HIMARI_FILE_STORE_MAX_BYTES`·`HIMARI_FILE_KEEP_DAYS`, `<data_root>/files` 배치 검사, 첨부 안내 고정 문장(§6), `Application.upload`·`send(text, attachments)`·`download`, 정리 작업 확장 |
| Web·CLI | §9 |
| Control | 없음(`UserMessage`의 블록이 늘 뿐) |

## 11. 하지 않는 것과 이유

- **메시지와 파일을 한 요청(multipart)으로 받지 않는다.** 파일마다 진행률과 재시도를 주기 어렵고, 메시지 경로가 큰 본문을 받아야 한다. 다른 호스트·API도 올리기 → 참조의 두 단계다(§2).
- **Workspace 파일을 경로로 바로 내려받지 않는다(Q2 (a)일 때).** 모델이 건넨 것만 대화에 남는다. 건넬 때의 바이트가 기록되므로 나중에 같은 카드를 눌러도 같은 파일이다.
- **업로드를 처음부터 Workspace에 두지 않는다.** 보내지 않은 파일이 모델이 보는 곳에 생기지 않게, 보낼 때 놓는다(§5.1).
- **올린 파일의 형식을 바꾸지 않는다(PDF → 글 등).** 모델이 Workspace 도구로 한다. Sandbox에 있는 도구에 달려 있다.

## 12. 소유자 결정

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 올린 원본과 건넨 파일을 어디에 남기나 | (a) Workspace 밖 불변 보관소에 한 벌 더(§3·§4.5) (b) Workspace 파일만 두고 기록에는 이름·해시만 | **(a)** — 첨부가 기록 그대로 남고, 내려받기가 쓰는 중의 파일을 주지 않는다. 다음 이미지 묶음은 같은 바이트를 매번 다시 보내야 하므로 어차피 필요하다. 비용은 올린 파일의 공간 두 배 |
| Q2 | 모델이 만든 파일을 사용자가 받는 길 | (a) 모델이 `present_file`로 건넨 것만, 대화에 카드로 (b) (a) + 대화 폴더 파일 탐색기(그 순간의 내용을 내려받음) (c) 탐색기만 | **(a)** — 다른 호스트의 기본 모양이고, 대화 기록에 무엇을 언제 받았는지 남는다. 탐색기(b)는 쓸모가 크지만 별도 화면이라 다음 묶음으로 나누는 것을 권한다 |
| Q3 | 한도 | (a) 파일 32 MiB, 메시지당 10개, 보관소 10 GiB(운영자가 바꿈) (b) 파일 100 MiB, 보관소 50 GiB | **(a)** — 이 서버의 디스크와 지금 쓰임새(문서·코드·데이터 파일)에 맞춘다. 운영자 설정이라 나중에 늘릴 수 있다 |
| Q5 | 오래된 보관본 (rev. 2에서 더함) | (a) 용량을 넘을 때 오래된 것부터 만료 (b) (a) + 마지막 사용 뒤 N일이 지나면 만료 | (a) — 소유자가 (b)를 골랐다 |
| Q4 | 올린 텍스트 파일의 내용을 메시지에 넣나 | (a) 넣지 않음: 이름과 경로만, 모델이 Tool로 읽음 (b) 작은 텍스트 파일(64 KiB 이하, UTF-8)은 내용까지 넣음 | **(a)** — 모든 파일이 같은 규칙이고, 모델 입력 크기가 첨부로 갑자기 커지지 않는다. Tool 호출 한 번이 더 들 뿐이다. 이미지 묶음에서 "모델 입력에 넣는 첨부"를 정할 때 다시 본다 |

**소유자 결정(2026-10-22):**
- **Q1 (a):** 불변 보관소를 둔다. 두 벌을 두는 이유(§3)와 한 벌로 줄일 수 없는 이유(하드링크·reflink)를 설명한 뒤 결정.
- **Q3:** 보관소 전체는 **1 GiB**(권고 10 GiB에서 줄임). Workspace에 작업 파일이 있으니 보관소는 더 작게 둔다는 뜻이다. 파일당 32 MiB, 메시지당 10개는 권고대로.
- **Q5 (b), 기간 30일:** 1 GiB를 넘을 때 오래된 것부터, 그리고 마지막 사용 뒤 30일이 지나면 자동 만료(§8.5). 바이트만 지우고 기록은 남긴다.
- **Q2 (a):** 모델이 `present_file`로 건넨 것만 대화에 카드로. 파일 탐색기는 다음 묶음. **Q4 (a):** 첨부 내용을 메시지에 넣지 않고 모델이 Tool로 읽음. 둘 다 권고대로("나머지는 권고대로").

**이미 정해진 것(2026-10-22, 소유자):** 파일 먼저, 이미지는 다음 묶음.

## 13. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 다른 사용자의 파일을 내려받음 | 내려받기는 대화 권한을 거친다. 다른 사용자의 대화, 없는 파일, 휴지통의 대화는 같은 404(§7.2) |
| 다른 대화의 업로드를 내 메시지에 붙임 | T1·T7a가 같은 트랜잭션에서 "같은 대화의 대기 업로드"를 확인(§4.2) |
| 올린 HTML·SVG가 이 출처에서 열려 스크립트 실행 | 늘 `application/octet-stream` + `attachment` + `nosniff` + `CSP: sandbox`(§7.2) |
| 파일 이름으로 경로를 벗어나거나(`../`), 쓰기 Tool의 임시 파일과 겹침 | 이름 규칙(§5.3), 늘 `uploads/` 한 단계, `RESOLVE_BENEATH`·`NO_SYMLINKS`(§5.2) |
| 모델이 `uploads`를 링크로 바꿔 두어 다음 업로드가 Root 밖에 쓰임 | `openat2`가 링크를 따라가지 않고 거부(§5.2) |
| 파일 이름으로 모델 지시를 주입(`</attachment>…`) | 목록 줄은 JSON 이스케이프한 데이터, 고정 문장으로 데이터임을 알림(§6) |
| 큰 업로드로 메모리·디스크를 채움 | 받으면서 세고 한도에서 끊음, `Content-Length` 미리 거부, 보관소 전체 한도, 대기 기록 수 한도(§8.3) |
| 업로드와 메시지 사이에 정리가 대기 기록을 지움 | 24시간 뒤에만 지움. 그래도 겹치면 T1이 확인에 실패해 메시지가 기록되지 않음(§5.1) |
| 보관본을 쓰고 기록하기 전에 정리나 영구 삭제가 지움 | 참조 없고 마지막 `put` 뒤 1시간이 지난 보관본만, 보관본 잠금 아래에서 지움. 같은 내용을 다시 쓰면 시각 갱신(§4.5·§8.2) |
| 영구 삭제가 롤백됐는데 보관본이 이미 지워짐 | 커밋 뒤에만 지움(§8.1) |
| 같은 내용의 다른 대화 파일까지 지움 | 참조가 없는 주소만 지움, 단일 writer 잠금 아래(§8.1·§8.2) |
| 모델이 쓰는 중인 파일을 건넴 | 읽기 전후 정체 비교, 다르면 FAILED(§7.1) |
| 모델이 고친 작업 파일 때문에 첨부 내용이 바뀜 | 첨부와 내려받기는 보관본(§3) |
| Sandbox의 명령이 보관본을 고침 | 보관소는 데이터 루트 안: Sandbox에서 보이지 않음(§4.5) |
| 업로드 요청의 CSRF | `POST`는 OriginGuard, 쿠키는 `SameSite=Strict` |
| 다시 보내기로 같은 파일이 여러 번 놓임 | 놓은 곳을 기록하고 다시 쓰지 않음(§5.1) |
| 보내는 중인 첨부가 만료로 사라짐 | 1시간 안에 쓰인 보관본은 만료시키지 않음. 대기 중 만료된 업로드는 T1이 거부(§4.2·§8.5) |
| 만료 때문에 기록과 바이트가 어긋남 | `blobs.expired_at`을 먼저 커밋하고 파일을 지움. 행이 만료면 파일이 남아도 만료로 다룸(§8.5) |
| 만료로 모델 입력이 바뀌어 캐시가 깨짐 | 첨부 한 줄은 기록에서 만들고 만료와 무관(§6·§8.5) |

## 14. 이미지 묶음으로 이어지는 것

이 설계가 남겨 두는 자리:
- `ModelInput.attachments`와 Provider의 바꾸기 자리(§6): 이미지를 받는 모델이면 이미지 블록을 더한다. 모델의 입력 형식(이미지 가능 여부)은 V3-P64 모델 목록에 칸을 더한다.
- 보관본은 바뀌지 않으므로, 기록을 다시 보낼 때 같은 이미지 바이트다(§3).
- 이미지 묶음에서 정할 것: 만료된 이미지를 모델 입력에서 어떻게 보일지(§8.5), 이미지 크기 줄이기와 토큰 추정, 압축 때 이미지 처리, 브라우저 안 미리보기(별도 출처나 `img`만 허용하는 응답), MCP Tool 결과의 이미지(지금은 모델에게 전달하지 않음, `mcp/prompts.py`).

## 15. 검증 계획

- **Data (인메모리·SQLite 양쪽):**
  - 대기 → 첨부: T1·T7a가 같은 대화의 대기 업로드만 받고, 다른 대화의 파일·없는 버전·이미 첨부된 업로드면 `INVALID_ARGUMENT`이며 아무것도 쓰지 않음.
  - `presented_files`: SUCCEEDED에서만 기록. 같은 경로를 다시 건네면 같은 `file_id`의 다음 버전.
  - 휴지통의 대화: 모든 파일 포트가 NOT_AUTHORIZED.
  - 영구 삭제 뒤 `files`에 그 대화 행이 없음(기존 전체 테이블 검사). 다른 대화가 같은 주소를 참조하면 보관본이 남음. 롤백을 주입하면 보관본도 남음.
  - v7 → v8 업그레이드와 백업.
- **BlobStore:** 한도를 넘는 스트림은 `staging`에 남지 않음, 같은 내용 두 번이면 한 벌, 파일·디렉터리 `fsync` 호출, 0600, 크기가 다른 보관본은 `BLOB_MISSING`.
- **정리:** 24시간 지난 대기 기록, 1시간 지난 참조 없는 보관본만 지움. 막 쓴 보관본과 참조된 보관본은 남음. 오래된 보관본과 같은 내용을 `put`한 직후(기록 전)에 정리·영구 삭제가 돌아도 그 보관본은 남음.
- **만료:** 30일 지난 것만 만료, 1 GiB를 넘기는 `put`은 오래된 것부터 만료시켜 자리를 만듦, 1시간 안의 것은 남음(그것만으로 차면 `STORE_FULL`). 만료 뒤 기록은 남고 내려받기 410 `file_expired`, 모델 요청 바이트는 그대로. 같은 내용을 다시 올리면 되살아나고 예전 기록도 다시 내려받힘. `expired_at` 커밋 뒤 파일 삭제 전에 실패를 주입하면 다음 정리가 파일을 지움. 대기 중 만료된 업로드로 보내면 거부.
- **Inside:**
  - `place_upload`: 이름 충돌(`name (2).ext`), `uploads`가 링크·파일이면 거부, 큰 파일 스트리밍.
  - `present_file`: 링크·하드링크·Root 밖 거부, 다른 대화 폴더 허용, 한도 초과 FAILED, 읽는 도중 바뀌면 FAILED.
- **이름 규칙:** `../x`, NUL, 제어 문자, `.himari-` 접두사, 긴 이름(문자를 자르지 않음), 빈 이름.
- **Prompt·Provider:** 첨부가 JSON 한 줄이 되고, `</attachment>`가 든 이름이 경계를 닫지 못함. 같은 입력이면 같은 요청 바이트(작업 파일을 지워도). `ask_user` 답의 첨부가 Tool 결과 안에 들어감. 압축 입력에 이름과 경로.
- **Web·CLI:** 업로드 한도(받는 중에 413, `Content-Length` 미리), 메시지당 10개, 다른 사용자 404, 휴지통 404, 내려받기 머리글(HTML을 올려도 `octet-stream`·`attachment`·`CSP: sandbox`), 다시 보내기에서 놓기가 한 번만, CLI `download`가 덮어쓰지 않음.
- **브라우저 스모크:** 파일 두 개 첨부(진행률, 빼기) → 보냄 → 모델이 `read_file`로 읽음(시험용 Provider) → 메시지 칩으로 내려받기가 원본과 같음 → 모델이 `present_file` → 카드로 내려받기 → 다른 대화로 간 뒤 늦게 끝난 업로드는 칩을 붙이지 않음 → 좁은 화면(390px).
