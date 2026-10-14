"""Composition root: the only place that imports concrete implementations.

``build_runtime`` wires the P0 stack over a Data store: ``InMemoryStore``
(tests; a "restart" is a new runtime over the same object) or the durable
``SqliteStore`` (a restart is a new process — or a new store — opening the
same database file).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from himari.control.ask_user import AskUserTool
from himari.control.driver import Driver
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore
from himari.environment.api import EnvironmentPlugin
from himari.prompt.api import RequestOptions
from himari.prompt.api import ContextBudget
from himari.prompt.compaction import Compactor
from himari.prompt.composer import Composer
from himari.prompt.loader import ContextLoader
from himari.provider.api import ProviderAdapter
from himari.tool.api import ToolHandler
from himari.tool.dispatcher import Dispatcher
from himari.tool.registry import ToolRegistry

DEFAULT_INSTRUCTIONS = ("You are HIMARI, a helpful assistant.",)


type Store = InMemoryStore | SqliteStore


@dataclass
class Runtime:
    store: Store
    data: InMemoryData | SqliteData
    registry: ToolRegistry
    driver: Driver


def build_runtime(
    provider: ProviderAdapter,
    *,
    tools: Iterable[ToolHandler] = (),
    environments: Iterable[EnvironmentPlugin] = (),
    store: Store | None = None,
    instructions: Sequence[str] = DEFAULT_INSTRUCTIONS,
    model_id: str = "scripted-model",
    max_runs_per_drive: int = 16,
    allow_require_user_tools: bool = False,  # keep False in P0 (no resolution API)
    reasoning_effort: str | None = None,
    context_budget: ContextBudget | None = None,  # None: no compaction or size check
) -> Runtime:
    store = store if store is not None else InMemoryStore()
    data = SqliteData(store) if isinstance(store, SqliteStore) else InMemoryData(store)
    registry = ToolRegistry([AskUserTool(), *tools], allow_require_user=allow_require_user_tools)
    for env in environments:
        for handler in env.tools():
            registry.register(handler)
    dispatcher = Dispatcher(registry, data, data, data)
    driver = Driver(
        log=data,
        loader=ContextLoader(data, data, registry, instructions),
        composer=Composer(),
        provider=provider,
        dispatcher=dispatcher,
        request_options=RequestOptions(model_id=model_id, reasoning_effort=reasoning_effort),
        max_runs_per_drive=max_runs_per_drive,
        planner=Compactor() if context_budget is not None else None,
        budget=context_budget,
    )
    return Runtime(store=store, data=data, registry=registry, driver=driver)


def read_env_file(path: str) -> dict[str, str]:
    """Minimal ``KEY=value`` reader for a local, git-ignored ``.env``. Values
    are never logged. Missing file → empty mapping."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return values
