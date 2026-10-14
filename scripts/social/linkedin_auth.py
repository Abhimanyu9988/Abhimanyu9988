#!/usr/bin/env python3
"""Connect (or reconnect) LinkedIn posting. Run on your own computer:

    python3 scripts/social/linkedin_auth.py

Opens LinkedIn in your browser, receives the approval on localhost, and stores the
token in your profile repository's GitHub secrets with the GitHub CLI. LinkedIn
tokens for self-serve apps last 60 days, so the daily job reminds you before expiry.
"""

import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/callback"
SCOPES = "openid profile w_member_social"
KEYCHAIN_SERVICE = "profile-social-linkedin"


def run(cmd, stdin=None):
    return subprocess.run(cmd, input=stdin, text=True, capture_output=True, check=True).stdout


def repo():
    login = run(["gh", "api", "user", "-q", ".login"]).strip()
    return f"{login}/{login}"


def keychain_get(account):
    if sys.platform != "darwin":
        return None
    try:
        return run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                    "-a", account, "-w"]).strip()
    except subprocess.CalledProcessError:
        return None


def keychain_set(account, value):
    if sys.platform == "darwin":
        subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                        "-a", account, "-w", value], check=True, capture_output=True)


def prompt(label, secret=False):
    import getpass
    value = getpass.getpass(f"  {label}: ") if secret else input(f"  {label}: ")
    return value.strip()


class Handler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        Handler.result = {k: v[0] for k, v in query.items()}
        ok = "code" in Handler.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("LinkedIn connected. You can close this tab." if ok else
               "LinkedIn did not approve: " + Handler.result.get("error_description", "unknown"))
        self.wfile.write(f"<h2 style='font-family:sans-serif'>{msg}</h2>".encode())

    def log_message(self, *args):
        pass


def main():
    if not shutil.which("gh"):
        sys.exit("The GitHub CLI (gh) is required: https://cli.github.com")
    target = repo()
    print(f"\nConnecting LinkedIn posting for {target}\n")

    client_id = keychain_get("client_id") or os.environ.get("LINKEDIN_CLIENT_ID")
    client_secret = keychain_get("client_secret") or os.environ.get("LINKEDIN_CLIENT_SECRET")
    if not (client_id and client_secret):
        print("  You need a LinkedIn developer app (one-time):")
        print("   1. Create it at https://www.linkedin.com/developers/apps/new")
        print("      It must be linked to a LinkedIn Page you admin. Use a personal page,")
        print("      not your employer's.")
        print("   2. Products tab: add 'Share on LinkedIn' and")
        print("      'Sign In with LinkedIn using OpenID Connect'.")
        print(f"   3. Auth tab: add the redirect URL  {REDIRECT}")
        print("   4. Copy the Client ID and Primary Client Secret from the Auth tab.\n")
        webbrowser.open("https://www.linkedin.com/developers/apps/new")
        client_id = prompt("Client ID")
        client_secret = prompt("Client Secret (hidden)", secret=True)
        if not (client_id and client_secret):
            sys.exit("Client ID and secret are required")
        keychain_set("client_id", client_id)
        keychain_set("client_secret", client_secret)

    state = secrets.token_urlsafe(16)
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "state": state, "scope": SCOPES})
    print("  A browser window opens. Sign in to LinkedIn and click Allow.")
    webbrowser.open(url)
    deadline = time.time() + 300
    while not Handler.result and time.time() < deadline:
        time.sleep(0.5)
    server.server_close()
    res = Handler.result
    if not res:
        sys.exit("Timed out waiting for LinkedIn. Run this again.")
    if res.get("state") != state:
        sys.exit("Security check failed (state mismatch). Run this again.")
    if "code" not in res:
        sys.exit("LinkedIn did not approve: " + res.get("error_description", res.get("error", "")))

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": res["code"], "redirect_uri": REDIRECT,
        "client_id": client_id, "client_secret": client_secret}).encode()
    req = urllib.request.Request("https://www.linkedin.com/oauth/v2/accessToken", data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        token = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as err:
        detail = err.read().decode()[:300]
        if "client" in detail.lower():
            for acct in ("client_id", "client_secret"):
                subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE,
                                "-a", acct], capture_output=True)
            sys.exit("LinkedIn rejected the app credentials. Run this again and re-enter them.")
        sys.exit(f"Token exchange failed: {detail}")
    access = token["access_token"]
    expires = int(time.time()) + int(token.get("expires_in", 5184000))

    req = urllib.request.Request("https://api.linkedin.com/v2/userinfo",
                                 headers={"Authorization": "Bearer " + access})
    me = json.load(urllib.request.urlopen(req, timeout=30))
    urn = "urn:li:person:" + me["sub"]

    run(["gh", "secret", "set", "LINKEDIN_ACCESS_TOKEN", "-R", target], stdin=access)
    run(["gh", "variable", "set", "LINKEDIN_PERSON_URN", "-R", target, "--body", urn])
    run(["gh", "variable", "set", "LINKEDIN_TOKEN_EXPIRES", "-R", target,
         "--body", str(expires)])
    days = (expires - int(time.time())) // 86400
    print(f"\n  ✓ Connected as {me.get('name', urn)}. Posting works for {days} days.")

    # What phone renewal needs: the app credentials and a key for the encrypted token file.
    run(["gh", "variable", "set", "LINKEDIN_CLIENT_ID", "-R", target, "--body", client_id])
    run(["gh", "secret", "set", "LINKEDIN_CLIENT_SECRET", "-R", target], stdin=client_secret)
    token_key = keychain_get("token_key") or secrets.token_urlsafe(32)
    keychain_set("token_key", token_key)
    run(["gh", "secret", "set", "LINKEDIN_TOKEN_KEY", "-R", target], stdin=token_key)
    setup_pages(target)


def setup_pages(target):
    """Serve docs/linkedin.html on GitHub Pages so renewals can finish on a phone."""
    try:
        run(["gh", "api", f"repos/{target}/contents/docs/linkedin.html", "--silent"])
    except subprocess.CalledProcessError:
        print("  Phone renewal: finishes after the files are pushed (setup does this).")
        return False
    try:
        info = json.loads(run(["gh", "api", f"repos/{target}/pages"]))
    except subprocess.CalledProcessError:
        branch = run(["gh", "repo", "view", target, "--json", "defaultBranchRef",
                      "-q", ".defaultBranchRef.name"]).strip() or "main"
        payload = json.dumps({"source": {"branch": branch, "path": "/docs"}})
        try:
            info = json.loads(run(["gh", "api", "-X", "POST", f"repos/{target}/pages",
                                   "--input", "-"], stdin=payload))
        except subprocess.CalledProcessError as err:
            print(f"  ! Couldn't turn on GitHub Pages ({(err.stderr or '').strip()[:150]}).")
            print("    Phone renewal stays off; renew on this Mac with this script instead.")
            return False
    site = (info.get("html_url") or f"https://{target.split('/')[0].lower()}.github.io/"
            f"{target.split('/')[1]}/").rstrip("/")
    redirect = site + "/linkedin.html"
    run(["gh", "variable", "set", "LINKEDIN_PAGES_REDIRECT", "-R", target, "--body", redirect])
    print("\n  Phone renewal is ready. One last step in your LinkedIn app's Auth tab:")
    print("  add this second redirect URL, next to the localhost one:\n")
    print(f"      {redirect}\n")
    print("  From then on, when the reminder issue appears, comment /linkedin on it")
    print("  from your phone. No laptop needed.\n")
    return True


if __name__ == "__main__":
    if "--pages-only" in sys.argv:
        setup_pages(repo())
    else:
        main()
