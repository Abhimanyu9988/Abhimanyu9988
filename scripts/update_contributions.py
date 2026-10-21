#!/usr/bin/env python3
"""Rebuild the "upstream contributions" section of a GitHub profile README.

Reads public GitHub data (PRs, reviews, issues, comments in the configured
orgs) and rewrites everything between the CONTRIB markers in README.md.
Also keeps data/contributions.json so new merged PRs can be detected and
turned into LinkedIn post drafts.

Standard library only. Configuration comes from environment variables:
  CONTRIB_USER   GitHub login (default: GITHUB_REPOSITORY_OWNER)
  CONTRIB_ORGS   comma-separated orgs to include (default: open-telemetry)
  CONTRIB_MAX    rows shown per table (default: 10)
  GITHUB_TOKEN   or GH_TOKEN, used for authenticated API calls
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
START = "<!-- CONTRIB:START -->"
END = "<!-- CONTRIB:END -->"
SEARCH_PAUSE = 2.5  # search API allows 30 requests/minute with a token
NEW_MERGE_DAYS = int(os.environ.get("NEW_MERGE_DAYS", "7"))  # merges older than this get no post


def api_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profile-contrib-log",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    for attempt in range(4):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code in (403, 429) and attempt < 3:
                reset = err.headers.get("x-ratelimit-reset")
                retry = err.headers.get("retry-after")
                if retry:
                    wait = int(retry)
                elif reset:
                    wait = max(1, int(reset) - int(time.time()) + 1)
                else:
                    wait = 30
                wait = min(wait, 90)
                print(f"Rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            body = err.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"GitHub API error {err.code} for {path}: {body}")
    raise SystemExit(f"GitHub API kept rate-limiting {path}")


def _search_pages(query, max_pages):
    items, page = [], 1
    while page <= max_pages:
        data = api_get("/search/issues", {
            "q": query, "sort": "updated", "order": "desc",
            "per_page": 100, "page": page,
        })
        batch = data.get("items", [])
        items.extend(batch)
        time.sleep(SEARCH_PAUSE)
        if len(batch) < 100 or len(items) >= data.get("total_count", 0):
            break
        page += 1
    return items


def search(query, max_pages=3):
    """GitHub has been answering `is:pr` queries with issues only, which hid every merged PR.
    If the answer mixes in the wrong type, ask again without the type qualifier and filter."""
    terms = query.split()
    want = "pr" if "is:pr" in terms else "issue" if "is:issue" in terms else None
    items = _search_pages(query, max_pages)
    if want and any(("pull_request" in i) != (want == "pr") for i in items):
        loose = " ".join(t for t in terms if t not in ("is:pr", "is:issue"))
        items = [i for i in _search_pages(loose, max_pages)
                 if ("pull_request" in i) == (want == "pr")]
    return items


def repo_of(item):
    parts = item["repository_url"].rstrip("/").split("/")
    return parts[-2] + "/" + parts[-1]


def day(ts):
    return (ts or "")[:10]


def collect(user, orgs):
    seen = {}
    buckets = {"authored": [], "reviewed": [], "issues": [], "comments": []}
    queries = {
        "authored": "is:pr author:{u} org:{o}",
        "reviewed": "is:pr reviewed-by:{u} -author:{u} org:{o}",
        "issues": "is:issue author:{u} org:{o}",
        "comments": "commenter:{u} -author:{u} org:{o}",
    }
    for org in orgs:
        for kind, q in queries.items():
            for item in search(q.format(u=user, o=org)):
                key = (kind, item["html_url"])
                if key in seen:
                    continue
                seen[key] = True
                buckets[kind].append(item)

    reviewed_urls = {i["html_url"] for i in buckets["reviewed"]}
    buckets["comments"] = [i for i in buckets["comments"]
                           if i["html_url"] not in reviewed_urls]

    merged, open_prs = [], []
    for item in buckets["authored"]:
        pr = item.get("pull_request") or {}
        entry = {
            "repo": repo_of(item), "number": item["number"],
            "title": item["title"], "url": item["html_url"],
        }
        if pr.get("merged_at"):
            entry["date"] = day(pr["merged_at"])
            merged.append(entry)
        elif item["state"] == "open":
            entry["date"] = day(item["created_at"])
            open_prs.append(entry)
    merged.sort(key=lambda e: (e["date"], e["url"]), reverse=True)
    open_prs.sort(key=lambda e: (e["date"], e["url"]), reverse=True)

    reviews = []
    for item in buckets["reviewed"]:
        reviews.append({
            "repo": repo_of(item), "number": item["number"],
            "title": item["title"], "url": item["html_url"],
            "updated": item["updated_at"],
        })
    reviews.sort(key=lambda e: e["updated"], reverse=True)

    discussions = []
    for kind, role in (("issues", "opened"), ("comments", "commented")):
        for item in buckets[kind]:
            discussions.append({
                "repo": repo_of(item), "number": item["number"],
                "title": item["title"], "url": item["html_url"],
                "role": role,
                "type": "PR" if item.get("pull_request") else "issue",
                "updated": item["updated_at"],
            })
    discussions.sort(key=lambda e: e["updated"], reverse=True)

    return {
        "merged": merged, "open": open_prs, "reviews": reviews,
        "discussions": discussions,
        "issues_opened": len(buckets["issues"]),
    }


def add_review_dates(user, reviews, limit):
    """Look up when the user actually reviewed each PR, for the rows shown."""
    shown = []
    for entry in reviews[: limit * 2]:
        owner_repo, number = entry["repo"], entry["number"]
        data = api_get(f"/repos/{owner_repo}/pulls/{number}/reviews",
                       {"per_page": 100})
        dates = [r.get("submitted_at") for r in data
                 if (r.get("user") or {}).get("login", "").lower() == user.lower()
                 and r.get("submitted_at")]
        if dates:
            entry = dict(entry, date=day(max(dates)))
            shown.append(entry)
    shown.sort(key=lambda e: (e["date"], e["url"]), reverse=True)
    return shown[:limit]


def esc(text):
    for ch in ("\\", "|", "[", "]", "<", ">"):
        text = text.replace(ch, "\\" + ch)
    return text.strip()


def short_repo(repo):
    return repo.split("/", 1)[1] if "/" in repo else repo


def search_link(user, orgs, qualifier):
    q = " ".join([qualifier.format(u=user)] + [f"org:{o}" for o in orgs])
    return "https://github.com/search?type=pullrequests&q=" + urllib.parse.quote(q)


def render(user, orgs, data, review_rows, limit):
    repos = {e["repo"] for key in ("merged", "open", "reviews", "discussions")
             for e in data[key]}
    def n(count, one, many):
        return f"**{count}** {one if count == 1 else many}"

    stats = [
        n(len(data["merged"]), "merged PR", "merged PRs"),
        n(len(data["open"]), "open PR", "open PRs"),
        n(len(data["reviews"]), "PR reviewed", "PRs reviewed"),
        n(data["issues_opened"], "issue opened", "issues opened"),
        n(len(repos), "repository", "repositories"),
    ]
    out = [START, "", "### Upstream open-source contributions", "",
           " · ".join(stats), ""]

    def table(title, header, rows, more_count, more_url):
        if not rows:
            return
        out.extend([f"#### {title}", "", header,
                    "|" + "---|" * header.count("|", 1), ])
        out.extend(rows)
        if more_count > 0:
            out.append("")
            out.append(f"[…and {more_count} more]({more_url})")
        out.append("")

    def pr_row(e):
        return (f"| {e['date']} | {short_repo(e['repo'])} | "
                f"[{esc(e['title'])}]({e['url']}) |")

    table("Merged pull requests", "| Merged | Repository | Pull request |",
          [pr_row(e) for e in data["merged"][:limit]],
          len(data["merged"]) - limit,
          search_link(user, orgs, "is:pr is:merged author:{u}"))
    table("In review", "| Opened | Repository | Pull request |",
          [pr_row(e) for e in data["open"][:limit]],
          len(data["open"]) - limit,
          search_link(user, orgs, "is:pr is:open author:{u}"))
    table("Code reviews", "| Reviewed | Repository | Pull request |",
          [pr_row(e) for e in review_rows],
          len(data["reviews"]) - len(review_rows),
          search_link(user, orgs, "is:pr reviewed-by:{u} -author:{u}"))
    table("Issues and discussions", "| Repository | Issue or PR | Role |",
          [f"| {short_repo(e['repo'])} | [{esc(e['title'])}]({e['url']}) "
           f"| {e['role']} |" for e in data["discussions"][:limit]],
          len(data["discussions"]) - limit,
          search_link(user, orgs, "commenter:{u}").replace(
              "type=pullrequests", "type=issues"))

    out.append("<sub>Updated automatically every day from public GitHub "
               "activity.</sub>")
    out.extend(["", END])
    return "\n".join(out)


def replace_section(readme, section):
    if START not in readme or END not in readme:
        raise SystemExit(f"README is missing the {START} / {END} markers")
    head, rest = readme.split(START, 1)
    _, tail = rest.split(END, 1)
    return head + section + tail


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--readme", default="README.md")
    ap.add_argument("--data", default="data/contributions.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the section, write nothing")
    ap.add_argument("--new-merged-out",
                    help="write PRs merged since the last run to this JSON file")
    args = ap.parse_args()

    user = os.environ.get("CONTRIB_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER")
    if not user:
        raise SystemExit("Set CONTRIB_USER to your GitHub login")
    orgs = [o.strip() for o in
            os.environ.get("CONTRIB_ORGS", "open-telemetry").split(",") if o.strip()]
    limit = int(os.environ.get("CONTRIB_MAX", "10"))

    data = collect(user, orgs)
    review_rows = add_review_dates(user, data["reviews"], limit)
    section = render(user, orgs, data, review_rows, limit)

    if args.dry_run:
        print(section)
        return

    with open(args.readme, encoding="utf-8") as fh:
        readme = fh.read()
    updated = replace_section(readme, section)
    if updated != readme:
        with open(args.readme, "w", encoding="utf-8") as fh:
            fh.write(updated)

    previous = None
    if os.path.exists(args.data):
        with open(args.data, encoding="utf-8") as fh:
            previous = json.load(fh)
    snapshot = {"user": user, "orgs": orgs, "merged": data["merged"],
                "open": data["open"]}
    os.makedirs(os.path.dirname(args.data) or ".", exist_ok=True)
    with open(args.data, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)
        fh.write("\n")

    if args.new_merged_out:
        new = []
        if previous is not None:
            known = {e["url"] for e in previous.get("merged", [])}
            # Only recent merges get a post: the first run after a gap (or after a fix)
            # must not open a draft for every PR from months ago.
            recent = (dt.date.today() - dt.timedelta(days=NEW_MERGE_DAYS)).isoformat()
            new = [e for e in data["merged"]
                   if e["url"] not in known and e.get("date", "") >= recent]
        with open(args.new_merged_out, "w", encoding="utf-8") as fh:
            json.dump(new, fh, indent=2)
        print(f"{len(new)} newly merged PR(s)")


if __name__ == "__main__":
    main()
