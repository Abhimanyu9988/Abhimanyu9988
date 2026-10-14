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


IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def fetch_image(url):
    """Download an image attached to a GitHub issue. Returns (bytes, content_type)."""
    import urllib.request
    attempts = [{}]
    token = env("GITHUB_TOKEN")
    if token:
        attempts.append({"Authorization": "Bearer " + token})
    last = None
    for extra in attempts:
        req = urllib.request.Request(url, headers=dict({"User-Agent": "profile-social-panel"},
                                                       **extra))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                data = resp.read(MAX_IMAGE_BYTES + 1)
        except Exception as err:  # retry with the token, then give up
            last = err
            continue
        if ctype not in IMAGE_TYPES:
            raise RuntimeError(f"The attachment isn't a JPG, PNG or GIF ({ctype or 'unknown'})")
        if len(data) > MAX_IMAGE_BYTES:
            raise RuntimeError("The image is larger than 8 MB; attach a smaller one")
        return data, ctype
    raise RuntimeError(f"Couldn't download the attached image ({last})")


def _headers(token):
    return {"Authorization": "Bearer " + token, "LinkedIn-Version": api_version(),
            "X-Restli-Protocol-Version": "2.0.0"}


def upload_image(data, ctype, token, author):
    """Upload image bytes to LinkedIn; returns the image URN to attach to a post."""
    _, res, _ = http("POST", "https://api.linkedin.com/rest/images?action=initializeUpload",
                     _headers(token), {"initializeUploadRequest": {"owner": author}})
    value = (res or {}).get("value", {})
    upload_url, image_urn = value.get("uploadUrl"), value.get("image")
    if not (upload_url and image_urn):
        raise RuntimeError("LinkedIn didn't return an upload slot for the image")
    http("PUT", upload_url, {"Authorization": "Bearer " + token, "Content-Type": ctype}, data)
    return image_urn


def publish(text, token, author, image=None, alt_text=""):
    """Publish text (plus an optional (bytes, content_type) image) as a public post.
    Returns the post URL."""
    if len(text) > MAX_CHARS:
        raise ValueError(f"Post is {len(text)} characters; LinkedIn allows {MAX_CHARS}")
    if not (token and author):
        raise RuntimeError("LinkedIn isn't connected")
    body = {
        "author": author,
        "commentary": to_little_text(text),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                         "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    try:
        if image:
            urn = upload_image(image[0], image[1], token, author)
            body["content"] = {"media": {"id": urn,
                                         "altText": (alt_text or text.splitlines()[0])[:300]}}
        status, _, headers = http("POST", "https://api.linkedin.com/rest/posts",
                                  _headers(token), body)
    except HttpError as err:
        if err.status == 401:
            raise RuntimeError("LinkedIn rejected the token (expired or revoked). Comment "
                               "`/linkedin` to renew it from your phone.") from None
        raise RuntimeError(f"LinkedIn error {err.status}: {err.body[:300]}") from None
    urn = {k.lower(): v for k, v in headers.items()}.get("x-restli-id", "")
    if status not in (200, 201) or not urn:
        raise RuntimeError(f"LinkedIn returned {status} without a post id")
    return f"https://www.linkedin.com/feed/update/{urn}/"
