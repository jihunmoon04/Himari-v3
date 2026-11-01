"""V3-P87 §5 (triggers), §6 (rules), §6.4 (merging): pure functions.

Every trigger gives a ``Candidate``; ``decide`` keeps those whose key no wake
took yet, applies the rules and merges what may speak into one wake.
Reminders (§5.3) take the rules' exceptions: they come with part 2b, the
rules here know them already.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from himari.data.api import MAX_WAKE_REASONS, Memory, MemoryCategory, WakeKind, WakeReason, WakeRecord, WakeStatus
from himari.presence.api import DeviceState

CHECK_IN_WINDOW = timedelta(hours=24)  # §5.1: a loop is spoken of within a day of its moment, else expired
RETURN_FRESH = timedelta(minutes=10)  # §5.2: a return older than this is not greeted
RETURN_WAIT = timedelta(seconds=20)  # §5.2: the user may speak first after an unlock
TALKING = timedelta(minutes=10)  # §6.1: a user message this recent means the user is talking
GAME_RECENT = timedelta(minutes=10)  # §5.4: a game that ended this recently
ACTIVITY_FOCUS = frozenset({"coding", "document", "design", "video"})  # §5.4 (video: owner, 2026-11-01)
# §6.4: the order reasons are told in, and kept when over MAX_WAKE_REASONS
MERGE_ORDER = (WakeKind.REMINDER, WakeKind.LOOP_DUE, WakeKind.FOCUS, WakeKind.GAME_ENDED, WakeKind.RETURNED)


@dataclass(frozen=True)
class ProactiveSettings:
    """V3-P87 §11, with the owner's numbers (§15; changed 2026-11-01: gap 30 min, focus 30 min)."""

    enabled: bool = False  # HIMARI_PROACTIVE
    quiet_start: time | None = time(23, 0)  # HIMARI_PROACTIVE_QUIET "23:00-08:00"; None: no quiet hours
    quiet_end: time | None = time(8, 0)
    gap_minutes: int = 30
    daily: int = 4
    return_hours: int = 4  # 0: no greeting on a return
    focus_minutes: int = 30  # 0: no FOCUS
    game_minutes: int = 30  # 0: no GAME_ENDED

    def describe(self) -> str:
        """The startup line and the status (§11)."""
        if not self.enabled:
            return "off (HIMARI_PROACTIVE=0)"
        quiet = "no quiet hours" if self.quiet_start is None or self.quiet_end is None else \
            f"quiet {self.quiet_start:%H:%M}-{self.quiet_end:%H:%M}"
        return f"on ({quiet}, gap {self.gap_minutes}m, {self.daily} a day)"


@dataclass(frozen=True)
class Return:
    """§5.2: a return Presence recorded, with how long the user was away —
    from the latest input any device recorded before it; None when none was."""

    device_id: str
    at: datetime
    via: str
    away_minutes: int | None


@dataclass(frozen=True)
class Candidate:
    reason: WakeReason
    key: str | None  # taken by the wake (§3.3); a reminder has none (§5.3)
    since: datetime  # when it became due: loops are told in this order
    aliases: tuple[str, ...] = ()  # the same moment a minute either side: taken, it is taken (clock jitter)

    @property
    def keys(self) -> tuple[str, ...]:
        return ((self.key,) if self.key is not None else ()) + self.aliases


@dataclass(frozen=True)
class Situation:
    """What the scheduler read for one principal, at ``now``."""

    now: datetime
    settings: ProactiveSettings
    zone: str  # the current device's time zone, else the server's
    device_id: str | None  # the current device
    device: DeviceState | None  # what Presence knows of it
    quiet_until: datetime | None  # the user's quiet, still on
    last_user_message_at: datetime | None  # in any conversation
    working: bool  # a job of the principal runs
    unanswered_wake: bool  # the most recent conversation ends with a wake turn's answer, not answered yet
    wakes: tuple[WakeRecord, ...]  # the wakes of the last day and more
    loops: tuple[Memory, ...]
    returned: Return | None


@dataclass(frozen=True)
class Decision:
    """What to do now. ``reasons`` empty: speak not. ``blocked``: the rule that held
    back what was due (None when nothing was). ``expired``: keys to take with no wake."""

    reasons: tuple[WakeReason, ...] = ()
    keys: tuple[str, ...] = ()
    reminders: tuple[int, ...] = ()
    expired: tuple[str, ...] = ()
    blocked: str | None = None
    held: tuple[Candidate, ...] = ()  # due, but held back by ``blocked`` (or past the reasons' limit)
    next_at: datetime | None = None  # the next loop's moment, for the status


