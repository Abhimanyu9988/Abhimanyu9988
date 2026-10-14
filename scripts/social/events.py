"""Builds the events the panel discusses: merged PRs, weekly roundups, ORCID
publications and peer reviews, new GitHub org memberships, manual milestones."""

import datetime as dt
import re

from common import HttpError, env, gh, gh_search, http, load, log, owner, save, trim, exists

KNOWN_JOURNALS = {"issn:2169-3536": "IEEE Access"}


def _user_name(login, cache={}):
    if login not in cache:
        try:
            info = gh("GET", f"/users/{login}") or {}
            cache[login] = info.get("name") or login
        except HttpError:
            cache[login] = login
    return cache[login]


# ---------------------------------------------------------------- merged PRs

def _linked_issue_numbers(body):
    nums = re.findall(r"(?i)\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?|part of|related to|"
                      r"addresses|for)\s*:?\s*#(\d+)", body or "")
    nums += re.findall(r"github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", body or "")
    out = []
    for n in nums:
        if n not in out:
            out.append(n)
    return out[:2]


def describe_pr(repo, number):
    """Text describing a PR, its linked issues and its reviewers."""
    pr = gh("GET", f"/repos/{repo}/pulls/{number}")
    lines = [f"Repository: {repo}", f"PR #{number}: {pr['title']}",
             f"URL: {pr['html_url']}", f"State: {'merged' if pr.get('merged_at') else pr['state']}"]
    if pr.get("merged_at"):
        merger = (pr.get("merged_by") or {}).get("login")
        lines.append(f"Merged on {pr['merged_at'][:10]}"
                     + (f" by {_user_name(merger)} (@{merger})" if merger else ""))
    lines.append(f"Size: {pr.get('changed_files', '?')} files, +{pr.get('additions', '?')} "
                 f"-{pr.get('deletions', '?')} lines")
    labels = [l["name"] for l in pr.get("labels", [])]
    if labels:
        lines.append("Labels: " + ", ".join(labels))
    try:
        files = gh("GET", f"/repos/{repo}/pulls/{number}/files", params={"per_page": 30})
        lines.append("Files: " + ", ".join(f["filename"] for f in files[:30]))
    except HttpError:
        pass
    try:
        reviews = gh("GET", f"/repos/{repo}/pulls/{number}/reviews", params={"per_page": 100})
        approvers = []
        for r in reviews:
            login = (r.get("user") or {}).get("login")
            if r.get("state") == "APPROVED" and login and login not in approvers \
                    and login != pr["user"]["login"]:
                approvers.append(login)
        if approvers:
            lines.append("Approved by: " + ", ".join(
                f"{_user_name(a)} (@{a})" for a in approvers[:5]))
    except HttpError:
        pass
    lines.append("\nPR description:\n" + trim(pr.get("body") or "(none)", 3500))
    for num in _linked_issue_numbers(pr.get("body")):
        try:
            issue = gh("GET", f"/repos/{repo}/issues/{num}")
            lines.append(f"\nLinked issue #{num}: {issue['title']}\n"
                         + trim(issue.get("body") or "", 1500))
        except HttpError:
            pass
    return "\n".join(lines)


def merged_pr_event(entry):
    return {
        "kind": "merged_pr",
        "id": entry["url"],
        "title": f"Merged PR in {entry['repo']}: {entry['title']}",
        "context": describe_pr(entry["repo"], entry["number"]),
        "ref": entry["url"],
        "short": f"{entry['repo'].split('/')[-1]}#{entry['number']}",
    }


# ---------------------------------------------------------------- weekly roundup

