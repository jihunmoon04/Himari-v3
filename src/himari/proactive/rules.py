"""V3-P87 §5 (triggers), §6 (rules), §6.4 (merging): pure functions.

Every trigger gives a ``Candidate``; ``decide`` keeps those whose key no wake
took yet, applies the rules and merges what may speak into one wake.

V3-P107 §3: the rules come in three tiers. Every wake waits for a current
device that is awake and not working. A reminder (the user asked for it) needs
no more. Something that just happened — a return, a game or a video that
ended (V3-P90 §5.3, let go past ENDED_FRESH) — also waits for the user's quiet
and for talk to end. HIMARI's own openings — a loop, a free wake (V3-P90 §6.2,
§7), an anniversary (character-bond §6), a piece of work just finished
(character-work §7.1) — also wait for an answer to the last wake, the quiet
hours and the gap, and a free wake, an anniversary and a finished piece for the
day's count. A long focus and a game's end by the program in front (V3-P87
§5.4) are made no more.

character-work §7.2 (K5): a free wake opens when the user is at the computer (the
current device awake with recent input) and FREE_MINUTES passed since the last wake
or the user's last message, whatever they are doing. §7.3 (K6): after one
unanswered wake HIMARI's own openings wait UNANSWERED_WAIT; after two in a row,
until the user speaks.
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
    Anniversary,
    LifeEntry,
    LifeKind,
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
TALKING = timedelta(minutes=10)  # §6.1: a user message this recent means the user is talking
ENDED_FRESH = timedelta(minutes=3)  # V3-P90 §5.3: a task's end is spoken of this soon, else let go
ENDED_GROUPS = frozenset({"game", "video"})  # V3-P90 §5.3: the ends the app can tell clearly
# V3-P107 §3: what just happened — past the gap, the quiet hours and the day, not past quiet or talk
EVENT_KINDS = frozenset({WakeKind.RETURNED, WakeKind.TASK_ENDED})
# V3-P107 §3: the kinds the talking rule holds back — all but a reminder (and the operator's try)
TALKING_KINDS = frozenset(set(WakeKind) - {WakeKind.REMINDER, WakeKind.TRY})
# §6.4, V3-P90 §4: the order reasons are told in, and kept when over MAX_WAKE_REASONS; character-bond §6 (B5):
# an anniversary right after a reminder
# character-work §7.1: a finished piece right after an anniversary, before a loop
MERGE_ORDER = (WakeKind.REMINDER, WakeKind.ANNIVERSARY, WakeKind.WORK_DONE, WakeKind.LOOP_DUE, WakeKind.TASK_ENDED,
               WakeKind.FREE, WakeKind.RETURNED)
# character-bond §6: the kinds the day's count holds (an anniversary counts with the free wakes; character-work
# §7.1: a finished piece too)
DAILY_KINDS = frozenset({WakeKind.FREE, WakeKind.ANNIVERSARY, WakeKind.WORK_DONE})
WORK_FRESH = timedelta(hours=3)  # character-work §7.1: a piece is spoken of within this of when it was made
PRESENT_IDLE = timedelta(minutes=10)  # §7.2: input this recent on the current device: the user is at the computer
UNANSWERED_WAIT = timedelta(hours=2)  # §7.3 (K6): after one unanswered wake, HIMARI's own openings wait this


@dataclass(frozen=True)
class ProactiveSettings:
    """V3-P87 §11, with the owner's numbers (§15; V3-P90 §8: gap 15 min, a return after an
    hour away; V3-P107 §4: one free interval, one daily count)."""

    enabled: bool = False  # HIMARI_PROACTIVE
    quiet_start: time | None = time(23, 0)  # HIMARI_PROACTIVE_QUIET "23:00-08:00"; None: no quiet hours
    quiet_end: time | None = time(8, 0)
    gap_minutes: int = 15  # V3-P90: for a loop and a free wake
    daily: int = 10  # V3-P107 §4: free wakes opened a day, passes too (each costs a request)
    free_minutes: int = 30  # character-work §7.2: this long since the last wake or the user's message; 0: none
    return_hours: int = 1  # 0: no greeting on a return
    ended_minutes: int = 60  # V3-P90 §5.3: a game or a video this long; 0: no TASK_ENDED

    def describe(self) -> str:
        """The startup line and the status (§11)."""
        if not self.enabled:
            return "off (HIMARI_PROACTIVE=0)"
        quiet = "no quiet hours" if self.quiet_start is None or self.quiet_end is None else \
            f"quiet {self.quiet_start:%H:%M}-{self.quiet_end:%H:%M}"
        free = "no free wakes" if self.free_minutes <= 0 or self.daily <= 0 else \
            f"free every {self.free_minutes}m, {self.daily} a day"
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
    free_ok: bool = False  # V3-P90 §7.5: the conversation's model was checked for free wakes
    # character-bond §6: today's anniversaries of the chosen persona's bond and the date they are of (the
    # server's zone) — given only with the bond told and a model checked for free wakes (it may pass)
    anniversaries: tuple[Anniversary, ...] = ()
    bond_day: str | None = None
    # character-work §7.1: the chosen persona's work pieces made lately — given only with the life told and a
    # model checked for free wakes (it may pass)
    work: tuple[LifeEntry, ...] = ()


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


def _fresh(returned: Return | None, device_id: str | None, now: datetime, sleeps: bool = False) -> bool:
    """§5.2: a return on the device still current, past the user's RETURN_WAIT, not older than
    RETURN_FRESH. V3-P90 §5.2: on a device that sends ``sleep``, at once and for SLEEP_RETURN_FRESH."""
    wait, fresh = (timedelta(0), SLEEP_RETURN_FRESH) if sleeps else (RETURN_WAIT, RETURN_FRESH)
    return returned is not None and returned.device_id == device_id and wait <= now - returned.at <= fresh


def reminder_candidates(loops: Iterable[Memory], returned: Return | None, device_id: str | None,
                        now: datetime, *, sleeps: bool = False) -> list[Candidate]:
    """§5.3: a scheduled reminder whose moment came — on time within REMIND_ON_TIME; after
    that (Q9), late at a fresh return that came after its on-time window, until REMIND_LATE
    (then Data marks it missed). No key: its delivery state keeps it from repeating.
    V3-P107 §2: a sleeping character is not woken for it — the rules hold it until the
    user is back."""
    fresh = _fresh(returned, device_id, now, sleeps)
    out: list[Candidate] = []
    for note in loops:
        moment = note.remind_instant
        if note.category is not MemoryCategory.LOOP or moment is None or moment > now \
                or note.remind_state != "scheduled":
            continue
        if now - moment < REMIND_ON_TIME:
            out.append(Candidate(WakeReason(WakeKind.REMINDER, memory_id=note.memory_id), None, moment))
        elif now - moment < REMIND_LATE and fresh and returned is not None \
                and returned.at >= moment + REMIND_ON_TIME:
            out.append(Candidate(WakeReason(WakeKind.REMINDER, memory_id=note.memory_id, late=True), None, moment))
    return out


def return_candidate(returned: Return | None, device_id: str | None, last_user_message_at: datetime | None,
                     settings: ProactiveSettings, now: datetime, *, sleeps: bool = False) -> Candidate | None:
    """§5.2 (Q3): greeted when the user was away RETURN_HOURS or more, said nothing
    meanwhile (nor in the RETURN_WAIT after it), on the device that is still the
    current one, and the record is fresh."""
    if returned is None or settings.return_hours <= 0 or returned.away_minutes is None:
        return None
    if not _fresh(returned, device_id, now, sleeps):
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


def at_computer(device: DeviceState | None) -> bool:
    """character-work §7.2: the user is at the computer — the current device connected, not locked
    nor asleep, with input within PRESENT_IDLE (what Presence already knows)."""
    return (device is not None and device.connected and not device.locked and device.sleep is None
            and device.idle_seconds is not None and device.idle_seconds <= PRESENT_IDLE.total_seconds())


def free_candidate(s: Situation) -> Candidate | None:
    """character-work §7.2 (K5): the user is at the computer and FREE_MINUTES passed since the
    latest wake of any kind (a pass too) or the user's last message, whatever the task. The task,
    when there is one, goes with it (its group and minutes); ``step`` counts the free wakes since
    the user's last message (from 1). Its key names the moment the wait started, so each wait
    opens once."""
    if not s.free_ok or s.settings.free_minutes <= 0 or s.device_id is None or not at_computer(s.device):
        return None
    marks = [w.created_at for w in s.wakes] + ([s.last_user_message_at] if s.last_user_message_at else [])
    wait_from = max(marks, default=None)
    if wait_from is not None and s.now - wait_from < timedelta(minutes=s.settings.free_minutes):
        return None
    step = sum(1 for w in s.wakes if w.reasons and all(r.kind is WakeKind.FREE for r in w.reasons)
               and (s.last_user_message_at is None or w.created_at > s.last_user_message_at))
    task = None if s.device is None else s.device.task
    key = f"free:{s.device_id}:{'start' if wait_from is None else _minute(wait_from)}"
    return Candidate(WakeReason(WakeKind.FREE, group=None if task is None else task.group,
                                minutes=None if task is None else int(task.for_seconds // 60), step=step + 1),
                     key, s.now)


def work_candidates(s: Situation) -> list[Candidate]:
    """character-work §7.1: each work piece made less than WORK_FRESH ago, once (its key names
    the piece), on the current device."""
    if s.device_id is None:
        return []
    return [Candidate(WakeReason(WakeKind.WORK_DONE, entry_id=e.entry_id), f"work:{e.entry_id}", e.created_at)
            for e in s.work if e.kind is LifeKind.WORK and timedelta(0) <= s.now - e.created_at < WORK_FRESH]


def anniversary_candidates(s: Situation) -> list[Candidate]:
    """character-bond §6: each of today's anniversaries, once that day (its key names the
    day and which one), on the current device."""
    if s.device_id is None or s.bond_day is None:
        return []
    return [Candidate(WakeReason(WakeKind.ANNIVERSARY, days=a.days, years=a.years, entry_id=a.entry_id),
                      f"anniversary:{s.bond_day}:{a.key}", s.now) for a in s.anniversaries]


def _sleeps(s: Situation) -> bool:
    return s.device is not None and s.device.sleeps


def candidates(s: Situation) -> tuple[list[Candidate], list[str], datetime | None]:
    """Every trigger's candidate (§5), the loop keys to expire, the next loop moment."""
    due, expired, upcoming = loop_candidates(s.loops, s.now)
    found = reminder_candidates(s.loops, s.returned, s.device_id, s.now, sleeps=_sleeps(s)) + due
    back = return_candidate(s.returned, s.device_id, s.last_user_message_at, s.settings, s.now,
                            sleeps=_sleeps(s))
    if back is not None:
        found.append(back)
    if (ended := task_ended_candidate(s.device_id, s.device, s.settings, s.now)) is not None:
        found.append(ended)
    found += anniversary_candidates(s)
    found += work_candidates(s)
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
    """§6.2: a wake the gap counts — one that spoke or is speaking:
    opened, speaking, answered, or failed after its turn left output (``said``: progress or tool
    calls, PR #156 re-review P2-2). Not one that said nothing (failed before a word, or
    cancelled as the user won: PR #156 review P1, a wake cut by a restart is tried again
    at once), nor a free wake that passed or was dropped (V3-P90 §7.3), nor the operator's try (§11)."""
    if _only(record, _TRY):
        return False
    return record.status in (WakeStatus.OPENED, WakeStatus.SPEAKING, WakeStatus.ANSWERED) or (
        record.status is WakeStatus.FAILED and record.wake_id in said)


