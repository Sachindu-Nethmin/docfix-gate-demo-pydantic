"""docfix-gate: label a GitHub issue and decide whether an AI agent should attempt it.

    python -m docfix_gate action                  run inside GitHub Actions on an `issues` event
    python -m docfix_gate classify --repo O/R --title T --body B
    python -m docfix_gate scan --repo O/R --limit 50 --out scan.jsonl   (read-only)
"""
import argparse
import json
import os
import subprocess
import sys

from . import classify as cls
from . import github, policy
from .codebook import CODEBOOK_VERSION

MARKER = "<!-- docfix-gate -->"
DEFAULT_MODEL = "claude-opus-5-5"


def _env(name, default=""):
    return os.environ.get(f"INPUT_{name.upper().replace('-', '_')}", default).strip()


def _bool(value, default=False):
    return default if value == "" else value.lower() in ("1", "true", "yes", "on")


def provider_from(kind, model, base_url, api_key, effort="low", prompt_suffix="", reasoning_effort=""):
    """The model endpoint to use, or None for rules only (also when the Anthropic key is missing)."""
    kind = (kind or "anthropic").lower()
    if kind == "none":
        return None
    if kind == "anthropic":
        if not api_key:
            return None
        return {"kind": "anthropic", "api_key": api_key, "model": model or DEFAULT_MODEL, "effort": effort or "low"}
    if kind == "openai-compatible":
        if not base_url or not model:
            raise SystemExit("provider openai-compatible needs base-url and model")
        extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        return {"kind": "openai", "base_url": base_url, "api_key": api_key, "model": model, "prompt_suffix": prompt_suffix, "extra": extra}
    raise SystemExit(f"unknown provider {kind}: use anthropic, openai-compatible or none")


def _summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")


def _output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def run_action():
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    issue = event.get("issue")
    if not issue or "pull_request" in issue:
        print("docfix-gate: not an issue event, nothing to do")
        return 0
    repo = os.environ["GITHUB_REPOSITORY"]
    token = _env("github-token") or os.environ.get("GITHUB_TOKEN", "")
    number, title, body = issue["number"], issue.get("title") or "", issue.get("body") or ""
    current = {l["name"] for l in issue.get("labels", [])}

    skip_labels = {s.strip() for s in _env("skip-labels").split(",") if s.strip()}
    if current & skip_labels or (_bool(_env("skip-if-assigned"), True) and issue.get("assignees")):
        print("docfix-gate: skipped (assigned, or has a skip label)")
        _output("route", "skipped")
        return 0

    kind = cls.repo_kind(repo, _env("repo-kind", "auto"))
    provider_kind = _env("provider", "anthropic")
    provider = provider_from(provider_kind, _env("model"), _env("base-url"), _env("api-key"), _env("effort", "low"),
                             _env("prompt-suffix"), _env("reasoning-effort"))
    if provider is None and provider_kind != "none":
        print("docfix-gate: no api-key given, so the rules are used without a model")
    label = cls.classify(repo, title, body, kind, provider)
    route, reason = policy.decide(label, kind)
    cfg = {"label_prefix": _env("label-prefix", "docfix/"), "category_labels": _bool(_env("category-labels"), True),
           "agent_ready_label": _env("agent-ready-label", "docfix/agent-ready"),
           "needs_human_label": _env("needs-human-label", "docfix/needs-human")}
    new = policy.labels_for(label, route, cfg)

    mode = _env("mode", "suggest").lower()  # suggest: write only the job summary; apply: label and comment
    if mode == "apply":
        ours = {l for l in current if l.startswith(cfg["label_prefix"])} | ({cfg["agent_ready_label"], cfg["needs_human_label"]} & current)
        for stale in sorted(ours - set(new)):
            github.remove_label(repo, number, stale, token)
        github.add_labels(repo, number, [l for l in new if l not in current], token)
        when = _env("comment", "needs-human").lower()  # always | needs-human | never
        if when == "always" or (when == "needs-human" and route == policy.HUMAN):
            github.upsert_comment(repo, number, policy.comment(label, route, reason, kind, MARKER), MARKER, token)

    for k, v in (("category", label["category"]), ("fix-check", label.get("fix_check", "")), ("route", route),
                 ("confidence", label.get("confidence", "")), ("method", label.get("method", "")), ("labels", ",".join(new))):
        _output(k, v)
    _summary("\n".join([
        f"### docfix-gate: issue #{number}",
        f"- Route: **{policy.route_words(route)}** ({reason})",
        f"- Type: `{label['category']}`" + (f", fix check `{label['fix_check']}`" if label.get("fix_check") else ""),
        f"- Labels {'added' if mode == 'apply' else 'suggested'}: {', '.join(new) or 'none'}",
        f"- Method: {label.get('method')} {label.get('model', '')}, confidence {label.get('confidence')}, repository kind `{kind}`",
        f"- Rule applied: {label.get('rule_override') or 'none'}" + (f"; model failed, used rules: {label['fallback']}" if label.get("fallback") else ""),
        f"- Codebook: {CODEBOOK_VERSION}"]))
    print(json.dumps({"issue": number, "route": route, "labels": new, **{k: label.get(k) for k in ("category", "fix_check", "confidence", "method")}}))
    return 0


