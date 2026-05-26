# nvenc reinit-storm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce browser-window encoder reinit storms on the xpra HEVC 4:4:4 nvenc path. Storm signature today on `orbital`: ~10 `failed to acquire cuda device lock` warnings/min + ~5 `failed to encode h265 frame`/min + `context_count == 2` indicating encoder cycling.

**Architecture:** Phase 0 ships instrumentation alone on `feature/nvenc-reinit-instrument-v6.4.3` to establish a baseline. Then R1+R2+R3 land together on `feature/nvenc-reinit-storm-v6.4.3`: R1 gates `update_encoding_options()` on candidate-space change (preventing operating-point nudges from triggering re-scoring); R2 makes edge resistance always-on (not gated on subregion detection); R3 wires up nvenc bitrate-only `nvEncReconfigureEncoder` so quality/speed nudges can adapt the live encoder instead of forcing teardown.

**Tech Stack:** Python 3 + Cython (`encoder.pyx`), pycuda, NVENC SDK 13. Tests use `unittest` + `AdHocStruct` mocking pattern from existing `test_verify_scaling.py`. Cython encoder changes require GPU-equipped server (`orbital`, RTX A2000) for integration validation; build via `./tests/docker/build-deb.sh` (no `--deploy` — Andrew handles install/restart; the implementer doesn't have sudo).

**Spec:** `docs/superpowers/specs/2026-05-23-nvenc-reinit-storm-design.md`

**Execution environment:** Run in a worktree off `fork/feature/onevpl-hevc-444-v6.4.3` so the implementation stacks on the VPL HEVC 4:4:4 work (which is what `orbital` actually runs day-to-day). Use the `superpowers:using-git-worktrees` skill to set this up before starting Task 1. The spec and plan live on `v6.4.3-achen` (committed there); inside the worktree you can read them via `git show v6.4.3-achen:docs/superpowers/plans/2026-05-23-nvenc-reinit-storm.md` (or, since worktrees share `.git`, by reading directly from the main checkout at `/home/achen/empty/xpra-src/docs/...`).

**Branch structure (inside the worktree):**
- Phase 0 tasks 1-6 → `feature/nvenc-reinit-instrument-v6.4.3` (off `fork/feature/onevpl-hevc-444-v6.4.3`). Ship + collect baseline before continuing.
- Tasks 7+ → `feature/nvenc-reinit-storm-v6.4.3` (off the Phase 0 branch after baseline collection).
- Master ports come last, after v6.4.3 validates.

---

## Phase 0: Quantify-only instrumentation

Aggregate `reinit_count` per window + cdc.lock warning enrichment. ~15 LOC. Function signature of `video_context_clean()` does NOT change — increment lives inside the existing `if csce or ve:` block so no-op cleanups don't inflate the count.

### Task 1: Create Phase 0 branch (inside the VPL-based worktree)

**Files:**
- None (git branch operation, must run inside the worktree)

**Precondition:** Worktree exists off `fork/feature/onevpl-hevc-444-v6.4.3`. Set up via `superpowers:using-git-worktrees` before starting; the rest of this task assumes you're `cd`'d into that worktree.

- [ ] **Step 1: Confirm worktree HEAD is on the VPL branch**

```bash
git rev-parse --abbrev-ref HEAD
git log --oneline HEAD -3
```

Expected: HEAD is `feature/onevpl-hevc-444-v6.4.3` (or a tracking branch off `fork/feature/onevpl-hevc-444-v6.4.3`). Recent commits start with the VPL stack (e.g., `bc27d19af8 vpl: replace decoder pool with module-level cache`).

- [ ] **Step 2: Create Phase 0 branch off the VPL tip**

```bash
git checkout -b feature/nvenc-reinit-instrument-v6.4.3
git log --oneline -1
```

Expected: branch created at the same SHA as the VPL branch tip.

- [ ] **Step 3: Confirm clean working tree**

```bash
git status --short | grep -v '^??' | wc -l
```

Expected: `0` (only untracked files allowed; no modified or staged tracked files).

- [ ] **Step 4: Copy spec + plan into the worktree**

Simpler than cherry-picking — just `cp` from the main checkout. The files don't need git history in this branch; they're docs the implementer references during the work.

```bash
mkdir -p docs/superpowers/specs docs/superpowers/plans
cp /home/achen/empty/xpra-src/docs/superpowers/specs/2026-05-23-nvenc-reinit-storm-design.md docs/superpowers/specs/
cp /home/achen/empty/xpra-src/docs/superpowers/plans/2026-05-23-nvenc-reinit-storm.md docs/superpowers/plans/
git add docs/superpowers/specs/2026-05-23-nvenc-reinit-storm-design.md docs/superpowers/plans/2026-05-23-nvenc-reinit-storm.md
git commit -m "$(cat <<'EOF'
docs: import nvenc reinit-storm spec and plan into worktree

Plain copy of the design spec and implementation plan from
v6.4.3-achen so the implementer has them in tree without needing
cross-tree access or cherry-picks of the iteration history.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
)" --author="Andrew Chen <achen.code@gmail.com>"
```

- [ ] **Step 5: Confirm spec+plan are present**

```bash
ls docs/superpowers/specs/ docs/superpowers/plans/
```

Expected: both files visible.

### Task 2: Add aggregate `reinit_count` field + increment in `video_context_clean()`

**Files:**
- Modify: `xpra/server/window/video_compress.py` (add field init in `__init__`, increment in `video_context_clean`, expose in `get_info`)

- [ ] **Step 1: Locate __init__ in WindowVideoSource**

```bash
grep -n "^    def __init__\b" xpra/server/window/video_compress.py | head -3
```

Expected: a line number for the constructor. The field init goes inside `__init__`.

- [ ] **Step 2: Add `self.reinit_count = 0` to `__init__`**

Find the existing line in `__init__` that initializes other counters or pipeline state (e.g., `self.last_pipeline_scores = ...`). Add immediately after it:

```python
        self.reinit_count: int = 0
```

- [ ] **Step 3: Increment inside the `if csce or ve:` block in `video_context_clean`**

Read `xpra/server/window/video_compress.py:404-419` first to see current state. Modify:

```python
    def video_context_clean(self) -> None:
        """ Calls clean() from the encode thread """
        csce = self._csc_encoder
        ve = self._video_encoder
        if csce or ve:
            self.reinit_count += 1
            if DEBUG_VIDEO_CLEAN:
                log.warn("video_context_clean() for wid %i: %s and %s", self.wid, csce, ve, backtrace=True)
            self._csc_encoder = None
            self._video_encoder = None

            def clean() -> None:
                if DEBUG_VIDEO_CLEAN:
                    log.warn("video_context_clean() done")
                self.csc_clean(csce)
                self.ve_clean(ve)
            self.call_in_encode_thread(False, clean)
```

The single-line addition is `self.reinit_count += 1` immediately inside the `if csce or ve:` block, before the `DEBUG_VIDEO_CLEAN` check.

- [ ] **Step 4: Expose `reinit_count` in `get_info()`**

Locate `get_info` (line ~314). Find the dict it returns. Add a key:

```python
            "reinit_count": self.reinit_count,
```

Place it alongside other simple int fields (e.g., near where `self.statistics` or pipeline counters are dumped).

- [ ] **Step 5: Verify the file still parses**

```bash
/usr/bin/python3 -c "import ast; ast.parse(open('xpra/server/window/video_compress.py').read())"
echo "EXIT=$?"
```

Expected: `EXIT=0`.

### Task 3: Unit test the reinit counter

**Files:**
- Create: `tests/unittests/unit/server/test_reinit_count.py`

- [ ] **Step 1: Write the failing test**

```python
#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests that WindowVideoSource.video_context_clean() bumps reinit_count
# ABOUTME: only when there is real teardown work (csce or ve is not None).

import unittest
from unittest.mock import MagicMock, patch


class ReinitCountTest(unittest.TestCase):

    def _make_wvs(self):
        """Construct a minimal WindowVideoSource for counter-only testing."""
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs.reinit_count = 0
        wvs._csc_encoder = None
        wvs._video_encoder = None
        wvs.wid = 1
        wvs.call_in_encode_thread = MagicMock()
        return wvs

    def test_noop_clean_does_not_bump(self):
        wvs = self._make_wvs()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 0,
                         "no-op cleanup (both encoders None) must not bump reinit_count")

    def test_clean_with_csce_bumps(self):
        wvs = self._make_wvs()
        wvs._csc_encoder = MagicMock()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 1)
        wvs.call_in_encode_thread.assert_called_once()

    def test_clean_with_ve_bumps(self):
        wvs = self._make_wvs()
        wvs._video_encoder = MagicMock()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 1)
        wvs.call_in_encode_thread.assert_called_once()

    def test_clean_with_both_bumps_once(self):
        wvs = self._make_wvs()
        wvs._csc_encoder = MagicMock()
        wvs._video_encoder = MagicMock()
        wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 1, "single cleanup is one event, not two")

    def test_repeated_noop_calls_do_not_bump(self):
        wvs = self._make_wvs()
        for _ in range(5):
            wvs.video_context_clean()
        self.assertEqual(wvs.reinit_count, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 -m unittest tests.unittests.unit.server.test_reinit_count -v
```

Expected: 5 tests pass.

If a test fails because of an unrelated WindowVideoSource attribute access during `video_context_clean` (e.g., the existing implementation references something other than `_csc_encoder`/`_video_encoder` in a code path you didn't notice), read the code at `video_context_clean` carefully and add the minimal attribute on `wvs` in `_make_wvs()` to satisfy it. Do NOT change `video_context_clean` to make the test pass — the test is the spec; if the function does more than the spec acknowledges, that's a code-level finding to address separately.

- [ ] **Step 3: Commit**

```bash
git add xpra/server/window/video_compress.py tests/unittests/unit/server/test_reinit_count.py
git commit -F - <<'EOF'
nvenc: add aggregate reinit_count for Phase 0 baseline

Increment WindowVideoSource.reinit_count inside the existing
`if csce or ve:` block in video_context_clean() so no-op cleanup
calls (both encoders already None) don't inflate the baseline.
Expose via get_info() for `xpra info :100 | grep reinit_count`.

Part of the nvenc reinit-storm design spec (Phase 0). See
docs/superpowers/specs/2026-05-23-nvenc-reinit-storm-design.md.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
git log --oneline -1
```

### Task 4: Enrich `cdc.lock` warning with current-holder info

**Files:**
- Modify: `xpra/codecs/nvidia/cuda/context.py` (`cuda_device_context` class)

The goal: when `__enter__` fails the non-blocking acquire, log who currently holds the lock and what phase they're in. This is the "blast-radius" diagnostic from the spec.

- [ ] **Step 1: Read the current `__enter__`**

Read `xpra/codecs/nvidia/cuda/context.py:525-575` to see the class body. We need to track the current holder's identity and phase.

- [ ] **Step 2: Add holder tracking fields**

In `cuda_device_context.__init__` (line 526), add fields after `self.lock = RLock()`:

```python
        self._holder_phase: str = ""   # "init" / "cleanup" / "compress" / ""
        self._holder_id: str = ""      # short identifier of the encoder holding the lock
```

- [ ] **Step 3: Add a setter method**

After `__init__` (before `__bool__`), add:

```python
    def set_holder(self, holder_id: str, phase: str) -> None:
        """Called by acquirers right after they win the lock. Cleared on release."""
        self._holder_id = holder_id
        self._holder_phase = phase
```

- [ ] **Step 4: Enrich the warning in `__enter__`**

Modify `__enter__` (line 538) from:

```python
    def __enter__(self):
        if not self.lock.acquire(False):
            raise TransientCodecException("failed to acquire cuda device lock")
        if not self.context:
            self.make_context()
        return self.push_context()
```

to:

```python
    def __enter__(self):
        if not self.lock.acquire(False):
            raise TransientCodecException(
                "failed to acquire cuda device lock "
                f"(held by {self._holder_id or '?'} during {self._holder_phase or '?'})"
            )
        if not self.context:
            self.make_context()
        return self.push_context()
```

- [ ] **Step 5: Clear holder on `__exit__`**

Modify `__exit__` to clear the holder after release:

```python
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.pop_context()
        self._holder_id = ""
        self._holder_phase = ""
        self.lock.release()
```

- [ ] **Step 6: Verify the file still parses**

```bash
/usr/bin/python3 -c "import ast; ast.parse(open('xpra/codecs/nvidia/cuda/context.py').read())"
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 7: Commit**

```bash
git add xpra/codecs/nvidia/cuda/context.py
git commit -F - <<'EOF'
nvenc: enrich cdc.lock warning with current-holder + phase

Phase 0 diagnostic. When cuda_device_context.__enter__ fails the
non-blocking acquire (storm signature: "failed to acquire cuda device
lock"), include who currently holds the lock and which phase they're
in (init/cleanup/compress). Holders set themselves via set_holder()
on acquisition and the field is cleared in __exit__.

Confirms the "init-vs-encode on shared device" diagnosis from the
spec and quantifies blast-radius window length.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 5: Wire holder tracking into the three cdc.lock acquirers

**Files:**
- Modify: `xpra/codecs/nvidia/nvenc/encoder.pyx` (`compress_image`, `do_clean`, `init_device`)

For now, we wire only nvenc's encoder (the dominant case). nvjpeg, nvdec etc. can be wired similarly later if needed.

- [ ] **Step 1: Add a helper for holder ID**

In `Encoder` class (in `encoder.pyx`), add a method to produce a short holder ID:

```python
    def _cdc_holder_id(self) -> str:
        # short, log-safe identifier including wid if available
        return f"nvenc-wid={getattr(self, 'wid', '?')}-codec={self.codec_name}"
```

(Insert near other small property/getter methods such as `get_type`/`get_encoding`.)

- [ ] **Step 2: Wire compress_image holder**

Find `compress_image` (line 1364). Modify the `with cuda_device_context as cuda_context:` block to set holder immediately after entering:

```python
        # cuda_device_context.__enter__ does self.context.push()
        with cuda_device_context as cuda_context:
            cuda_device_context.set_holder(self._cdc_holder_id(), "compress")
            quality = options.get("quality", -1)
            ...
```

- [ ] **Step 3: Wire init_device holder**

Find `init_device` (line 592). Modify the `with self.cuda_device_context as cuda_context:` block:

```python
        with self.cuda_device_context as cuda_context:
            self.cuda_device_context.set_holder(self._cdc_holder_id(), "init")
            self.init_cuda(cuda_context)
            self.init_cuda_kernel(cuda_context)
```

- [ ] **Step 4: Wire do_clean holder**

Find `do_clean` (use `grep -n "cdef void do_clean\|def do_clean" xpra/codecs/nvidia/nvenc/encoder.pyx` to locate). The cdc.lock is acquired blocking inside (per PR #4875, `cdc.lock.acquire()` at line 1172). Immediately after the acquire, set holder:

```python
            cdc.lock.acquire()
            cdc.set_holder(self._cdc_holder_id(), "cleanup")
            try:
                ...
            finally:
                cdc._holder_id = ""
                cdc._holder_phase = ""
                cdc.lock.release()
```

(The existing `finally: cdc.lock.release()` already exists at line 1183; add the two clearing lines before it.)

- [ ] **Step 5: Compile the Cython module to verify syntax**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 setup.py build_ext --inplace 2>&1 | tail -20
echo "EXIT=$?"
```

Expected: `EXIT=0`. If Cython compile fails, read the error, fix, repeat.

- [ ] **Step 6: Commit**

```bash
git add xpra/codecs/nvidia/nvenc/encoder.pyx
git commit -F - <<'EOF'
nvenc: track cdc.lock holder ID and phase for warning enrichment

Wire compress_image, init_device, and do_clean to call
cuda_device_context.set_holder() right after acquiring cdc.lock,
with phase "compress"/"init"/"cleanup" respectively. When some
other caller hits the non-blocking acquire fail path in __enter__,
the warning now reads e.g. "failed to acquire cuda device lock
(held by nvenc-wid=42-codec=hevc during init)".

Part of Phase 0 of nvenc reinit-storm design spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 6: Build .deb (Andrew handles deployment + baseline collection)

**Files:**
- None (build step)

The implementer (Claude) does NOT have sudo and cannot install the .deb. After build succeeds, Andrew handles install/restart/baseline collection separately. The implementer should continue directly into R1/R2/R3 tasks after the build succeeds — no waiting for live measurements before continuing.

- [ ] **Step 1: Build the .deb with Phase 0 changes (no deploy)**

```bash
./tests/docker/build-deb.sh 2>&1 | tail -20
echo "EXIT=$?"
```

Expected: `EXIT=0`. Build artifacts in `build-deb-out/`. Do NOT pass `--deploy` — the implementer doesn't have sudo to install.

- [ ] **Step 2: Confirm artifacts**

```bash
ls -la build-deb-out/*.deb 2>&1
```

Expected: at least one `.deb` file. Note the filename(s) — Andrew will use them for install.

- [ ] **Step 3: Push Phase 0 branch**

```bash
git push fork feature/nvenc-reinit-instrument-v6.4.3:feature/nvenc-reinit-instrument-v6.4.3
```

Expected: branch pushed.

**Andrew handles separately (out of scope for the implementer):**
- Install the .deb (`sudo dpkg -i build-deb-out/*.deb` or equivalent).
- Restart `xpra.service`.
- Manual smoke tests: confirm `xpra info :100 | grep reinit_count` appears; confirm enriched cdc.lock warnings under Edge tab-restore workload.
- Optional 24h baseline collection if before/after attribution matters.

The implementer proceeds to Task 7 immediately after the build artifacts exist.

---

## R1: Candidate-space-only re-scoring

The dominant storm engine is `update_encoding_options()` being called from `WindowSource.reconfigure()` on every auto-tuner tick. R1 gates the body of `update_encoding_options` on a candidate-space tuple comparison; quality/speed nudges that don't change the tuple skip the scoring step and push the new operating point directly to the active encoder.

### Task 7: Create R1+R2+R3 branch (inside the same worktree)

**Files:**
- None (git branch operation)

- [ ] **Step 1: Branch off the Phase 0 deployed tip**

```bash
git checkout feature/nvenc-reinit-instrument-v6.4.3
git checkout -b feature/nvenc-reinit-storm-v6.4.3
git log --oneline -3
```

Expected: new branch off the Phase 0 tip (Task 6 commit, which is the deploy+baseline marker).

### Task 8: Add `_compute_candidate_space()` helper

**Files:**
- Modify: `xpra/server/window/video_compress.py` (add helper method, init new fields in `__init__`)

The candidate space tuple per the spec:

```python
_candidate_space = (
    self.encoding,
    self.content_type,
    self.common_video_encodings,
    self.pixel_format,
    self.window_dimensions,
    self.video_subregion.rectangle,
    self.full_csc_modes,
    self._candidate_pixel_format_fingerprint,
    self._desired_scaling,
)
```

For Phase 1 implementation, `_candidate_pixel_format_fingerprint` is a frozenset of `(codec_name, target_pixel_format)` pairs per candidate, with Y2 deadband applied (Y2 lands in R2 task; for now use the raw `YUV444_THRESHOLD`-based logic and update once Y2 deadband lands).

- [ ] **Step 1: Initialize new fields in `__init__`**

Add near `self.reinit_count = 0` (added in Task 2):

```python
        self._last_candidate_space: tuple | None = None
        self._consecutive_encode_failures: int = 0
```

- [ ] **Step 2: Add helper for the pixel-format fingerprint**

Add as a new method on `WindowVideoSource`. The thresholds are read via `envint` directly (not imported from `encoder.pyx`, because module-level `cdef int` constants in Cython aren't Python-importable). The candidate's YUV444P-capability is checked via `YUV444_CODEC_SUPPORT` — a Python dict that IS importable:

```python
    def _compute_candidate_pixel_format_fingerprint(self) -> frozenset:
        """Per-candidate target pixel format map, hashed.

        For each candidate in common_video_encodings, compute whether it
        would target YUV444P at the current quality, with Y2 deadband
        applied based on the CURRENT encoder's pixel format. Returns a
        frozenset of (encoding, target_pixel_format) pairs.
        """
        from xpra.util.env import envint
        try:
            from xpra.codecs.nvidia.nvenc.encoder import YUV444_CODEC_SUPPORT
        except ImportError:
            YUV444_CODEC_SUPPORT = {}
        # Thresholds duplicated here (not imported) because the Cython-side
        # values are cdef int constants not exposed to Python. Keep defaults
        # in sync with encoder.pyx.
        yuv444_threshold = envint("XPRA_NVENC_YUV444_THRESHOLD", 85)
        yuv444_deadband = envint("XPRA_NVENC_YUV444_DEADBAND", 5)
        try:
            current_pf = self._video_encoder.get_src_format() if self._video_encoder else None
        except AttributeError:
            current_pf = None
        currently_yuv444 = (current_pf == "YUV444P")
        if currently_yuv444:
            yuv444_active = self._current_quality >= (yuv444_threshold - yuv444_deadband)
        else:
            yuv444_active = self._current_quality >= yuv444_threshold
        entries = []
        for encoding in self.common_video_encodings:
            # YUV444_CODEC_SUPPORT keys are the actual nvenc encoding names
            # (h264, h265 — NOT 'hevc'). av1 has False by default. Anything
            # outside the dict is non-nvenc and uses its own input pixel format.
            if YUV444_CODEC_SUPPORT.get(encoding, False):
                target_pf = "YUV444P" if yuv444_active else "NV12"
            elif encoding in YUV444_CODEC_SUPPORT:
                # Known to nvenc but YUV444-incapable → always NV12 for these
                target_pf = "NV12"
            else:
                target_pf = self.pixel_format
            entries.append((encoding, target_pf))
        return frozenset(entries)
```

- [ ] **Step 3: Add helper for desired scaling**

Add as a new method on `WindowVideoSource`:

```python
    def _compute_desired_scaling(self) -> tuple:
        """Fresh output of calculate_scaling for current state.
        NOT self.actual_scaling, which describes the installed pipeline (lags reality)."""
        ww, wh = self.window_dimensions
        w = ww & self.width_mask
        h = wh & self.height_mask
        vs = self.video_subregion
        if vs and vs.rectangle:
            r = vs.rectangle
            w = r.width & self.width_mask
            h = r.height & self.height_mask   # NOT width_mask; existing code at
                                              # video_compress.py:1396 has the same typo
                                              # but the right mask for height is height_mask
        return self.calculate_scaling(w, h, self.max_w, self.max_h)
```

- [ ] **Step 4: Add the candidate-space helper**

```python
    def _compute_candidate_space(self) -> tuple:
        """Tuple R1 compares against the cached last_candidate_space to decide
        whether scoring needs to re-run."""
        return (
            self.encoding,
            self.content_type,
            tuple(self.common_video_encodings),
            self.pixel_format,
            self.window_dimensions,
            self.video_subregion.rectangle if self.video_subregion else None,
            tuple(sorted(self.full_csc_modes.items())) if self.full_csc_modes else (),
            self._compute_candidate_pixel_format_fingerprint(),
            self._compute_desired_scaling(),
        )
```

- [ ] **Step 5: Verify file parses**

```bash
/usr/bin/python3 -c "import ast; ast.parse(open('xpra/server/window/video_compress.py').read())"
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 6: Commit**

```bash
git add xpra/server/window/video_compress.py
git commit -F - <<'EOF'
nvenc: add candidate-space tuple helpers for R1

Adds three helpers on WindowVideoSource:
- _compute_candidate_pixel_format_fingerprint: per-candidate target
  pixel format map, frozenset-hashed. Catches encoding=auto transitions
  where a different candidate would win (e.g., AV1 vs h265 at YUV444P
  threshold). Y2 deadband is applied per-current-encoder's pixel format
  to inherit hysteresis (YUV444_DEADBAND is wired in the R2 task).
- _compute_desired_scaling: fresh calculate_scaling output, not the
  installed pipeline's stale actual_scaling.
- _compute_candidate_space: assembles the tuple R1 will compare.

No behavior change yet — R1's gate in update_encoding_options is wired
in the next task.

Part of R1 from the nvenc reinit-storm design spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 9: Unit test the candidate-space helpers

**Files:**
- Create: `tests/unittests/unit/server/test_candidate_space.py`

- [ ] **Step 1: Write failing tests**

```python
#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests R1's candidate-space tuple helpers detect transitions correctly.
# ABOUTME: Covers per-candidate pixel-format fingerprint + desired-scaling staleness.

import unittest
from unittest.mock import MagicMock


class CandidateSpaceTest(unittest.TestCase):

    def _make_wvs(self, quality=50, encoding="auto",
                  common_video_encodings=("h264", "hevc", "av1"),
                  pixel_format="BGRX",
                  window_dimensions=(1920, 1080)):
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs._current_quality = quality
        wvs.encoding = encoding
        wvs.common_video_encodings = common_video_encodings
        wvs.pixel_format = pixel_format
        wvs.window_dimensions = window_dimensions
        wvs.width_mask = 0xFFFE
        wvs.height_mask = 0xFFFE
        wvs.max_w = 4096
        wvs.max_h = 4096
        wvs.content_type = "browser"
        wvs.full_csc_modes = {}
        wvs.video_subregion = MagicMock(rectangle=None)
        wvs._video_encoder = None
        wvs.calculate_scaling = lambda w, h, mw, mh: (1, 1)
        return wvs

    def test_fingerprint_stable_for_low_quality(self):
        wvs = self._make_wvs(quality=50)
        fp1 = wvs._compute_candidate_pixel_format_fingerprint()
        wvs._current_quality = 60
        fp2 = wvs._compute_candidate_pixel_format_fingerprint()
        self.assertEqual(fp1, fp2,
                         "quality changes within the same band must not change fingerprint")

    def test_fingerprint_changes_crossing_yuv444_threshold(self):
        wvs = self._make_wvs(quality=80)
        fp_low = wvs._compute_candidate_pixel_format_fingerprint()
        wvs._current_quality = 90
        fp_high = wvs._compute_candidate_pixel_format_fingerprint()
        self.assertNotEqual(fp_low, fp_high,
                            "crossing YUV444_THRESHOLD must change fingerprint")

    def test_candidate_space_changes_with_content_type(self):
        wvs = self._make_wvs()
        space1 = wvs._compute_candidate_space()
        wvs.content_type = "video"
        space2 = wvs._compute_candidate_space()
        self.assertNotEqual(space1, space2)

    def test_candidate_space_changes_with_dims(self):
        wvs = self._make_wvs(window_dimensions=(1920, 1080))
        space1 = wvs._compute_candidate_space()
        wvs.window_dimensions = (1280, 720)
        space2 = wvs._compute_candidate_space()
        self.assertNotEqual(space1, space2)

    def test_candidate_space_stable_with_quality_nudge(self):
        wvs = self._make_wvs(quality=50)
        space1 = wvs._compute_candidate_space()
        wvs._current_quality = 55
        space2 = wvs._compute_candidate_space()
        self.assertEqual(space1, space2,
                         "small quality nudges within band must not change candidate space")

    def test_desired_scaling_is_fresh(self):
        wvs = self._make_wvs()
        wvs.calculate_scaling = lambda w, h, mw, mh: (1, 2)
        scaling = wvs._compute_desired_scaling()
        self.assertEqual(scaling, (1, 2),
                         "desired scaling must use the fresh calculate_scaling result")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 -m unittest tests.unittests.unit.server.test_candidate_space -v
```

Expected: 6 tests pass. If `YUV444_DEADBAND` import fails because R2 hasn't run yet, add a temporary shim near the top of `xpra/codecs/nvidia/nvenc/encoder.pyx` — `YUV444_DEADBAND = envint("XPRA_NVENC_YUV444_DEADBAND", 5)` (R2 task will move/keep this as part of its own changes). Re-run tests.

- [ ] **Step 3: Commit**

```bash
git add tests/unittests/unit/server/test_candidate_space.py xpra/codecs/nvidia/nvenc/encoder.pyx
git commit -F - <<'EOF'
nvenc: unit tests for R1 candidate-space helpers

Cover the four key transition cases:
- Quality nudge within band → space unchanged (no re-score)
- Quality nudge crossing YUV444_THRESHOLD → fingerprint changes → re-score
- Content_type change → space changes → re-score
- Dimension change → space changes → re-score

Adds YUV444_DEADBAND envvar (default 5) since the fingerprint logic
references it; full Y2 hysteresis behavior lands in the R2 task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 10: Add `_push_operating_point()` helper

**Files:**
- Modify: `xpra/server/window/video_compress.py`

- [ ] **Step 1: Add the helper**

Add as a new method on `WindowVideoSource`:

```python
    def _push_operating_point(self) -> None:
        """Push current quality/speed to the active video encoder without
        triggering teardown or re-scoring. R1 calls this when the candidate
        space is unchanged."""
        ve = self._video_encoder
        if ve is None:
            return
        try:
            ve.set_encoding_quality(self._current_quality)
        except AttributeError:
            pass
        try:
            ve.set_encoding_speed(self._current_speed)
        except AttributeError:
            pass
```

The `AttributeError` swallows are deliberate — non-nvenc encoders may not implement these methods. Pre-R3, nvenc's `set_encoding_quality` is a no-op for runtime bitrate (stashes the field, doesn't push to NVENC); R3 makes it functional.

- [ ] **Step 2: Verify file parses**

```bash
/usr/bin/python3 -c "import ast; ast.parse(open('xpra/server/window/video_compress.py').read())"
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 3: Commit**

```bash
git add xpra/server/window/video_compress.py
git commit -F - <<'EOF'
nvenc: add _push_operating_point() helper for R1

Pushes current quality/speed to the active video encoder without
triggering teardown. R1 calls this on the "candidate space unchanged"
path in update_encoding_options.

Pre-R3, the nvenc encoder's set_encoding_quality/speed are no-ops for
runtime bitrate. R3 wires them to nvEncReconfigureEncoder so this push
actually adapts the live encoder.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 11: Gate `update_encoding_options()` on candidate-space comparison

**Files:**
- Modify: `xpra/server/window/video_compress.py:1327-1346` (`update_encoding_options`)

- [ ] **Step 1: Re-read the current implementation**

```bash
sed -n '1327,1350p' xpra/server/window/video_compress.py
```

Expected output: the current ~20-line `update_encoding_options` body matching the spec snapshot.

(You may NOT use `sed` for editing; the above is just for reading. Use the Read tool's offset/limit for the actual file fetch.)

- [ ] **Step 2: Replace with the gated version**

Important: `update_pipeline_scores()` has an internal throttle (~0.75s on unchanged `last_pipeline_params`). The cache MUST NOT advance until we know scoring actually ran with the new params — otherwise a throttled no-op call would falsely mark the candidate space as handled and we'd skip the re-score on subsequent ticks. To detect actual refresh, snapshot the score-set identity before calling and compare after; only advance the cache if it changed (or if force_reload was passed, which bypasses the throttle):

```python
    def update_encoding_options(self, force_reload=False) -> None:
        """
            This is called when we want to force a full re-init (force_reload=True)
            or from the timer that allows to tune the quality and speed.
            (this tuning is done in `WindowSource.reconfigure`)
            Here we re-evaluate if the csc and video pipeline we are currently using
            is really the best one, and if not we invalidate it.
            This uses get_video_pipeline_options() to get a list of pipeline
            options with a score for each.

            Can be called from any thread.
        """
        super().update_encoding_options(force_reload)
        self.update_encoding_video_subregion()
        new_space = self._compute_candidate_space()
        space_changed = (new_space != self._last_candidate_space)
        if force_reload:
            self.video_context_clean()
        if space_changed or force_reload:
            # Snapshot the score-set identity to detect whether
            # update_pipeline_scores actually refreshed (it may throttle
            # internally — see last_pipeline_params throttle in that method).
            prev_scores_id = id(self.last_pipeline_scores)
            self.update_pipeline_scores(force_reload)
            scores_refreshed = (id(self.last_pipeline_scores) != prev_scores_id) or force_reload
            if scores_refreshed:
                # Only advance the cache when fresh scores actually landed,
                # so a throttle-induced no-op doesn't mark the space "handled".
                self._last_candidate_space = new_space
                if not self.verify_csc_and_encoder() and not force_reload:
                    self.video_context_clean()
            # else: scoring throttled, leave cache stale — next tick retries
        else:
            # candidate space unchanged — push operating-point only
            self._push_operating_point()
        self._last_pipeline_check = monotonic()
```

If `id(self.last_pipeline_scores)` isn't a reliable refresh indicator (e.g., update_pipeline_scores assigns to a different attribute), substitute with whichever attribute the throttle actually rotates. Confirm by reading update_pipeline_scores at implementation time.

- [ ] **Step 3: Verify file parses**

```bash
/usr/bin/python3 -c "import ast; ast.parse(open('xpra/server/window/video_compress.py').read())"
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 4: Unit-test the gate**

Append to `tests/unittests/unit/server/test_candidate_space.py`:

```python


class UpdateEncodingOptionsGateTest(unittest.TestCase):

    def _make_wvs(self):
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs.reinit_count = 0
        wvs._csc_encoder = None
        wvs._video_encoder = None
        wvs.wid = 1
        wvs._current_quality = 50
        wvs._current_speed = 50
        wvs.encoding = "auto"
        wvs.common_video_encodings = ("h264", "hevc")
        wvs.pixel_format = "BGRX"
        wvs.window_dimensions = (1920, 1080)
        wvs.width_mask = 0xFFFE
        wvs.height_mask = 0xFFFE
        wvs.max_w = 4096
        wvs.max_h = 4096
        wvs.content_type = "browser"
        wvs.full_csc_modes = {}
        wvs.video_subregion = MagicMock(rectangle=None)
        wvs.calculate_scaling = lambda w, h, mw, mh: (1, 1)
        wvs.update_encoding_video_subregion = MagicMock()
        wvs.update_pipeline_scores = MagicMock()
        wvs.verify_csc_and_encoder = MagicMock(return_value=True)
        wvs._last_candidate_space = None
        wvs._last_pipeline_check = 0
        wvs.call_in_encode_thread = MagicMock()
        # super().update_encoding_options is a no-op stub
        return wvs

    def test_first_call_runs_scoring(self):
        wvs = self._make_wvs()
        with patch.object(type(wvs).__mro__[1], "update_encoding_options", lambda self, fr: None):
            wvs.update_encoding_options(force_reload=False)
        wvs.update_pipeline_scores.assert_called_once_with(False)

    def test_unchanged_space_skips_scoring(self):
        wvs = self._make_wvs()
        with patch.object(type(wvs).__mro__[1], "update_encoding_options", lambda self, fr: None):
            wvs.update_encoding_options(force_reload=False)
            wvs.update_pipeline_scores.reset_mock()
            # call again, nothing has changed
            wvs.update_encoding_options(force_reload=False)
        wvs.update_pipeline_scores.assert_not_called()

    def test_quality_nudge_in_band_skips_scoring(self):
        wvs = self._make_wvs()
        with patch.object(type(wvs).__mro__[1], "update_encoding_options", lambda self, fr: None):
            wvs.update_encoding_options(force_reload=False)
            wvs.update_pipeline_scores.reset_mock()
            wvs._current_quality = 55  # small nudge, no band change
            wvs.update_encoding_options(force_reload=False)
        wvs.update_pipeline_scores.assert_not_called()

    def test_content_type_change_triggers_scoring(self):
        wvs = self._make_wvs()
        with patch.object(type(wvs).__mro__[1], "update_encoding_options", lambda self, fr: None):
            wvs.update_encoding_options(force_reload=False)
            wvs.update_pipeline_scores.reset_mock()
            wvs.content_type = "video"
            wvs.update_encoding_options(force_reload=False)
        wvs.update_pipeline_scores.assert_called_once_with(False)
```

- [ ] **Step 5: Run all candidate-space tests**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 -m unittest tests.unittests.unit.server.test_candidate_space -v
```

Expected: 10 tests pass (6 from Task 9 + 4 new).

If `super().update_encoding_options` access fails, the `patch.object(type(wvs).__mro__[1], ...)` approach above bypasses MRO at runtime; if the mocking fails, replace with a direct stub method on the class via `WindowVideoSource.update_encoding_options.__wrapped__` or similar pytest-style patching. If unittest infrastructure makes this hard, swap to `pytest` and use `monkeypatch`.

- [ ] **Step 6: Commit**

```bash
git add xpra/server/window/video_compress.py tests/unittests/unit/server/test_candidate_space.py
git commit -F - <<'EOF'
nvenc: gate update_encoding_options on candidate-space change (R1)

Wraps the body of update_encoding_options in a candidate-space tuple
comparison. When the tuple matches the last cached space, scoring is
skipped and _push_operating_point() applies the new quality/speed to
the active encoder. This eliminates the dominant storm engine:
auto-tuner timer firing -> reconfigure -> re-score on every nudge.

Unit tests cover the four key cases:
- First call: scoring runs (no cache yet).
- Unchanged space: scoring skipped.
- Quality nudge in band: scoring skipped.
- Content_type change: scoring runs.

Primary lever from the nvenc reinit-storm design spec (R1).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 12: Reroute `quality_changed`/`speed_changed` overrides

**Files:**
- Modify: `xpra/server/window/video_compress.py:804-815` (the two overrides)

- [ ] **Step 1: Add `_maybe_invalidate_for_operating_point()` helper**

```python
    def _maybe_invalidate_for_operating_point(self) -> None:
        """Called by quality_changed/speed_changed (client-driven property
        notifies). Tear down only if the candidate space genuinely changed."""
        new_space = self._compute_candidate_space()
        if new_space != self._last_candidate_space:
            self.video_context_clean()
            self._last_candidate_space = new_space
        else:
            self._push_operating_point()
```

- [ ] **Step 2: Modify the overrides**

Replace the existing `quality_changed`/`speed_changed` in `video_compress.py` (lines 804-815 currently call `video_context_clean()` unconditionally):

```python
    def quality_changed(self, window, *args) -> bool:
        super().quality_changed(window, args)
        self._maybe_invalidate_for_operating_point()
        return True

    def speed_changed(self, window, *args) -> bool:
        super().speed_changed(window, args)
        self._maybe_invalidate_for_operating_point()
        return True
```

- [ ] **Step 3: Verify parse**

```bash
/usr/bin/python3 -c "import ast; ast.parse(open('xpra/server/window/video_compress.py').read())"
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 4: Commit**

```bash
git add xpra/server/window/video_compress.py
git commit -F - <<'EOF'
nvenc: reroute quality_changed/speed_changed through R1 gate

These GObject notify handlers fire only for client-driven property
changes (xpra control encoding=..., client window settings), NOT for
auto-tuner nudges. But they currently unconditionally tear down the
encoder via video_context_clean(). Reroute through
_maybe_invalidate_for_operating_point() so the candidate-space gate
applies: if the change is genuinely a candidate-space change, tear
down; otherwise just push the operating point to the active encoder.

Secondary fix to R1 (client-driven path). The dominant storm engine
is the auto-tuner path through update_encoding_options.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 13: Safety valve for stuck encoders

**Files:**
- Modify: `xpra/server/window/video_compress.py`

If R1 sticks on a poor-fit encoder forever, an explicit reseed is needed. We add a consecutive-encode-failure counter that forces re-scoring after N failures.

- [ ] **Step 1: Find the existing encode-failure handler**

```bash
grep -n "encoded(\|encode_fail\|frame_failed\|except\b" xpra/server/window/video_compress.py | head -20
```

Look for the `make_data_packet` and surrounding methods that detect encode failures. The relevant hook is wherever a compress attempt raises `TransientCodecException` or returns falsy data.

- [ ] **Step 2: Add the safety valve method**

```python
    R1_FORCE_RESELECT_AFTER = envint("XPRA_R1_FORCE_RESELECT_AFTER", 5)

    def _r1_note_encode_outcome(self, success: bool) -> None:
        """Called from the encode path. After N consecutive failures, force
        R1 to re-score on the next update_encoding_options."""
        if success:
            self._consecutive_encode_failures = 0
            return
        self._consecutive_encode_failures += 1
        if self._consecutive_encode_failures >= self.R1_FORCE_RESELECT_AFTER:
            log.warn("forcing encoder re-selection after %i consecutive failures",
                     self._consecutive_encode_failures)
            self.video_context_clean()
            self._last_candidate_space = None  # force re-score on next call
            self._consecutive_encode_failures = 0
```

(`envint` is already imported in this file — check imports near the top to confirm; if not, add `from xpra.util.env import envint`.)

- [ ] **Step 3: Hook the safety valve into the encode outcome paths**

Find places where the encode result is checked. The cleanest hook is wherever `make_data_packet` returns/catches. Pattern: on caught `TransientCodecException` from the encoder, call `self._r1_note_encode_outcome(False)`; on successful packet construction, call `self._r1_note_encode_outcome(True)`.

If the existing exception-handler is inside `do_video_encode` or `video_encode`, add the call there. If you can't find a clean hook, add a thin wrapper around `self._video_encoder.compress_image` calls that bridges to `_r1_note_encode_outcome`.

If the hook locations aren't obvious from the current code state, leave the method defined but unwired and document the gap. The safety valve is a backstop — if instrumentation shows R1 doesn't get stuck in practice, this is fine. Mark it as TODO with a follow-up reference. (NOTE: this is the one place in this plan where a TODO is acceptable, because the hook location is exploration-dependent.)

- [ ] **Step 4: Add a unit test for the safety valve logic**

Append to `tests/unittests/unit/server/test_candidate_space.py`:

```python


class SafetyValveTest(unittest.TestCase):

    def _make_wvs(self):
        from xpra.server.window.video_compress import WindowVideoSource
        wvs = WindowVideoSource.__new__(WindowVideoSource)
        wvs.reinit_count = 0
        wvs._csc_encoder = None
        wvs._video_encoder = None
        wvs.wid = 1
        wvs._consecutive_encode_failures = 0
        wvs._last_candidate_space = ("placeholder",)
        wvs.call_in_encode_thread = MagicMock()
        return wvs

    def test_success_resets_counter(self):
        wvs = self._make_wvs()
        wvs._consecutive_encode_failures = 3
        wvs._r1_note_encode_outcome(True)
        self.assertEqual(wvs._consecutive_encode_failures, 0)

    def test_failure_below_threshold_does_not_invalidate(self):
        wvs = self._make_wvs()
        for _ in range(wvs.R1_FORCE_RESELECT_AFTER - 1):
            wvs._r1_note_encode_outcome(False)
        self.assertEqual(wvs._consecutive_encode_failures,
                         wvs.R1_FORCE_RESELECT_AFTER - 1)
        self.assertEqual(wvs._last_candidate_space, ("placeholder",),
                         "candidate space cache must persist")

    def test_failure_at_threshold_invalidates(self):
        wvs = self._make_wvs()
        for _ in range(wvs.R1_FORCE_RESELECT_AFTER):
            wvs._r1_note_encode_outcome(False)
        self.assertEqual(wvs._consecutive_encode_failures, 0)
        self.assertIsNone(wvs._last_candidate_space,
                          "safety valve must invalidate cache")
```

- [ ] **Step 5: Run the safety valve tests**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 -m unittest tests.unittests.unit.server.test_candidate_space -v
```

Expected: 13 tests pass (10 from previous + 3 new).

- [ ] **Step 6: Commit**

```bash
git add xpra/server/window/video_compress.py tests/unittests/unit/server/test_candidate_space.py
git commit -F - <<'EOF'
nvenc: R1 safety valve for stuck encoders

After N consecutive encode failures (default 5, tunable via
XPRA_R1_FORCE_RESELECT_AFTER), invalidate the candidate-space cache
and force a re-score on the next update_encoding_options. Backstop
against R1 sticking on a poor-fit encoder forever (e.g., if some
input change isn't in the candidate-space tuple).

Hook into encode-outcome paths is exploration-dependent; method
defined but the caller may not yet be wired (TODO documented in
code if unwired). Phase 0 instrumentation will surface whether
the backstop fires in practice.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

---

## R2: Edge resistance always-on

R2 has two parts: R2a decouples `setup_cost_mult` from `detection` so the base setup-cost penalty applies regardless of subregion detection state. Y2 adds a deadband around `YUV444_THRESHOLD` in `get_target_pixel_format()` so quality oscillations 83↔87 don't flip pixel format.

### Task 14: R2a — decouple `setup_cost_mult` from `detection`

**Files:**
- Modify: `xpra/server/window/video_scoring.py:115`

- [ ] **Step 1: Read current formula**

```bash
sed -n '112,120p' xpra/server/window/video_scoring.py
```

Expected:
```
    # multiplier for setup_cost:
    # (lose points if we have less than N fps)
    setup_cost_mult = int(detection) * (1 + max(0, MIN_FPS_COST - ffps))
```

- [ ] **Step 2: Replace the formula**

```python
    # multiplier for setup_cost:
    # The base term ALWAYS applies — switching from a current encoder is
    # real cost regardless of whether video subregion detection is active.
    # The `detection` factor only adds extra weight to the fps-based bump,
    # preserving the prior intent for subregion-detection scenarios.
    # See the nvenc reinit-storm design spec (R2).
    setup_cost_mult = 1 + int(detection) * max(0, MIN_FPS_COST - ffps)
```

- [ ] **Step 3: Verify parse**

```bash
/usr/bin/python3 -c "import ast; ast.parse(open('xpra/server/window/video_scoring.py').read())"
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 4: Unit-test edge resistance behavior**

Create `tests/unittests/unit/server/test_edge_resistance.py`:

```python
#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests R2a — setup_cost_mult >= 1 regardless of detection state.

import unittest


class EdgeResistanceTest(unittest.TestCase):

    def _setup_cost_mult(self, detection: bool, ffps: int) -> int:
        # Inline copy of the formula in video_scoring.py:115 so we test the
        # math directly without dragging in the full scoring stack.
        from xpra.server.window.video_scoring import MIN_FPS_COST
        return 1 + int(detection) * max(0, MIN_FPS_COST - ffps)

    def test_no_detection_high_fps_is_one(self):
        self.assertEqual(self._setup_cost_mult(False, 30), 1)

    def test_no_detection_zero_fps_is_still_one(self):
        # The fps bonus only kicks in with detection=True
        self.assertEqual(self._setup_cost_mult(False, 0), 1)

    def test_detection_high_fps_is_one(self):
        self.assertEqual(self._setup_cost_mult(True, 30), 1)

    def test_detection_low_fps_amplifies(self):
        # MIN_FPS_COST defaults to 4; at fps=0, mult = 1 + 1 * (4 - 0) = 5
        from xpra.server.window.video_scoring import MIN_FPS_COST
        self.assertEqual(self._setup_cost_mult(True, 0), 1 + MIN_FPS_COST)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: Run the tests**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 -m unittest tests.unittests.unit.server.test_edge_resistance -v
```

Expected: 4 tests pass.

- [ ] **Step 6: Commit**

```bash
git add xpra/server/window/video_scoring.py tests/unittests/unit/server/test_edge_resistance.py
git commit -F - <<'EOF'
nvenc: edge resistance always-on (R2a)

Change setup_cost_mult formula so the base term is always 1 (not 0)
regardless of subregion detection state. Today the formula was

  setup_cost_mult = int(detection) * (1 + max(0, MIN_FPS_COST - ffps))

so for windows without video subregion detection (most browser windows),
setup_cost_mult was 0, meaning ee_score = 100 - setup_cost * 0 = 100
for every candidate including those that aren't the current encoder.
Edge resistance was silently disabled.

New formula:

  setup_cost_mult = 1 + int(detection) * max(0, MIN_FPS_COST - ffps)

The base 1 always applies. The fps-bump preserves the original
detection-gated behavior. Net effect: switching from a current
encoder always carries a setup-cost penalty.

Secondary lever from the nvenc reinit-storm design spec (R2).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 15: Y2 — deadband around `YUV444_THRESHOLD`

**Files:**
- Modify: `xpra/codecs/nvidia/nvenc/encoder.pyx` (`YUV444_DEADBAND` envvar + `get_target_pixel_format` body)

(If Task 9 already added the `YUV444_DEADBAND` envvar as a temporary shim, this task moves/integrates it into the proper location. Adjust accordingly.)

- [ ] **Step 1: Confirm or add the envvar declaration**

```bash
grep -n "YUV444_DEADBAND" xpra/codecs/nvidia/nvenc/encoder.pyx
```

If the envvar is already present (added in Task 9), confirm the default value is `5`. If not, add it near `YUV444_THRESHOLD` (line 105):

```python
cdef int YUV444_DEADBAND = envint("XPRA_NVENC_YUV444_DEADBAND", 5)
```

- [ ] **Step 2: Apply hysteresis in `get_target_pixel_format`**

Read the current function body around line 636 (`if (quality>=YUV444_THRESHOLD and not self.scaling) or not hasyuv420:`). Modify to use an asymmetric threshold based on the current `self.pixel_format`:

```python
            if hasyuv444:
                # Y2 hysteresis: stay in YUV444P until quality drops below
                # (YUV444_THRESHOLD - YUV444_DEADBAND); switch up at the raw
                # threshold. Prevents flapping around quality=85 from auto-tuner.
                if self.pixel_format == "YUV444P":
                    upcross = YUV444_THRESHOLD - YUV444_DEADBAND
                else:
                    upcross = YUV444_THRESHOLD
                if (quality >= upcross and not self.scaling) or not hasyuv420:
                    v = "YUV444P"
```

- [ ] **Step 3: Compile**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 setup.py build_ext --inplace 2>&1 | tail -10
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 4: Commit**

```bash
git add xpra/codecs/nvidia/nvenc/encoder.pyx
git commit -F - <<'EOF'
nvenc: YUV444_THRESHOLD deadband for Y2 hysteresis

Adds XPRA_NVENC_YUV444_DEADBAND (default 5). When the encoder is
currently in YUV444P, the threshold to drop back to NV12 is lowered
to (YUV444_THRESHOLD - YUV444_DEADBAND). Switching UP to YUV444P
still happens at the raw threshold. This absorbs auto-tuner quality
oscillations of ±5 around the boundary without flipping the pixel
format (which would force a teardown that can't be avoided via
nvEncReconfigureEncoder).

R1's candidate-space fingerprint inherits this hysteresis because
it reads the encoder's actual target_pixel_format-equivalent logic.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

---

## R3: Bitrate-only nvenc reconfigure

R3 has two prerequisites + the reconfigure path itself. The 2017 code (commit `189a05d01f`) only reconfigured for pixel-format changes (which nvenc can't do); bitrate-only changes have never been wired. We add a NEW path that's guarded to bitrate-only / same-preset / same-pixel-format / same-dims.

### Task 16: R3 Prerequisite 1 — wire bitrate into `tune_qp()`

**Files:**
- Modify: `xpra/codecs/nvidia/nvenc/encoder.pyx:870-908` (`tune_qp` body)

- [ ] **Step 1: Re-read current `tune_qp`**

Read `xpra/codecs/nvidia/nvenc/encoder.pyx:870-908`. Note that rate-control mode is already set to `NV_ENC_PARAMS_RC_VBR` (line 878) but `averageBitRate`/`maxBitRate` are commented out (lines 901-904). The function uses QP min/max clamping for quality control.

- [ ] **Step 2: Add bitrate fields without disrupting QP clamping**

In the `tune_qp` body, BEFORE the final `#log("qp: %i", qp)` line, add:

```python
        # R3 Prerequisite 1: populate bitrate targets so VBR rate control has
        # something to aim for. Without this, target_bitrate/max_bitrate are
        # computed by update_bitrate() but never reach nvenc. QP clamping above
        # provides quality bounds; these provide bitrate bounds.
        if self.target_bitrate > 0:
            rc.averageBitRate = self.target_bitrate
        if self.max_bitrate > 0:
            rc.maxBitRate = self.max_bitrate
```

- [ ] **Step 3: Compile**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 setup.py build_ext --inplace 2>&1 | tail -10
echo "EXIT=$?"
```

Expected: `EXIT=0`.

- [ ] **Step 4: Commit**

```bash
git add xpra/codecs/nvidia/nvenc/encoder.pyx
git commit -F - <<'EOF'
nvenc: wire averageBitRate/maxBitRate into tune_qp (R3 prereq 1)

update_bitrate() has been computing self.target_bitrate /
self.max_bitrate since 2014 but they were never plumbed into the
NV_ENC_RC_PARAMS struct that nvenc reads. The averageBitRate /
maxBitRate fields in tune_qp were commented out (lines 901-904)
since at least 2017.

Add unconditional population of those fields when the target/max
values are >0. QP min/max clamping above continues to provide
quality bounds; these provide bitrate bounds. Rate control mode
was already VBR (line 878).

Required precondition for R3 — nvEncReconfigureEncoder needs
non-zero bitrate fields in reInitEncodeParams.encodeConfig.rcParams
to actually change the encoder's bitrate target at runtime.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

### Task 17: R3 Prerequisite 2 — cache init params at init time (with deep-copied encodeConfig)

**Files:**
- Modify: `xpra/codecs/nvidia/nvenc/encoder.pyx` (cache snapshot fields + init logic + dealloc)

The reconfigure path must NOT call `init_params()` (which calls `get_preset()` and may return a different `presetGUID`). Instead, cache the init params at init time and overlay only bitrate fields during reconfigure.

**Critical memory-management note:** `init_params()` allocates `params.encodeConfig` on the heap. The existing call sites `free()` it in a `finally` block right after `nvEncInitializeEncoder`. So a naïve `memcpy(&_cached_init_params, &params, ...)` produces a struct whose `encodeConfig` pointer dangles immediately after init's finally. We must **deep-copy** `encodeConfig` into our own heap-allocated buffer and free it in `__dealloc__`.

- [ ] **Step 1: Add cache fields**

Find the `Encoder` class field declarations (search for `cdef class Encoder` and the field declarations below it). Add:

```python
        # R3: cached init params snapshot for reconfigure.
        # _cached_init_params holds the struct shallow-copy.
        # _cached_encode_config holds OUR OWN heap-allocated copy of the
        # encodeConfig — init_params() allocates the original which gets
        # freed by the init caller in its finally, so we must not retain
        # the original pointer.
        cdef NV_ENC_INITIALIZE_PARAMS _cached_init_params
        cdef NV_ENC_CONFIG *_cached_encode_config
        cdef int _cached_init_params_valid
        cdef GUID _cached_preset_guid
```

- [ ] **Step 2: Snapshot the init params at init time**

In `init_nvenc()` (line 760+), find where `init_params(self.codec, &params)` is called and the encoder is successfully constructed (before the `finally` that frees `params.encodeConfig`). Add immediately after `nvEncInitializeEncoder` returns success:

```python
        # R3: cache the init params for the reconfigure path. We do a SHALLOW
        # copy of the outer struct, then a DEEP copy of encodeConfig into our
        # own heap buffer. The original params.encodeConfig will be freed in
        # the caller's finally; our cached pointer is independent.
        memcpy(&self._cached_init_params, &params, sizeof(NV_ENC_INITIALIZE_PARAMS))
        if params.encodeConfig != NULL:
            self._cached_encode_config = <NV_ENC_CONFIG*> malloc(sizeof(NV_ENC_CONFIG))
            assert self._cached_encode_config != NULL, "OOM caching encodeConfig"
            memcpy(self._cached_encode_config, params.encodeConfig, sizeof(NV_ENC_CONFIG))
            # Point the cached struct at OUR copy, not the about-to-be-freed original.
            self._cached_init_params.encodeConfig = self._cached_encode_config
        else:
            self._cached_encode_config = NULL
            self._cached_init_params.encodeConfig = NULL
        self._cached_preset_guid = params.presetGUID
        self._cached_init_params_valid = 1
```

(`memcpy`, `malloc`, `free` — make sure imports are present: `from libc.string cimport memcpy` and `from libc.stdlib cimport malloc, free`.)

- [ ] **Step 3: Add helper to copy cached params into a fresh destination**

The destination passed to `nvEncReconfigureEncoder` must also have its own heap-allocated `encodeConfig` — the cleanup `finally` in `_apply_reconfigure` (Task 18) will `free()` that destination's encodeConfig. We MUST NOT let it free our cached buffer.

```python
    cdef void _copy_cached_init_params(self, NV_ENC_INITIALIZE_PARAMS *dest):
        """Copy the snapshot taken at init time into `dest`. Allocates a
        fresh encodeConfig buffer on dest — caller is responsible for free()-ing
        dest.encodeConfig after use (matches the init_params caller contract)."""
        assert self._cached_init_params_valid, "init params snapshot missing"
        memcpy(dest, &self._cached_init_params, sizeof(NV_ENC_INITIALIZE_PARAMS))
        # Allocate a FRESH encodeConfig for dest so caller's free() doesn't
        # touch our cached buffer. Copy contents from our cache.
        if self._cached_encode_config != NULL:
            dest.encodeConfig = <NV_ENC_CONFIG*> malloc(sizeof(NV_ENC_CONFIG))
            assert dest.encodeConfig != NULL, "OOM copying encodeConfig"
            memcpy(dest.encodeConfig, self._cached_encode_config, sizeof(NV_ENC_CONFIG))
        else:
            dest.encodeConfig = NULL
```

- [ ] **Step 4: Free the cached buffer in `__dealloc__`**

Locate `__dealloc__` (or `clean` / equivalent cleanup). Add:

```python
        if self._cached_encode_config != NULL:
            free(self._cached_encode_config)
            self._cached_encode_config = NULL
        self._cached_init_params_valid = 0
```

Place this in the same teardown context that already frees per-Encoder pycuda allocations (so the cleanup ordering matches the existing pattern).

- [ ] **Step 5: Compile**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 setup.py build_ext --inplace 2>&1 | tail -15
echo "EXIT=$?"
```

Expected: `EXIT=0`. If `memcpy`/`malloc`/`free` aren't imported, add the cimports at the top.

- [ ] **Step 6: Commit**

```bash
git add xpra/codecs/nvidia/nvenc/encoder.pyx
git commit -m "$(cat <<'EOF'
nvenc: cache init params for reconfigure path (R3 prereq 2)

R3's reconfigure path must NOT re-run init_params() because that
calls get_preset(), which depends on speed/quality and may select
a different presetGUID. nvEncReconfigureEncoder rejects preset
changes on an existing encoder.

Cache NV_ENC_INITIALIZE_PARAMS at successful init time on the
Encoder instance. Critically: init_params() allocates encodeConfig
on the heap, and the existing init caller frees it in finally
immediately after. So we deep-copy encodeConfig into our own
malloc'd buffer (_cached_encode_config) and free it in dealloc.

_copy_cached_init_params() lifts the snapshot into a fresh
destination, allocating ANOTHER heap encodeConfig for the destination
so the caller's free() doesn't touch our cached buffer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
)" --author="Andrew Chen <achen.code@gmail.com>"
```

### Task 18: R3 — `_apply_reconfigure` helper + `set_encoding_quality`

**Files:**
- Modify: `xpra/codecs/nvidia/nvenc/encoder.pyx` (`set_encoding_quality`, new `_apply_reconfigure`)

- [ ] **Step 1: Replace `set_encoding_quality` body**

Current body (lines 1307-1332) just stashes self.quality and returns. Replace with:

```python
    def set_encoding_quality(self, int quality) -> None:
        assert self.context, "context is not initialized"
        if self.quality == quality:
            return
        cdef int old_quality = self.quality
        self.quality = quality
        # Apply edge resistance to target quality (preserve existing logic)
        cdef int target_quality
        if quality < LOSSLESS_THRESHOLD:
            raw_delta = quality - old_quality
            max_delta = max(-1, min(1, raw_delta)) * 10
            if abs(raw_delta) < abs(max_delta):
                delta = raw_delta
            else:
                delta = max_delta
            target_quality = quality - delta
        else:
            target_quality = 100
        log("set_encoding_quality(%s) target quality=%s", quality, target_quality)
        # Determine new pixel format / lossless target. If they would change,
        # fall through (no reconfigure) — R1's candidate-space gate will
        # catch the pixel-format band change and force teardown on next tick.
        new_pixel_format = self.get_target_pixel_format(target_quality)
        new_lossless = self.get_target_lossless(new_pixel_format, target_quality)
        if new_pixel_format != self.pixel_format or new_lossless != self.lossless:
            log("set_encoding_quality(%s): pixel format/lossless would change "
                "(%s->%s, %s->%s) — falling through to teardown via R1",
                quality, self.pixel_format, new_pixel_format,
                self.lossless, new_lossless)
            return
        # Bitrate-only path: recompute bitrate targets and push via reconfigure
        self.update_bitrate()
        self._apply_reconfigure(reset_encoder=1, force_idr=1)
```

- [ ] **Step 2: Add `_apply_reconfigure`**

Add as a new method on Encoder (place it adjacent to `set_encoding_speed`/`set_encoding_quality`):

```python
    cdef void _apply_reconfigure(self, int reset_encoder, int force_idr):
        """Reconfigure the live nvenc encoder. Uses the cached init-params
        snapshot (R3 prereq 2) so presetGUID is preserved. Bitrate fields
        come from tune_qp() (R3 prereq 1) populating rcParams from
        self.target_bitrate/self.max_bitrate.

        Rate-limited: minimum interval between reconfigure calls is
        RECONFIGURE_MIN_INTERVAL_NS (default 250ms) so auto-tuner bursts
        don't issue back-to-back reconfigures. Callers can ignore the
        return value; the implicit skip means the cached bitrate is
        already current enough.
        """
        cdef double now = monotonic()
        if now - self._last_reconfigure_time < RECONFIGURE_MIN_INTERVAL_S:
            return  # debounced: prior reconfigure within min-interval window
        self._last_reconfigure_time = now
        cdef NV_ENC_RECONFIGURE_PARAMS reconfigure_params
        cdef NVENCSTATUS r
        memset(&reconfigure_params, 0, sizeof(NV_ENC_RECONFIGURE_PARAMS))
        reconfigure_params.version = NV_ENC_RECONFIGURE_PARAMS_VER
        # Copy the cached snapshot — DO NOT call init_params() here.
        # init_params() re-runs get_preset() and may change presetGUID,
        # which nvenc reconfigure rejects. _copy_cached_init_params
        # allocates a fresh encodeConfig on dest; the finally below
        # free()s it.
        self._copy_cached_init_params(&reconfigure_params.reInitEncodeParams)
        try:
            # Overlay updated bitrate fields via tune_qp on the rcParams.
            if reconfigure_params.reInitEncodeParams.encodeConfig != NULL:
                self.tune_qp(&reconfigure_params.reInitEncodeParams.encodeConfig.rcParams)
            reconfigure_params.resetEncoder = reset_encoder
            reconfigure_params.forceIDR = force_idr
            with nogil:
                r = self.functionList.nvEncReconfigureEncoder(self.context, &reconfigure_params)
            raiseNVENC(r, "reconfiguring encoder")
            log("nvEncReconfigureEncoder OK: target_bitrate=%i max_bitrate=%i",
                self.target_bitrate, self.max_bitrate)
        finally:
            if reconfigure_params.reInitEncodeParams.encodeConfig != NULL:
                free(reconfigure_params.reInitEncodeParams.encodeConfig)
```

Add the debounce-interval constant near the other module-level envvars at the top of the file:

```python
cdef double RECONFIGURE_MIN_INTERVAL_S = envint("XPRA_NVENC_RECONFIGURE_MIN_INTERVAL_MS", 250) / 1000.0
```

Add the field declaration to the `cdef class Encoder` block:

```python
        cdef double _last_reconfigure_time
```

Initialize it to `0.0` in `__cinit__` (or wherever fields are init'd).

- [ ] **Step 3: Modify `set_encoding_speed` for preset-boundary check**

The current `set_encoding_speed` (line 1302) calls `update_bitrate()` if speed changed but doesn't push to nvenc. We need to:

1. Detect when the new speed would change the preset GUID (NVENC reconfigure cannot change presetGUID).
2. On preset change, mark the encoder unrecoverable so the next `verify_csc_and_encoder()` returns False and the existing teardown path fires cleanly.
3. Otherwise, update bitrate and reconfigure.

First, declare the new field. Add to the `cdef class Encoder` field block:

```python
        cdef int _preset_unrecoverable
```

Initialize in `__cinit__` (or wherever other fields are init'd):

```python
        self._preset_unrecoverable = 0
```

Make `is_closed()` honor the flag (locate the existing `is_closed` method; if it's currently `return bool(self.closed)`, change to):

```python
    def is_closed(self) -> bool:
        return bool(self.closed) or bool(self._preset_unrecoverable)
```

Now the `set_encoding_speed` body:

```python
    def set_encoding_speed(self, int speed) -> None:
        if self.speed == speed:
            return
        cdef GUID new_preset
        cdef int new_preset_changed = 0
        self.speed = speed  # commit the new speed first so get_preset reads it
        if self._cached_init_params_valid:
            new_preset = self.get_preset(self.codec)
            new_preset_changed = (memcmp(&new_preset, &self._cached_preset_guid, sizeof(GUID)) != 0)
        if new_preset_changed:
            log("set_encoding_speed(%i): preset boundary crossed; marking encoder "
                "as unrecoverable so verify_csc_and_encoder triggers teardown", speed)
            self._preset_unrecoverable = 1
            return
        self.update_bitrate()
        self._apply_reconfigure(reset_encoder=1, force_idr=1)
```

- [ ] **Step 4: Compile**

```bash
cd /home/achen/empty/xpra-src && /usr/bin/python3 setup.py build_ext --inplace 2>&1 | tail -20
echo "EXIT=$?"
```

Expected: `EXIT=0`. If `memcmp` isn't imported, add `from libc.string cimport memcpy, memcmp`.

- [ ] **Step 5: Commit**

```bash
git add xpra/codecs/nvidia/nvenc/encoder.pyx
git commit -F - <<'EOF'
nvenc: bitrate-only reconfigure path (R3)

Wires set_encoding_quality and set_encoding_speed to actually push
bitrate changes to the live encoder via nvEncReconfigureEncoder
instead of being silently dropped (quality) or stashed without
effect (speed).

Guards:
- Quality: if target pixel_format or lossless mode would change,
  fall through (no reconfigure). R1's candidate-space tuple's
  fingerprint element catches the band change and triggers teardown.
- Speed: compare get_preset() against the cached preset GUID. If
  different, set _preset_unrecoverable so is_closed() returns True;
  next verify_csc_and_encoder() returns False; teardown happens
  cleanly via the existing path.

_apply_reconfigure uses the cached init-params snapshot (Prereq 2)
to preserve presetGUID across the reconfigure call. tune_qp (Prereq 1)
populates the bitrate fields from self.target_bitrate/max_bitrate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
Sponsored-By: Netflix
EOF
```

---

## Integration validation

### Task 19: Build R1+R2+R3 .deb (Andrew handles deploy)

**Files:**
- None (build step)

- [ ] **Step 1: Build .deb (no deploy)**

```bash
./tests/docker/build-deb.sh 2>&1 | tail -20
echo "EXIT=$?"
```

Expected: `EXIT=0`. Build artifacts in `build-deb-out/`.

- [ ] **Step 2: Confirm artifacts**

```bash
ls -la build-deb-out/*.deb 2>&1
```

Expected: at least one `.deb` file.

- [ ] **Step 3: Push branch**

```bash
git push fork feature/nvenc-reinit-storm-v6.4.3:feature/nvenc-reinit-storm-v6.4.3
```

Expected: branch pushed.

### Task 20: Live verification (Andrew runs; implementer documents expectations)

The implementer cannot run these checks (no sudo to install the .deb). Document the expected post-deploy verification so Andrew has a checklist:

**Andrew's post-deploy verification checklist:**

1. Install the .deb from `build-deb-out/`, restart `xpra.service`.
2. Open Edge with multiple tabs; play NPR Tiny Desk video in one tab; scroll/switch between others for ≥1 min.
3. Run the gate checks:

```bash
xpra info :100 | grep context_count
journalctl --user -u xpra.service --since "5 min ago" | grep -c "failed to acquire cuda device lock"
journalctl --user -u xpra.service --since "5 min ago" | grep -c "failed to encode h265"
xpra info :100 | grep reinit_count
journalctl --user -u xpra.service --since "5 min ago" | grep -c "nvEncReconfigureEncoder OK"
```

Expected per spec:
- `context_count == 1` for active video window (was 2 during storm)
- `cdc.lock` warnings < 5 per minute (was ~10)
- `failed to encode h265` warnings near 0 (was ~5/min)
- `reinit_count` deltas drop by >80% vs Phase 0 baseline
- `nvEncReconfigureEncoder OK` > 0 (R3 actively firing for operating-point nudges)

If any gate fails, report findings back to the implementer; the diff at HEAD is on `feature/nvenc-reinit-storm-v6.4.3` for follow-up debugging.

---

## Codex review

### Task 21: Combined spec + plan codex review

**Files:**
- Plan & spec only.

- [ ] **Step 1: Stage both for combined review**

The spec is at `docs/superpowers/specs/2026-05-23-nvenc-reinit-storm-design.md`. The plan is at `docs/superpowers/plans/2026-05-23-nvenc-reinit-storm.md`. Both are on `feature/nvenc-reinit-storm-v6.4.3` after Task 20.

- [ ] **Step 2: Run combined codex review**

```bash
codex-review-capture --base fork/feature/onevpl-hevc-444-v6.4.3
```

Expected: codex reviews the full diff from the VPL base to HEAD — that's Phase 0 changes + R1/R2/R3 code + spec + plan (cherry-picked in Task 1) in one pass. The header comment in the spec instructs codex to focus on prose; the code changes get reviewed substantively.

- [ ] **Step 3: Triage codex findings**

Read the verdict. Real issues → fix and re-run. Style/preference → document and move on. If codex finds another round of P2 architectural issues, pause and bring them to Andrew before pressing on.

---

## Deferred follow-ups (intentional)

The following items are mentioned in the spec but intentionally NOT implemented in this plan. They're listed here so they don't get silently dropped during the master port:

- **`XPRA_EDGE_RESISTANCE_BOOST` envvar (spec § R2b).** Tunable additive bonus to `ee_score` for the current encoder. Spec calls it "optional" — needed only if R2a alone over-sticks. Defer until post-deployment data justifies it.
- **Storm regression test (spec § Verification — Storm regression test).** Parameterized version of the manual repro script in Task 20, suitable for CI. Requires a CUDA-equipped CI runner; depends on whether xpra's CI infrastructure includes one. If we add it later, the test would: open N Edge tab windows on the server, play HEVC 4:4:4 video in one, rapidly switch focus; assert `reinit_count` delta over 60s remains below threshold. Useful as a regression gate; not strictly required to ship.
- **Per-path reinit counters / delta-type tracking / edge-resistance snapshot diagnostics.** Spec deferred these to lever-specific opt-in env vars (`XPRA_R1_DIAG=1`, `XPRA_R2_DIAG=1`). Add only if post-deployment data shows we need finer-grained diagnosis.

## Master port (deferred)

After v6.4.3-achen validates over a week of real use, port to `master` for upstream PRs. Each lever can ship as its own PR if scope is too large for one:
- Phase 0 alone as one PR.
- R2 alone as one PR (smallest, most contained — the one-line scoring fix).
- R1 + R3 together as one PR (they're conceptually paired — R1's `_push_operating_point` only does something useful once R3 is wired).

Branch names: `feature/nvenc-reinit-{instrument,r2,r1-r3}-master` off `origin/master`. Apply commits cherry-pick style; resolve any conflicts that arise from upstream drift. PR description references the design spec.

---

## Self-review

(For the plan author — not the implementer.)

**Spec coverage check:**
- Phase 0 aggregate reinit counter → Task 2-3 ✓
- cdc.lock warning enrichment → Task 4-5 ✓
- R1 candidate-space gate + helpers → Task 8-11 ✓
- R1 quality_changed/speed_changed reroute → Task 12 ✓
- R1 safety valve → Task 13 ✓
- R2a edge resistance always-on → Task 14 ✓
- Y2 deadband → Task 15 ✓
- R3 prereq 1 (tune_qp bitrate wiring) → Task 16 ✓
- R3 prereq 2 (cached init params) → Task 17 ✓
- R3 main path (set_encoding_quality/speed + _apply_reconfigure) → Task 18 ✓
- Verification gates → Task 20 ✓
- Branch+deploy strategy → Tasks 1, 6, 7, 19 ✓
- Master port → noted at end as deferred ✓

**Spec NOT covered in plan (intentional deferrals):**
- Future Work fps_band — explicitly deferred per the spec's own Future Work section
- Master ports — described but actual port commits deferred until v6.4.3 validates

**Placeholders / TODOs:**
- Task 13 Step 3 has one acknowledged TODO (safety valve hook location is exploration-dependent). All other tasks are concrete.

**Type/method name consistency:**
- `_compute_candidate_space()` / `_last_candidate_space` / `_push_operating_point()` / `_maybe_invalidate_for_operating_point()` / `_compute_candidate_pixel_format_fingerprint()` / `_compute_desired_scaling()` — used consistently across Tasks 8-13.
- `_apply_reconfigure()` / `_copy_cached_init_params()` / `_cached_init_params` / `_cached_preset_guid` / `_cached_init_params_valid` / `_preset_unrecoverable` — used consistently across Tasks 17-18.
- `reinit_count` — used in Task 2, Task 6, Task 20.
- `YUV444_DEADBAND` — declared in Task 9 or 15, used in Task 8/15 fingerprint helper.

**Spec coverage that may be thin in plan:**
- The `XPRA_EDGE_RESISTANCE_BOOST` envvar mentioned in spec R2b is NOT implemented in the plan. It's described in the spec as "optional", needed if R2a is too sticky. Decision: defer until we have post-deployment data. Add to a "Future tweaks" section in the spec if we want a paper trail.
