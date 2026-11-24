# 결정 기록

기준: **2026-11-24**. 지금 유효한 결정만 모았다. 여기 적힌 것은 모두 지켜지고 있으며, 따로 적지 않으면 구현되어 운영 서버에 배포되어 있다. 지금 상태 요약은 [status.md](status.md), 구조는 [architecture.md](architecture.md)에 있다.

- **번호를 새로 매겼다.** 2026-11-24에 `H-01`부터 다시 시작했다. 옛 기록(`V3-001`~`V3-P109`, 철회되거나 바뀐 것 포함)은 [archive/decisions-v3.md](archive/decisions-v3.md)에 그대로 있다. 각 항목의 `근거:`가 그 옛 번호다.
- **코드 주석과 커밋 메시지는 옛 번호를 쓴다.** `V3-P71` 같은 번호를 만나면 옛 기록에서 찾고, 지금도 유효한지는 이 문서에서 본다.
- 세부 계약은 각 설계 문서와 모듈의 `api.py`가 원본이다(H-72).

**바꾸는 규칙**

1. 새 결정은 다음 번호(`H-75` …)로 더하고 결정한 날을 적는다.
2. 있는 결정을 바꾸면 그 항목을 고치고 끝에 `(바꿈 YYYY-MM-DD: 무엇을)`을 붙인다.
3. 결정을 없애면 항목을 지우지 않고 `(없앰 YYYY-MM-DD: 이유)`만 남긴다. 번호는 다시 쓰지 않는다.
4. 코드가 결정을 소급해 정하지 않는다. 코드가 결정과 다르면 둘 중 하나를 고친다.

## 1. 방향

- **H-01** HIMARI v3는 **Windows 반려 에이전트의 서버 쪽 두뇌**다. Windows 앱(`clients/windows/Himari.Companion`)이 몸, 서버(`src/himari`)가 두뇌, Web(`web/`)이 관리 도구, CLI가 운영용 도구다. 범용 코딩·작업 에이전트 일(명령 실행, 프로젝트 폴더 다루기, 작업 콘솔)은 하지 않는다. 그 일은 Claude Code·Codex 같은 도구가 맡는다. 근거: V3-P83, V3-P106, V3-P108
- **H-02** v2의 계층(Task/ManagerTurn, Worktree, Git 기반 권한, Work Apply/Discard, HumanDirection)을 가져오지 않는다. v2의 구현·데이터와 호환된다고 가정하지 않는다. 근거: V3-001, 옛 기록 §D
- **H-03** 계약 기반 모듈화: 모듈 내부를 바꿔도 공개 계약을 지키면 다른 모듈을 고치지 않아도 된다. 근거: V3-013, V3-P05

## 2. 실행 (Control)

