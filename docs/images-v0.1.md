# 이미지를 모델 입력으로 v0.1

> 2026-10-22 · **PROPOSED (V3-P67)** · rev. 2 — 소유자 결정 반영(§12): Q1·Q3·Q4·Q5 권고대로, Q2는 (b) 줄임(§3.1).
>
> 소유자 방향(2026-10-22): 기본 에이전트 기능을 먼저 갖춘다. 파일(V3-P66) 다음에 이미지를 한다. 이미지는 "첨부 파일이 모델 입력으로 들어가는 경우"이므로 V3-P66의 파일 기록 위에 얹는다([file-upload-download-v0.1.md](file-upload-download-v0.1.md) §14).
>
> 앞선 결정: 파일 보관소와 첨부(V3-P66: 불변 보관본, 첨부 한 줄, 만료), 대화별 모델(V3-P64), wire-exact 원문 보존(V3-048, Confirmed — 이 설계가 개정을 묻는다, Q1), 프롬프트 캐시(V3-P62 B2).

## 1. 왜, 그리고 범위

지금 모델은 이미지를 볼 수 없다.
- 사용자가 이미지를 첨부하면 파일로 대화 폴더에 놓이고, 모델에게는 이름·경로 한 줄만 간다(V3-P66 §6).
- MCP Tool이 돌려준 이미지는 `[image content omitted (image/png)]`로 바뀐다(`mcp/tools.py` `render_result`).
- 모델이 Workspace에서 만든 그림(그래프 등)을 스스로 확인할 길이 없다. `inside/read_file`은 이진 파일이면 "내용을 보이지 않음"이다.

선언한 모델 중 이미지 입력을 받는 것(OpenRouter 모델 API `architecture.input_modalities`, 2026-10-22 확인): `openai/gpt-6-luna`, `openai/gpt-6.1-sol`, `anthropic/claude-sonnet-5.5`, `anthropic/claude-haiku-5.5`, `openai/gpt-5-nano`, `xiaomi/mimo-v2.6-pro`, `deepseek/deepseek-v4.1-flash`, `minimax/minimax-m3`. 받지 않는 것: `z-ai/glm-5.3`.

| 포함 | 제외 (이후) |
| --- | --- |
| 사용자가 첨부한 이미지를 모델이 봄 | 오디오·비디오·PDF 페이지를 모델 입력으로(PDF는 지금처럼 명령으로) |
| MCP Tool 결과의 이미지를 모델이 봄 | |
| 모델용 사본: 긴 변 1,568픽셀로 줄이고 메타데이터를 지움(Q2) | |
| 모델이 Workspace 이미지를 `read_file`로 봄(Q3) | 이미지 생성 모델(출력이 이미지) |
| 이미지를 받지 않는 모델에게는 지금의 한 줄 | MCP 리소스 이미지(`resource`의 blob) |
| 크기 추정에 이미지 토큰을 셈 | |
| Web에서 이미지 첨부와 Tool 이미지의 미리보기(Q4) | |
| 요청 원문 저장에서 이미지를 보관본 참조로(Q1) | |

## 2. 다른 호스트와 표준

- **OpenAI Chat 형식(OpenRouter가 쓰는 형식):** 사용자 메시지의 `content`를 배열로 하고 `{"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}` 부분을 넣는다. Tool 메시지(`role: "tool"`)의 내용은 글이다.
- **Anthropic:** 사용자 메시지와 `tool_result` 안에 `image` 블록(base64)을 넣을 수 있다. OpenRouter는 OpenAI 형식을 받아 바꾼다.
- **MCP:** Tool 결과의 `content`에 `{"type": "image", "data": <base64>, "mimeType": …}`(ImageContent)가 있다. 호스트가 모델에게 전달하는 것이 사양의 의도다.
- **코드 에이전트(Claude Code 등):** 파일 읽기 Tool이 이미지 파일이면 이미지를 돌려준다. 사용자가 붙인 이미지는 메시지에 들어간다.
- 받는 형식은 사실상 PNG·JPEG·GIF·WebP 네 가지다(OpenAI·Anthropic 공통). 이미지 하나 크기 상한은 공급자마다 다르다(Anthropic 5 MB, OpenAI 20 MB). 큰 이미지는 공급자가 줄여서 센다.

## 3. 무엇이 이미지인가

