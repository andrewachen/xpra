# nvenc reinit-storm fix — post-deploy verification

Branch: `feature/nvenc-reinit-storm-v6.4.3`
.deb artifacts: `.claude/worktrees/nvenc-storm/build-deb-out/`

R1 (candidate-space gate + safety valve), R2 (always-on edge resistance + Y2 deadband), R3 (bitrate-only reconfigure) are all in this build, stacked on the Phase 0 instrumentation baseline.

## Deploy

1. Install the .deb from `build-deb-out/` via apt (Andrew's daily-driver flow).
2. Restart the xpra service:

   ```bash
   systemctl --user restart xpra.service
   ```

3. Reconnect the client.

## Reproduce the storm conditions

Open Edge in the xpra session with multiple tabs. Play NPR Tiny Desk (or any 30+fps video). Switch tabs / scroll the page periodically for at least 1 minute. This is the workload that previously triggered the storm.

## Gate checks

Run after the 1-min workload:

```bash
xpra info :100 | grep context_count
journalctl --user -u xpra.service --since "5 min ago" | grep -c "failed to acquire cuda device lock"
journalctl --user -u xpra.service --since "5 min ago" | grep -c "failed to encode h265"
xpra info :100 | grep reinit_count
journalctl --user -u xpra.service --since "5 min ago" | grep -c "nvEncReconfigureEncoder OK"
```

## Expected outcomes

| Signal | Pre-fix (Phase 0 baseline) | Target |
| --- | --- | --- |
| `context_count` for active video window | 2 | 1 |
| `failed to acquire cuda device lock` warnings | ~10/min | < 5/min |
| `failed to encode h265` warnings | ~5/min | near 0 |
| `reinit_count` delta during workload | (Phase 0 baseline) | > 80% lower |
| `nvEncReconfigureEncoder OK` log lines | 0 (didn't exist) | > 0 (R3 is firing) |

## What to do if a gate fails

The diff at HEAD on `feature/nvenc-reinit-storm-v6.4.3` is the entire R1+R2+R3 stack. Report back to me with:

- Which gate failed and by how much (e.g., `context_count=2` instead of 1).
- The journalctl log span where the failure shows up.
- Whether the safety-valve log line `forcing teardown for stuck encoder` appeared (R1's escape hatch firing means the candidate-space gate isn't catching a transition it should).

I'll triage from there. The most likely failure modes per layer:

- **R1 not catching a candidate-space transition** → `context_count` stays > 1, `reinit_count` doesn't drop. Likely cause: a non-obvious cause of `update_encoding_options` triggering rebuild we haven't gated. Look at the safety-valve log to see what cycle the encoder is stuck in.
- **R2 edge resistance not engaging** → cdc.lock warnings drop only slightly. Check if `setup_cost_mult` is actually scoring against re-init candidates the way we expect.
- **R3 not firing** → no `nvEncReconfigureEncoder OK` lines. Check `set_encoding_quality`/`set_encoding_speed` is being called with non-trivial deltas; if pixel-format/lossless check is always falling through, the bitrate path never runs.