def weekly_event(orgs, days=7):
    user = owner()
    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    since = since_dt.strftime("%Y-%m-%d")
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = {"Merged PRs": [], "New PRs opened": [], "PRs reviewed": [],
                "Issues opened": [], "Issues and PRs discussed": []}
    seen = set()

    def add(section, item, extra=""):
        key = (section, item["html_url"])
        if key in seen:
            return
        seen.add(key)
        repo = item["repository_url"].split("/repos/")[-1]
        sections[section].append(f"- {repo}#{item['number']}: {item['title']}{extra} "
                                 f"({item['html_url']})")

    for org in orgs:
        for it in gh_search(f"is:pr author:{user} org:{org} merged:>={since}"):
            add("Merged PRs", it)
        for it in gh_search(f"is:pr author:{user} org:{org} created:>={since}"):
            if not (it.get("pull_request") or {}).get("merged_at"):
                add("New PRs opened", it)
        for it in gh_search(f"is:pr reviewed-by:{user} -author:{user} org:{org} updated:>={since}"):
            repo = it["repository_url"].split("/repos/")[-1]
            try:
                reviews = gh("GET", f"/repos/{repo}/pulls/{it['number']}/reviews",
                             params={"per_page": 100})
            except HttpError:
                continue
            states = [r["state"].lower() for r in reviews
                      if (r.get("user") or {}).get("login", "").lower() == user.lower()
                      and (r.get("submitted_at") or "") >= since_iso]
            if states:
                verdict = "approved" if "approved" in states else "reviewed"
                add("PRs reviewed", it, f" [{verdict}]")
        for it in gh_search(f"is:issue author:{user} org:{org} created:>={since}"):
            add("Issues opened", it)
        for it in gh_search(f"commenter:{user} -author:{user} org:{org} updated:>={since}"):
            repo = it["repository_url"].split("/repos/")[-1]
            try:
                comments = gh("GET", f"/repos/{repo}/issues/{it['number']}/comments",
                              params={"since": since_iso, "per_page": 100})
            except HttpError:
                continue
            if any((c.get("user") or {}).get("login", "").lower() == user.lower()
                   for c in comments):
                add("Issues and PRs discussed", it)

    queue = load("roundup_queue.json", [])
    lines = [f"Activity from {since} to {dt.date.today()} (public GitHub data, orgs: "
             f"{', '.join(orgs)}):"]
    total = 0
    for name, items in sections.items():
        if items:
            total += len(items)
            lines.append(f"\n{name} ({len(items)}):")
            lines.extend(items[:15])
    if queue:
        lines.append("\nItems the panel saved for this roundup:")
        for q in queue:
            lines.append(f"- {q['title']} ({q.get('ref') or 'no link'})\n  Notes: "
                         + trim(q.get("summary", ""), 600))
    return {
        "kind": "weekly_roundup",
        "id": f"weekly-{dt.date.today().isoformat()}",
        "title": f"Weekly roundup, week ending {dt.date.today().isoformat()}",
        "context": trim("\n".join(lines), 9000),
        "allowed": ("post", "skip"),
        "short": f"roundup {dt.date.today().isoformat()}",
        "empty": total == 0 and not queue,
    }


# ---------------------------------------------------------------- ORCID

