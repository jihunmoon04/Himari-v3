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
