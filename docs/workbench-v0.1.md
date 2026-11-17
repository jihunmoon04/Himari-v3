# HIMARI 작업 앱: 작업 콘솔 창과 hub 실행기 관리 (V3-P104 설계 초안 v0.1)

> 2026-11-18 · rev. 1 · **설계 초안** · 대상: v3 `cb56a53`, hub `f1f5119`
> 앞 문서: [작업 콘솔](web-console-v0.1.md)(V3-P102), [Web에서 hub 프로젝트](hub-projects-ui-v0.1.md)(V3-P103), [반려 1단계](companion-v0.1.md)(V3-P83), hub README "백그라운드 실행과 터널"

## 1. 목표와 범위

- **작업 콘솔을 앱으로:** v3 Web의 작업 콘솔(V3-P102)을 브라우저 탭이 아니라 기기의 앱 창으로 쓴다(WebView2). 브라우저로 여는 것도 그대로 된다.
- **hub 실행기를 앱에서 켜고 끈다:** 기본은 **앱을 켜면 실행기도 켜지고, 앱을 끄면 앱이 켠 실행기도 꺼진다**. 묶이지 않게 끌 수 있다.
- **반려 앱과는 따로(소유자 2026-11-18):** 반려 앱(V3-P83, 캐릭터·말 걸기·화면)은 다른 개념으로 두고, 나중에 가능하면 합친다. 그래서 이 앱은 합치기 쉬운 모양으로 만든다(§7).
- **범위 밖:** Windows 밖(macOS·Linux 데스크톱), 앱 설치 꾸러미와 자동 업데이트, hub 자체를 실행 파일 하나로 묶기, 실행기 확인 창(hub `local` 방식의 tkinter 창)을 이 앱으로 옮기기(나중).

## 2. 지금

- 반려 앱 `clients/windows/Himari.Companion`: .NET 10 WPF(+ WinForms 트레이), WebView2(답변 읽기 창, 데이터 폴더 `%LOCALAPPDATA%\HIMARI\reader`), 설정 `%LOCALAPPDATA%\HIMARI\window.json`, 기기 토큰은 Windows 자격 증명 관리자.
- hub 실행기(기기마다): `himari-hub runner start`(창 없는 백그라운드, 기록 `<hub 홈>\runner.log`), `runner stop`(`runner.stop` 파일을 두고 최대 30초 기다린 뒤 강제 종료), 한 기기에 하나(`runner.lock`), 떠 있는 실행기의 pid는 `runner.pid`. hub는 로그인 시 자동 시작을 2026-11-16에 빼고 "통합 기기 앱이 `runner start`를 부른다"로 남겼다(hub `f4e3b49`).
- hub 홈은 `HIMARI_HUB_HOME` 또는 `%USERPROFILE%\.himari-hub`. 이 PC(laptop)에서는 저장소 폴더에서 `uv run himari-hub …`로 돈다.

## 3. 앱 구성

새 프로젝트 `clients/windows/Himari.Workbench`(WinExe, `net10.0-windows10.0.17763.0`, WPF + WebView2, 반려 앱과 같은 판).

| 부분 | 하는 일 | 화면 의존 |
| --- | --- | --- |
| `RunnerSupervisor` | 실행기 켜기·끄기·상태 읽기, 앱이 켠 것 기억 | 없음(합칠 때 그대로 옮김) |
| `WorkbenchSettings` | `%LOCALAPPDATA%\HIMARI\workbench.json` 읽기·쓰기 | 없음 |
| `MainWindow` | 위쪽 상태 줄 + WebView2(작업 콘솔) | WPF |
| 트레이 | 열기, 실행기 켜기/끄기, 설정, 종료 | WinForms `NotifyIcon` |
| `SettingsWindow` | 서버 주소, hub 명령·홈, 연동 설정, 로그인 시 시작 | WPF |

- **하나만:** 같은 사용자에 앱은 하나(이름 붙은 Mutex `Local\HIMARI.Workbench`). 두 번째로 켜면 첫 앱의 창을 앞으로 가져오고 끝난다.
- **창을 닫으면 트레이로:** 창의 X는 숨김. 앱 종료는 트레이의 "종료"(또는 Windows 로그오프·종료). 처음 숨길 때 한 번 "트레이에서 계속 돕니다"를 알린다.

## 4. 작업 콘솔 창

- WebView2가 설정의 **서버 주소**(`https://…`, 반려 앱과 같은 서버)를 연다. 데이터 폴더는 `%LOCALAPPDATA%\HIMARI\workbench\webview`(반려 앱의 읽기 창과 나눔). 로그인은 지금의 운영자 로그인 그대로이고 쿠키는 이 데이터 폴더에만 남는다.
- **이동 제한:** 최상위 이동은 서버 주소와 같은 출처만. 다른 출처의 링크와 `window.open`은 기본 브라우저로 넘긴다(`NewWindowRequested`·`NavigationStarting`). 서버 주소가 `https`가 아니면 열지 않는다(loopback `http://127.0.0.1`·`localhost`만 예외, 개발용).
- **권한:** 카메라·마이크·위치·알림 등 WebView2 권한 요청은 모두 거절한다(작업 콘솔은 쓰지 않음). 파일 고르기(첨부)와 내려받기는 WebView2 기본 동작 그대로.
- **인증서 오류는 넘기지 않는다**(`ServerCertificateErrorDetected`를 허용하지 않음). 사설 인증서를 쓰면 Windows 인증서 저장소에 넣어야 한다고 설정에 적는다.
- 개발자 도구는 꺼 둔다(설정의 "개발자 모드"에서만 켬).