**형식은 바이트로 정한다.** 선언된 `media_type`(사용자 업로드는 브라우저가, 건넨 파일은 확장자로 정함)을 믿지 않는다. 보관본 앞부분의 매직 바이트로 판정한다.

| 형식 | 시작 바이트 |
| --- | --- |
| PNG | `89 50 4E 47 0D 0A 1A 0A` |
| JPEG | `FF D8 FF` |
| GIF | `GIF87a`, `GIF89a` |
| WebP | `RIFF` …(4바이트)… `WEBP` |

- 그 밖(SVG, HEIC, BMP, TIFF 등)은 이미지가 아니다. 지금처럼 파일 한 줄이다. SVG는 글이고 스크립트를 담을 수 있으므로 특히 이미지로 다루지 않는다.
- **크기와 픽셀:** 헤더에서 가로·세로를 읽는다(순수 Python, 각 형식의 첫 수십~수천 바이트). 읽지 못하면 이미지가 아니다.
- **모델에게 보일 수 있는 이미지:** 위 네 형식이고, 픽셀 수(가로 × 세로)가 5,000만 이하. 넘으면 디코드하지 않고 한 줄로 가며 이유를 붙인다(`"not_shown": "too large to show"`). 그 밖의 이미지는 모델용 사본을 만들어 보낸다(§3.1).
- 판정 결과(형식, 가로, 세로)와 모델용 사본은 파일 기록에 저장한다(§4). 매 요청마다 다시 만들지 않는다.

### 3.1 모델용 사본 (소유자 결정 Q2 (b))

모델에게는 원본이 아니라 **한 번 만든 사본**이 간다. 사용자가 내려받는 것은 늘 원본이다.

- **왜:** 휴대폰 사진(4032×3024 JPEG, 3~6 MB)과 4K 화면 캡처는 공급자 한도(Anthropic 5 MB)를 자주 넘는다. 또 이미지는 대화가 이어지는 동안 매 요청에 다시 실린다. 원본 4 MB면 요청마다 base64로 약 5.3 MB다. 사진의 EXIF(위치 정보 등)도 공급자에게 그대로 간다.
- **어떻게:**
  - 사진의 회전 정보(EXIF orientation)를 적용한다.
  - 긴 변이 1,568픽셀을 넘으면 비율대로 줄인다. 넘지 않으면 크기는 그대로 둔다.
  - 메타데이터 없이 다시 인코딩한다. 투명도나 팔레트가 있으면(GIF 포함) PNG로, 아니면 JPEG(품질 85)로 저장한다. 움직이는 GIF·WebP는 첫 프레임만 쓴다.
  - 사본은 보관소에 따로 둔다(보관본 하나, `files`의 `view_blob_key`). 크기 추정과 Provider는 사본의 형식·가로·세로를 쓴다.
- **언제:** 파일 기록을 만들 때 한 번 만든다. 사용자 업로드(`Application.upload`가 `stage_upload` 전에)와 Tool 이미지(Dispatcher가 대기 기록 전에, §4.2)다. 건넨 파일은 모델 입력이 아니므로 만들지 않는다. 한 번 만든 사본이 늘 쓰이므로, 다시 보낼 때 같은 바이트이고 캐시가 유지된다.
- **어디서(보안):** 이미지 디코드는 신뢰할 수 없는 입력을 다루는 공격 면이다(압축 폭탄, 코덱 취약점). 그래서 다음을 지킨다.
  - 헤더로 픽셀 수를 먼저 확인해 5,000만을 넘으면 디코드하지 않는다(§3).
  - 디코드와 인코딩은 **서버 프로세스 밖의 자식 프로세스**에서 한다. 입력은 표준 입력, 출력은 표준 출력이다. 한도는 메모리(`RLIMIT_AS`) 512 MiB, CPU 10초, 출력 8 MiB, 벽시계 30초다. 넘으면 죽이고 실패로 다룬다.
  - 서버의 Sandbox(bubblewrap, V3-P41)가 ENFORCED면 그 안에서 띄운다(네트워크 없음, 파일시스템은 읽기 전용 실행 환경만). 아니면 위 한도만으로 띄운다. Pillow의 `MAX_IMAGE_PIXELS`도 같은 값으로 둔다.
- **실패:** 디코드할 수 없거나 한도를 넘으면 사본이 없다. 그 파일은 이미지 정보 없이 지금의 한 줄로 간다(`"not_shown": "could not be prepared"`). 업로드나 Tool 결과 자체는 실패로 만들지 않는다.
- **의존성:** Pillow를 기본 의존성에 더한다(`pyproject.toml`). 판정(§3)은 Pillow 없이 순수 Python으로 한다. 그래서 Pillow를 쓰는 곳은 자식 프로세스뿐이다.

