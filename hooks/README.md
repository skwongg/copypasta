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

- **trade-post-watcher** — Polls the tailed X accounts every 60s during market hours. On a genuine entry/exit alert: signs the alert JSON and pipes it to `fire_entries.py --mode live` (only when `ARMED` exists), then wakes the agent to relay the alert to chat.
- **copy-trader-exits** — Runs the exit monitor (`run_exits.py --mode live`) every ~2 min during market hours when armed.
- **copy-trader-notifier** — Legacy; intentionally disabled (`"enabled": false`). Notifications go through the deterministic notifier path instead.
