# 세계: 날씨·화제·줄거리·숨긴 뒷이야기 v0.1 (캐릭터 로드맵 5단계, 2026-11-25)

[캐릭터 로드맵](character-architecture.md) 5단계. [삶](character-life-v0.1.md)·[관계](character-bond-v0.1.md)·[상태](character-state-v0.1.md) 위에 만든다. 소유자 방침(2026-11-25)에 따라 짧게 정하고 써 보며 고친다.

## 1. 무엇을 만드나

- **날씨:** 이야기 작가와 runtime state가 오늘 날씨를 안다.
- **화제:** 작가가 그날의 화제 몇 개(제목만)를 보고, 어울리면 일기에 하나쯤 녹인다.
- **줄거리:** 몇 주 이어지는 흐름("요즘 그림 연습 중")을 작가가 시작하고, 이어 가고, 끝낸다.
- **숨긴 뒷이야기:** 페르소나 글의 일부를 관계 단계가 될 때까지 모델에게 보이지 않는다.

## 2. 날씨

- 위치는 설정 `HIMARI_LOCATION`(`위도,경도`, 없으면 날씨 없음). Open-Meteo(키 없음)에서 오늘의 날씨 요약(상태, 최저·최고 기온, 비·눈)을 하루 몇 번 읽어 메모리에 둔다. 실패하면 그냥 없다.
- 작가 입력과 runtime state `world.weather`(한 줄)에 넣는다. HIMARI의 세계가 다른 곳이어도(페르소나의 세계 설정) 이것은 **사용자 쪽 날씨**라고 적는다.

## 3. 화제

- MCP 서버 중 검색 도구가 있는 것(지금 `exa`)이 설정에 있으면, 작가가 돌기 전에 하루 한 번 고정 질의("오늘 한국에서 화제인 소식")로 검색해 **제목 5개까지**만 작가 입력에 넣는다. 도구가 없거나 실패하면 빠진다. `HIMARI_LIFE_TOPICS=0`으로 끈다.
- 작가 규칙: 어울리면 하나만 일기에 녹인다. 정치·사건 사고는 의견 없이 스쳐 지나가거나 쓰지 않는다. 화제를 사실로 단정해 자세히 지어내지 않는다.
- 대화 모델에는 화제를 따로 넣지 않는다(일기에 녹은 것만 보인다).

## 4. 줄거리

- 삶 항목에 종류 `arc`: 제목, 목표 한 줄, 상태(`open`·`closed`), 시작·끝 날짜. 페르소나마다 열린 것 2개까지.
- 작가 출력에 `arc`: 시작(`start`), 진행(`advance`: 그날 일기가 그 줄거리를 이어 감), 끝(`close`: 결말 한 줄). 하루에 하나만 움직인다.
- runtime state `himari_life.arcs`: 열린 줄거리의 제목과 목표.
- 작가에게: 줄거리는 몇 주에 걸쳐 천천히, 날마다 꼭 다루지 않는다.

## 5. 숨긴 뒷이야기

- 페르소나 글에서 `[familiar]` 또는 `[close]`로 시작하는 줄은 관계 단계가 그만큼 되기 전에는 모델에게 보내지 않는다. 단계가 되면 다음 턴부터 들어간다(캐시가 한 번 바뀜).
- Web 페르소나 첫 양식에 한 줄 안내를 더한다: "숨긴 이야기: 줄 앞에 [familiar] 또는 [close]를 붙이면 그만큼 가까워진 뒤에 드러납니다".
- 표시가 붙은 줄은 Web에는 그대로 보인다(주인의 글이니까).

## 6. 하지 않는 것(v0.1)

- 화제를 대화 중에 직접 검색해 주는 기능(지금의 MCP 검색 도구로 이미 된다).
- 날씨 예보로 먼저 말 걸기.
- 줄거리를 사용자가 Web에서 직접 시작하기(고치기·지우기는 Life 화면에서 된다).

## 7. 정한 것(권고안대로, 써 보며 바꿈)

| # | 정한 것 |
| --- | --- |
| W1 | 날씨는 Open-Meteo, 위치는 설정 하나. 없으면 날씨도 없다 |
| W2 | 화제는 검색 도구가 있는 MCP 서버로 하루 한 번, 제목 5개, 작가에게만 |
| W3 | 줄거리는 삶 항목의 `arc`, 열린 것 2개까지, 하루에 하나만 움직임 |
| W4 | 숨긴 뒷이야기는 페르소나 글의 줄머리 표시 `[familiar]`·`[close]` |

