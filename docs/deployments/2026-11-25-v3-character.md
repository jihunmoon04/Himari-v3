# HIMARI v3 운영 배포: 캐릭터 로드맵 1~5단계, 스키마 v25 — 2026-11-25

2026-11-24T23:24Z(2026-11-25 08:24 KST), 운영 서버 `rabbithole`. 소유자가 "배포해줘"라고 한 뒤 했다.

| 구성 | 이전 | 이번 |
| --- | --- | --- |
| v3 Web(`himari-v3`) | `d98bbca298e7f76363f8b023669a6136a56dc903` | `498968830e011498bd3cd6c883ce7743433253af` |
| DB 스키마 | 21 | **25** |

들어간 것은 캐릭터 로드맵([character-architecture.md](../character-architecture.md)) 1~5단계다.

| PR | 단계 | 결정 | 스키마 |
| --- | --- | --- | --- |
| #238 | 1·2 삶(정사·일기·일정, 이야기 작가, 기억 정리의 `self`) | H-77 | v22 `life_entries`·`life_ids`·`life_writes` |
| #239 | 3 관계(관계 정사, 함께한 날, 관계 단계, 기념일 wake) | H-78 | v23 `bond_stages`, `life_entries.tag`·`month_day` |
| #240 | 4 상태(이어지는 기분, 하루 리듬, 앱의 페르소나 메뉴) | H-79 | v24 `himari_moods`, `life_entries.time_of_day` |
| #241 | 5 세계(날씨, 화제, 줄거리, 숨긴 뒷이야기) | H-80 | v25 `life_entries.title`·`end_day`·`ending`·`arc_id` |

그 앞의 설계 문서 PR #237도 함께 들어갔다. 스키마는 새 표와 열을 더하기만 한다.

## 한 일

- **1단계(빌드·검사, 서비스는 건드리지 않음):** 지난 배포와 같은 스크립트(`/tmp/himari-deploy-a-4989688.sh`). 릴리스 `/opt/himari-v3/releases/498968830e01`, 가상환경 `/opt/himari-v3/venvs/498968830e01`(의존성은 직전 판과 같음).
- **리허설(`/tmp/himari-rehearse-v25.sh`):** 운영 DB를 읽기 전용으로 임시 폴더에 복사하고 새 판의 `SqliteStore`로 열어 v21에서 v25로 올렸다. 운영 DB는 읽기만 했다.
  - 전과 뒤가 같음: 대화 49, 이벤트 1,072, Tool 실행 315, 메모 23, 일화 29, 페르소나 2, Environment 49, wake 23.
  - 뒤: 스키마 25, `quick_check` ok, `foreign_key_check` 0, 새 표 다섯(`bond_stages`·`himari_moods`·`life_entries`·`life_ids`·`life_writes`)과 `life_entries`의 새 열 일곱.
- **2단계(전환, `/tmp/himari-deploy-b-4989688.sh`):** 지난 배포의 스크립트에서 스키마 기대값을 21 → 25로 바꾸고, 업그레이드 백업 줄·새 표·이야기 작가가 켜졌는지를 준비 확인에 더했다. 준비가 안 되면 스스로 되돌린다.
  - 미리 확인: 진행 중인 Run과 wake 0, 스키마 21.
  - 백업: `himari-v3` 단위 파일, `/usr/local/bin/himari`, `deployment.json`, `DEPLOYED_SHA`, `web.env`, `mcp.json`, `/etc/himari-v3`(tar와 ACL), DB(v21, `quick_check` ok).
  - 서비스 중단은 약 1초(23:24:53 멈춤 → 23:24:54 시작).
- 첫 시작에서 앱이 DB를 v25로 올렸다. 그 전에 백업 `/srv/himari-v3/himari.db.v21-20261124T232456219345Z.bak`을 만들었다.
- 설정은 바꾸지 않았다. `HIMARI_LOCATION`을 넣지 않아 날씨는 꺼져 있다.

## 확인

| 확인 | 결과 |
| --- | --- |
| v3 전체 pytest (`coder`) | 통과: 2959 passed, 85 skipped (218초) |
| mypy / 모듈 의존성 / 렌더러 / wheel | 통과(275개 파일) / 통과 / 통과 / 화면·자산 2개·KaTeX |
| 실호스트 sandbox | 9 passed, 2 skipped |
| 준비 확인 | 통과: `himari-v3` active, `/` 200·nonce CSP, HTTPS 200, DB 판 25·`quick_check` ok, Environment 49(`machine` 39), 대화 49, 새 표 다섯, 업그레이드 백업 줄, 대화 폴더 줄, `reaching out` 줄, Traceback 없음 |
| 시작 로그 | `HIMARI's life on, story writer on (deepseek/deepseek-v4.1-flash-1017, at 22:00, 200,000 tokens a day each)`, `weather off (no HIMARI_LOCATION), the day's topics on (exa/web_search_exa)`. 경고는 원래 있던 `inclusionai/ling-3.0-flash` 미선언 하나뿐 |
| 배포 뒤 | `himari proactive`(CLI → 서버) 정상 응답 |
| 이야기 작가 첫 호출 | 23:35Z(시작 뒤 첫 10분 주기). 2026-11-24 일기를 지금 페르소나(1번)로 썼다: `applied`, 2,848 토큰. 일기 1·일정 1(11-26 21:00)·정사 1·줄거리 1이 생겼다. 화제 다섯이 `exa`에서 왔으나 "이 시각 헤드라인" 같은 뉴스 모음 제목이라 일기에 쓰이지 않았다. 그날 일화 1개가 들어가 일기가 그 대화를 주로 다뤘다 |

검사 기록: `/opt/himari-v3/checks/498968830e01`(각 검사의 로그와 JSON, `ready.json`, 배포 로그 둘).

## 남은 것

- **날씨:** `/etc/himari-v3/web.env`에 `HIMARI_LOCATION=위도,경도`를 넣고 `himari-v3`를 다시 시작하면 켜진다.
- **앱:** 페르소나 메뉴와 시작 표정(H-79)은 Windows 앱을 다시 빌드해 설치해야 들어간다. 서버는 이미 받을 준비가 되어 있다.
- **써 보기는 소유자가 한다:** Life 화면(정사·일기·일정·줄거리·기분 줄)과 Bond 섹션을 브라우저로 보기, 대화에서 자기 이야기를 꺼내는 빈도, 관계 정사가 쌓이는 모양, 화제가 일기에 녹는 모양.
- 옛 릴리스와 DB의 `.bak` 파일(v19, v20, v21)은 지우지 않았다.

## 되돌리기

배포 백업은 `/opt/himari-v3/rollback/498968830e01-20261124T232453Z`에 있다. **이전 판은 스키마 25를 거부하므로 DB도 v21로 되돌려야 한다.** 되돌리면 전환 뒤 기록된 대화와 삶·관계 기록은 잃는다.

1. `sudo systemctl stop himari-v3`
2. 백업의 `himari-v3.service`를 `/etc/systemd/system/`에, `himari`를 `/usr/local/bin/`에 되돌린다.
3. `/srv/himari-v3/himari.db`·`-wal`·`-shm`을 지우고 다음 가운데 하나를 제자리에 둔다(소유자 `himari-v3`, 0600):
   - 백업의 `db/` 아래 파일들
   - 업그레이드가 만든 `himari.db.v21-20261124T232456219345Z.bak`(이름을 `himari.db`로)
4. `sudo systemctl daemon-reload && sudo systemctl start himari-v3`
