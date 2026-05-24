<!--
CODEX: this file is a design specification for the nvenc reinit-storm
fix in xpra. The document itself IS the deliverable for this commit —
no executable code changes accompany it intentionally; implementation
happens in follow-up commits per the plan inside.

This is the SECOND pass of this spec. The first pass had four levers
(R1, R2, R3, R4); your prior review correctly flagged that (a) Phase 0
instrumented the wrong choke point, (b) R1's candidate-space tuple
missed scaling cascade, (c) R3 framing as "revival of 2017 code" was
wrong (the 2017 code never had a bitrate-only path), and (d) R4 as
"per-device init queue" was redundant because device_lock already
serializes init-vs-init. This pass corrects all four; R4 is dropped
entirely.

Please review the PROSE substantively: are the technical claims correct
against the referenced code (file paths, line numbers, commit SHAs)?
Is the three-lever architecture (R1 candidate-space gate + quality/
speed reroute, R2 edge resistance always-on, R3 bitrate-only nvenc
reconfigure) sound? Are root causes correctly attributed? Specifically
check: is the cdc.lock contention story in "Why no init queue" right?
Is `quality_changed`/`speed_changed` actually going through
`video_context_clean()` directly without passing through
`update_encoding_options`? Flag misleading framing, internal
inconsistencies, or anything technically wrong. Ignore the fact that
no executable code is modified — that is by design for this commit.
Treat the file's location and tracked-ness as out of scope.
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

Two distinct paths to teardown that R1 must intercept:

1. **`update_encoding_options()`** runs on a timer (~every few seconds) AND on content-type changes. Each invocation calls `update_pipeline_scores()` which can pick a different winner.
2. **`quality_changed()`** (line 806) and **`speed_changed()`** (line 811) call `video_context_clean()` *directly*, bypassing `update_encoding_options` entirely. Every operating-point nudge from the auto-tuner currently tears down the encoder before any scoring or stickiness logic gets a chance.

For browser workloads where content_type and dimensions are stable for minutes but the auto-tuner generates sustained quality/speed nudges, this means **the encoder is torn down on every nudge**. With close-scoring candidates (h264 vs hevc, NV12 vs YUV444P at quality threshold boundaries), even when scoring would have picked the same winner, the teardown still happens. **This is the storm's dominant engine for browser/text-y workloads.**

R1 must therefore (a) reroute `quality_changed`/`speed_changed` to skip teardown when the candidate space is unchanged, and (b) gate `update_encoding_options` on candidate-space tuple comparison.

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

## Phase 0 — Instrumentation

Ships alone first on `feature/nvenc-reinit-instrument-v6.4.3`, port to `feature/nvenc-reinit-instrument-master`. Build via `./tests/docker/build-deb.sh --deploy`. Baseline measurement: 24h of normal use including at least one Edge tab-restore + one sustained video playback. Dump `xpra info :100 | jq '.. | .reinit_counters? // empty'` hourly into a CSV.

### Counters

1. **Reinit-by-path** (per-window dict): path name → count. Path names match `video_context_clean()` call sites — note that this is the actual choke point, NOT `cleanup_codecs()` which is just one of several wrappers:
   - `"force_reload"` — `update_encoding_options(force_reload=True)`
   - `"verify_failed"` — `update_encoding_options` via `verify_csc_and_encoder()` returning False
   - `"verify_failed_after_space_change"` — same as above but with R1 in place (post-Phase 1)
   - `"no_video"` — `update_pipeline_scores` `checknovideo()` paths
   - `"new_encoding"` — `set_new_encoding`
   - `"video_subregion"` — `update_encoding_video_subregion` triggered teardown
   - `"quality_changed"` — direct call from `quality_changed()` (line 806) — expected to be a dominant pre-R1 signal
   - `"speed_changed"` — direct call from `speed_changed()` (line 811) — expected to be a dominant pre-R1 signal
   - `"encoder_timeout"` — `video_encoder_timeout()` (line 2724)
   - `"encoder_closed"` — direct call when active encoder reports closed (line 2619)
   - `"safety_valve"` — R1's consecutive-failure safety valve (post-Phase 1)
   - `"shutdown"` — cleanup at shutdown (filtered from storm view)
   - `"unspecified"` — anything missed (must stay at 0 in steady state)

