"""Composition root: the only place that imports concrete implementations.

``build_runtime`` wires the P0 stack over a Data store: ``InMemoryStore``
(tests; a "restart" is a new runtime over the same object) or the durable
``SqliteStore`` (a restart is a new process — or a new store — opening the
same database file).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from himari.control.ask_user import AskUserTool
from himari.control.api import ModelCatalog, ModelOffer, PresenceSource
from himari.control.driver import Driver
from himari.common import TrustedScope
from himari.data.memory import InMemoryData, InMemoryStore
from himari.data.sqlite import SqliteData, SqliteStore
from himari.environment.api import EnvironmentPlugin
from himari.prompt.api import MEMORY_SECTION, TURN_GUIDANCE, ContextBudget, SourceNote, SystemSpec
from himari.prompt.compaction import Compactor, read_summary
from himari.prompt.composer import Composer
from himari.prompt.loader import ContextLoader
from himari.prompt.system import BASE_INSTRUCTIONS
from himari.provider.api import ProviderAdapter, ReadsImageCopies
from himari.data.api import DEFAULT_FILE_MAX_BYTES, ViewMaker
from himari.tool.access import ConfiguredExternalAccess
from himari.tool.api import ToolHandler
from himari.tool.dispatcher import Dispatcher
from himari.tool.images import StoredResultImages
from himari.tool.conversation_search import conversation_search_tools
from himari.tool.memory import memory_tools
from himari.tool.registry import ToolRegistry

DEFAULT_INSTRUCTIONS = BASE_INSTRUCTIONS  # V3-P89 §3.1: the operator's base; its own replaces only this


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
    models: ModelCatalog  # the offered models (V3-P64)


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
    no_progress_runs: int | None = None,  # None: no NO_PROGRESS stop; V3-P62's default is NO_PROGRESS_RUNS
    allow_require_user_tools: bool = False,  # keep False in P0 (no resolution API)
    reasoning_effort: str | None = None,
    context_budget: ContextBudget | None = None,  # None: no compaction or size check
    # The offered models (V3-P64). None: ``model_id`` alone, with
    # ``reasoning_effort`` and ``context_budget``; given, those three are unused.
    models: ModelCatalog | None = None,
    # External Tool source → enabled by default (V3-P53); a Conversation's own
    # choice overrides it. None: every registered source, on by default.
    external_defaults: Mapping[str, bool] | None = None,
    source_notes: Sequence[SourceNote] = (),  # tool server notes (V3-P60); used for enabled sources only
    image_views: ViewMaker | None = None,  # model copies of images (V3-P67); None: images are not prepared
    file_max_bytes: int = DEFAULT_FILE_MAX_BYTES,
    memory: bool = False,  # V3-P71: the memory Tools, the memory section and the notes in the state
    conversation_search: bool = False,  # V3-P72: conversation_search and conversation_read
    streaming: bool = False,  # V3-P73: answering Runs ask for a stream (models that declare it)
    time_zone: str | None = None,  # V3-P83 §6.4: the turn's time and the time lines; None: the model is not told
    presence: PresenceSource | None = None,  # V3-P83 §6.2: where the user is; None: the model is not told
    # V3-P89 §6: what the system text's sections follow besides ``memory`` and ``turn_budget``
    # (which this sets); the text and its order are the Prompt module's. None: ``instructions``
    # as given, with no sections — only the memory section and the turn rule are added after
    # them as before (callers with their own base text: tests). Application.open always gives one.
    system: SystemSpec | None = None,
) -> Runtime:
    store = store if store is not None else InMemoryStore()
    data = SqliteData(store) if isinstance(store, SqliteStore) else InMemoryData(store)

    async def compacted_through(scope: TrustedScope) -> int | None:
        """V3-P72 §3.2: the last Event this Conversation's compaction summary
        covers (as ContextStatus reads it), or None when it has none."""
        snapshot = await data.get_latest_snapshot(scope)
        return snapshot.covers_through_sequence if snapshot is not None and read_summary(snapshot) else None

    registry = ToolRegistry([AskUserTool(), *tools, *(memory_tools(data, time_zone=time_zone or "UTC") if memory else ()),
                             *(conversation_search_tools(data, compacted_through) if conversation_search else ())],
                            allow_require_user=allow_require_user_tools)
    environments = tuple(environments)
    for env in environments:
        for handler in env.tools():
            registry.register(handler)
    sources = registry.external_sources()
    defaults = dict.fromkeys(sources, True) if external_defaults is None else dict(external_defaults)
    if not sources <= defaults.keys():
        raise ValueError(f"no default for the external sources {sorted(sources - defaults.keys())}")
    access = ConfiguredExternalAccess(defaults, data)
    dispatcher = Dispatcher(registry, data, data, data, access,
                            images=StoredResultImages(data.blobs, data, image_views, file_max_bytes))
    if isinstance(provider, ReadsImageCopies):  # V3-P67 §7.4: image copies are read when a request is sent
        provider.use_blobs(data.blobs.read_bytes)
    if models is None:
        capabilities = provider.describe_capabilities(model_id)
        models = ModelCatalog((ModelOffer(model_id, reasoning_effort, capabilities.reasoning_efforts, context_budget,
                                          capabilities.max_output_tokens),), model_id)
    # the turn's rule is in the system text when the turn guards are on (V3-P62 §8)
    spec = None if system is None else replace(system, memory=memory, turn_guidance=turn_budget is not None)
    if spec is None:
        instructions = (*instructions, *((MEMORY_SECTION,) if memory else ()),
                        *((TURN_GUIDANCE,) if turn_budget is not None else ()))
    driver = Driver(
        log=data,
        loader=ContextLoader(data, data, registry, instructions, access, source_notes, files=data,
                             environments={env.kind: env for env in environments}, time_zone=time_zone,
                             system=spec),
        composer=Composer(),
        provider=provider,
        dispatcher=dispatcher,
        models=models,
        settings=data,
        user_instructions=data,
        memories=data if memory else None,
        max_runs_per_drive=max_runs_per_drive,
        planner=Compactor() if any(o.budget is not None for o in models.offers) else None,
        turn_budget=turn_budget,
        no_progress_runs=no_progress_runs,
        streaming=streaming,
        time_zone=time_zone,
        presence=presence,
    )
    return Runtime(store=store, data=data, registry=registry, driver=driver, access=access, models=models)


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
