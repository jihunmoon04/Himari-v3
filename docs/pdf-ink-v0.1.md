# PDF 필기와 에이전트에게 넘기기 v0.1

> 2026-10-26 · **PROPOSED (V3-P77)** · rev. 1: 설계 초안.
>
> 소유자 방향
> - V3-P75 설계 때(2026-10-25): 나중에 iPad에서 PDF에 Apple Pencil로 필기하고, 그것을 바로 에이전트에게 넘긴다.
> - 2026-10-26: V3-P75 5단계(PDF 필기)의 설계 문서를 시작한다.
> - 디자인은 지금 웹처럼 간다(V3-P75 §3). 구체적인 디자인은 나중에 바꿔도 된다.
> - 2026-10-26: 직접적인 필기 구현은 레퍼런스가 있다면 참고해도 좋다(§4.6).
>
> 앞선 결정
> - **V3-P75(웹 탐색기·편집기)**
>   - 대화는 가운데, 파일은 오른쪽 패널에 둔다. 패널은 전체로 키울 수 있다.
>   - 소유자의 웹 쓰기는 Workspace와 상관없이 숨김 밖이면 된다. runner를 거치고, 판 정보(identity)로 충돌을 감지한다.
>   - 저장하면 이전 판이 남는다. runner가 rename 전에 뜬 사본이고, `VersionStore`에 30일/200 MiB까지 둔다. **1 MiB가 넘는 파일은 사본을 못 떠서 웹에서 바꾸지 않는다.**
>   - PDF는 지금 크기와 종류만 보이고, 다운로드로만 받는다. 다운로드는 `attachment`이고 `CSP: sandbox`다. §10.2에 이 단계의 방향(pdf.js, ink 주석, 페이지 이미지)이 있다.
> - **V3-P76(편집기에서 에이전트 부르기)**: 참조 칩(`FileReference`)과 Ask HIMARI가 있다. 저장하지 않은 것은 가리키지 않는다(Q1, "Save first").
> - **V3-P67(이미지)**: 첨부한 PNG·JPEG는 Sandbox 안에서 사본으로 다시 만들어 모델에게 보낸다.
> - **V3-P70(문서 도구)**: `machine/read_document`가 PDF를 페이지 글로 읽고, 고른 페이지는 이미지로 그려 모델에게 보인다(pypdfium2). 이 렌더러는 주석도 그린다.

## 1. 범위

정하는 것
1. **보기**: 파일 패널에서 PDF를 연다(§3).
2. **필기**: 펜, 형광펜, 지우개, 되돌리기를 쓴다. Apple Pencil은 필압을 쓰고, 손바닥은 무시한다(§4).
3. **저장**: PDF 표준 ink 주석으로 저장한다. 원본 바이트는 그대로 두고 뒤에 덧붙인다(증분 저장). 이전 판을 남긴다(§5, §6).
4. **에이전트에게 넘기기**: "이 페이지를 HIMARI에게"로 페이지 이미지와 페이지 참조를 대화에 붙인다(§7).

정하지 않는 것
- 글 상자, 도형, 글 선택 강조, 스티커 메모, 양식 채우기, 서명.
- 페이지 편집(더하기, 지우기, 돌리기, 순서 바꾸기).
- 암호가 걸린 PDF에 쓰기. 보기는 암호가 없어도 열리는 것만 한다.
- 글 찾기, 쪽 미리보기 목록.
- 에이전트가 PDF에 필기하는 것.
- 손글씨를 글로 바꾸는 서버 쪽 인식. 모델이 페이지 이미지를 보고 읽는다.
- 페이지 일부(사각형)만 골라 넘기기. 나중의 일이다(§12).

## 2. 다른 도구는 어떻게 하나 (2026년 기준, 제가 아는 범위)

- **GoodNotes·Notability(iPad)**
  - 펜 입력만 필기로 받고, 손가락은 넘기기와 확대에 쓴다. 필압에 따라 굵기가 바뀐다.
  - 필기는 앱 안에 따로 두고, 내보낼 때 PDF에 합친다.
