"""Data access.

The only layer permitted to touch SQLAlchemy. Engines and the backtest ask this
layer for data; they never query directly. That is enforced by the contracts in
`.importlinter`, not merely documented, because the point-in-time filter lives
here and a query that goes around it reintroduces look-ahead bias invisibly.
"""

from app.repositories import (
    candle_repo,
    filing_repo,
    fundamental_repo,
    instrument_repo,
    news_repo,
    promotion_repo,
    quota_repo,
)

__all__ = [
    "candle_repo",
    "filing_repo",
    "fundamental_repo",
    "instrument_repo",
    "news_repo",
    "promotion_repo",
    "quota_repo",
]