- **H-04** **Run = LLM 호출 1회.** Control이 Run 반복을 정하고 모델의 final response에서 멈춘다. Provider의 정규화 출력은 `FINAL | TOOL_CALLS` 둘이다. 근거: V3-001, V3-002, V3-044, V3-P106
- **H-05** 한 턴은 `HIMARI_MAX_RUNS`(기본 12, 1~1000) Run에서 멈춘다. 멈춘 턴은 Continue(`resume`)로 잇는다. 끝이 가까우면 Current State의 `turn_limit`으로 모델에 알린다. 토큰 예산·진척 없음·자리 비움 같은 다른 한도는 없다. 근거: V3-P62, V3-P106
- **H-06** 사용자에게 묻는 것은 **답의 글로** 묻고, 사용자의 다음 메시지가 새 턴이다. 질문 Tool·Pause·남은 턴 닫기는 없다. 기록에 남은 옛 대기 질문은 다음 메시지가 취소로 닫는다. 근거: V3-P106
- **H-07** 실행 중인 작업은 취소(`/cancel`)할 수 있다. 진행 중인 모델 호출은 결과 불명(`OUTCOME_UNKNOWN`)으로 기록하고 다시 보내지 않는다. 진행 중인 Tool은 claim 규칙(H-34)대로, 시작하지 않은 호출은 `CANCELLED`로 닫는다. 근거: V3-057, V3-P106
- **H-08** 같은 대화의 실행은 프로세스 안 대화별 lock으로 하나씩 한다. 이미 실행 중이면 대기열에 넣지 않고 `BUSY`로 바로 거부한다. 다른 대화는 동시에 돈다. 근거: V3-024, V3-045, V3-P24
- **H-09** 크래시 뒤 중복은 영속 표시로 막는다. Provider 송신 직전(`DISPATCH_CLAIMED`)과 부작용 Tool 실행 직전(`EXECUTION_CLAIMED`)에 기록하고, 재시작 때 결과 불명으로 처리하며 자동으로 다시 실행하지 않는다. 저장된 응답은 Provider를 다시 부르지 않고 다시 투영한다. 근거: V3-045, V3-P31
- **H-10** Tool 실패는 실패 ToolResult로 모델에 준다. 결과가 불확실하면 상태를 `UNKNOWN`으로 두고 "결과 불명, 상태를 확인할 것"을 결과로 붙인다. 다시 할지는 모델이 확인한 뒤 새 호출로 정한다. Runtime의 숨은 자동 복구는 없다. 근거: V3-005, V3-047, V3-P37
- **H-11** 메시지는 백그라운드 작업으로 돈다. `/messages`는 입력을 커밋한 뒤에야 202를 돌려준다. 작업 상태는 프로세스 메모리에 있고, 내구 정보는 Data에 있다. 같은 `idempotency_key`의 같은 메시지는 같은 Event를 돌려준다. 근거: V3-P35, V3-P44, V3-P46
- **H-12** 대화마다 모델과 추론 강도를 고른다. 고를 수 있는 것은 운영자 목록(`HIMARI_MODELS`)뿐이고 기본은 `HIMARI_MODEL`이다. 고른 것은 턴 시작에 정해져 그 턴 안에서는 바뀌지 않는다. 목록에서 빠진 모델은 기본 모델로 돈다. 근거: V3-P64
- **H-13** 문맥 압축은 **요약 + 최근 exact tail** 한 가지다. 경계와 시점(예산의 80%)은 코드가 정하고 모델은 요약만 쓴다. 크기는 Provider가 잰 입력 토큰에 추정을 더해 본다. 큰 부분은 나눠 이어 요약하고, 중간에 실패하면 아무것도 커밋하지 않는다. 수동 압축(`/compact`)도 같은 계획과 커밋 경로를 쓴다. 입력 거부 기준은 모델 창에서 출력 몫을 뺀 값이고 `max_tokens`는 보내지 않는다. 문맥 크기 조회(`/context`)는 표시 전용이다. 근거: V3-052, V3-053, V3-056, V3-P48, V3-P49

## 3. Data

- **H-14** 원본을 보존한다. Conversation의 입력·출력은 exact하게, **모든 Run의 원본 Provider 요청·응답**도 남긴다. 요청은 보낸 바이트 그대로(이미지는 보관본 표지), 스트림은 SSE 바이트 그대로다. 인증 헤더는 저장하지 않는다. Tool의 원본 결과도 모델에 준 결과와 따로 둔다. 근거: V3-003, V3-018, V3-048, V3-P36, V3-P73
- **H-15** Prompt Snapshot은 대화별 **최신 1개**(압축 요약 + 이후 exact 입력)만 둔다. 압축해도 원본 기록은 바뀌지 않는다. 다음 Run의 계기는 이미 기록된 Event의 참조다. 근거: V3-004, V3-016, V3-019
- **H-16** 저장소는 SQLite 파일 하나다(WAL, `synchronous=FULL`). 포트 호출 하나가 트랜잭션 하나이고, 검증을 먼저 해서 실패하면 부분 효과가 없다. 저장소는 단일 writer 잠금을 잡는다. DB 파일은 실행 사용자 소유 0600이고 링크는 거부한다. 지금 스키마는 v20이다. 근거: V3-P42, V3-P108
- **H-17** 스키마를 올리기 전에 DB 백업(`himari.db.v<이전 판>-<시각>.bak`, 0600)을 만들고 검증한다. 백업에 실패하면 시작하지 않는다. 백업은 자동으로 지우지 않는다. 되돌리기는 이전 릴리스 + 백업 복원이다. 근거: V3-P65
- **H-18** Data의 공개 Port는 쓰는 쪽(Consumer)마다 나눈 sub-port다. 다른 모듈은 표를 직접 읽거나 고치지 않는다. 근거: V3-039, V3-P43
- **H-19** Tool 구현은 실행 원장을 모르고 `ToolOutcome`만 돌려준다. Tool의 효과, 실행 종결, Conversation Event는 Tool Dispatcher가 한 번에 커밋한다. 근거: V3-042, V3-P11, V3-P26
- **H-20** 대화 삭제는 휴지통 → 복원 또는 영구 삭제다. `HIMARI_TRASH_DAYS`(기본 30)일이 지나면 서버가 영구 삭제한다. 휴지통의 대화는 다른 모든 Port에서 없는 대화와 같다. 영구 삭제는 그 대화의 모든 기록, 대화 폴더, 파일 기록, 그 대화에서 저장된 메모·일화를 지운다. MCP 서버가 자기 쪽에 둔 데이터는 지우지 않고 확인 창에 알린다. 실행 중이면 먼저 취소한다. 근거: V3-P65, V3-P71, V3-P85

