"""
Task-plan LLM translator.

Counterpart to `llm_translator.py` (which still emits the legacy flat
actions schema). This one targets the new Task/Step schema consumed by
`daemon.TaskDaemon` — i.e. the LLM emits a multi-step plan with wait
predicates instead of a list of immediate orders.

Public API:

    plan = translate_to_plan(utterance: str, lean_state_json: str) -> dict
    task = build_task(utterance, plan) -> Task

`plan` always contains:
    intent: str
    steps: list[dict]
    confidence: float
    reasoning: str
    _model, _latency_sec
And on failure:
    _error, _raw

We deliberately re-use `voice_commander.snapshot_to_lean_state` to
build the game state — the prompt is the only thing that changes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .task import Task

PROMPT_PATH = Path(__file__).parent / "prompts" / "task_planner_prompt.md"
ADVISER_PROMPT_PATH = Path(__file__).parent / "prompts" / "adviser_prompt.md"
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def load_adviser_prompt() -> str:
    # The adviser prompt references task_planner_prompt for verb/predicate
    # vocabulary. We concatenate so the model sees both without us having
    # to maintain two copies of the verb table.
    return (ADVISER_PROMPT_PATH.read_text(encoding="utf-8")
            + "\n\n---\n\n# Reference: task planner vocabulary\n\n"
            + PROMPT_PATH.read_text(encoding="utf-8"))


def translate_to_plan(utterance: str,
                      game_state_json: str,
                      model: str = DEFAULT_MODEL) -> dict:
    """Ask Gemini to plan. Always returns a dict; check for `_error`."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("missing GEMINI_API_KEY env var")

    client = genai.Client(api_key=api_key)
    system = load_prompt()
    user_msg = (
        f"<game_state>\n{game_state_json}\n</game_state>\n\n"
        f"<utterance>\n{utterance}\n</utterance>\n\n"
        "Output ONLY valid JSON matching the schema (one object containing intent / steps / "
        "confidence / reasoning）。"
    )

    cfg_kwargs: dict[str, Any] = dict(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=0.2,
        max_output_tokens=4000,
    )
    if model.startswith("gemini-3"):
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    t0 = time.time()
    resp = client.models.generate_content(
        model=model,
        contents=user_msg,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    latency = time.time() - t0

    text = (resp.text or "").strip()
    # Defensive: strip markdown fences in case the model ignored the rule.
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        return {
            "_error": f"JSON parse failed: {e}",
            "_raw": text[:600],
            "_latency_sec": round(latency, 3),
            "_model": model,
        }

    plan["_latency_sec"] = round(latency, 3)
    plan["_model"] = model
    err = _validate_plan(plan)
    if err:
        plan["_error"] = err
        plan["_raw"] = text[:600]
    return plan


def _validate_plan(plan: dict) -> Optional[str]:
    """Cheap structural validation. Predicate kinds + verbs are checked
    deeper inside the daemon — here we only catch the obvious gibberish."""
    if not isinstance(plan, dict):
        return "plan is not an object"
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return "plan.steps is not a list"
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return f"steps[{i}] is not an object"
        kind = s.get("kind")
        if kind not in ("action", "wait", "branch"):
            return f"steps[{i}].kind invalid: {kind!r}"
        if kind == "action":
            if not isinstance(s.get("verb"), str) or not s["verb"]:
                return f"steps[{i}] action missing verb"
            if "params" in s and not isinstance(s["params"], dict):
                return f"steps[{i}].params is not an object"
        if kind in ("wait", "branch"):
            u = s.get("until")
            if not isinstance(u, dict) or "kind" not in u:
                return f"steps[{i}].until missing or malformed"
        if kind == "branch":
            if not isinstance(s.get("then"), list) or not isinstance(s.get("otherwise"), list):
                return f"steps[{i}] branch needs both `then` and `otherwise` lists"
    return None


def build_task(utterance: str, plan: dict) -> Task:
    """Wrap a validated plan into a Task ready for `daemon.add_task()`."""
    intent = str(plan.get("intent") or utterance)
    steps = plan.get("steps") or []
    return Task.new(intent=intent, steps=steps, utterance=utterance)


def propose_advice(game_state_json: str,
                   model: str = DEFAULT_MODEL) -> dict:
    """Ask Gemini to act as the adviser. Returns:

        {commentary: str, suggestions: [{title, confidence, reason, task_plan}],
         _model, _latency_sec}

    On parse / shape failure, returns `{_error, _raw, ...}` (caller can
    show commentary='?' and skip).
    """
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("missing GEMINI_API_KEY env var")

    client = genai.Client(api_key=api_key)
    system = load_adviser_prompt()
    user_msg = (
        f"<game_state>\n{game_state_json}\n</game_state>\n\n"
        "Output JSON matching the schema (commentary + suggestions)."
    )

    cfg_kwargs: dict[str, Any] = dict(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=0.3,
        max_output_tokens=3000,
    )
    if model.startswith("gemini-3"):
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    t0 = time.time()
    resp = client.models.generate_content(
        model=model,
        contents=user_msg,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    latency = time.time() - t0

    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        advice = json.loads(text)
    except json.JSONDecodeError as e:
        return {
            "_error": f"JSON parse failed: {e}",
            "_raw": text[:600],
            "_latency_sec": round(latency, 3),
            "_model": model,
        }

    advice["_latency_sec"] = round(latency, 3)
    advice["_model"] = model
    err = _validate_advice(advice)
    if err:
        advice["_error"] = err
        advice["_raw"] = text[:600]
    return advice


def _validate_advice(advice: dict) -> Optional[str]:
    if not isinstance(advice, dict):
        return "advice is not an object"
    if not isinstance(advice.get("commentary", ""), str):
        return "commentary is not a string"
    sug = advice.get("suggestions")
    if not isinstance(sug, list):
        return "suggestions is not a list"
    for i, s in enumerate(sug):
        if not isinstance(s, dict):
            return f"suggestions[{i}] not an object"
        if not isinstance(s.get("title"), str) or not s["title"]:
            return f"suggestions[{i}].title missing"
        if s.get("confidence") not in ("high", "med", "low"):
            return f"suggestions[{i}].confidence must be high/med/low"
        plan = s.get("task_plan")
        if not isinstance(plan, dict):
            return f"suggestions[{i}].task_plan missing"
        # Reuse the plan validator — same schema.
        plan_err = _validate_plan(plan)
        if plan_err:
            return f"suggestions[{i}].task_plan invalid: {plan_err}"
    return None


if __name__ == "__main__":                                   # pragma: no cover
    # Quick manual test: requires GEMINI_API_KEY + a running OpenRA.
    import sys

    from openra_client import OpenRAClient
    from voice_commander import snapshot_to_lean_state

    utt = " ".join(sys.argv[1:]) or "build a power plant at a sensible spot"
    with OpenRAClient() as c:
        if not c.ping():
            print("ping failed")
            sys.exit(1)
        snap = c.snapshot()
        state = snapshot_to_lean_state(snap)
        plan = translate_to_plan(utt, json.dumps(state, ensure_ascii=False))
    print(json.dumps(plan, indent=2, ensure_ascii=False))
