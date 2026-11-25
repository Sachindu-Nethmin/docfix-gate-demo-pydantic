"""Classification without any model: the study's keyword screen and template screen, plus plain cues.

This is the fallback when no model is configured or a model call fails. It is deterministic, free and
sends nothing anywhere, but it is much less accurate than a model (see the README for measured numbers),
so its labels are marked low confidence.
"""
import re

from .codebook import CVD, DC, NOT_DOC, TOOLING

# the study's candidate screen: an issue with none of these words is NOT_DOC
DOC_WORDS = re.compile(r"\b(?:docstrings?|documentation|docs?|readme|typo)\b", re.I)

FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
CHECKLIST = re.compile(r"(?m)^\s*[-*]\s*\[[ xX]\].*$")

DOC = r"(?:docs?|documentation|docstrings?|readme|user guide|api reference|tutorial|changelog)"
DOC_TOOL_REPO = re.compile(r"(?:^|[/_.-])(?:sphinx\w*|sphinxcontrib\w*|numpydoc|pydocstyle|docutils|pdoc\w*|mkdocs\w*|"
                           r"mkdocstrings\w*|docstring[-_]?\w*|readthedocs\w*|doc8|interrogate|breathe|nbsphinx|myst[-_]\w+|"
                           r"furo|jupyter[-_]book)(?:$|[/_.-])", re.I)
CVD_CUES = [
    re.compile(rf"\b(?:the\s+)?{DOC}\s+(?:for\s+\S+\s+)?(?:says?|states?|claims?|mentions?|describes?|indicates?|specif(?:y|ies)|suggests?|shows?|reads?|promises?)\b", re.I),
    re.compile(rf"\b(?:according to|as per|contrary to|unlike|in contrast to|per)\s+(?:the\s+)?(?:\w+\s+)?{DOC}\b", re.I),
    re.compile(rf"\b(?:does(?:n't| not)|doesnt|do not|don't)\s+match\s+(?:the\s+)?(?:\w+\s+)?{DOC}\b", re.I),
    re.compile(rf"\b(?:inconsistent|at odds|disagrees?|conflicts?|contradicts?)\s+with\s+(?:the\s+)?(?:\w+\s+)?{DOC}\b", re.I),
    re.compile(rf"\b{DOC}\b[^.\n]{{0,120}}\b(?:but|however|yet|whereas|while|instead|actually)\b", re.I),
    re.compile(r"\b(?:as|is|are|was)\s+documented\b|\bdocumented\s+(?:behaviou?r|as|to|default)\b", re.I),
]
DC_CUES = [
    re.compile(rf"\b(?:typo|typos|misspell\w*|spelling|grammar)\b[^.\n]{{0,80}}\b{DOC}\b|\b{DOC}\b[^.\n]{{0,80}}\b(?:typo|typos|misspell\w*|spelling|grammar)\b", re.I),
    re.compile(rf"\b{DOC}\b[^.\n]{{0,60}}\b(?:is|are|looks?|seems?)\s+(?:\w+\s+)?(?:wrong|incorrect|outdated|out of date|out-of-date|misleading|unclear|confusing|missing|incomplete|inaccurate|stale|broken)\b", re.I),
    re.compile(rf"\b(?:fix|update|improve|clarify|correct|expand|rewrite|add|document)\b[^.\n]{{0,40}}\b{DOC}\b", re.I),
    re.compile(rf"\b{DOC}\b[^.\n]{{0,60}}\b(?:should|could|needs?\s+to|must)\s+(?:\w+\s+)?(?:mention|say|explain|state|clarify|include|be updated|be clarified|be fixed|be corrected)\b", re.I),
    re.compile(r"\b(?:not documented|undocumented|is not mentioned in the docs?|missing from the docs?|broken links?|dead links?|404)\b", re.I),
]
CODE_BUG_CUES = re.compile(r"Traceback \(most recent call last\)|\b(?:raises?|crash(?:es|ed)?|segfault|regression|exception|stack trace|"
                           r"feature request|add support for|should support|would be nice)\b", re.I)

# documentation repositories
SITE_CUES = re.compile(r"\b(?:navigation|nav ?bar|sidebar|side bar|theme|css|layout|alignment|clipp\w+|search (?:box|bar|results?)|"
                       r"dark mode|responsive|mobile view|build (?:fails?|error|pipeline)|deploy\w*|version (?:switcher|dropdown|selector)|"
                       r"favicon|footer|header|rendering|renders?|page (?:width|title bar))\b", re.I)
