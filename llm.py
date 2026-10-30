"""Every model call goes through here.

Callers ask one structured question: a system prompt, a JSON schema and
the user text. They get back a dict that matches the schema, or an
LLMError with a short reason.

Which AI answers is set in .env with LLM_PROVIDER:

  claude-cli         (default) the Claude Code CLI on your own sign-in.
                     No API key and no per-call cost beyond your plan.
  anthropic          the Anthropic API. Needs ANTHROPIC_API_KEY and the
                     `anthropic` Python package.
  openai-compatible  any server that speaks the OpenAI chat API: OpenAI
                     itself, Ollama, LM Studio and others. Needs
                     LLM_BASE_URL and LLM_MODEL, plus LLM_API_KEY when the
                     server wants one.

Callers pass short model names ("sonnet", "haiku"). The CLI takes those as
they are. The API path maps them to full model ids, and LLM_MODEL
overrides the choice for any provider except claude-cli.
"""

import json
import os
import re
import subprocess

from claude_cli import find_claude


class LLMError(Exception):
    pass


PROVIDERS = ("claude-cli", "anthropic", "openai-compatible")

# Short names used across the code, mapped to Anthropic API model ids.
_ANTHROPIC_IDS = {
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "opus": "claude-opus-5-5",
}


def provider():
    name = (os.environ.get("LLM_PROVIDER") or "claude-cli").strip().lower()
    return name if name in PROVIDERS else "claude-cli"


def describe():
    """A short line for the health check: which AI and which model."""
    p = provider()
    if p == "claude-cli":
        return {"provider": p, "binary": find_claude()}
    if p == "anthropic":
        return {"provider": p, "model": _anthropic_model("sonnet"),
                "key_set": bool(os.environ.get("ANTHROPIC_API_KEY"))}
    return {"provider": p, "base_url": os.environ.get("LLM_BASE_URL", ""),
            "model": os.environ.get("LLM_MODEL", "")}


def run(system, schema, prompt, timeout=120, model="sonnet", claude_bin=None):
    """Ask one structured question. Returns the parsed dict."""
    p = provider()
    if p == "anthropic":
        return _run_anthropic(system, schema, prompt, timeout, model)
    if p == "openai-compatible":
        return _run_openai(system, schema, prompt, timeout)
    return _run_claude_cli(system, schema, prompt, timeout, model, claude_bin)


# ---------- Claude Code CLI ----------

def _run_claude_cli(system, schema, prompt, timeout, model, claude_bin):
    argv = [
        claude_bin or find_claude(), "-p",
        "--model", model or "sonnet",
        "--tools", "",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", system,
        "--json-schema", json.dumps(schema),
    ]
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True,
                              text=True, timeout=timeout,
                              cwd=os.path.expanduser("~"))
    except subprocess.TimeoutExpired:
        raise LLMError(f"claude CLI timed out after {timeout}s")
    except OSError as e:
        raise LLMError(f"claude CLI could not start: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-200:].strip()
        raise LLMError(f"claude CLI exited {proc.returncode}"
                       + (f": {tail}" if tail else ""))
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        raise LLMError("claude CLI did not return JSON")
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        raise LLMError("claude CLI reported an error")
    result = envelope.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if not isinstance(result, dict):
        result = envelope.get("structured_output")
    if not isinstance(result, dict):
        raise LLMError("claude CLI did not return a result")
    return result


# ---------- Anthropic API ----------

def _anthropic_model(model):
    override = (os.environ.get("LLM_MODEL") or "").strip()
    if override:
        return override
    return _ANTHROPIC_IDS.get((model or "sonnet").lower(), model or "claude-sonnet-5-5")


def _run_anthropic(system, schema, prompt, timeout, model):
    try:
        import anthropic
    except ImportError:
        raise LLMError("LLM_PROVIDER is anthropic but the `anthropic` package "
                       "is not installed. Run: pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMError("ANTHROPIC_API_KEY is not set in .env")
    client = anthropic.Anthropic(timeout=float(timeout))
    model_id = _anthropic_model(model)
    try:
        response = client.messages.create(
            model=model_id,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except (anthropic.BadRequestError, TypeError):
        # Some schemas use features structured output does not accept, and
        # an older SDK has no output_config (TypeError). Ask for the same
        # JSON in plain text instead and parse it.
        try:
            response = client.messages.create(
                model=model_id,
                max_tokens=16000,
                system=system + _schema_note(schema),
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as e:
            raise LLMError(f"Anthropic API error: {e}")
    except anthropic.AuthenticationError:
        raise LLMError("Anthropic API key was rejected")
    except anthropic.RateLimitError:
        raise LLMError("Anthropic API rate limit, try again shortly")
    except anthropic.APITimeoutError:
        raise LLMError(f"Anthropic API timed out after {timeout}s")
    except anthropic.APIConnectionError:
        raise LLMError("could not reach the Anthropic API")
    except anthropic.APIError as e:
        raise LLMError(f"Anthropic API error: {e}")
    if getattr(response, "stop_reason", None) == "refusal":
        raise LLMError("the model declined this request")
    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_json(text, "Anthropic API")


# ---------- OpenAI-compatible (OpenAI, Ollama, LM Studio) ----------

def _run_openai(system, schema, prompt, timeout):
    import requests

    base = (os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    model = (os.environ.get("LLM_MODEL") or "").strip()
    if not base or not model:
        raise LLMError("LLM_BASE_URL and LLM_MODEL must be set in .env for "
                       "openai-compatible")
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("LLM_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system + _schema_note(schema)},
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema},
        },
    }
    url = base + "/chat/completions"
    try:
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
        if r.status_code == 400:
            # Older servers do not take json_schema. The schema is in the
            # system prompt as well, so ask without it.
            body.pop("response_format")
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
    except requests.Timeout:
        raise LLMError(f"{base} timed out after {timeout}s")
    except requests.RequestException as e:
        raise LLMError(f"could not reach {base}: {e}")
    if r.status_code != 200:
        raise LLMError(f"{base} returned {r.status_code}: {r.text[:200]}")
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError(f"{base} returned an answer in an unexpected shape")
    return _parse_json(text, base)


# ---------- helpers ----------

def _schema_note(schema):
    return ("\n\nReply with one JSON object and nothing else. It must match "
            "this JSON schema:\n" + json.dumps(schema))


def _parse_json(text, source):
    text = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        result = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            result = json.loads(text[start:end + 1]) if start >= 0 else None
        except ValueError:
            result = None
    if not isinstance(result, dict):
        raise LLMError(f"{source} did not return JSON")
    return result
