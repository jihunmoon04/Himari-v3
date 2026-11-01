# 도트 격자 정리본

`moods/neutral.png`(1230×1278)를 실제 도트 격자로 정리한 결과다. 애니메이션 프레임은 이 파일을 기준으로 부분만 수정해 만든다.

- `neutral.png`: 205×206, 투명 배경, 색 32개(인덱스 0은 투명)
- `neutral.aseprite`: 같은 그림의 Aseprite 원본(레이어 1개)
- `neutral-x6.png`: 6배 최근접 확대 미리보기
- `himari-32.gpl`: 공통 팔레트. 새 프레임과 표정도 이 팔레트만 사용한다. 원래 32색이고, 졸기의 zzz용 하늘색 두 개(33, 34)를 나중에 더했다.

## 만든 방법

`../tools`의 Lua 스크립트를 Aseprite 배치 모드로 실행했다.

1. `grid.lua`: 경계선의 주기로 격자를 측정했다. 가로 약 6.02px(오프셋 4.75), 세로 약 6.255px(오프셋 6.75)이다. 두 배 간격(12px)도 점수가 비슷했지만 축소해 보면 얼굴이 뭉개져서 6px 격자를 택했다. 가로세로 간격이 다른 것은 생성 이미지가 세로로 약간 늘어나 있기 때문이다.
2. `downsample.lua`: 칸마다 가운데 50% 영역 픽셀의 메도이드 색을 택한다. 칸 경계의 안티앨리어싱 색은 버리고, 불투명 픽셀이 절반 이하인 칸은 투명으로 둔다.
3. `palette.lua`: 6466색을 CIELAB 가중 k-means로 32색으로 줄였다. 가장 먼 색부터 시드를 잡아서 피부, 눈, 리본 같은 작은 영역이 머리카락 색에 흡수되지 않게 했다. Aseprite 기본 양자화는 피부가 회분홍으로 바뀌어 쓰지 않았다.

```
aseprite -b --script-param in=neutral.png --script-param out=grid.txt --script grid.lua
aseprite -b --script-param in=neutral.png --script-param sx=6.02 --script-param ox=4.75 --script-param sy=6.255 --script-param oy=6.75 --script-param out=n6.png --script-param preview=n6-x6.png --script downsample.lua
aseprite -b --script-param in=n6.png --script-param colors=32 --script-param out=neutral.png --script-param preview=neutral-x6.png --script-param log=palette.txt --script palette.lua
```

## 파츠 레이어

`neutral-parts.aseprite`는 `../tools/split.lua`로 `neutral.png`를 애니메이션 단위로 나눈 파일이다. 아래에서 위 순서다. `neutral-parts-map.png`는 파츠마다 다른 색을 입혀 경계를 보여준다.

| 레이어 | 내용 | 나눈 기준 |
|---|---|---|
| `base` | 휠체어, 등받이, 치마, 다리, 신발 | 다른 파츠에 들지 않은 나머지 |
| `hair tail` | 왼쪽으로 흘러내린 머리카락 | 다각형 영역 안의 머리카락 색. 등받이 사각형은 제외 |
| `underlay` | 움직이는 파츠의 이음선 두 줄을 복사한 것 | 아래 설명 참고 |
| `upper (breathing)/body` | 머리, 앞머리, 아호게, 상체, 소매, 손 | 상체 다각형. 등받이는 제외 |
| `upper (breathing)/eyes` | 두 눈 사각형 (104–113, 54–59), (119–128, 60–64) | 좌표 |
| `upper (breathing)/mouth` | 입 사각형 (110–118, 64–68) | 좌표 |
| `halo` | 헤일로 고리와 분홍 조각 | 머리 위 영역의 분홍, 크림색 |

- **좌표 기준**: 좌표는 205×206 neutral 자세에만 맞는다. 다른 표정 그림에 쓰려면 `split.lua`의 영역을 다시 잡아야 한다.
- **`underlay`**: 상체는 위로, 머리카락 끝은 아래로 1–2px 움직일 때 반대쪽 이음선 뒤가 비게 된다. 이 레이어가 그 자리를 같은 색으로 채운다. 쉬는 자세에서는 위 파츠에 가려 보이지 않는다.
- **눈, 입 레이어**: 사각형 영역의 피부와 머리카락까지 포함한다. 깜빡임이나 말하기 프레임은 이 레이어만 새로 그리면 된다.
- **헤일로와 아호게**: 헤일로가 아호게와 겹치는 몇 픽셀은 헤일로 쪽에 들어가 있다. 헤일로를 움직이면 그 자리의 아호게가 비므로, 필요하면 `body`에 직접 메워야 한다.