- **Apple 미리보기·파일 앱의 마크업**: PencilKit으로 쓰고, PDF 주석으로 저장한다. 다른 앱에서도 보인다.
- **Adobe Acrobat**: 필기는 `/Ink` 주석이고, 모양(appearance stream)을 함께 쓴다. 저장은 보통 증분 저장이다.
- **Firefox의 PDF 보기(pdf.js)**: 잉크, 강조, 글 상자 편집기가 있다. 저장하면 원본 뒤에 증분 갱신을 덧붙인다.
- **Claude·ChatGPT 앱**: 그린 것을 넘길 때는 페이지(또는 일부)를 이미지로 붙인다.

이 문서는 다음을 따른다.
- 입력: iPad 노트 앱의 펜 규칙.
- 저장: Acrobat과 pdf.js처럼 표준 주석과 증분 저장.
- 넘기기: 페이지 이미지.

## 3. 보기

### 3.1 화면

- 탐색기에서 `.pdf`(또는 앞부분이 `%PDF-`인 파일)를 열면, 다운로드 안내 대신 PDF 보기가 열린다. 위치는 지금의 파일 쪽(패널, 또는 Files 화면)이다.
- 페이지는 세로로 이어진다. 손가락으로 넘기고 두 손가락으로 확대한다. 데스크톱에서는 휠로 넘기고 Ctrl+휠로 확대한다.
- 머리(지금의 경로 줄 오른쪽 묶음)에 둘 것
  - 쪽 표시 "3 / 12"(눌러서 쪽 번호로 가기).
  - 도구(§4).
  - Save, "Ask HIMARI".
  - 파일 메뉴(⋯): Previous versions, Download, Delete.
- iPad에서는 패널을 전체로 키워서 쓴다(지금의 확대 버튼). 줄이면 대화로 돌아온다.
- 색, 글꼴, 간격은 기존 토큰만 쓴다. 테마가 어두워도 페이지 자체는 종이색 그대로 그린다.

### 3.2 그리기

- 렌더러는 **pdf.js**(`pdfjs-dist`, Apache-2.0)다.
  - CodeMirror처럼 scratch에서 묶어 `static/pdfjs/`에 넣는다. `LICENSE`와 `SOURCE`(판, 내려받은 곳, 해시)를 함께 둔다.
  - 판은 고정하고, 판을 올리는 것도 PR로 한다.
  - 파싱은 pdf.js의 Web Worker에서 한다. worker 파일도 같은 출처의 `static/`에서 온다.
- 보이는 쪽과 그 앞뒤 한 쪽만 canvas로 그린다. 멀어진 쪽의 canvas는 비운다.
- 화면 배율(devicePixelRatio)을 곱해 선명하게 그린다. 다만 **canvas 하나는 1,600만 픽셀**(iOS Safari의 한도) 아래로 줄인다.
- 기존 주석(다른 앱의 필기, 강조 등)은 pdf.js가 페이지와 함께 그린다. 고치거나 누를 수는 없다(§4.4).
- 바이트는 `GET /api/machine/download`를 `fetch`로 받는다(ArrayBuffer). 이미 sandbox 머리글이 붙은 끝점이라 새 읽기 끝점은 없다.

### 3.3 한도 (Q2)

- 보기와 필기는 **64 MiB까지** 한다. 넘으면 지금처럼 크기와 다운로드만 보인다.
- iPad Safari의 메모리 때문이다. 한 탭이 다룰 수 있는 양에 여유를 둔 값이다.
- 암호가 필요한 PDF는 "This PDF is password-protected; download it to open." 으로 끝난다.
- 암호 없이 열리지만 암호화된 PDF(권한 암호만 있는 것)는 보기만 하고 필기 도구를 끈다. 새 주석도 암호화해야 하기 때문이다(§5.4).

### 3.4 보안 (Q6)

