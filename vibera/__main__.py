"""Default entry point: launch the floating chat overlay.

    python -m vibera

Requires:
  - macOS Apple Silicon (PyObjC overlay, Metal-backed whisper.cpp)
  - OpenRA running with ExternalControl listening (default port 7778)
  - GEMINI_API_KEY env var set
"""

from .floating_chat import main

if __name__ == "__main__":
    main()