## 8. 구현에서 정한 것 (2026-11-25)

설계대로 만들었고 2026-11-25 배포했다(H-80, [기록](deployments/2026-11-25-v3-character.md)). 아래는 만든 것과, 설계가 비워 둔 곳에서 기존 방식에 맞춰 정한 것이다(소유자 방침: 묻지 않고 권고안으로 정하고, 써 보며 고친다).

### 8.1 만든 것

| 부분 | 자리 |
| --- | --- |
| 세계 패키지 | 새 패키지 `himari.world`(층 1, `common`만 씀, `scripts/check_imports.py`). `world/api.py`: `WeatherDay`(한 줄 `line()`), Port `WeatherSource`·`TopicSource`, `TOPICS_QUERY`, `MAX_TOPICS`(5), `parse_location`. `world/weather.py`: `OpenMeteoWeather`(httpx, 메모리에 둠). `world/topics.py`: `SearchTopics`(날짜마다 한 번), `topic_titles` |
| 계약 | `data/api.py`: `LifeKind.ARC`, `LifeEntry.title`·`end_day`·`ending`·`arc_id`와 `arc_open`, `ArcOp`(`start`·`advance`·`close`), `ArcMove`, `LifePlan.arc`, `MAX_OPEN_ARCS`(2), `MAX_ARC_TITLE_CHARS`(60), `MAX_ARC_CHARS`(200), `LifeStore.edit_life(..., title=)` |
| 저장 | 스키마 v25: `life_entries`에 `title`·`end_day`·`ending`·`arc_id` 열. `data/sqlite.py`와 `data/memory.py` 둘 다, 공통 규칙(`check_arc_title`, `check_arc_move`, `arc_starts`, `arc_moved`, `edited_title`, 줄거리를 넣은 `order_life`)은 `data/rules.py`. 줄거리 움직임은 `apply_life_write`가 같은 트랜잭션에서 한다 |
| 이야기 작가 | `life/writer.py`: `WRITER_INSTRUCTIONS`에 날씨·화제·줄거리 규칙과 `arc` 칸, 입력에 `weather`·`topics`·`arcs`, `answer_arc`. `life/worker.py`: 쓰는 날의 날씨(메모리에서), 그날 화제(부르기 직전에만), 관계 단계로 거른 페르소나 |
| 프롬프트 | `prompt/api.py`: `persona_for_stage`(W4), `WORLD_ABOUT`·`world_data`, `himari_life`의 `arcs`(`LIFE_ARCS_CHARS` 500), `LIFE_RULE`에 "(arcs)" 한 구절, `CurrentStateProjection.weather`·`PromptInputs.weather`. `prompt/system.py`: 원칙 문단의 정보 목록에 `world` |
| Control | `driver._read_user_and_persona`: 페르소나 글을 저장된 관계 단계(`BondStore.bond_stage`)로 거르고, 삶과 함께 오늘 날씨 한 줄을 메모리에서 읽는다 |
| 서버 | `bootstrap/world.py`(`search_tool`, `topic_search`, `WEATHER_SECONDS`), `Application.open(location=, life_topics=)`, 주기 작업 `weather`(3시간), 시작 로그 `weather …, the day's topics …`, `mcp/tools.McpTool`의 `name`·`client`. 설정 `HIMARI_LOCATION`, `HIMARI_LIFE_TOPICS`(`interfaces/web/config.py`). `/api/life`에 `world`(`weather`·`today`·`topics`)와 줄거리의 `title`·`status`·`end_day`·`ending`, 일기의 `arc_id`. `PUT /api/life/{id}`가 `title`을 받는다 |
| Web | Settings → Life에 Arcs 묶음(제목, 목표, 열림·닫힘과 기간, 결말, 제목·목표 고치기, 지우기)과 날씨·화제 한 줄. Personas 첫 양식 끝에 숨긴 이야기 안내 한 줄 |
| 시험 | `tests/world/`(날씨·화제, 가짜 transport와 가짜 검색), `tests/data/test_arcs.py`(두 저장소), `tests/data/test_sqlite.py`(v24 → v25, `V25_UNDO`·`_ADDED_IN[25]`), `tests/life/`(입력·`arc`·숨긴 줄·화제를 한 번), `tests/prompt/test_world_prompt.py`, `tests/bootstrap/test_world_app.py`(숨긴 줄이 다음 턴부터, 날씨가 턴의 state에, 가짜 MCP 서버의 검색 Tool로 화제), `tests/interfaces/test_web_life.py`·`test_web.py`(설정), `Life.test.tsx`·`Personas.test.tsx`, 실제 모델 확인 `tests/live/test_life_live.py` |

