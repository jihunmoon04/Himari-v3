"""HIMARI Web server: a JSON API over the assembled Application plus the
static UI (V3-P46).

Authentication: the local operator signs in with the operator secret
(``POST /api/session``) and gets an in-memory session in an ``HttpOnly``
``SameSite=Strict`` cookie; a CLI sends ``Authorization: Bearer`` with the
token derived from the secret. Every other API route requires one of the
two. Foreign or missing Conversations are indistinguishable (404).

The browser reads progress by polling: the job status of a Conversation
and its Events after the last sequence it has. Messages start background
jobs (202); a second message while one runs is refused (409).

Tool views (MCP Apps, V3-P58): events say which calls have one
(``app_view``); ``GET …/calls/{run}/{call}/app`` gives a view as JSON with a
signed URL of the sandbox origin's proxy (``sandbox.py``). The view's HTML
is never served from this origin.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from himari.bootstrap.application import (
    Application,
    AppViewRefused,
    AppViewState,
    ModelChoiceView,
    ModelRefused,
    ModelView,
    PromptRefused,
    PromptView,
    ToolSourceView,
    UnknownToolSource,
)
from himari.bootstrap.operator import OPERATOR, derive_cli_bearer, verify_operator_token
from himari.common import (
    ASK_USER_TOOL_ID,
    AttachmentRef,
    CallId,
    CallRef,
    ContentBlock,
    ConversationId,
    PrincipalId,
    RunId,
    TextBlock,
    TrustedScope,
    thaw,
)
from himari.control.api import ContextStatus, ControlError, ControlErrorCode, JobStatus, TurnUsage
from himari.data.api import (
    MAX_LIST_LIMIT,
    AssistantFinal,
    AssistantToolCalls,
    ConversationEvent,
    ConversationSummary,
    DataError,
    DataErrorCode,
    ToolResultRecord,
    UserMessage,
)
from himari.interfaces.web.config import WebConfig
from himari.interfaces.web.sandbox import SandboxGrant, SandboxSigner
from himari.interfaces.web.security import (
    SESSION_COOKIE,
    BodyLimitMiddleware,
    OriginGuardMiddleware,
    SecurityHeadersMiddleware,
    error_body,
    page_csp,
)

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 32_000
MAX_EVENTS_PAGE = 500
SESSION_SECONDS = 7 * 24 * 3600
FAILED_LOGIN_DELAY_SECONDS = 1.0
STATIC_FILES = {"app.css": "text/css", "app.js": "text/javascript", "app_view.js": "text/javascript",
                "rich_text.js": "text/javascript"}

Opener = Callable[[], Awaitable[Application]]


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- sessions ---------------------------------------------------------------------------


class Sessions:
    """In-memory browser sessions: a restart signs everyone out."""

    def __init__(self, lifetime: float = SESSION_SECONDS) -> None:
        self._lifetime = lifetime
        self._by_id: dict[str, tuple[PrincipalId, float]] = {}

    def create(self, principal: PrincipalId) -> str:
        session_id = secrets.token_urlsafe(32)
        self._by_id[session_id] = (principal, time.monotonic() + self._lifetime)
        return session_id

    def principal(self, session_id: str | None) -> PrincipalId | None:
        if not session_id:
            return None
        entry = self._by_id.get(session_id)
        if entry is None:
            return None
        if entry[1] < time.monotonic():
            del self._by_id[session_id]
            return None
        return entry[0]

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._by_id.pop(session_id, None)


# --- request bodies ------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoginBody(_Body):
    token: str = Field(max_length=256)


class CreateBody(_Body):
    title: str | None = Field(default=None, max_length=1000)


class RenameBody(_Body):
    title: str | None = Field(max_length=1000)


class MessageBody(_Body):
    text: str


class ModelBody(_Body):
    model: str | None = Field(max_length=200)  # null: follow the default model
    effort: str | None = Field(default=None, max_length=32)  # null: the model's default effort


class ToolSourceBody(_Body):
    enabled: bool | None  # null: follow the source's default


class PromptExpandBody(_Body):
    """In the body, not the path: a prompt's name may hold "/" (V3-P60 C4)."""

    source: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, str] = Field(default_factory=dict, max_length=64)


