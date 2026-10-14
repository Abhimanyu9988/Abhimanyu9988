"""Draft issues: rendering, reading the draft back, the panel log."""

import json
import re

from common import gh, owner, repo

START, END = "<!-- POST:START -->", "<!-- POST:END -->"
META_RE = re.compile(r"<!-- social:meta (\{.*?\}) -->", re.S)

VERDICT_TEXT = {
    "post": "worth posting",
    "split": "split decision, your call",
    "roundup": "save for the weekly roundup",
    "skip": "not worth posting",
}


OPEN_RE = re.compile(r"\n?<!-- OPEN:START -->.*?<!-- OPEN:END -->\n?", re.S)


def compose_url(text):
    """LinkedIn's (undocumented but widely used) link that opens the post composer
    with the text filled in, so you can add @tags and post it yourself."""
    import urllib.parse
    return "https://www.linkedin.com/feed/?shareActive=true&text=" + urllib.parse.quote(text)


def open_line(text):
    return ("<!-- OPEN:START -->\n**Want to @tag people?** [Open this draft in LinkedIn]"
            f"({compose_url(text)}), type @ before the names, and post it there. Then comment "
            "`/posted <link to the post>` here so the panel knows. If you edit the box above "
            "first, comment `/open` for a fresh link.\n<!-- OPEN:END -->")


def _fence(text):
    ticks = "````" if "```" in text else "```"
    return f"{ticks}text\n{text}\n{ticks}"


def _scores(transcript):
    final = {}
    for t in transcript:
        if t.get("score") is not None:
            final[t["who"]] = t  # later rounds overwrite earlier ones
    return ", ".join(f"{who} {t['verdict']} {t['score']}/10" for who, t in final.items())


def _discussion(transcript):
    lines = []
    for t in transcript:
        rnd = t["round"]
        rnd = f"round {rnd}" if isinstance(rnd, int) else rnd
        if t.get("error"):
            lines.append(f"- **{t['who']}, {rnd}:** failed ({t['error']})")
            continue
        head = t.get("verdict", "")
        if t.get("score") is not None:
            head += f" ({t['score']}/10)"
        reason = (t.get("reason") or "").replace("\n", " ")
        lines.append(f"- **{t['who']}, {rnd}:** {head}. {reason}".replace(":** .", ":**"))
    return "\n".join(lines)


def render(event, decision):
    meta = {"kind": event["kind"], "id": event["id"], "title": event["title"],
            "ref": event.get("ref"), "short": event.get("short", "")}
    verdict = decision["verdict"]
    scores = _scores(decision["transcript"])
    parts = [f"<!-- social:meta {json.dumps(meta)} -->",
             f"**AI panel:** {VERDICT_TEXT.get(verdict, verdict)}"
             + (f" · {scores}" if scores else "")]
    if verdict == "split":
        parts.append("\nThe editors disagreed and there was no tiebreaker. Read the "
                     "discussion below, then post it or close this issue.")
    if decision.get("note"):
        parts.append(decision["note"])
    parts.append("\n**To publish:** edit the text in the box if you like, then comment "
                 "`/post`. It goes out right away as a public post from your own profile.\n"
                 "**Picture (optional):** attach one in a comment first; `/post` includes the "
                 "latest one you attached.\n"
                 "Other commands: `/redraft make it warmer` · `/roundup` (save for Friday's "
                 "roundup) · `/post noimage` · close this issue to drop it.\n")
    parts += [START, _fence(decision["draft"]), END, open_line(decision["draft"]), ""]
    if decision.get("alternative"):
        who = decision.get("alt_by") or "other editor"
        parts += [f"<details><summary>Alternative draft ({who})</summary>\n",
                  _fence(decision["alternative"]), "</details>\n"]
    if decision["transcript"]:
        parts += ["<details><summary>Panel discussion</summary>\n",
                  _discussion(decision["transcript"]), "</details>\n"]
    if event.get("ref"):
        parts.append(f"Source: {event['ref']}")
    if decision.get("cost"):
        parts.append(f"<sub>{decision['cost']}</sub>")
    return "\n".join(parts)


def read_draft(body):
    if START not in body or END not in body:
        raise ValueError("This issue has no draft block (POST:START/POST:END markers)")
    inner = body.split(START, 1)[1].split(END, 1)[0].strip()
    inner = re.sub(r"^`{3,}[a-z]*\s*\n", "", inner)
    inner = re.sub(r"\n`{3,}\s*$", "", inner)
    return inner.strip()


def replace_draft(body, new_draft, alternative=None, alt_by=None):
    head, rest = body.split(START, 1)
    _, tail = rest.split(END, 1)
    tail = re.sub(r"<details><summary>Alternative draft.*?</details>\n?", "", tail, flags=re.S)
    tail = OPEN_RE.sub("\n", tail)
    alt = ""
    if alternative:
        alt = (f"\n<details><summary>Alternative draft ({alt_by or 'other editor'})</summary>\n\n"
               f"{_fence(alternative)}\n</details>\n")
    return (head + START + "\n" + _fence(new_draft) + "\n" + END + "\n" + open_line(new_draft)
            + "\n" + alt + tail)


def read_meta(body):
    m = META_RE.search(body or "")
    return json.loads(m.group(1)) if m else {}


def create_draft_issue(event, decision):
    labels = ["linkedin-draft"] + (["panel-split"] if decision["verdict"] == "split" else [])
    title = f"LinkedIn draft: {event['title']}"[:240]
    issue = gh("POST", f"/repos/{repo()}/issues", {
        "title": title, "body": render(event, decision),
        "labels": labels, "assignees": [owner()]})
    return issue["html_url"]


def panel_log_issue():
    found = gh("GET", f"/repos/{repo()}/issues",
               params={"labels": "panel-log", "state": "open", "per_page": 1})
    if found:
        return found[0]["number"]
    issue = gh("POST", f"/repos/{repo()}/issues", {
        "title": "AI panel log",
        "body": ("The AI panel notes here what it decided not to post, or saved for the "
                 "weekly roundup.\n\nTo post something anyway, comment "
                 "`/draft <link or a few words about it>` on this issue."),
        "labels": ["panel-log"]})
    return issue["number"]


def log_decision(event, decision):
    verdict = decision["verdict"]
    text = (f"**{VERDICT_TEXT.get(verdict, verdict).capitalize()}:** {event['title']}"
            + (f"\n{event['ref']}" if event.get("ref") else "")
            + f"\n\n{_discussion(decision['transcript'])}")
    if decision.get("cost"):
        text += f"\n\n<sub>{decision['cost']}</sub>"
    if verdict == "skip":
        ref = event.get("ref") or event["title"]
        text += f"\n\nTo post it anyway, comment: `/draft {ref}`"
    gh("POST", f"/repos/{repo()}/issues/{panel_log_issue()}/comments", {"body": text})


def comment(number, text):
    gh("POST", f"/repos/{repo()}/issues/{number}/comments", {"body": text})


def react(comment_id, content):
    try:
        gh("POST", f"/repos/{repo()}/issues/comments/{comment_id}/reactions",
           {"content": content})
    except Exception:
        pass
