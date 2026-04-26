"""Centralised env-driven configuration for vibera.

All runtime knobs go through here so users tune via env / .env without
editing source. Read once at import time; modules import the constants.
"""

from __future__ import annotations

import os


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# -- OpenRA connection ----------------------------------------------------
OPENRA_HOST: str = os.environ.get("OPENRA_HOST", "127.0.0.1")
# vibera ships a patch (patches/openra-port.patch) that moves OpenRA's
# ExternalControl off the upstream default 7777 to 7778, so the default
# here matches the patched port. Override with OPENRA_PORT if you keep
# the upstream default or run multiple instances.
OPENRA_PORT: int = int(os.environ.get("OPENRA_PORT", "7778"))

# -- LLM (Gemini only for now) -------------------------------------------
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "") or os.environ.get(
    "GOOGLE_API_KEY", "")
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

# -- Voice / STT ---------------------------------------------------------
# whisper.cpp model size: tiny / base / small / medium / large-v3 etc.
# small is a good default on Apple Silicon (Metal-backed): ~470 MB on
# disk, sub-second transcription for short utterances. Bump to medium
# for more accuracy at the cost of latency.
VOICE_MODEL: str = os.environ.get("VIBERA_VOICE_MODEL", "small")
# Whisper language hint. "en" / "zh" / "auto" (auto-detect — less accurate).
VOICE_LANG: str = os.environ.get("VIBERA_VOICE_LANG", "en")
# Silence-rejection RMS threshold. Below this the clip is treated as
# silence and skipped to avoid burning whisper on dead mics. Bump up if
# you get false positives, bump down if quiet speech is being dropped.
VOICE_SILENCE_RMS: float = float(os.environ.get("VIBERA_VOICE_SILENCE_RMS",
                                                "0.0025"))

# -- Persistence ---------------------------------------------------------
import pathlib
STATE_DIR: pathlib.Path = pathlib.Path(
    os.environ.get("VIBERA_STATE_DIR",
                   str(pathlib.Path.home() / ".vibera")))

# -- Misc ----------------------------------------------------------------
DEBUG: bool = _bool("VIBERA_DEBUG", False)