## 4. Data

### 4.1 파일 기록에 이미지 정보

```python
@dataclass(frozen=True)
class ImageInfo(DeepFrozen):
    media_type: str   # 원본: image/png | image/jpeg | image/gif | image/webp (바이트로 판정)
    width: int
    height: int
    view: StoredBlob           # 모델용 사본(§3.1)
    view_media_type: str       # image/jpeg | image/png
    view_width: int
    view_height: int

FileRecord.image: ImageInfo | None   # 판정을 통과하고 사본을 만든 파일만
```

- 판정과 사본은 기록을 만들 때 한 번 한다: 업로드(`stage_upload`에 넘김)와 Tool 이미지(§4.2). 건넨 파일은 하지 않는다. 판정은 Data의 순수 함수(`data/api.py` `sniff_image(head: bytes)`)다. 사본은 §3.1의 자식 프로세스가 만들고, 부르는 쪽(Application, Dispatcher)이 보관소에 넣는다.
- SQLite v9: `files`에 `image_type`, `image_width`, `image_height`, `view_blob_key`(`REFERENCES blobs`), `view_type`, `view_width`, `view_height`(모두 NULL 가능). v8 → v9 업그레이드 때 이미 있는 파일은 판정하지 않고 NULL로 둔다. 그래서 그 파일들은 이미지로 보이지 않는다. 지금 배포 전이라 이미 있는 파일이 거의 없다.
- **사본도 보관본이다.** 참조를 세는 모든 곳은 `blob_key`와 `view_blob_key`를 함께 본다: 참조 없는 보관본 지우기, 대기 업로드의 만료 제외, 영구 삭제 뒤 해제(V3-P66 §8). 사본이 만료되면 모델에게는 그 첨부가 `"expired"` 줄로 간다. 원본만 남아 있어도 사본을 다시 만들지 않는다(같은 바이트 원칙).

### 4.2 Tool이 돌려준 이미지

Tool 결과의 이미지도 보관소에 남는 파일이다(`FileOrigin.TOOL`). 기록 흐름은 업로드와 같다: 대기 → 커밋이 참조하며 첨부.

1. Tool은 `ToolOutcome.result_images: tuple[ResultImage, ...]`로 바이트를 넘긴다(`ResultImage(data: bytes, media_type: str)`, `tool/api.py`). MCP 커넥터는 `tool.api`만 쓰므로 Data를 몰라도 된다.
2. Dispatcher가 결과를 기록하기 전에 각 이미지를 처리한다.
   - 바이트로 판정해(§3) 이미지가 아니면 글 한 줄 `[an image of an unsupported type was returned]`로 바꾼다.
   - 이미지면 보관소에 넣는다(`BlobStore.put`). 그다음 Data `stage_tool_file(scope, call, blob, name, image)`로 대기 기록을 만든다. 이름은 `image-<n>.<확장자>`다.
   - 받은 `file_id`로 `AttachmentRef(file_id, 1)`을 `model_result` 블록 끝에 붙인다(Tool의 글 다음, 받은 순서대로).
   - 보관소가 가득 차는 등 저장에 실패하면 그 자리에 글 한 줄 `[an image was returned but could not be kept]`을 붙인다. 결과 자체는 실패로 만들지 않는다.
3. T7(`commit_tool_outcome`)이 `model_result`의 `AttachmentRef`를 같은 트랜잭션에서 확인하고 첨부 상태로 바꾼다. 조건은 같은 대화, `origin=tool`, 대기 상태, 그 호출(`call`)의 것이다. 업로드의 T1 확인(V3-P66 §4.2)과 같은 자리이고, 어긋나면 `INVALID_ARGUMENT`다.
4. 대기 상태로 남은 Tool 이미지는 업로드처럼 24시간 뒤 정리된다. 기록 전에 서버가 죽은 경우다.

- 한도: 호출 하나에 이미지 8개, 이미지 하나 `HIMARI_FILE_MAX_BYTES`(32 MiB)까지 보관한다. 넘는 것은 글 한 줄로 바꾼다. 모델에게 보일지는 §3의 픽셀 규칙과 사본(§3.1)이 따로 정한다.
- 건네기 한도(1시간 256 MiB, V3-P66 §7.1)와는 별개다. Tool 이미지는 사용자에게 건넨 파일이 아니다. 다만 같은 보관소 용량을 쓴다.