## 5. 실행기 관리

### 5.1 hub 명령과 홈

- **hub 명령:** 설정의 명령 줄(실행 파일 + 인자 목록). 기본은 `uv` + `["run", "--project", "<hub 폴더>", "himari-hub"]`이고, 처음 켤 때 hub 폴더를 고르게 한다. 실행 파일 하나로 묶인 hub가 생기면 그 경로만 적으면 된다.
- 명령은 셸을 거치지 않고 인자 목록으로 실행한다(`ProcessStartInfo.ArgumentList`, `UseShellExecute = false`, 창 없음). 사용자 입력을 명령 문자열로 이어 붙이지 않는다.
- **hub 홈:** 기본 `%USERPROFILE%\.himari-hub`, 설정에서 바꿀 수 있다. 바꾸면 실행할 때 `HIMARI_HUB_HOME`으로 넘긴다.
- 이 앱은 hub의 토큰(`runner.token` 등)을 읽지 않는다. 읽는 것은 상태 파일(§5.2)과 `runner.log`의 끝부분뿐이다.

### 5.2 상태

5초마다(창이 보이면 2초마다) 읽는다. 파이썬을 띄우지 않고 파일만 본다.

| 상태 | 판단 |
| --- | --- |
| 꺼짐 | `runner.pid`가 없거나, 그 pid의 프로세스가 없음 |
| 켜는 중 | 이 앱이 `runner start`를 불렀고 아직 pid가 없음(15초까지) |
| 떠 있음 · 연결됨 / 연결 안 됨 | pid가 살아 있고, 상태 파일(H9)의 `connected` |
| 끄는 중 | 이 앱이 `runner stop`을 불렀고 아직 pid가 살아 있음 |
| 오류 | `runner start`·`stop`이 0이 아닌 코드로 끝남(그 출력의 마지막 줄을 보임) |

- **pid 확인:** pid만 보면 다른 프로세스가 같은 번호를 받았을 때 속는다. 그래서 `runner.pid`의 프로세스가 살아 있고, 그 시작 시각이 `runner.pid` 파일의 수정 시각보다 앞서지 않는지 본다(hub `background.running`과 같은 뜻). 확신할 수 없으면 "알 수 없음"으로 보이고 끄기 단추는 `runner stop`에 맡긴다.
- **H9(hub에 더함):** 실행기가 `<hub 홈>\runner.state.json`을 쓴다: `{"pid", "server", "name", "connected", "since", "error"}`. 연결될 때·끊길 때·끝날 때 원자적으로(`.tmp` → `replace`) 쓴다. 토큰과 경로는 넣지 않는다. 없는 hub에서는 "떠 있음(연결 여부 모름)"으로 보인다.

### 5.3 켜기·끄기와 "앱이 켠 것"

- **켜기:** `<hub 명령> runner start`. 이미 떠 있으면 부르지 않는다(hub도 거절함). 끝나면 출력의 pid를 `workbench.json`의 `started_pid`와 `started_at`(그 프로세스의 시작 시각)으로 적는다. 앱이 죽었다 다시 켜져도 "앱이 켠 실행기"를 안다.
- **끄기(사용자가 누름):** `<hub 명령> runner stop`. 누가 켰든 끈다. 끝나면 `started_pid`를 지운다.
- **연동(기본 켬, 설정 "앱과 함께 실행기 켜고 끄기"):**
  - 앱이 켜질 때: 실행기가 꺼져 있으면 켠다. 떠 있으면 그대로 두고, 앱이 켠 것으로 치지 않는다(`started_pid`가 그 pid가 아니면).
  - 앱이 끝날 때(트레이 종료, 로그오프·종료): 떠 있는 실행기가 **앱이 켠 그 실행기**(pid와 시작 시각이 `started_pid`·`started_at`과 같음)일 때만 끈다. 터미널에서 따로 켠 실행기는 끄지 않는다.
  - 연동을 끄면 앱은 켜고 끄기를 사람이 누를 때만 한다.
- **로그오프·Windows 종료:** `SessionEnding`에서 앱이 켠 실행기에 `runner stop`을 부른다. `runner stop`은 최대 30초를 기다리지만 Windows가 그 전에 끝낼 수 있다. 그래도 실행기의 진행 중인 작업은 hub가 실패로 정리하고(실행기 연결이 끊김), 프로젝트 요청은 `lost`로 남아 사용자가 정리한다(hub 설계 §4.6). 앱은 이것을 막으려 하지 않는다.
- **로그인 시 시작:** 설정 "Windows에 로그인하면 앱 시작"(기본 끔). `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`의 값 하나(`HIMARI Workbench`). 켜 두면 연동에 따라 실행기도 켜진다(hub가 로그인 시작을 이 앱에 맡긴 자리).