# --- JSON views ------------------------------------------------------------------------------


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _text(blocks: tuple[ContentBlock, ...]) -> str:
    return "\n\n".join(b.text if isinstance(b, TextBlock) else f"[attachment {b.file_id}]"
                       for b in blocks if isinstance(b, (TextBlock, AttachmentRef)))


def summary_view(summary: ConversationSummary) -> dict[str, Any]:
    return {
        "id": summary.conversation_id,
        "title": summary.title,
        "preview": summary.preview,
        "created_at": _time(summary.created_at),
        "last_activity_at": _time(summary.last_activity_at),
        "last_sequence": summary.last_sequence,
    }


def event_view(event: ConversationEvent, views: dict[CallRef, tuple[bool, bool]] | None = None,
               models: dict[RunId, str] | None = None) -> dict[str, Any]:
    """``views``: Application.view_flags for the page — (shown while it
    runs, stored with its result) per call with a view that may be shown.
    ``models``: the model of each Run that wrote an assistant event (V3-P64 §7)."""
    views = {} if views is None else views
    models = {} if models is None else models
    view: dict[str, Any] = {"sequence": event.sequence, "kind": event.kind.value, "created_at": _time(event.created_at)}
    payload = event.payload
    if isinstance(payload, UserMessage):
        view["text"] = _text(payload.blocks)
        # A call is identified by (run_id, call_id): call ids repeat across Runs.
        view["answers_call"] = None if payload.answers_call is None else payload.answers_call.call_id
        view["answers_run"] = None if payload.answers_call is None else payload.answers_call.run_id
    elif isinstance(payload, AssistantFinal):
        view["text"] = _text(payload.blocks)
        view["model"] = models.get(event.origin_run_id) if event.origin_run_id else None
    elif isinstance(payload, AssistantToolCalls):
        view["text"] = _text(payload.commentary)
        view["model"] = models.get(event.origin_run_id) if event.origin_run_id else None
        view["run_id"] = event.origin_run_id
        run = event.origin_run_id
        view["calls"] = [{"call_id": c.call_id, "tool_id": c.tool_id, "arguments": thaw(c.arguments),
                          "app_view": run is not None and views.get(CallRef(run, c.call_id), (False, False))[0]}
                         for c in sorted(payload.calls, key=lambda c: c.ordinal)]
    elif isinstance(payload, ToolResultRecord):
        view["run_id"] = payload.call.run_id
        view["call_id"] = payload.call.call_id
        view["is_error"] = payload.model_result.is_error
        view["text"] = _text(payload.model_result.blocks)
        view["app_view"] = views.get(payload.call, (False, False))[1]
    return view


def app_view_view(state: AppViewState, sandbox_url: str | None) -> dict[str, Any]:
    view: dict[str, Any] = {
        "state": state.state, "source": state.source, "tool_id": state.tool_id,
        "tool": None if state.tool is None else thaw(state.tool),
        "arguments": thaw(state.arguments), "status": state.status.value,
    }
    if state.resource is None:  # ended without a view: how to close it, and why
        view["outcome"], view["reason"] = state.outcome, state.reason
        return view
    declared = state.resource.csp
    view.update({
        "digest": state.resource.digest, "html": state.resource.html, "prefers_border": state.resource.prefers_border,
        "csp": {"connect_domains": list(state.csp.connect_domains),
                "resource_domains": list(state.csp.resource_domains)},
        "declared_csp": {"connect_domains": list(declared.connect_domains),
                         "resource_domains": list(declared.resource_domains),
                         "frame_domains": list(declared.frame_domains),
                         "base_uri_domains": list(declared.base_uri_domains)},
        "sandbox_url": sandbox_url,
        "result": None if state.state != "done" else thaw(state.result),
    })
    return view


MAX_DETAIL_CHARS = 500  # a job's result detail, as shown to its owner


def job_view(status: JobStatus) -> dict[str, Any]:
    result = status.result
    detail = None if result is None or not result.detail else result.detail
    if detail is not None and len(detail) > MAX_DETAIL_CHARS:
        detail = detail[: MAX_DETAIL_CHARS - 1] + "…"
    return {
        "running": status.running,
        "generation": status.generation,
        "state": None if result is None else result.state.value,
        "detail": detail,  # why it ended so (e.g. the size that was refused); plain text
        "unknown_calls": 0 if result is None else len(result.unknown_calls),
        "compactions": 0 if result is None else len(result.compaction_run_ids),
        "error": status.error,
        "started_at": _time(status.started_at),
        "finished_at": _time(status.finished_at),
        "stop_requested": None if status.stop_requested is None else status.stop_requested.value,
        "turn_usage": None if status.turn_usage is None else turn_usage_view(status.turn_usage),
    }


