# OpenRouter deployment catalog

Checked on 2026-10-20 against the public [models API](https://openrouter.ai/api/v1/models)
and live tool-call/result continuation requests. These are provider capabilities;
the offered list remains the operator's `HIMARI_MODELS` setting.

| Model | Context limit used | Maximum output declared | Verified selectable efforts |
| --- | ---: | ---: | --- |
| `openai/gpt-6-luna` | 1,050,000 | 128,000 | Existing declaration retained |
| `openai/gpt-6.1-sol` | 1,050,000 | 128,000 | low, medium, high |
| `anthropic/claude-sonnet-5.5` | 1,000,000 | 128,000 | low, medium, high |
| `xiaomi/mimo-v2.6-pro` | 1,048,576 | 131,072 | none declared |
| `z-ai/glm-5.3` | 1,048,576 | 943,718 | low, medium, high |
| `deepseek/deepseek-v4.1-flash` | 1,048,576 | 943,718 | low, medium, high |
| `minimax/minimax-m3` | 524,288 | 512,000 | none declared |

The smaller model/top-provider context is used. Output maxima come from the catalog;
they are not requested output lengths or a guarantee that all providers allow them.
The existing input-ceiling rule reserves at most half the window. All seven models
therefore accept the deployment's 200,000-token context budget.

MiMo and MiniMax advertise `reasoning`, but not `reasoning_effort`; their selector
has no effort control. No new model gets a cache breakpoint: only Luna has measured
breakpoint support. Advertised cache prices do not establish actual cache hits.

Example operator configuration (Luna stays the default):

```dotenv
HIMARI_MODEL=openai/gpt-6-luna
HIMARI_MODELS=openai/gpt-6-luna,xiaomi/mimo-v2.6-pro,z-ai/glm-5.3=medium,deepseek/deepseek-v4.1-flash=medium,minimax/minimax-m3,openai/gpt-6.1-sol=medium,anthropic/claude-sonnet-5.5=medium
```

The live checks do not establish comparative quality or reliable Korean chess
commentary. Sonnet rejected the probe's forced named `tool_choice`; continuation
passed with the automatic tool selection HIMARI actually uses.

## Images (V3-P67, 2026-10-22)

`architecture.input_modalities` lists `image` for every model above except `z-ai/glm-5.3`.
Each of those models was then checked with `tests/live/test_openrouter_images_live.py`
(a 256×256 JPEG of one colour): it named the colour of ① an image in a user message
and ② an image in the user message HIMARI puts after a Tool group. All eight passed
both, so `image_input` is declared for `openai/gpt-6-luna`, `openai/gpt-6.1-sol`,
`anthropic/claude-sonnet-5.5`, `anthropic/claude-haiku-5.5`, `openai/gpt-5-nano`,
`xiaomi/mimo-v2.6-pro`, `deepseek/deepseek-v4.1-flash` and `minimax/minimax-m3`.
`z-ai/glm-5.3` gets attachment lines only. The checks say the models read the image;
they do not measure how well they describe real pictures.

## Points on a screen (V3-P84, 2026-10-30)

Each model with `image_input` was checked with `tests/live/test_openrouter_screen_live.py`:
five 1280×720 screens, each asked twice as HIMARI sends a screen (the `screen` attachment
line with its size, `SCREEN_NOTE` and `SCREEN_MOVE_NOTE`). A `[[move:X,Y]]` counted when it
fell on the target with 24 pixels to spare; a model passes with 8 of 10.

The screens: **button** (a small Save button beside Cancel), **corner** (Settings in the
top-right corner), **menu** (View in a File/Edit/View/Help bar), **shape** (a red circle
among grey squares), **field** (a search field). They are plain drawings: real desktops
are busier, with small icons, many windows and scaling, so these counts are not the
accuracy to expect there; the owner's own use is the next check.

Two runs on 2026-10-30, with the same eight models:

```
HIMARI_LIVE=1 HIMARI_LIVE_SCREEN_OUT=docs/live-results/screen-points-2026-10-30.jsonl \
HIMARI_LIVE_SCREEN_MODELS=<models> python -m pytest -q -s tests/live/test_openrouter_screen_live.py
```

- The first run kept pass or fail only: seven passed, `openai/gpt-5-nano` hit 6 of 10
  (both Save asks missed).
- The second run kept every answer, in
  [live-results/screen-points-2026-10-30.jsonl](live-results/screen-points-2026-10-30.jsonl)
  (model, scene, ask, target box, point, hit). Hits per scene, of 2:

| Model | button | corner | menu | shape | field | of 10 |
| --- | --- | --- | --- | --- | --- | --- |
| `openai/gpt-6-luna` | 2 | 2 | 2 | 2 | 2 | 10 |
| `openai/gpt-6.1-sol` | 2 | 2 | 2 | 2 | 2 | 10 |
| `anthropic/claude-sonnet-5.5` | 2 | 2 | 2 | 2 | 2 | 10 |
| `anthropic/claude-haiku-5.5` | 2 | 2 | 2 | 2 | 2 | 10 |
| `xiaomi/mimo-v2.6-pro` | 2 | 2 | 2 | 2 | 2 | 10 |
| `deepseek/deepseek-v4.1-flash` | 2 | 2 | 2 | 2 | 2 | 10 |
| `minimax/minimax-m3` | 2 | 2 | 2 | 2 | 2 | 10 |
| `openai/gpt-5-nano` | 1 | 2 | 2 | 0 | 0 | 5 |

`screen_points` is declared for the seven that passed both runs. `openai/gpt-5-nano`
missed by 30–160 pixels from the target's edge (left of Save, right of the circle, below
the field), so it sees screens but is not told how to move.

