# Be the commander, not the clicker

> *Working title for a long-form write-up. Outline below; fill in the
> war stories, expand the prose, add code snippets, then cross-post to
> HN / r/openra / r/LocalLLaMA. This file is intentionally not yet a
> finished essay — it's the skeleton.*

---

## 1. Thesis

Real-time strategy games are gated by APM, not strategy. The skill
that separates a beginner from a pro is mostly mechanical. The
*thinking* — when to expand, where to attack, what to sacrifice — is
a small fraction of the operational load.

That made sense when the only available "input bandwidth" was a
keyboard and a mouse, and the only available "command interpreter" was
a UI built around hotkeys. That's no longer true. Local STT runs in
under a second. LLMs can convert natural language to structured action
plans. Game engines can be patched to accept those plans on a socket.

Put those three together and you get an RTS where the player is a
commander instead of an operator. Vibera is one working example.

> *(write 2–3 paragraphs expanding this. Mention the 200 APM ceiling
> on competitive StarCraft, mention the population that bounced off
> the genre, mention that "real generals don't aim individual rifles".
> Don't sneer at competitive RTS — concede that mechanical mastery is
> a real and valid pursuit. The argument is for a parallel mode of
> play, not a replacement.)*

## 2. What APM actually gates

> *(this section is the core argument — spend the most time here.
> Walk through a concrete example: in RA1, a competent opening is
> ~10 building decisions in the first 3 minutes plus continuous
> harvester management plus production queues plus scouting. The
> *decisions* take maybe 30 seconds of thought; the *clicks* take
> 3 minutes of execution. Most of what looks like "RTS skill" is the
> ability to spread attention across that execution gap.)*
>
> *(then make the harder claim: most strategic mistakes happen
> because the player can't think and execute simultaneously. Free
> the execution and decisions get better. Cite anything you can
> from cognitive load research if you want. Or don't — the
> intuitive case is strong enough.)*

## 3. The technical recipe

> *(architecture diagram from the README, expanded. Walk through the
> three loops:)*
>
> 1. **Voice → action.** Push-to-talk Whisper. Why local STT not API:
>    latency, privacy, cost. Why this matters for the player UX.
> 2. **State → adviser.** Snapshot pump → event bus → LLM. Why
>    event-driven not polling: token cost stays *flat* in game
>    length, vs linear if you poll. Show the numbers — your
>    `commander.py` has 5k tokens/min as the polling baseline; the
>    adviser sits at ~zero idle and spikes only on real events.
> 3. **Reactor pipeline.** Zero-token, sub-second responses to
>    deterministic situations (auto-place a finished building). Most
>    "agent" projects forget this layer and waste tokens on things a
>    20-line Python function handles better.
>
> *(this is the section where the technically curious HN reader
> commits to reading the rest. Code excerpts welcome. The
> `event-driven > polling` argument is genuinely novel-ish for game
> agents and will get cited.)*

## 4. Picking the right RTS to start with

> *(short. Three constraints: (a) the engine has to expose game state
> to a third-party process, (b) it has to accept commands from the
> same channel, (c) it has to run on macOS in 2025. OpenRA Red Alert
> 1 is currently the only serious option that satisfies all three. The
> ExternalControl trait was added by upstream; vibera contributes a
> one-line port patch and consumes it.)*
>
> *(mention that the pattern generalises: any RTS that exposes its
> state and accepts external commands could host this kind of agent.
> Modders for SC2 / AoE2 / Warcraft 3 / Beyond All Reason — there's
> nothing here that's RA1-specific, the prompts are the only RA1
> coupling.)*

## 5. War stories

> *(this is the section that makes the post sticky. Each story should
> be 200–400 words, concrete, with at least one moment of "and then I
> realised...".)*
>
> Candidates:
> - Whisper.cpp Metal context fighting OpenRA's Metal context — the
>   crash you hit, the prewarm fix, why predicting concurrency bugs
>   in two unrelated Metal users is hard.
> - Designing the JSON action vocabulary. Started too rich, the LLM
>   couldn't reliably emit valid actions; pruned to 12 verbs and
>   accuracy jumped. Lesson about LLM action-space design.
> - The adviser hallucinating unit IDs. Validator catches it. Why
>   you need a validator even when the prompt says "never invent IDs"
>   in bold.
> - Single-threaded build queue: the LLM kept queuing two buildings
>   in one tick. The B1 hard rule in the prompt was the fix. Lesson
>   about encoding game *mechanics* into prompts, not just game
>   *vocabulary*.

## 6. What vibera is not

> *(an aggressive limitations section. Better to under-promise and
> let the demo over-deliver than the reverse.)*
>
> - Not a micro replacement. A skilled human still outmicros vibera.
> - Not a competitive tool. Won't beat top OpenRA AI on hard map.
>   *(yet — but don't lead with this caveat.)*
> - Not a general game-playing agent. RA1-specific prompts.
> - Not robust. macOS only. Alpha. Crashes. The floating chat has
>   a known bug forwarding voice transcripts.
> - Not a research paper. No benchmarks, no ablations, no published
>   comparisons. It's a working POC in the open.

## 7. What's interesting about it anyway

> *(close on the genre-design claim. This is what makes the post
> shareable.)*
>
> - **Accessibility.** RTS is one of the worst genres for motor
>   impairment. Voice-as-input changes the eligibility list. (One
>   sentence, no special section — this matters but isn't the lead.)
> - **A new kind of RTS UX is now buildable.** What used to require
>   a Blizzard-scale team can be prototyped by one person in a few
>   weekends because three commodity ingredients (local STT, an LLM
>   that emits JSON, and a moddable engine with an external-control
>   trait) finally exist together.
> - **The agent pattern is reusable.** Anything with a real-time
>   state stream and a finite action vocabulary — sim games,
>   industrial control demos, browser automation — can use the same
>   event-driven structure with bounded token cost.
> - **The genre might just be due.** RTS peaked commercially around
>   2002 and has been declining since. Maybe APM was the bottleneck
>   the whole time.

## 8. Try it / read the code / yell at me

> *(repo link, install path, demo description, invitation to file
> issues. End with one provocation: "what would you build if your
> RTS understood plain English?")*

---

## Notes for the author (delete before publishing)

- **Length target:** 1500–2500 words. Don't pad.
- **Tone:** technical, plain, slightly opinionated. No hype words.
  No "revolutionary", no "game-changing", no exclamation marks.
- **Title for HN:** *"Show HN: Be the commander, not the clicker —
  voice + LLM RTS on Red Alert 1"*. Keep it under 80 chars.
- **Body for HN submission:** 200 words, see the README intro for the
  template. Don't paste the full essay into the HN body.
- **Best post day:** Tuesday or Wednesday, 6:30–8:00 AM Pacific.
- **First-hour rule:** be at the keyboard for 4–6h after posting,
  reply to every comment within 10 minutes.
- **Pre-empt the top 5 critical questions:** all answered in the
  README's FAQ section. If a commenter raises one, link them there.
- **Cross-post timing:** r/openra + r/LocalLLaMA 24h after HN, with a
  link back to the HN thread as a secondary comment.
