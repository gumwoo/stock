"""Execution-timing invariants.

Enforces `execution_at >= next_tradable_open(decision_at)`, and raises rather
than corrects when that is violated. A signal computed from a session's close
cannot fill at that same close; permitting it would be look-ahead bias wearing a
different hat.

Implemented in Phase 3.
"""