## 5. 이미지를 내는 곳

| 곳 | 바뀌는 것 |
| --- | --- |
| 사용자 첨부 | 없음: 이미 `AttachmentRef`. 기록에 `image`가 붙을 뿐 |
| `ask_user`의 답에 붙인 이미지 | 같음. 답은 Tool 결과로 가므로 §7.2의 Tool 결과 자리에 놓인다 |
| MCP Tool 결과 | `render_result`가 `image` 항목을 글에서 빼고 `result_images`로 넘긴다(base64 풀기, 실패하면 지금의 한 줄). 오디오 등은 지금처럼 한 줄 |
| `inside/read_file` (Q3) | 파일이 §3의 이미지이면 이미지를 돌려준다(`result_images` 하나 + 글 `"<path> (<bytes> bytes): image/png 800×600"`). `offset`은 이미지에 쓰지 않는다. 이미지를 볼 수 없는 모델이면 지금처럼 글만 |
| `inside/present_file` | 없음. 사용자에게 건넨 파일은 모델 입력이 아니다 |

`read_file`이 이미지를 돌려줄지는 모델의 기능에 달려 있다. 그런데 Tool은 모델을 모른다. 그래서 Tool은 늘 이미지를 넘기고, 이미지를 받지 않는 모델에게는 Provider가 한 줄로 바꾼다(§7.3).

## 6. Prompt

### 6.1 첨부 정보

```python
AttachmentInfo.image: ImageInfo | None   # 기록의 판정(§4.1)
AttachmentInfo.blob_key: str            # 보관본 주소: Provider가 바이트를 넣을 때 쓴다(§7.4)
AttachmentInfo.expired: bool
```

- loader는 지금처럼 기록을 한 번에 읽는다(`read_file_records`). 바이트는 읽지 않는다. `ModelInput`이 가볍게 남는다.

### 6.2 크기 추정

이미지 하나의 토큰을 가로·세로로 센다. 공급자마다 다르므로 두 공식 중 큰 값을 쓴다(보수적).
- Anthropic 방식: 긴 변이 1,568을 넘으면 비율대로 줄인 뒤 `ceil(w × h / 750)`.
- OpenAI 방식(high detail): 2,048×2,048 안으로 줄이고, 짧은 변을 768로 줄인 뒤 `85 + 170 × (512 타일 수)`.
- 가로·세로는 사본의 값이다(이미 1,568픽셀 이하).
- 이미지로 보내지 않는 첨부(모델이 못 봄, 너무 큼, 사본 없음, 만료)는 지금처럼 그 한 줄의 글로 센다.
- 이 셈은 `prompt/api.py`의 `image_tokens(info)` 하나로 하고, Provider가 이미지로 보내는지와 같은 판단(`shown_as_image(info, capabilities)`)을 따른다. "세는 것이 보내는 것"(V3-P66 C1)을 지킨다.

### 6.3 압축

압축 요약 요청에는 이미지를 넣지 않는다. 요약 입력의 첨부는 지금처럼 한 줄(이름·경로)이다. 그래서 요약은 이미지가 있었다는 것과 그 경로를 안다. 모델이 다시 보려면 `read_file`로 연다(Q3 (a)일 때).

## 7. Provider

### 7.1 모델 기능

`ProviderCapabilities.image_input: bool`. OpenRouter의 선언 목록(`provider/openrouter.py`)에 모델마다 둔다. **실제 요청으로 확인한 모델에만 켠다**(§13의 실시간 확인). 목록에 없는 모델은 꺼진다(지금의 "모르면 가정하지 않음" 규칙). 이 값은 대화의 모델에 따라 정해진다. 모델을 바꾸면 다음 턴부터 그 모델의 값이다(V3-P64). `context_key`에 이미 모델이 들어 있으므로 크기 측정값이 섞이지 않는다.

### 7.2 이미지의 자리