def turn_usage_view(usage: TurnUsage) -> dict[str, Any]:
    """What the job's drive used so far (V3-P62 §7)."""
    return {"runs": usage.runs, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cached_input_tokens": usage.cached_input_tokens, "cache_write_tokens": usage.cache_write_tokens,
            "budget": usage.budget, "max_runs": usage.max_runs}


def context_view(status: ContextStatus | None) -> dict[str, Any]:
    """The model's view of a Conversation's size, for display. Events up to
    ``covered_through_sequence`` reach the model only through ``summary``."""
    if status is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "estimated_tokens": status.estimated_tokens,
        "measured": status.measured,
        "budget_tokens": status.budget_tokens,
        "compact_at_tokens": status.compact_at_tokens,
        "hard_limit_tokens": status.hard_limit_tokens,
        "compactions": status.compactions,
        "covered_through_sequence": status.covered_through_sequence,
        "summary": status.summary,
    }


def model_view(view: ModelView) -> dict[str, Any]:
    return {"id": view.model_id, "default_effort": view.default_effort, "efforts": list(view.efforts),
            "window_known": view.window_known, "default": view.is_default}


def model_choice_view(view: ModelChoiceView) -> dict[str, Any]:
    chosen = view.chosen
    return {"model": view.model_id, "effort": view.effort,
            "chosen": None if chosen is None else {"model": chosen.model_id, "effort": chosen.reasoning_effort},
            "fallback": view.fallback}


# How a refused model choice answers (V3-P64 §4.3).
MODEL_REFUSAL_STATUS = {"unknown_model": 404, "invalid_effort": 422, "invalid_argument": 400}


def tool_source_view(view: ToolSourceView) -> dict[str, Any]:
    return {
        "source": view.source,
        "enabled": view.enabled,
        "default": view.enabled_by_default,
        "choice": view.choice,
        "available": view.available,
        "tools": list(view.tool_ids),
        "problems": list(view.problems),
    }


def prompt_view(view: PromptView) -> dict[str, Any]:
    return {
        "source": view.source,
        "name": view.name,
        "title": view.title,
        "description": view.description,
        "arguments": [{"name": a.name, "description": a.description, "required": a.required}
                      for a in view.arguments],
    }


# How a refused prompt answers (V3-P60 §4).
PROMPT_REFUSAL_STATUS = {"unknown_prompt": 404, "source_disabled": 409, "invalid_arguments": 400,
                         "unsupported_content": 422, "server_error": 502}


# --- the application ---------------------------------------------------------------------------