- **H-75** (2026-11-24) 작업 에이전트 시대의 추상을 걷었다. 대화는 자기가 가진 **Environment 종류**만 기록한다(만들 때 정함, Data `EnvironmentState.has_environment`). Dispatcher는 대화에 그 종류가 없으면 그 Environment의 Tool을 실행하지 않는다. `machine`의 대화 폴더는 언제나 `conversations/<id>`라서 Binding·Workspace id·작업 폴더 경로를 따로 두지 않는다. Artifact(Plan/Note)는 쓰는 Tool이 없어 없앴다. 스키마 v21이 `activations`를 `environments`로 옮기고 `bindings`·`workspaces`·`artifacts` 표와 Workspace·Binding 열을 지운다. 근거: V3-P19, V3-P32, V3-P101, V3-P106, V3-P109

## 4. Prompt

- **H-21** 순서는 **캐시 우선**이다: Stable Prefix(시스템 글, Tool 정의) → Conversation(압축 요약 + exact Event, Tool 호출과 결과는 `call_id`로 묶은 provider-native 그룹) → Current State. Current State는 마지막 user-role `<runtime_state>` 블록에 JSON 데이터로 들어간다. 모델·추론 설정은 별도 API 필드다. 불완전한 Tool 그룹을 지어내지 않는다. 근거: V3-015, V3-017, V3-026, V3-P38
- **H-22** Prompt 핵심은 I/O 없는 순수 함수 `compose(PromptInputs) -> ModelInput`이고, 읽기는 loader가 한다. Prompt는 모델을 모른다. 시스템 글의 문장은 `prompt.api`에 모은다. 근거: V3-043, V3-049, V3-P89
- **H-23** **지시가 되는 것은 시스템 글과 사용자의 메시지뿐이다.** Tool 결과, 파일, 기억, 창 제목, wake 사유, MCP 데이터는 정보다. 시스템 글에 이 원칙을 한 문단으로 두고, 믿을 수 없는 글이 붙는 자리에만 짧게 표시한다. 근거: V3-P89
- **H-24** 시스템 글: 바탕 글(반려 켬 `COMPANION_BASE` / 끔 `ASSISTANT_BASE`) → 원칙 → 절(메시지와 시각, 도구로 일하기, 기억, 반려) → MCP 서버 안내문 → 사용자의 글(페르소나, 지시문). 운영자가 바탕 글을 주면 둘 다 대신한다. 반려가 꺼지면 짧은 대화 비서로 답하고 페르소나를 넣지 않는다. 근거: V3-P69, V3-P89, V3-P108
- **H-25** Current State에는 `now`(턴 시작 시각, 보낸 기기의 시간대 또는 `HIMARI_TIME_ZONE`), 지금 기기와 하는 일, `user_memory`(핵심 기억, 열린 고리, 최근 일화), 인터페이스, 남은 Run이 들어간다. 30분 넘게 쉬었다 온 메시지 앞에는 저장 시각 줄을 붙인다. 근거: V3-P71, V3-P83, V3-P85, V3-P88, V3-P93
- **H-26** 작업 진행 줄: 시스템 글의 `PROGRESS_NOTE`에 따라 모델은 단기 목표가 바뀔 때만 한 줄을 Tool 호출과 함께 쓴다. Tool 호출과 함께 온 글은 그 호출의 commentary로 보존하고 final로 보지 않는다. Web은 Tool 단계를 작업 묶음 하나로 접는다. 근거: V3-P16, V3-P68
- **H-27** 고정 시스템 규칙과 턴별 `interface`(Web, 기기 등)를 나눈다. 인터페이스는 인증된 출처로 정해 입력·wake와 함께 기록한다. 인터페이스가 바뀌어도 시스템 글과 Tool 정의는 그대로다. 근거: V3-P93

## 5. Provider

