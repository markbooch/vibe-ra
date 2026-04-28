# Be the commander, not the clicker

> *Long-form companion piece to [vibera](https://github.com/markbooch/vibe-ra).
> Section 5 ("war stories") is intentionally left as an outline — those
> need to be written from lived experience, not reconstructed.*

---

## 1. Thesis

Real-time strategy games are gated by APM, not strategy. The skill that
separates a beginner from a pro is mostly mechanical: clicks per minute,
hotkey muscle memory, build-order rote. The *thinking* — when to
expand, where to attack, what to sacrifice — is a small fraction of the
operational load.

That made sense in 1996. The only available input bandwidth was a
keyboard and a mouse. The only available command interpreter was a UI
built around hotkeys. If you wanted to give the player more leverage,
you had to give them more buttons, and the players who learned to press
those buttons faster won. The genre's competitive ceiling — somewhere
around 250–300 effective actions per minute at the StarCraft II
professional level — is a measurement of how fast a human nervous
system can dispatch micro-decisions through ten fingers, nothing more.

That ceiling is no longer load-bearing. Local speech-to-text runs in
under a second on a laptop. LLMs convert natural language into
structured action plans well enough to drive a game loop. Modern game
engines, or at least the moddable ones, can be patched to accept those
plans on a socket. Put those three together and you get an RTS where
the player is a commander instead of an operator.

`vibera` is one working example. It is not a research project, not a
benchmark, not a competitive tool. It is a proof that the ingredients
for a different kind of RTS UX are now sitting on the shelf, and that a
single person can wire them together in a few weekends.

None of this is an argument against competitive RTS. Mechanical mastery
is a real and valid pursuit, and the people who chase 300 APM are doing
something genuinely impressive. The argument is for a parallel mode of
play — one that the current input pipeline simply cannot express.

## 2. What APM actually gates

Watch a Red Alert 1 opening at the level of someone who has played the
game for a year. In the first three minutes they will: place a power
plant, place a refinery, queue a barracks, queue a war factory, queue
two harvesters, train four infantry, scout one map edge, react to
whatever they see, place a second power plant, and start a second
refinery. That is roughly ten *decisions*. Maybe twenty if you count
the small reactive ones.

The same player will issue several hundred *clicks* to make those ten
decisions happen. Open the production tab. Click the building. Click
the placement spot. Rotate the placement. Switch tabs. Click the
infantry. Drag-select the infantry. Right-click the move target.
Re-select the harvester that just got distracted. Switch tabs again.
Each click is cheap individually, and the experienced player has them
fused into muscle memory, but the cumulative attentional cost is the
entire reason "macro" is treated as a separate skill from "strategy".

The decisions take about thirty seconds of thought. The clicks take
three minutes of execution. Most of what looks like "RTS skill" — and
most of the gap between a beginner and an expert — is the ability to
spread attention across that execution gap without losing the thread of
the higher-level plan.

The harder claim, and the one this project is built on: most strategic
mistakes happen because the player cannot think and execute
simultaneously. The plan that loses the game is rarely a *bad* plan. It
is the right plan, abandoned three minutes in because a harvester got
shot at and the player had to context-switch and never came back to it.
Free the execution, and the decisions get better. Not by a small
margin, either — the things humans are bad at in RTS are precisely the
things they would be good at if they had a competent staff officer
running the production queue and watching for emergencies.

Vibera is, in effect, that staff officer. You give the orders. It
clicks the buttons.

## 3. The technical recipe

The architecture is three loops glued together. Each loop is small
enough to read in one sitting; the interesting design choices are at
the seams.

**Voice → action.** Push-to-talk through `whisper.cpp` (the `small`
model, running on Metal). Pressing the mic key starts a 16 kHz capture;
releasing it triggers a transcribe. End-to-end latency is roughly
600–900 ms on an M-series laptop, which is fast enough that the player
forgets the model is local. The choice of local STT instead of an API
is not a preference, it is a constraint: anything that adds another
network round-trip on top of the LLM call breaks the feedback loop the
player needs. Privacy and cost are nice side effects.

**State → adviser.** This is the part that matters. A snapshot pump
opens one socket to OpenRA's `ExternalControl` trait and ticks at a few
Hz, diffing the previous snapshot against the current one and emitting
events: *power dropped below threshold*, *unit died*, *building
finished*, *enemy spotted*. Those events feed an LLM-backed adviser,
which proposes plans only when the event stream warrants it.

The naive design is to poll the LLM every few seconds with the full
game state. That works, and it costs about five thousand tokens a
minute, scaling linearly with game length and with state complexity.
The event-driven design idles at near-zero token cost and spikes only
when something interesting happens. Across a twenty-minute game the
difference is roughly an order of magnitude in cost and, more
importantly, an order of magnitude in *signal-to-noise* — the adviser
suggests power plants when power matters, not every fifth tick.

**Reactor pipeline.** A small layer of deterministic Python handlers
sits between the snapshot pump and the LLM. When a building finishes
construction, a reactor places it. When a harvester is idle next to a
refinery, a reactor sends it back to ore. These are sub-second,
zero-token responses to situations that do not need a language model.
Most "agent" projects skip this layer and waste the model's attention
on chores. They probably should not.

The three loops do not block each other. Voice runs in its own thread,
the pump in its own, the adviser in its own, and the reactors fire on
the pump's thread. The chat window marshals everything back to the
main Cocoa thread. None of this is novel, but the discipline of
keeping the LLM out of the hot path is what makes the system feel
responsive instead of laggy.

## 4. Picking the right RTS to start with

The shortlist is short. Three constraints have to be satisfied
simultaneously: the engine has to expose game state to a third-party
process, it has to accept commands from the same channel, and it has
to run on macOS in 2025.

That eliminates almost everything. StarCraft II's API is Windows-only
and Blizzard has been quiet about its future. AoE2:DE is Windows-only
in practice. SupCom is unmaintained on macOS. Beyond All Reason has
the moddability but the bot interface is built for headless training,
not for a sidecar agent.

OpenRA is currently the only mainstream-engine option that satisfies
all three. The `ExternalControl` trait was added by upstream a few
years ago specifically to enable this kind of integration; vibera
contributes a one-line port patch (so the trait listens on a port that
does not collide with common dev tools) and consumes the trait
otherwise unmodified. Red Alert 1 was the first mod to wire it up; the
same approach should work for Tiberian Dawn and Dune 2000 with no code
changes.

The pattern generalises beyond OpenRA. Any RTS that exposes its game
state and accepts external commands could host this kind of agent. The
prompts in `vibera/prompts/` are the only RA1-specific code in the project;
the snapshot pump, the event bus, the reactor layer, the validator,
and the daemon are all engine-agnostic. Modders for SC2, AoE2,
Warcraft 3, Beyond All Reason — there is nothing in vibera that you
could not lift wholesale into your engine of choice.

## 5. War stories

> *(Outline only. These need to be written from the actual debugging
> sessions, with the specific commit hashes, the specific stack traces,
> the specific moments of "and then I realised...". Reconstructing them
> from memory after the fact would lose the texture that makes them
> worth reading.)*
>
> Candidates worth writing up:
>
> - **The Metal context fight.** Whisper.cpp and OpenRA both wanted
>   the GPU. The crash, the prewarm fix, the lesson about predicting
>   concurrency bugs across two unrelated frameworks that share a
>   resource neither one knows about.
> - **Pruning the action vocabulary.** Started with a rich JSON schema
>   so the LLM could express anything. The LLM responded by emitting
>   invalid combinations roughly a third of the time. Cut it down to
>   twelve verbs and accuracy jumped to the high nineties. Lesson
>   about LLM action-space design: smaller is almost always better.
> - **The hallucinated unit IDs.** The adviser was confident, in
>   prose, about ordering units that did not exist. The validator
>   catches it now. Lesson about why "never invent IDs" in bold in the
>   prompt is not enough.
> - **The double-build bug.** The LLM kept queuing two buildings in a
>   single tick. The B1 hard rule in the prompt was the fix. Lesson
>   about encoding game *mechanics* into prompts, not just game
>   *vocabulary* — the model knows what a refinery is, it does not
>   know that you cannot place two foundations at once.

## 6. What vibera is not

Better to under-promise here and let the demo over-deliver than the
reverse.

It is not a micro replacement. A skilled human still outmicros vibera
in any fight that comes down to individual unit movement. The agent is
good at intent, mediocre at execution at the unit level, and not even
trying to be good at things like dancing tanks in and out of range.

It is not a competitive tool. It will not beat the strong OpenRA AI on
a hard map today. That is a fixable problem — most of the gap is
opening sophistication and economy management, both of which are
prompt-engineering rather than architecture changes — but it is not
fixed yet, and pretending otherwise would waste your time.

It is not a general game-playing agent. The prompts are written for
RA1 specifically. The architecture would carry to other RTS games, the
prompts would not.

It is not robust. macOS only. Apple Silicon only. Alpha. The install
path requires a Gemini API key, a working OpenRA build, a microphone
permission grant, and a tolerance for things that crash occasionally.

It is not a research paper. There are no benchmarks here, no
ablations, no published comparisons against baselines. It is a working
proof of concept built in the open. If somebody wants to write the
paper, the code is sitting right there.

## 7. What's interesting about it anyway

A new kind of RTS UX is now buildable by one person. What used to
require a Blizzard-scale engineering team — voice control, an
intelligent staff officer, plain-language orders — can be prototyped in
a few weekends because three commodity ingredients (local STT, an LLM
that emits structured JSON, a moddable engine with an external-control
trait) finally exist together. None of those ingredients are new on
their own. Their intersection is.

The agent pattern is reusable. Anything with a real-time state stream
and a finite action vocabulary — sim games, factory builders, browser
automation, industrial control demos, robot teleoperation interfaces —
can use the same event-driven structure with bounded token cost and a
deterministic reactor layer for the boring stuff. Vibera is small
enough that the pattern is legible; it is not buried under framework
code.

Accessibility is real. RTS is one of the most APM-hostile genres for
players with motor impairments, repetitive-strain injuries, or one
working hand. Voice-as-input changes the eligibility list. This is not
the headline reason to care about the project, but it is a side effect
worth noting.

And the genre might just be due. RTS peaked commercially around 2002
and has been slowly declining ever since. Every few years someone
asks "why don't they make RTS games anymore?" and the answers focus on
publisher economics or competition from MOBAs. Maybe it was simpler
than that. Maybe APM was the bottleneck the whole time, and the
ceiling kept new players out faster than veterans could be retained.
That is testable now.

## 8. Try it / read the code / yell at me

The repo is at <https://github.com/markbooch/vibe-ra>. macOS Apple
Silicon only for the moment. The README walks through install; the
short version is: clone, install Python deps, drop your Gemini key
into `.env`, build the patched OpenRA fork, launch a skirmish, talk to
the floating window.

Issues, prompt improvements, and engine ports are all welcome. The
adviser prompt in particular is a place where small changes produce
visibly different play, and watching what other people's tweaks
produce is most of the fun of working on this.

One question to leave you with: *what would you build if your RTS
understood plain English?*

---

## Notes for the author (delete before publishing)

- **Length target:** 1500–2500 words. Currently around 2200 with
  Section 5 still as outline. Filling in three war stories adds
  ~1000 words; cut elsewhere if needed.
- **Tone:** technical, plain, slightly opinionated. No hype words.
  No "revolutionary", no "game-changing", no exclamation marks.
- **Title for HN:** *"Show HN: Be the commander, not the clicker —
  voice + LLM RTS on Red Alert 1"*. Keep it under 80 chars.
- **Body for HN submission:** 200 words, see the README intro for the
  template. Don't paste the full essay into the HN body; link to it.
- **Best post day:** Tuesday or Wednesday, 6:30–8:00 AM Pacific.
- **First-hour rule:** be at the keyboard for 4–6h after posting,
  reply to every comment within 10 minutes.
- **Pre-empt the top 5 critical questions:** all answered in the
  README's FAQ section. If a commenter raises one, link them there.
- **Cross-post timing:** r/openra + r/LocalLLaMA 24h after HN, with a
  link back to the HN thread as a secondary comment.
