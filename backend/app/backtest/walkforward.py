"""Walk-forward evaluation.

Tuning weights on a period and then reporting that same period's performance is
overfitting, and it would quietly undo the point-in-time work everywhere else.
This module rolls a train/evaluate window forward and tags each run
IN_SAMPLE / OUT_OF_SAMPLE / HOLDOUT so the two can be shown side by side.

Implemented in Phase 3.
"""
