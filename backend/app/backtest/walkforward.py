"""Walk-forward windows: rolling train/evaluate splits over a session list.

A single train/test split gives one number from one arbitrary cut point. Walk
forward instead and the same history yields several independent evaluation
periods, and — more usefully — shows whether a rule that worked in 2024 was
still working in 2025.

**Windows are counted in sessions, not dates.** A "six month" window is a
different number of trading days depending on where it falls, and windows
defined in calendar months drift against the data they are measured on. The
boundaries that come out are real session dates, which is also what makes them
safe to hand to the backtest service.

**Evaluation windows tile.** Each session in the evaluated span appears in
exactly one evaluation period — not twice, and not zero times. That is
enforced rather than assumed: the step must equal the evaluation length, so
the shapes that would break it cannot be constructed. Training windows do
overlap, which is the point of rolling them.

That is a statement about *dates*, not about a portfolio. Each window is run
as an independent simulation starting from cash with no position, so window 1
does not inherit what window 0 was holding when it ended. The per-fold results
are comparable to each other, and chaining them into a single "continuous
out-of-sample equity curve" would describe a portfolio that never existed. A
continuously refitted simulation is a different thing, needing the engine to
carry state across refits, and is not what this produces.

**The holdout is carved off before anything else and never returned.** Windows
are generated from what remains, so no amount of iterating on window
parameters can reach it. That is the only protection it has: a holdout you can
accidentally evaluate against is not a holdout, and this module cannot hand it
out at all.

This module is pure — it takes a list of dates and returns windows. Whether
those windows can be simulated, and what the data behind them looks like, is
the service's question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class SampleType(StrEnum):
    """Which side of the evidence a result sits on."""

    IN_SAMPLE = "IN_SAMPLE"
    """Measured over the period the rule was chosen on. Not evidence."""

    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    """Measured over a period the rule had not seen when it was chosen."""

    HOLDOUT = "HOLDOUT"
    """The reserved tail, evaluated once after every choice has been made.

    `generate` never produces a window over it and `walk_forward` never scores
    it. Scoring is a separate, deliberate call, because a holdout that is
    reported on every iteration gets fitted by eye — which is harder to notice
    than fitting it in code, and no less real.
    """


class WalkForwardError(Exception):
    """The requested split cannot be made from the sessions available."""


@dataclass(frozen=True, slots=True)
class Window:
    """One train/evaluate pair, in real session dates."""

    index: int
    train_start: date
    train_end: date
    eval_start: date
    eval_end: date

    def __post_init__(self) -> None:
        if self.train_end >= self.eval_start:
            raise WalkForwardError(
                f"window {self.index}: training ends {self.train_end}, on or after "
                f"evaluation starts {self.eval_start}; the two must not overlap"
            )


@dataclass(frozen=True, slots=True)
class Split:
    """Every window, plus the holdout that none of them can see."""

    windows: tuple[Window, ...]
    holdout_start: date | None = None
    holdout_end: date | None = None

    @property
    def has_holdout(self) -> bool:
        return self.holdout_start is not None


def generate(
    sessions: list[date],
    *,
    train_sessions: int,
    eval_sessions: int,
    step_sessions: int | None = None,
    anchored: bool = False,
    holdout_sessions: int = 0,
) -> Split:
    """Roll a train/evaluate window across `sessions`.

    Args:
        train_sessions: length of each training period.
        eval_sessions: length of each evaluation period.
        step_sessions: how far to roll between windows. It must equal
            `eval_sessions`, and exists as a parameter so that intent is
            stated rather than assumed. Any other value breaks the property
            the rest of this module and its readers rely on — see below.
        anchored: keep every training period starting at the first session,
            growing it, rather than rolling a fixed-length one. Anchored
            training uses more data; rolling training is the one that reveals
            a rule decaying, because an old regime eventually falls out of it.
        holdout_sessions: sessions reserved at the end, removed before any
            window is built and not reachable from the result's windows.

    Raises:
        WalkForwardError: when the sessions cannot support one whole window.
            Returning an empty list instead would let a caller report "no
            overfitting detected" from a split that never happened.
    """
    if train_sessions < 1 or eval_sessions < 1:
        raise WalkForwardError(
            f"train ({train_sessions}) and evaluation ({eval_sessions}) lengths "
            "must both be at least one session"
        )
    if holdout_sessions < 0:
        raise WalkForwardError(f"holdout cannot be negative, got {holdout_sessions}")

    step = step_sessions if step_sessions is not None else eval_sessions
    if step < 1:
        raise WalkForwardError(f"step must be at least one session, got {step}")
    if step != eval_sessions:
        # Anything else quietly breaks what `evaluation_covers` reports, and
        # it breaks it in the flattering direction. Measured on two years of
        # NYSE sessions with 120/60 windows:
        #
        #   step 30  span 360 sessions, 660 observations — every day in the
        #            overlap counted twice, and the span cannot show it
        #   step 90  span 330 sessions, 240 evaluated — 90 sessions inside the
        #            reported range never evaluated at all, and nothing says so
        #
        # Overlapping walk-forward is a real technique, but using it means
        # reporting `unique_oos_sessions` and `total_oos_observations`
        # separately rather than one span. Until something needs that, the
        # honest move is to refuse the shapes we cannot describe.
        raise WalkForwardError(
            f"step_sessions ({step}) must equal eval_sessions ({eval_sessions}): "
            "evaluation periods must tile the timeline exactly once, or the "
            "reported out-of-sample span silently covers days that were "
            "measured twice or not at all"
        )

    if holdout_sessions >= len(sessions):
        raise WalkForwardError(
            f"holdout of {holdout_sessions} sessions leaves nothing of the "
            f"{len(sessions)} available"
        )

    usable = sessions[: len(sessions) - holdout_sessions] if holdout_sessions else sessions
    held_out = sessions[len(sessions) - holdout_sessions :] if holdout_sessions else []

    needed = train_sessions + eval_sessions
    if len(usable) < needed:
        raise WalkForwardError(
            f"a {train_sessions}+{eval_sessions} window needs {needed} sessions but "
            f"only {len(usable)} are available"
            + (f" after reserving {holdout_sessions} for the holdout" if holdout_sessions else "")
        )

    windows: list[Window] = []
    start = 0
    while start + needed <= len(usable):
        train_lo = 0 if anchored else start
        train_hi = start + train_sessions  # exclusive
        eval_hi = train_hi + eval_sessions  # exclusive

        windows.append(
            Window(
                index=len(windows),
                train_start=usable[train_lo],
                train_end=usable[train_hi - 1],
                eval_start=usable[train_hi],
                eval_end=usable[eval_hi - 1],
            )
        )
        start += step

    return Split(
        windows=tuple(windows),
        holdout_start=held_out[0] if held_out else None,
        holdout_end=held_out[-1] if held_out else None,
    )


def evaluation_covers(split: Split) -> tuple[date, date] | None:
    """The span the evaluation windows collectively cover, or None.

    Reported next to a walk-forward result so a reader can see how much of the
    history the out-of-sample figures actually speak for. A split whose
    evaluation periods cover eight months of a three-year history is not a
    three-year out-of-sample test, however many windows it has.

    This is a contiguous, once-each span because `generate` refuses any step
    that would make it otherwise. A single pair of dates cannot express "360
    sessions, 660 observations" or "330 sessions, 90 of them never measured",
    so those splits are not constructible rather than being summarised
    misleadingly.
    """
    if not split.windows:
        return None
    return split.windows[0].eval_start, split.windows[-1].eval_end
