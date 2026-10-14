"""Shared helpers: HTTP, GitHub API, JSON state files. Standard library only."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_API = "https://api.github.com"


class HttpError(Exception):
    def __init__(self, status, body, headers=None):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body
        self.headers = headers or {}


def http(method, url, headers=None, body=None, timeout=120):
    """Send a request; return (status, parsed JSON or text, response headers)."""
    data = None
    headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _parse(raw), dict(resp.headers)
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        raise HttpError(err.code, raw, dict(err.headers)) from None


def http_bytes(url, headers=None, timeout=60, limit=10 * 1024 * 1024):
    """GET binary content. Returns (bytes, content_type)."""
    req = urllib.request.Request(url, headers=dict(headers or {}, **{"User-Agent": "profile-social-panel"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(limit + 1)
            if len(data) > limit:
                raise HttpError(413, f"image larger than {limit // (1024 * 1024)} MB")
            return data, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        raise HttpError(err.code, err.read().decode("utf-8", "replace")) from None


def _parse(raw):
    try:
        return json.loads(raw) if raw.strip() else None
    except ValueError:
        return raw


def env(name, default=None, required=False):
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise SystemExit(f"Missing required setting: {name}")
        return default
    return value


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- GitHub

def _gh_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profile-social-panel",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def gh(method, path, body=None, params=None):
    url = path if path.startswith("http") else GITHUB_API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            return http(method, url, _gh_headers(), body)[1]
        except HttpError as err:
            if err.status in (403, 429) and attempt < 3 and (
                    "rate limit" in err.body.lower() or "retry-after" in
                    {k.lower() for k in err.headers}):
                wait = int(err.headers.get("Retry-After") or err.headers.get("retry-after") or 30)
                log(f"GitHub rate limit, waiting {min(wait, 90)}s")
                time.sleep(min(wait, 90))
                continue
            raise
    raise SystemExit("GitHub kept rate-limiting")


def gh_search(query, limit=100):
    data = gh("GET", "/search/issues",
              params={"q": query, "sort": "updated", "order": "desc",
                      "per_page": min(limit, 100)})
    time.sleep(float(os.environ.get("SEARCH_PAUSE", "2.5")))
    return (data or {}).get("items", [])


def repo():
    return env("GITHUB_REPOSITORY", required=True)


def owner():
    return env("GITHUB_REPOSITORY_OWNER") or repo().split("/")[0]


def ensure_label(name, color, description):
    try:
        gh("POST", f"/repos/{repo()}/labels",
           {"name": name, "color": color, "description": description})
    except HttpError as err:
        if err.status != 422:  # 422 = already exists
            raise


LABELS = {
    "linkedin-draft": ("0A66C2", "Draft LinkedIn post waiting for you"),
    "linkedin-posted": ("057642", "Published to LinkedIn"),
    "panel-split": ("D93F0B", "AI panel disagreed; your call"),
    "panel-log": ("BFBFBF", "AI panel decisions"),
    "milestone": ("FBCA04", "A milestone to share; the panel drafts a post"),
    "linkedin-auth": ("B60205", "LinkedIn connection needs renewing"),
}


def ensure_labels():
    for name, (color, desc) in LABELS.items():
        ensure_label(name, color, desc)


# ---------------------------------------------------------------- state

DATA_DIR = "data"


def load(name, default):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save(name, value):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def exists(name):
    return os.path.exists(os.path.join(DATA_DIR, name))


def trim(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n[…truncated]"