PDF는 믿을 수 없는 입력이다. 클라우드 폴더나 인터넷에서 온 파일일 수 있다. pdf.js는 페이지 출처에서 돈다. 그래서 pdf.js의 버그는 곧 소유자 세션의 위험이다. 그래서 다음을 지킨다.
- **eval을 끈다.** pdf.js의 `isEvalSupported: false`. 페이지 CSP(`default-src 'self'`)에도 `unsafe-eval`이 없다. 알려진 pdf.js 코드 실행 결함(2024년 글꼴 경로)은 eval을 거쳤다.
- **PDF의 스크립트를 끈다**(`enableScripting: false`). XFA 양식을 끈다(`enableXfa: false`).
- **주석을 DOM으로 만들지 않는다.** 링크, 양식 칸, 첨부를 누를 수 있는 층(annotation layer)을 두지 않는다. 주석은 canvas에 그림으로만 그린다. 그래서 PDF 안의 링크는 이번 판에서 누를 수 없다.
- CSP는 바꾸지 않는다. pdf.js의 wasm 디코더(JPEG 2000 등)가 `wasm-unsafe-eval`을 요구하면, 그것을 더하지 않고 pdf.js의 JS 대체 경로를 쓴다. 1단계에서 확인한다.
- 다른 길(Q6의 B): PDF 보기 전체를 MCP Apps의 sandbox 출처(V3-P58) iframe 안에서 돌린다. 바이트와 결과만 postMessage로 오간다. 더 단단하지만, 정적 파일을 두 출처에 내야 하고 큰 바이트를 메시지로 옮겨야 한다. 펜 입력과 확대도 iframe 안에서 처리해야 한다. 권고는 A(위의 설정)다. B는 pdf.js의 결함이 또 나오면 옮겨 갈 길로 남긴다.

## 4. 필기

### 4.1 입력 규칙 (iPad 노트 앱과 같게)

- **펜**(`pointerType: "pen"`, Apple Pencil): 도구가 골라져 있으면 늘 그린다.
- **손가락**(`touch`): 그리지 않는다. 넘기기와 확대만 한다.
  - 펜으로 쓰는 동안 닿은 손바닥은 그래서 무시된다.
  - 펜이 없는 기기를 위해 도구 메뉴에 "Draw with finger"를 둔다. 기본은 꺼짐이다.
- **마우스**: 데스크톱에서 펜 도구가 골라져 있으면 그린다.
- 펜이 닿은 동안은 그 페이지에서 스크롤이 일어나지 않게 막는다(`preventDefault`). 손가락 스크롤은 브라우저에 맡긴다(`touch-action: pan-x pan-y pinch-zoom`).
- 부드럽게
  - `getCoalescedEvents()`로 사이 점까지 받는다.
  - 그리는 중에는 `getPredictedEvents()`로 끝을 조금 앞서 그려 지연을 줄인다. 저장하는 선에는 넣지 않는다.

### 4.2 도구

| 도구 | 동작 |
| --- | --- |
| 펜 | 색 셋(검정, 파랑, 빨강), 굵기 둘. 필압(`pressure`)에 따라 굵기가 바뀐다. 마우스는 필압이 없으니 고정 굵기다 |
| 형광펜 | 노랑, 반투명, 고정 굵기, 글 위에 겹쳐 보인다 |
| 지우개 | 닿은 **선 하나를 통째로** 지운다(stroke eraser). 픽셀 단위로 지우지 않는다 |
| 되돌리기·다시하기 | 이번에 연 동안의 그리기와 지우기. Ctrl/Cmd+Z, Shift+Ctrl/Cmd+Z |

- 마지막에 쓴 도구와 색은 브라우저에 기억한다(`localStorage`, 실패해도 기본값).

### 4.3 좌표

- 점은 PDF 사용자 공간(포인트)으로 바꿔 둔다. pdf.js viewport의 `convertToPdfPoint`를 쓴다.
- 그래서 확대나 화면 크기가 바뀌어도 선이 그 자리에 있다. 페이지 회전(`/Rotate`)과 CropBox도 이 변환이 다룬다.
- 선은 점들(x, y, 필압)과 도구, 색, 굵기다.

### 4.4 지울 수 있는 것

- 이번에 그린 선(아직 저장 안 함).
- **HIMARI가 저장한 필기.** 주석 이름(`/NM`)이 `himari-ink-`로 시작하는 ink 주석이다. 지우면 다음 저장에서 그 페이지의 `/Annots`에서 뺀다.
- 다른 앱이 만든 주석은 그림으로만 보이고 지우지 않는다. 남의 형식을 망가뜨리지 않기 위해서다.

### 4.5 저장하지 않은 필기 (Q5)