def unanswered(s: Situation) -> bool:
    """character-work §7.3 (K6): whether the unanswered wake still holds HIMARI's own openings —
    the wakes shown since the user's last message (``spoke``): one holds them UNANSWERED_WAIT, two
    or more until the user speaks (as does none read: the wake is older than what was read)."""
    if not s.unanswered_wake:
        return False
    shown = [w for w in s.wakes if spoke(w, s.said)
             and (s.last_user_message_at is None or w.created_at > s.last_user_message_at)]
    return len(shown) != 1 or s.now - shown[0].created_at < UNANSWERED_WAIT


def _blocked(s: Situation, kind: WakeKind) -> str | None:
    """V3-P107 §3: the first rule that keeps a wake of this ``kind`` from being spoken, in three
    tiers. Every wake: a current device, awake, no work running. A reminder: nothing more (the
    user asked for it: past their quiet too, Q8 of V3-P87). What just happened (EVENT_KINDS): the
    user's quiet and talk. HIMARI's own openings (a loop, a free wake, an anniversary, a finished
    piece): also an unanswered wake (character-work §7.3: for a while after one, until the user
    speaks after two), the quiet hours and the gap, and the free kinds their day's count."""
    if s.device_id is None:
        return "no_device"
    if s.working:
        return "working"
    if s.device is not None and s.device.sleep is not None:
        return "asleep"
    if kind is WakeKind.REMINDER:
        return None
    if s.quiet_until is not None and s.quiet_until > s.now:
        return "quiet"
    if s.last_user_message_at is not None and s.now - s.last_user_message_at < TALKING:
        return "talking"
    if kind in EVENT_KINDS:
        return None
    if unanswered(s):
        return "unanswered"
    if in_quiet_hours(s.now, s.zone, s.settings.quiet_start, s.settings.quiet_end):
        return "quiet_hours"
    counted = [w for w in s.wakes if spoke(w, s.said)]
    last = max((w.created_at for w in counted), default=None)
    if last is not None and s.now - last < timedelta(minutes=s.settings.gap_minutes):
        return "gap"
    if kind in DAILY_KINDS and free_today(s) >= s.settings.daily:
        return "daily"
    return None