- **H-28** Provider는 OpenRouter Chat Completions 하나이고, HTTP 클라이언트로 직접 부른다(SDK 재직렬화·자동 재시도 없음). 응답은 role과 종료 사유가 출력과 맞을 때만 받는다(fail-closed). 오류 응답도 원본을 저장한다. 연결 뒤의 전송 오류는 결과 불명이다. Tool id의 `/`는 `__`로 바꿔 보낸다. 근거: V3-048, V3-P38
- **H-29** 모델 능력은 **실제 요청으로 확인한 것만** 선언한다(`ProviderCapabilities`: 추론 강도, `image_input`, `streaming`, `screen_points`, `screen_tool`, `speaks_first`, `free_wake`, `memory_passes`, `companion`). 선언하지 않은 값은 보내기 전에 거부하거나 그 기능을 끈다. 확인 기록은 [openrouter-model-catalog.md](openrouter-model-catalog.md)와 `tests/live`. 근거: V3-P38, V3-P67, V3-P73, V3-P84, V3-P91, V3-P92
- **H-30** reasoning: 강도는 중립 옵션을 Provider가 네이티브 필드로 바꾼다. 사용량은 기록만 하고 프롬프트에 넣지 않는다. continuation(`reasoning_details`)은 그 assistant 턴에 붙여 두고, 같은 provider·모델일 때만 그대로 다시 보낸다. 비공개 추론 원문은 전제하지 않는다. 근거: V3-006, V3-023, V3-049, V3-P39
- **H-31** 스트리밍: 확인한 모델은 답을 스트림으로 받는다. 조각은 메모리의 `LiveOutput`을 거쳐 `GET …/live`(SSE)로 보인다. Event는 끝난 답에서만 생긴다. 끝나지 않은 답은 흐리게 보이고 기록과 모델 입력에 들어가지 않는다. 중지하면 연결을 끊는다. `HIMARI_STREAMING=0`으로 끈다. 근거: V3-P73
- **H-32** 캐시: 대화별 해시를 OpenRouter `session_id`로 보내고, 캐시 읽기·쓰기 토큰을 기록한다. 턴의 사용량은 Web 입력창 아래에 보인다. 근거: V3-P62(남은 부분)

## 6. Tool

- **H-33** Tool 출처는 셋이다: Internal(기억, 대화 검색, 화면), Environment(`machine`), External(MCP). 모두 같은 `ToolHandler` SPI를 구현하고, Tool id는 `출처/이름` 꼴의 공통 문법이다. 인자 schema는 지원하는 부분집합만 쓰며 등록 때 fail-closed로 검사한다. 외부 schema 검증은 자원을 제한한 자식 프로세스에서 한다. 근거: V3-008, V3-040, V3-041, V3-054, V3-P33
- **H-34** claim 규칙: Environment·External Tool 가운데 `READ`가 아닌 것은 실행 전에 claim하고, 크래시 뒤 다시 실행하지 않는다. Internal Tool은 claim 없이 결과와 함께 커밋한다. 목록에 보이는 것은 권한이 아니며, Dispatcher가 실행 직전에 다시 확인한다. 근거: V3-045, V3-054, V3-055, V3-P32
- **H-35** 한 Run의 Tool 호출은 지금 원래 순서대로 하나씩 실행한다. 충돌이 없는 호출의 병렬 실행은 계약으로 허용하며, 스케줄러는 Port를 바꾸지 않고 나중에 더한다. 그룹이 모두 끝나야 다음 Run을 시작한다. 근거: V3-022, V3-P12, V3-P25

## 7. 파일·대화 폴더

- **H-36** 대화마다 **대화 폴더** `<HIMARI_DATA_ROOT>/conversations/<id>`(폴더 0700, 파일 0600)를 둔다. 파일 Tool(`machine/list_files`·`read_file`·`write_file`·`edit_file`·`delete_file`)은 서버 프로세스 안에서 **그 대화 폴더 안에서만** 읽고 쓴다. 폴더 밖은 거부(`outside`)하고, 폴더 안의 링크는 따라가지 않는다(`links`). 쓰기는 임시 파일 + 이름 바꾸기다. `HIMARI_MACHINE_PRINCIPALS`에 없는 사용자의 대화는 말만 한다. 명령 실행, 붙인 Workspace, `himari-runner`는 없다. 근거: V3-P74, V3-P100, V3-P101, V3-P106, V3-P109
- **H-37** 올리기는 두 단계다: 파일을 올려 대기 기록을 받고, 메시지가 `attachments`로 참조한다. 올린 원본과 모델이 건넨 파일은 불변 보관소 `<data_root>/files`(해시 주소)에 두고, 보낼 때 대화 폴더의 `uploads/`로 복사한다. 모델은 `present_file`로 파일을 건넨다(그 순간의 바이트가 보관본, 같은 경로는 다음 판). 내려받기는 늘 `attachment` + `nosniff` + `CSP: sandbox`다. 한도: 파일 32 MiB, 메시지당 10개, 보관소 1 GiB, 마지막 사용 뒤 30일. 근거: V3-P66
- **H-38** 이미지: 바이트로 판정한 PNG·JPEG·GIF·WebP만, 5,000만 픽셀까지. 모델에는 sandbox에서 한 번 만든 사본(긴 변 1,568픽셀, 메타데이터 없음)을 보낸다. 첨부, MCP 결과, 파일 Tool로 연 이미지를 `image_input` 모델이 본다. 요청 하나에 100개·32 MiB까지. 근거: V3-P67
- **H-39** 문서 읽기: `machine/read_document`가 PDF·DOCX·XLSX·PPTX·HWPX를 단위(페이지·슬라이드·시트·부분)로 나눈 글로 돌려준다(호출당 64 KiB, 이어 읽기 `next`). `view`면 PDF 페이지를 이미지로 준다. 파싱은 server sandbox 안의 문서 worker가 하고, 라이브러리는 따로 둔 도구 모음(`HIMARI_DOCUMENTS_DIR`)에 있다. 문서를 만드는 스크립트 실행은 없다. 근거: V3-P70, V3-P106, V3-P109
- **H-40** server sandbox: 문서·이미지 worker는 Bubblewrap + Landlock 안에서 돈다(네트워크 없음, 데이터 폴더에 닿지 않음, 자원 한도). 시작 때 실제로 띄워 탈출을 시도하는 자체 검사를 통과해야 켜지고, 통과하지 못하면 그 기능이 꺼진다. sandbox 없이 도는 길은 없다. `HIMARI_SANDBOX=0`이면 sandbox와 함께 그 기능이 꺼진다. 근거: V3-036, V3-037, V3-050, V3-P59, V3-P67, V3-P70

