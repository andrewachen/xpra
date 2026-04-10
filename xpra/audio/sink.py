#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
from time import monotonic
from collections import deque
from collections.abc import Sequence
from threading import Lock
from typing import Any, Literal, NoReturn

from xpra.audio.audio_pipeline import AudioPipeline
from xpra.gstreamer.common import (
    normv, make_buffer, plugin_str,
    get_default_appsrc_attributes, get_element_str,
    GST_FLOW_OK,
)
from xpra.audio.gstreamer_util import (
    get_decoder_elements, has_plugins,
    get_queue_time, get_decoders,
    get_default_sink_plugin, get_sink_plugins,
    MP3, CODEC_ORDER, QUEUE_LEAK,
    GST_QUEUE_NO_LEAK, MS_TO_NS, DEFAULT_SINK_PLUGIN_OPTIONS,
)
from xpra.audio.jitter import (
    DelayHistogram,
    JITTER_MIN_SAMPLES, JITTER_PERCENTILE, MAX_JITTER_BUFFER,
    PEAK_HOLD_SECONDS,
)
from xpra.util.gobject import one_arg_signal
from xpra.net.compression import decompress_by_name
from xpra.scripts.config import InitExit
from xpra.common import SizedBuffer
from xpra.os_util import gi_import
from xpra.util.str_fn import csv
from xpra.util.env import envint, envbool
from xpra.util.thread import start_thread
from xpra.log import Logger

GLib = gi_import("GLib")

_tempo_error = ""
_tempo_backend = "none"
# importing tempo registers the "tempo" BaseTransform element with GStreamer:
try:
    from xpra.audio.tempo import tempo_available, get_load_error, get_backend_name
    _tempo_available = tempo_available()
    _tempo_backend = get_backend_name()
    if not _tempo_available:
        _tempo_error = get_load_error()
except Exception as e:
    _tempo_available = False
    _tempo_error = str(e)

log = Logger("audio")
gstlog = Logger("gstreamer")

GObject = gi_import("GObject")

SINK_SHARED_DEFAULT_ATTRIBUTES: dict[str, Any] = {
    "sync": False,
}
NON_AUTO_SINK_ATTRIBUTES: dict[str, Any] = {
    "async": True,
    "qos": True,
}

SINK_DEFAULT_ATTRIBUTES: dict[str, dict[str, str]] = {
    "pulsesink": {"client-name": "Xpra"},
    "wasapisink": {"low-latency": True, "buffer-time": 10000},
    "wasapi2sink": {"low-latency": True, "buffer-time": 10000},
}

QUEUE_SILENT = envbool("XPRA_QUEUE_SILENT", False)
QUEUE_TIME = get_queue_time(450)

UNMUTE_DELAY = envint("XPRA_UNMUTE_DELAY", 1000)
# sinks that handle volume internally and don't pop on start:
NO_UNMUTE_RAMP = {"wasapisink", "wasapi2sink"}
GRACE_PERIOD = envint("XPRA_SOUND_GRACE_PERIOD", 2000)
# percentage: from 0 for no margin, to 200% which triples the buffer target
MARGIN = max(0, min(200, envint("XPRA_SOUND_MARGIN", 50)))
# how high we push up the min-level to prevent underruns:
UNDERRUN_MIN_LEVEL = max(0, envint("XPRA_SOUND_UNDERRUN_MIN_LEVEL", 150))
CLOCK_SYNC = envbool("XPRA_CLOCK_SYNC", False)

# proportional controller for dynamic audio buffer sizing:
AV_SYNC_INTERVAL_MS = 200
AV_SYNC_DEAD_BAND_MS = 15
AV_SYNC_GAIN = 0.3
AV_SYNC_MAX_STEP_MS = 30
AV_SYNC_HEADROOM_MS = 30

# NetEQ-inspired tempo control via libsonic or SoundTouch (ctypes).
# Applied via BaseTransform element — pass-through at tempo=1.0
# (near-zero cost on LAN), only invokes the library when stretching.
TEMPO_NORMAL = 1.0
TEMPO_PREEMPTIVE_EXPAND = 0.975
TEMPO_ACCELERATE = 1.025
TEMPO_FAST_ACCELERATE = 1.05
TEMPO_COOLDOWN_TICKS = 5  # minimum ticks between changes (5 × 200ms = 1 second)
# Opus default frame duration. Buffer levels quantize in steps of this size,
# so the "normal" zone must be wider than this to avoid oscillation:
OPUS_FRAME_MS = 20
# sonic needs 2 * (48000/65) = 1476 samples ≈ 30ms. Use 2 frames for safety:
TEMPO_MIN_TARGET_MS = 2 * OPUS_FRAME_MS