- 머리의 상태 표시는 편집기와 같다: Saved, Unsaved, Saving….
- 다른 파일·폴더·대화로 가려 하면 편집기처럼 묻는다(`mayLeave`).
- **브라우저에 임시로 남긴다.** iPad Safari는 탭을 자주 다시 불러온다.
  - 저장 안 한 선(JSON)을 IndexedDB에 둔다. 키는 경로와 identity다.
  - 같은 파일의 같은 판을 다시 열면 "Unsaved ink from before was restored"와 함께 되살린다.
  - 판이 달라졌으면 묻는다: "Put it on the current version" 또는 "Discard". 선은 페이지 좌표라 새 판 위에 다시 올릴 수 있다.
  - 저장하면 지운다. 저장소를 쓸 수 없으면 조용히 건너뛴다(`try/catch`).

### 4.6 참고할 구현 (소유자: 레퍼런스가 있으면 참고해도 좋다, 2026-10-26)

필기의 손맛은 직접 처음부터 맞추기 어렵다. 그래서 검증된 오픈소스를 쓰거나 참고한다. 라이선스가 허용하는 것만 쓰고, 묶어 넣는 것은 `LICENSE`·`SOURCE`를 함께 둔다(CodeMirror와 같음).

| 구현 | 라이선스 | 쓰는 방법 |
| --- | --- | --- |
| **perfect-freehand**(Steve Ruiz) | MIT | **그대로 쓴다.** 점(x, y, 필압)을 받아 굵기가 바뀌는 선의 외곽선 다각형을 돌려준다. 화면에 그릴 때와 모양 스트림(§5.1)에 같은 외곽선을 쓴다. 그래서 화면과 저장 결과가 같다. Excalidraw의 자유 그리기도 이것을 쓴다 |
| **pdf.js의 InkEditor**(Firefox) | Apache-2.0 | 저장 경로(§5.2)와 좌표 변환, 주석 직렬화를 참고하거나 그대로 쓴다. 입력 규칙(§4.1)이 우리와 다르면 입력 층만 우리 것으로 둔다 |
| **Excalidraw**의 자유 그리기 | MIT | 점 줄이기, 압력이 없는 입력(마우스)에서 굵기를 흉내 내는 방법을 참고 |
| Apple의 웹 문서(Pointer Events와 Apple Pencil) | — | `pressure`, `altitudeAngle`, `getCoalescedEvents`, `getPredictedEvents`의 Safari 동작 확인 |

- **tldraw**는 참고만 한다. 코드는 가져오지 않는다. 라이선스가 오픈소스가 아니다(상용 이용에 따로 라이선스 필요).
- 매개변수(굵기, 가늘어짐, 부드러움, 형광펜 모양)는 2단계에서 iPad로 써 보고 정한다. 소유자 확인을 받는다.

## 5. 저장 (Q1)

### 5.1 결과 파일의 계약

저장한 파일은 다음을 지킨다.
1. **원본 바이트는 한 바이트도 바꾸지 않는다.** 새 파일은 "원본 + 증분 갱신"이다(ISO 32000 §7.5.6 incremental update).
   - 그래서 다른 앱이 쓴 것, 디지털 서명의 서명 범위, 우리가 모르는 기능이 그대로 남는다.
   - 증분 갱신은 원본이 쓰는 상호참조 형식을 따른다(표 또는 상호참조 스트림).
2. 새 필기는 페이지마다 **`/Subtype /Ink` 주석**이다.
   - `/InkList`: 선의 중심 점들.
   - `/C`(색), `/CA`(형광펜은 투명도), `/BS << /W 굵기 >>`.
   - `/NM (himari-ink-<무작위>)`, `/M`(시각), `/F 4`(인쇄됨).
   - **`/AP /N` 모양 스트림.** 필압에 따라 굵기가 바뀌는 선은 모양 스트림의 채운 외곽선(§4.6의 perfect-freehand가 만든 것)으로 그린다. `/InkList`와 `/BS /W`만으로는 굵기가 바뀌는 선을 나타낼 수 없기 때문이다. 모양 스트림이 있어서 Acrobat, 미리보기, pypdfium2, pdf.js가 같은 모양으로 그린다.
3. 바뀐 페이지는 새 `/Annots` 배열을 가진 페이지 객체로 다시 쓴다. 지운 HIMARI 주석은 그 배열에서 빠진다(객체는 남아도 쓰이지 않는다).

### 5.2 누가 만드나

