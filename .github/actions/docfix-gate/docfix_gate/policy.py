"""Turn a label into a route: send the issue to an agent, to a person, or leave it alone.

The rules come from the docfix-predict study:
- In code repositories, agents produced a test-verified fix for 34.2% of CODE_VS_DOCS issues but only
  8.6% of DOC_CONTENT issues, because the fix the tests accept usually changes code (69 of 71). So a
  DOC_CONTENT issue in a code repository goes to a person, with a note on how to rewrite it.
- In documentation repositories, a fix is safe for an agent when something can check it: a build or link
  checker (automatic), or the issue itself stating the correct content (review). When the correct content
  needs product knowledge, the issue goes to a person.
- A model's confidence is not permission: a low-confidence label or a possible prompt injection never
  sends an issue to an agent.
"""
from .codebook import CVD, DC, NOT_DOC, TOOLING

AGENT, HUMAN, NONE = "agent", "human", "none"

STUDY_NOTE = ("In the docfix-predict study (13,149 GitHub issues, 219,589 agent runs), agents produced a fix that passed "
              "the project's tests for 8.6% of issues framed as documentation changes, against 34.2% for issues where the "
              "code contradicts the documentation. In 69 of 71 such issues, the accepted fix changed code.")


def decide(label, kind):
    """Return (route, reason)."""
    cat, check, conf = label.get("category"), label.get("fix_check", ""), label.get("confidence", "low")
    if label.get("injection_flag"):
        return HUMAN, "the issue contains text that looks like instructions to an AI, so a person should read it first"
    if kind == "code":
        if cat == CVD:
            if conf == "low":
                return HUMAN, "the code may contradict the documentation, but the label has low confidence"
            return AGENT, "the code contradicts the documentation; the project's tests can check a code fix"
        if cat == DC:
            return HUMAN, ("the issue asks for a documentation change, which the project's tests cannot check; "
                           "if code is really at fault, a test can check the fix")
        return NONE, "not a documentation issue"
    # documentation repository
    if cat == DC and check in ("automatic", "review"):
        if conf == "low":
            return HUMAN, "a documentation fix, but the label has low confidence"
        how = "a build, link checker or spell checker can confirm the fix" if check == "automatic" else "the issue states the correct content, so a reviewer can check the fix"
        return AGENT, how
    if cat in (DC, CVD):
        if cat == CVD:
            return HUMAN, "the documentation and the product disagree; someone must confirm how the product behaves"
        return HUMAN, "the correct content is not in the issue; it needs someone who knows the product"
    if cat == TOOLING:
        return HUMAN, "a problem with the documentation site or its tooling, not the content"
    return NONE, "not a documentation issue"


def labels_for(label, route, cfg):
    """The labels this decision adds, given the configured names (empty names are skipped)."""
    out = []
    cat = label.get("category")
    if cat and cat != NOT_DOC and cfg.get("category_labels", True):
        out.append(cfg.get("label_prefix", "docfix/") + cat.lower().replace("_", "-"))
    if route == AGENT and cfg.get("agent_ready_label"):
        out.append(cfg["agent_ready_label"])
    if route == HUMAN and cfg.get("needs_human_label"):
        out.append(cfg["needs_human_label"])
    return [l for l in out if l]


def _quote(text):
    """Issue text shown back in a comment: one line, no mentions, inside a code span."""
    t = " ".join((text or "").split())[:200].replace("`", "'").replace("@", "@​")
    return f"`{t}`" if t else ""


def comment(label, route, reason, kind, marker):
    """The comment posted on the issue. Only fixed text and evidence that was found in the issue are shown."""
    lines = [marker, f"**docfix-gate:** {route_words(route)}.", "", f"- Type: `{label.get('category')}`"]
    if kind == "docs" and label.get("fix_check"):
        lines.append(f"- How a fix can be checked: `{label['fix_check']}`")
    lines.append(f"- Why: {reason}.")
    ev = _quote(label.get("doc_evidence"))
    if ev:
        lines.append(f"- From the issue: {ev}")
    if kind == "code" and label.get("category") == DC:
        lines += ["", STUDY_NOTE, "",
                  "If the problem is that the code behaves wrongly, please edit the issue to say what the code should do, "
                  "with a short example that fails today. If only the text is wrong, a person can fix it directly."]
    if kind == "docs" and route == HUMAN and label.get("category") in (DC, CVD):
        lines += ["", "To make this issue ready for an agent, add the correct content or a link to where it is defined "
                      "(for example the product screen, release note or API reference)."]
    lines += ["", f"<sub>Automated label from the docfix-gate GitHub Action ({label.get('method')}"
                  f"{', ' + label['model'] if label.get('model') else ''}, confidence {label.get('confidence')}). "
                  "Remove the label if it is wrong.</sub>"]
    return "\n".join(lines)


def route_words(route):
    return {AGENT: "suitable for an AI agent", HUMAN: "needs a person", NONE: "no action"}[route]