### 8.2 정한 것

1. **새 패키지 `himari.world`.** Control도 날씨를 읽어야 해서 삶(`himari.life`, Control 옆 층) 안에 둘 수 없다. 그래서 `common`만 쓰는 아래 층 패키지로 두고 Control과 작가가 그 api로 읽는다. MCP 검색 Tool을 화제 검색으로 잇는 일은 조립하는 곳(`bootstrap/world.py`)이 한다.
2. **날씨를 읽는 방법(W1).** Open-Meteo `daily`(날씨 코드, 최저·최고 기온, 비, 눈)를 서버 시간대(`HIMARI_TIME_ZONE`)로 어제·오늘·내일 한 번에 읽는다(`past_days=1`, `forecast_days=2`). 작가가 아침에 어제를 써도 그날 날씨가 있다. 주기 작업 `weather`가 시작할 때와 3시간마다 읽는다. 요청 10초, 응답 64KB까지. 실패하면 앞서 읽은 것을 그대로 두고, 그 날짜가 빠지면 날씨가 없다. 턴과 작가는 메모리만 읽으므로 네트워크를 기다리지 않는다.
3. **날씨 한 줄**은 다른 runtime state처럼 영어다: `light rain, 3 to 9°C, rain 2.1 mm`(기온은 반올림, 비 0.1mm·눈 0.1cm 미만은 뺌). 날짜는 넣지 않는다(턴에는 오늘, 작가에게는 쓰는 날의 것).
4. **날씨는 삶과 함께만.** 반려와 삶이 켜지고 `HIMARI_LOCATION`이 있을 때만 읽고 알린다. runtime state `world`는 `{"about": WORLD_ABOUT, "weather": 한 줄}`이고, about에 "사용자 쪽 현실의 날씨이며 페르소나의 세계는 다를 수 있다"를 적었다. 반려 절에 새 규칙은 넣지 않았다(about과 원칙 문단만). 원칙 문단의 정보 목록에는 위치가 없어도 삶이 켜지면 `the weather on the user's side (world)`가 늘 들어간다(위치 설정 하나로 시스템 글이 갈리지 않게).
5. **검색 Tool 찾기(W2).** `mcp.json` 순서대로, 시작 때 연결된 서버가 등록한 Tool 중 서버 쪽 이름에 `search`가 들어가고 입력에 글자 `query`가 있는 첫 것을 쓴다(지금은 `exa/web_search_exa`). 입력에 `numResults`(또는 `num_results`·`count`·`limit`)가 있으면 10을 준다. 대화의 켬/끔 기본값(`default`)과 상관없이 쓴다(그 값은 대화 모델에 Tool을 보일지다). 대화 밖이라 대화별 세션이 아닌 공유 세션으로 부른다. 결과는 대화 모델이 보는 글로 바꿔 읽는다. 서버가 오류로 표시한 결과는 실패다.
6. **제목 읽기.** JSON `results[].title`, 없으면 `Title:` 줄, 없으면 Markdown 제목, 없으면 Markdown 링크 글. 5개까지, 공백을 접고 120자에서 자르며, 같은 제목은 한 번.
7. **하루 한 번.** 쓰는 날짜마다 한 번만 검색하고 그 결과를 모든 사용자가 쓴다. 성공이든 실패든 그 날짜에는 다시 하지 않는다(실패하면 그날 화제 없음). 작가가 실제로 부를 때만 검색한다(그날 일기가 있거나 세 번 다 했으면 검색하지 않음. 예산을 넘어 미루는 날은 검색이 먼저라 이미 했다). 30초 안에 답이 없으면 없음이다. 이야기 작가가 없으면(기억 작업 모델이 없는 등) 화제도 없다.
8. **화제 규칙(작가).** 어울릴 때만 하나까지 일기에 녹인다. 정치·범죄·사고·재난은 의견 없이 스치거나 쓰지 않는다. 제목에 없는 자세한 것을 사실처럼 쓰지 않는다. 대화 모델에는 넣지 않는다(일기에 녹은 것만 보인다).
9. **줄거리 저장(W3).** 삶 항목의 `arc` 종류다. 글(`text`)이 목표, 날짜(`day`)가 시작일이고 `title`, 닫으면 `end_day`와 `ending`이 붙는다. 상태는 따로 저장하지 않고 `end_day`가 있으면 닫힌 것으로 본다(Web에는 `status`로 보인다). "진행"은 그날 일기의 `arc_id`로 남긴다. 시작과 끝내기도 그날 일기에 잇는다. 진행은 줄거리 항목을 바꾸지 않는다(리비전 그대로). 끝내기는 리비전을 올린다.
10. **작가 답의 `arc`.** `{"move": "start", "title", "goal"}`, `{"move": "advance", "id"}`, `{"move": "close", "id", "ending"}` 셋 중 하나. `id`는 입력의 `arcs[].id`(항목 번호)이고 숫자 글(`"5"`)도 받는다. 제목 60자, 목표·결말 200자이고 공백은 한 칸으로 접어 한 줄로 만든다. 모양이 틀리면 그 칸만 버린다. 저장소가 할 수 없는 움직임(열린 것이 이미 둘인데 시작, 열린 것과 같은 제목으로 시작, 닫혔거나 없거나 다른 페르소나의 것이거나 지운 줄거리를 움직임)도 그것만 빠지고 일기는 쓴다. 옛 모양 답(`arc` 없음)은 그대로 통과한다. 그날 일기를 사용자가 고쳐 두었으면 일기를 건드리지 않으므로 이음은 남지 않는다(시작·끝내기는 된다).
11. **작가 입력 `arcs`.** 열린 줄거리마다 번호, 제목, 목표, 시작일, 움직인 날(작가가 읽는 14일 안의 최근 5개). 닫힌 것은 보여 주지 않는다(남길 것은 작가가 정사로 남길 수 있다). 작가에게 "몇 주에 걸쳐 천천히, 날마다 다루지 않는다, 대부분의 날은 움직이지 않는다"를 적었다.
12. **runtime state `himari_life.arcs`.** 열린 것의 제목과 목표만(500자 안). 줄거리만 있어도 `himari_life`가 생긴다. `LIFE_RULE`의 삶 목록에 "what runs through your weeks (arcs)"를 더했다.
13. **Web에서 고치기.** 줄거리는 제목과 목표만 고친다(결말과 열림·닫힘은 고치지 않는다). 고치면 사용자의 것(`user`)이 되지만 작가는 그 줄거리를 계속 잇고 끝낼 수 있다(글은 사용자의 것 그대로, 끝낼 때 끝 날짜와 결말만 붙인다). 제목은 줄거리만 가진다(다른 항목에 주면 422 `invalid_life`). 지우기는 다른 항목과 같다. Life 화면은 지금처럼 30일 전부터 보이므로, 열린 줄거리는 늘, 닫힌 것은 30일 안에 시작했거나 끝난 것이 보인다.
14. **숨긴 줄의 표시(W4).** 줄 맨 앞(들여쓰기와 `-`·`*`·`•` 목록 표시 뒤도 됨)의 `[familiar]`·`[close]`(대소문자 무관). 단계가 되면 표시를 떼고 넣는다. 줄 가운데의 표시는 그냥 글이다. 표시가 없는 글은 그대로 가므로 지금 페르소나들의 캐시는 바뀌지 않는다. 여러 줄 단락은 줄마다 붙여야 한다.
15. **어느 단계로 거르나.** 턴 시작에 저장된 단계(`bond_stage`, 읽어도 움직이지 않음)를 읽는다. 같은 턴의 관계 읽기(`read_bond`)가 단계를 올리면 그 다음 턴부터 드러난다(설계의 "다음 턴부터"). 삶이 꺼져 있어도 저장된 단계로 거른다(그때는 단계가 저절로 움직이지 않지만 Web에서 정한 단계는 따른다).
16. **이야기 작가도 거른다.** 작가도 같은 단계로 거른 페르소나를 받는다. 숨긴 이야기가 일기를 거쳐 먼저 새지 않게 하려는 것이다. 기억 정리는 페르소나 글을 읽지 않으므로 그대로다.
17. **시스템 글.** Web·앱·CLI에서 같은 단계면 같다(시험 있음). 단계가 바뀌면 페르소나가 든 시스템 글(캐시 앞부분)이 한 번 바뀐다(설계가 받아들임).
18. **첫 양식 안내**는 설계 문구 그대로 양식의 마지막 줄이다. 표시로 시작하지 않는 줄이라, 지우지 않으면 다른 안내 줄처럼 모델에 그대로 간다.
19. **보이는 곳.** 시작 로그(`HIMARI web: weather on (Open-Meteo at 37.57,126.98), the day's topics on (exa/web_search_exa)` 같은 한 줄), `/api/life`의 `world`, Life 화면의 "Weather: … · The day's topics: …" 한 줄. 꺼진 까닭도 같은 자리에 나온다(`off (no HIMARI_LOCATION)`, `off (HIMARI_LIFE_TOPICS=0)`, `off (no MCP search Tool)`, `off (no story writer)`).