2. **Delta-type** (per-window dict): bumped after teardown + new init by comparing pre-teardown spec vs new spec:
   - `"bitrate_only"` — pixel_format, dims, codec_type, csc, scaling identical (R3's potential reach)
   - `"pixel_format_change"` — pixel_format differs
   - `"dim_change"` — encoder dims differ
   - `"codec_change"` — codec_type differs
   - `"csc_change"` — csc dst format differs
   - `"scaling_change"` — scaling tuple (output of `calculate_scaling`) differs

3. **Edge resistance snapshot** (per-window): last winner's `detection`, `setup_cost_mult`, `ee_score`, `ecsc_score`. Validates R2's assumption that `detection=False` is common.

4. **cdc.lock blast-radius marker** (per-device, journal-level): on every `TransientCodecException` from `cuda_device_context.__enter__`, log which encoder currently holds the lock and what phase (init / cleanup / compress). Confirms the "init-vs-encode on shared device" diagnosis and quantifies blast radius. Logged inline with the existing warning, not aggregated.

### Implementation

`video_context_clean()` (line 404 in `video_compress.py`) is the actual choke point — `cleanup_codecs()`, `quality_changed()`, `speed_changed()`, `video_encoder_timeout()`, and the encoder-closed handling all funnel through it. Add a `reason: str = "unspecified"` parameter; each caller passes its identifier. For delta-type, `prev_spec_snapshot` captures `(pixel_format, width, height, codec_type, csc_dst_format, scaling)` before teardown; after new encoder is built, diff against new spec.

`get_pipeline_score()` extends its return tuple with `(detection, setup_cost_mult, ee_score, ecsc_score)` as a debug field; `WindowVideoSource` stores the last-winner's snapshot in a field, exposed via `get_info()`.

`WindowVideoSource.get_info()` adds `reinit_counters` sub-dict. Surfaces in `xpra info :100`.

Counters bump only on teardown — zero hot-path cost.

### Cost

~50-80 LOC across `video_compress.py` + `video_scoring.py` + a small `reinit_stats.py` helper.

## Phase 1 — R1 candidate-space-only re-scoring

### Candidate-space tuple

Cached on `WindowVideoSource`. The tuple includes both **raw inputs** that the scoring engine consumes AND **derived outputs** whose values cascade from operating-point changes:

```python
_candidate_space = (
    self.encoding,                    # "auto", "h264", "hevc", "stream", ...
    self.content_type,                # "text", "video", "browser", ...
    self.common_video_encodings,      # tuple of negotiated encodings
    self.pixel_format,                # input pixel format from window
    self.window_dimensions,           # (w, h) after width_mask/height_mask
    self.video_subregion.rectangle,   # None or rect
    self.full_csc_modes,              # client's csc capabilities
    self._target_quality_band,        # 0 if quality < YUV444_THRESHOLD else 1
    self.actual_scaling,              # current (num, den) result from calculate_scaling
)
```

Two derived elements need explanation:

- **`_target_quality_band`**: handles YUV444_THRESHOLD. Any quality crossing the threshold flips the band → tuple inequality → re-score is required because pixel format changes. (The threshold-deadband hysteresis (Y2) below reduces oscillation crossings.)
- **`actual_scaling`**: `calculate_scaling()` consumes many inputs (`scaling_control`, `client_render_size`, `actual_scaling`, fullscreen heuristics in `update_actual_scaling`). Several of those cascade from quality/speed in auto-tuner mode. Capturing the **result** rather than the inputs is structurally correct: if scaling stays the same, the encoder dimensions stay the same and re-scoring is unnecessary; if scaling changes, encoder dimensions change and re-scoring is required. The cached value is refreshed inside `_compute_candidate_space()` before comparison.

### Gate (in `update_encoding_options`)

```python
def update_encoding_options(self, force_reload=False):
    super().update_encoding_options(force_reload)
    self.update_encoding_video_subregion()
    new_space = self._compute_candidate_space()
    space_changed = (new_space != self._last_candidate_space)
    if force_reload:
        self.video_context_clean("force_reload")
    if space_changed or force_reload:
        self.update_pipeline_scores(force_reload)
        self._last_candidate_space = new_space
        if not self.verify_csc_and_encoder() and not force_reload:
            self.video_context_clean("verify_failed_after_space_change")
    else:
        # candidate space unchanged → push operating-point only
        self._push_operating_point()
    self._last_pipeline_check = monotonic()
```

`_push_operating_point()` calls `self._video_encoder.set_encoding_quality(self._current_quality)` and `set_encoding_speed(self._current_speed)` directly. Today these are no-ops at nvenc level; R3 (below) makes them effective.

### Quality/speed change reroute (the dominant storm fix)

`quality_changed()` and `speed_changed()` currently call `video_context_clean()` directly (line 806, 811). With R1 in place they must instead check the candidate-space tuple and only tear down if it changed:

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
        self.video_context_clean("quality_changed")  # or "speed_changed"
    else:
        # operating point moved but candidate space stable — push to live encoder
        self._push_operating_point()
```

This is the single biggest behavioral change R1 makes. Without it, the candidate-space gate in `update_encoding_options` is ineffective because every auto-tuner nudge tears down the encoder before that gate runs.

### Safety valve

If a poor-fit encoder is selected and R1 keeps it forever, no auto-recovery. Failure counter:

```python
def encode_frame_failed(self):
    self._consecutive_encode_failures += 1
    if self._consecutive_encode_failures >= R1_FORCE_RESELECT_AFTER:
        log.warn(f"forcing encoder re-selection after {n} consecutive failures")
        self.video_context_clean("safety_valve")
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

Pattern: when `set_encoding_quality()`/`set_encoding_speed()` is called and target pixel_format/lossless are unchanged, call `nvEncReconfigureEncoder` with the new bitrate. When pixel_format/lossless WOULD change, fall through to teardown (R1's `_target_quality_band` tuple element catches that case).

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
        # (R1's _target_quality_band tuple element will catch it on next tick)
        return
    self.update_bitrate()
    self._apply_reconfigure(reset_encoder=1, force_idr=1)

cdef _apply_reconfigure(self, int reset_encoder, int force_idr):
    cdef NV_ENC_RECONFIGURE_PARAMS reconfigure_params
    memset(&reconfigure_params, 0, sizeof(NV_ENC_RECONFIGURE_PARAMS))
    reconfigure_params.version = NV_ENC_RECONFIGURE_PARAMS_VER
    try:
        self.init_params(self.codec, &reconfigure_params.reInitEncodeParams)
        reconfigure_params.resetEncoder = reset_encoder
        reconfigure_params.forceIDR = force_idr
        with nogil:
            r = self.functionList.nvEncReconfigureEncoder(self.context, &reconfigure_params)
        raiseNVENC(r, "reconfiguring encoder")
    finally:
        if reconfigure_params.reInitEncodeParams.encodeConfig != NULL:
            free(reconfigure_params.reInitEncodeParams.encodeConfig)
```

`set_encoding_speed` likewise calls `_apply_reconfigure` after `update_bitrate`.

### Lock interaction

Need to read existing locking model in `compress_image()` before finalizing. Per SDK 13 docs, `nvEncReconfigureEncoder` is thread-safe relative to other nvenc calls on the same encoder, but the encoder's own per-instance lock must serialize reconfigure vs `nvEncEncodePicture`. Plan to use the same lock pattern compress_image already uses; revisit during implementation if there's a gap.

### Test

The 2017 test commit `8151e96537` is not reusable — it tested the pixel-format reconfigure path (which never worked). New test required: exercise `set_encoding_quality(q1)` then `set_encoding_quality(q2)` where both q1 and q2 map to the same pixel_format/lossless, verify `nvEncReconfigureEncoder` was invoked (mock or trace), verify subsequent frames encode with the new bitrate target, verify no teardown happened (encoder instance ID stays the same).

### Risks

- `init_params()` may have drifted relative to what `nvEncReconfigureEncoder` expects in its `reInitEncodeParams` field. Need to validate that `init_params(self.codec, &reInitEncodeParams)` produces a structurally valid `NV_ENC_INITIALIZE_PARAMS` that nvenc accepts post-init.
- `resetEncoder=1` forces an IDR keyframe on the next frame; bitrate-only changes cause a momentary keyframe spike. Probably fine; instrumentation should confirm.
- Reconfigure rate-limiting: if the auto-tuner nudges quality every 100ms, we shouldn't invoke `nvEncReconfigureEncoder` every 100ms either — debounce or coalesce in `set_encoding_quality` (e.g., min 250ms between reconfigure calls).

### Cost

~80-100 LOC in `encoder.pyx` + 1 new test.

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
- Counter dict appears in `xpra info :100 | grep -A30 reinit_counters` for every active window.
- Manual test: trigger `force_reload` via `xpra control :100 encoding h264` → confirm `force_reload` counter bumps.
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

## Sponsored-By

Sponsored-By: Netflix