- 증분 갱신은 **브라우저**가 만든다. pdf.js가 이미 문서 구조를 읽었기 때문이다.
- **1단계에서 확인할 것.** pdf.js의 쓰기 경로(annotation storage → `saveDocument`, 증분 갱신을 쓰는 곳)에 §4의 선을 넣을 수 있는지 본다. pdf.js는 Firefox에서 ink 주석을 이렇게 저장한다.
  - 넣을 수 있으면 그 경로를 쓴다. 판을 고정하고, 왕복 시험(§10)으로 지킨다.
  - 넣을 수 없으면 증분 갱신 작성기를 직접 만든다. 새 객체, 페이지 객체, 상호참조, trailer(`/Prev`)를 쓰고 압축은 Flate로 한다.
  - 어느 쪽이든 결과는 §5.1의 계약을 지킨다.
- pdf-lib처럼 파일 전체를 다시 쓰는 방식은 쓰지 않는다. 원본이 바뀌고(서명이 깨짐) 큰 파일에서 느리기 때문이다.

### 5.3 서버로 보내는 것: 증분 갱신만

- 브라우저는 **덧붙일 바이트만** 보낸다. 보통 수 KB이고, 한도는 8 MiB다.
  - iPad의 모바일 회선으로 30 MB짜리 교재를 저장할 때마다 다시 올리지 않기 위해서다.
- 새 끝점: `POST /api/machine/pdf/append?path=…`
  - 본문: `application/octet-stream`, 덧붙일 바이트.
  - 머리글: `X-Himari-Expected: <identity>`. 브라우저가 읽은 파일의 identity이고, 십진 문자열 넷을 쉼표로 잇는다.
  - 이 경로만 본문 한도를 8 MiB로 늘린다(`BodyLimitMiddleware`의 `larger`).
- 서버(Bootstrap `Explorer.append_pdf`)가 하는 일
  1. 숨김·경로 검사, 크기 한도(64 MiB + 8 MiB).
  2. runner `open`으로 파일을 연다. 그 identity가 `expected`와 다르면 409 `changed`다.
  3. 원본을 임시 파일에 복사하고 덧붙일 바이트를 붙인다.
  4. 가벼운 확인을 한다. 원본이 `%PDF-`로 시작한다. 덧붙일 바이트에 `startxref`가 있고 `%%EOF`로 끝난다. 그 `startxref` 값이 원본 크기보다 크고 새 파일 크기보다 작다. 구조 전체는 검사하지 않는다. 브라우저도 소유자의 것이고, 망가진 결과는 이전 판으로 되돌린다.
  5. runner `write`(`mode: "replace"`, `expected`, 내용은 fd)로 원자적으로 바꾼다. runner가 그사이 바뀌었으면 거부한다(`CHANGED`).
  6. runner가 준 사본으로 이전 판을 남긴다(§6).
  7. 답: 새 identity, 이전 판을 남겼는지.
- 브라우저는 "원본 + 증분"을 이미 메모리에 갖고 있다. 그래서 새 identity를 받으면 다시 받지 않고 그 바이트로 문서를 다시 연다.

### 5.4 쓰지 않는 경우

- 암호화된 PDF: 필기 도구를 끈다(§3.3).
- 64 MiB 넘는 파일: 보기부터 하지 않는다.
- 서명이 있는 PDF: 쓸 수 있다. 증분 저장이라 서명한 범위는 그대로다. 다만 서명 정책(DocMDP)에 따라 주석 추가를 "서명 뒤 변경"으로 보이는 앱이 있다. 그래서 처음 그릴 때 한 번 알린다: "This PDF is signed. Notes are added after the signature; some apps show that as a change."
- runner가 프로토콜 3보다 오래됐으면(§6.1) 저장을 거부한다: "restart himari-runner".

### 5.5 충돌

- 저장할 때 파일이 바뀌어 있으면(에이전트, 다른 기기, Drive 동기화) 409 `changed`다. 편집기와 같은 막대가 뜬다.
  - **"Load the server's version and keep my ink"**: 새 판을 열고, 저장하지 않은 선을 그 위에 다시 올린다(페이지 좌표). 쪽 수가 줄어 없어진 쪽의 선은 버리고 알린다.
  - "Discard my ink".
