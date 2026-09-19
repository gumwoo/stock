"""Point-in-time data access — the only door into the backtest's data.

Two filters, always applied together in reproduce mode:

    available_at <= simulation_asof     could the market have known this?
    ingested_at  <= data_snapshot_at    did we have it when the run happened?

The second exists because a later backfill has a *past* `filed_at` and therefore
sails through the first filter, silently changing the result of a backtest that
was run before the backfill landed.

Implemented in Phase 3. The module exists now so the architecture contract that
forbids engines from reaching past it is live from the first commit.
"""
