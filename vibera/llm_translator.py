"""
LLM translator: utterance + game state -> structured JSON command.
Uses Google Gemini (gemini-2.5-flash-lite or newer).
"""
import json
import os
import time
from pathlib import Path

PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def translate(utterance: str, game_state_json: str, model: str = DEFAULT_MODEL) -> dict:
    """Translate via Gemini. Requires GEMINI_API_KEY env."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("missing GEMINI_API_KEY env var")

    client = genai.Client(api_key=api_key)
    system = load_system_prompt()
    user_msg = f"""<game_state>
{game_state_json}
</game_state>

<utterance>
{utterance}
</utterance>

Output ONLY valid JSON matching the schema."""

    # 2.5 series does not support thinking_config; 3.x does.
    cfg_kwargs = dict(
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
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        result = json.loads(text)
        result["_latency_sec"] = round(latency, 3)
        result["_model"] = model
        return result
    except json.JSONDecodeError as e:
        return {
            "_error": f"JSON parse failed: {e}",
            "_raw": text,
            "_latency_sec": round(latency, 3),
            "_model": model,
        }