- 탐색기가 하던 대로, 열려 있는 동안 몇 초마다 `stat`으로 바뀜을 본다. 저장 안 한 선이 없으면 새 판을 조용히 다시 연다.

## 6. 이전 판 (Q3)

### 6.1 runner: 큰 파일의 사본 (프로토콜 3)

- 지금 `replace`는 1 MiB 넘는 파일을 거부한다(사본 한도 `MAX_SNAPSHOT_BYTES`).
- `write`(`mode: "replace"`)에 `copy_max`(바이트)를 더한다. **runner가 지키는 상한은 64 MiB**다. 그 크기까지 rename 전에 사본을 뜬다(지금과 같은 `O_TMPFILE` 사본).
- `PROTOCOL_VERSION`을 3으로 올린다. HIMARI는 `hello`가 3 이상일 때만 `copy_max`를 보낸다.
  - 2판 runner는 `copy_max`를 모르고 1 MiB에서 거부한다. 그래서 잘못 덮어쓰지는 않지만, PDF 저장이 늘 실패한다. 그 경우 HIMARI가 미리 "restart himari-runner"로 거부한다.
- 편집기의 `save`는 지금처럼 1 MiB다(`copy_max`를 보내지 않음).

### 6.2 보관

- PDF의 판은 **따로 둔다**: `file-versions-pdf/`, 1 GiB / 30일(Q3).
  - 사용자 판(200 MiB)에 섞으면 30 MB짜리 판 몇 개가 텍스트 파일의 판을 모두 밀어낸다(V3-P76 Q5와 같은 이유).
- **한 파일은 30분에 한 판만** 남긴다.
  - 그 파일의 마지막 판이 30분 안에 남았으면 이번 저장은 판을 남기지 않는다.
  - 그래서 "필기를 시작하기 전"으로는 늘 돌아갈 수 있다. 저장할 때마다의 중간 상태로는 못 돌아간다. 열어 둔 동안의 세부는 되돌리기(§4.2)가 맡는다.
  - 30 MB 파일을 열 번 저장해도 판은 하나다.
- Previous versions에는 판의 시각과 크기가 보인다. "Restore"는 그 판을 지금 파일 위에 쓴다(지금 파일도 판으로 남음).
  - PDF 판은 글로 비교할 수 없다. 그래서 Compare 대신 "Open"(그 판을 읽기 전용으로 보기)을 둔다.
- 지운 PDF의 복원(Deleted files)도 같은 저장소에서 한다. 64 MiB까지이고, V3-P76의 `install`을 쓴다.

## 7. 에이전트에게 넘기기 (Q4)

### 7.1 화면

- 머리의 **"Ask HIMARI"**: 지금 보이는 쪽(화면 가운데에 걸친 쪽)을 넘긴다.
  - 저장 안 한 필기가 있으면 버튼이 "Save and ask"가 된다. 누르면 저장한 다음 넘긴다. V3-P76의 "Save first"와 같은 원칙(저장된 것만 가리킨다)을 한 번의 누름으로 한다.
- 대화 입력창에 둘이 붙는다.
  1. **페이지 이미지** 첨부. 브라우저가 pdf.js로 그 쪽을 필기까지 그린다. PNG이고 긴 변은 2,000px이다(필기 글씨를 읽을 수 있는 크기). 기존 업로드(V3-P66)로 올라가고, V3-P67처럼 Sandbox가 사본을 만든다.
  2. **페이지 참조** 칩 "notes.pdf · p. 3". 누르면 그 쪽이 열린다.
- 그다음은 V3-P76과 같다. 칩을 지우거나 글을 더해 보낸다.

### 7.2 데이터와 모델에게

- `FileReference`에 `page`(1부터, 없으면 None)와 `pages`(전체 쪽 수)를 더한다.
  - 이벤트 코덱에 선택 필드 둘을 더한다. 표는 그대로다.
  - 페이지 참조는 `start`/`end`/`excerpt`가 없다(파일 전체 참조와 같은 꼴).
- 참조 요청(`references[]`)에 `page`를 더한다. 서버가 하는 일
  - runner로 파일의 identity를 확인한다(다르면 `reference_stale`).
  - 쪽 수는 서버가 문서 작업자(V3-P70)로 센다. 작업자가 없거나 실패하면 브라우저가 보낸 값을 쓰지 않고 `pages`를 비운다.
  - `page`가 범위 밖이면 거부한다.