def compute_tempo(level_ms: int, target_ms: int,
                  current_tempo: float, ticks_at_tempo: int) -> float:
    """
    Decide playback tempo based on current queue level vs target.

    NetEQ-inspired discrete states — slows down to prevent underrun,
    speeds up to drain excess buffer without dropping audio.
    """
    if target_ms < TEMPO_MIN_TARGET_MS:
        return TEMPO_NORMAL
    # NetEQ-inspired thresholds (fraction of target buffer):
    lower_limit = target_ms * 3 // 4
    # normal zone must be wider than buffer granularity (OPUS_FRAME_MS)
    # to avoid reacting to normal packet arrival oscillation:
    higher_limit = max(target_ms, lower_limit + 2 * OPUS_FRAME_MS)
    # severely overfull — drain aggressively:
    fast_limit = higher_limit * 4
    if level_ms <= lower_limit:
        desired = TEMPO_PREEMPTIVE_EXPAND
    elif level_ms > fast_limit:
        desired = TEMPO_FAST_ACCELERATE
    elif level_ms > higher_limit:
        desired = TEMPO_ACCELERATE
    else:
        desired = TEMPO_NORMAL
    # override cooldown for emergency: never accelerate while buffer is empty
    if level_ms == 0 and current_tempo > TEMPO_NORMAL:
        return TEMPO_PREEMPTIVE_EXPAND
    if desired != current_tempo and ticks_at_tempo < TEMPO_COOLDOWN_TICKS:
        return current_tempo
    return desired


def uncompress_data(data: bytes, metadata: dict) -> SizedBuffer:
    if not data or not metadata:
        return data
    compress = metadata.get("compress")
    if not compress:
        return data
    if compress != "lz4":
        raise ValueError(f"unsupported compresssion {compress!r}")
    v = decompress_by_name(data, compress)
    # log("decompressed %s data: %i bytes into %i bytes", compress, len(data), len(v))
    return v