def create_app(config: WebConfig, opener: Opener, signer: SandboxSigner | None = None) -> FastAPI:
    """``signer``: shared with the sandbox server (``sandbox.py``); made here
    when not given (one per process, so a restart voids old view URLs)."""
    sessions = Sessions()
    signer = SandboxSigner() if signer is None else signer

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        application = await opener()
        app.state.himari = application
        app.state.bearer = derive_cli_bearer(application.operator_secret)
        try:
            yield
        finally:
            await application.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def himari(request: Request) -> Application:
        application: Application = request.app.state.himari
        return application

    def principal(request: Request) -> PrincipalId:
        found = sessions.principal(request.cookies.get(SESSION_COOKIE))
        if found is not None:
            return found
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and verify_operator_token(token.strip(), request.app.state.bearer):
            return OPERATOR
        raise ApiError(401, "unauthenticated", "Sign in first.")

    def scope_of(conversation_id: str, who: PrincipalId) -> TrustedScope:
        return TrustedScope(who, ConversationId(conversation_id))

    # -- errors: a fixed envelope, never echoing input or internals --------------------

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)

    @app.exception_handler(DataError)
    async def data_error(request: Request, error: DataError) -> JSONResponse:
        if error.code in (DataErrorCode.NOT_AUTHORIZED, DataErrorCode.NOT_FOUND):
            return JSONResponse({"error": {"code": "not_found", "message": "No such conversation."}}, status_code=404)
        if error.code is DataErrorCode.INVALID_ARGUMENT:
            return JSONResponse({"error": {"code": "invalid_argument", "message": str(error)}}, status_code=400)
        log.error("unexpected data error: %s", error.code.value)
        return JSONResponse({"error": {"code": "conflict", "message": "The request conflicts with the current state."}},
                            status_code=409)

    @app.exception_handler(ControlError)
    async def control_error(request: Request, error: ControlError) -> JSONResponse:
        if error.code is ControlErrorCode.BUSY:
            return JSONResponse({"error": {"code": "busy", "message": "This conversation is still working."}},
                                status_code=409)
        return JSONResponse({"error": {"code": "conflict", "message": "Not possible right now."}}, status_code=409)

    @app.exception_handler(AppViewRefused)
    async def no_view(request: Request, error: AppViewRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": "No view to show for this call."}},
                            status_code=404)

    @app.exception_handler(PromptRefused)
    async def prompt_refused(request: Request, error: PromptRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=PROMPT_REFUSAL_STATUS.get(error.code, 409))

    @app.exception_handler(ModelRefused)
    async def model_refused(request: Request, error: ModelRefused) -> JSONResponse:
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=MODEL_REFUSAL_STATUS.get(error.code, 400))

    @app.exception_handler(UnknownToolSource)
    async def unknown_source(request: Request, error: UnknownToolSource) -> JSONResponse:
        return JSONResponse({"error": {"code": "not_found", "message": "No such tool source."}}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "invalid_request", "message": "The request is not valid."}},
                            status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> Response:
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        return Response(error_body(code, "Not available."), status_code=error.status_code,
                        media_type="application/json")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        log.exception("unexpected error on %s", request.url.path)
        return JSONResponse({"error": {"code": "internal_error", "message": "Something went wrong."}}, status_code=500)

    # -- session -----------------------------------------------------------------------------

    @app.get("/api/session")
    async def read_session(request: Request) -> dict[str, Any]:
        application = himari(request)
        signed_in = sessions.principal(request.cookies.get(SESSION_COOKIE)) is not None
        return {"authenticated": signed_in, "model": application.model_id if signed_in else None,
                "sandbox": application.current_sandbox_state.value if signed_in else None}

    @app.get("/api/server")
    async def read_server(request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """What this server runs with: the model, whether commands can run, the budget."""
        application = himari(request)
        turn = application.turn
        return {"model": application.model_id, "sandbox": application.current_sandbox_state.value,
                "models": [model_view(m) for m in application.models],
                "context_budget": application.context_tokens,  # the default model's
                "turn": {"token_budget": turn.turn_budget, "unattended_runs": turn.unattended_runs,
                         "max_runs": turn.max_runs, "no_progress_runs": turn.no_progress_runs,
                         "seen_seconds": turn.seen_seconds}}

    @app.post("/api/session", status_code=204)
    async def sign_in(body: LoginBody, request: Request, response: Response) -> None:
        if not verify_operator_token(body.token.strip(), himari(request).operator_secret):
            await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
            raise ApiError(401, "invalid_token", "That token is not valid.")
        sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.set_cookie(SESSION_COOKIE, sessions.create(OPERATOR), max_age=SESSION_SECONDS, path="/",
                            httponly=True, samesite="strict", secure=config.secure_cookie)

    @app.delete("/api/session", status_code=204)
    async def sign_out(request: Request, response: Response) -> None:
        sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict", secure=config.secure_cookie)

    # -- conversations -----------------------------------------------------------------------

    @app.get("/api/conversations")
    async def list_conversations(request: Request, who: PrincipalId = Depends(principal),
                                 limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
            raise ApiError(400, "invalid_argument", f"limit must be 1..{MAX_LIST_LIMIT}, offset >= 0")
        page = await himari(request).data.list_conversations(who, limit=limit, offset=offset)
        return {"items": [summary_view(s) for s in page],
                "next_offset": offset + limit if len(page) == limit else None}  # may lead to an empty page

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(body: CreateBody, request: Request,
                                  who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application = himari(request)
        cid = await application.create_conversation(who, title=body.title)
        return summary_view(await application.data.read_conversation(scope_of(cid, who)))

    async def _detail(application: Application, scope: TrustedScope) -> dict[str, Any]:
        summary = await application.data.read_conversation(scope)  # authorizes first
        application.seen(scope)  # a client is reading (V3-P62 §5)
        waiting = await application.data.find_waiting_call(scope)
        question = None
        if waiting is not None:
            for record in await application.data.read_tool_executions(scope, waiting.run_id):
                if record.call == waiting and record.tool_id == ASK_USER_TOOL_ID:
                    asked = record.arguments.get("question")
                    question = {"call_id": waiting.call_id, "question": asked if isinstance(asked, str) else ""}
        job = application.runner.status(scope)
        # An accepted input (or finished Tool results) with no assistant
        # output after it — e.g. the server stopped before the job ran.
        last = await application.data.read_events(scope, after_sequence=summary.last_sequence - 1, limit=1) \
            if summary.last_sequence else ()
        continuable = (not job.running and question is None and bool(last)
                       and last[-1].kind.value in ("user_message", "tool_result", "assistant_tool_calls"))
        return {"conversation": summary_view(summary), "job": job_view(job), "waiting": question,
                "continuable": continuable}

    @app.get("/api/conversations/{conversation_id}")
    async def read_conversation(conversation_id: str, request: Request,
                                who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        return await _detail(himari(request), scope_of(conversation_id, who))

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(conversation_id: str, body: RenameBody, request: Request,
                                  who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.rename_conversation(scope, body.title)
        return summary_view(await application.data.read_conversation(scope))

    @app.get("/api/conversations/{conversation_id}/events")
    async def read_events(conversation_id: str, request: Request, who: PrincipalId = Depends(principal),
                          after: int = 0, limit: int = 200) -> dict[str, Any]:
        if after < 0 or not 1 <= limit <= MAX_EVENTS_PAGE:
            raise ApiError(400, "invalid_argument", f"after >= 0 and limit 1..{MAX_EVENTS_PAGE}")
        application, scope = himari(request), scope_of(conversation_id, who)
        events = await application.data.read_events(scope, after_sequence=after, limit=limit)
        application.seen(scope)  # a client is reading (V3-P62 §5): only after the read was authorized
        runs = [e.origin_run_id for e in events if isinstance(e.payload, AssistantToolCalls) and e.origin_run_id]
        runs += [e.payload.call.run_id for e in events if isinstance(e.payload, ToolResultRecord)]
        views = await application.view_flags(scope, runs) if runs and application.views_configured else {}
        writers = [e.origin_run_id for e in events
                   if isinstance(e.payload, (AssistantFinal, AssistantToolCalls)) and e.origin_run_id]
        models = await application.run_models(scope, writers) if writers else {}
        return {"items": [event_view(e, views, models) for e in events],
                "last_sequence": events[-1].sequence if events else after}

    @app.get("/api/conversations/{conversation_id}/calls/{run_id}/{call_id}/app")
    async def read_app_view(conversation_id: str, run_id: str, call_id: str, request: Request, origin: str,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """A call's view (V3-P58 §6.1). ``origin``: the page's own origin,
        one of the allowed ones; the view is framed only by it."""
        if origin not in config.allowed_origins:
            raise ApiError(400, "invalid_argument", "origin must be this page's origin.")
        state = await himari(request).app_view(scope_of(conversation_id, who), CallRef(RunId(run_id), CallId(call_id)))
        url = None
        if state.resource is not None:
            grant = SandboxGrant(origin, state.csp.connect_domains, state.csp.resource_domains)
            url = f"{config.sandbox_origin(origin)}/sandbox?{signer.query(grant)}"
        return app_view_view(state, url)

    @app.get("/api/conversations/{conversation_id}/context")
    async def read_context(conversation_id: str, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        # Composes the whole history like a Run would: fetched on demand, not polled.
        return context_view(await himari(request).context_status(scope_of(conversation_id, who)))

    @app.get("/api/conversations/{conversation_id}/model")
    async def read_model(conversation_id: str, request: Request,
                         who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The model and effort the next turn uses, and the stored choice (V3-P64 §7)."""
        return model_choice_view(await himari(request).model_choice(scope_of(conversation_id, who)))

    @app.put("/api/conversations/{conversation_id}/model")
    async def set_model(conversation_id: str, body: ModelBody, request: Request,
                        who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Saved at once, used from the next turn (one under way keeps its model)."""
        view = await himari(request).set_model(scope_of(conversation_id, who), body.model, body.effort)
        return model_choice_view(view)

    @app.get("/api/conversations/{conversation_id}/tools")
    async def read_tool_sources(conversation_id: str, request: Request,
                                who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The configured MCP servers and whether this conversation uses them."""
        views = await himari(request).tool_sources(scope_of(conversation_id, who))
        return {"sources": [tool_source_view(v) for v in views]}

    @app.put("/api/conversations/{conversation_id}/tools/{source}")
    async def set_tool_source(conversation_id: str, source: str, body: ToolSourceBody, request: Request,
                              who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)  # 404 for a foreign id before anything else
        await application.set_tool_source(scope, source, body.enabled)
        return {"sources": [tool_source_view(v) for v in await application.tool_sources(scope)]}

    @app.get("/api/conversations/{conversation_id}/prompts")
    async def read_prompts(conversation_id: str, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The prompts of the servers on in this conversation (V3-P60 §4)."""
        return {"prompts": [prompt_view(v) for v in await himari(request).prompts(scope_of(conversation_id, who))]}

    @app.post("/api/conversations/{conversation_id}/prompts/expand")
    async def expand_prompt(conversation_id: str, body: PromptExpandBody, request: Request,
                            who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """The prompt as text for the composer; nothing is sent: the user
        reviews it and sends it as an ordinary message."""
        text = await himari(request).expand_prompt(scope_of(conversation_id, who), body.source, body.name,
                                                   body.arguments)
        return {"text": text}

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(conversation_id: str, body: MessageBody, request: Request,
                           who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        text = body.text.strip()
        if not text or len(text) > MAX_MESSAGE_CHARS:
            raise ApiError(400, "invalid_argument", f"A message is 1..{MAX_MESSAGE_CHARS} characters.")
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)  # 404 before a job is started for a foreign id
        return job_view(await application.runner.submit(scope, UserMessage((TextBlock(text),))))

    @app.post("/api/conversations/{conversation_id}/resume", status_code=202)
    async def resume(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(await application.runner.submit(scope, None))

    @app.post("/api/conversations/{conversation_id}/pause", status_code=202)
    async def pause(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Stop the running job at the next safe boundary (V3-P55); nothing if idle."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(application.runner.request_pause(scope))

    @app.post("/api/conversations/{conversation_id}/cancel", status_code=202)
    async def cancel(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Stop the running job now (V3-P55); nothing if idle."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(application.runner.cancel(scope))

    @app.post("/api/conversations/{conversation_id}/dismiss", status_code=202)
    async def dismiss(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Close what was left waiting — a question, or never-started steps
        of a paused or interrupted turn (V3-P55). Busy while a job runs."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(await application.runner.submit_cancel(scope))

    @app.post("/api/conversations/{conversation_id}/compact", status_code=202)
    async def compact(conversation_id: str, request: Request, who: PrincipalId = Depends(principal)) -> dict[str, Any]:
        """Summarize the older history now, whatever its size (no Run)."""
        application, scope = himari(request), scope_of(conversation_id, who)
        await application.data.read_conversation(scope)
        return job_view(await application.runner.submit_compaction(scope))

    # -- static UI ----------------------------------------------------------------------------

    static = resources.files("himari.interfaces.web") / "static"

    @app.get("/")
    async def index() -> Response:
        return Response((static / "index.html").read_bytes(), media_type="text/html; charset=utf-8")

    @app.get("/assets/{name}")
    async def asset(name: str) -> Response:
        media = STATIC_FILES.get(name)
        if media is None:
            raise ApiError(404, "not_found", "Not available.")
        return Response((static / name).read_bytes(), media_type=f"{media}; charset=utf-8")

    # Outer → inner: headers, Host, Origin, body size, routes.
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(OriginGuardMiddleware, allowed_origins=config.allowed_origins)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(config.allowed_hosts))
    app.add_middleware(SecurityHeadersMiddleware, csp=page_csp(config.sandbox_origins))
    return app