- 모델에게 가는 줄:

  ```
  <file_reference>{"path": "/home/coder/drive/notes.pdf", "page": 3, "pages": 12}</file_reference>
  ```

- `FILE_REFERENCE_NOTE`에 한 줄을 더한다: "A reference with `page` points at one page of a PDF; the attached image of that page shows the user's handwriting as they see it. `read_document` reads the page's text."
- 이미지를 볼 수 없는 모델이면 V3-P67처럼 이미지 대신 줄이 가고, 웹이 경고를 띄운다. 참조는 그대로다.

### 7.3 왜 둘 다인가

- 이미지만 보내면 모델이 원본 글을 정확히 읽을 길이 약하다(작은 글씨, 수식).
- 참조만 보내면 모델이 `read_document`를 불러야 필기를 본다. 부르지 않을 수도 있다.
- 둘 다 보내면, 필기는 이미지로 바로 보고 원문은 Tool로 정확히 읽는다.

## 8. 모듈 경계

| 층 | 바뀌는 것 |
| --- | --- |
| Runner | `write` replace의 `copy_max`(≤ 64 MiB), 프로토콜 3 |
| Environment (machine) | `MachineEnvironment.copies_large()`: 프로토콜 3인지(`keeps()`와 같은 방식) |
| Bootstrap | `Explorer.append_pdf`, PDF 판 저장소(`VersionStore(root/"file-versions-pdf")`, 한도는 생성자 인자), 30분 규칙, 참조의 `page` 확인 |
| Data | 이벤트 코덱: `FileReference.page`, `pages` |
| Prompt | `<file_reference>` 줄의 `page`, `pages`, `FILE_REFERENCE_NOTE` 한 줄 |
| Web | `POST /api/machine/pdf/append`(본문 8 MiB), `show`의 `kind: "pdf"`, 참조 요청의 `page`, `static/pdfjs/`, `static/freehand/`(perfect-freehand), `pdf.js`(보기·필기 부품, `FilesPane`이 PDF를 이 부품에 맡김) |

- 필기 층은 `files.js`와 따로 `pdf.js`(우리 파일. 이름이 겹치면 `pdfpane.js`) 하나에 둔다. `FilesPane`은 PDF 파일을 열 때 이 부품을 불러 쓴다. 편집기 부품과 같은 모양이다(`loadEditor`처럼 `loadPdf`).

## 9. 실패

| 상황 | 결과 |
| --- | --- |
| 저장 중 연결이 끊김 | 서버가 끝까지 가거나(파일 바뀜) 아무것도 안 함. 브라우저는 "Not saved"이고 선은 남는다. 다시 저장하면, 앞의 저장이 됐을 때는 409 `changed`가 되어 §5.5로 간다 |
| 이전 판을 못 남김(저장소 가득) | 편집기처럼 저장하지 않는다(`versions_unavailable`). 30분 규칙으로 건너뛰는 경우는 예외 |
| runner가 오래됨 | 저장 거부, "restart himari-runner" |
| 페이지 이미지 업로드 실패 | 참조 칩만 붙고 "The page image could not be attached" |
| pdf.js가 못 여는 PDF | "This PDF could not be opened here" + Download |

## 10. 보안과 검증

보안
- 쓰기는 소유자의 웹 쓰기다(V3-P75 §7.1). 숨김 목록과 runner를 그대로 거친다.
- 서버는 원본을 runner로 읽고 identity로 묶는다. 브라우저가 보낸 것은 덧붙일 바이트뿐이다. 원본을 바꿔 보낼 길이 없다.
- 페이지 이미지는 V3-P67 경로로만 모델에게 간다(Sandbox 사본).
- pdf.js 설정(§3.4)은 시험으로 지킨다. 설정 객체를 한곳에 두고, smoke가 그 값을 확인한다.

검증
- **왕복**: 필기를 저장한 파일을 다시 연다.
  - 원본 바이트가 새 파일의 앞부분과 같다.
  - pypdfium2(문서 작업자)로 그 쪽을 그리면 필기 자리의 픽셀이 바뀌어 있다.
  - pdf.js로 다시 열면 ink 주석 수와 `/NM`이 맞다.
  - 상호참조 표 PDF와 상호참조 스트림 PDF 둘 다 확인한다. 회전된 쪽, CropBox가 있는 쪽도 확인한다.
