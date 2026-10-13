"""Composition root: the only place that imports concrete implementations.

``build_in_memory_runtime`` wires the P0 in-memory stack. Passing an existing
``store`` builds a fresh runtime over the same "durable" data — this is how a
process restart is modelled.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from himari.control.ask_user import AskUserTool
from himari.control.driver import Driver
from himari.data.memory import InMemoryData, InMemoryStore
from himari.environment.api import EnvironmentPlugin
from himari.prompt.api import RequestOptions
from himari.prompt.composer import Composer
from himari.prompt.loader import ContextLoader
from himari.provider.api import ProviderAdapter
from himari.tool.api import ToolHandler
from himari.tool.dispatcher import Dispatcher
from himari.tool.registry import ToolRegistry

DEFAULT_INSTRUCTIONS = ("You are HIMARI, a helpful assistant.",)


@dataclass
class InMemoryRuntime:
    store: InMemoryStore
    data: InMemoryData
    registry: ToolRegistry
    driver: Driver


def build_in_memory_runtime(
    provider: ProviderAdapter,
    *,
    tools: Iterable[ToolHandler] = (),
    environments: Iterable[EnvironmentPlugin] = (),
    store: InMemoryStore | None = None,
    instructions: Sequence[str] = DEFAULT_INSTRUCTIONS,
    model_id: str = "scripted-model",
    max_runs_per_drive: int = 16,
) -> InMemoryRuntime:
    store = store if store is not None else InMemoryStore()
    data = InMemoryData(store)
    registry = ToolRegistry([AskUserTool(), *tools])
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
        request_options=RequestOptions(model_id=model_id),
        max_runs_per_drive=max_runs_per_drive,
    )
    return InMemoryRuntime(store=store, data=data, registry=registry, driver=driver)
