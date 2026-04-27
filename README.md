# vibera

### Be the commander, not the clicker.

![platform: macOS Apple Silicon](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-blue)
![license: MIT](https://img.shields.io/badge/license-MIT-green)
![status: alpha](https://img.shields.io/badge/status-alpha-orange)

Real-time strategy games are gated by APM, not strategy. The skill that
separates a beginner from a pro is mostly mechanical: clicks per minute,
hotkey muscle memory, build-order rote. The *thinking* — when to expand,
where to attack, what to sacrifice — is a small fraction of the
operational load.

`vibera` removes the operational floor. You speak commands in plain
language; a local Whisper model transcribes; an LLM translates intent
into structured game actions; an event-driven AI staff officer watches
the battlefield and proposes plans. You decide. The agent operates.

Today it works against [OpenRA Red Alert 1](https://www.openra.net/).
The pattern generalises to any RTS that exposes its game state.

> **Status:** alpha. The voice → action loop works end-to-end. The
> adviser proposes plans for opening, economy, power, and combat
> events. Rough edges: see *Status* below. **macOS Apple Silicon only**
> for now.

## What it looks like

> *(Screenshot placeholder — see `docs/` for a longer write-up of the
> design.)*

You say: *"Build five rifle infantry and send them to scout the
northwest ridge."* The window transcribes, Gemini emits a JSON action
plan, OpenRA queues the units and walks them out. Power runs low? The
adviser pings you mid-game with a one-click suggestion to build another
power plant.

## Why this matters beyond a single game

- **Accessibility.** RTS is one of the most APM-hostile genres for
  players with motor impairments, repetitive-strain injuries, or single
  hands. Voice-as-input changes the eligibility list.
- **Genre design.** If "operating" the game can be delegated, RTS
  becomes a thinking-and-talking game like a tabletop war-game with a
  clock. That's a different — and arguably more interesting — design
  space than the current APM arms race.
- **Agent architecture.** vibera is a small, working example of an
  event-driven LLM agent with bounded token cost (the staff officer is
  triggered by game events, not on a polling loop). The pattern is
  reusable for anything with a real-time state stream and a finite
  action vocabulary.

## Architecture

```
mic ──► whisper.cpp ──► raw text ──┐
                                   ▼
                      Gemini  ──► JSON action plan ──► OpenRA ExternalControl (TCP 7778)
                                   ▲
       game-state pump ◄──── snapshot socket ◄──── OpenRA
                                   │
                                   ▼
                      Adviser (Gemini, event-driven)
                                   │
                                   ▼
                      floating chat UI (PyObjC)
```

Three independent loops:

1. **Voice → action.** Push-to-talk. Whisper transcribes locally;
   Gemini turns the text into a structured action plan; the OpenRA
   client dispatches. Sub-second on Apple Silicon for a short
   utterance.
2. **State → adviser.** A snapshot pump streams game state. An event
   bus emits high-level events (`UnderAttack`, `EnemySpotted`,
   `PowerLow`, `EconomyIdle`, `OpeningComplete`). The adviser is
   triggered by those events, not on a timer — token cost stays flat
   instead of scaling with game length.
3. **Reactor pipeline.** A daemon runs reactor scripts (zero-token,
   sub-second) for things that don't need an LLM, e.g. auto-placing a
   building once the queue says it's ready.

## Status

What works:

- Voice → transcription → LLM → OpenRA action loop, end-to-end
- Adviser proposes plans for opening (deploy MCV), economy (build
  refinery), power (build power plant), and combat events
- Daemon executes multi-step task plans with predicate-driven waits
- Reactor pipeline auto-places finished buildings
- Floating always-on-top chat window with text and voice input

Known issues:

- Floating chat occasionally fails to forward the transcribed text from
  the voice button into the input box (UI bridge issue, tracked)
- OpenRA's macOS Metal renderer occasionally crashes — unrelated to
  vibera, restart the game
- The adviser sometimes hallucinates unit IDs when game state is sparse;
  validator catches most but not all
- No Windows / Linux support and none planned (PyObjC + Metal-Whisper
  is the macOS-first reason)

## Requirements

- macOS 14+ on Apple Silicon. Intel and Linux are out of scope.
- Python 3.11+ (3.13 OK).
- A Gemini API key. Set `GEMINI_API_KEY`.
- A working OpenRA build (see *Setup OpenRA* below).
- A microphone, plus a launcher (terminal / IDE / shell wrapper) whose
  app bundle declares `NSMicrophoneUsageDescription` in `Info.plist`.
  Stock `Terminal.app` does **not** declare it and macOS will silently
  kill `vibera` at the first audio call. Most modern third-party
  terminals and IDEs do declare it — see *Microphone permission* below.

## Install

```bash
git clone https://github.com/markbooch/vibe-ra.git
cd vibe-ra

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — at minimum set GEMINI_API_KEY=...
```

## Setup OpenRA

`vibera` does not bundle OpenRA. You build OpenRA yourself and apply a
small patch that moves the `ExternalControl` trait off the upstream
default port `7777` to `7778`, which is what `vibera` defaults to. (If
you prefer the upstream port, skip the patch and set `OPENRA_PORT=7777`
in your `.env`.)

```bash
# pick any directory outside this repo
git clone https://github.com/OpenRA/OpenRA.git
cd OpenRA
git apply /path/to/vibe-ra/patches/openra-port.patch

# follow upstream OpenRA build instructions, then launch the RA mod
make
mono OpenRA.dll Game.Mod=ra
```

Or run the helper:

```bash
./setup.sh /path/to/OpenRA/checkout
```

## Run

```bash
# from a launcher with microphone permission (see note in Requirements)
source .venv/bin/activate
python -m vibera
```

The floating chat window appears. Click the mic button to record, click
again to stop and transcribe; the text is sent to Gemini, which emits a
structured action plan to OpenRA. Type in the box for text commands.

## Configuration

All knobs are env vars (see `.env.example`):

| var                         | default                | meaning                                    |
| --------------------------- | ---------------------- | ------------------------------------------ |
| `GEMINI_API_KEY`            | (required)             | Gemini API key                              |
| `GEMINI_MODEL`              | `gemini-2.5-flash-lite`| LLM for translator + adviser                |
| `OPENRA_HOST`               | `127.0.0.1`            | OpenRA ExternalControl host                 |
| `OPENRA_PORT`               | `7778`                 | OpenRA ExternalControl port                 |
| `VIBERA_VOICE_MODEL`        | `small`                | whisper.cpp model size                      |
| `VIBERA_VOICE_LANG`         | `en`                   | spoken language; `auto` to autodetect       |
| `VIBERA_VOICE_SILENCE_RMS`  | `0.0025`               | silence-rejection RMS threshold             |
| `VIBERA_STATE_DIR`          | `~/.vibera`            | task history, etc.                          |
| `VIBERA_DEBUG`              | `0`                    | verbose logs                                |

## Microphone permission

macOS gates microphone access per launching app bundle. If `vibera` is
started from a binary whose `Info.plist` lacks
`NSMicrophoneUsageDescription`, the OS kills the process at the first
`AudioUnitInitialize` call with no permission prompt. Stock
`Terminal.app` is in this category. Most third-party terminals and IDEs
declare the key correctly; if yours does not, run `vibera` from one
that does.

To reset permission for a specific bundle id (find it via
`osascript -e 'id of app "<App Name>"'`):

```bash
tccutil reset Microphone <bundle-id>
```

Never run `tccutil reset Microphone` with no argument — it wipes every
app's microphone permission.

## FAQ

**Isn't APM the point of RTS?** For competitive StarCraft, sure.
vibera doesn't replace that scene; it opens RTS to the much larger
audience that bounced off the genre because mechanical execution
dwarfed strategic thinking. Chess survived computer engines by
becoming a *thinking* game played at slower time controls. RTS can do
the same.

**If the LLM does the operations, isn't it just turn-based?** No. The
*commander's* decisions are still real-time and consequential — what
to attack, when to expand, when to retreat, when to commit reserves.
Voice removes click-execution, not decision pressure. The LLM is staff,
not autopilot.

**Why Gemini, not a local LLM?** A working tactical commander needs
~1–3s end-to-end latency, JSON-strict output, and ~30–60k tokens of
context for the system prompt + game state. Local models that hit all
three on consumer Apple Silicon hardware are still rare in early 2025;
Gemini Flash is fast, cheap, and reliably structured. Swapping to a
local model is a future option (the `LLMTranslator` interface is
deliberately small).

**Why Red Alert 1, not StarCraft 2 / AoE / Warcraft?** OpenRA exposes
an `ExternalControl` trait that vibera can patch and talk to. SC2
doesn't expose live game state to third parties on macOS. RA1 via
OpenRA is currently the *only* serious RTS where this is buildable on
macOS without reverse-engineering.

**Does this just become an aimbot for RTS?** vibera doesn't do micro
better than a human — it does macro at human-comprehensible speed.
There's no per-unit AI; the LLM operates on whole groups via abstract
verbs (`attack_move`, `retreat`, `produce`). A skilled human still
out-microes vibera; the point is to free the human's attention for
*strategic* decisions instead of click-execution.

**Will it work on Windows / Linux?** Not currently planned. The mic
capture, floating chat window, and Whisper Metal acceleration are all
macOS-specific. PRs welcome but I won't drive it.

## License

MIT — see `LICENSE`.

`patches/openra-port.patch` is a derivative of OpenRA and is therefore
GPLv3 (header in the patch). vibera itself only talks to OpenRA over a
socket, which is mere aggregation.

See `NOTICE` for full third-party attribution.
