"""The few GitHub REST calls the action needs, with the standard library only."""
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


class GitHubError(Exception):
    pass


def _call(method, path, token, body=None, tries=4):
    """One REST call, retried with backoff on timeouts, connection errors and server errors."""
    for attempt in range(tries):
        try:
            return _call_once(method, path, token, body)
        except GitHubError as e:
            if attempt == tries - 1 or not str(e).split("HTTP ")[-1][:1] in ("5",):
                raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            if attempt == tries - 1:
                raise GitHubError(f"{method} {path}: {type(e).__name__}") from None
        time.sleep(2 * (attempt + 1))


def _call_once(method, path, token, body=None):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "docfix-gate")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if method == "DELETE" and e.code == 404:
            return None
        raise GitHubError(f"{method} {path}: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}") from None


def add_labels(repo, number, labels, token):
    if labels:
        _call("POST", f"/repos/{repo}/issues/{number}/labels", token, {"labels": labels})


def remove_label(repo, number, label, token):
    _call("DELETE", f"/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}", token)


def upsert_comment(repo, number, body, marker, token):
    """Edit this action's earlier comment if there is one, so an edited issue does not collect comments."""
    page = 1
    while True:
        comments = _call("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}", token) or []
        for c in comments:
            if marker in (c.get("body") or ""):
                _call("PATCH", f"/repos/{repo}/issues/comments/{c['id']}", token, {"body": body})
                return "updated"
        if len(comments) < 100:
            break
        page += 1
    _call("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
    return "created"


def list_issues(repo, token, state="all", limit=100):
    """Issues (not pull requests), newest first, for a read-only scan."""
    out, page = [], 1
    while len(out) < limit:
        batch = _call("GET", f"/repos/{repo}/issues?state={state}&per_page=100&page={page}", token) or []
        out += [i for i in batch if "pull_request" not in i]
        if len(batch) < 100:
            break
        page += 1
    return out[:limit]