## 8. MCP

- **H-41** MCP 서버는 운영자가 `<HIMARI_DATA_ROOT>/mcp.json`(0600)에 적은 **Streamable HTTP** 서버만 쓴다. URL은 `https://` 또는 loopback `http://`이고, 리다이렉트와 프록시는 쓰지 않는다. 토큰은 환경 변수에서 읽어 그 URL에만 보낸다. 목록에 적은 Tool만 등록하고, 효과 등급(`read`·`scoped_write`·`unknown`)은 운영자가 정한다. 연결되지 않은 서버는 그것만 빠진다. stdio(`command`) 항목은 띄우지 않고 이유와 함께 빠진다. 근거: V3-054, V3-055, V3-P109
- **H-42** 서버는 대화마다 켜고 끈다. 따로 정하지 않은 대화는 서버의 기본값을 따르고, 기본값은 Web Settings → Tool servers에서 바꾼다(`control/mcp-defaults.json`, [mcp-defaults-v0.1.md](mcp-defaults-v0.1.md)). Stop·시간 초과 때는 `notifications/cancelled`를 보낸다(최선 노력). 근거: V3-055
- **H-43** 서버가 주는 다른 것: 안내문은 운영자가 정한 자리(`system`·`data`·`off`, 서버당 4,000자·합 16,000자)에, 리소스는 모델용 Tool `<서버>/list_resources`·`read_resource`로(기본 효과 `unknown`, 목록에 있는 URI만, `ui://`와 사용자용 리소스 제외), 프롬프트는 **사용자가** Web 입력창의 `/`로 골라 채운다. 모델은 프롬프트를 스스로 꺼내지 못한다. 근거: V3-P60
- **H-44** MCP 서버가 준 Tool 화면(MCP Apps)은 보이지 않는다. 모델이 부를 수 없는 화면 전용 Tool은 등록하지 않는다. 근거: V3-P97

## 9. 반려: 기기·표정·화면