def _minute(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _around(prefix: str, moment: datetime) -> tuple[str, tuple[str, ...]]:
    """A key at the minute and its neighbours: a moment counted back from a duration
    moves by a second or so from tick to tick, and may cross a minute."""
    key = f"{prefix}:{_minute(moment)}"
    return key, (f"{prefix}:{_minute(moment - timedelta(minutes=1))}",
                 f"{prefix}:{_minute(moment + timedelta(minutes=1))}")


def loop_moment(note: Memory) -> datetime | None:
    """§5.1 (Q4): a time an hour after it; a date only at 18:00 of that day in
    the zone it was written in (its instant is that day's last moment there)."""
    if note.category is not MemoryCategory.LOOP or note.due_at is None or note.due_instant is None:
        return None
    if len(note.due_at) == 10:  # a date: due_instant is its end of day in the zone
        return note.due_instant + timedelta(microseconds=1) - timedelta(hours=6)
    return note.due_instant + timedelta(hours=1)


def loop_candidates(loops: Iterable[Memory], now: datetime
                    ) -> tuple[list[Candidate], list[str], datetime | None]:
    """§5.1: the loops due now (within CHECK_IN_WINDOW of their moment), the keys of
    those past it, and the next moment to come."""
    due: list[Candidate] = []
    expired: list[str] = []
    upcoming: datetime | None = None
    for note in loops:
        moment = loop_moment(note)
        if moment is None:
            continue
        key = f"loop:m{note.memory_id}:{note.due_at}"
        if now < moment:
            upcoming = moment if upcoming is None else min(upcoming, moment)
        elif now - moment < CHECK_IN_WINDOW:
            due.append(Candidate(WakeReason(WakeKind.LOOP_DUE, memory_id=note.memory_id), key, moment))
        else:
            expired.append(key)
    return due, expired, upcoming


def return_candidate(returned: Return | None, device_id: str | None, last_user_message_at: datetime | None,
                     settings: ProactiveSettings, now: datetime) -> Candidate | None:
    """§5.2 (Q3): greeted when the user was away RETURN_HOURS or more, said nothing
    meanwhile (nor in the RETURN_WAIT after it), on the device that is still the
    current one, and the record is fresh."""
    if returned is None or settings.return_hours <= 0 or returned.away_minutes is None:
        return None
    if returned.device_id != device_id or not RETURN_WAIT <= now - returned.at <= RETURN_FRESH:
        return None
    if returned.away_minutes < settings.return_hours * 60:
        return None
    away_from = returned.at - timedelta(minutes=returned.away_minutes)
    if last_user_message_at is not None and last_user_message_at > away_from:
        return None  # back already (the Web, the CLI), or spoke first after the unlock
    return Candidate(WakeReason(WakeKind.RETURNED, away_minutes=returned.away_minutes, via=returned.via),
                     f"return:{returned.device_id}:{_minute(returned.at)}", returned.at)


def activity_candidates(device_id: str | None, device: DeviceState | None, settings: ProactiveSettings,
                        now: datetime) -> list[Candidate]:
    """§5.4: a long focus (once per stretch) and a game that just ended."""
    if device_id is None or device is None:
        return []
    out: list[Candidate] = []
    activity = device.activity
    if (settings.focus_minutes > 0 and activity is not None and activity.category in ACTIVITY_FOCUS
            and activity.for_seconds >= settings.focus_minutes * 60):
        started = now - timedelta(seconds=activity.for_seconds)
        key, aliases = _around(f"focus:{device_id}", started)
        out.append(Candidate(WakeReason(WakeKind.FOCUS, activity=activity.category,
                                        minutes=int(activity.for_seconds // 60)), key, now, aliases))
    before = device.before
    if (settings.game_minutes > 0 and before is not None and before.category == "game"
            and before.lasted_seconds >= settings.game_minutes * 60
            and before.ended_seconds_ago <= GAME_RECENT.total_seconds()):
        ended = now - timedelta(seconds=before.ended_seconds_ago)
        key, aliases = _around(f"game:{device_id}", ended)
        out.append(Candidate(WakeReason(WakeKind.GAME_ENDED, minutes=int(before.lasted_seconds // 60)),
                             key, ended, aliases))
    return out


def candidates(s: Situation) -> tuple[list[Candidate], list[str], datetime | None]:
    """Every trigger's candidate (§5), the loop keys to expire, the next loop moment."""
    due, expired, upcoming = loop_candidates(s.loops, s.now)
    found = list(due)
    back = return_candidate(s.returned, s.device_id, s.last_user_message_at, s.settings, s.now)
    if back is not None:
        found.append(back)
    found += activity_candidates(s.device_id, s.device, s.settings, s.now)
    return found, expired, upcoming


def in_quiet_hours(now: datetime, zone: str, start: time | None, end: time | None) -> bool:
    """§6.2: in the device's zone; a range over midnight (23:00-08:00) wraps."""
    if start is None or end is None or start == end:
        return False
    local = now.astimezone(ZoneInfo(zone)).time()
    return start <= local < end if start < end else (local >= start or local < end)


def local_midnight(now: datetime, zone: str) -> datetime:
    """The start of ``now``'s day in ``zone`` (the daily limit's day, §6.2)."""
    local = now.astimezone(ZoneInfo(zone))
    return datetime.combine(local.date(), time(), tzinfo=ZoneInfo(zone)).astimezone(UTC)


def _only(record: WakeRecord, kinds: frozenset[WakeKind]) -> bool:
    return all(r.kind in kinds for r in record.reasons)


_TRY = frozenset({WakeKind.TRY})
_REMINDER = frozenset({WakeKind.REMINDER})


def spoke(record: WakeRecord) -> bool:
    """§6.2: a wake the gap and the daily limit count — one that spoke or is speaking.
    Not one that said nothing (failed, or cancelled as the user won: PR #156 review
    P1, a wake cut by a restart is tried again at once), nor the operator's try (§11)."""
    return record.status in (WakeStatus.OPENED, WakeStatus.ANSWERED) and not _only(record, _TRY)


def _blocked(s: Situation, reminders_only: bool) -> str | None:
    """§6.1–§6.2 in order: the first rule that keeps what is due from being spoken.
    A reminder (``reminders_only``) passes the user's quiet (Q8), the talking
    rule, an unanswered wake, the quiet hours, the gap and the daily limit."""
    if s.device_id is None:
        return "no_device"
    if s.working:
        return "working"
    if reminders_only:
        return None
    if s.quiet_until is not None and s.quiet_until > s.now:
        return "quiet"
    if s.last_user_message_at is not None and s.now - s.last_user_message_at < TALKING:
        return "talking"
    if s.unanswered_wake:
        return "unanswered"
    if in_quiet_hours(s.now, s.zone, s.settings.quiet_start, s.settings.quiet_end):
        return "quiet_hours"
    counted = [w for w in s.wakes if spoke(w)]
    last = max((w.created_at for w in counted), default=None)
    if last is not None and s.now - last < timedelta(minutes=s.settings.gap_minutes):
        return "gap"
    midnight = local_midnight(s.now, s.zone)
    today = [w for w in counted if w.created_at >= midnight and not _only(w, _REMINDER)]
    if len(today) >= s.settings.daily:
        return "daily"
    return None


def decide(s: Situation, taken: frozenset[str] = frozenset()) -> Decision:
    """§5–§6.4: what to speak of now, in one wake, or why not. ``taken``: those of the
    candidates' keys (``candidates``) a wake took or that expired."""
    found, expired, upcoming = candidates(s)
    due = [c for c in found if not taken.intersection(c.keys)]
    expired_now = tuple(k for k in expired if k not in taken)
    if not due:
        return Decision(expired=expired_now, next_at=upcoming)
    reminders = [c for c in due if c.reason.kind is WakeKind.REMINDER]
    blocked = _blocked(s, reminders_only=False)
    speakable = due if blocked is None else (reminders if _blocked(s, reminders_only=True) is None else [])
    if not speakable:
        return Decision(expired=expired_now, blocked=blocked, held=tuple(due), next_at=upcoming)
    ordered = sorted(speakable, key=lambda c: (MERGE_ORDER.index(c.reason.kind), c.since))
    chosen, over = ordered[:MAX_WAKE_REASONS], ordered[MAX_WAKE_REASONS:]
    held = tuple(over) + tuple(c for c in due if c not in speakable)
    return Decision(reasons=tuple(c.reason for c in chosen), keys=tuple(c.key for c in chosen if c.key is not None),
                    reminders=tuple(c.reason.memory_id for c in chosen if c.reason.kind is WakeKind.REMINDER
                                    and c.reason.memory_id is not None),
                    expired=expired_now, blocked=blocked if held else None, held=held, next_at=upcoming)


def candidate_keys(found: Sequence[Candidate], expired: Sequence[str]) -> tuple[str, ...]:
    """Every key ``decide`` asks about (to read ``taken`` in one query)."""
    return tuple(dict.fromkeys([k for c in found for k in c.keys] + list(expired)))