## 움직임 시험

`../tools/idle-test.lua`로 파츠를 움직여 6프레임 숨쉬기 동작을 만들었다. 결과는 `idle-test.aseprite`이고, `idle-test-x3.gif`는 3배 확대 GIF다. 한 번 숨쉬는 데 4초(올라간 상태 약 2초, 내려온 상태 약 2초)다.

- 상체 그룹: 0, −1, −1, −1, 0, 0px
- 머리카락 끝: 0, 0, −1, −1, −1, 0px. 상체보다 250ms 늦게 같은 방향으로 따라간다(소유자 검토 2026-10-31: 반대로 움직이면 어색함).
- 헤일로: 0, −1, −1, −2, −2, −1px
- 프레임 길이: 1250, 250, 750, 1000, 250, 500ms

스크립트는 쉬는 자세에서 불투명하던 안쪽 픽셀이 투명해진 개수를 센다. 다섯 프레임 모두 0이다. 머리카락 끝도 위로 움직이므로 `split.lua`의 `underlay`는 그 아래쪽 이음선을 채운다.

```
aseprite -b --script-param in=neutral.png --script-param out=neutral-parts.aseprite --script-param debug=neutral-parts-map.png --script split.lua
aseprite -b --script-param in=neutral-parts.aseprite --script-param out=idle-test.aseprite --script-param log=idle.txt --script-param sheet=idle-sheet.png --script-param gif=idle.gif --script idle-test.lua
```

## 깜빡임과 말하기

`../tools/face.lua`가 `neutral-parts.aseprite`의 `eyes`와 `mouth` 레이어만 고쳐서 `neutral-face.aseprite`를 만든다. 몸, 머리카락, 휠체어는 모든 프레임에서 같다.

| 태그 | 프레임 | 길이 | 내용 |
|---|---|---|---|
| `blink` | 1–4 | 2600, 60, 90, 60ms | 뜬 눈, 반쯤 감음(윗눈꺼풀 2줄 내림), 감음(아래로 휜 속눈썹선), 반쯤 감음 |
| `talk` | 5–7, 핑퐁 | 120, 140, 120ms | 작게 벌림, 크게 벌림(입안과 혀), 작게 벌림 |

- **고정 기준**: 눈을 감을 때 눈을 가로지르는 머리카락 가닥은 그대로 둔다.
- **편집 방법**: 색은 모두 `himari-32.gpl` 안에서 골랐다. 프레임을 손볼 때는 Aseprite에서 해당 레이어를 직접 고치거나 `face.lua`의 좌표를 고쳐 다시 만든다.
- **미리보기**: `blink-x3.gif`, `talk-x3.gif`, 얼굴 확대 비교 `face-strip.png`(뜬 눈, 반, 감음, 작은 입, 큰 입)
- **낱장**: `face.lua`가 `frames/`에 205×206 PNG로 쓴다(`open`, `blink-half`, `blink-closed`, `talk-small`, `talk-wide`). 앱은 이제 이 낱장을 쓰지 않는다(아래 "앱 자산").

```
aseprite -b --script-param in=neutral-parts.aseprite --script-param out=neutral-face.aseprite --script-param frames=frames --script face.lua
aseprite -b neutral-face.aseprite --tag blink --scale 3 --save-as blink-x3.gif
```

## 다른 표정 7종

`moods/`의 happy, excited, sad, surprised, thinking, embarrassed, worried는 `../tools/mood.lua`로 만들었다. 생성 이미지들은 헤일로, 머리카락, 몸의 세부가 서로 달라서 얼굴만 가져와 neutral 위에 합성한다. 그래서 표정이 바뀌어도 얼굴 밖의 모든 픽셀이 neutral과 같다.

1. 표정마다 격자 오프셋을 neutral 간격(6.02×6.255px)에서 다시 찾는다. 가로 4.00–5.35, 세로 0.70이었다.
2. `downsample.lua`와 같은 규칙으로 205×206으로 줄이고, `himari-32.gpl`에서 가장 가까운(CIELAB) 색으로 바꾼다.
3. neutral과 일치하는 픽셀이 가장 많은 이동량을 찾는다. 7종 모두 (0, 0)이었다.
4. 얼굴 사각형 (101–133, 47–72)만 표정 그림에서 가져온다. 턱의 손(129 이상, 66 이상)은 제외한다.