- **H-45** 기기 짝짓기: Web에서 8글자 코드(10분, 한 번)를 만들고 앱이 그것을 기기 토큰으로 바꾼다. 서버는 해시만, 앱은 OS 자격 증명 보관소에 둔다. 기기 토큰은 허용 목록의 경로(대화, 접속 신호, 화면, 공개 제공 파일)만 쓰고, 기억·지시문·페르소나·설정·기기·삭제는 403이다. 근거: V3-P83, V3-P93, V3-P108
- **H-46** 사건 흐름은 사용자 단위 `GET /api/events`(SSE)이고 기기와 Web이 같이 쓴다. 서버는 기기 연결과 마지막 입력으로 **지금 기기**를 정한다(메모리에만). 반려 전용 대화는 없다. 앱은 가장 최근의 반려 가능한 대화로 이어 가고 새 대화도 연다. 근거: V3-P83, V3-P92
- **H-47** 표정은 Tool이 아니라 답 안의 `[[mood:이름]]` 표시다. 이름은 고정 목록 8개(`common.MOODS`)다. Data는 원문 그대로 두고, 서버가 떼어 `display`와 `live`로 보낸다. 듣는 중·졸림 같은 앱 자신의 상태는 앱이 정한다. 근거: V3-P83
- **H-48** 화면 보여 주기: 앱이 앞 창이 있는 모니터를 캐릭터를 뺀 채 찍어(긴 변 1,280) 메시지의 `screen`으로 붙인다. 모델에는 최근 두 장만 이미지로 간다. 좌표를 확인한 모델(`screen_points`)은 `[[move:X,Y]]`로 캐릭터를 그 자리로 옮긴다. 클릭·입력은 하지 않는다. 근거: V3-P84
- **H-49** 화면 도구 `look_at_screen`: 그 턴의 기기에만 요청하고 앱이 찍어 보낸다(15초). `screen_tool` 모델에만 주고, 앱 설정 "히마리가 필요할 때 화면 보기"(기본 켬)를 따른다. 찍을 때마다 표시하고, 제외 목록의 프로그램이 보이면 거절한다. 한 Run에 한 번이다. 메시지마다 자동 첨부는 기본 끔이다. 근거: V3-P91
- **H-50** 활동 신호: 앱이 `POST /api/device/presence`로 세션, 활동(고정 분류), 미디어, 마이크·카메라를 기기별 통째 스냅샷으로 보낸다. 서버는 메모리에만 두고 Current State에 정보로 넣는다. 방해 금지는 사용자가 켜는 **"조용히"뿐**이다. 창 제목과 미디어는 기본 끔이다. 근거: V3-P88
- **H-51** 반려 모델: `image_input`·`screen_points`·`screen_tool`·`speaks_first`·`free_wake`를 모두 통과한 모델만 `companion`이다. 기기가 여는 새 대화는 반려 기본 모델(`HIMARI_COMPANION_DEFAULT_MODEL`)을 쓴다. 반려가 안 되는 모델의 대화에 온 기기 입력은 `companion_unsupported`로 거부한다. `HIMARI_COMPANION_MODELS_ONLY=1`이면 반려 모델만 제공한다. 근거: V3-P92
- **H-52** 기기는 그 대화의 공개 제공 파일(`present_file`이 성공한 것)만 목록으로 보고 내려받는다. 앱은 파일 카드와 저장 대화상자를 보이고 크기·SHA-256을 확인한다. 기기 기능 선언은 연결마다 받은 `connection_generation`에 묶는다. 근거: V3-P93
- **H-53** 캐릭터 그림: Aseprite 원본을 내보내기 스크립트가 색 인덱스 시트 한 장과 `character.json`으로 바꾸고, 둘 다 저장소에 넣는다. 앱의 `CharacterRenderer`·`CharacterAnimator`가 몸·표정·눈입을 합친다. 서버 계약과는 무관하다. 근거: V3-P86
- **H-54** Windows 앱은 소유자의 로컬 에이전트가 만들고 기술·애니메이션·형식을 정한다. 앱이 지킬 계약: 토큰은 OS 보관소, 서버 요청은 네이티브 쪽에서, 원격 코드 실행 금지. 근거: V3-P83

## 10. 먼저 말 걸기

- **H-55** 서버가 여는 턴의 계기는 `wake` 이벤트다. **언제**는 서버 규칙이, **무엇을**은 모델이 정한다. `begin_wake` 한 트랜잭션이 이벤트, wake 기록, 중복을 막는 열쇠를 남긴다. wake는 가장 최근의 반려 가능한 대화에서 지금 기기의 캐릭터가 말한다. wake 도중 사용자 메시지가 오면 사용자가 이긴다. `bootstrap/schedule`이 30초마다 판단하고, 규칙은 순수 함수 모듈 `himari.proactive`에 있다. 근거: V3-P87, V3-P92
- **H-56** 계기는 다섯이다: 알림(`loop` 기억의 `remind_at`, 모델·사용자만 정함), 열린 고리, 돌아옴(잠에서 깸, 1시간 이상 비움), 작업 끝남, 자유 wake. 잠듦은 앱이 정해 `sleep`으로 알린다. 근거: V3-P87, V3-P90, V3-P107
- **H-57** 조절 규칙은 세 단계다. ① 모든 wake: 지금 기기 없음·작업 중·잠이면 막는다. ② 돌아옴·작업 끝남: 더해 "조용히"·대화 중 10분. ③ 열린 고리·자유 wake: 더해 답하지 않은 wake·23–08시·간격 15분, 자유 wake는 하루 예산. 알림은 잠든 캐릭터를 깨우지 않고 사용자가 돌아오면 말한다. 근거: V3-P107
- **H-58** 자유 wake: 같은 작업이 `HIMARI_PROACTIVE_FREE_MINUTES`(기본 30)분 이어지면 연다. 하루 `HIMARI_PROACTIVE_FREE_DAILY`(기본 10)개까지이며 pass도 센다. 모델은 `[[pass]]`로 말하지 않을 수 있고 그 wake는 숨긴다. 도구를 부르면 드러나 pass할 수 없다(화면 도구만 예외). `free_wake` 모델만 쓴다. 근거: V3-P90, V3-P91, V3-P107

