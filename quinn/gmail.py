"""Gmail integration — OAuth (installed-app loopback flow) + draft REST calls.

Design decision worth defending: Quinn **never auto-sends email**. The DELIVER
step creates a *draft* in the connected Gmail account; a human then approves it
(from the Slack card via `py -m quinn.run --send-mail <id>`, or by opening Gmail
and pressing Send themselves). The riskiest side effect in the whole pipeline —
an email leaving the building under our name — always has a human finger on the
trigger, while everything upstream stays fully automated.

Auth, in plain terms:
  * ``gmail_Creds.json`` (root, gitignored) is a Google OAuth "installed app"
    client (client_id + client_secret). It identifies the APP, not the user.
  * One-time consent: ``py -m quinn.gmail`` prints an accounts.google.com URL;
    the user approves in their own browser (we never see their password); Google
    redirects to a localhost listener with a code; we exchange it for tokens and
    cache them in ``gmail_token.json`` (gitignored).
  * After that, ``_access_token()`` silently refreshes as needed.

Scope: ``gmail.compose`` only — create/update/delete drafts and send. No read
access to the mailbox. Least privilege that still covers the draft workflow.

Stdlib only (urllib + email.message + http.server), consistent with the rest of
the transport code in this repo.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREDS_PATH = Path(os.environ.get("QUINN_GMAIL_CREDS", ROOT / "gmail_Creds.json"))
TOKEN_PATH = Path(os.environ.get("QUINN_GMAIL_TOKEN", ROOT / "gmail_token.json"))

SCOPE = "https://www.googleapis.com/auth/gmail.compose"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
LOOPBACK_PORT = 8765          # loopback redirect for the installed-app flow
CONSENT_TIMEOUT_S = 300
HTTP_TIMEOUT_S = 30


class GmailError(RuntimeError):
    """Anything that goes wrong talking to Google."""


class GmailNotAuthorized(GmailError):
    """No cached token — the one-time consent flow hasn't been run yet."""


# --------------------------------------------------------------------------- #
# OAuth plumbing                                                               #
# --------------------------------------------------------------------------- #

def _client() -> dict:
    if not CREDS_PATH.exists():
        raise GmailError(f"missing OAuth client file: {CREDS_PATH}")
    node = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    return node.get("installed") or node.get("web") or node