`moods-faces.png`는 8종의 얼굴 확대 비교다. 순서는 윗줄 neutral, happy, excited, sad, 아랫줄 surprised, thinking, embarrassed, worried다.

```
aseprite -b --script-param in=happy.png --script-param neutral=neutral.png --script-param sx=6.02 --script-param ox=4.00 --script-param sy=6.255 --script-param oy=0.70 --script-param out=moods/happy.png --script-param log=happy.txt --script mood.lua
```

깜빡임과 말하기 변형은 이제 `faces.aseprite`에 표정마다 있다(아래 "다른 표정의 눈·입").

## 앱 자산: 원본과 내보내기

앱은 `Himari.Companion/Assets/character.png`(색 인덱스 시트 한 장)와 `character.json`만 쓴다. 형식과 재생 규칙은 [docs/companion-animation-v0.1.md](../../../../../docs/companion-animation-v0.1.md)에 있다. 이제 그림을 고칠 원본은 아래 두 파일이다. 위의 `neutral-*`, `frames/`, `moods/`는 만든 과정의 기록이다.

| 파일 | 내용 |
|---|---|
| `body.aseprite` | 몸 동작. 태그 하나가 동작 하나(`idle` 6프레임, 그 뒤로 `sleep`, `yawn`, `glance`, `ahoge`, `twinkle`). 숨긴 레이어 `@face`의 1×1 점이 그 프레임에서 얼굴을 놓을 자리다(쉬는 자세 (101, 47), 숨쉬기 프레임은 1px 위). 숨긴 `@eyes`, `@mouth`의 셀 데이터는 그 프레임의 눈·입을 강제한다. |
| `faces.aseprite` | 얼굴 33×26. 표정마다 프레임 하나, 태그 이름이 표정 이름. 그룹 `eyes/half`, `eyes/closed`, `mouth/small`, `mouth/wide`에 기본 얼굴과 다른 픽셀만 그린다(happy는 눈 변형이 없다). 숨긴 `@body`는 맞춰 볼 몸 그림이다. 턱의 손(129 이상, 66 이상)과 그 왼쪽 턱선 아래의 손가락(68–72행, 아래 "얼굴에서 손 빼기")은 몸 쪽이라 비워 둔다. |

- **처음 만들기**: `../tools/sources.lua`가 `idle-test.aseprite`, `neutral-face.aseprite`, `moods/`에서 두 원본을 한 번 만들었다. 그대로 다시 돌리면 고친 `faces.aseprite`를 덮어쓴다. 숨쉬기를 바꿀 때는 `idle-test.lua`를 고쳐 돌린 뒤 `--script-param only=body`로 `body.aseprite`만 다시 만들고, 이어서 `legs.lua`(아래 "다리 교체")와 `clips.lua`를 돌린다.
- **neutral 눈·입 다시 그림**(소유자 검토 2026-10-31): `../tools/neutral-variants.lua`가 `faces.aseprite`의 neutral 변형을 고쳤다. 반쯤 감은 눈은 뜬 눈의 속눈썹처럼 오른쪽으로 내려가고, 감은 눈은 선 위의 갈색 채움을 없앴다. 두 경우 모두 바깥쪽 속눈썹 끝을 눈마다 2px 줄였다. 말하는 입은 다문 입 선의 두 끝 (112,65), (116,67)을 그대로 두고 가운데가 벌어진다. 그래서 `frames/`의 낱장과는 이제 다르다.
- **내보내기**: 원본을 고친 뒤 `../tools/export.lua`를 돌린다(`pixel/`에서 실행).
  - 같은 그림 조각은 시트에 한 번만 넣는다(지금 78조각, 1002×1674).
  - UI 테스트가 비교할 기준 그림 12장(`idle-1-neutral`, `idle-2-happy`, `idle-1-neutral-closed-wide`, `sleep-4-neutral-closed`, `yawn-2-happy`, `glance-2-sad-left`, `ahoge-2-neutral`, `twinkle-3-neutral`, `stretch-4-sad-closed`, `stretch-5-worried`, `stretch-8-happy`, `stretch-9-neutral`)도 함께 쓴다.
  - 다음 중 하나라도 어기면 아무것도 쓰지 않고 실패한다: 색 인덱스 모드와 `himari-32.gpl` 팔레트, 모든 몸 프레임의 `@face` 점, 모든 동작의 첫 프레임과 단발 동작의 마지막 프레임 = idle 첫 프레임, 반복 동작의 `loopFrom`, 표정 8종.

