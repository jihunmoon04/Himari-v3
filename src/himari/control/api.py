"""Control public contract (CTRL-01, CTRL-05).

Source documents: docs/control-prompt-provider-v0.1.md §3, §5.1, §7;
docs/decisions.md V3-024, V3-044, V3-045.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from himari.common import CallRef, RunId, TrustedScope
from himari.data.api import ConversationEvent, RecoveryReport, UserMessage


class DriveState(Enum):
    COMPLETED = "completed"  # FINAL committed
    WAITING_FOR_USER = "waiting_for_user"  # ask_user pending; lock released
    RECOVERY_REQUIRED = "recovery_required"  # UNKNOWN effect / incomplete history blocks the next Run
    PROVIDER_FAILED = "provider_failed"  # certain: no response
    OUTCOME_UNKNOWN = "outcome_unknown"  # send uncertain; never resent
    INVALID_MODEL_OUTPUT = "invalid_model_output"  # raw kept, no effects
    REQUEST_REJECTED = "request_rejected"  # Prompt/Provider refused before send
    RUN_LIMIT_REACHED = "run_limit_reached"


@dataclass(frozen=True)
class DriveResult:
    state: DriveState
    run_ids: tuple[RunId, ...] = ()
    final_event: ConversationEvent | None = None
    waiting_call: CallRef | None = None
    unknown_calls: tuple[CallRef, ...] = ()
    recovery: RecoveryReport | None = None
    detail: str = ""


class ControlErrorCode(Enum):
    BUSY = "busy"  # another driver holds this Conversation in this process


class ControlError(Exception):
    def __init__(self, code: ControlErrorCode, message: str = "") -> None:
        super().__init__(f"{code.value}: {message}" if message else code.value)
        self.code = code


class ConversationDriver(Protocol):
    async def drive(self, scope: TrustedScope, message: UserMessage) -> DriveResult:
        """Record one user input (T7a if an ask_user call is waiting, else T1)
        and run the loop until FINAL, a wait, or a stop condition.
        Raises ControlError(BUSY) if the Conversation is already being driven."""
        ...
