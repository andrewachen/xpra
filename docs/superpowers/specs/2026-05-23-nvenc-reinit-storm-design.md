<!--
CODEX: this file is a design specification for the nvenc reinit-storm
fix in xpra. The document itself IS the deliverable for this commit —
no executable code changes accompany it intentionally; implementation
happens in follow-up commits per the plan inside.

This is the FIFTH pass of this spec. Prior reviews each surfaced 4
substantive issues that have been addressed in iteration. Pass 4 added
issues around: (a) `_effective_encoder_pixel_format` keyed on the
current encoder only (missed per-candidate transitions for
`encoding=auto`); (b) tuple using `self.actual_scaling` (the installed
pipeline's scaling, lags reality) instead of fresh-computed desired
scaling; (c) R3's reconfigure path re-running `init_params()` which
calls `get_preset()` — speed nudges crossing a preset boundary would
fail because NVENC reconfigure forbids preset change.

Pass 5 corrections:

1. **R1 tuple element**: `_candidate_pixel_format_fingerprint` is now a
   hash of the per-candidate target-pixel-format MAP (with Y2 deadband
   applied per candidate), not a single value from the current encoder.
   This catches `encoding=auto` transitions where the current encoder's
   pixel format wouldn't change but a different candidate would win.
2. **R1 scaling**: tuple uses `_desired_scaling` (fresh from
   `calculate_scaling()`), not `self.actual_scaling` (installed
   pipeline). Avoids the lag.
3. **R3 preset preservation**: added Prerequisite 2 explaining that
   `_apply_reconfigure()` must NOT re-call `init_params()`. Instead,
   cache the original `presetGUID` at init time and copy that snapshot
   into `reInitEncodeParams`, overlaying only rate-control fields.
   Speed nudges crossing a preset boundary fall through to teardown.
4. **Phase 0 slimmed**: per-path counter machinery dropped. Phase 0 now
   covers only (a) aggregate reinit counter + (b) cdc.lock warning
   enrichment. Per-path / delta-type / edge-resistance diagnostics
   deferred to lever-specific opt-in env vars. ~15 LOC instead of 50-80.
   The function-signature change on `video_context_clean(reason)` is
   dropped — much less invasive for upstream.

Please review the PROSE substantively: are the technical claims correct
against the referenced code? Is the three-lever architecture sound?
Specifically check: does the per-candidate fingerprint approach actually
catch the AV1-vs-h264 mixed-candidate transition? Does the cached-preset
approach in `_apply_reconfigure` align with NVENC SDK 13 semantics for
`nvEncReconfigureEncoder`? Flag misleading framing, internal
inconsistencies, or anything technically wrong. Treat the file's
location and tracked-ness as out of scope.
-->

# nvenc reinit-storm — design spec

**Date:** 2026-05-23
**Authors:** Andrew, Claude
**Status:** Spec (pre-implementation-plan)
**Memory references:** [[nvenc_audit_findings]], [[nvenc_dealloc_uaf_fix]]
**Related PRs:** #4875 (cleanup leak fix, load-bearing for SEGV path — do not regress), #4890 (dealloc UAF), #4891 (uvloop atexit)

## Problem statement

Live-measured on `orbital` running deb `706a2efcf6`, single NPR Tiny Desk video in Microsoft Edge:

- ~10 `failed to acquire cuda device lock` warnings/min, sustained.
- ~5 `Error: failed to encode h265 frame`/min, sustained (~1 per 250 frames at 21 fps).
- `client.window.<wid>.encoder.context_count == 2` for the single active video window — direct fingerprint of encoder cycling (old + new alive simultaneously).
- p50 encode latency fine (~13 ms damage→packet); storm shows up in p99 spikes and dropped frames.

The storm survives all post-audit fixes (#4875, #4890, #4891). It is not a leak, not a regression — it is the system's normal behavior under sustained operating-point churn from the browser auto-tuner. The fix is structural.

## Root causes

Three independent levers at three different code layers. (An earlier draft proposed a fourth — a per-device init queue — but investigation showed `device_lock` already serializes init-vs-init; the visible `cdc.lock` warnings come from init-vs-encode contention on OTHER encoders sharing the same device, which falls off naturally when R1/R2/R3 reduce init frequency. See "Why no init queue" below.)

### R1 — Selection coupled to operating-point nudges

The auto-tuner runs on a timer (~every few seconds) calling `WindowSource.reconfigure()`, which invokes `update_quality()`/`update_speed()` (updating `_current_quality`/`_current_speed` directly — not through GObject notify signals) and then `update_encoding_options()`. The latter calls `update_pipeline_scores()` which re-runs scoring against the new quality/speed targets and can pick a different winner.

For browser workloads where content_type and dimensions are stable for minutes but the auto-tuner generates sustained quality/speed nudges, this means **scoring re-runs on every tuner tick**. With close-scoring candidates (h264 vs hevc, NV12 vs YUV444P at quality threshold boundaries), small operating-point shifts flip the winner. Each flip triggers `verify_csc_and_encoder() == False` and `video_context_clean()`. **This is the storm's dominant engine for browser/text-y workloads.**

R1 gates `update_encoding_options` on candidate-space tuple comparison: skip re-scoring when nothing in the tuple changed; push the new operating point through `_push_operating_point()` instead. This is the primary intercept and matches the other Claude's memory framing of "decouple selection from operating-point nudges."

`quality_changed()` and `speed_changed()` are GObject window-property notify handlers in `compress.py:772,777`. Their `compress.py` super methods only update `_quality_hint`/`_speed_hint` — they're triggered when a client explicitly sets quality/speed on a window (e.g., via `xpra control`). The `video_compress.py` override at line 804-811 currently calls `video_context_clean()` from these, which is heavyweight for what's usually a rare client-driven event. R1 reroutes those overrides through the same `_maybe_invalidate_for_operating_point()` helper to skip teardown when the candidate space is unchanged. This is a SECONDARY fix, NOT the dominant storm engine.

For video playback in steady state, the candidate space and operating point are both stable; R1 doesn't change behavior.

### R2 — Edge resistance silently disabled for non-subregion windows

`video_scoring.py:115`:

```python
setup_cost_mult = int(detection) * (1 + max(0, MIN_FPS_COST - ffps))
```

where `detection = bool(vs) and vs.detection` (`vs` is the WindowSource's video subregion). When `detection=False`, `setup_cost_mult=0`, which means `ee_score = 100 - setup_cost*0 = 100` for any candidate encoder regardless of whether it matches the current one. Edge resistance — the score boost that should make the current encoder sticky — silently disappears for any window without active video subregion detection.

Browser windows (especially text-y tabs) have `detection=False` most of the time. They have no stickiness, encoders flap freely.

The original `detection` gate (date predates current tree) is preserved with the additional fps-based bump, but the base penalty must always apply: switching cost is real regardless of subregion detection state.

### R3 — Bitrate/speed nudges can't be applied to a live nvenc encoder

Today, `set_encoding_quality()` and `set_encoding_speed()` at `nvenc/encoder.pyx:1302,1307` are effectively no-ops for the live encoder's encode parameters. `set_encoding_quality` stashes `self.quality` and returns. `set_encoding_speed` calls `update_bitrate()` which only updates `self.target_bitrate`/`self.max_bitrate` instance fields — never pushed to nvenc.

Commit `189a05d01f` (July 2017, issue #1550) removed the only existing reconfigure code path. The 2017 code worked like this:

```python
new_pixel_format = self.get_target_pixel_format(target_quality)
new_lossless = self.get_target_lossless(new_pixel_format, target_quality)
if new_pixel_format == self.pixel_format and new_lossless == self.lossless:
    return  # bitrate-only changes: skip reconfigure entirely
# else: attempt nvEncReconfigureEncoder for the pixel-format/lossless change
```

So the 2017 code did NOT reconfigure for bitrate-only changes — it returned early. It only invoked `nvEncReconfigureEncoder` for pixel-format/lossless changes, which is precisely the case reconfigure CAN'T handle (would require new buffers). Totaam disabled the call because every path that actually invoked it would fail. There was never a working bitrate-only reconfigure path.

**R3 is therefore new work, not a revival.** The plan is to add a bitrate-only reconfigure path that didn't previously exist: when `set_encoding_quality`/`set_encoding_speed` is called and target pixel_format/lossless are unchanged, call `nvEncReconfigureEncoder` with the new bitrate/preset. When pixel_format/lossless WOULD change, fall through to teardown as today.

The 2017 test `8151e96537` cannot be reused as-is — it tested the pixel-format reconfigure path (which never worked). A new test is required.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ video_compress.py (WindowVideoSource)                           │
│                                                                 │
│  quality_changed() / speed_changed() ──── R1 reroute ───────┐   │
│       (skip video_context_clean when candidate space same)  │   │
│                                                             │   │
│  update_encoding_options() ──── R1 candidate-space gate ────┤   │
│       │                                                     │   │
│       ├─→ update_pipeline_scores()                          │   │
│       │      │                                              │   │
│       │      └─→ get_pipeline_score() in video_scoring.py   │   │
│       │             │                                       │   │
│       │             └── R2 edge resistance (always-on)      │   │
│       │                                                     │   │
│       └─→ verify_csc_and_encoder() ── R1 safety valve  ────┘   │
│                                                                 │
│  video_context_clean() ── Phase 0 counter choke point           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (on score change → teardown)
┌─────────────────────────────────────────────────────────────────┐
│ nvenc/encoder.pyx                                               │
│                                                                 │
│  set_encoding_quality()/_speed() ── R3 bitrate reconfigure      │
│       (pixel-format guard, else fall through to teardown)       │
└─────────────────────────────────────────────────────────────────┘
```

The three levers are at three distinct code locations:
- R1 lives at the WindowSource scoring trigger AND at `quality_changed`/`speed_changed` (`video_compress.py`).
- R2 lives inside the scoring math (`video_scoring.py:get_pipeline_score`).
- R3 lives at the encoder bitrate path (`nvenc/encoder.pyx:set_encoding_{quality,speed}`).

They reinforce: R1 prevents most teardowns (both via gate and via quality_changed reroute) → R2 only kicks in for the scoring runs R1 lets through → R3 makes the operating-point nudges R1 reroutes actually take effect on the live encoder instead of being lost.

### Why no init queue (R4 dropped)

Earlier drafts proposed a per-cuda-device init serialization queue to address the `failed to acquire cuda device lock` warnings. Investigation showed:

1. `threaded_init_device` (`nvenc/encoder.pyx:564`) holds the module-global `device_lock` across the entire `init_device()` call. Multiple threaded inits **cannot** be in their heavy phase simultaneously — they're already serialized.
2. The "failed to acquire cuda device lock" warning fires from `cuda/context.py:540` (`cuda_device_context.__enter__`) where the acquire is **non-blocking**. Any other holder triggers an immediate `TransientCodecException`, not a contention wait.
3. `cuda_device_context` is shared across ALL encoders on the same GPU. So the contention is **init/cleanup of one encoder vs. compress_image of every OTHER encoder on the same device**, not init-vs-init.

An init queue would not change this — the blast radius of each ~100-200ms init/cleanup window onto OTHER encoders' encode attempts is the visible signal. The right fix is to reduce init frequency (R1/R2/R3), which directly shrinks the blast radius window count. The warnings drop off automatically.

The alternative — making compress_image block on cdc.lock instead of non-blocking — would just trade warning spam for latency spikes during init/cleanup. The upstream code already retries via the TransientCodecException path, so the warnings ARE the retry mechanism's surface signal.

## Phase 0 — Instrumentation (quantify-only)

Ships alone first on `feature/nvenc-reinit-instrument-v6.4.3`, port to `feature/nvenc-reinit-instrument-master`. Build via `./tests/docker/build-deb.sh --deploy`. Baseline measurement: 24h of normal use including at least one Edge tab-restore + one sustained video playback.

**Scoped strictly to quantifying improvement.** Identifying which path dominates is deferred — code analysis (`update_encoding_options` from `WindowSource.reconfigure()`) is strong enough to act on, and per-path diagnosis can be added reactively if R1/R2/R3 don't fully clear the storm.

### What we add

1. **Aggregate reinit counter** (per-window int). Single field on `WindowVideoSource`, incremented inside the existing `if csce or ve:` block in `video_context_clean()` (so no-op calls where both are already `None` don't inflate the count — that block is the real teardown work). Exposed via `get_info()` as `reinit_count`. ~5 LOC.

2. **`cdc.lock` warning enrichment** in `cuda_device_context.__enter__` (`cuda/context.py:540`). When the non-blocking acquire fails, log the current holder + phase (init / cleanup / compress) alongside the existing "failed to acquire cuda device lock" message. Confirms the "init-vs-encode on shared device" diagnosis and lets us measure blast-radius window length. ~10 LOC.

### What we DON'T add in Phase 0

- **Per-path `reason` parameter on `video_context_clean()`** — invasive (touches ~13 call sites with a function-signature change), and we don't need per-path identification given the code analysis. If R1/R2/R3 land and the storm doesn't improve as expected, per-path counters can be added as a follow-up.
- **Delta-type tracking (bitrate_only vs pixel_format_change etc.)** — moderate scope, R3-design-validation specific. Defer until Phase 3 if we want to measure R3's actual reach.
- **Edge-resistance snapshot (detection/setup_cost_mult/ee_score/ecsc_score)** — R2-design-validation specific. Defer to R2 implementation; add behind `XPRA_R2_DIAG=1` env var when needed.
- **R1 gate diagnostic counters** (skipped vs re-scored, reasons) — R1-design-validation specific. Bake into Phase 1 behind `XPRA_R1_DIAG=1` env var.

### Existing diagnostics (no new code)

- `journalctl --user -u xpra.service | grep 'failed to acquire cuda device lock' | wc -l` → rate per minute.
- `journalctl --user -u xpra.service | grep 'failed to encode h265 frame' | wc -l` → rate per minute.
- `xpra info :100 | grep context_count` → 1 = healthy, 2 = mid-cycle.
- `xpra info :100 | grep reinit_count` (after Phase 0) → aggregate per window.

### Baseline collection

24h of normal use. CSV columns: timestamp, `reinit_count` deltas per active window per 5min, `cdc.lock` warning rate, `failed to encode` rate. The 5min bucket smooths over individual storms; the daily total quantifies steady-state.

### Cost

~15 LOC across `video_compress.py` + `cuda/context.py`. No new helper module. No function-signature changes.

## Phase 1 — R1 candidate-space-only re-scoring

### Candidate-space tuple

Cached on `WindowVideoSource`. The tuple includes both **raw inputs** that the scoring engine consumes AND **derived outputs** whose values cascade from operating-point changes. Critically, derived outputs are computed using the encoder's **hysteretic** logic (Y2 deadband etc.), not raw thresholds — otherwise R1's gate fires on transient oscillations that Y2 would have absorbed:

```python
_candidate_space = (
    self.encoding,                                # "auto", "h264", "hevc", "stream", ...
    self.content_type,                            # "text", "video", "browser", ...
    self.common_video_encodings,                  # tuple of negotiated encodings
    self.pixel_format,                            # input pixel format from window
    self.window_dimensions,                       # (w, h) after width_mask/height_mask
    self.video_subregion.rectangle,               # None or rect
    self.full_csc_modes,                          # client's csc capabilities
    self._candidate_pixel_format_fingerprint,     # hash of per-candidate target-pixel-format map (Y2-hysteretic)
    self._desired_scaling,                        # fresh (num, den) from calculate_scaling for current state
)
```

Two derived elements need explanation:

- **`_candidate_pixel_format_fingerprint`**: a fingerprint of the per-candidate target pixel format map, NOT a single value computed from the current encoder. For each candidate in `common_video_encodings`, compute what its `get_target_pixel_format()`-equivalent would return given current quality (with Y2 deadband applied). Sort and hash the resulting map. This avoids two failure modes the single-value approach has:
  - For `encoding=auto` with mixed candidates (e.g., AV1 stays NV12 while h264/h265 can switch to YUV444P at high quality), the current encoder's pixel format won't change when crossing quality 85, so a single-value approach would suppress the re-score that should pick the higher-quality candidate.
  - The fingerprint inherits Y2's deadband automatically: small oscillations 83↔87 don't change the map, so R1's gate stays closed and no teardown fires.
  - For non-nvenc candidates without pixel-format-vs-quality logic, their entry in the map is constant — they don't perturb the fingerprint.
- **`_desired_scaling`**: fresh output of `calculate_scaling()` given current width/height + auto-tuner state. NOT `self.actual_scaling` (which describes the **installed** pipeline and lags reality). The tuple compares the **desired** scaling against the candidate-space cache. When quality/speed nudges shift `calculate_scaling`'s output, the desired value changes immediately even before any new pipeline exists. `_compute_candidate_space()` computes this fresh each invocation; `self.actual_scaling` is mutated only when a new pipeline is installed.

### Gate (in `update_encoding_options`)

```python
def update_encoding_options(self, force_reload=False):
    super().update_encoding_options(force_reload)
    self.update_encoding_video_subregion()
    new_space = self._compute_candidate_space()
    space_changed = (new_space != self._last_candidate_space)
    if force_reload:
        self.video_context_clean()
    if space_changed or force_reload:
        self.update_pipeline_scores(force_reload)
        self._last_candidate_space = new_space
        if not self.verify_csc_and_encoder() and not force_reload:
            self.video_context_clean()
    else:
        # candidate space unchanged → push operating-point only
        self._push_operating_point()
    self._last_pipeline_check = monotonic()
```

`_push_operating_point()` calls `self._video_encoder.set_encoding_quality(self._current_quality)` and `set_encoding_speed(self._current_speed)` directly. Today these are no-ops at nvenc level; R3 (below) makes them effective.

### Quality/speed change reroute (secondary fix for client-driven changes)

`quality_changed()` and `speed_changed()` overrides in `video_compress.py:804-811` currently call `video_context_clean()` directly. These fire only for client-driven property changes (NOT auto-tuner — auto-tuner uses `update_quality`/`update_speed` instead, which flow through `update_encoding_options` and are caught by the primary R1 gate above). With R1 in place these overrides instead check the candidate-space tuple and only tear down if it changed:

```python
def quality_changed(self, window, *args) -> bool:
    super().quality_changed(window, args)
    self._maybe_invalidate_for_operating_point()
    return True

def speed_changed(self, window, *args) -> bool:
    super().speed_changed(window, args)
    self._maybe_invalidate_for_operating_point()
    return True

def _maybe_invalidate_for_operating_point(self):
    new_space = self._compute_candidate_space()
    if new_space != self._last_candidate_space:
        # candidate space genuinely changed (e.g., YUV444_THRESHOLD crossing
        # or scaling cascade) — full teardown is correct
        self.video_context_clean()  # candidate space changed, full teardown
    else:
        # operating point moved but candidate space stable — push to live encoder
        self._push_operating_point()
```

Less critical than the primary gate (because the auto-tuner doesn't fire these signals), but still a measurable improvement for client-driven quality changes.

### Safety valve

If a poor-fit encoder is selected and R1 keeps it forever, no auto-recovery. Failure counter:

```python
def encode_frame_failed(self):
    self._consecutive_encode_failures += 1
    if self._consecutive_encode_failures >= R1_FORCE_RESELECT_AFTER:
        log.warn(f"forcing encoder re-selection after {n} consecutive failures")
        self.video_context_clean()
        self._last_candidate_space = None  # force re-score
        self._consecutive_encode_failures = 0

def encode_frame_succeeded(self):
    self._consecutive_encode_failures = 0
```

`R1_FORCE_RESELECT_AFTER = envint("XPRA_R1_FORCE_RESELECT_AFTER", 5)`.

### Risks

- Stickiness on subtle changes not in the candidate-space tuple. Mitigated by safety valve + Phase 0 counters showing safety-valve trips.
- Tuple granularity. Too granular → degenerates to today's behavior. Too coarse → miss legit re-selection triggers. Refine on baseline data.

### Cost

~120-150 LOC in `video_compress.py`.

## Phase 2 — R2 edge resistance always-on (+ Y2 threshold hysteresis)

### R2a — decouple setup_cost_mult from detection

`video_scoring.py:115`:

```python
# before
setup_cost_mult = int(detection) * (1 + max(0, MIN_FPS_COST - ffps))

# after
setup_cost_mult = 1 + int(detection) * max(0, MIN_FPS_COST - ffps)
```

`setup_cost_mult` is always at least `1`, so `ee_score = 100 - setup_cost` and `ecsc_score = 80 - setup_cost*80//100` always apply the switching penalty. The `detection` factor still adds an additional fps-based bump when subregion detection is active.

For nvenc (`setup_cost=100`), this drops `ee_score` for a fresh competitor to `0` while the current encoder stays at `100`. A 100-point delta out of a total score in the 200-400 range — substantial.

### R2b — magnitude tuning envvar

`XPRA_EDGE_RESISTANCE_BOOST` (default 0). Added to `ee_score` for the current encoder. Lets us tune stickiness if R2a alone overshoots (sticking too long) without code changes.

### Y2 — YUV444_THRESHOLD hysteresis

Same boundary-stickiness pattern as R2, applied to the quality threshold:

```python
cdef int YUV444_THRESHOLD = envint("XPRA_NVENC_YUV444_THRESHOLD", 85)
cdef int YUV444_DEADBAND = envint("XPRA_NVENC_YUV444_DEADBAND", 5)

def get_target_pixel_format(self, int quality):
    if self.pixel_format == "YUV444P":
        upcross = YUV444_THRESHOLD - YUV444_DEADBAND  # stay in YUV444P until quality < 80
    else:
        upcross = YUV444_THRESHOLD  # switch to YUV444P when quality >= 85
    # ... existing logic using upcross instead of YUV444_THRESHOLD
```

Reduces oscillation around quality=85 by 5 quality points. The auto-tuner typically nudges quality by 1-2 points per step, so a deadband of 5 absorbs single-step flutter at the boundary without hiding a genuine quality drop. Tunable via env var if instrumentation shows we want more (8) or less (3).

### Risks

- Too sticky → miss legitimate score improvements. Mitigation: instrumentation captures `ee_score`/`ecsc_score` of the winner; "current encoder stayed but its score dropped below #2 by N points" is a signal.

### Cost

~30 LOC. One-line for R2a. ~20 LOC for Y2. Optional `XPRA_EDGE_RESISTANCE_BOOST` adds ~5 LOC.

## Phase 3 — R3 bitrate-only reconfigure

### Approach

R3 is genuinely new code — the 2017 code at commit `189a05d01f` only invoked `nvEncReconfigureEncoder` for pixel-format/lossless changes (which reconfigure can't handle, hence the disable). It explicitly returned on bitrate-only changes. There has never been a working bitrate-only reconfigure path in xpra's nvenc encoder.

Pattern: when `set_encoding_quality()`/`set_encoding_speed()` is called and target pixel_format/lossless are unchanged, call `nvEncReconfigureEncoder` with the new bitrate. When pixel_format/lossless WOULD change, fall through to teardown (R1's `_candidate_pixel_format_fingerprint` tuple element catches that case).

### Prerequisite 1: wire up rate control

`update_bitrate()` (encoder.pyx:1334) computes `self.target_bitrate` and `self.max_bitrate` but those are **never pushed to nvenc**. In `tune_qp()` (line 870-908), the rate-control assignments are commented out (lines 901-904):

```python
#rc.averageBitRate = 1
...
#rc.averageBitRate = self.max_bitrate or 10*1024*1024
#rc.maxBitRate = self.max_bitrate or 10*1024*1024
```

Same pattern at lines 1621-1635 (per-frame). So today, `self.target_bitrate` is a vestigial field: it gets computed but nothing reads it.

R3 cannot work without first plumbing these through. The implementation must:

1. Add proper rate-control field population in `tune_qp()`. Replace the commented-out lines with logic that selects the right NV_ENC_PARAMS_RC_MODE (`NV_ENC_PARAMS_RC_VBR` is the safe default for bitrate-target operation) and sets `rc.averageBitRate = self.target_bitrate` and `rc.maxBitRate = self.max_bitrate`.
2. Verify `tune_qp` is called during the reconfigure path (so the new bitrate values land in `reconfigure_params.reInitEncodeParams.encodeConfig->rcParams`).
3. The `tune_qp` change should be additive — preserve any QP-based path that's currently active (currently the function name suggests QP-mode logic exists somewhere, even if rate control assignments are commented out). Read the full `tune_qp` body before deciding which mode to switch to.

### Prerequisite 2: preserve preset across reconfigure

`init_params()` calls `get_preset()` which depends on speed/quality. If we re-invoke `init_params()` with the new speed during reconfigure, `get_preset()` may return a DIFFERENT `presetGUID` than the one the encoder was initialized with. **NVENC does not allow changing presetGUID on an existing encoder via `nvEncReconfigureEncoder`** — the call would fail.

The reconfigure path must construct `reInitEncodeParams` WITHOUT re-running preset selection:

1. Cache the original `presetGUID` (and possibly the whole `NV_ENC_INITIALIZE_PARAMS` snapshot) on the Encoder instance at init time.
2. In `_apply_reconfigure()`, construct `reInitEncodeParams` by copying the cached snapshot, then overlay ONLY the rate-control fields (`rc.averageBitRate`, `rc.maxBitRate`, possibly rate-control mode). Do not call `init_params()` from within reconfigure.
3. In `set_encoding_speed()`, compute what `get_preset()` would return for the new speed and compare to the cached preset. If different → preset boundary crossing → fall through to teardown instead of reconfigure (R1's candidate-space tuple won't catch this directly; we add `self._effective_preset_guid` to it OR detect at the encoder level and signal up via a teardown-eligible path).

This makes R3's "bitrate-only" framing precise: bitrate-only AND same-preset AND same-pixel-format AND same-dims. Anything else falls through to teardown.

```python
def set_encoding_quality(self, int quality) -> None:
    cdef NV_ENC_RECONFIGURE_PARAMS reconfigure_params
    assert self.context, "context is not initialized"
    if self.quality == quality:
        return
    self.quality = quality
    target_quality = self._compute_target_quality(quality)
    new_pixel_format = self.get_target_pixel_format(target_quality)
    new_lossless = self.get_target_lossless(new_pixel_format, target_quality)
    if new_pixel_format != self.pixel_format or new_lossless != self.lossless:
        # pixel format change → cannot reconfigure, fall through to teardown
        # (R1's _candidate_pixel_format_fingerprint tuple element will catch it on next tick)
        return
    self.update_bitrate()
    self._apply_reconfigure(reset_encoder=1, force_idr=1)

cdef _apply_reconfigure(self, int reset_encoder, int force_idr):
    # Per Prerequisite 2 below: do NOT call self.init_params() here —
    # it re-runs get_preset() and would change presetGUID, which nvenc
    # reconfigure forbids. Use the cached init-params snapshot taken at
    # init time and overlay only the rate-control fields.
    cdef NV_ENC_RECONFIGURE_PARAMS reconfigure_params
    memset(&reconfigure_params, 0, sizeof(NV_ENC_RECONFIGURE_PARAMS))
    reconfigure_params.version = NV_ENC_RECONFIGURE_PARAMS_VER
    try:
        # Copy the cached init-params snapshot (struct copy + deep copy
        # of any heap-allocated fields like encodeConfig). The snapshot
        # was captured into self._init_params_snapshot at successful init.
        self._copy_cached_init_params(&reconfigure_params.reInitEncodeParams)
        # Overlay updated rate-control fields from self.target_bitrate /
        # self.max_bitrate. tune_qp() should be reused for this; once R3
        # prerequisite 1 lands it populates rc.averageBitRate / maxBitRate.
        if reconfigure_params.reInitEncodeParams.encodeConfig != NULL:
            self.tune_qp(&reconfigure_params.reInitEncodeParams.encodeConfig.rcParams)
        reconfigure_params.resetEncoder = reset_encoder
        reconfigure_params.forceIDR = force_idr
        with nogil:
            r = self.functionList.nvEncReconfigureEncoder(self.context, &reconfigure_params)
        raiseNVENC(r, "reconfiguring encoder")
    finally:
        if reconfigure_params.reInitEncodeParams.encodeConfig != NULL:
            free(reconfigure_params.reInitEncodeParams.encodeConfig)
```

`set_encoding_speed` likewise calls `_apply_reconfigure` after `update_bitrate`, but only after first verifying that `get_preset()` for the new speed still returns the same `presetGUID` as cached (see Prerequisite 2).

### Lock interaction

Need to read existing locking model in `compress_image()` before finalizing. Per SDK 13 docs, `nvEncReconfigureEncoder` is thread-safe relative to other nvenc calls on the same encoder, but the encoder's own per-instance lock must serialize reconfigure vs `nvEncEncodePicture`. Plan to use the same lock pattern compress_image already uses; revisit during implementation if there's a gap.

### Test

The 2017 test commit `8151e96537` is not reusable — it tested the pixel-format reconfigure path (which never worked). New test required: exercise `set_encoding_quality(q1)` then `set_encoding_quality(q2)` where both q1 and q2 map to the same pixel_format/lossless, verify `nvEncReconfigureEncoder` was invoked (mock or trace), verify subsequent frames encode with the new bitrate target, verify no teardown happened (encoder instance ID stays the same).

### Risks

- `init_params()` may have drifted relative to what `nvEncReconfigureEncoder` expects in its `reInitEncodeParams` field. Need to validate that `init_params(self.codec, &reInitEncodeParams)` produces a structurally valid `NV_ENC_INITIALIZE_PARAMS` that nvenc accepts post-init.
- `resetEncoder=1` forces an IDR keyframe on the next frame; bitrate-only changes cause a momentary keyframe spike. Probably fine; instrumentation should confirm.
- Reconfigure rate-limiting: if the auto-tuner nudges quality every 100ms, we shouldn't invoke `nvEncReconfigureEncoder` every 100ms either — debounce or coalesce in `set_encoding_quality` (e.g., min 250ms between reconfigure calls).

### Cost

~120-180 LOC in `encoder.pyx` (reconfigure path + rate-control plumbing in `tune_qp`) + 1-2 new tests (one for bitrate-only reconfigure, one for tune_qp rate-control population).

## ~~Phase 4~~ — Dropped

Earlier drafts proposed a per-cuda-device init queue to serialize encoder inits. Investigation (see "Why no init queue" in the Architecture section) showed:

1. `threaded_init_device()` already holds `device_lock` across `init_device()`, serializing init-vs-init.
2. The `failed to acquire cuda device lock` warnings come from a non-blocking acquire in `compress_image()` on OTHER encoders sharing the same device, not from init-vs-init contention.
3. The fix for the visible signal is reducing init frequency (R1/R2/R3), not serializing concurrent inits that aren't actually concurrent.

The phase is retained as a section heading for traceability — if implementation reveals init-vs-init contention exists in some path we missed, the queue design above (preserved in git history) can be revived.

## Branch & deploy strategy

| Phase | Branch | Off | Deploy |
|---|---|---|---|
| 0 | `feature/nvenc-reinit-instrument-v6.4.3` | `v6.4.3-achen` | `build-deb.sh --deploy` |
| 0 | `feature/nvenc-reinit-instrument-master` | `origin/master` | port for upstream PR |
| 1-3 | `feature/nvenc-reinit-storm-v6.4.3` | post-Phase-0 baseline | `build-deb.sh --deploy` |
| 1-3 | `feature/nvenc-reinit-storm-master` | `origin/master` | port for upstream PR |

Phase 0 ships to orbital first. After 24-72h of baseline collection, R1-R3 land together on the storm branch. Upstream PRs follow master ports.

## Verification

### Phase 0
- Aggregate counter appears in `xpra info :100 | grep reinit_count` for every active window.
- Manual test: trigger any teardown (`xpra control :100 encoding h264`) → confirm `reinit_count` increments. No-op cleanup paths (cancel, repeated cleanup) must NOT increment.
- `cdc.lock` warning enrichment: trigger a tab-restore storm; confirm at least one enriched warning shows current-holder + phase fields.
- 24h soak collects baseline.

### R1-R3 gates (from memo)
1. Storm repro (new script): N Edge tab windows on server, HEVC 4:4:4 video in one, rapid focus switches. Pre-R1: `cdc.lock` failures within 60s. Post-R1-R3: zero or near-zero.
2. Live: `xpra info :100 | grep context_count` shows `1` for active video window across ≥1 min of playback + quality changes + tab switches.
3. Journal: < 1 `failed to acquire cuda device lock` per minute under normal load (drops naturally as init frequency falls).
4. Phase 0 counters: total reinits/min on browser windows drops by >80% vs baseline; `quality_changed`/`speed_changed` counters drop to ≈0.
5. codex review + Opus subagent review of the diff before push.

### Unit tests
- R1 gate: candidate-space tuple changes trigger re-score; operating-point nudges don't.
- R1 reroute: `quality_changed`/`speed_changed` with stable candidate space does NOT call `video_context_clean`; with changed candidate space DOES.
- R2a: `setup_cost_mult >= 1` regardless of `detection`; `ee_score` decreases when current encoder doesn't match.
- Y2: quality oscillation 83-87 with `XPRA_NVENC_YUV444_DEADBAND=5` (default) doesn't flip pixel format; a drop from 86 → 79 (>deadband) does.
- R3 bitrate-only: `set_encoding_quality(q1)` → `set_encoding_quality(q2)` where both map to same pixel_format/lossless invokes `nvEncReconfigureEncoder`, no teardown, encoder instance stays same.
- R3 pixel-format change: `set_encoding_quality` crossing the threshold band falls through (no reconfigure call); R1's tuple catches the case on next `update_encoding_options`.

### Storm regression test
- Parameterized version of the repro script; CI gate if reinit/min exceeds threshold.

## What NOT to do (carry-forward from memo)

- Don't rewrite PR #4875's three-part cleanup fix. Load-bearing for the SEGV path.
- Don't use `tests/scripts/nvenc_cleanup_repro.py` as the gate for this work — it catches the SEGV/cleanup leak pattern, not the storm.
- Don't suppress the `cdc.lock` warnings — they're the diagnostic signal that init frequency is too high. They fall off naturally as R1/R2/R3 land.
- Don't disable nvenc as a workaround.
- Don't try to revive the 2017 reconfigure test (`8151e96537`) — it tested the never-working pixel-format reconfigure path, not bitrate-only.

## Open questions / known unknowns

1. **Lock interaction on R3.** `compress_image()` takes `cdc.lock` via `cuda_device_context.__enter__` during encode. `nvEncReconfigureEncoder` per SDK 13 docs is thread-safe vs other nvenc calls but the encoder's own per-instance locking model needs to be re-read at implementation time to ensure reconfigure doesn't race with an in-flight encode. Plan resolves during implementation.
2. **Phase 0 baseline duration.** 24h is the proposed minimum; may extend if storm rate is highly variable across workdays.
3. **R3 cost-benefit if R1 alone is sufficient.** If Phase 0 data shows R1 reroute reduces reinit rate by >95%, R3 becomes a smaller win (most operating-point nudges no longer reach the encoder anyway). Still worth shipping for the runtime-bitrate capability and to make R1's `_push_operating_point()` path actually do something at the encoder level, but the framing shifts from "storm fix" to "capability unlock + bitrate accuracy."
4. **Subregion-aware edge resistance.** Original `detection` gate on `setup_cost_mult` was added for a reason that's no longer documented. Possible original intent: when subregion detection is active, more aggressive bias because re-scoring is part of the detection flow. R2a removes the gate entirely; we may need to re-add a softer version (e.g., `setup_cost_mult = 1 + int(detection) * X` where X is calibrated) if instrumentation shows R2a sticks too aggressively in subregion-detection scenarios.

## Future work (deferred)

### Dynamic codec switching for non-subregion activity

R1+R2+R3 handle two of three browser-interactivity scenarios:

- **Light browsing** (low fps, no subregion): R2's setup-cost penalty biases initial pick toward simple codecs (webp/jpeg/nvjpeg); R1 keeps that pick stable through quality nudges. ✓
- **Video starts playing** (subregion detection finds the rectangle): `video_subregion.rectangle` is already in R1's candidate-space tuple, so the None → Rect transition triggers re-score; with `detection=True`, fps bonus is active and nvenc wins. ✓
- **Full-window animation / "crazy" pages** (high fps, but motion is global so subregion detection doesn't latch): no tuple element changes → R1 stays locked on the initial (simple) codec → pays bandwidth cost for what could have been encoded by nvenc. ✗

The third case is deferred. If Phase 0 + post-deployment data shows it's a problem in practice (e.g., bandwidth usage stays elevated on heavy-animation sites because nvenc never engages), add an `_fps_band` tuple element with hysteresis (3 bands: quiet / active / heavy; asymmetric thresholds to avoid flapping). Pair with R2 de-gating: drop the `int(detection)` factor from the fps bonus in `setup_cost_mult` so the existing fps-vs-setup-cost mechanism works for non-subregion windows too. Estimated cost ~30 LOC + tuning. The two changes go together — R2 de-gating without `_fps_band` is a no-op because R1 prevents the scoring that would consume the new math.

Why deferred: subregion detection probably covers most "real" video scenarios; the value of catching the remaining edge case is unknown without measurement; introducing yet another hysteresis on a noisy signal (fps) adds tuning surface we shouldn't pick blind.

## Sponsored-By

Sponsored-By: Netflix