```
aseprite -b --script-param body=clean/body.aseprite --script-param faces=clean/faces.aseprite --script-param palette=clean/himari-32.gpl --script-param out=../../Himari.Companion/Assets --script-param refs=../../Himari.Companion.UiTests/Fixtures/character --script tools/export.lua
```

결과물(시트, JSON, 기준 그림)은 저장소에 넣는다. 빌드와 CI에는 Aseprite가 없다.

## 졸기

`../tools/clips.lua`(처음 이름 `sleep.lua`)가 `body.aseprite`의 파츠를 움직여 idle 뒤의 동작을 모두 만든다. 다시 돌리면 idle 뒤를 지우고 새로 만든다. 이 스크립트가 만들지 않는 동작이나 태그 없는 프레임이 idle 뒤에 있으면 아무것도 하지 않고 실패한다(PR #144, #145 검수). idle을 다시 만들었다면(`sources.lua only=body`) 이것도 다시 돌린다. 아래는 `sleep`이고, 나머지 동작은 그다음 절에 있다.

- **잠들기**(앞 3프레임, 한 번): 반쯤 감은 눈 → 고개 1px → 고개 2px와 감은 눈. 태그 사용자 데이터 `loopFrom=4`가 반복 시작이다.
- **자는 중**(500ms × 12): 고개 2px 아래 3초, 1px 아래 3초. 머리카락 끝은 한 칸, 헤일로는 두 칸 늦게 따라간다.
- **zzz**: `zzz` 레이어. z 하나가 12칸(6초) 동안 머리 오른쪽에서 작고 흐리게 나타나, 떠오르며 3px에서 7px로 커지고, 위에서 흐려지며 사라진다. 세 개가 4칸 간격이라 늘 "zzz"로 보인다(소유자 검토 2026-10-31: 커지면서 위에서 사라지게).
  - **색**: 파스텔 하늘색(인덱스 33)에 연한 파랑 테두리(34). 처음 고른 진한 하늘색·파랑은 이질적이라 바꿨다.
  - **모양**: 위아래 획이 한 칸씩 어긋난 기울어진 z이고, 테두리는 대각선을 건너뛰어 모서리가 둥글게 보인다.
  - **흐려짐**: 색인 팔레트라 반투명이 없다. 처음 칸과 마지막 두 칸은 테두리를 빼고, 마지막 칸은 픽셀을 한 칸씩 건너뛴다. 더 옅은 색을 쓰면 어두운 바탕화면에서 오히려 빛나 보여서 쓰지 않았다. 캐릭터에 파랑이 없어 이 두 색을 `himari-32.gpl` 끝에 더했다. `--script-param palette=clean/himari-32.gpl --script-param faces=clean/faces.aseprite`를 주면 두 원본의 팔레트도 이 파일로 맞춘다.
- **태그**: 프레임을 idle 바로 뒤에 넣으면 Aseprite가 idle 태그를 늘린다. 스크립트가 idle 끝을 되돌리고, `export.lua`는 태그끼리 프레임이 겹치면 실패한다.
- **틈 메우기**: 파츠를 내리면 휠체어 쪽과 만나던 자리가 빈다. 바깥과 이어지지 않은 투명 픽셀을 쉬는 자세 색으로 맨 아래 `fill` 레이어에 채운다.

```
aseprite -b --script-param body=clean/body.aseprite --script-param log=clips.txt --script tools/clips.lua
```

## 하품과 가끔 나오는 동작

같은 `clips.lua`가 만든다. 모두 쉬는 자세로 시작해서 쉬는 자세로 끝나는 단발 동작이다. 길이와 동작은 [설계 문서 §5.4](../../../../../docs/companion-animation-v0.1.md)에 있다.

- **`yawn`**: 반쯤 감은 눈 → 손으로 입을 가린 자세 그림 → 반쯤 감은 눈. 자세 그림은 `poses/yawn-hand.png`이고, `clips.lua`의 `POSES`에 적은 상자(98–170열, 40–105행)만 상체 레이어에 붙인다. 다른 레이어는 그 상자를 비우고, `@face` 셀 데이터 `own`으로 표정 얼굴을 얹지 않게 한다.

## 다리 교체 (`../tools/legs.lua`)

소유자 결정(2026-10-31): 허벅지가 짧게 접혀 무릎이 높던 다리를, 허벅지가 좌석을 따라 나오고 다리를 꼰 새 그림(`poses/legs.png`)으로 바꿨다. 새 그림은 소유자가 붙여 넣은 382×445 그림을 `pose.lua`로 변환했다(도트 약 2.23px, 상체는 neutral과 같았다).

- idle 여섯 프레임의 상자(100–176열, 106–195행)를 새 그림으로 바꾼다. 다리는 움직이지 않으므로 `base` 레이어에 넣고, 새 허벅지가 덮는 상체 레이어의 무릎 쪽 픽셀은 `base`로 옮긴다.
- 상체가 숨쉬며 1px 올라갈 때 틈이 생기지 않도록 상자 바로 위 줄을 `underlay`에 복사한다. 186행 아래는 neutral에 있던 자리만 받는다(붙여 넣은 그림 가장자리의 잡티).
- 쉬는 자세와 숨쉬기 프레임의 한 칸짜리 구멍 수를 로그에 쓴다. 바꾸기 전과 같다.
- 잠들 때 상체가 내려가도 무릎 위 치마 단은 `clips.lua`가 제자리에 두고 상체보다 앞에 그린다. 경계는 치마 단을 따라 (104, 104)에서 (152, 97)까지 비스듬히 자르고, 내려가는 상체는 그 뒤로 들어간다. 같이 내려가면 치마 단이 허벅지를 덮어 경계가 흐려졌다(소유자 검토 2026-10-31).

```
aseprite -b --script-param body=clean/body.aseprite --script-param conv=clean/poses/legs.png --script-param neutral=clean/neutral.png --script-param log=legs.txt --script tools/legs.lua
aseprite -b --script-param body=clean/body.aseprite --script-param log=clips.txt --script tools/clips.lua
```

`neutral.png`과 `neutral-*`, `moods/`는 예전 다리 그대로인 기록이다. 앱의 그림은 `body.aseprite`가 기준이다.

## 자세 그림 변환 (`../tools/pose.lua`)

Codex가 그린 1230×1278 그림을 neutral과 같은 205×206 도트로 옮긴다. 도트 격자(약 6.02×6.26px)의 위치를 찾고, 칸마다 가장 흔한 색을 골라 원본 32색 중 가장 가까운 색(CIELAB)으로 바꾼 뒤, 원본과 가장 잘 맞도록 ±3px 안에서 위치를 맞춘다. 결과 그림과 원본과의 차이 지도를 쓴다. 생성 그림은 바꾸지 말라는 부분도 조금씩 바뀌므로, 바뀐 부분만 상자로 잘라 쓴다. 붙여 넣은 작은 그림은 `sx`, `sy`(도트 크기), `ox`, `oy`(neutral (0, 0) 칸의 왼쪽 위가 그림에서 놓이는 자리 근처), `bg=corner`(왼쪽 위 색을 투명으로)를 준다.
- **`glance`**: idle과 같은 숨쉬기에 `@eyes` `left` → `right`. 눈 그림은 `../tools/glance-variants.lua`가 `faces.aseprite`에 그린다(`eyes/left`, `eyes/right`).
- **`ahoge`**: 상체 레이어에서 아호게 윗부분(20–25행, 108–128열)만 1px씩 좌우로 옮긴다. 뿌리는 그대로다.
- **`stretch`**: 아래 "기지개"에 따로 적었다. 처음 시험한 어깨만 올리는 작은 기지개는 기지개로 읽히지 않아 뺐다(소유자 검토 2026-10-31).
- **`twinkle`**: 헤일로 오른쪽 위 (131, 10)에 4방향 별(흰 중심, 분홍 끝)이 작게 → 크게 → 작게 두 번 반짝이고, 헤일로 별의 위아래 끝이 1px 길어진다.

```
aseprite -b --script-param faces=clean/faces.aseprite --script tools/glance-variants.lua
```

## 다른 표정의 눈·입

`../tools/mood-variants.lua`가 `faces.aseprite`의 표정 7종에 눈·입 변형을 그렸다. `mood-variants.png`가 결과다(줄: neutral, happy, excited, sad, surprised, thinking, embarrassed, worried / 칸: 기본, 반쯤 감음, 감음, 작은 입, 큰 입. 어두운 칸은 그 변형이 없음). neutral은 소유자가 확인한 손 그림(`neutral-variants.lua`)이고, 이 스크립트는 그 모양을 규칙으로 옮긴 것이다.

- **눈**: 표정마다 속눈썹 모양이 달라서 그 표정의 눈에서 직접 계산한다. 눈 칸(왼눈 103–114 × 52–59, 오른눈 119–129 × 57–64)에서 속눈썹 색이 있는 열을 눈으로 본다.
  - 반쯤 감음: 눈 윗선에 맞춘 직선을 눈 높이의 절반만큼 내려 한 줄 눈꺼풀로 그리고, 그 위는 피부로 덮는다. 두 줄 넘게 두꺼운 속눈썹의 남는 줄과 바깥 끝 아래 테두리도 지운다.
  - 감음: 눈의 보통 아랫선 위에 호를 그리고 양 끝을 한 줄 올린다.
  - 둘 다 바깥 끝을 한 칸 짧게 해서 꼬리가 길어 보이지 않게 한다(neutral 검토와 같은 기준).
  - 덮는 피부색은 같은 줄의 눈 바깥에서 가져온다. 그래서 embarrassed의 볼 홍조 띠는 감은 눈 위로도 이어진다.
  - happy는 이미 눈을 감고 웃는 얼굴이라 눈 변형이 없다.
- **입**: 표정마다 입 위치에 작은 틀을 놓는다. 작은 입은 3×2, 큰 입은 3×4다.
  - happy와 surprised는 원래 입 안을 어둡게 하거나 한 줄 더 연다.
  - excited는 원래 입이 크게 벌어져 있다. 그래서 작은 입은 입을 줄이고, 큰 입은 원래 입에 입안을 보인다.

```
aseprite -b --script-param faces=clean/faces.aseprite --script tools/mood-variants.lua
```

다시 돌리면 7종의 변형을 새로 그린다(neutral은 건드리지 않는다). 특정 표정만 손으로 고쳤다면 그 표정은 `--script-param only=<표정>`으로 따로 다루거나 다시 돌리지 않는다.

## 얼굴에서 손 빼기 (`../tools/face-hand.lua`)

소유자 요청(2026-11-01): 쉬는 자세에서 왼손이 턱을 받치고 있어서, 그 자세에서 잘라 낸 표정 8종의 얼굴 사각형에 턱선 오른쪽 아래의 손가락이 함께 들어 있었다. 손이 다른 곳에 있는 프레임(기지개)에서는 손가락만 턱에 떠 보였다.

- `faces.aseprite`의 `face` 레이어에서 표정마다 같은 43칸을 지운다. 손가락 윗선(68행)과 턱선((123, 68), (121, 69), (118, 70), (117, 71)) 아래, 기존 손 자리(129열) 왼쪽이다(68행 124–128, 69행 122–128, 70행 120–128, 71·72행 118–128). 턱선은 얼굴에 남긴다.
- 몸 프레임은 그 자리에 같은 손을 이미 갖고 있다(43칸 중 42칸이 neutral 얼굴과 같고, 나머지 한 칸은 neutral 얼굴에서 투명). 그래서 neutral의 쉬는 자세는 그대로이고(`idle-1-neutral` 기준 그림 불변), 다른 표정은 손 자리가 neutral의 손으로 바뀌어 6–10칸(thinking은 30칸) 달라진다.
- 다시 돌려도 지울 것이 없으면 아무것도 바꾸지 않는다. 돌린 뒤에는 `export.lua`로 다시 내보낸다.

```
aseprite -b --script-param faces=clean/faces.aseprite --script-param log=face-hand.txt --script tools/face-hand.lua
```

## 기지개 (`../tools/stretch.py`)

소유자 요청(2026-11-01): 깬 직후에 트는 `stretch`. 볼에 댄 손에서 팔을 들어 머리 위에서 깍지 끼는 동안 꼰 다리를 풀어 앞으로 쭉 펴고, 다 뻗은 상태에서 상체를 1px씩 두 번 올렸다 내린 뒤 펼 때의 역순으로 접는다(19프레임, 5.2초).

- **그림**: 소유자가 이미지 생성 도구로 만들어 2026-11-01에 준 그림 두 장이다. 상체 7장면(`himari-stretch-seven-keyposes.png`, 1312×1199)과 다리 8장면(`himari-leg-stretch-eight-keyposes.png`, 1536×1024). 저장소에는 넣지 않았고(소유자 PC의 `build/animation-reference/`, git 무시), 변환 결과만 `poses/stretch/`에 있다. 하품의 Codex 그림(`requests/big-motions-v1/`)과 같은 방식이다.
  - `stretch.json`의 `sources`에 두 그림의 SHA-256이 있다. `stretch.py`는 같은 이름의 그림이 이 해시와 다르면 저장된 정렬값으로 변환하지 않고 멈춘다(`--refit`이면 새로 찾는다). 원본이 없어도 앱 실행, `clips.lua`, `export.lua`는 커밋된 `poses/stretch/`만으로 다시 만들 수 있다. 원본이 필요한 것은 변환을 다시 할 때뿐이다.
- **변환**: `stretch.py`가 장면마다 잘라 배경을 지우고, 상체는 머리(78–150열, 18–82행)에, 다리는 휠체어(다리 상자 밖)에 맞춰 크기와 위치를 찾는다. 도트 한 칸이 그림의 약 2.5–2.8px이다. 칸 가운데 절반의 가장 흔한 색을 `himari-32.gpl`로 옮긴다(`pose.lua`와 같은 방식).
  - 상체: 이웃 장면에서 따라 들어온 조각을 지우고, 그림의 크고 크림색인 헤일로를 지운 뒤 쉬는 자세의 헤일로를 얹는다. 96행 위는 그림이, 118행 위는 아래에 아무것도 없을 때만 그림이 보인다. 얼굴 위치는 표정 얼굴의 어두운 선(눈, 눈썹)이 가장 잘 맞는 곳이다.
  - 다리: 98열 오른쪽, 102행 아래(다리 영역)를 그림으로 바꾼다. 휠체어 바퀴와 등받이는 쉬는 자세 그대로다.
  - 다리 순서는 그림 번호가 아니라 꼰 정도로 정했다. 펼 때 L3(쉬는 자세와 비슷) → L2(위 다리를 들어 풂) → L4 → L6 → L7 → L8(쭉 폄). 접을 때는 그 역순이다(소유자 검토: 펼 때도 접을 때의 역순이 자연스럽다).
- **결과**: `poses/stretch/upper-<n>.png`(상체 레이어), `upper-7-lift1.png`(1px 올린 것), `legs-<n>.png`(다리 영역), `stretch.json`(정렬값, 얼굴 위치, 프레임 목록). 정렬 탐색은 몇 분 걸려서 `stretch.json`에 남기고 다시 쓴다. 처음부터 다시 찾으려면 `--refit`을 준다.
- **`clips.lua`**: `stretch.json`의 프레임 목록대로 만든다. `upper`가 있으면 상체 레이어가 그 그림이 되고 다른 상체 파츠와 헤일로는 비운다. `legs`가 있으면 `base`의 다리 영역을 바꾸고, 쉬는 상체를 쓰는 프레임에서는 `underlay`와, 다리 영역에서 `base`와 겹쳐 그린 무릎 픽셀을 뺀다. 눈을 감은 6–7장면은 `@face`가 `own`이다. 틈 메우기는 하지 않는다.
- **필요한 것**: Python 3과 numpy, Pillow, PATH의 `aseprite`(쉬는 자세 레이어를 읽는다).
- **남은 손질**: 손가락 테두리가 덩어리져 보인다. 6–7장면은 원래 그림에 옆머리 똥머리와 리본이 없다. 오른쪽 팔걸이 근처에서 그림의 팔걸이와 겹친 자리가 있다. Aseprite에서 `body.aseprite`의 해당 프레임을 고치면 되지만, `clips.lua`를 다시 돌리면 덮어쓰므로 고친 것은 `poses/stretch/`의 그림에 해야 한다.

```
python -I tools/stretch.py --sheets ../../../../build/animation-reference
aseprite -b --script-param body=clean/body.aseprite --script-param log=clips.txt --script tools/clips.lua
```
