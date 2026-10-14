"""Publishing to LinkedIn through the Posts API (Share on LinkedIn, w_member_social)."""

import datetime as dt
import re

from common import HttpError, env, http

RESERVED = set("|{}@[]()<>#*_~\\")
HASHTAG = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_]{0,60})")
URL = re.compile(r"https?://\S+")
MAX_CHARS = 3000


def _escape(text):
    return "".join("\\" + ch if ch in RESERVED else ch for ch in text)


def _escape_keep_hashtags(text):
    out, pos = [], 0
    for m in HASHTAG.finditer(text):
        out.append(_escape(text[pos:m.start()]))
        out.append("{hashtag|\\#|" + _escape(m.group(1)) + "}")
        pos = m.end()
    out.append(_escape(text[pos:]))
    return "".join(out)


def to_little_text(text):
    """Escape LinkedIn's reserved characters; turn #words into real hashtags.
    URLs are escaped as plain text too (LinkedIn still links them)."""
    pieces, pos = [], 0
    for m in URL.finditer(text):
        pieces.append(_escape_keep_hashtags(text[pos:m.start()]))
        pieces.append(_escape(m.group(0)))
        pos = m.end()
    pieces.append(_escape_keep_hashtags(text[pos:]))
    return "".join(pieces)


def api_version():
    """LinkedIn wants a YYYYMM version header; default to two months ago, which is
    always a released version and stays supported for about a year."""
    configured = env("LINKEDIN_VERSION")
    if configured:
        return configured
    today = dt.date.today().replace(day=1)
    month = today.month - 2
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    return f"{year}{month:02d}"


def publish(text):
    """Publish text as a public post. Returns the post URL."""
    if len(text) > MAX_CHARS:
        raise ValueError(f"Post is {len(text)} characters; LinkedIn allows {MAX_CHARS}")
    token = env("LINKEDIN_ACCESS_TOKEN", required=True)
    author = env("LINKEDIN_PERSON_URN", required=True)
    try:
        status, _, headers = http("POST", "https://api.linkedin.com/rest/posts", {
            "Authorization": "Bearer " + token,
            "LinkedIn-Version": api_version(),
            "X-Restli-Protocol-Version": "2.0.0",
        }, {
            "author": author,
            "commentary": to_little_text(text),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        })
    except HttpError as err:
        if err.status == 401:
            raise RuntimeError("LinkedIn rejected the token (expired or revoked). Renew it with: "
                               "python3 scripts/social/linkedin_auth.py") from None
        raise RuntimeError(f"LinkedIn error {err.status}: {err.body[:300]}") from None
    urn = {k.lower(): v for k, v in headers.items()}.get("x-restli-id", "")
    if status not in (200, 201) or not urn:
        raise RuntimeError(f"LinkedIn returned {status} without a post id")
    return f"https://www.linkedin.com/feed/update/{urn}/"
