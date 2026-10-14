"""Where the LinkedIn token lives, and phone-based renewal.

GitHub Actions can't update repository secrets with its built-in token, so a token
renewed from your phone is stored encrypted in data/linkedin_token.enc (AES-256,
key = the LINKEDIN_TOKEN_KEY secret). Publishing uses whichever token is newer:
that file, or the LINKEDIN_ACCESS_TOKEN secret set from your Mac.
"""

import datetime as dt
import hashlib
import hmac
import os
import subprocess
import time
import urllib.parse

from common import HttpError, env, http, load, save

TOKEN_FILE = os.path.join("data", "linkedin_token.enc")
SCOPES = "openid profile w_member_social"


def _openssl(args, data, key):
    res = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-a", "-A",
         "-pass", "env:TOKEN_KEY"] + args,
        input=data, capture_output=True, text=True, env=dict(os.environ, TOKEN_KEY=key))
    if res.returncode != 0:
        raise RuntimeError("Token encryption failed: " + res.stderr.strip()[:200])
    return res.stdout


def encrypt(token, key):
    return _openssl(["-salt"], token, key)


def decrypt(blob, key):
    return _openssl(["-d"], blob.strip(), key)


def current():
    """(token, person_urn, expires_epoch) of the newest usable token, or Nones."""
    best = (env("LINKEDIN_ACCESS_TOKEN"), env("LINKEDIN_PERSON_URN"),
            int(env("LINKEDIN_TOKEN_EXPIRES", "0") or 0))
    info = load("linkedin.json", {})
    key = env("LINKEDIN_TOKEN_KEY")
    if info and key and os.path.exists(TOKEN_FILE) and info.get("expires", 0) > best[2]:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            best = (decrypt(fh.read(), key), info.get("person_urn"), info["expires"])
    if not best[0]:
        return None, None, 0
    return best


def expires():
    info = load("linkedin.json", {})
    return max(int(env("LINKEDIN_TOKEN_EXPIRES", "0") or 0), int(info.get("expires", 0)))


# ------------------------------------------------------------------ phone renewal

def _state(day):
    key = env("LINKEDIN_TOKEN_KEY", required=True)
    return hmac.new(key.encode(), f"renew:{day}".encode(), hashlib.sha256).hexdigest()[:32]


def renewal_ready():
    missing = [n for n in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET",
                           "LINKEDIN_TOKEN_KEY", "LINKEDIN_PAGES_REDIRECT") if not env(n)]
    return missing


def authorize_url():
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": env("LINKEDIN_CLIENT_ID", required=True),
        "redirect_uri": env("LINKEDIN_PAGES_REDIRECT", required=True),
        "state": _state(today), "scope": SCOPES})


def state_ok(state):
    now = dt.datetime.now(dt.timezone.utc).date()
    valid = {_state((now - dt.timedelta(days=d)).isoformat()) for d in (0, 1)}
    return any(hmac.compare_digest(state, v) for v in valid)


def exchange(code):
    """Swap the authorization code for a token and store it encrypted."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": env("LINKEDIN_PAGES_REDIRECT", required=True),
        "client_id": env("LINKEDIN_CLIENT_ID", required=True),
        "client_secret": env("LINKEDIN_CLIENT_SECRET", required=True)})
    try:
        _, token, _ = http("POST", "https://www.linkedin.com/oauth/v2/accessToken",
                           {"Content-Type": "application/x-www-form-urlencoded"}, body)
        _, me, _ = http("GET", "https://api.linkedin.com/v2/userinfo",
                        {"Authorization": "Bearer " + token["access_token"]})
    except HttpError as err:
        raise RuntimeError(f"LinkedIn refused the renewal ({err.status}): {err.body[:200]}. "
                           "Codes expire after a few minutes; comment /linkedin to get a "
                           "fresh link.") from None
    expires_at = int(time.time()) + int(token.get("expires_in", 5184000))
    os.makedirs("data", exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(encrypt(token["access_token"], env("LINKEDIN_TOKEN_KEY", required=True)) + "\n")
    save("linkedin.json", {"expires": expires_at, "person_urn": "urn:li:person:" + me["sub"],
                           "renewed": dt.date.today().isoformat()})
    return me.get("name", ""), expires_at
