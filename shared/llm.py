"""Anthropic LLM wrapper for synthlab-tools.

Two backends, resolved automatically:
  1. Anthropic SDK  — used when ANTHROPIC_API_KEY is set (env or repo .env)
  2. claude -p CLI  — fallback that runs on the local Claude Code login
                      (Max plan), so tools work with zero API-key setup.

Models:
  DEFAULT_MODEL   claude-sonnet-4-6   routine calls ($3/$15 per MTok)
  JUDGMENT_MODEL  claude-fable-5      judgment-heavy calls (clip scoring,
                                      SEO grading). On the SDK path Fable
                                      requests include a server-side
                                      fallback to claude-opus-4-8 so a
                                      safety-classifier refusal never
                                      fails a cron run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = os.environ.get("SYNTHLAB_LLM_DEFAULT", "claude-sonnet-4-6")
JUDGMENT_MODEL = os.environ.get("SYNTHLAB_LLM_JUDGMENT", "claude-fable-5")
FALLBACK_MODEL = "claude-opus-4-8"

# claude CLI aliases, used if the CLI rejects a full model id
_CLI_ALIAS = {DEFAULT_MODEL: "sonnet", JUDGMENT_MODEL: "opus", FALLBACK_MODEL: "opus"}


# Only these keys are loaded from .env into the process env. The .env also
# holds a manual-recovery CLAUDE_CODE_OAUTH_TOKEN; loading it here would push it
# into every `claude -p` subprocess and override the Max-login keychain auth
# (the exact failure the global "never export CLAUDE_CODE_OAUTH_TOKEN" rule
# guards against). Whitelist, so the token stays inert on disk.
_DOTENV_ALLOWED = ("ANTHROPIC_API_KEY", "SYNTHLAB_LLM_DEFAULT", "SYNTHLAB_LLM_JUDGMENT")


def _load_dotenv() -> None:
    env_file = REPO / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            if k in _DOTENV_ALLOWED:
                os.environ.setdefault(k, v.strip().strip("'\""))


_load_dotenv()


def _api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _extract_json(text: str):
    """Parse JSON out of a model reply, tolerating code fences and prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # last resort: first {...} or [...] span
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"model did not return parseable JSON: {text[:300]!r}")


def _sdk_complete(prompt: str, system: str | None, model: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()
    kwargs: dict = dict(model=model, max_tokens=max_tokens,
                        messages=[{"role": "user", "content": prompt}])
    if system:
        kwargs["system"] = system

    if model == JUDGMENT_MODEL and "fable" in model:
        # Fable 5: thinking always on (omit the param); opt into the
        # server-side refusal fallback so benign-but-flagged inputs
        # (security-audit text, etc.) get re-served by Opus 4.8.
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-06-01"],
            fallbacks=[{"model": FALLBACK_MODEL}],
            **kwargs,
        )
    else:
        response = client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError("model refused the request (safety classifier)")
    return "".join(b.text for b in response.content if b.type == "text")


def _cli_complete(prompt: str, system: str | None, model: str, timeout: int = 600) -> str:
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    for model_arg in (model, _CLI_ALIAS.get(model, "sonnet")):
        cmd = ["claude", "-p", full_prompt, "--model", model_arg, "--output-format", "text"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"claude -p timed out after {timeout}s")
        if result.returncode == 0:
            return result.stdout.strip()
        # unknown-model errors fall through to the alias retry
        if "model" not in result.stderr.lower():
            break
    raise RuntimeError(f"claude -p failed: {result.stderr.strip()[:500]}")


def complete(prompt: str, *, system: str | None = None, model: str | None = None,
             judgment: bool = False, max_tokens: int = 8000) -> str:
    """Run one completion. judgment=True routes to the Fable-tier model."""
    resolved = model or (JUDGMENT_MODEL if judgment else DEFAULT_MODEL)
    if _api_key():
        return _sdk_complete(prompt, system, resolved, max_tokens)
    return _cli_complete(prompt, system, resolved)


def complete_json(prompt: str, *, system: str | None = None, model: str | None = None,
                  judgment: bool = False, max_tokens: int = 8000):
    """Like complete() but instructs for and parses a JSON reply."""
    json_system = ((system + "\n\n") if system else "") + \
        "Respond with valid JSON only. No prose, no markdown fences, no explanation."
    raw = complete(prompt, system=json_system, model=model,
                   judgment=judgment, max_tokens=max_tokens)
    return _extract_json(raw)


if __name__ == "__main__":
    import sys
    print(complete(sys.argv[1] if len(sys.argv) > 1 else "Reply with exactly: OK"))