## 11. 기억·대화 검색

- **H-59** 메모: 맥락이 담긴 1~2문장의 명제다. 분류는 `user`·`preference`·`project`·`reference`, 그리고 자동 기억의 `loop`(열린 고리)다. 하나 500자, 1,000개까지. 모델은 사용자가 말한 것만 `memory_save`로 승인 없이 저장하고(대화에 줄과 Undo가 남음), `memory_forget`·`memory_search`를 쓴다. `user`·`preference`는 Current State `user_memory`(2,000자)에 들어가고 나머지는 검색한다. 턴 시작에 읽는다. `HIMARI_MEMORY=0`은 모델의 사용만 끈다. 근거: V3-007, V3-020, V3-P71
- **H-60** 자동 기억: 대화가 잠잠해지면(작업 끝 10분 뒤) 확인된 싼 모델(`memory_passes`)이 한 번 읽어 사실·열린 고리의 추가·고침·지움과 일화 하나를 낸다. 입력은 사용자 메시지(HIMARI 답 앞 800자는 일화에만)이고 Tool 결과·파일 글은 넣지 않는다. 사용자가 쓴 기억은 고치지 못한다. 자동 기억은 물러남(`retired_at`)으로 지우고 30일 안에 되살릴 수 있다. 열린 고리와 최근 7일 일화 5개(1,500자)가 Current State에 들어간다. 기억 작업은 대화의 Run이 아니라 따로 둔 작업 기록이며 하루 토큰 상한이 있다. 근거: V3-P85
- **H-61** 대화 검색: `conversation_search`(낱말)와 `conversation_read`로 지난 대화의 원문을 찾는다. 같은 사용자의 대화만, 휴지통 제외, 지금 대화는 압축 요약이 덮는 앞부분만, 사용자 메시지와 최종 답만 찾는다. 색인 없이 접은 글을 훑는다. `HIMARI_CONVERSATION_SEARCH=0`으로 끈다. 근거: V3-P72

## 12. 페르소나·지시문

- **H-62** 페르소나: 이름(목록에만 보임, 모델에 가지 않음, 40자, 겹침 금지) + 글(10,000자), 사용자당 20개. **지금 페르소나는 하나 또는 없음**이고 모든 대화·모든 기기에 다음 턴부터 쓰인다. Web Settings → Personas에서만 다룬다. 반려가 꺼진 서버에서는 저장만 된다. 앱에서 바꾸기, 페르소나별 그림, 페르소나별 기억은 없다. 근거: V3-P108
- **H-63** 사용자 지시문: 사용자당 하나(10,000자), Web Settings → Instructions에서만 고친다. 모델에게는 고치는 Tool이 없다. 시스템 글 끝에 `<user_instructions>` 경계로 들어가며, 턴 시작에 읽어 그 턴 내내 같다. 리비전으로 동시 수정 충돌을 막는다. 근거: V3-P69, V3-P98, V3-P108

## 13. Web·CLI

- **H-64** Web은 React + TypeScript(Vite)로 만든 관리 도구다: 대화와 설정(Instructions·Personas·Memory·Devices·Tool servers·Trash). 서버가 빌드 결과를 직접 서빙하고 화면마다 주소가 있다(`/`, `/c/<id>`, `/trash`, `/settings/<항목>`). 설계는 [web-console-v0.1.md](web-console-v0.1.md)의 §4·§7(hub 부분은 뺐다). 근거: V3-P79, V3-P102(Web 부분), V3-P106
- **H-65** Web은 서버가 아는 상태를 보인다: 서버·모델·sandbox 상태(`/api/server`), 작업이 멈춘 이유, MCP 연결 문제, 다른 대화의 작업 중 표시. 작성 중인 글은 자리(홈, 대화)마다 이 탭에 둔다. 근거: V3-058, V3-P78, V3-P80
- **H-66** CLI는 Web에 없는 명령만 둔다: `himari proactive [try]`, `himari memory passes|backfill|retry`. 실행 중인 서버의 클라이언트이며 DB를 직접 열지 않는다. loopback 주소만, 운영자 비밀값에서 파생한 bearer를 쓴다. 변경 요청은 다시 보내지 않는다. 종료 코드는 0 완료, 1 거부·닿지 못함, 2 사용법, 4 결과 불명이다. 근거: V3-P47, V3-P98

## 14. 운영·보안