class AudioSink(AudioPipeline):
    __gsignals__ = AudioPipeline.__generic_signals__.copy()
    __gsignals__ |= {
        "eos": one_arg_signal,
    }

    def __init__(self, sink_type: str, sink_options: dict, codecs: Sequence[str], codec_options: dict, volume=1.0):
        if not sink_type:
            sink_type = get_default_sink_plugin()
        if sink_type not in get_sink_plugins():
            raise InitExit(1, "invalid sink: %s" % sink_type)
        matching = [x for x in CODEC_ORDER if (x in codecs and x in get_decoders())]
        log("AudioSink(..) found matching codecs %s", matching)
        if not matching:
            raise InitExit(1, "no matching codecs between arguments '%s' and supported list '%s'" % (
                csv(codecs), csv(get_decoders().keys())))
        codec = matching[0]
        decoder, parser, stream_compressor = get_decoder_elements(codec)
        super().__init__(codec)
        self.container_format = (parser or "").replace("demux", "").replace("depay", "")
        self.sink_type = sink_type
        self.stream_compressor = stream_compressor
        log("container format=%s, stream_compressor=%s, sink type=%s",
            self.container_format, self.stream_compressor, self.sink_type)
        self.levels = deque(maxlen=100)
        self.volume = None
        self.src = None
        self.sink = None
        self.queue = None
        self.normal_volume = volume
        self.target_volume = volume
        self.volume_timer = 0
        self.overruns = 0
        self.underruns = 0
        self.overrun_events = deque(maxlen=100)
        self.queue_state = "starting"
        self.last_data = None
        self.last_underrun = 0
        self.last_overrun = 0
        self.refill = True
        self.last_max_update = monotonic()
        self.last_min_update = monotonic()
        self.level_lock = Lock()
        self.av_sync_target: int = 0
        self.av_sync_timer: int = 0
        self.tempo_element = None
        self.current_tempo: float = TEMPO_NORMAL
        self.ticks_at_tempo: int = 0
        self.tempo_count: int = 0
        self.probe_errors: int = 0
        # EMA-smoothed buffer level prevents tempo oscillation from
        # quantized 20ms queue steps (NetEQ's BufferLevelFilter):
        self._filtered_level: float = 0.0
        # track ms of audio added/removed by stretching. subtracted from
        # the raw level so tempo decisions account for prior adjustments
        # (prevents accelerate→expand→accelerate feedback loop):
        self._sample_memory: float = 0.0
        # subprocess-local jitter measurement (for QUIC pipe bypass where
        # _process_audio_data in the main process is skipped):
        self._delay_histogram = DelayHistogram()
        self._delay_peaks: deque[tuple[float, float]] = deque(maxlen=8)
        self._cached_p97: float = 0.0
        self._last_audio_arrival: float = 0.0
        self._last_audio_server_time: int = 0
        self._jitter_target: int = 0
        pipeline_els = [get_element_str("appsrc", get_default_appsrc_attributes())]
        if parser:
            pipeline_els.append(parser)
        if decoder:
            decoder_str = plugin_str(decoder, codec_options)
            pipeline_els.append(decoder_str)
        pipeline_els.append("audioconvert")
        pipeline_els.append("audioresample")
        if QUEUE_TIME > 0:
            pipeline_els.append(get_element_str("queue", {
                "name": "queue",
                "min-threshold-time": 0,
                "max-size-buffers": 0,
                "max-size-bytes": 0,
                "max-size-time": QUEUE_TIME,
                "leaky": QUEUE_LEAK,
            }))
        if _tempo_available:
            pipeline_els.append("audioconvert")
            pipeline_els.append(plugin_str("capsfilter", {
                "caps": "audio/x-raw,format=S16LE",
            }))
            pipeline_els.append(get_element_str("tempo", {"name": "tempo"}))
            pipeline_els.append("audioconvert")
            gstlog("tempo element enabled (BaseTransform, %s)", _tempo_backend)
        pipeline_els.append(get_element_str("volume", {"name": "volume", "volume": 0}))
        if CLOCK_SYNC:
            if not has_plugins("clocksync"):
                log.warn("Warning: cannot enable clocksync, element not found")
            else:
                pipeline_els.append("clocksync")
        sink_attributes = SINK_SHARED_DEFAULT_ATTRIBUTES.copy()
        # anything older than this may cause problems (ie: centos 6.x)
        # because the attributes may not exist
        sink_attributes.update(SINK_DEFAULT_ATTRIBUTES.get(sink_type, {}))
        get_options_cb = DEFAULT_SINK_PLUGIN_OPTIONS.get(sink_type.replace("sink", ""))
        if get_options_cb:
            v = get_options_cb()
            log("%s()=%s", get_options_cb, v)
            sink_attributes.update(v)
        if sink_options:
            sink_attributes.update(sink_options)
        sink_attributes["name"] = "sink"
        if sink_type != "autoaudiosink":
            sink_attributes.update(NON_AUTO_SINK_ATTRIBUTES)
        sink_str = plugin_str(sink_type, sink_attributes)
        pipeline_els.append(sink_str)
        if not self.setup_pipeline_and_bus(pipeline_els):
            return
        self.volume = self.pipeline.get_by_name("volume")
        self.src = self.pipeline.get_by_name("src")
        self.sink = self.pipeline.get_by_name("sink")
        self.queue = self.pipeline.get_by_name("queue")
        self.tempo_element = self.pipeline.get_by_name("tempo")
        if self.queue:
            if QUEUE_SILENT:
                self.queue.set_property("silent", False)
            else:
                self.queue.connect("overrun", self.queue_overrun)
                self.queue.connect("underrun", self.queue_underrun)
                self.queue.connect("running", self.queue_running)
                self.queue.connect("pushing", self.queue_pushing)
        self.init_file(codec)

    def __repr__(self):  # pylint: disable=arguments-differ
        return "AudioSink('%s' - %s)" % (self.pipeline_str, self.state)

    def cleanup(self) -> None:
        from xpra.audio.device_monitor import stop_device_monitor
        stop_device_monitor()
        super().cleanup()
        self.tempo_element = None
        self.cancel_volume_timer()
        self.cancel_av_sync_timer()
        self.sink_type = ""
        self.src = None

    def start(self) -> bool:
        if not super().start():
            return False
        if self.sink:
            for prop in ("actual-buffer-time", "actual-latency-time",
                         "low-latency", "use-audioclient3", "exclusive"):
                try:
                    gstlog("%s %s: %s", self.sink_type, prop, self.sink.get_property(prop))
                except Exception:
                    pass
        if self.sink_type in NO_UNMUTE_RAMP:
            self.set_volume(int(self.normal_volume * 100))
        else:
            GLib.timeout_add(UNMUTE_DELAY, self.start_adjust_volume)
        from xpra.audio.device_monitor import start_device_monitor
        start_device_monitor(self._on_device_change)
        return True

    def _on_device_change(self) -> None:
        log.info("audio output device changed")
        # emit synchronously — idle_emit would race with cleanup:
        self.emit("error", "AUDIO_DEVICE_CHANGED")
        self.cleanup()

    def start_adjust_volume(self, interval: int = 100) -> bool:
        if self.volume_timer != 0:
            GLib.source_remove(self.volume_timer)
        self.volume_timer = GLib.timeout_add(interval, self.adjust_volume)
        return False

    def cancel_volume_timer(self) -> None:
        if self.volume_timer != 0:
            GLib.source_remove(self.volume_timer)
            self.volume_timer = 0

    def cancel_av_sync_timer(self) -> None:
        if self.av_sync_timer != 0:
            GLib.source_remove(self.av_sync_timer)
            self.av_sync_timer = 0

    def set_av_sync_target(self, target_ms: int) -> None:
        prev_target = self.av_sync_target
        self.av_sync_target = max(0, target_ms)
        if self.av_sync_target > 0:
            # jump buffer immediately on first target or significant increase
            # (waiting for the next tick adds up to 200ms delay):
            first_target = prev_target == 0
            significant_increase = self.av_sync_target > prev_target + AV_SYNC_MAX_STEP_MS
            if (first_target or significant_increase) and self.queue and self.level_lock.acquire(False):
                try:
                    if first_target:
                        self.queue.set_property("min-threshold-time", 0)
                    target_max = self.av_sync_target + AV_SYNC_HEADROOM_MS
                    self.queue.set_property("max-size-time", target_max * MS_TO_NS)
                    gstlog("av_sync: jump max-size-time=%i (target=%i, prev=%i)",
                           target_max, self.av_sync_target, prev_target)
                finally:
                    self.level_lock.release()
            if self.av_sync_timer == 0:
                self.av_sync_timer = GLib.timeout_add(AV_SYNC_INTERVAL_MS, self._av_sync_tick)
        elif self.av_sync_target == 0 and self._jitter_target == 0:
            self.cancel_av_sync_timer()

    def _compute_jitter_target(self) -> int:
        """Compute buffer target from locally-measured jitter (p97 + peak hold)."""
        hist = self._delay_histogram
        if hist.count < JITTER_MIN_SAMPLES:
            return 0
        p97 = hist.percentile(JITTER_PERCENTILE)
        self._cached_p97 = p97
        jitter_ms = min(p97, MAX_JITTER_BUFFER)
        now = monotonic()
        active_peaks = [amp for t, amp in self._delay_peaks if now - t < PEAK_HOLD_SECONDS]
        if active_peaks:
            jitter_ms = max(jitter_ms, min(max(active_peaks), MAX_JITTER_BUFFER))
        return max(20, int(jitter_ms)) if jitter_ms > 0 else 0

    def _av_sync_tick(self) -> bool:
        if not self.queue or self.queue_state == "starting":
            return True
        # combine externally-set target (video decode) with local jitter:
        self._jitter_target = self._compute_jitter_target()
        effective_target = max(self.av_sync_target, self._jitter_target)
        current_max = self.queue.get_property("max-size-time") // MS_TO_NS
        target_max = effective_target + AV_SYNC_HEADROOM_MS
        # asymmetric: always grow (no dead band), only shrink past dead band:
        if current_max < target_max:
            new_max = target_max
        elif current_max - target_max >= AV_SYNC_DEAD_BAND_MS:
            max_correction = max(-AV_SYNC_MAX_STEP_MS, min(AV_SYNC_MAX_STEP_MS,
                                                            (current_max - target_max) * AV_SYNC_GAIN))
            new_max = max(AV_SYNC_HEADROOM_MS, int(current_max - max_correction))
        else:
            new_max = current_max
        if new_max != current_max and self.level_lock.acquire(False):
            try:
                self.queue.set_property("max-size-time", new_max * MS_TO_NS)
                gstlog("av_sync_tick: target=%i (ext=%i, jitter=%i), max=%i→%i",
                       effective_target, self.av_sync_target, self._jitter_target,
                       current_max, new_max)
            finally:
                self.level_lock.release()
        # tempo control via BaseTransform element (libsonic/SoundTouch):
        if self.tempo_element:
            raw_level = self.queue.get_property("current-level-time") // MS_TO_NS
            # adjust for audio added/removed by prior stretching:
            adjusted = max(0, raw_level - self._sample_memory)
            # asymmetric EMA: track drops quickly (react to drains before
            # underrun), track rises slowly (avoid false accelerate triggers):
            alpha = 0.3 if adjusted < self._filtered_level else 0.1
            self._filtered_level = alpha * adjusted + (1 - alpha) * self._filtered_level
            level_ms = int(self._filtered_level)
            self.ticks_at_tempo += 1
            new_tempo = compute_tempo(level_ms, effective_target,
                                      self.current_tempo, self.ticks_at_tempo)
            lower = effective_target * 3 // 4
            higher = max(effective_target, lower + 2 * OPUS_FRAME_MS)
            gstlog("av_sync_tick: raw=%i, adj=%i, filt=%i, target=%i (ext=%i, jitter=%i), "
                   "limits=[%i,%i], tempo=%.3f, mem=%.1f, ticks=%i",
                   raw_level, int(adjusted), level_ms,
                   effective_target, self.av_sync_target, self._jitter_target,
                   lower, higher, self.current_tempo,
                   self._sample_memory, self.ticks_at_tempo)
            if new_tempo != self.current_tempo:
                self.tempo_element.set_tempo(new_tempo)
                gstlog("av_sync_tick: tempo %.3f→%.3f (filt=%i, target=%i)",
                       self.current_tempo, new_tempo, level_ms, effective_target)
                self.current_tempo = new_tempo
                self.ticks_at_tempo = 0
                # reset sample memory on tempo change — it tracked
                # the effect of the previous tempo, not the new one:
                self._sample_memory = 0.0
            elif self.current_tempo != TEMPO_NORMAL:
                # accumulate sample memory: at tempo T, each 20ms tick
                # adds/removes (T-1.0) * OPUS_FRAME_MS worth of audio:
                self._sample_memory += (self.current_tempo - 1.0) * OPUS_FRAME_MS
        return True

    def adjust_volume(self) -> bool:
        if not self.volume:
            self.volume_timer = 0
            return False
        cv = self.volume.get_property("volume")
        delta = self.target_volume - cv
        from math import sqrt, copysign
        change = copysign(sqrt(abs(delta)), delta) / 15.0
        gstlog("adjust_volume current volume=%.2f, change=%.2f", cv, change)
        self.volume.set_property("volume", max(0, cv + change))
        if abs(delta) < 0.01:
            self.volume_timer = 0
            return False
        return True

    def queue_pushing(self, *_args) -> Literal[True]:
        gstlog("queue_pushing")
        self.queue_state = "pushing"
        self.emit_info()
        return True

    def queue_running(self, *_args) -> Literal[True]:
        gstlog("queue_running")
        self.queue_state = "running"
        self.emit_info()
        return True

    def queue_underrun(self, *_args) -> Literal[True]:
        now = monotonic()
        if self.queue_state == "starting" or 1000 * (now - self.start_time) < GRACE_PERIOD:
            gstlog("ignoring underrun during startup")
            return True
        self.underruns += 1
        gstlog("queue_underrun")
        self.queue_state = "underrun"
        if now - self.last_underrun > 5:
            # only count underruns when we're back to no min time:
            qmin = self.queue.get_property("min-threshold-time") // MS_TO_NS
            clt = self.queue.get_property("current-level-time") // MS_TO_NS
            gstlog("queue_underrun level=%3i, min=%3i", clt, qmin)
            if qmin == 0 and clt < 10:
                self.last_underrun = now
                self.refill = True
                self.set_max_level()
                self.set_min_level()
        self.emit_info()
        return True

    def get_level_range(self, mintime=2, maxtime=10) -> int:
        now = monotonic()
        filtered = [v for t, v in tuple(self.levels) if mintime <= (now - t) <= maxtime]
        if len(filtered) >= 10:
            maxl = max(filtered)
            minl = min(filtered)
            # range of the levels recorded:
            return maxl - minl
        return 0

    def queue_overrun(self, *_args) -> Literal[True]:
        now = monotonic()
        if self.queue_state == "starting" or 1000 * (now - self.start_time) < GRACE_PERIOD:
            gstlog("ignoring overrun during startup")
            return True
        clt = self.queue.get_property("current-level-time") // MS_TO_NS
        log("queue_overrun level=%ims", clt)
        now = monotonic()
        # grace period of recording overruns:
        # (because when we record an overrun, we lower the max-time,
        # which causes more overruns!)
        if now - self.last_overrun > 2:
            self.last_overrun = now
            self.set_max_level()
            self.overrun_events.append(now)
        self.overruns += 1
        return True

    def set_min_level(self) -> None:
        if self.av_sync_target > 0:
            return
        if not self.queue:
            return
        now = monotonic()
        elapsed = now - self.last_min_update
        lrange = self.get_level_range()
        log("set_min_level() lrange=%i, elapsed=%i", lrange, elapsed)
        if elapsed < 1:
            # not more than once a second
            return
        if self.refill:
            # need to have a gap between min and max,
            # so we cannot go higher than mst-50:
            mst = self.queue.get_property("max-size-time") // MS_TO_NS
            mrange = max(lrange + 100, UNDERRUN_MIN_LEVEL)
            mtt = min(mst - 50, mrange)
            gstlog("set_min_level mtt=%3i, max-size-time=%3i, lrange=%s, mrange=%s (UNDERRUN_MIN_LEVEL=%s)",
                   mtt, mst, lrange, mrange, UNDERRUN_MIN_LEVEL)
        else:
            mtt = 0
        cmtt = self.queue.get_property("min-threshold-time") // MS_TO_NS
        if cmtt == mtt:
            return
        if not self.level_lock.acquire(False):
            gstlog("cannot get level lock for setting min-threshold-time")
            return
        try:
            self.queue.set_property("min-threshold-time", mtt * MS_TO_NS)
            gstlog("set_min_level min-threshold-time=%s", mtt)
            self.last_min_update = now
        finally:
            self.level_lock.release()

    def set_max_level(self) -> None:
        if self.av_sync_target > 0:
            return
        if not self.queue:
            return
        now = monotonic()
        elapsed = now - self.last_max_update
        if elapsed < 1:
            # not more than once a second
            return
        lrange = self.get_level_range(mintime=0)
        log("set_max_level lrange=%3i, elapsed=%is", lrange, int(elapsed))
        cmst = self.queue.get_property("max-size-time") // MS_TO_NS
        # overruns in the last minute:
        olm = len([x for x in tuple(self.overrun_events) if now - x < 60])
        # increase target if we have more than 5 overruns in the last minute:
        target_mst = lrange * (100 + MARGIN + min(100, olm * 20)) // 100
        # from 100% down to 0% in 2 seconds after underrun:
        pct = max(0, int((self.last_overrun + 2 - now) * 50))
        # use this last_overrun percentage value to temporarily decrease the target
        # (causes overruns that drop packets and lower the buffer level)
        target_mst = max(50, int(target_mst - pct * lrange // 100))
        mst = (cmst + target_mst) // 2
        if self.refill:
            # temporarily raise max level during underruns,
            # so set_min_level has more room for manoeuver:
            mst += UNDERRUN_MIN_LEVEL
        # cap it at 1 second:
        mst = min(mst, 1000)
        log("set_max_level overrun count=%-2i, margin=%3i, pct=%2i, cmst=%3i, target=%3i, mst=%3i",
            olm, MARGIN, pct, cmst, target_mst, mst)
        if abs(cmst - mst) <= max(50, lrange // 2):
            # not enough difference
            return
        if not self.level_lock.acquire(False):
            gstlog("cannot get level lock for setting max-size-time")
            return
        try:
            self.queue.set_property("max-size-time", mst * MS_TO_NS)
            log("set_max_level max-size-time=%s", mst)
            self.last_max_update = now
        finally:
            self.level_lock.release()

    def eos(self) -> int:
        gstlog("eos()")
        if self.src:
            self.src.emit('end-of-stream')
        self.cleanup()
        return GST_FLOW_OK

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        if QUEUE_TIME > 0 and self.queue:
            clt = self.queue.get_property("current-level-time")
            qmax = self.queue.get_property("max-size-time")
            qmin = self.queue.get_property("min-threshold-time")
            info["queue"] = {
                "min": qmin // MS_TO_NS,
                "max": qmax // MS_TO_NS,
                "cur": clt // MS_TO_NS,
                "pct": min(QUEUE_TIME, clt) * 100 // qmax,
                "overruns": self.overruns,
                "underruns": self.underruns,
                "state": self.queue_state,
                "tempo": self.current_tempo,
                "pitch": self.tempo_element is not None,
                "pitch_error": _tempo_error,
                "pitch_backend": _tempo_backend,
                "pitch_status": getattr(self.tempo_element, "tempo_status", ""),
                "tempo_adjusted": getattr(self.tempo_element, "tempo_count", 0),
                "probe_errors": getattr(self.tempo_element, "probe_errors", 0),
                "padded": getattr(getattr(self.tempo_element, "_processor", None), "padding_count", 0),
                "jitter_p97": self._cached_p97,
                "jitter_target": self._jitter_target,
                "jitter_samples": self._delay_histogram.count,
            }
        info["sink"] = self.get_element_properties(
            self.sink,
            "buffer-time", "latency-time",
            "actual-buffer-time", "actual-latency-time",
            # "next_sample", "eos_rendering",
            "async", "blocksize",
            "enable-last-sample",
            "max-bitrate", "max-lateness",
            # "processing-deadline",
            "qos", "render-delay", "sync",
            "throttle-time", "ts-offset",
            "low-latency", "use-audioclient3", "exclusive",
            ignore_missing=True
        )
        return info

    def can_push_buffer(self) -> bool:
        if not self.src:
            log("no source, dropping buffer")
            return False
        if self.state in ("stopped", "error"):
            log("pipeline is %s, dropping buffer", self.state)
            return False
        return True

    def _measure_jitter(self, metadata: dict) -> None:
        """Measure inter-arrival jitter from packet timestamps.

        Computes one-way delay variation D from server PTS intervals vs client
        arrival intervals. D feeds into the exponential-decay histogram whose
        p97 drives the adaptive buffer target and tempo control.
        """
        pts_ns = metadata.get("timestamp", -1)
        if pts_ns > 0:
            server_time_ms = pts_ns // 1_000_000
        else:
            server_time_ms = metadata.get("time", 0)
        if not server_time_ms:
            return
        client_now = monotonic()
        if self._last_audio_arrival > 0 and self._last_audio_server_time > 0:
            arrival_diff = (client_now - self._last_audio_arrival) * 1000
            send_diff = server_time_ms - self._last_audio_server_time
            D = max(0.0, arrival_diff - send_diff)
            # filter out gaps (> 2s = outage recovery):
            if arrival_diff < 2000 and send_diff < 2000:
                self._delay_histogram.add(D)
                if D > 2 * max(self._cached_p97, 10):
                    self._delay_peaks.append((client_now, D))
        self._last_audio_arrival = client_now
        self._last_audio_server_time = server_time_ms

    def add_data(self, data: bytes, metadata: dict, packet_metadata=()) -> None:
        if not self.can_push_buffer():
            return
        self._measure_jitter(metadata)
        data = uncompress_data(data, metadata)
        for x in packet_metadata:
            self.do_add_data(x, {})
        if self.do_add_data(data, metadata):
            self.rec_queue_level(data)
            self.set_max_level()
            self.set_min_level()
            # drop back down quickly if the level has reached min:
            if self.refill:
                clt = self.queue.get_property("current-level-time") // MS_TO_NS
                qmin = self.queue.get_property("min-threshold-time") // MS_TO_NS
                gstlog("add_data: refill=%s, level=%i, min=%i", self.refill, clt, qmin)
                if 0 < qmin < clt:
                    self.refill = False
        # start the av sync timer if jitter data is ready but no external target set it:
        if self.av_sync_timer == 0 and self._delay_histogram.count >= JITTER_MIN_SAMPLES:
            self.av_sync_timer = GLib.timeout_add(AV_SYNC_INTERVAL_MS, self._av_sync_tick)
        self.emit_info()

    def do_add_data(self, data, metadata: dict) -> bool:
        # having a timestamp causes problems with the queue and overruns:
        log("do_add_data(%s bytes, %s) queue_state=%s", len(data), metadata, self.queue_state)
        self.save_to_file(data)
        buf = make_buffer(data)
        if metadata:
            # having a timestamp causes problems with the queue and overruns:
            # ts = metadata.get("timestamp")
            # if ts is not None:
            #    buf.timestamp = normv(ts)
            #    log.info("timestamp=%s", ts)
            d = metadata.get("duration")
            if d is not None:
                d = normv(d)
                if d > 0:
                    buf.duration = normv(d)
        if self.push_buffer(buf) == GST_FLOW_OK:
            self.inc_buffer_count()
            self.inc_byte_count(len(data))
            return True
        return False

    def rec_queue_level(self, data) -> None:
        q = self.queue
        if not q:
            return
        clt = q.get_property("current-level-time") // MS_TO_NS
        log("pushed %5i bytes, new buffer level: %3ims, queue state=%s", len(data), clt, self.queue_state)
        now = monotonic()
        self.levels.append((now, clt))

    def push_buffer(self, buf) -> int:
        # buf.size = size
        # buf.timestamp = timestamp
        # buf.duration = duration
        # buf.offset = offset
        # buf.offset_end = offset_end
        # buf.set_caps(gst.caps_from_string(caps))
        r = self.src.emit("push-buffer", buf)
        if r == GST_FLOW_OK:
            return r
        if self.queue_state != "error":
            log.error("Error pushing buffer: %s", r)
            self.update_state("error")
            self.emit('error', "push-buffer error: %s" % r)
        return 1


GObject.type_register(AudioSink)


def main() -> int:
    from xpra.platform import program_context
    with program_context("Audio-Record"):
        args = sys.argv
        log.enable_debug()
        import os.path
        if len(args) not in (2, 3):
            print("usage: %s [-v|--verbose] filename [codec]" % sys.argv[0])
            return 1
        filename = args[1]
        if not os.path.exists(filename):
            print("file %s does not exist" % filename)
            return 2
        decoders = get_decoders()
        if len(args) == 3:
            codec = args[2]
            if codec not in decoders:
                print("invalid codec: %s" % codec)
                print("only supported: %s" % str(decoders.keys()))
                return 2
            codecs = [codec]
        else:
            codec = None
            parts = filename.split(".")
            if len(parts) > 1:
                extension = parts[-1]
                if extension.lower() in decoders:
                    codec = extension.lower()
                    print("guessed codec %s from file extension %s" % (codec, extension))
            if codec is None:
                print("assuming this is an mp3 file...")
                codec = MP3
            codecs = [codec]

        log.enable_debug()
        with open(filename, "rb") as f:
            data = f.read()
        print("loaded %s bytes from %s" % (len(data), filename))
        # force no leak since we push all the data at once
        from xpra.audio import gstreamer_util
        gstreamer_util.QUEUE_LEAK = GST_QUEUE_NO_LEAK
        gstreamer_util.QUEUE_SILENT = True

        ss = AudioSink("", sink_options={}, codecs=codecs, codec_options={})

        def eos(*eos_args) -> None:
            print("eos%s" % (eos_args,))
            GLib.idle_add(glib_mainloop.quit)

        ss.connect("eos", eos)
        ss.start()

        glib_mainloop = GLib.MainLoop()

        import signal

        def deadly_signal(*_args) -> None:
            GLib.idle_add(ss.stop)
            GLib.idle_add(glib_mainloop.quit)

            def force_quit(_sig, _frame) -> NoReturn:
                sys.exit()

            signal.signal(signal.SIGINT, force_quit)
            signal.signal(signal.SIGTERM, force_quit)

        signal.signal(signal.SIGINT, deadly_signal)
        signal.signal(signal.SIGTERM, deadly_signal)

        def check_for_end(*_args) -> bool:
            qtime = ss.queue.get_property("current-level-time") // MS_TO_NS
            if qtime <= 0:
                log.info("underrun (end of stream)")
                start_thread(ss.stop, "stop", daemon=True)
                GLib.timeout_add(500, glib_mainloop.quit)
                return False
            return True

        GLib.timeout_add(1000, check_for_end)
        GLib.idle_add(ss.add_data, data)

        glib_mainloop.run()
        return 0


if __name__ == "__main__":
    sys.exit(main())
