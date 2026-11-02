"""V3-P87 §5 (triggers), §6 (rules), §6.4 (merging): pure functions.

Every trigger gives a ``Candidate``; ``decide`` keeps those whose key no wake
took yet, applies the rules and merges what may speak into one wake.

V3-P90 §5: each kind of wake has only the rules that fit it. A reminder is
held back only by no device, work and a screen that is off — a sleeping
character wakes to tell it (then it is ``told`` until the user sees it); a
return (waking from sleep) by the user's quiet; a game or a video that just
ended (§5.3) also by talking, and is let go, not held, past ENDED_FRESH; a
loop also by an unanswered wake, the quiet hours and the gap; a free wake
(§6.2, §7) by all of those and its day's budget. A long focus and a game's
end by the program in front (V3-P87 §5.4) are made no more.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from himari.data.api import (
    MAX_WAKE_REASONS,
    REMIND_LATE,
    REMIND_ON_TIME,
    Memory,
    MemoryCategory,
    WakeKind,
    WakeReason,
    WakeRecord,
    WakeStatus,
    free_wake,
)
from himari.presence.api import DeviceState

CHECK_IN_WINDOW = timedelta(hours=24)  # §5.1: a loop is spoken of within a day of its moment, else expired
RETURN_FRESH = timedelta(minutes=10)  # §5.2: a return older than this is not greeted
RETURN_WAIT = timedelta(seconds=20)  # §5.2: the user may speak first after an unlock
# V3-P90 §5.2: an app that sends ``sleep`` checked the wake already — no wait, and a
# return held back is let go sooner
SLEEP_RETURN_FRESH = timedelta(minutes=3)
TOLD_SEEN = timedelta(minutes=10)  # V3-P90 §5.1: a return this soon after a told reminder saw it
TALKING = timedelta(minutes=10)  # §6.1: a user message this recent means the user is talking
ENDED_FRESH = timedelta(minutes=3)  # V3-P90 §5.3: a task's end is spoken of this soon, else let go
ENDED_GROUPS = frozenset({"game", "video"})  # V3-P90 §5.3: the ends the app can tell clearly
# V3-P90 §5: the kinds the talking rule holds back (a reminder and a return are not)
TALKING_KINDS = frozenset({WakeKind.LOOP_DUE, WakeKind.TASK_ENDED, WakeKind.FREE, WakeKind.FOCUS,
                           WakeKind.GAME_ENDED})
# §6.4, V3-P90 §4: the order reasons are told in, and kept when over MAX_WAKE_REASONS
MERGE_ORDER = (WakeKind.REMINDER, WakeKind.LOOP_DUE, WakeKind.TASK_ENDED, WakeKind.FREE, WakeKind.RETURNED)


@dataclass(frozen=True)
class ProactiveSettings:
    """V3-P87 §11, with the owner's numbers (§15; changed 2026-11-01: focus 30 min;
    V3-P90 §8: gap 15 min, a return after an hour away)."""

    enabled: bool = False  # HIMARI_PROACTIVE
    quiet_start: time | None = time(23, 0)  # HIMARI_PROACTIVE_QUIET "23:00-08:00"; None: no quiet hours
    quiet_end: time | None = time(8, 0)
    gap_minutes: int = 15  # V3-P90: for a loop and a free wake
    daily: int = 6  # V3-P90 §7.4: free wakes that spoke, a day
    attempts: int = 12  # V3-P90 §7.4: free wakes opened (passes too), a day — the cost's limit
    free_steps: tuple[int, ...] = (15, 30, 60)  # V3-P90 §6.2: minutes to each free wake, the last repeated; (): none
    return_hours: int = 1  # 0: no greeting on a return
    ended_minutes: int = 60  # V3-P90 §5.3: a game or a video this long; 0: no TASK_ENDED

    def describe(self) -> str:
        """The startup line and the status (§11)."""
        if not self.enabled:
            return "off (HIMARI_PROACTIVE=0)"
        quiet = "no quiet hours" if self.quiet_start is None or self.quiet_end is None else \
            f"quiet {self.quiet_start:%H:%M}-{self.quiet_end:%H:%M}"
        free = "no free wakes" if not self.free_steps else f"{self.daily} free a day"
        return f"on ({quiet}, gap {self.gap_minutes}m, {free})"


@dataclass(frozen=True)
class Return:
    """§5.2: a return Presence recorded, with how long the user was away —
    from the latest input any device recorded before it; None when none was."""

    device_id: str
    at: datetime
    via: str
    away_minutes: int | None


@dataclass(frozen=True)
class Told:
    """V3-P90 §5.1: a reminder told to a sleeping device, not seen yet: by which device, when.
    ``device_id`` None: its wake is gone (purged) — no return can show it was seen, so the
    next one tells it again."""

    memory_id: int
    device_id: str | None
    at: datetime


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
    said: frozenset[str] = frozenset()  # ended wakes (failed) whose turn left assistant output
    told: tuple[Told, ...] = ()  # V3-P90 §5.1: reminders told to a sleeping device, not seen yet
    free_ok: bool = False  # V3-P90 §7.5: the conversation's model was checked for free wakes


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
    seen: tuple[int, ...] = ()  # V3-P90 §5.1: told reminders a return shows the user saw


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


def _fresh(returned: Return | None, device_id: str | None, now: datetime, sleeps: bool = False) -> bool:
    """§5.2: a return on the device still current, past the user's RETURN_WAIT, not older than
    RETURN_FRESH. V3-P90 §5.2: on a device that sends ``sleep``, at once and for SLEEP_RETURN_FRESH."""
    wait, fresh = (timedelta(0), SLEEP_RETURN_FRESH) if sleeps else (RETURN_WAIT, RETURN_FRESH)
    return returned is not None and returned.device_id == device_id and wait <= now - returned.at <= fresh


def seen_reminders(told: Iterable[Told], returned: Return | None, device_id: str | None, now: datetime,
                   sleeps: bool = False) -> tuple[int, ...]:
    """V3-P90 §5.1: told reminders a fresh return shows the user saw — on the device they
    were told on, within TOLD_SEEN of the telling (the bubble was left up there)."""
    if returned is None or not _fresh(returned, device_id, now, sleeps):
        return ()
    return tuple(t.memory_id for t in told
                 if t.device_id == returned.device_id and timedelta(0) <= returned.at - t.at <= TOLD_SEEN)


def reminder_candidates(loops: Iterable[Memory], returned: Return | None, device_id: str | None,
                        now: datetime, *, sleeps: bool = False, asleep: bool = False,
                        told: Iterable[Told] = ()) -> list[Candidate]:
    """§5.3: a scheduled reminder whose moment came — on time within REMIND_ON_TIME; after
    that (Q9), late at a fresh return that came after its on-time window, until REMIND_LATE
    (then Data marks it missed). No key: its delivery state keeps it from repeating.
    V3-P90 §5.1: told to a sleeping device (``asleep``), it is marked so; a told one the
    user did not see is told again (``again``) at a fresh return."""
    told = tuple(told)
    fresh = _fresh(returned, device_id, now, sleeps)
    seen = set(seen_reminders(told, returned, device_id, now, sleeps))
    told_at = {t.memory_id: t.at for t in told}
    out: list[Candidate] = []
    for note in loops:
        moment = note.remind_instant
        if note.category is not MemoryCategory.LOOP or moment is None or moment > now:
            continue
        if note.remind_state == "told":  # again at a return after the telling (not one before it)
            when = told_at.get(note.memory_id)
            if fresh and returned is not None and when is not None and returned.at >= when                     and note.memory_id not in seen and now - moment < REMIND_LATE:
                out.append(Candidate(WakeReason(WakeKind.REMINDER, memory_id=note.memory_id, again=True),
                                     None, moment))
            continue
        if note.remind_state != "scheduled":
            continue
        if now - moment < REMIND_ON_TIME:
            out.append(Candidate(WakeReason(WakeKind.REMINDER, memory_id=note.memory_id, asleep=asleep),
                                 None, moment))
        elif now - moment < REMIND_LATE and fresh and returned is not None \
                and returned.at >= moment + REMIND_ON_TIME:
            out.append(Candidate(WakeReason(WakeKind.REMINDER, memory_id=note.memory_id, late=True), None, moment))
    return out


def return_candidate(returned: Return | None, device_id: str | None, last_user_message_at: datetime | None,
                     settings: ProactiveSettings, now: datetime, *, sleeps: bool = False,
                     told: Iterable[Told] = ()) -> Candidate | None:
    """§5.2 (Q3): greeted when the user was away RETURN_HOURS or more, said nothing
    meanwhile (nor in the RETURN_WAIT after it), on the device that is still the
    current one, and the record is fresh. V3-P90 §5.2: not when the return shows a
    reminder told while it slept was seen (the user came to it)."""
    if returned is None or settings.return_hours <= 0 or returned.away_minutes is None:
        return None
    if not _fresh(returned, device_id, now, sleeps):
        return None
    if seen_reminders(told, returned, device_id, now, sleeps):
        return None
    if returned.away_minutes < settings.return_hours * 60:
        return None
    away_from = returned.at - timedelta(minutes=returned.away_minutes)
    if last_user_message_at is not None and last_user_message_at > away_from:
        return None  # back already (the Web, the CLI), or spoke first after the unlock
    return Candidate(WakeReason(WakeKind.RETURNED, away_minutes=returned.away_minutes, via=returned.via),
                     f"return:{returned.device_id}:{_minute(returned.at)}", returned.at)


def task_ended_candidate(device_id: str | None, device: DeviceState | None, settings: ProactiveSettings,
                         now: datetime) -> Candidate | None:
    """V3-P90 §5.3: a game or a video the app saw clearly end, after ENDED_MINUTES or more, within
    ENDED_FRESH of its end (held back longer, it is let go: "did the game end?" nine minutes on is odd)."""
    ended = None if device is None else device.task_ended
    if device_id is None or ended is None or settings.ended_minutes <= 0 or ended.group not in ENDED_GROUPS:
        return None
    if ended.lasted_seconds < settings.ended_minutes * 60 or ended.ended_seconds_ago > ENDED_FRESH.total_seconds():
        return None
    at = now - timedelta(seconds=ended.ended_seconds_ago)
    # PR #165 review P2-1: its group in the key — a game and a video that end a minute apart are two ends
    key, aliases = _around(f"ended:{device_id}:{ended.group}", at)
    return Candidate(WakeReason(WakeKind.TASK_ENDED, group=ended.group, minutes=int(ended.lasted_seconds // 60)),
                     key, at, aliases)


def free_candidate(s: Situation) -> Candidate | None:
    """V3-P90 §6.2: the same task went on long enough. A cycle starts with the task, or again with a
    user message after it (the step goes back to the first); each free wake of the cycle (a pass too)
    is a step; the wait runs from the cycle's start or the latest wake of any kind since. Its key names
    the cycle's start and the step, so a new cycle opens its first step again (PR #162 review P1-8)."""
    task = None if s.device is None else s.device.task
    if not s.free_ok or not s.settings.free_steps or s.device_id is None or task is None:
        return None
    cycle = s.now - timedelta(seconds=task.for_seconds)
    if s.last_user_message_at is not None and s.last_user_message_at > cycle:
        cycle = s.last_user_message_at
    since = [w for w in s.wakes if w.created_at >= cycle]
    step = sum(1 for w in since if free_wake(w.reasons))
    wait_from = max([cycle, *(w.created_at for w in since)])
    minutes = s.settings.free_steps[min(step, len(s.settings.free_steps) - 1)]
    if s.now - wait_from < timedelta(minutes=minutes):
        return None
    key, aliases = _around(f"free:{s.device_id}", cycle)  # a task's start moves by a second from look to look
    return Candidate(WakeReason(WakeKind.FREE, group=task.group, minutes=int(task.for_seconds // 60), step=step + 1),
                     f"{key}:{step + 1}", s.now, tuple(f"{a}:{step + 1}" for a in aliases))


def _sleeps(s: Situation) -> bool:
    return s.device is not None and s.device.sleeps


def candidates(s: Situation) -> tuple[list[Candidate], list[str], datetime | None]:
    """Every trigger's candidate (§5), the loop keys to expire, the next loop moment."""
    due, expired, upcoming = loop_candidates(s.loops, s.now)
    asleep = s.device is not None and s.device.sleep is not None
    found = reminder_candidates(s.loops, s.returned, s.device_id, s.now, sleeps=_sleeps(s), asleep=asleep,
                                told=s.told) + due
    back = return_candidate(s.returned, s.device_id, s.last_user_message_at, s.settings, s.now,
                            sleeps=_sleeps(s), told=s.told)
    if back is not None:
        found.append(back)
    if (ended := task_ended_candidate(s.device_id, s.device, s.settings, s.now)) is not None:
        found.append(ended)
    if (free := free_candidate(s)) is not None:
        found.append(free)
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


def spoke(record: WakeRecord, said: frozenset[str] = frozenset()) -> bool:
    """§6.2: a wake the gap and the daily limit count — one that spoke or is speaking:
    opened, speaking, answered, or failed after its turn left output (``said``: progress or tool
    calls, PR #156 re-review P2-2). Not one that said nothing (failed before a word, or
    cancelled as the user won: PR #156 review P1, a wake cut by a restart is tried again
    at once), nor a free wake that passed or was dropped (V3-P90 §7.3), nor the operator's try (§11)."""
    if _only(record, _TRY):
        return False
    return record.status in (WakeStatus.OPENED, WakeStatus.SPEAKING, WakeStatus.ANSWERED) or (
        record.status is WakeStatus.FAILED and record.wake_id in said)


def _blocked(s: Situation, kind: WakeKind) -> str | None:
    """V3-P90 §5 in order: the first rule that keeps a wake of this ``kind`` from being
    spoken. A reminder passes all but no device, work and a screen that is off (a
    sleeping character wakes for it, Q8 of V3-P87 still: past the user's quiet); a
    return passes all but the user's quiet; the daily limit holds back the activity
    triggers only."""
    if s.device_id is None:
        return "no_device"
    if s.working:
        return "working"
    sleep = None if s.device is None else s.device.sleep
    if kind is WakeKind.REMINDER:
        return "display_off" if sleep == "display_off" else None
    if sleep is not None:
        return "asleep"
    if s.quiet_until is not None and s.quiet_until > s.now:
        return "quiet"
    if kind is WakeKind.RETURNED:
        return None
    if s.last_user_message_at is not None and s.now - s.last_user_message_at < TALKING:
        return "talking"
    if kind is WakeKind.TASK_ENDED:  # V3-P90 §5.3: a new thing that happened — no gap, no day, no hours
        return None
    if s.unanswered_wake:
        return "unanswered"
    if in_quiet_hours(s.now, s.zone, s.settings.quiet_start, s.settings.quiet_end):
        return "quiet_hours"
    counted = [w for w in s.wakes if spoke(w, s.said)]
    last = max((w.created_at for w in counted), default=None)
    if last is not None and s.now - last < timedelta(minutes=s.settings.gap_minutes):
        return "gap"
    if kind is not WakeKind.FREE:
        return None
    # V3-P90 §7.4: a free wake's day — those that spoke, and all opened (a pass costs too)
    midnight = local_midnight(s.now, s.zone)
    today = [w for w in s.wakes if w.created_at >= midnight and free_wake(w.reasons)]
    if sum(1 for w in today if spoke(w, s.said)) >= s.settings.daily:
        return "daily"
    if len(today) >= s.settings.attempts:
        return "attempts"
    return None


def decide(s: Situation, taken: frozenset[str] = frozenset()) -> Decision:
    """§5–§6.4: what to speak of now, in one wake, or why not. ``taken``: those of the
    candidates' keys (``candidates``) a wake took or that expired."""
    found, expired, upcoming = candidates(s)
    seen = seen_reminders(s.told, s.returned, s.device_id, s.now, _sleeps(s))
    due = [c for c in found if not taken.intersection(c.keys)]
    expired_now = tuple(k for k in expired if k not in taken)
    if not due:
        return Decision(expired=expired_now, next_at=upcoming, seen=seen)
    rules = [_blocked(s, c.reason.kind) for c in due]
    blocked = next((rule for rule in rules if rule is not None), None)
    speakable = [c for c, rule in zip(due, rules, strict=True) if rule is None]
    if any(c.reason.kind is not WakeKind.FREE for c in speakable):  # V3-P90 §7.3: a free wake goes alone
        speakable = [c for c in speakable if c.reason.kind is not WakeKind.FREE]
    if not speakable:
        return Decision(expired=expired_now, blocked=blocked, held=tuple(due), next_at=upcoming, seen=seen)
    ordered = sorted(speakable, key=lambda c: (MERGE_ORDER.index(c.reason.kind), c.since))
    chosen, over = ordered[:MAX_WAKE_REASONS], ordered[MAX_WAKE_REASONS:]
    held = tuple(over) + tuple(c for c in due if c not in speakable)
    return Decision(reasons=tuple(c.reason for c in chosen), keys=tuple(c.key for c in chosen if c.key is not None),
                    reminders=tuple(c.reason.memory_id for c in chosen if c.reason.kind is WakeKind.REMINDER
                                    and c.reason.memory_id is not None),
                    expired=expired_now, blocked=blocked if held else None, held=held, next_at=upcoming,
                    seen=seen)


def candidate_keys(found: Sequence[Candidate], expired: Sequence[str]) -> tuple[str, ...]:
    """Every key ``decide`` asks about (to read ``taken`` in one query)."""
    return tuple(dict.fromkeys([k for c in found for k in c.keys] + list(expired)))
