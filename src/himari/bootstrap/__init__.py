"""Composition root: the only place that imports concrete implementations.

``build_runtime`` wires the P0 stack over a Data store: ``InMemoryStore``
(tests; a "restart" is a new runtime over the same object) or the durable
``SqliteStore`` (a restart is a new process — or a new store — opening the
same database file).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from himari.control.ask_user import AskUserTool
from himari.control.api import TURN_GUIDANCE
from himari.control.driver import NO_PROGRESS_RUNS, Driver
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore
from himari.environment.api import EnvironmentPlugin
from himari.prompt.api import RequestOptions, SourceNote
from himari.prompt.api import ContextBudget
from himari.prompt.compaction import Compactor
from himari.prompt.composer import Composer
from himari.prompt.loader import ContextLoader
from himari.provider.api import ProviderAdapter
from himari.tool.access import ConfiguredExternalAccess
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
    # The one ExternalAccess the catalog and Dispatcher ask; Tool views ask it
    # too (V3-P58 §3.7), so "is this source on here" has one answer.
    access: ConfiguredExternalAccess


def build_runtime(
    provider: ProviderAdapter,
    *,
    tools: Iterable[ToolHandler] = (),
    environments: Iterable[EnvironmentPlugin] = (),
    store: Store | None = None,
    instructions: Sequence[str] = DEFAULT_INSTRUCTIONS,
    model_id: str = "scripted-model",
    max_runs_per_drive: int | None = 16,  # None: no Run limit (then a turn_budget is needed, V3-P62)
    turn_budget: int | None = None,  # tokens a drive may use (V3-P62 §3); None: none
    no_progress_runs: int = NO_PROGRESS_RUNS,
    allow_require_user_tools: bool = False,  # keep False in P0 (no resolution API)
    reasoning_effort: str | None = None,
    context_budget: ContextBudget | None = None,  # None: no compaction or size check
    # External Tool source → enabled by default (V3-P53); a Conversation's own
    # choice overrides it. None: every registered source, on by default.
    external_defaults: Mapping[str, bool] | None = None,
    source_notes: Sequence[SourceNote] = (),  # tool server notes (V3-P60); used for enabled sources only
) -> Runtime:
    store = store if store is not None else InMemoryStore()
    data = SqliteData(store) if isinstance(store, SqliteStore) else InMemoryData(store)
    registry = ToolRegistry([AskUserTool(), *tools], allow_require_user=allow_require_user_tools)
    for env in environments:
        for handler in env.tools():
            registry.register(handler)
    sources = registry.external_sources()
    defaults = dict.fromkeys(sources, True) if external_defaults is None else dict(external_defaults)
    if not sources <= defaults.keys():
        raise ValueError(f"no default for the external sources {sorted(sources - defaults.keys())}")
    access = ConfiguredExternalAccess(defaults, data)
    dispatcher = Dispatcher(registry, data, data, data, access)
    if turn_budget is not None:  # the rule for the turn's state, fixed in the system prompt (V3-P62 §8)
        instructions = (*instructions, TURN_GUIDANCE)
    driver = Driver(
        log=data,
        loader=ContextLoader(data, data, registry, instructions, access, source_notes),
        composer=Composer(),
        provider=provider,
        dispatcher=dispatcher,
        request_options=RequestOptions(model_id=model_id, reasoning_effort=reasoning_effort),
        max_runs_per_drive=max_runs_per_drive,
        planner=Compactor() if context_budget is not None else None,
        budget=context_budget,
        turn_budget=turn_budget,
        no_progress_runs=no_progress_runs,
    )
    return Runtime(store=store, data=data, registry=registry, driver=driver, access=access)


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
