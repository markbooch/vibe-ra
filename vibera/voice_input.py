"""
Voice input — record from default mic, transcribe with whisper.cpp,
return alias-rewritten text suitable for translate_to_plan().

Why
---
The whole project is "voice-controlled RA1". This module wires mic →
whisper.cpp (Metal-backed on Apple Silicon) → text, so floating_chat's
🎙 button can replace typing.

Design
------
* **One transcription engine instance, lazily loaded.** First record()
  call downloads (if needed) and warms the configured whisper model
  (~470 MB for `small`). Subsequent calls reuse the loaded model.

* **Push-to-record API**, not push-to-talk:
      vi = VoiceInput()
      vi.start()      # opens mic stream, accumulates frames
      ...
      text = vi.stop_and_transcribe()   # blocks until whisper done

  The UI button is a toggle: first click → start, second click → stop +
  transcribe. Keeps Cocoa happy (no nested run-loop weirdness).

* **Initial-prompt biasing + alias replacement** — INITIAL_PROMPT
  steers whisper toward RA1 vocabulary, ALIASES post-rewrites cases the
  model still gets wrong (e.g. "heavy tank" → "3tnk"). Both are easy to
  extend without touching transcription code.

* **Language is configurable.** Default `en`; set
  `VIBERA_VOICE_LANG=zh` (or another whisper language code) to switch.

* **No threading model imposed.** start()/stop() are thread-safe via an
  RLock; transcribe runs synchronously on the calling thread (caller
  wraps in a worker thread if needed).

Cost
----
Zero. Fully offline. ~150–500 ms wall time for a 3-second utterance on
a current-gen Apple Silicon machine using `small` + Metal.

Smoke
-----
    python3 -m vibera.voice_input    # speak for 4s, prints transcribed text
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .config import VOICE_LANG, VOICE_MODEL, VOICE_SILENCE_RMS

log = logging.getLogger("vibera.voice_input")

# Whisper model size. Override via VIBERA_VOICE_MODEL env.
#   tiny     —  39 MB  ~30ms   poor accuracy, OK for hotword-style
#   base     —  74 MB  ~60ms
#   small    — 466 MB  ~200ms  recommended sweet spot on M-series
#   medium   — 1.5 GB  ~600ms  noticeably better, still real-time
#   large-v3 — 3.0 GB  ~2s     best accuracy, too slow for live UX
WHISPER_MODEL = VOICE_MODEL

# Sample rate Whisper expects. Anything else gets resampled internally
# but giving it the native rate avoids a CPU pass.
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "float32"

# Initial prompt — biases the model toward RA1 vocabulary. Whisper
# clamps this to last 224 tokens, so put the most important codes near
# the END (they're more likely to be retained).
INITIAL_PROMPT = (
    "Voice command for a real-time strategy game (Red Alert). "
    "Common verbs: build, train, move, attack, defend, retreat, scout, "
    "rally, sell, repair, push, hold. "
    "Map directions: NW, NE, SW, SE, north, south, east, west, center. "
    "Buildings: fact (construction yard), powr (power plant), "
    "apwr (advanced power), proc (refinery), barr (Soviet barracks), "
    "tent (Allied barracks), weap (war factory), dome (radar dome), "
    "stek (Soviet tech center), atek (Allied tech center), "
    "iron (iron curtain), tsla (tesla coil), ftur (flame tower), "
    "pbox (pillbox), agun (AA gun), sam (SAM site). "
    "Units: mcv, harv (harvester), e1 (rifle infantry), e3 (rocket "
    "soldier), e6 (engineer), 1tnk (light tank), 2tnk (medium tank), "
    "3tnk (heavy tank), 4tnk (mammoth), v2rl (V2 rocket), arty "
    "(artillery), apc, jeep, ftrk (mobile flak), dog."
)

# Hard alias map — applied after Whisper transcription. Whisper sometimes
# transcribes RA1 codes phonetically or as the long English form; these
# rewrites normalise to the short codes the LLM planner expects.
# Order matters: longer keys are tried first so "heavy tank" beats "tank".
# Match is case-insensitive.
ALIASES: dict[str, str] = {
    # tanks
    "heavy tank": "3tnk", "heavy tanks": "3tnk",
    "medium tank": "2tnk", "medium tanks": "2tnk",
    "light tank": "1tnk", "light tanks": "1tnk",
    "mammoth tank": "4tnk", "mammoth tanks": "4tnk", "mammoth": "4tnk",
    # vehicles
    "v2 rocket": "v2rl", "v2 launcher": "v2rl", "v2": "v2rl",
    "artillery": "arty",
    "harvester": "harv", "ore truck": "harv", "miner": "harv",
    "construction vehicle": "mcv", "mcv": "mcv",
    "mobile flak": "ftrk", "flak truck": "ftrk",
    # infantry
    "rifle infantry": "e1", "rifleman": "e1", "riflemen": "e1",
    "rocket soldier": "e3", "rocket infantry": "e3", "rocketeer": "e3",
    "engineer": "e6", "engineers": "e6",
    "grenadier": "e2", "grenadiers": "e2",
    "flamethrower": "e4", "flame infantry": "e4",
    # buildings — long forms whisper prefers
    "soviet tech center": "stek", "soviet tech": "stek",
    "allied tech center": "atek", "allied tech": "atek",
    "radar dome": "dome", "radar": "dome",
    "power plant": "powr", "powerplant": "powr",
    "advanced power plant": "apwr", "advanced power": "apwr",
    "ore refinery": "proc", "refinery": "proc",
    "soviet barracks": "barr", "barracks": "barr",
    "allied barracks": "tent",
    "war factory": "weap",
    "iron curtain": "iron",
    "tesla coil": "tsla", "tesla": "tsla",
    "flame tower": "ftur",
    "pillbox": "pbox", "machine gun nest": "pbox",
    "aa gun": "agun", "anti air gun": "agun", "flak cannon": "agun",
    "sam site": "sam", "sam launcher": "sam",
    # quadrants — model often outputs words
    "north west": "NW", "northwest": "NW",
    "north east": "NE", "northeast": "NE",
    "south west": "SW", "southwest": "SW",
    "south east": "SE", "southeast": "SE",
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_model_lock = threading.Lock()
_model = None  # type: ignore[assignment]

# macOS mic permission state — requested at most once per process.
_mic_permission_requested = False
_mic_permission_lock = threading.Lock()


def _ensure_mic_permission() -> bool:
    """On macOS, sounddevice / PortAudio will silently return zero-filled
    audio if the process lacks Microphone TCC authorization — no error,
    no popup. We have to explicitly call AVCaptureDevice's request API
    to trigger the system permission dialog. Idempotent.

    Returns True if authorized (or non-Mac), False otherwise."""
    global _mic_permission_requested
    if sys.platform != "darwin":
        return True
    with _mic_permission_lock:
        try:
            import AVFoundation  # type: ignore
        except ImportError:
            log.warning("VoiceInput: pyobjc-framework-AVFoundation not installed; "
                        "cannot pre-request mic permission. Install with: "
                        "pip install pyobjc-framework-AVFoundation")
            return True   # let sounddevice try; may still work if granted
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio)
        # 0=NotDetermined 1=Restricted 2=Denied 3=Authorized
        if status == 3:
            return True
        if status in (1, 2):
            log.error("VoiceInput: mic permission denied/restricted (status=%d). "
                      "Open System Settings → Privacy & Security → Microphone "
                      "and enable 'Python'. Then restart floating_chat.", status)
            return False
        if _mic_permission_requested:
            return False
        _mic_permission_requested = True

        result = {"granted": None}
        ev = threading.Event()

        def _cb(granted):
            result["granted"] = bool(granted)
            ev.set()

        log.info("VoiceInput: requesting mic permission — system popup will appear")
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, _cb)
        # Wait up to 60s for the user to click the dialog. We do NOT block
        # forever — better to fail loud.
        if not ev.wait(timeout=60.0):
            log.error("VoiceInput: timed out waiting for mic permission dialog")
            return False
        granted = result["granted"]
        log.info("VoiceInput: mic permission %s", "granted" if granted else "denied")
        return bool(granted)


def _ensure_model():
    """Lazy-load the whisper model. First call downloads ~470 MB."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from pywhispercpp.model import Model
        log.info("VoiceInput: loading whisper model %r (first call may "
                 "download)...", WHISPER_MODEL)
        t0 = time.time()
        _model = Model(
            model=WHISPER_MODEL,
            print_realtime=False,
            print_progress=False,
            print_timestamps=False,
        )
        log.info("VoiceInput: model loaded in %.2fs", time.time() - t0)
        return _model


