# HIMARI v3 운영 배포: 캐릭터 로드맵 7·8단계(페르소나 항목, 설정 나누기), 스키마 v27 — 2026-11-26

2026-11-25T15:29Z(2026-11-26 00:29 KST), 운영 서버 `rabbithole`. 소유자가 "병합 배포해줘"라고 한 뒤 했다.

| 구성 | 이전 | 이번 |
| --- | --- | --- |
| v3 Web(`himari-v3`) | `5b45f7d5271eee0c79a100583e8a9bf3e57d34be` | `3db542ea48b291bec7b219dce238cfeb232783a7` |
| DB 스키마 | 26 | **27** |

들어간 것은 v3 #246이다.

- 대화 프롬프트 고침: `LIFE_RULE`이 "자유롭게, 다만 매 답마다는 아니고 사용자가 자기 이야기를 할 땐 그 이야기에"로 바뀌었다. 상한에 안 맞는 긴 항목 하나가 뒤의 항목을 막지 않는다. 글 전체가 보이는 최신 조각은 요약을 빼고 보낸다.
- **7단계 페르소나 항목**(H-82, [설계](../character-persona-fields-v0.1.md)): 14칸, 모델이 보는 글은 저장할 때 칸에서 만든다. 작가와 작업자는 `persona_makes`를 받는다. Web 편집은 칸으로 한다.
- **8단계 설정 나누기**(H-83, [설계](../character-settings-v0.1.md)): Web 설정이 HIMARI와 You and HIMARI로 나뉜다. Memory·Timeline은 없어지고, 일화는 Moments together에서 고친다.

스키마는 `personas`에 `fields`(JSON) 열 하나를 더하고, 글이 있는 페르소나를 `{"free": 글}`로 채운다.

## 한 일

- **1단계(빌드·검사, 서비스는 건드리지 않음):** 지난 배포와 같은 스크립트(`/tmp/himari-deploy-a-3db542e.sh`)를 썼다. 릴리스는 `/opt/himari-v3/releases/3db542ea48b2`, 가상환경은 `/opt/himari-v3/venvs/3db542ea48b2`이다(의존성은 직전 판과 같음).
- **리허설(`/tmp/himari-rehearse-v27.sh`):** 운영 DB를 읽기 전용으로 임시 폴더에 복사하고, 새 판의 `SqliteStore`로 열어 v26에서 v27로 올렸다.
  - 업그레이드 전후 수가 같다: 대화 32, 이벤트 1000, Tool 실행 265, 메모 25, 일화 32, 페르소나 2, Environment 32, wake 28. 지난 배포 때보다 대화가 적은 것은 그 사이 소유자가 지운 대화다.
  - 업그레이드 뒤: 스키마 27, `quick_check` ok, `foreign_key_check` 0, `personas.fields` 있음, 삶 항목 13개.
  - **페르소나 글이 전후로 같다**(모든 페르소나 글의 해시가 같음). 글 있는 페르소나 2개 모두 `fields.free`가 글과 같다.
- **2단계(전환, `/tmp/himari-deploy-b-3db542e.sh`):** 지난 배포 스크립트에서 스키마 기대값을 26에서 27로, 업그레이드 백업 줄을 v26으로 바꾸고, 새 열 확인을 더했다.
  - 미리 확인: 진행 중인 Run과 wake 0, 스키마 26.
  - 서비스 중단은 1초 안이었다(15:29:16 멈춤과 시작).
- 첫 시작에서 앱이 DB를 v27로 올렸다. 그 전에 백업 `/srv/himari-v3/himari.db.v26-20261125T152918395592Z.bak`을 만들었다.
- 설정은 바꾸지 않았다.

## 확인

| 확인 | 결과 |
| --- | --- |
| v3 전체 pytest (`coder`) | 통과: 3003 passed, 86 skipped (228초) |
| mypy / 모듈 의존성 / 렌더러 / wheel | 통과(279개 파일) / 통과 / 통과 / 화면·자산 2개·KaTeX |
| 실호스트 sandbox | 9 passed, 2 skipped |
| 준비 확인 | 통과: `himari-v3` active, `/` 200·nonce CSP, HTTPS 200, DB 판 27·`quick_check` ok, Environment 32(`machine` 26), 대화 32, 업그레이드 백업 줄, `reaching out` 줄 |
| 시작 로그 | 이야기 작가 켜짐, 날씨 꺼짐·화제 켜짐. 경고는 원래 있던 `inclusionai/ling-3.0-flash` 하나뿐이고 Traceback은 없다 |
| 배포 뒤 | 페르소나 1(993자)과 3(1,747자)의 `fields`가 `{"free": …}`. `himari proactive` 정상(오늘 0/10, 지금 기기 `d3`, 조용한 시간) |

검사 기록: `/opt/himari-v3/checks/3db542ea48b2`.

## 남은 것

- **써 보기는 소유자가 한다:**
  - 페르소나를 칸으로 옮겨 적기. 지금은 둘 다 자유 글 한 칸이다.
  - "하는 일과 만드는 것" 칸을 채운 뒤 작가가 고르는 프로젝트 확인.
  - 새 설정 화면(HIMARI, You and HIMARI)을 브라우저로 보기.
  - 자기 얘기를 끼워 넣는 빈도.
- 앱(Windows)은 다시 빌드하지 않아도 된다. 페르소나 메뉴는 이름만 읽는다.
- 옛 릴리스와 DB의 `.bak` 파일(v19~v26)은 지우지 않았다.

## 되돌리기

배포 백업은 `/opt/himari-v3/rollback/3db542ea48b2-20261125T152915Z`에 있다. **이전 판은 스키마 27을 거부하므로 DB도 v26으로 되돌려야 한다.** 되돌리면 전환 뒤 기록된 대화와 삶 기록, 페르소나 고침은 잃는다.

1. `sudo systemctl stop himari-v3`
2. 백업의 `himari-v3.service`를 `/etc/systemd/system/`에, `himari`를 `/usr/local/bin/`에 되돌린다.
3. `/srv/himari-v3/himari.db`·`-wal`·`-shm`을 지우고, 다음 가운데 하나를 제자리에 둔다(소유자 `himari-v3`, 0600):
   - 백업의 `db/` 아래 파일들
   - 업그레이드가 만든 `himari.db.v26-20261125T152918395592Z.bak`(이름을 `himari.db`로)
4. `sudo systemctl daemon-reload && sudo systemctl start himari-v3`
