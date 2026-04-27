# Limitations

This is alpha software. Read this before you file an issue.

## Platform

- **macOS Apple Silicon only.** No Intel Mac, no Linux, no Windows. The
  voice input layer uses `whisper.cpp` Metal acceleration and the chat
  UI is PyObjC/Cocoa. A Linux/X11 port is conceivable; nobody is
  working on it. PRs welcome — see `CONTRIBUTING.md`.
- **Tested on macOS 14 and 15.** Older versions probably work; nobody
  has checked.

## Dependencies you have to bring yourself

- **A Gemini API key.** The task translator and adviser both call
  Gemini. There is no local LLM fallback today. Estimated cost during
  active play is well under one cent per game with `gemini-2.5-flash`,
  but you do need a key.
- **A working OpenRA build with the port patch applied.** The
  `setup.sh` script runs the patch idempotently, but you still need
  .NET 8 and the OpenRA build prerequisites. The OpenRA project's own
  install docs are the source of truth here.
- **.NET 8 specifically.** OpenRA's current release does not build on
  .NET 9 or 10. On macOS:
  `brew install dotnet@8` and add it to your `PATH`.
- **Microphone permission for whichever terminal launches vibera.** If
  voice input silently does nothing, this is almost always why.

## Known runtime issues

- **First snapshot can race.** If you fire a command in the first
  second after launch, the snapshot pump may not yet have a state to
  pass to the LLM. The chat surfaces an error and you can retry.
- **Adviser proposals lag the event by 1–3 seconds.** This is the LLM
  round-trip, not a bug. Reactor-driven responses (auto-place, harvester
  rebind) are sub-second.
- **Whisper occasionally mistranscribes unit names.** The vocabulary
  hint helps, but "rifle infantry" and "rocket infantry" are close
  enough phonetically that a low-volume mic capture can confuse them.
  Edit the chat input before sending if you see a wrong transcript.

## What does not work yet

- **No persistent commander memory across games.** The adviser starts
  fresh every match. Cross-game learning is out of scope for now.
- **No multi-player coordination.** Vibera assumes you are the only
  human in the lobby. Two-vibera matches probably work; nobody has
  tried.
- **No replay analysis.** Snapshots are not logged to disk by default.
- **No benchmarks.** There is no published win rate against the OpenRA
  AI, no comparison against human play, no ablation of the adviser.
  Adding them is welcome work; nobody has done it.

## What is not on the roadmap

- A Discord/voice-chat front end. The push-to-talk model assumes you
  are at the keyboard.
- A web/Electron version. The Cocoa front end is not portable; a port
  would mean rewriting `floating_chat.py`.
- Support for non-RTS games. The architecture would carry, the prompts
  would not.
- Beating the strong OpenRA AI. The agent is built for a player to
  *use*, not to be one.

## How to file a useful issue

1. Confirm you are on macOS Apple Silicon and `python3 -m vibera`
   launches without an exception.
2. Include the full traceback, not a screenshot of a screenshot.
3. If it involves OpenRA: include the `OpenRA.log` excerpt from the
   relevant time window.
4. If it involves voice: include what you said, what was transcribed,
   and which model size you have downloaded.
5. If it involves the LLM: include the offending JSON action plan if
   you can capture it (`vibera/.vibera/` may have logs).