def _apply_aliases(text: str) -> str:
    """Hard-rewrite known RA1 vocabulary mismatches.

    Whole-word, case-insensitive replacement so "Heavy tank" / "HEAVY TANK"
    both map. Longer keys tried first so "heavy tank" beats "tank".
    """
    import re
    out = text
    for key in sorted(ALIASES, key=len, reverse=True):
        # \b doesn't behave on CJK; for plain ASCII keys word-bound is safe.
        if all(c.isascii() and (c.isalnum() or c == ' ') for c in key):
            pat = re.compile(r"\b" + re.escape(key) + r"\b", re.IGNORECASE)
            out = pat.sub(ALIASES[key], out)
        else:
            # Non-ASCII alias (kept for forward-compat / user customisation).
            if key in out:
                out = out.replace(key, ALIASES[key])
    return out


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class VoiceInput:
    """Record from mic, transcribe on demand. Thread-safe."""

    def __init__(self,
                 sample_rate: int = SAMPLE_RATE,
                 max_seconds: float = 30.0):
        self.sample_rate = sample_rate
        self.max_seconds = max_seconds
        self._lock = threading.RLock()
        self._stream = None
        self._frames: list[np.ndarray] = []
        self._started_at: Optional[float] = None
        self._recording = False

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    def start(self) -> None:
        """Open mic and begin accumulating frames. Idempotent."""
        with self._lock:
            if self._recording:
                log.warning("VoiceInput.start: already recording")
                return
            import sounddevice as sd
            self._frames = []
            self._started_at = time.time()
            self._recording = True

            def _cb(indata, frames, t_info, status):
                if status:
                    log.debug("sounddevice status: %s", status)
                # Copy — sounddevice reuses the buffer.
                self._frames.append(indata.copy())
                # Hard cap so a stuck record doesn't OOM us.
                if (time.time() - (self._started_at or 0)) > self.max_seconds:
                    log.warning("VoiceInput: hit max_seconds cap (%.1fs)",
                                self.max_seconds)
                    # Stop the stream from inside the callback safely:
                    # raise CallbackStop is the documented way.
                    raise sd.CallbackStop

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=_cb,
            )
            self._stream.start()
            log.info("VoiceInput: recording started")

    def stop_and_transcribe(self) -> str:
        """Close mic, run whisper, return alias-rewritten text. ''
        on no-audio / failure (caller decides what to do)."""
        with self._lock:
            if not self._recording:
                log.warning("VoiceInput.stop: not recording")
                return ""
            self._recording = False
            stream = self._stream
            frames = self._frames
            started = self._started_at or time.time()
            self._stream = None
            self._frames = []
            self._started_at = None

        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception:
            log.exception("VoiceInput: stream close failed")

        elapsed = time.time() - started
        if not frames:
            log.warning("VoiceInput: no audio frames captured")
            return ""

        audio = np.concatenate(frames, axis=0).flatten().astype("float32")
        # Drop the noisy first 80 ms — that's about how long it takes the
        # mic AGC to settle on macOS, and it kills whisper on quiet starts.
        skip = int(self.sample_rate * 0.08)
        if audio.shape[0] > skip * 2:
            audio = audio[skip:]

        if audio.shape[0] < self.sample_rate * 0.3:   # < 300 ms
            log.warning("VoiceInput: clip too short (%.2fs) — skipping",
                        audio.shape[0] / self.sample_rate)
            return ""

        # Crude silence detector — avoids burning whisper on dead mics.
        # Threshold chosen empirically: ambient noise floor on a typical built-in
        # mic is ~0.001-0.004; quiet speech ~0.01+. The default 0.0025 catches
        # near-silence without rejecting soft commands. Override with
        # VIBERA_VOICE_SILENCE_RMS env. If you trip this often, raise the
        # macOS "System Settings > Sound > Input volume" first before lowering.
        rms = float(np.sqrt(np.mean(audio * audio)))
        if rms < VOICE_SILENCE_RMS:
            log.warning("VoiceInput: clip looks silent (rms=%.4f, threshold=%.4f) "
                        "— skipping (try raising mic input volume)",
                        rms, VOICE_SILENCE_RMS)
            return ""
        log.info("VoiceInput: clip rms=%.4f, len=%.2fs — transcribing",
                 rms, audio.shape[0] / self.sample_rate)

        log.info("VoiceInput: ensuring model...")
        try:
            model = _ensure_model()
        except Exception:
            log.exception("VoiceInput: model load failed")
            return ""
        log.info("VoiceInput: calling whisper.transcribe (audio=%d samples)",
                 audio.shape[0])
        t0 = time.time()
        try:
            segments = model.transcribe(
                audio,
                language=VOICE_LANG,
                initial_prompt=INITIAL_PROMPT,
                # temperature kept default; whisper.cpp falls back through
                # 0.0..1.0 on its own when confidence is low.
            )
        except Exception:
            log.exception("VoiceInput: whisper transcribe failed")
            return ""
        latency = time.time() - t0
        log.info("VoiceInput: whisper.transcribe returned in %.2fs", latency)

        raw = "".join(seg.text for seg in segments).strip()
        rewritten = _apply_aliases(raw)
        audio_secs = audio.shape[0] / self.sample_rate
        log.info("VoiceInput: transcribed %.2fs audio in %.2fs -> %r%s",
                 audio_secs, latency, rewritten,
                 f"  (raw={raw!r})" if rewritten != raw else "")
        return rewritten


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    print("Speak for 4 seconds (try: 'send three heavy tanks to the "
          "north west corner')...")
    vi = VoiceInput()
    vi.start()
    time.sleep(4.0)
    text = vi.stop_and_transcribe()
    print(f"\n→ {text!r}")