def _find(obj, key):
    """All dicts found under `key` anywhere in a nested JSON structure."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.extend(v if isinstance(v, list) else [v])
            else:
                out.extend(_find(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_find(v, key))
    return [o for o in out if isinstance(o, dict)]


def _val(obj, *path):
    for p in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(p)
    return obj


def _date(d):
    if not isinstance(d, dict):
        return ""
    parts = [_val(d, k, "value") for k in ("year", "month", "day")]
    return "-".join(p for p in parts if p)


def orcid_fetch(orcid):
    base = f"https://pub.orcid.org/v3.0/{orcid}"
    headers = {"Accept": "application/json", "User-Agent": "profile-social-panel"}
    works = http("GET", base + "/works", headers)[1]
    reviews = http("GET", base + "/peer-reviews", headers)[1]
    work_items = []
    for w in _find(works, "work-summary"):
        doi = ""
        for ext in _find(w, "external-id"):
            if str(ext.get("external-id-type", "")).lower() == "doi":
                doi = ext.get("external-id-value", "")
        work_items.append({
            "id": str(w.get("put-code")),
            "title": _val(w, "title", "title", "value") or "(untitled)",
            "journal": _val(w, "journal-title", "value") or "",
            "type": w.get("type", ""),
            "date": _date(w.get("publication-date")),
            "url": _val(w, "url", "value") or (f"https://doi.org/{doi}" if doi else ""),
        })
    review_items = []
    for r in _find(reviews, "peer-review-summary"):
        group = str(r.get("review-group-id", ""))
        review_items.append({
            "id": str(r.get("put-code")),
            "venue": KNOWN_JOURNALS.get(group.lower(), group),
            "organization": _val(r, "convening-organization", "name") or "",
            "date": _date(r.get("completion-date")),
            "role": r.get("reviewer-role", ""),
        })
    # ORCID groups duplicates of the same work; keep one per put-code
    dedup = {w["id"]: w for w in work_items}
    return list(dedup.values()), {r["id"]: r for r in review_items}.values()


def orcid_events():
    orcid = env("ORCID_ID")
    if not orcid:
        return []
    try:
        works, reviews = orcid_fetch(orcid)
    except Exception as err:  # ORCID outages must not break the daily job
        log(f"ORCID check skipped: {err}")
        return []
    reviews = list(reviews)
    first_run = not exists("orcid.json")
    known = load("orcid.json", {"works": [], "reviews": []})
    new_works = [w for w in works if w["id"] not in known["works"]]
    new_reviews = [r for r in reviews if r["id"] not in known["reviews"]]
    save("orcid.json", {"works": sorted(w["id"] for w in works),
                        "reviews": sorted(r["id"] for r in reviews)})
    if first_run:
        log(f"ORCID baseline saved: {len(works)} works, {len(reviews)} reviews")
        return []
    events = []
    for w in new_works:
        events.append({
            "kind": "publication",
            "id": f"orcid-work-{w['id']}",
            "title": f"New publication on ORCID: {w['title']}",
            "context": (f"A new work appeared on his ORCID record ({orcid}).\n"
                        f"Title: {w['title']}\nVenue: {w['journal'] or '-'}\nType: {w['type']}\n"
                        f"Date: {w['date'] or '-'}\nLink: {w['url'] or '-'}\n"
                        f"ORCID: https://orcid.org/{orcid}"),
            "ref": w["url"] or f"https://orcid.org/{orcid}",
            "short": "publication",
        })
    if new_reviews:
        venues = sorted({r["venue"] or r["organization"] or "a journal" for r in new_reviews})
        total = len(reviews)
        events.append({
            "kind": "peer_review",
            "id": "orcid-reviews-" + "-".join(sorted(r["id"] for r in new_reviews)),
            "title": f"{len(new_reviews)} new peer review(s) recorded on ORCID",
            "context": (f"{len(new_reviews)} new verified peer review(s) for: "
                        f"{', '.join(venues)}. Total verified reviews on ORCID: {total}.\n"
                        "Peer review is confidential: never mention the manuscript, its topic "
                        "or its authors; only the venue and the count.\n"
                        f"ORCID: https://orcid.org/{orcid}"),
            "ref": f"https://orcid.org/{orcid}",
            "short": "peer reviews",
        })
    return events


# ---------------------------------------------------------------- org memberships

def org_events():
    user = owner()
    try:
        orgs = gh("GET", f"/users/{user}/orgs", params={"per_page": 100}) or []
    except HttpError as err:
        log(f"Org check skipped: {err}")
        return []
    logins = sorted(o["login"] for o in orgs)
    first_run = not exists("orgs.json")
    known = load("orgs.json", [])
    save("orgs.json", logins)
    if first_run:
        return []
    events = []
    for login in logins:
        if login in known:
            continue
        try:
            info = gh("GET", f"/orgs/{login}") or {}
        except HttpError:
            info = {}
        events.append({
            "kind": "milestone",
            "id": f"org-{login}",
            "title": f"Became a member of the {info.get('name') or login} GitHub organization",
            "context": (f"He is now a public member of the GitHub organization "
                        f"{info.get('name') or login} (https://github.com/{login}).\n"
                        f"Organization description: {info.get('description') or '-'}"),
            "ref": f"https://github.com/{login}",
            "short": f"joined {login}",
        })
    return events


# ---------------------------------------------------------------- manual

URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(pull|issues)/(\d+)")
PR_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s#?)]+)/pull/(\d+)")


def pr_status_lines(texts, limit=12):
    """Current state of every PR linked in the given texts, fetched now."""
    seen, lines = [], []
    for text in texts:
        for repo, num in PR_RE.findall(text or ""):
            if (repo, num) in seen or len(seen) >= limit:
                continue
            seen.append((repo, num))
            try:
                pr = gh("GET", f"/repos/{repo}/pulls/{num}")
            except HttpError:
                continue
            if pr.get("merged_at"):
                state = f"MERGED on {pr['merged_at'][:10]}"
            elif pr.get("state") == "open":
                state = "OPEN (in review, not merged)"
            else:
                state = "CLOSED without merging"
            lines.append(f"- {repo}#{num} {pr['title']}: {state}")
    if not lines:
        return ""
    return ("\nCURRENT STATUS OF THE PULL REQUESTS MENTIONED (checked just now; this overrides "
            "any older wording above):\n" + "\n".join(lines))


def manual_event(title, text, source_url=None):
    parts = [f"In his own words:\n{trim(text, 3000)}"]
    bodies = [text]
    for repo, kind, num in URL_RE.findall(text or "")[:2]:
        try:
            if kind == "pull":
                parts.append("\n" + describe_pr(repo, num))
            else:
                issue = gh("GET", f"/repos/{repo}/issues/{num}")
                bodies.append(issue.get("body") or "")
                parts.append(f"\nReferenced issue {repo}#{num}: {issue['title']} "
                             f"({issue['state']}, written {issue.get('created_at', '')[:10]})\n"
                             + trim(issue.get("body") or "", 2500))
        except HttpError:
            pass
    parts.append(pr_status_lines(bodies))
    return {
        "kind": "milestone",
        "id": source_url or f"manual-{dt.datetime.now().timestamp():.0f}",
        "title": title,
        "context": trim("\n".join(parts), 9000),
        "ref": source_url,
        "short": "milestone",
        "force": True,
    }