## Memory passes (V3-P85, 2026-10-31)

Seven inexpensive models were checked with `tests/live/test_memory_passes_live.py`
(`openai/gpt-6.1-sol` and `anthropic/claude-sonnet-5.5`, at $2/$10 per million tokens,
were left out: a pass runs after every quiet conversation). Twelve fixed conversations,
mostly Korean, each read three times exactly as a memory pass reads them (the same
input, `MEMORY_PASS_INSTRUCTIONS`, `low` effort where declared, a 4,000-token output
cap), then the same answer checks and plans:

**facts** (work and environment), **changed fact** (a saved city, now another),
**finished loop** (a saved interview, now done), **passing fact** (a trip until a date),
**web relayed** (HIMARI's answer quotes a page), **sensitive, not asked** (medication),
**sensitive, asked** (an allergy, "꼭 기억해 줘"), **greetings**, **dated loop** (a car
inspection on a date), **suggestion not taken** (HIMARI suggests PostgreSQL, the user
keeps SQLite), **preference** (short lists, the recommendation first), **refusal**
("이건 기억하지 마" before a salary).

The bars (V3-P85 §15): well-formed answers ≥ 95%; decisions dropped for evidence on no
user message ≤ 10%; expected notes found ≥ 80%; their dates right ≥ 90%; notes resting
on a user message that does not say them ≤ 5%; no note from HIMARI's words or a page;
sensitive notes applied without a request ≤ 2%; episodes stating HIMARI's suggestion as
decided ≤ 5%.

```
HIMARI_LIVE=1 HIMARI_LIVE_MEMORY_OUT=docs/live-results/memory-passes-2026-10-31.jsonl \
HIMARI_LIVE_MEMORY_MODELS=<model> python -m pytest -q -s tests/live/test_memory_passes_live.py
```

Every answer and its grading:
[live-results/memory-passes-2026-10-31.jsonl](live-results/memory-passes-2026-10-31.jsonl).

| Model | Well-formed | Found | Dates | Off evidence | From HIMARI/page | Sensitive leaks | Missed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `openai/gpt-6-luna` | 1.00 | 1.00 | 1.00 | 0.00 | 0 | 0 | — |
| `anthropic/claude-haiku-5.5` | 1.00 | 0.94 | 1.00 | 0.00 | 0 | 0 | the finished loop, 2 of 3 |
| `xiaomi/mimo-v2.6-pro` | 1.00 | 1.00 | 1.00 | 0.00 | 0 | 0 | — |
| `deepseek/deepseek-v4.1-flash` | 1.00 | 1.00 | 1.00 | 0.00 | 0 | 0 | — |
| `minimax/minimax-m3` | 1.00 | 1.00 | 1.00 | 0.00 | 0 | 0 | — |
| `z-ai/glm-5.3` | 1.00 | 0.97 | **0.67** | 0.00 | 0 | 0 | the trip's end, 2 of 3; the finished loop once |
| `openai/gpt-5-nano` | 0.97 | **0.79** | **0.50** | 0.00 | 0 | 0 | the finished loop (rewritten, not forgotten), notes and dates |

`memory_passes` is declared for the five that met every bar. No model made a note from
HIMARI's words or the page, and none kept the medication or the salary.

A first run on the same day changed three things before the run above, none of them in
a model's favour alone:
- **The instructions' example.** It read `"id": "m12 (update, forget)"`, and weaker models
  copied it literally (gpt-5-nano: 7 of 12 malformed). The example now shows real values,
  and an id that is not a note's names no note (`unknown_note`) instead of failing the answer.
- **The dated loop was a dental visit.** That is a health matter, and models did what the
  instructions ask: marked it sensitive (kept as a proposal) or wrote "busy that afternoon"
  without it. It is now a car inspection; a loop counts by its date as well.
- **The grading.** A trip until a date counts as a loop with that date as well as a fact
  with that end, and a note citing the user message it rests on counted as off evidence
  when it also named something from another message.

The check measures plain cases. Real conversations are longer and less tidy; the owner's
own conversations are the next check, with every automatic note shown and undoable.