def _local_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main(argv=None):
    p = argparse.ArgumentParser(prog="docfix_gate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("action", help="run inside GitHub Actions")
    for name in ("classify", "scan"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True, help="owner/name")
        s.add_argument("--kind", default="auto", choices=["auto", "code", "docs"])
        s.add_argument("--provider", default="none", choices=["none", "anthropic", "openai-compatible"])
        s.add_argument("--model", default="")
        s.add_argument("--base-url", default="")
        s.add_argument("--api-key-env", default="", help="name of the environment variable that holds the model key")
        s.add_argument("--effort", default="low", help="Anthropic effort level")
        s.add_argument("--prompt-suffix", default="", help="text added after the issue in the prompt")
        s.add_argument("--reasoning-effort", default="", help="for OpenAI-format endpoints, for example none to turn off Qwen3 thinking on Ollama")
        if name == "classify":
            s.add_argument("--title", default="")
            s.add_argument("--body", default="")
        else:
            s.add_argument("--limit", type=int, default=50)
            s.add_argument("--state", default="all", choices=["all", "open", "closed"])
            s.add_argument("--out", default="")
    a = p.parse_args(argv)
    if a.cmd == "action":
        return run_action()

    token = _local_token()
    key = os.environ.get(a.api_key_env, "") if a.api_key_env else ""
    provider = provider_from(a.provider, a.model, a.base_url, key, a.effort, a.prompt_suffix, a.reasoning_effort)
    if a.provider == "anthropic" and provider is None:
        raise SystemExit("provider anthropic needs --api-key-env naming a variable that holds the key")
    kind = cls.repo_kind(a.repo, a.kind)
    issues = ([{"number": None, "title": a.title, "body": a.body, "labels": [], "html_url": ""}] if a.cmd == "classify"
              else github.list_issues(a.repo, token, a.state, a.limit))
    out = open(a.out, "w") if getattr(a, "out", "") else None
    for i in issues:
        label = cls.classify(a.repo, i.get("title"), i.get("body"), kind, provider)
        route, reason = policy.decide(label, kind)
        row = {"number": i.get("number"), "title": (i.get("title") or "")[:120], "maintainer_labels": [l["name"] for l in i.get("labels", [])],
               "route": route, "reason": reason, **label}
        print(json.dumps(row) if a.cmd == "classify" else f"#{row['number']:<5} {route:6s} {label['category']:13s} {label.get('fix_check',''):15s} {row['title'][:70]}")
        if out:
            out.write(json.dumps(row) + "\n")
    if out:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
