# HIMARI v3 운영 배포: 페르소나 규칙 강화 — 2026-11-25

2026-11-24T15:33Z(2026-11-25 00:33 KST), 운영 서버 `rabbithole`. 소유자가 병합·배포 제안에 "다음 진행하자"고 한 뒤 했다.

| 구성 | 이전 | 이번 |
| --- | --- | --- |
| v3 Web(`himari-v3`) | `d190852517667016cd970ea08fe33de0958f3a9d` | `d98bbca298e7f76363f8b023669a6136a56dc903` |
| DB 스키마 | 21 | 21(변경 없음) |

들어간 것은 v3 #235다([설계 §14](../companion-persona-v0.1.md#14-페르소나-규칙-강화-2026-11-25), [캐릭터 로드맵](../character-architecture.md) 0단계).
- 시스템 글의 고정 문장: `TALKING_RULE`(새)과 `ABILITY_NOTE`(새), `PERSONA_RULE`·`COMPANION_BASE`·`ASSISTANT_BASE`·`EXPRESSION_RULE`·`SPEAKING_RULE` 고침. 순서와 절은 그대로다.
- Web의 첫 페르소나 양식에 "어디서 왔고 지금 어디에 있는지", "대사 예시", "습관" 칸.
- 같은 장면으로 돌린 페르소나 실제 모델 확인: 85 → 97 / 100(설계 §14).

## 한 일

- **1단계(빌드·검사, 서비스는 건드리지 않음):** 어제와 같은 스크립트(`/tmp/himari-deploy-a-d98bbca.sh`). 릴리스 `/opt/himari-v3/releases/d98bbca298e7`, 가상환경 `/opt/himari-v3/venvs/d98bbca298e7`(의존성은 직전 판과 같음).
- **2단계(전환, `/tmp/himari-deploy-b-d98bbca.sh`):** 어제 스크립트에서 v21 업그레이드 확인을 "스키마 21 그대로, 업그레이드 없음"으로 바꿨다.
  - 미리 확인: 진행 중인 Run과 wake 0, 스키마 21, 새 코드가 `web.env`를 읽음.
  - 백업: `himari-v3` 단위 파일, `/usr/local/bin/himari`, `deployment.json`, `DEPLOYED_SHA`, `web.env`, `mcp.json`, `/etc/himari-v3`(tar와 ACL), DB(v21, `quick_check` ok).
  - 서비스 중단은 1초 안(15:33:42 멈춤 → 같은 초에 시작).

## 확인

| 확인 | 결과 |
| --- | --- |
| v3 전체 pytest (`coder`) | 통과: 2800 passed, 82 skipped (196초) |
| mypy / 모듈 의존성 / 렌더러 / wheel | 통과(246개 파일) / 통과 / 통과 / 화면·자산 2개·KaTeX |
| 실호스트 sandbox (`HIMARI_SANDBOX_HOST=1 tests/sandbox_host`) | 9 passed, 2 skipped |
| 준비 확인 | 통과: `himari-v3` active, `/` 200·nonce CSP, HTTPS 200, DB 판 21·`quick_check` ok, `environments` 49(`machine` 39)·대화 49 그대로, 업그레이드 없음, `conversation folders` 줄, `reaching out` 줄, Traceback 없음 |
| 시작 로그 | 경고는 원래 있던 `inclusionai/ling-3.0-flash` 미선언 하나뿐 |
| 새 판의 내용 | 릴리스 가상환경에서 `TALKING_RULE`·`ABILITY_NOTE`를 읽음, 서빙하는 Web 번들에 새 양식("대사 예시")이 있음 |

검사 기록: `/opt/himari-v3/checks/d98bbca298e7`(각 검사의 로그와 JSON, `ready.json`, 배포 로그 둘).

## 하지 않은 것

- **소유자가 써 본다.** Web Settings → Personas에서 새 양식으로 페르소나를 하나 써 보고, Web과 반려 앱에서 말투·길이가 어떤지 본다. 이것이 0단계의 마지막 확인이다(로드맵 §10).

## 되돌리기

배포 백업은 `/opt/himari-v3/rollback/d98bbca298e7-20261124T153341Z`에 있다. 스키마가 같아 DB는 되돌리지 않는다.

1. `sudo systemctl stop himari-v3`
2. 백업의 `himari-v3.service`를 `/etc/systemd/system/`에, `himari`를 `/usr/local/bin/`에 되돌린다.
3. `sudo systemctl daemon-reload && sudo systemctl start himari-v3`
