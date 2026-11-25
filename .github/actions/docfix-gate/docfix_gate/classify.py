"""Label one issue: screen it, ask the model if one is configured, then enforce the codebook's rules.

The model only proposes. The rules below decide, exactly as in the study: a documentation label needs
the project's own documentation as its source and quoted evidence, and DOC_CONTENT needs a request that a
text change alone would meet. Here the evidence must also be found in the issue, word for word.
"""
import re

from . import llm, rules
from .codebook import CVD, DC, DOC_CATEGORIES, NOT_DOC, SYSTEM_CODE, SYSTEM_DOCS, TOOLING, user_prompt

DOCS_REPO = re.compile(r"(?:^|[/_.-])(?:docs?|documentation|handbook|manual|guides?)(?:$|[/_.-])", re.I)
INJECTION = re.compile(r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts?|rules)|"
                       r"disregard (?:all |your |the )?(?:previous|prior|above|earlier)|you are now\b|new system prompt|"
                       r"\bsystem prompt\b|act as (?:an?|the) |developer mode|\bDAN mode\b|reveal (?:your|the) (?:secrets?|tokens?|"
                       r"environment|api key)|print (?:the )?(?:env|environment variables)", re.I)


def repo_kind(repo, setting="auto"):
    """'code' or 'docs'. With 'auto', a repository whose name says docs, documentation or handbook is 'docs'."""
    if setting in ("code", "docs"):
        return setting
    name = (repo or "").split("/")[-1]
    return "docs" if DOCS_REPO.search(name) else "code"


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def evidence_in_issue(evidence, title, body):
    """True if the quoted evidence really appears in the issue (ignoring case, punctuation and spacing).

    A quote of eight words or more also passes when a run of at least eight of its words appears, so a
    model that trims or joins two nearby phrases is not punished.
    """
    ev, issue = _norm(evidence), _norm((title or "") + " " + (body or ""))
    if not ev:
        return False
    if ev in issue:
        return True
    words = ev.split()
    if len(words) >= 8:
        for i in range(len(words) - 7):
            if " ".join(words[i:i + 8]) in issue:
                return True
    return False


def apply_rules_code(label, title, body):
    """The study's rules for prompt v3, plus the check that the evidence is in the issue."""
    cat = label["category"]
    if label.get("repo_is_doc_tool") == "yes" and cat != TOOLING:
        return {**label, "category": TOOLING, "rule_override": "the repository is a documentation tool"}
    broken = None
    if cat in DOC_CATEGORIES:
        if label.get("doc_source") != "own":
            broken = "the documentation cited is not the project's own"
        elif not (label.get("doc_evidence") or "").strip():
            broken = "no quoted evidence"
        elif not evidence_in_issue(label["doc_evidence"], title, body):
            broken = "the quoted evidence is not in the issue"
        elif cat == DC and label.get("docs_only_fix") != "yes":
            broken = "the request needs a code change"
    if broken:
        return {**label, "category": NOT_DOC, "rule_override": broken}
    return {**label, "rule_override": ""}


def apply_rules_docs(label, title, body):
    """In a documentation repository no evidence is required, but an unverified quote is never shown."""
    if label.get("doc_evidence") and not evidence_in_issue(label["doc_evidence"], title, body):
        label = {**label, "doc_evidence": ""}
    return {**label, "rule_override": ""}


def classify(repo, title, body, kind="code", provider=None):
    """Return the label for one issue.

    `provider` is None for rules only, or a dict with base_url, api_key and model. If the model call or
    its answer fails, the rules are used and the reason is recorded in `fallback`.
    """
    docs_mode = kind == "docs"
    by_rules = rules.classify_docs if docs_mode else rules.classify_code
    flagged = bool(INJECTION.search((title or "") + "\n" + (body or "")))
    extra = {"repo_kind": kind, "injection_flag": flagged, "fallback": ""}

    if not docs_mode and not rules.is_candidate(title, body):
        # the study's screen: an issue with no documentation words is NOT_DOC, and the model is not asked
        return {**by_rules(repo, title, body), "rule_override": "", "model": "", **extra}
    if not provider:
        return {**by_rules(repo, title, body), "rule_override": "", "model": "", **extra}
    system = SYSTEM_DOCS if docs_mode else SYSTEM_CODE
    user = user_prompt(repo, title, body) + provider.get("prompt_suffix", "")
    try:
        if provider.get("kind") == "anthropic":
            text = llm.anthropic_chat(provider.get("api_key", ""), provider["model"], system, user, docs_mode,
                                      provider.get("effort", "low"), provider.get("timeout", 120))
        else:
            text = llm.chat(provider["base_url"], provider.get("api_key", ""), provider["model"], system, user,
                            timeout=provider.get("timeout", 300), extra=provider.get("extra"))
        label = llm.parse_answer(text, docs_mode)
    except llm.ModelError as e:
        return {**by_rules(repo, title, body), "rule_override": "", "model": "", **extra, "fallback": str(e)[:300]}
    label = apply_rules_docs(label, title, body) if docs_mode else apply_rules_code(label, title, body)
    return {**label, "method": "model", "model": provider["model"], **extra}
