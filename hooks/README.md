# Hooks

Event hook scripts and definitions for the copy-trader deployment.

These are the source of truth. The live copies run from `/home/hatch/hooks/`:

- `scripts/` → deployed to `/home/hatch/hooks/scripts/`
- `definitions/` → deployed to `/home/hatch/hooks/definitions/`

After editing, copy the changed files to the live location:

```bash
cp hooks/scripts/*.sh /home/hatch/hooks/scripts/
cp hooks/definitions/*.json /home/hatch/hooks/definitions/
```

## Hooks

- **trade-post-watcher** — Polls the tailed X accounts every 60s during market hours. On a genuine entry/exit alert: signs the alert JSON and pipes it to `fire_entries.py --mode live`, then wakes the agent to relay the alert plus the copy-trader result (`copytrader` field) to chat. `fire_entries.py` refuses locally when the live state is disarmed or halted.
- **copy-trader-exits** — Runs the exit monitor (`run_exits.py --mode live`) every 60s during market hours: reconcile, reprice resting exits, cancel stale entries, stop-loss and take-profit ladder. Refuses locally when disarmed or halted.
- **copy-trader-notifier** — Drains live fill/status events from the live state (`notifications.py --mode live`).

Arm/halt with `python3 ~/workspace/copy-trader/kill.py --mode live --arm|--halt|--disarm|--status`. The checkout-level `ARMED`, `KILL` and `positions.json` files are no longer read by anything.