### 8.3 시험

- 전체 pytest(WSL): **2959 passed, 85 skipped**(4분 21초. 상태 때보다 38개 늘었다). `mypy --strict` 문제 없음(275 파일). `scripts/check_imports.py` OK.
- Web: `npx vitest run` 7파일 35개 통과(Life 1개 더함, Personas 첫 양식 확인 한 줄), `npm run typecheck`, `npm run lint` 통과.
- 보통 시험에는 네트워크가 없다. 날씨는 httpx 가짜 transport, 화제는 가짜 검색과 가짜 MCP 서버(`tests/mcp_fake.py`)로 본다.

### 8.4 실제 확인 (2026-11-25)

- **Open-Meteo:** WSL에서 날씨 클라이언트로 서울(37.57,126.98)을 한 번 읽었다(키 없음). 2026-11-24~26 세 날이 왔고 오늘 줄은 `partly cloudy, -4 to 6°C`.
- **이야기 작가:** `tests/live/test_life_live.py -k story_writer`(`HIMARI_LIVE=1`)를 운영 키로 한 번 돌렸다. 모델 `deepseek/deepseek-v4.1-flash-1017`, 작가 호출 하나, 입력에 견본 날씨(`light rain, 3 to 9°C, rain 2.1 mm`), 견본 화제 다섯(정치 하나 포함), 열린 줄거리 하나("커버곡 연습", 시작한 날 한 번 움직임). **1 passed.** 운영 exa 서버는 부르지 않았다.
  - 입력 1,527·출력 957 토큰, 약 2,500토큰(1센트 아래. 키 사용량 숫자는 실행 직후 바뀌지 않았다).
  - 모든 칸이 모양대로 왔다: 일정 둘(시각 `14:00`·`11:00`), 정사 하나, 기분 `thinking`과 까닭, `arc`는 `{"move": "advance", "id": 5}`(입력의 열린 줄거리).
  - 본 것: 일기가 "비가 오는 날이라 창가 자리를 비워뒀다"로 날씨를 썼고, 줄거리를 일기("커버곡 첫 소절을 다시 붙잡았는데…")와 일정("커버곡 후렴 구간을 다시 녹음해 본다")으로 이었다. 화제 다섯은 하나도 쓰지 않았다(정치 제목 포함). 화면 속 방에 사는 페르소나가 사용자 쪽 비를 자기 날씨처럼 쓴 것이 어색한지는 써 보며 본다.

### 8.5 알려진 한계

- 날씨 위치는 서버 설정 하나다. 사용자가 다른 곳에 있으면 어긋난다. 날짜는 서버 시간대로 본다.
- 시작 직후(첫 읽기 전)나 Open-Meteo가 계속 실패하면 날씨가 없다. 바깥 기온 같은 지금 날씨는 없고 하루 요약만 있다.
- 화제는 검색 엔진이 고른 기사 제목이라 "화제"가 아닐 수 있다. 운영 exa 서버의 결과 모양으로는 확인하지 않았다(가짜 서버와 견본 제목만). 결과 모양이 달라 제목을 못 읽으면 화제가 없어진다(일기는 그대로 쓴다).
- 화제 검색이 실패한 날은 다시 하지 않는다.
- 줄거리 진행은 그날 일기에 잇기만 하고 따로 요약을 남기지 않는다. 14일보다 오래 움직이지 않은 줄거리는 작가가 시작일만 본다.
- 숨긴 줄 표시는 줄 앞에만 붙는다. 앱에서는 이 단계의 어떤 것도 보이지 않는다(설계대로).