def _post_form(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise GmailError(f"token endpoint HTTP {exc.code}: "
                         f"{exc.read().decode('utf-8', 'replace')[:300]}") from exc


def _save_token(tok: dict) -> None:
    # Preserve the refresh_token across refreshes (Google omits it on refresh).
    if TOKEN_PATH.exists():
        old = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        tok.setdefault("refresh_token", old.get("refresh_token"))
    tok["_expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    TOKEN_PATH.write_text(json.dumps(tok, indent=2), encoding="utf-8")


def _access_token() -> str:
    """Return a valid access token, refreshing via the cached refresh_token."""
    if not TOKEN_PATH.exists():
        raise GmailNotAuthorized("no gmail_token.json — run: py -m quinn.gmail")
    tok = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    if time.time() < tok.get("_expires_at", 0) and tok.get("access_token"):
        return tok["access_token"]
    client = _client()
    fresh = _post_form(TOKEN_ENDPOINT, {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": tok.get("refresh_token", ""),
        "grant_type": "refresh_token",
    })
    _save_token(fresh)
    return fresh["access_token"]


def is_configured() -> bool:
    """True when both the OAuth client and a cached user token exist."""
    return CREDS_PATH.exists() and TOKEN_PATH.exists()


# --------------------------------------------------------------------------- #
# One-time consent flow (py -m quinn.gmail)                                    #
# --------------------------------------------------------------------------- #

class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    """Collects auth codes from Google's loopback redirect.

    Browsers hit this listener with extra requests (favicon.ico, prefetch), and
    a prefetch can even carry a truncated query. So: (a) requests without a
    code/error param are ignored outright, and (b) every captured code is
    APPENDED to a queue — the consent loop tries each until one exchanges,
    instead of trusting the first thing that arrives.
    """
    codes: list[str] = []
    error: str | None = None

    def do_GET(self):                                          # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (qs.get("code") or [None])[0]
        error = (qs.get("error") or [None])[0]
        if code is None and error is None:                     # favicon / noise
            self.send_response(404)
            self.end_headers()
            return
        if code:
            _CodeCatcher.codes.append(code.strip())
        if error:
            _CodeCatcher.error = error
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Quinn is connected to Gmail — you can close this tab." \
            if code else f"Authorization failed: {error}"
        self.wfile.write(f"<h2>{msg}</h2>".encode())

    def log_message(self, *_):                                 # silence stdlib chatter
        pass


def run_consent_flow() -> None:
    """Interactive one-time authorization. Prints the URL; the USER approves.

    Keeps listening until a captured code successfully exchanges for tokens (or
    the timeout hits) — a malformed/truncated callback is logged and skipped,
    not fatal, because the real redirect usually lands moments later.
    """
    client = _client()
    redirect = f"http://localhost:{LOOPBACK_PORT}"
    url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",      # -> refresh_token
        "prompt": "consent",           # force refresh_token even on re-consent
    })

    _CodeCatcher.codes, _CodeCatcher.error = [], None
    server = http.server.HTTPServer(("localhost", LOOPBACK_PORT), _CodeCatcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("Open this URL in your browser and approve access "
          f"(scope: gmail.compose — drafts only, no mailbox read):\n\n{url}\n",
          flush=True)
    try:
        webbrowser.open(url)
    except Exception:                                          # noqa: BLE001
        pass                                                   # URL is printed anyway

    deadline = time.time() + CONSENT_TIMEOUT_S
    tried: set[str] = set()
    try:
        while time.time() < deadline:
            if _CodeCatcher.error:
                raise GmailError(
                    f"consent denied: {_CodeCatcher.error}. If this says "
                    "access_denied, add your Google account as a Test User on "
                    "the OAuth consent screen (Google Cloud console) and retry.")
            fresh = [c for c in _CodeCatcher.codes if c not in tried]
            for code in fresh:
                tried.add(code)
                try:
                    tok = _post_form(TOKEN_ENDPOINT, {
                        "client_id": client["client_id"],
                        "client_secret": client["client_secret"],
                        "code": code,
                        "redirect_uri": redirect,
                        "grant_type": "authorization_code",
                    })
                except GmailError as exc:
                    print(f"  (callback code didn't exchange — waiting for a "
                          f"clean one: {exc})", flush=True)
                    continue
                _save_token(tok)
                print(f"Authorized. Token cached at {TOKEN_PATH.name} "
                      "(gitignored). Quinn can now create Gmail drafts.")
                return
            time.sleep(0.5)
        raise GmailError("consent timed out after 5 minutes — run again")
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- #
# Gmail REST calls (drafts only)                                               #
# --------------------------------------------------------------------------- #

def _api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{GMAIL_API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {_access_token()}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise GmailError(f"gmail {method} {path} -> HTTP {exc.code}: "
                         f"{exc.read().decode('utf-8', 'replace')[:300]}") from exc


def create_draft(*, to: str, subject: str, body: str) -> str:
    """Create a draft in the connected mailbox. Returns the draft id."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    out = _api("POST", "/drafts", {"message": {"raw": raw}})
    return out["id"]


def send_draft(draft_id: str) -> str:
    """Send an existing draft (the human-approval action). Returns message id."""
    out = _api("POST", "/drafts/send", {"id": draft_id})
    return out.get("id", "")


def delete_draft(draft_id: str) -> bool:
    """Discard a rejected draft. Best-effort — True on success."""
    try:
        _api("DELETE", f"/drafts/{draft_id}")
        return True
    except GmailError:
        return False


if __name__ == "__main__":
    run_consent_flow()