- **사용자 메시지:** `content`가 배열이 된다. 글 → 첨부 순서이고, 이미지 첨부는 그 줄(이름·경로) 바로 다음에 `image_url` 부분이 온다. 모델이 어느 파일의 이미지인지 안다.
- **Tool 결과:** OpenAI 형식의 Tool 메시지는 글만 받는다. 그래서 한 Tool 그룹의 결과 메시지들이 모두 끝난 **바로 뒤에** 사용자 메시지 하나를 둔다. 내용은 `"Images from the tool results above:"`와, 이미지마다 `"[<tool id> call <n>] <첨부 한 줄>"`, 그리고 `image_url` 부분이다. Tool 결과 글에는 지금처럼 그 첨부 한 줄이 남는다.
  - 호출과 그 결과 사이에는 아무것도 끼우지 않는다(prompt-composition의 규칙). 이미지 메시지는 그룹 전체가 끝난 뒤에 온다.
  - 이 메시지는 사용자가 쓴 것이 아니다. 앞의 줄로 Tool 결과의 이미지임을 밝힌다. 고정 지시문에도 문장을 하나 더한다: "A user message that starts with 'Images from the tool results above:' only carries images the tools returned; it is not written by the user."
- **`ask_user`의 답의 이미지:** 답은 Tool 결과이므로 위의 Tool 결과 자리에 놓인다.

### 7.3 이미지로 보내지 않는 경우

다음이면 지금의 첨부 한 줄만 간다. 줄에 이유를 더한다(`"not_shown": "…"`).
- 모델이 이미지를 받지 않음: `"this model does not take images"`
- 픽셀 수가 5,000만을 넘음: `"too large to show"`
- 사본을 만들지 못함: `"could not be prepared"`
- 보관본이 만료됨: `"expired"` (V3-P66 §8.5에서 예고한 대로, 그 지점부터 캐시가 한 번 깨진다)
- 이 요청에서 이미지가 이미 `MAX_IMAGES_PER_REQUEST`(100)개: 가장 오래된 것부터 줄로 간다. 공급자의 요청당 상한을 넘지 않기 위한 안전장치다(Q5).

### 7.4 원문 저장 (Q1)

이미지 바이트를 base64로 넣으면, 요청 원문이 Run마다 그 이미지를 통째로 담는다. V3-048에 따라 그 원문을 그대로 DB에 저장하므로, 이미지 하나 500 KB면 그 대화의 Run마다 0.7 MB씩 쌓인다.

권고는 **(a) 저장하는 원문에서 이미지 부분을 보관본 참조로 바꾸는 것**이다.
- Provider는 요청 본문을 만들 때 이미지 URL 자리에 사본의 `himari-blob:<view_blob_key>:<view_media_type>` 표지를 넣는다. 이 본문이 저장된다(`RawRepresentation.WIRE_EXACT_BLOB_REFS`, 새 값).
- 보낼 때(`invoke_prepared`) 표지를 보관본 바이트의 data URI로 바꿔 보낸다. 바꾸기는 결정적이다(같은 보관본 → 같은 바이트). 그래서 저장된 원문과 보관소가 있으면 보낸 바이트를 정확히 다시 만들 수 있다.
- 보관본이 만료된 뒤에는 정확히 다시 만들 수 없다. 기록은 이 한계를 명시한다. Run 기록은 그 이상을 주장하지 않는다(`RawRepresentation`의 원칙).
- 표지를 바꿀 때 보관본이 없으면(만료가 요청 준비와 보내기 사이에 일어남) 보내지 않는다. 그 Run은 지금의 "보내기 전 실패"로 끝난다(`FAILED_NO_RESPONSE`, 결과 확실). 다음 Run은 그 첨부를 줄로 보낸다.
- 표지 문자열은 이미지 URL 자리에만 쓴다. 사용자의 글이나 Tool 결과 글에 같은 문자열이 있어도 바꾸지 않는다. 바꾸기는 JSON 구조의 `image_url.url` 값에만 하고, 문자열 치환을 하지 않는다.
- 이것은 Confirmed 결정(V3-048)의 개정이므로 소유자의 명시 승인이 필요하다.

(b)는 지금처럼 전체 바이트를 저장하는 것이다. 단순하지만 DB가 빨리 커진다. 업그레이드 전 백업(V3-P65)과 `secure_delete`의 비용도 함께 커진다.

## 8. Web·CLI

