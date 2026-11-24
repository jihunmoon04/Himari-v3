# 지금 상태

기준: **2026-11-24**. 한 쪽 요약이다. 구조는 [architecture.md](architecture.md), 지금 유효한 결정은 [decisions.md](decisions.md)(H 번호), 배포 기록은 [deployments/](deployments/)에 있다.

## 한 줄 정의

HIMARI v3는 **Windows 반려 에이전트의 서버 쪽 두뇌**다. 대화·기억·먼저 말 걸기를 맡고, 범용 코딩·작업 에이전트 일은 하지 않는다(H-01).

## 구성

| 부분 | 위치 | 맡은 일 |
| --- | --- | --- |
| 몸 | `clients/windows/Himari.Companion` (C#/WPF) | 픽셀 캐릭터·말풍선·표정, 활동 신호, 화면 찍기, 단축키 |
| 두뇌 | `src/himari` (Python, FastAPI, 단일 프로세스) | 대화(Run 반복), 기억, 먼저 말 걸기, 지금 기기, Tool, MCP |
| 관리 화면 | `web/` (React + TypeScript) | 대화, Settings(Instructions·Personas·Memory·Devices·Tool servers·Trash) |
| CLI | `himari` | 먼저 말 걸기 상태·시험, 기억 정리 다시 돌리기 |

## 지금 동작하는 기능

- **대화와 실행:** Run = LLM 호출 1회, 한 턴은 `HIMARI_MAX_RUNS`(기본 12)에서 멈추고 Continue로 잇는다. 취소, 문맥 압축(자동·수동), 대화별 모델·추론 강도, OpenRouter 스트리밍, 원본 요청·응답 보존. 묻는 것은 답의 글로 한다. → [control-prompt-provider](control-prompt-provider-v0.1.md), [conversation-model](conversation-model-v0.1.md), [streaming](streaming-v0.1.md)
- **Data:** SQLite(WAL) 하나, 스키마 v21, 올리기 전 백업, 휴지통과 30일 뒤 영구 삭제. → [data-contract-v0.2](data-contract-v0.2.md), [conversation-delete](conversation-delete-v0.1.md)
- **프롬프트:** 캐시 우선 순서, 반려 켬/끔 두 바탕 글, "지시는 시스템 글과 사용자 메시지뿐" 원칙, Current State(지금 시각·기기·활동·기억), 작업 진행 줄. → [prompt-composition](prompt-composition.md), [prompt-cleanup](prompt-cleanup-v0.1.md), [interface-capabilities](interface-capabilities-v0.1.md), [work-progress](work-progress-v0.1.md)
- **파일:** 대화 폴더(`<데이터 폴더>/conversations/<id>`) 안에서만 읽고 쓰는 파일 Tool, 올리기·내려받기와 `present_file`, 이미지 입력, `read_document`(PDF·DOCX·XLSX·PPTX·HWPX, server sandbox). → [server-slim](server-slim-v0.1.md), [file-upload-download](file-upload-download-v0.1.md), [images](images-v0.1.md), [documents](documents-v0.1.md)
- **MCP:** Streamable HTTP 서버, 허용 목록의 Tool, 대화별 켬/끔과 Settings의 기본값, 서버 안내문·리소스 Tool·`/` 프롬프트. → [mcp-context](mcp-context-v0.1.md), [mcp-defaults](mcp-defaults-v0.1.md)
- **반려:** 기기 짝짓기와 기기 토큰, 사건 흐름 `GET /api/events`, 지금 기기, 활동 신호와 "조용히", 답 안의 표정 `[[mood:…]]`, 화면 붙이기와 캐릭터 이동 `[[move:X,Y]]`, 화면 도구 `look_at_screen`, 반려 모델 게이팅, 기기에서 제공 파일 받기, 캐릭터 애니메이션, 답 준비 중 표시. → [companion](companion-v0.1.md), [companion-screen](companion-screen-v0.1.md), [screen-tool](screen-tool-v0.1.md), [activity-signals](activity-signals-v0.1.md), [companion-animation](companion-animation-v0.1.md), [windows-reply-progress](windows-reply-progress.md)
- **먼저 말 걸기:** 알림·열린 고리·돌아옴·작업 끝남·자유 wake, 세 단계 조절 규칙, 자유 wake는 30분 간격·하루 10개, `[[pass]]`. → [proactive-v0.3](proactive-v0.3.md)(바꾼 부분), [v0.2](proactive-v0.2.md)·[v0.1](proactive-v0.1.md)(나머지 세부)
- **기억·대화 검색:** `memory_save`·`memory_forget`·`memory_search`, 대화가 잠잠해진 뒤 자동 기억·일화·열린 고리, `conversation_search`·`conversation_read`. → [memory-v0.1](memory-v0.1.md), [memory-v0.2](memory-v0.2.md), [conversation-search](conversation-search-v0.1.md)
- **페르소나·지시문:** 페르소나 20개까지 저장, 하나를 골라 모든 대화·기기에, 사용자 지시문 하나. → [companion-persona](companion-persona-v0.1.md), [user-instructions](user-instructions-v0.1.md)
- **Web·CLI:** React Web(대화, 설정, 휴지통, `/` 프롬프트, 초안 보존), CLI는 `himari proactive`·`himari memory`만. → [web-console](web-console-v0.1.md) §4·§7(hub 부분 제외)

## 운영

- 운영 서버: `rabbithole`. 서버는 loopback에 바인딩하고 TLS reverse proxy 뒤에 있다.
- 마지막 배포: **H-75**(v3 #230, `d190852`), 2026-11-24 23:25 KST. 스키마 v21.
- DB 스키마: **v21**. 대화 폴더는 `/srv/himari-v3/conversations`. MCP 서버는 `exa`(HTTP) 하나. `himari-runner`는 없다.
- 2026-11-23~24 배포 기록: [V3-P106](deployments/2026-11-23-v3-p106.md)(작업 에이전트 기능 걷어 냄, hub 퇴역), [V3-P107](deployments/2026-11-24-v3-p107.md)(먼저 말 걸기 간략화), [V3-P108](deployments/2026-11-24-v3-p108.md)(페르소나, v20), [V3-P109](deployments/2026-11-24-v3-p109.md)(서버 줄이기), [H-75](deployments/2026-11-24-v3-h75.md)(옛 추상 정리, v21).
- 운영 서버에 남긴 것: 옛 대화 폴더 `/home/coder/HIMARI/conversations`, 옛 릴리스와 DB `.bak` 파일. 되돌릴 일이 없으면 소유자가 지운다.

## 확인하지 않은 것

- **V3-P108 실제 모델 확인**은 시험 장면으로만 했다(2026-11-24, 운영 키, 모델 다섯, [설계 §9 결과](companion-persona-v0.1.md#9-검증)). 도중에 바꾼 페르소나, 기억과 다툼, 반려 끔, 파일 넘기기는 대체로 지켰다. 남은 문제는 Web 답이 긴 것, 페르소나가 없을 때 모델마다 말투가 다른 것, 드물게 옛 말투나 옛 별명이 섞이는 것이다. 아직 보지 않은 것: 도구를 모두 준 실제 대화, Dispatcher·Data를 거친 `present_file`, 반려 앱에서 듣는 말투.
- **Personas 화면**을 브라우저로 직접 보지 않았다(컴포넌트 시험 `Personas.test.tsx`만, ui_smoke·e2e 없음).
- **V3-P109 파일 Tool을 실제 모델로** 쓰지 않았다(대화에서 파일 만들기·읽기·`present_file`·업로드). 운영 서버에서 폴더 목록과 읽기만 확인했다.
- **V3-P107을 반려 앱으로** 보지 않았다: 잠든 동안 알림 시각이 되면 깰 때 말하는지, 같은 작업 30분 뒤 자유 wake가 열리는지.
- **Windows 앱 정리(#221)** 는 앱을 다시 빌드할 때 들어간다. 소유자 PC의 앱에 들어갔는지는 기록이 없다.

## 알려진 부채

- **Linux 전용 의존**([server-slim §7](server-slim-v0.1.md#7-남는-linux-의존과-다음-단계)): 파일 잠금(`fcntl`)과 소유자·권한 검사(`geteuid`, 0600/0700), `O_NOFOLLOW`·`O_DIRECTORY`, worker의 `resource.setrlimit`, 문서·이미지 worker의 Bubblewrap + Landlock sandbox.
- **옛 내용이 섞인 문서:** [contracts.md](contracts.md), [module-boundaries.md](module-boundaries.md), [prompt-composition.md](prompt-composition.md), [data-and-tools.md](data-and-tools.md), [data-contract-v0.2.md](data-contract-v0.2.md), [control-prompt-provider-v0.1.md](control-prompt-provider-v0.1.md) 같은 기준 문서에 Inside·`run_command`·`ask_user`·Pause와 Workspace·Binding·Artifact 이야기가 남아 있다(각 문서 머리에 주의를 달았다). 어긋나면 [decisions.md](decisions.md)와 [architecture.md](architecture.md)가 우선이다. 빠진 기능의 설계는 [archive/](archive/README.md)에 옮겼다.

## 다음 후보

아직 하지 않은 것만 적는다.

**캐릭터 로드맵**([character-architecture.md](character-architecture.md), H-76). 이 순서로 간다.

0. **기반:** 페르소나 첫 양식(세계 접점·대사 예시)과 페르소나 규칙 강화([companion-persona §14](companion-persona-v0.1.md#14-페르소나-규칙-강화-2026-11-25), 배포 전).
1. **일관성:** 페르소나별 삶 정사, 기억 정리가 HIMARI의 자기 주장을 정사로 모음.
2. **삶:** 이야기 작가(밤마다 일기·일정), 먼저 말 걸기의 새 계기.
3. **관계:** 관계 정사(호칭·약속·농담·처음), 기념일, 사건으로 넘어가는 관계 단계.
4. **상태와 몸:** 이어지는 기분, 하루 리듬, 이벤트 그림, 페르소나별 그림, 앱에서 페르소나 바꾸기.
5. **세계:** 날씨·뉴스·트렌드, 몇 주짜리 줄거리, 숨긴 뒷이야기.

**그 밖**(순서는 정하지 않았다):

- **로컬 구성:** 위 Linux 의존을 플랫폼 계층 하나로 모으고 Windows에서는 Job Object로 worker를 묶기([server-slim §7](server-slim-v0.1.md#7-남는-linux-의존과-다음-단계)).
- **작업 중에 온 메시지를 받아 두기**(지금은 `BUSY`), **기억의 임베딩 검색**, **로컬 파일**(`pc` Environment), **앱의 파일 첨부·붙여넣기, 음성, 자동 업데이트**([companion §12](companion-v0.1.md#12-다음-단계-방향-이-패킷에서-구현하지-않음)).
