# 도트 격자 정리본

`moods/neutral.png`(1230×1278)를 실제 도트 격자로 정리한 결과다. 애니메이션 프레임은 이 파일을 기준으로 부분만 수정해 만든다.

- `neutral.png`: 205×206, 투명 배경, 색 32개(인덱스 0은 투명)
- `neutral.aseprite`: 같은 그림의 Aseprite 원본(레이어 1개)
- `neutral-x6.png`: 6배 최근접 확대 미리보기
- `himari-32.gpl`: 공통 팔레트. 새 프레임과 표정도 이 팔레트만 사용한다.

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

`../tools/idle-test.lua`로 파츠를 움직여 4프레임 대기 동작을 만들었다. 결과는 `idle-test.aseprite`이고, `idle-test-x3.gif`는 3배 확대 GIF다.

- 상체 그룹: 0, −1, −1, 0px
- 헤일로: 0, −1, −2, −1px
- 머리카락 끝: 0, 0, +1, 0px
- 프레임 길이: 400, 300, 400, 300ms

스크립트는 쉬는 자세에서 불투명하던 안쪽 픽셀이 투명해진 개수를 센다. 세 프레임 모두 0이다.

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
- **앱용 낱장**: `frames/`에 205×206 PNG로 있다. `open`, `blink-half`, `blink-closed`, `talk-small`, `talk-wide`

```
aseprite -b --script-param in=neutral-parts.aseprite --script-param out=neutral-face.aseprite --script-param frames=f%d.png --script face.lua
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

깜빡임과 말하기는 아직 neutral에만 있다. 다른 표정에서 쓰려면 그 표정의 눈과 입 영역에 같은 방식으로 프레임을 그려야 한다.