- **지우기**: HIMARI 주석을 지우고 저장하면 `/Annots`에서 빠진다. 다른 앱의 주석은 남는다.
- **서버**
  - identity가 다르면 409이고 파일은 그대로다. 덧붙일 바이트 확인이 실패하면 400이다. 한도를 넘으면 413이다.
  - 이전 판, 30분 규칙, 저장소가 가득할 때를 확인한다.
  - 프로토콜 2 runner면 거부한다. 숨김 경로는 거부한다.
- **runner**: `copy_max`까지는 사본을 뜨고 넘으면 `TOO_LARGE`다. 사본은 rename 전 내용이다.
- **입력**: headless Chromium에서 CDP `Input.dispatchMouseEvent`(`pointerType: "pen"`, `force`)로 펜을 흉내 낸다.
  - 펜은 그리고, 터치는 그리지 않는다.
  - 필압이 굵기를 바꾼다.
- **보안**: JavaScript 동작이 든 PDF, 링크가 든 PDF로 확인한다. 스크립트가 돌지 않고 링크가 DOM에 없다.
- **iPad**: 실제 기기 확인은 소유자가 한다. 펜, 손바닥, 두 손가락 확대, 저장, 다시 열기, Ask.

## 11. 단계

1. **보기**
   - pdf.js 번들, 패널의 PDF 보기, 64 MiB 한도, 보안 설정, `kind: "pdf"`.
   - §5.2의 쓰기 경로도 이때 확인하고, 결과를 구현 노트에 적는다.
2. **필기와 저장**
   - 필기 층과 도구, 증분 저장, 끝점, runner 프로토콜 3, PDF 판 저장소와 30분 규칙.
   - 브라우저 임시 보관, 충돌.
3. **Ask HIMARI**: 페이지 이미지, 페이지 참조(`FileReference.page`), 프롬프트.

배포: 2단계부터 himari-runner 재시작이 필요하다(프로토콜 3). 3단계를 쓰기 시작한 뒤 이전 빌드로 되돌리면, 페이지 참조가 든 대화는 V3-P76 1단계 때처럼 열리지 않는다.

## 12. 나중의 일

- 페이지 일부(사각형)만 골라 넘기기. 손글씨가 작은 곳을 크게 보낼 수 있다.
- 글 상자, 강조(글 선택), 도형.
- 글 찾기, 쪽 미리보기 목록.
- pdf.js 보기를 sandbox 출처로 옮기기(Q6의 B).

## 13. 소유자 결정 (대기)

| # | 질문 | 선택지 | 권고 |
| --- | --- | --- | --- |
| Q1 | 필기를 어디에 저장하나? | A. PDF 안의 표준 ink 주석(증분 저장). 다른 앱과 Drive에서도 보임 / B. 따로 둔 파일(예: `notes.pdf.himari-ink.json`). 원본은 그대로지만 HIMARI에서만 보임 | **A** |
| Q2 | 보기·필기 크기 한도? | 32 MiB / **64 MiB** / 128 MiB. 넘으면 다운로드만 | **64 MiB** |
| Q3 | PDF 이전 판 보관? | **따로 1 GiB / 30일, 한 파일은 30분에 한 판** / 사용자 판(200 MiB)에 함께 / 저장할 때마다 한 판 | **따로 1 GiB / 30일, 30분에 한 판** |
| Q4 | 에이전트에게 무엇을 넘기나? | **페이지 이미지 + 페이지 참조** / 참조만(에이전트가 `read_document`로 봄) / 이미지만 | **이미지 + 참조** |
| Q5 | 언제 저장하나? | **직접 저장(Save, Ask 때 저장, 떠날 때 묻기) + 저장 안 한 필기는 브라우저에 임시 보관** / 몇 초마다 자동 저장 | **직접 저장 + 임시 보관**. 자동 저장은 Drive 파일을 저장할 때마다 통째로 다시 올리게 하기 때문이다 |
| Q6 | pdf.js를 어디서 돌리나? | **A. 페이지 출처에서, eval·스크립트·DOM 주석 끄기** / B. sandbox 출처 iframe | **A**. B는 나중의 길로 남김 |
