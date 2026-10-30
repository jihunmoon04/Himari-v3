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
five 1280×720 screens (a small Save button, Settings in the top-right corner, a menu
word, a red circle among grey squares, a search field), each asked twice as HIMARI
sends a screen (the `screen` attachment line with its size, `SCREEN_NOTE` and
`SCREEN_MOVE_NOTE`). A `[[move:X,Y]]` counted when it fell on the target with 24 pixels
to spare. Seven reached at least 8 of 10, so `screen_points` is declared for
`openai/gpt-6-luna`, `openai/gpt-6.1-sol`, `anthropic/claude-sonnet-5.5`,
`anthropic/claude-haiku-5.5`, `xiaomi/mimo-v2.6-pro`, `deepseek/deepseek-v4.1-flash`
and `minimax/minimax-m3`. `openai/gpt-5-nano` hit 6 of 10 (its misses include the small Save
button both times), so it sees screens but is not told how to move. The screens are
plain drawings; real desktops are busier, so the owner's own use is the next check.
