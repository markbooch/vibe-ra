# vibera

Voice-controlled AI staff officer for **OpenRA Red Alert 1**.

![platform: macOS Apple Silicon](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-blue)
![license: MIT](https://img.shields.io/badge/license-MIT-green)

`vibera` lets you play RA1 by talking. A floating chat window listens to
your microphone, transcribes locally with `whisper.cpp`, sends the text
through Gemini, and emits structured commands to OpenRA's
`ExternalControl` socket. An event-driven AI adviser also watches the
game state and proposes tactical / economic plans.

> **Status:** alpha. Expect rough edges. **macOS Apple Silicon only.**

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

## Requirements

- macOS 14+ on Apple Silicon (M1/M2/M3/M4). Intel and Linux are out of scope.
- Python 3.11+ (3.13 OK).
- A Gemini API key (free tier is fine for a single player). Set `GEMINI_API_KEY`.
- A working OpenRA build (see *Setup OpenRA* below).
- A microphone, plus a launcher (terminal / IDE / shell wrapper) whose
  app bundle declares `NSMicrophoneUsageDescription` in `Info.plist`.
  Stock `Terminal.app` does **not** declare it and macOS will silently
  kill `vibera` at the first audio call. Most modern third-party
  terminals and IDEs do declare it — see *Microphone permission* below.

## Install

```bash
git clone <your-fork-of-this-repo> vibe-ra
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

The floating chat window appears. Click 🎙 to record, click again to
stop and transcribe; the text is sent to Gemini, which emits a structured
action plan to OpenRA. Type freely in the box for text commands.

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

## License

MIT — see `LICENSE`.

`patches/openra-port.patch` is a derivative of OpenRA and is therefore
GPLv3 (header in the patch). vibera itself only talks to OpenRA over a
socket, which is mere aggregation.

See `NOTICE` for full third-party attribution.