- **H-67** 서버는 평문 HTTP라 **loopback에만** 바인딩한다. 다른 기기에서 쓰려면 같은 기기의 TLS reverse proxy 뒤에 두고 `HIMARI_WEB_ALLOWED_HOSTS`·`HIMARI_WEB_ALLOWED_ORIGINS`를 준다. 외부 주소 바인딩과 다른 기기용 평문 Origin은 시작 때 거부한다. 근거: V3-P46
- **H-68** 인증: 운영자가 비밀값으로 로그인하면 서버 메모리 세션을 `HttpOnly`·`SameSite=Strict` 쿠키로 준다(https Origin이면 늘 `Secure`). 쿠키로 하는 변경 요청은 허용된 `Origin`이 있어야 한다. 보안 헤더와 CSP를 늘 붙인다. 남의 대화와 없는 대화는 같은 404다. 근거: V3-P46
- **H-69** 데이터 폴더는 하나(`HIMARI_DATA_ROOT`, 실행 사용자 소유 0700)다: `himari.db`, `files/`, `conversations/`, `control/operator.secret`, `mcp.json`. 운영자 비밀값은 32바이트 난수이며 손상되면 고치지 않고 거부한다. 지금 사용자는 운영자 한 명이다. 근거: V3-P45, V3-P109
- **H-70** 새 서버 코드는 이식 가능한 `os` 호출만 쓴다(서버를 Linux 밖에서도 돌리는 방향). 남은 Linux 의존은 [server-slim-v0.1.md](server-slim-v0.1.md) §7에 있다. 근거: V3-P109

## 15. 모듈 경계·검증

- **H-71** 확장 지점 SPI는 셋이다: `ToolHandler`, `ProviderAdapter`, `EnvironmentPlugin`. Environment는 자기 Tool을 직접 제공하고(`tools()`), kind마다 하나를 Composition Root(`bootstrap`)에 등록한다. 근거: V3-040, V3-041, V3-P19
- **H-72** 계약은 코드로 선언한다. 각 모듈의 `api.py`(Protocol + frozen dataclass, 깊게 불변)가 원본이고 문서는 근거를 담는다. 여러 계약이 함께 쓰는 값은 `himari.common`에만 둔다. 근거: V3-029, V3-046, V3-P21, V3-P29
- **H-73** 의존 방향은 정적 검사(`scripts/check_imports.py`)로 강제한다. 다른 모듈은 `himari.<모듈>.api`와 `himari.common`만 import한다. MCP 커넥터(`src/himari/mcp`)는 `common`과 `tool.api`만 쓴다. 근거: V3-059, V3-P20
- **H-74** pytest는 공개 계약을 만들거나 바꾸거나 없앨 때만 전체를, 그 밖에는 바뀐 모듈만 돌린다. 타입 검사는 `mypy --strict`다. 실호스트 sandbox 검사(`HIMARI_SANDBOX_HOST=1`)와 실제 모델 확인(`HIMARI_LIVE=1`)은 따로 켜는 검사다. 단일 프로세스·단일 사용자 규모이며, 분산 lease·다중 worker는 미뤘다(들이면 공개 계약 변경). 근거: V3-038, V3-045, V3-P06

## 열린 문제

지금 정하지 않은 것만 적는다. 정하면 위에 H 번호로 옮긴다.

- **서버를 어디서 돌릴지.** Windows PC에서 서버와 앱을 함께 돌리려면 남은 Linux 의존(파일 잠금·권한 검사, `setrlimit`, Bubblewrap/Landlock sandbox)을 플랫폼 계층 하나로 모으고 Windows용 worker 격리(Job Object)를 정해야 한다([server-slim-v0.1.md](server-slim-v0.1.md) §7).
- **문서 만들기.** 명령 실행이 빠져 모델이 DOCX·PDF 같은 문서를 만들 길이 없다(글 파일은 `write_file`로 만든다). 필요한지, 필요하면 어떤 Tool로 할지.
- **작업 중에 온 메시지.** 지금은 `BUSY`로 거부한다. 받아 두었다가 다음 Run 사이에 넣을지([companion-v0.1.md](companion-v0.1.md) §12).
- **기억 검색 방식.** 지금은 낱말 검색이다. 임베딩 검색(Provider의 임베딩 Port)이 필요한지.
- **MCP 확장.** 쓰기 Tool의 사용자 확인, OAuth, 끊긴 SSE 이어 받기는 미뤘다. 그때까지 확인 없이 실행되면 곤란한 Tool은 허용 목록에 넣지 않는다.
- **보존과 삭제.** 원본 기록은 영구 삭제 때까지 남는다. 저장 데이터 암호화, 보존 기간, MCP 서버 쪽 데이터를 지우는 훅은 정하지 않았다.