def free_today(s: Situation) -> int:
    """V3-P107 §4: the free wakes opened since the device's midnight — a pass too (it cost a request).
    character-bond §6 (B5): an anniversary wake (one that may pass) counts with them; character-work §7.1:
    so does a finished piece's."""
    midnight = local_midnight(s.now, s.zone)
    return sum(1 for w in s.wakes if w.created_at >= midnight and free_wake(w.reasons))


def decide(s: Situation, taken: frozenset[str] = frozenset()) -> Decision:
    """§5–§6.4: what to speak of now, in one wake, or why not. ``taken``: those of the
    candidates' keys (``candidates``) a wake took or that expired."""
    found, expired, upcoming = candidates(s)
    due = [c for c in found if not taken.intersection(c.keys)]
    expired_now = tuple(k for k in expired if k not in taken)
    if not due:
        return Decision(expired=expired_now, next_at=upcoming)
    rules = [_blocked(s, c.reason.kind) for c in due]
    blocked = next((rule for rule in rules if rule is not None), None)
    speakable = [c for c, rule in zip(due, rules, strict=True) if rule is None]
    if any(c.reason.kind is not WakeKind.FREE for c in speakable):  # V3-P90 §7.3: a free wake goes alone
        speakable = [c for c in speakable if c.reason.kind is not WakeKind.FREE]
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