- **미리보기(Q4):** `GET …/files/{id}/{version}/view`. 기록에 `image`가 있는 파일만 된다(그 밖은 404). 보내는 것은 **모델용 사본**이다. 모델이 본 것과 같고, 작고, 메타데이터가 없다.
  - 응답의 `Content-Type`은 사본의 형식이다(`image/jpeg` 또는 `image/png`, 우리가 만든 것). 그 밖의 머리글: `Content-Disposition: inline`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`.
  - 화면은 `<img>`로만 쓴다. 메시지의 이미지 첨부와 Tool 결과의 이미지는 썸네일(최대 높이 240px)이고, 누르면 지금처럼 내려받는다.
  - 만료된 파일은 썸네일 대신 "expired"를 보인다.
- 이벤트 응답의 파일 보기에 `image: {media_type, width, height} | null`을 더한다. Tool 결과 이벤트에는 `images: [파일 보기]`를 더한다.
- **CLI:** 파일 줄에 `[image 800×600]`을 붙인다. Tool 결과의 이미지도 `[file]` 줄로 보이고 `himari download`로 받는다.

## 9. 수명주기와 한도

| 항목 | 값 |
| --- | --- |
| 모델에게 보일 이미지 하나 | 원본 5,000만 픽셀 이하. 사본은 긴 변 1,568픽셀 이하 |
| 사본 만들기 | 자식 프로세스, 메모리 512 MiB, CPU 10초, 벽시계 30초, 출력 8 MiB |
| 호출 하나가 돌려줄 이미지 | 8개 (넘으면 글 한 줄) |
| 요청 하나의 이미지 | 100개 (넘으면 오래된 것부터 줄, Q5) |
| Tool 이미지 보관 | 다른 파일과 같은 보관소, 같은 만료(1 GiB, 30일) |

- 이미지가 만료되면 그 첨부는 줄로 바뀐다. 그 지점부터 캐시가 한 번 깨진다(V3-P66 §8.5에서 예고).
- 대화를 영구 삭제하면 Tool 이미지 기록도 `files`와 함께 지워진다(V3-P66 §8.1 그대로).

## 10. 계약 변경 (Proposed)

| 모듈 | 변경 |
| --- | --- |
| Data | `ImageInfo`(사본 포함), `sniff_image`, `FileRecord.image`, `FileOrigin.TOOL`, `stage_tool_file`, T7의 Tool 이미지 첨부 확인. 참조 세기에 `view_blob_key`. SQLite v9(`files`의 이미지·사본 열) |
| 새 모듈 `himari/imaging/` | 사본을 만드는 자식 프로세스와 그 실행기(한도, Sandbox가 있으면 그 안). Pillow는 이 자식 프로세스에서만 import. `common`만 쓴다 |
| Tool | `ResultImage`, `ToolOutcome.result_images`. Dispatcher의 보관·대기 기록·블록 붙이기(§4.2) |
| MCP | `render_result`가 이미지를 `result_images`로 |
| Environment (Inside) | `read_file`이 이미지면 이미지를(Q3) |
| Prompt | `AttachmentInfo.image`·`blob_key`·`expired`, `image_tokens`, `shown_as_image`, 고정 문장 하나 |
| Provider | `ProviderCapabilities.image_input`, 선언 목록, 메시지 내용 배열, Tool 결과 이미지 메시지, 표지와 보낼 때의 바꾸기(Q1), `RawRepresentation.WIRE_EXACT_BLOB_REFS` |
| Bootstrap | Provider에 보관본 읽기(`invoke_prepared`의 바꾸기용), Dispatcher에 BlobStore와 사본 만들기, `Application.upload`에서 사본 만들기. 의존성 Pillow |
| Web·CLI | 미리보기 경로(Q4), 이미지 썸네일, 파일 보기의 `image`, CLI의 `[image W×H]` |
| Control | 없음 |

## 11. 하지 않는 것과 이유

- **서버 프로세스에서 이미지를 디코드하지 않는다.** 판정은 헤더만 읽고, 디코드는 한도를 둔 자식 프로세스에서만 한다(§3.1).
- **이미지를 Tool 메시지 안에 넣지 않는다.** OpenAI 형식의 Tool 메시지는 글만 받는다. 공급자에 따라 되는 곳이 있어도, 모델마다 다르게 보내면 시험과 캐시가 복잡해진다. 그룹 뒤의 사용자 메시지 하나는 모든 공급자가 받는다.
- **SVG를 이미지로 다루지 않는다.** 글이고 스크립트를 담을 수 있다. 미리보기도 하지 않는다.
- **이미지 바이트를 `ModelInput`에 넣지 않는다.** 크기 추정과 압축 계획은 바이트가 필요 없다. 바이트는 보낼 때만 읽는다.
- **이미지를 볼 수 없는 모델에게 이미지를 설명해 주는 다른 모델을 부르지 않는다.** 비용과 정확성의 문제가 있고, 모델을 바꾸면 되는 일이다.

## 12. 소유자 결정

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 요청 원문 저장 | (a) 저장 원문은 이미지 자리에 보관본 표지, 보낼 때 바이트로 바꿈. V3-048을 개정(§7.4) (b) 지금처럼 보낸 바이트 그대로 저장 | **(a)** — 이미지 하나가 Run마다 DB에 다시 쌓이는 것을 막는다. 보관본이 있는 동안 보낸 바이트를 정확히 다시 만들 수 있다. 만료 뒤에는 못 만든다는 한계를 기록에 밝힌다 |
| Q2 | 큰 이미지 | (a) 줄이지 않음: 5 MiB·8,000픽셀을 넘으면 모델에게 보이지 않고 이유를 한 줄로 (b) Pillow를 더해 긴 변 1,568픽셀로 줄인 사본(메타데이터 제거)을 따로 보관해 보냄 | 처음 권고 (a). 다시 따져 **(b)** 로 바꿈 — 휴대폰 사진이 공급자 한도로 빠지고, 매 요청이 무거워지고, 위치 정보가 나간다. 디코드의 보안 부담은 §3.1로 막는다 |
| Q3 | 모델이 Workspace 이미지를 보는 길 | (a) `inside/read_file`이 이미지 파일이면 이미지를 돌려줌 (b) 새 Tool `inside/view_image` (c) 없음(첨부·MCP만) | **(a)** — 다른 코드 에이전트의 방식이고, 모델이 Tool을 하나 더 배울 필요가 없다. 모델이 만든 그래프를 스스로 확인할 수 있다 |
| Q4 | Web 미리보기 | (a) 네 형식만 `<img>` 썸네일, 판정한 형식·`nosniff`·`CSP sandbox`로(§8) (b) 없음, 내려받기만 | **(a)** — 사용자가 모델이 본 것을 볼 수 있다. 판정한 래스터 형식만 보내므로 이 출처에서 스크립트가 돌 길이 없다 |
| Q5 | 요청 하나의 이미지 수 | (a) 크기 예산과 압축에 맡기고, 안전장치로 100개(넘으면 오래된 것부터 줄) (b) 최근 20개만 이미지, 나머지는 줄 | **(a)** — 이미지도 토큰으로 세므로 예산이 차면 압축이 오래된 것을 접는다. (b)는 새 이미지가 올 때마다 오래된 메시지의 모양이 바뀌어 매번 캐시가 깨진다 |

**소유자 결정(2026-10-22):**
- **Q1 (a):** 저장하는 요청 원문에서 이미지 자리는 보관본 표지로 하고, 보낼 때 바꾼다. Confirmed 결정 **V3-048의 개정**이다. 이 질문을 따로 짚어 여쭌 뒤 "나머지는 괜찮다"로 승인받았다. V3-048에 개정 메모를 남긴다.
- **Q2 (b):** 줄인다. 처음 권고는 (a)였다. 줄이지 않을 때의 손해를 다시 따져 권고를 바꾼 뒤 결정했다. 휴대폰 사진이 공급자 한도로 빠지고, 매 요청이 무거워지고, 위치 정보가 나가기 때문이다. 보안 부담은 헤더 픽셀 확인, 한도를 둔 자식 프로세스, Sandbox로 막는다(§3.1).
- **Q3 (a), Q4 (a), Q5 (a):** 권고대로.

**이미 정해진 것(2026-10-22, 소유자):** 파일 다음에 이미지.

## 13. 위협과 대응

| 위협 | 대응 |
| --- | --- |
| 이미지라고 선언한 HTML·SVG가 미리보기로 이 출처에서 열림 | 형식은 바이트로 판정한 네 래스터만, 응답 형식도 판정값, `nosniff`, `CSP: default-src 'none'; sandbox`, `<img>`로만 씀(§8) |
| 조작한 헤더(거대한 가로·세로)로 추정이나 서버가 무너짐 | 판정은 헤더만 읽음, 5,000만 픽셀을 넘으면 디코드하지 않음(§3) |
| 압축 폭탄·코덱 취약점으로 서버가 멈추거나 뚫림 | 디코드는 자식 프로세스에서만, 메모리·CPU·시간·출력 한도, Sandbox가 있으면 그 안(§3.1). 실패는 "사본 없음"일 뿐 |
| 사진의 위치 정보가 공급자에게 감 | 사본은 메타데이터 없이 다시 인코딩(§3.1). 원본은 내려받기에만 |
| 사용자의 글에 표지 문자열을 넣어 보관본을 끌어옴 | 바꾸기는 JSON의 `image_url.url` 자리에만, 문자열 치환 없음(§7.4) |
| 다른 사용자의 보관본을 표지로 가리킴 | 표지는 Provider가 이 대화의 기록에서만 만듦. 주소에 principal이 들어 있음(V3-P66 C11) |
| Tool 이미지 메시지를 사용자의 말로 여김 | 앞 줄로 밝히고 고정 문장으로 알림(§7.2) |
| MCP 서버가 거대한·많은 이미지를 돌려줌 | 호출당 8개, 하나 32 MiB, 보관소 한도. 넘으면 글 한 줄(§4.2) |
| 만료가 준비와 보내기 사이에 일어남 | 보내지 않고 "보내기 전 실패"로, 다음 Run은 줄로(§7.4) |
| 이미지를 못 보는 모델에 이미지를 보내 요청이 거부됨 | 선언(확인한 모델만)에 따라 줄로(§7.1, §7.3) |
| 원문 저장이 이미지로 DB를 채움 | 표지 저장(Q1) |

## 14. 검증 계획

- **판정:** 네 형식의 실제 파일과 잘린 헤더, 형식은 맞는데 크기가 0인 것, 확장자만 png인 HTML, SVG, 거대한 가로·세로 값.
- **사본:** 큰 사진은 긴 변 1,568로 줄고, 작은 것은 크기 그대로. EXIF 회전이 적용되고 메타데이터(위치 포함)가 사라짐. 투명 PNG·GIF는 PNG, 나머지는 JPEG. 움직이는 GIF는 첫 프레임. 압축 폭탄(헤더는 작게 속이고 실제는 큼)은 한도에 걸려 "사본 없음". 자식 프로세스가 한도를 넘으면 죽고 서버는 그대로. 같은 원본이면 같은 사본이 쓰임(다시 만들지 않음).
- **Data:** `stage_tool_file`과 T7 첨부(같은 호출의 것만, 다른 호출·다른 대화·업로드면 `INVALID_ARGUMENT`), v8 → v9 업그레이드, 양쪽 백엔드. 사본 보관본이 참조로 세어짐: 정리·만료 제외·영구 삭제 해제.
- **Dispatcher:** 이미지가 보관되고 결과 끝에 첨부로 붙음. 이미지가 아니면 줄, 보관소가 가득 차면 줄이고 결과는 성공. 9개째부터 줄.
- **MCP:** `image` 항목이 `result_images`로, 잘못된 base64는 줄로.
- **`read_file`:** 이미지 파일이면 이미지와 글. 이미지가 아니면 지금처럼.
- **Prompt:** `image_tokens`가 두 공식의 큰 값. 이미지를 볼 수 있는 모델과 없는 모델의 추정이 각각 보내는 것과 맞음. 압축 입력에는 줄만.
- **Provider:** 사용자 메시지의 내용 배열(글, 줄, 이미지 순서). Tool 그룹 뒤의 이미지 메시지 하나(호출과 결과 사이에는 없음). 못 보는 모델·너무 큼·만료·100개 초과는 줄과 이유. 저장 원문은 표지이고 보내는 바이트는 data URI이며, 같은 입력이면 둘 다 같은 바이트. 사용자 글 속의 표지 문자열은 바뀌지 않음. 보관본이 없으면 보내지 않음.
- **실시간 확인(opt-in, `tests/live`):** 이미지를 받는다고 선언된 모델마다 두 가지를 실제 요청으로 확인한다. ① 사용자 메시지의 이미지를 읽어 그 안의 글자나 색을 답하는지 ② Tool 그룹 뒤 이미지 메시지 방식의 이미지를 읽고 이어서 답하는지. 통과한 모델만 `image_input`을 켠다. 결과는 `docs/openrouter-model-catalog.md`에 적는다.
- **Web·CLI:** 미리보기 머리글(HTML을 PNG로 속여 올려도 판정에서 이미지가 아님 → 404), 썸네일, CLI 줄. 브라우저 스모크에 이미지 첨부와 Tool 이미지의 썸네일.
