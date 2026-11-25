"""Model clients and a strict answer parser.

Two kinds of endpoint:
- Anthropic's Messages API, through the official `anthropic` SDK, with a JSON schema so the answer is
  always valid JSON with allowed values.
- Any chat completion endpoint in the OpenAI format (Ollama, vLLM, OpenRouter, Groq, Gemini's OpenAI
  endpoint, company gateways), with the standard library only.

GitHub Models is not offered: GitHub retired it on 30 July 2026.
"""
import json
import re
import urllib.error
import urllib.request

from .codebook import CATEGORIES, CONFIDENCE, FIX_CHECKS

THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
# models that accept Anthropic's server-side refusal fallback (`fallbacks: "default"`)
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def answer_schema(docs_mode):
    """JSON schema of the answer, so Anthropic's structured outputs only return allowed values."""
    s = {"main_request": {"type": "string"}, "doc_evidence": {"type": "string"},
         "category": {"type": "string", "enum": CATEGORIES}, "confidence": {"type": "string", "enum": CONFIDENCE},
         "reason": {"type": "string"}}
    if docs_mode:
        s.update({"checkable_by_tool": {"type": "string", "enum": ["yes", "no"]},
                  "correct_content_in_issue": {"type": "string", "enum": ["yes", "no"]}})
    else:
        s.update({"repo_is_doc_tool": {"type": "string", "enum": ["yes", "no"]},
                  "doc_source": {"type": "string", "enum": ["own", "external", "none"]},
                  "docs_only_fix": {"type": "string", "enum": ["yes", "no"]}})
    return {"type": "object", "properties": s, "required": list(s), "additionalProperties": False}


def anthropic_chat(api_key, model, system, user, docs_mode, effort="low", timeout=120):
    """One classification through Anthropic's Messages API. Raises ModelError on any failure or refusal."""
    try:
        import anthropic
    except ImportError:
        raise ModelError("the anthropic package is not installed (pip install anthropic)") from None
    client = anthropic.Anthropic(api_key=api_key or None, timeout=timeout, max_retries=2)
    kwargs = {"model": model, "max_tokens": 4000, "system": system, "messages": [{"role": "user", "content": user}],
              "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": answer_schema(docs_mode)}}}
    try:
        if model in FALLBACK_MODELS:
            # on a policy refusal, the API re-runs the request on a fallback model inside the same call
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.RateLimitError:
        raise ModelError("Anthropic API rate limit") from None
    except anthropic.APIStatusError as e:
        raise ModelError(f"Anthropic API error {e.status_code}") from None
    except anthropic.APIConnectionError:
        raise ModelError("could not reach the Anthropic API") from None
    if resp.stop_reason == "refusal":
        raise ModelError("the model declined to classify this issue")
    if resp.stop_reason == "max_tokens":
        raise ModelError("the answer was cut off")
    return next((b.text for b in resp.content if b.type == "text"), "")


class ModelError(Exception):
    """The model could not be reached or did not give a usable answer."""


def _post(url, payload, api_key, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def chat(base_url, api_key, model, system, user, timeout=90, extra=None):
    """Send one request and return the assistant's text.

    `extra` is merged into the request, for example {"reasoning_effort": "none"}, which turns thinking off
    for Qwen3 and similar models on Ollama (a thinking model can otherwise spend the whole token budget
    thinking and return no answer). Some endpoints reject `response_format`, `temperature` or
    `max_tokens`, so a refused request is retried once with only the model, the messages and `extra`.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    extra = extra or {}
    bodies = [{"model": model, "messages": messages, "temperature": 0, "max_tokens": 1500, "response_format": {"type": "json_object"}, **extra},
              {"model": model, "messages": messages, **extra}]
    last = None
    for body in bodies:
        try:
            data = _post(url, body, api_key, timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = ModelError(f"HTTP {e.code} from the model endpoint: {detail}")
            if e.code in (400, 404, 415, 422):
                continue
            raise last from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise ModelError(f"could not reach the model endpoint: {type(e).__name__}") from None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ModelError("the model endpoint returned no message") from None
        if isinstance(content, list):  # some gateways return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or ""
    raise last


def _json_object(text):
    """The first balanced JSON object in the text, or None."""
    text = THINK.sub(" ", text or "")
    start = text.find("{")
    while start != -1:
        depth, in_string, escape = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
            elif c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except ValueError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    return None


def _enum(value, allowed, default):
    value = str(value or "").strip()
    for a in allowed:
        if value.lower() == a.lower():
            return a
    return default


def parse_answer(text, docs_mode):
    """Turn the model's reply into a validated label. Anything outside the allowed values is rejected.

    The category must be one of the four allowed names, otherwise the answer is unusable: the caller
    then falls back to the rules. Every other field is forced into its allowed values or a safe default.
    """
    obj = _json_object(text)
    if obj is None:
        raise ModelError("the model did not answer with a JSON object")
    category = _enum(obj.get("category"), CATEGORIES, None)
    if category is None:
        raise ModelError("the model chose a category that is not allowed")
    label = {
        "category": category,
        "confidence": _enum(obj.get("confidence"), CONFIDENCE, "low"),
        "main_request": str(obj.get("main_request") or "")[:200],
        "doc_evidence": str(obj.get("doc_evidence") or "")[:400],
        "reason": str(obj.get("reason") or "")[:300],
        "repo_is_doc_tool": _enum(obj.get("repo_is_doc_tool"), ["yes", "no"], "no"),
        "doc_source": _enum(obj.get("doc_source"), ["own", "external", "none"], "own" if docs_mode else "none"),
        "docs_only_fix": _enum(obj.get("docs_only_fix"), ["yes", "no"], "" if docs_mode else "no"),
        "fix_check": "",
    }
    if docs_mode:
        label["fix_check"] = derive_fix_check(obj)
    return label


def derive_fix_check(obj):
    """automatic if a tool can check the fix, review if the issue gives the correct content, otherwise
    needs_knowledge. A missing or unclear answer counts as no, so the result errs towards a person."""
    if "checkable_by_tool" not in obj and "correct_content_in_issue" not in obj:
        return _enum(obj.get("fix_check"), FIX_CHECKS, "needs_knowledge")  # older answer format
    if _enum(obj.get("checkable_by_tool"), ["yes", "no"], "no") == "yes":
        return "automatic"
    if _enum(obj.get("correct_content_in_issue"), ["yes", "no"], "no") == "yes":
        return "review"
    return "needs_knowledge"
