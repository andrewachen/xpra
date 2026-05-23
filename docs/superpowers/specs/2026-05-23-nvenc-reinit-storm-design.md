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

Four independent levers at four different code layers. None are symptom fixes — each addresses a distinct architectural contribution.

### R1 — Selection coupled to operating-point nudges

`update_encoding_options()` runs on a timer (~every few seconds) AND on every quality/speed/content-type nudge. Each invocation calls `update_pipeline_scores()` which can pick a different winner. For browser workloads where content_type and dimensions are stable for minutes but the auto-tuner generates sustained quality/speed nudges, this means the winner can flip on every nudge. With close-scoring candidates (h264 vs hevc, NV12 vs YUV444P at quality threshold boundaries), small operating-point shifts flip the winner. **This is the storm's dominant engine for browser/text-y workloads.**

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

Commit `189a05d01f` (July 2017, issue #1550) disabled `nvEncReconfigureEncoder` because the old code path tried to switch *pixel format* via reconfigure (which is impossible — would require new buffers). Rather than guard reconfigure to bitrate-only changes, totaam disabled it entirely:

```python
def set_encoding_quality(self, int quality) -> None:
    #cdef NV_ENC_RECONFIGURE_PARAMS reconfigure_params
    assert self.context, "context is not initialized"
    if self.quality == quality:
        return
    # ... compute target_quality ...
    self.quality = quality
    # code removed:
    # new_pixel_format = self.get_target_pixel_format(target_quality)
    # ...
    # we can't switch pixel format, ... best to just tear down ...
    return
```

The bitrate-only reconfigure path was thrown out with the bathwater. nvenc has no way to apply a runtime bitrate change today; every quality/speed nudge that reaches the encoder is a no-op, and if scoring decides the no-op encoder is no longer best, teardown follows.

### R4 — Parallel inits contend on cdc.lock

`device_lock` serializes the module-globals-touching part of init, but the heavyweight part (CUDA buffer allocation, `nvEncRegisterResource`, kernel load) runs outside `device_lock` to avoid the "fleet-wide outage" failure mode #4875 documented. So multiple inits can be in their heavy phase simultaneously, all fighting on `cdc.lock`.

Even after R1+R2+R3 reduce reinit frequency, some legitimate swaps (content_type changes, dimension changes, codec changes) will still occur. Those legitimate swaps must not fight each other.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ video_compress.py (WindowVideoSource)                           │
│                                                                 │
│  update_encoding_options() ──── R1 candidate-space gate ────┐   │
│       │                                                     │   │
│       ├─→ update_pipeline_scores()                          │   │
│       │      │                                              │   │
│       │      └─→ get_pipeline_score() in video_scoring.py   │   │
│       │             │                                       │   │
│       │             └── R2 edge resistance (always-on)      │   │
│       │                                                     │   │
│       └─→ verify_csc_and_encoder() ── R1 safety valve  ────┘   │
│                                                                 │
│  Phase 0: per-path reinit counters dumped via get_info()        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (on score change → teardown)
┌─────────────────────────────────────────────────────────────────┐
│ nvenc/encoder.pyx                                               │
│                                                                 │
│  set_encoding_quality()/_speed() ── R3 bitrate reconfigure      │
│       (pixel-format guard, else fall through to teardown)       │
│                                                                 │
│  init_context() / threaded_init_device() ── init-queue entry    │
│       │                                                         │
│       └── Phase 4 device init queue (per-CUDA-device)           │
└─────────────────────────────────────────────────────────────────┘
```

The four levers are at four distinct code locations:
- R1 lives at the WindowSource scoring trigger (`video_compress.py:update_encoding_options`).
- R2 lives inside the scoring math (`video_scoring.py:get_pipeline_score`).
- R3 lives at the encoder bitrate path (`nvenc/encoder.pyx:set_encoding_{quality,speed}`).
- R4 lives at the device-allocation entry (`cuda_context.py` or wherever `cuda_device_context` is defined).

None modify the same code blocks. They reinforce: R1 prevents most scoring runs → R2 only kicks in for the runs R1 lets through → R3 makes the operating-point nudges R1 lets through (e.g., YUV444_THRESHOLD crossing) cheap → R4 makes the residual legitimate swaps graceful.

## Phase 0 — Instrumentation

Ships alone first on `feature/nvenc-reinit-instrument-v6.4.3`, port to `feature/nvenc-reinit-instrument-master`. Build via `./tests/docker/build-deb.sh --deploy`. Baseline measurement: 24h of normal use including at least one Edge tab-restore + one sustained video playback. Dump `xpra info :100 | jq '.. | .reinit_counters? // empty'` hourly into a CSV.

### Counters

1. **Reinit-by-path** (per-window dict): path name → count. Path names match `cleanup_codecs()` call sites:
   - `"force_reload"` — `update_encoding_options(force_reload=True)`
   - `"verify_failed"` — `update_encoding_options` via `verify_csc_and_encoder()` returning False
   - `"verify_failed_after_space_change"` — same as above but with R1 in place (post-Phase 1)
   - `"no_video"` — `update_pipeline_scores` `checknovideo()` paths
   - `"new_encoding"` — `set_new_encoding`
   - `"video_subregion"` — `update_encoding_video_subregion` triggered teardown
   - `"safety_valve"` — R1's consecutive-failure safety valve (post-Phase 1)
   - `"shutdown"` — cleanup at shutdown (filtered from storm view)
   - `"unspecified"` — anything missed

2. **Delta-type** (per-window dict): bumped after teardown + new init by comparing pre-teardown spec vs new spec:
   - `"bitrate_only"` — pixel_format, dims, codec_type, csc identical (R3's potential reach)
   - `"pixel_format_change"` — pixel_format differs
   - `"dim_change"` — dims differ
   - `"codec_change"` — codec_type differs
   - `"csc_change"` — csc dst format differs

3. **Edge resistance snapshot** (per-window): last winner's `detection`, `setup_cost_mult`, `ee_score`, `ecsc_score`. Validates R2's assumption that `detection=False` is common.

4. **Init queue depth** (per-cuda-device, post-Phase 4): high-watermark of queue depth.

### Implementation

`cleanup_codecs(reason: str, prev_spec_snapshot=None)` becomes the choke point. Add a `reason` parameter (default `"unspecified"` so anything missed is visible). Each call site passes its identifier. For delta-type, `prev_spec_snapshot` captures `(pixel_format, width, height, codec_type, csc_dst_format, scaling)` before teardown; after new encoder is built, diff against new spec.

`get_pipeline_score()` extends its return tuple with `(detection, setup_cost_mult, ee_score, ecsc_score)` as a debug field; `WindowVideoSource` stores the last-winner's snapshot in a field, exposed via `get_info()`.

`WindowVideoSource.get_info()` adds `reinit_counters` sub-dict. Surfaces in `xpra info :100`.

Counters bump only on teardown — zero hot-path cost.

### Cost

~50-80 LOC across `video_compress.py` + `video_scoring.py` + a small `reinit_stats.py` helper.

## Phase 1 — R1 candidate-space-only re-scoring

### Candidate-space tuple

Cached on `WindowVideoSource`:

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
)
```

`_target_quality_band` is the simple handling for YUV444_THRESHOLD: any quality crossing the threshold flips the band → tuple inequality → re-score. The threshold-deadband hysteresis (Y2) is folded into R2's stickiness theme below.

### Gate

```python
def update_encoding_options(self, force_reload=False):
    super().update_encoding_options(force_reload)
    self.update_encoding_video_subregion()
    new_space = self._compute_candidate_space()
    space_changed = (new_space != self._last_candidate_space)
    if force_reload:
        self.cleanup_codecs("force_reload")
    if space_changed or force_reload:
        self.update_pipeline_scores(force_reload)
        self._last_candidate_space = new_space
        if not self.verify_csc_and_encoder() and not force_reload:
            self.cleanup_codecs("verify_failed_after_space_change")
    else:
        # candidate space unchanged → push operating-point only
        self._push_operating_point()
    self._last_pipeline_check = monotonic()
```

`_push_operating_point()` calls `self._video_encoder.set_encoding_quality(self._current_quality)` and `set_encoding_speed(self._current_speed)` directly. Today these are no-ops at nvenc level; R3 (below) makes them effective.

### Safety valve

If a poor-fit encoder is selected and R1 keeps it forever, no auto-recovery. Failure counter:

```python
def encode_frame_failed(self):
    self._consecutive_encode_failures += 1
    if self._consecutive_encode_failures >= R1_FORCE_RESELECT_AFTER:
        log.warn(f"forcing encoder re-selection after {n} consecutive failures")
        self.cleanup_codecs("safety_valve")
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

Revive the 2017 code (commit `189a05d01f`), guarded to bitrate-only changes:

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

### Test resurrection

Commit `8151e96537` ("add test for nvenc reconfigure") had a test exercising the reconfigure path. Pull from history, adapt to current test scaffolding.

### Risks

- Resurrected code has 10 years of drift relative to current `init_params()` semantics. Need to re-validate that `init_params(self.codec, &reInitEncodeParams)` produces a structurally valid `NV_ENC_INITIALIZE_PARAMS`.
- `resetEncoder=1` forces an IDR; bitrate-only changes cause momentary keyframe spike. Probably fine.

### Cost

~80-100 LOC in `encoder.pyx` + 1 test file.

## Phase 4 — Device init serialization

### Approach

Per-`cuda_device_context` init queue:

```python
class cuda_device_context:
    # existing fields...
    self.init_queue = queue.Queue()
    self.init_serializer = threading.Thread(
        target=self._init_serializer_loop, daemon=True, name="cuda-init-serializer"
    )
    self.init_serializer.start()

    def _init_serializer_loop(self):
        while not self._shutdown:
            init_fn, done_event, result_box = self.init_queue.get()
            if init_fn is None:
                break
            try:
                result_box["value"] = init_fn()
            except Exception as e:
                result_box["error"] = e
            finally:
                done_event.set()

    def serialize_init(self, init_fn):
        done = threading.Event()
        box = {}
        self.init_queue.put((init_fn, done, box))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["value"]
```

`Encoder.threaded_init_device()` wraps its heavy-init body in `self.cuda_device_context.serialize_init(lambda: self._do_heavy_init())`.

### Queue vs lock

A queue gives FIFO fairness — first-arrived first-served — important during storms where many inits arrive in the same ms-window. With a contended lock there's no fairness guarantee.

### Interaction with existing locks (PR #4875)

- `device_lock` — serializes INIT module-globals touch (unchanged).
- `init_complete` event — per-encoder cleanup-vs-init serialization (unchanged).
- `cdc.lock` — cleanup vs compress_image (unchanged).
- New init queue — init-vs-init serialization per device.

Different axes; all four needed.

### Per-init timeout

5s timeout in the queued init body raises `TransientCodecException`, surfaces as "failed to encode" upstream. Prevents one hung init from blocking the whole device's queue. Not a regression — today's parallel inits already fail under cdc.lock contention; this centralizes the failure.

### Cost

~80 LOC in `cuda_context.py` + ~10 LOC plumbing in `encoder.pyx`.

## Branch & deploy strategy

| Phase | Branch | Off | Deploy |
|---|---|---|---|
| 0 | `feature/nvenc-reinit-instrument-v6.4.3` | `v6.4.3-achen` | `build-deb.sh --deploy` |
| 0 | `feature/nvenc-reinit-instrument-master` | `origin/master` | port for upstream PR |
| 1-4 | `feature/nvenc-reinit-storm-v6.4.3` | post-Phase-0 baseline | `build-deb.sh --deploy` |
| 1-4 | `feature/nvenc-reinit-storm-master` | `origin/master` | port for upstream PR |

Phase 0 ships to orbital first. After 24-72h of baseline collection, R1-R4 land together on the storm branch. Upstream PRs follow master ports.

## Verification

### Phase 0
- Counter dict appears in `xpra info :100 | grep -A30 reinit_counters` for every active window.
- Manual test: trigger `force_reload` via `xpra control :100 encoding h264` → confirm `force_reload` counter bumps.
- 24h soak collects baseline.

### R1-R4 gates (from memo)
1. Storm repro (new script): N Edge tab windows on server, HEVC 4:4:4 video in one, rapid focus switches. Pre-R1: `cdc.lock` failures within 60s. Post-R1+R4: zero.
2. Live: `xpra info :100 | grep context_count` shows `1` for active video window across ≥1 min of playback + quality changes + tab switches.
3. Journal: < 1 `failed to acquire cuda device lock` per minute under normal load.
4. Phase 0 counters: total reinits/min on browser windows drops by >80% vs baseline.
5. codex review + Opus subagent review of the diff before push.

### Unit tests
- R1: candidate-space tuple changes trigger re-score; operating-point nudges don't.
- R2a: `setup_cost_mult >= 1` regardless of `detection`; `ee_score` decreases when current encoder doesn't match.
- Y2: quality oscillation 83-87 with `XPRA_NVENC_YUV444_DEADBAND=5` (default) doesn't flip pixel format; a drop from 86 → 79 (>deadband) does.
- R3: bitrate-only reconfigure does NOT teardown; pixel-format change DOES (fallthrough).
- R4: init queue serializes; queue depth never exceeds N during stress.

### Integration test
- Resurrect `8151e96537` (nvenc reconfigure test), adapt to current scaffolding.

### Storm regression test
- Parameterized version of the repro script; CI gate if reinit/min exceeds threshold.

## What NOT to do (carry-forward from memo)

- Don't rewrite PR #4875's three-part cleanup fix. Load-bearing for the SEGV path.
- Don't use `tests/scripts/nvenc_cleanup_repro.py` as the gate for this work — it catches the SEGV/cleanup leak pattern, not the storm.
- Don't suppress the `cdc.lock` warnings — they're the diagnostic signal until R4 lands.
- Don't disable nvenc as a workaround.

## Open questions / known unknowns

1. **Lock interaction on R3.** Need to read current `compress_image()` locking before finalizing the reconfigure thread-safety story. Plan resolves during implementation; design assumes the existing per-encoder lock pattern is sufficient.
2. **Phase 0 baseline duration.** 24h is the proposed minimum; may extend if storm rate is highly variable across workdays.
3. **R3 cost-benefit if R1 alone is sufficient.** If Phase 0 data shows R1 reduces reinit rate by >95%, R3 becomes a feature unlock rather than a storm fix. Worth shipping anyway for the runtime-bitrate capability, but the framing changes.
4. **R4 queue-vs-lock decision.** Queue is recommended for fairness, but a lock with a fair scheduler (e.g., `threading.Semaphore` with manual ordering) could work too. Default to the queue.

## Sponsored-By

Sponsored-By: Netflix
