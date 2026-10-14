#!/usr/bin/env python3
"""Entry point for the AI panel jobs.

  run.py daily --new-merged FILE   merged PRs + ORCID + org memberships + token reminder
  run.py weekly                    Friday roundup
  run.py command                   handle /post, /redraft, /draft, /roundup (issue_comment)
  run.py milestone                 an issue labelled "milestone" was opened (issues event)
  run.py selftest claude|openai|gemini
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import events  # noqa: E402
import issues  # noqa: E402
import linkedin  # noqa: E402
import llm  # noqa: E402
import panel  # noqa: E402
from common import (HttpError, ensure_labels, env, gh, load, log, owner,  # noqa: E402
                    repo, save)

STYLE_FILE = "social/style.md"


def style():
    try:
        with open(STYLE_FILE, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return "Write in first person, plainly, without hype. Do not mention my employer."


def recent_posts():
    return load("posts.json", [])


def orgs():
    return [o.strip() for o in env("CONTRIB_ORGS", "open-telemetry").split(",") if o.strip()]


def ai_ready():
    return llm.available("claude") or llm.available("openai")


def template_decision(event):
    return {
        "verdict": "post",
        "draft": (f"{event['title']}\n\n[One or two lines on the problem it solves and who it "
                  f"helps.]\n\n[What you reproduced or verified, and how.]\n\n"
                  f"{event.get('ref') or ''}\n\n#OpenTelemetry #OpenSource #Observability"),
        "alternative": None, "transcript": [], "cost": "",
        "note": "No AI keys are configured, so this is a fill-in template.",
    }


def handle(event, handled):
    """Run the panel on one event and act on the verdict."""
    if event["id"] in handled:
        log(f"Already handled: {event['id']}")
        return
    if not ai_ready():
        if event["kind"] == "merged_pr":
            url = issues.create_draft_issue(event, template_decision(event))
            log(f"Template draft: {url}")
        handled.append(event["id"])
        return
    try:
        decision = panel.debate(event, style(), recent_posts())
    except Exception as err:  # never lose an event because an API failed
        log(f"Panel failed for {event['id']}: {err}")
        decision = template_decision(event)
        decision["note"] = f"The AI panel failed ({str(err)[:200]}), so this is a template."
    verdict = decision["verdict"]
    if verdict in ("post", "split"):
        url = issues.create_draft_issue(event, decision)
        log(f"Draft ({verdict}): {url}")
    else:
        if verdict == "roundup":
            queue = load("roundup_queue.json", [])
            queue.append({"title": event["title"], "ref": event.get("ref"),
                          "summary": decision["draft"][:1200],
                          "added": dt.date.today().isoformat()})
            save("roundup_queue.json", queue)
        issues.log_decision(event, decision)
        log(f"{verdict}: {event['title']}")
    handled.append(event["id"])


def token_reminder():
    expires = env("LINKEDIN_TOKEN_EXPIRES")
    if not expires:
        return
    days = (int(expires) - time.time()) / 86400
    if days > 7:
        return
    open_ones = gh("GET", f"/repos/{repo()}/issues",
                   params={"labels": "linkedin-auth", "state": "open", "per_page": 1})
    if open_ones:
        return
    when = "has expired" if days <= 0 else f"expires in {max(1, int(days))} day(s)"
    gh("POST", f"/repos/{repo()}/issues", {
        "title": f"LinkedIn connection {when}",
        "labels": ["linkedin-auth"], "assignees": [owner()],
        "body": ("LinkedIn posting tokens last 60 days. To renew, run this on your Mac "
                 "inside your profile repository folder, then close this issue:\n\n"
                 "```\npython3 scripts/social/linkedin_auth.py\n```")})


def cmd_daily(args):
    ensure_labels()
    handled = load("handled.json", [])
    evs = []
    if args.new_merged and os.path.exists(args.new_merged):
        with open(args.new_merged, encoding="utf-8") as fh:
            for entry in json.load(fh):
                try:
                    evs.append(events.merged_pr_event(entry))
                except HttpError as err:
                    log(f"Could not read {entry['url']}: {err}")
    evs += events.orcid_events()
    evs += events.org_events()
    for ev in evs:
        handle(ev, handled)
    save("handled.json", handled[-500:])
    token_reminder()


def cmd_weekly(args):
    ensure_labels()
    ev = events.weekly_event(orgs())
    if ev["empty"]:
        log("Quiet week: nothing to round up")
        return
    handled = load("handled.json", [])
    handle(ev, handled)
    save("handled.json", handled[-500:])
    save("roundup_queue.json", [])


def _event_payload():
    with open(env("GITHUB_EVENT_PATH", required=True), encoding="utf-8") as fh:
        return json.load(fh)


def _event_from_meta(meta):
    ref = meta.get("ref") or ""
    m = events.URL_RE.search(ref)
    context = meta.get("title", "")
    if m and m.group(2) == "pull":
        try:
            context = events.describe_pr(m.group(1), m.group(3))
        except HttpError:
            pass
    return {"kind": meta.get("kind", "milestone"), "id": meta.get("id", ref),
            "title": meta.get("title", ""), "ref": ref, "context": context,
            "short": meta.get("short", "")}


def cmd_command(args):
    payload = _event_payload()
    c = payload["comment"]
    issue = payload["issue"]
    if c["user"]["login"].lower() != owner().lower():
        log("Comment is not from the repository owner; ignoring")
        return
    text = c["body"].strip()
    word = text.split(None, 1)[0].lower() if text else ""
    rest = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    labels = {l["name"] for l in issue.get("labels", [])}
    number = issue["number"]
    ensure_labels()

    if word == "/post":
        if "linkedin-draft" not in labels:
            issues.comment(number, "`/post` only works on an issue labelled `linkedin-draft`.")
            return
        issues.react(c["id"], "eyes")
        body = gh("GET", f"/repos/{repo()}/issues/{number}")["body"] or ""
        draft = issues.read_draft(body)
        if not env("LINKEDIN_ACCESS_TOKEN"):
            issues.comment(number, "LinkedIn isn't connected yet, so I can't publish. Copy the "
                                   "text above into LinkedIn, or connect posting by running "
                                   "`python3 scripts/social/linkedin_auth.py` on your Mac.")
            return
        try:
            url = linkedin.publish(draft)
        except (RuntimeError, ValueError) as err:
            issues.comment(number, f"Publishing failed: {err}")
            raise SystemExit(1)
        meta = issues.read_meta(body)
        posts = load("posts.json", [])
        posts.append({"date": dt.date.today().isoformat(), "url": url,
                      "kind": meta.get("kind"), "ref": meta.get("ref"),
                      "first_line": draft.splitlines()[0][:160] if draft else ""})
        save("posts.json", posts[-200:])
        issues.comment(number, f"Published on LinkedIn: {url}")
        gh("PATCH", f"/repos/{repo()}/issues/{number}",
           {"state": "closed", "labels": sorted((labels - {"linkedin-draft", "panel-split"})
                                                | {"linkedin-posted"})})
        issues.react(c["id"], "rocket")

    elif word == "/redraft":
        if "linkedin-draft" not in labels:
            issues.comment(number, "`/redraft` only works on an issue labelled `linkedin-draft`.")
            return
        if not rest:
            issues.comment(number, "Say what to change, for example: `/redraft shorter, and "
                                   "thank the reviewers by name`")
            return
        issues.react(c["id"], "eyes")
        body = gh("GET", f"/repos/{repo()}/issues/{number}")["body"] or ""
        event = _event_from_meta(issues.read_meta(body))
        result = panel.revise(issues.read_draft(body), rest, event, style(), recent_posts())
        gh("PATCH", f"/repos/{repo()}/issues/{number}",
           {"body": issues.replace_draft(body, result["draft"], result.get("alternative"),
                                         result.get("alt_by"))})
        changes = "\n".join(f"- **{t['who']}:** {t.get('reason') or t.get('error', '')}"
                            for t in result["transcript"])
        issues.comment(number, "Redrafted; the box above has the new text"
                               + (f" (by {result['main_by']})" if result.get("main_by") else "")
                               + f".\n\n{changes}\n\n<sub>{result['cost']}</sub>")
        issues.react(c["id"], "rocket")

    elif word == "/draft":
        if not rest:
            issues.comment(number, "Add a link or a few words, for example: "
                                   "`/draft my talk proposal was accepted <link>`")
            return
        issues.react(c["id"], "eyes")
        first = rest.splitlines()[0]
        url = events.URL_RE.search(rest)
        ev = events.manual_event(first[:120], rest, url.group(0) if url else None)
        ev["id"] = f"draft-{c['id']}"
        decision = panel.debate(ev, style(), recent_posts()) if ai_ready() \
            else template_decision(ev)
        link = issues.create_draft_issue(ev, decision)
        issues.comment(number, f"Draft ready: {link}")
        issues.react(c["id"], "rocket")

    elif word == "/roundup":
        body = gh("GET", f"/repos/{repo()}/issues/{number}")["body"] or ""
        meta = issues.read_meta(body)
        queue = load("roundup_queue.json", [])
        queue.append({"title": meta.get("title") or issue["title"], "ref": meta.get("ref"),
                      "summary": issues.read_draft(body)[:1200] if issues.START in body else "",
                      "added": dt.date.today().isoformat()})
        save("roundup_queue.json", queue)
        issues.comment(number, "Saved for Friday's roundup.")
        gh("PATCH", f"/repos/{repo()}/issues/{number}", {"state": "closed"})
        issues.react(c["id"], "+1")


def cmd_milestone(args):
    payload = _event_payload()
    issue = payload["issue"]
    if issue["user"]["login"].lower() != owner().lower():
        return
    labels = {l["name"] for l in issue.get("labels", [])}
    if "milestone" not in labels or "linkedin-draft" in labels:
        return
    ev = events.manual_event(issue["title"], f"{issue['title']}\n\n{issue.get('body') or ''}",
                             issue["html_url"])
    decision = panel.debate(ev, style(), recent_posts()) if ai_ready() \
        else template_decision(ev)
    original = issue.get("body") or ""
    body = issues.render(ev, decision) + (
        f"\n\n<details><summary>Your original note</summary>\n\n{original}\n</details>"
        if original else "")
    gh("PATCH", f"/repos/{repo()}/issues/{issue['number']}",
       {"body": body, "labels": sorted(labels | {"linkedin-draft"}),
        "assignees": [owner()]})


def cmd_selftest(args):
    try:
        ok, model = llm.selftest(args.provider)
    except Exception as err:
        print(f"  {err}")
        sys.exit(1)
    print(f"{llm.PROVIDERS[args.provider]['label']} ({model}): {'OK' if ok else 'unexpected reply'}")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily")
    d.add_argument("--new-merged")
    sub.add_parser("weekly")
    sub.add_parser("command")
    sub.add_parser("milestone")
    s = sub.add_parser("selftest")
    s.add_argument("provider", choices=list(llm.PROVIDERS))
    args = ap.parse_args()
    {"daily": cmd_daily, "weekly": cmd_weekly, "command": cmd_command,
     "milestone": cmd_milestone, "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    main()
