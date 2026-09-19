"""Orchestration between persistence and the pure engines.

This layer may query; the engines it calls may not. That seam is what lets the
same engine code run unchanged in the live path and inside a backtest — the
engine only ever sees plain values and cannot tell which context it is in.
"""
