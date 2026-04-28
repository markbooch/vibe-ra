# Contributing

Thanks for looking. This is a small project; PRs and issues are both
welcome, no formal CLA, MIT-licensed.

## Quick start (developer install)

```bash
git clone https://github.com/markbooch/vibe-ra.git
cd vibe-ra
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then add your GEMINI_API_KEY
```

You also need a working OpenRA build with the port patch applied — see
the README for that side of the setup.

Run the app:

```bash
python -m vibera
```

## Project layout

```
vibera/                Python package (entry: __main__.py)
  floating_chat.py     PyObjC Cocoa chat window (UI + voice button)
  voice_input.py       Push-to-talk + whisper.cpp wrapper
  daemon.py            Task lifecycle + step executor
  commander.py         LLM task-translator (utterance → action plan)
  adviser.py           Event-driven LLM staff officer
  reactors.py          Deterministic auto-responses (zero-token)
  snapshot_pump.py     Polls OpenRA, diffs state, emits events
  openra_client.py     TCP client for the ExternalControl trait
  prompts/             System prompts for the LLMs (English)
  ...
patches/               OpenRA patches we depend on
docs/                  manifesto, screenshots
```

## What kinds of PR are welcome

- **Bug fixes** with a reproducer in the issue or PR description.
- **Prompt improvements** — particularly to `vibera/prompts/adviser_prompt.md`
  and `vibera/prompts/system_prompt.md`. Changes here produce visible
  behaviour changes; please describe what game situations got better.
- **Linux or Windows port.** The Cocoa-bound bits live in
  `floating_chat.py` and `voice_input.py`. Everything else is portable.
  A clean abstraction layer for the front end would be a big PR but a
  high-value one.
- **Other RTS engines.** The snapshot/reactor/daemon stack is engine
  agnostic. If you wire up another engine (BAR, OpenSAGE, OpenAge),
  open a draft PR early so we can think about the right seam.
- **Tests.** There are essentially none. A pytest harness for the
  prompt translator (golden-file testing of utterance → plan against a
  fixed snapshot) would be welcome.

## What kinds of PR are likely to be declined

- "Use framework X instead of plain Python." The lack of framework is
  a feature; the project should stay readable in one sitting.
- "Add a local-LLM backend." Not opposed in principle, but local LLMs
  that emit reliable structured JSON for action plans are still
  fragile. Show working benchmarks first.
- "Rewrite in Rust/Go/Swift." Not on the table.

## Style

- **Python:** roughly PEP 8. There is no enforced linter today; if you
  want to add `ruff` config, that is itself a welcome PR.
- **Comments:** prefer "why" over "what". The code is short enough to
  speak for itself.
- **No emojis in code.** A few are in the UI (mic icon, status dot)
  and that's the limit.

## Filing issues

A good bug report has:

1. macOS version + chip (`uname -m` and About This Mac).
2. Python version (`python3 --version`).
3. Output of `pip list | grep -E 'vibera|whisper|google-genai|pyobjc'`.
4. Full traceback (text, not screenshot).
5. What you were doing, what you expected, what happened instead.
6. If the problem touches OpenRA: relevant `OpenRA.log` lines.
7. If the problem touches voice: the model size you have downloaded
   (`small`, `base`, `tiny`) and roughly what you said.

## Communication

GitHub issues for bugs and feature discussion. There is no Discord or
chat server; I want the discussion to stay searchable.

## License

By contributing, you agree your contribution is licensed under the
project's [MIT License](LICENSE).