AUTO_CUES = re.compile(r"\b(?:broken (?:links?|anchors?|images?)|dead links?|404|page not found|typos?|spelling|misspell\w*|grammar|"
                       r"grammatical|formatting|indentation|markdown (?:error|issue|formatting)|redundant line)\b", re.I)
KNOWLEDGE_CUES = re.compile(r"\b(?:add (?:a |the |new )?(?:documentation|docs?|page|section|guide|tutorial|content)|missing (?:content|page|"
                            r"section|guide|details?|documentation|docs?|images?|examples?|system requirements)|is missing|are missing|"
                            r"not documented|undocumented|update (?:the )?(?:screenshots?|images?|gifs?)|screenshots?|gifs?|review and update|"
                            r"new feature|migration guide|document (?:the|how))\b", re.I)
MISMATCH_CUES = re.compile(r"\b(?:does(?:n't| not)|do not|don't) match (?:the )?(?:ui|product|console|editor|behaviou?r|actual)|"
                           r"\b(?:the )?(?:ui|product|console|editor|form|picker)\b[^.\n]{0,80}\b(?:but|however|whereas|while|instead|actually|"
                           r"offers|shows|lists|has)\b|\b(?:renamed|no longer|out of date|outdated)\b", re.I)


def screen(text):
    """The study's template screen: drop fenced code, template comments and checklist lines."""
    text = FENCE.sub(" ", text or "")
    text = HTML_COMMENT.sub(" ", text)
    return CHECKLIST.sub(" ", text)


def is_candidate(title, body):
    """True if the issue mentions documentation outside code blocks, template comments and checklists."""
    return bool(DOC_WORDS.search(screen((title or "") + "\n" + (body or ""))))


def _first(patterns, text):
    for p in patterns:
        m = p.search(text)
        if m:
            start, end = text.rfind("\n", 0, m.start()) + 1, text.find("\n", m.end())
            line = text[start:end if end != -1 else len(text)].strip()
            return re.sub(r"\s+", " ", line)[:220]
    return ""


def classify_code(repo, title, body):
    """Rules for a code repository. Returns the same fields as a model answer."""
    text = screen((title or "") + "\n" + (body or ""))
    base = {"method": "rules", "confidence": "low", "main_request": "", "repo_is_doc_tool": "no", "doc_source": "none",
            "docs_only_fix": "no", "doc_evidence": "", "fix_check": ""}
    if DOC_TOOL_REPO.search(repo or ""):
        return {**base, "category": TOOLING, "repo_is_doc_tool": "yes", "reason": "the repository is a documentation tool"}
    if not DOC_WORDS.search(text):
        return {**base, "category": NOT_DOC, "confidence": "medium", "reason": "no documentation words outside code, comments and checklists"}
    cvd, dc = _first(CVD_CUES, text), _first(DC_CUES, text)
    bug = bool(CODE_BUG_CUES.search(text))
    if cvd:
        return {**base, "category": CVD, "doc_source": "own", "doc_evidence": cvd, "reason": "the issue sets the documentation against what the code does"}
    if dc and not bug:
        return {**base, "category": DC, "doc_source": "own", "docs_only_fix": "yes", "doc_evidence": dc,
                "reason": "the issue asks for a change to the documentation text"}
    return {**base, "category": NOT_DOC, "reason": "documentation is mentioned, but not as the main request"}


def classify_docs(repo, title, body):
    """Rules for a documentation repository, where almost every issue is about documentation."""
    text = screen((title or "") + "\n" + (body or ""))
    head = (title or "") + "\n" + text[:1500]
    base = {"method": "rules", "confidence": "low", "main_request": "", "doc_evidence": "", "repo_is_doc_tool": "no",
            "doc_source": "own", "docs_only_fix": ""}
    if SITE_CUES.search(title or "") and not AUTO_CUES.search(title or ""):
        return {**base, "category": TOOLING, "fix_check": "review", "reason": "the title describes the documentation site, not its content"}
    if AUTO_CUES.search(head):
        # broken links and typos are named outright and a build or checker confirms the fix, so medium, not low
        return {**base, "category": DC, "fix_check": "automatic", "confidence": "medium",
                "reason": "a build, link check or spell check can confirm this fix"}
    if MISMATCH_CUES.search(head):
        return {**base, "category": CVD, "fix_check": "needs_knowledge", "reason": "the documentation and the product disagree"}
    if KNOWLEDGE_CUES.search(head) or len(text.strip()) < 40:
        return {**base, "category": DC, "fix_check": "needs_knowledge", "reason": "the correct content is not in the issue"}
    return {**base, "category": DC, "fix_check": "review", "reason": "a change to documentation content"}