### 5.4 보이는 곳

- **상태 줄**(창 위쪽, 한 줄): "실행기: 연결됨 · laptop · rabbithole" + 켜기/끄기 + "기록"(`runner.log`를 기본 편집기로 열기) + 설정. 오류면 빨간 글과 마지막 줄.
- **트레이:** 아이콘 툴팁에 실행기 상태. 메뉴: 작업 콘솔 열기, 실행기 켜기/끄기, 설정, 종료. 상태가 "연결됨"에서 다른 것으로 바뀌면 한 번 알림(5분에 한 번까지).
- 작업 콘솔(Web)은 지금처럼 hub에서 본 기기 연결(`runner_connected`)을 보인다. 이 앱은 Web에 실행기 상태를 알리지 않는다(Web은 hub에서 본다).

## 6. 보안

- 앱은 hub의 토큰과 v3의 운영자 비밀을 다루지 않는다. 작업 콘솔의 로그인은 WebView2 쿠키로만 남는다(브라우저와 같음).
- hub 명령은 사용자가 이 기기에서 적은 것만 실행한다. 원격(Web, 서버)에서 이 앱에 명령을 보내는 길은 없다: 앱은 서버에 아무것도 열어 두지 않고, Web 페이지에 앱의 기능을 넘기지 않는다(WebView2 `AddHostObjectToScript`·`postMessage` 다리를 두지 않음).
- `workbench.json`에는 서버 주소, hub 명령, hub 홈, 설정, `started_pid`·`started_at`만 둔다.

## 7. 반려 앱과 합칠 때를 위한 경계

- **같은 기술, 같은 자리:** 반려 앱과 같은 .NET·WPF·WebView2 판, 같은 `clients/windows` 아래. 합치면 `RunnerSupervisor`·`WorkbenchSettings`를 반려 앱으로 옮기고 작업 콘솔 창을 메뉴 하나로 붙인다.
- **실행기는 한 앱만:** 반려 앱에는 실행기 관리를 넣지 않는다(기기에 실행기는 하나: 두 앱이 켜고 끄면 꼬인다). 합칠 때까지 이 앱만 한다.
- **설정은 따로:** `workbench.json`과 `window.json`을 섞지 않는다. 합칠 때 옮기는 코드가 하나 생긴다.
- **트레이 아이콘 둘:** 두 앱을 함께 켜면 아이콘이 둘이다. 합칠 때까지 받아들인다.
- **WebView2 데이터 폴더는 따로**(`workbench\webview`, `reader`): 합쳐도 그대로 둘 수 있다.

## 8. hub에 필요한 것

| id | 무엇 | 크기 |
| --- | --- | --- |
| H9 | 실행기가 `<hub 홈>\runner.state.json`(§5.2)을 씀 | 작음 |
| H10 | `runner start`가 성공하면 표준 출력의 마지막 줄에 `pid <n>`를 기계가 읽을 수 있게(지금 문구 "실행기를 백그라운드로 띄웠습니다 (pid N)"에서 뽑을 수도 있지만 문구는 계약이 아님) | 작음 |

## 9. 검사

- `RunnerSupervisor` 단위 시험(가짜 프로세스 실행기와 임시 hub 홈): 상태 판단 표(§5.2) 전부, pid 재사용(시작 시각이 늦음), 켜기 중 앱 종료, "앱이 켠 것"만 끄기, 다시 켜진 앱이 `started_pid`로 앎, `runner start`·`stop` 실패의 오류 표시, 인자 목록이 셸 없이 넘어감.
- `WorkbenchSettings`: 없는 파일, 깨진 파일(기본값으로, 덮어쓰지 않고 알림), 원자적 쓰기.
- 창(실기): 같은 출처만 이동, 다른 출처는 브라우저로, 권한 거절, 인증서 오류에서 열지 않음, 두 번째 실행이 첫 창을 앞으로.
- 실기(이 PC): 앱을 켜면 실행기가 켜지고 Web에서 laptop이 연결됨으로 보임 → 앱을 끄면 꺼짐 → 터미널에서 켠 실행기는 앱을 꺼도 남음 → 로그오프.

## 10. 단계

| 단계 | 내용 |
| --- | --- |
| B1 | hub H9·H10 |
| B2 | `RunnerSupervisor`, `WorkbenchSettings`와 단위 시험 |
| B3 | 창(WebView2, 이동 제한), 상태 줄, 트레이, 설정, 하나만 |
| B4 | 연동, 로그오프, 로그인 시 시작, 실기 확인 |

V3-P103(Web 화면)과 서로 기다리지 않는다.

## 11. 열린 질문

1. 앱 이름: "HIMARI 작업 앱"(`Himari.Workbench`)으로 둘지.
2. hub 명령의 기본값: `uv run --project <hub 폴더> himari-hub`(지금 이 PC의 방식, 권고) 말고 다른 설치 방식을 먼저 지원할지.
3. 창을 닫으면 트레이로(권고) 대신 앱을 끝낼지.
