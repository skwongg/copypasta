"""OAuth 2.0 + PKCE client for Robinhood's Agentic MCP.

Setup flow (this VM is headless, so Silas approves in his desktop browser):

    1. ``python oauth_client.py auth-url``  -> prints the authorization URL (stdout only).
    2. Silas opens it in a desktop browser (mobile redirects to the Robinhood app
       and will NOT work), logs into Robinhood, and approves the agent.
    3. Robinhood redirects the browser to the loopback redirect URI below, which
       fails to load (nothing listens) -- that is expected. Silas copies the
       ``code=...`` value from the browser's address bar and pastes it back.
    4. The parent agent then calls ``exchange_code()`` with the pasted code.

Redirect-URI choice: ``http://127.0.0.1:8743/callback``.

Why: the headless VM cannot host a callback the user's browser can reach, and
Robinhood does not advertise an out-of-band (``urn:ietf:wg:oauth:2.0:oob``)
option in its OAuth metadata. The community-established pattern for this
exact server (see the robinhood-agentic skill) is a loopback redirect where
the user manually copies the code from the address bar. Loopback HTTP is used
(instead of HTTPS) per RFC 8252 -- loopback redirects are exempt from the
HTTPS requirement. ``127.0.0.1`` (numeric) is used rather than ``localhost``
to avoid IPv6/localhost resolution ambiguity.

Endpoints (verified live against the discovery document on 2026-09-21):

    discovery:   https://agent.robinhood.com/.well-known/oauth-authorization-server
    register:    https://agent.robinhood.com/oauth/trading/register
    authorize:   https://robinhood.com/oauth
    token:       https://api.robinhood.com/oauth2/token/

OAuth metadata: public client (token_endpoint_auth_method "none"), PKCE S256
only, response_type "code", grant types authorization_code + refresh_token,
single scope "internal".

SECURITY: this module never prints access tokens, refresh tokens, code
verifiers, or client secrets to stdout or logs. The authorization URL is the
only value ever printed. The CLI ``exchange`` command deliberately REFUSES to
run -- code exchange happens only via the parent agent with Silas's pasted
code, never from this CLI.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import time
import urllib.parse
import urllib.request

WORKDIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_FILE = os.path.join(WORKDIR, ".oauth_client.json")
TOKENS_FILE = os.path.join(WORKDIR, ".tokens.json")

DISCOVERY_URL = "https://agent.robinhood.com/.well-known/oauth-authorization-server"

# Fallback endpoints matching the approved scope doc, used only if the
# discovery document cannot be fetched.
FALLBACK_METADATA = {
    "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
    "authorization_endpoint": "https://robinhood.com/oauth",
    "token_endpoint": "https://api.robinhood.com/oauth2/token/",
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["internal"],
}

REDIRECT_URI = "http://127.0.0.1:8743/callback"
SCOPE = "internal"
CLIENT_NAME = "copy-trader-oauth (headless VM, manual code paste)"
TOKEN_REFRESH_SKEW_SECONDS = 60  # treat token as expired this long before expiry


class OAuthError(Exception):
    """Raised when an OAuth/HTTP step fails."""


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------

def _http_json(url, payload=None, timeout=30):
    """POST (payload dict or None -> GET) and parse a JSON response.

    Raises OAuthError with the server's error body on any failure. Honors
    https_proxy env automatically via urllib.
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise OAuthError(
            "HTTP %s from %s: %s" % (e.code, url, body[:500])
        ) from None
    except Exception as e:  # network errors, JSON decode errors
        raise OAuthError("request to %s failed: %r" % (url, e)) from None


def _http_form(url, fields, timeout=30):
    """POST application/x-www-form-urlencoded, parse a JSON response."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise OAuthError(
            "HTTP %s from %s: %s" % (e.code, url, body[:500])
        ) from None
    except Exception as e:
        raise OAuthError("request to %s failed: %r" % (url, e)) from None


def _secure_write_json(path, obj):
    """Write JSON with mode 0600, enforced via os.open and verified after."""
    data = json.dumps(obj, indent=2).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)  # enforce in case the file already existed
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        raise OAuthError(
            "failed to enforce 0600 on %s (mode is %o)" % (path, mode)
        )


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _now():
    return int(time.time())


# ---------------------------------------------------------------------------
# OAuth metadata discovery
# ---------------------------------------------------------------------------

def discover_metadata():
    """Fetch the OAuth authorization-server metadata (RFC 8414).

    Falls back to the hardcoded scope-doc endpoints if discovery fails, and
    notes the fallback on stderr.
    """
    try:
        metadata, _ = _http_json(DISCOVERY_URL)
        if "authorization_endpoint" not in metadata or "token_endpoint" not in metadata:
            raise OAuthError("discovery document missing endpoints")
        return metadata
    except OAuthError as e:
        print(
            "warning: OAuth discovery failed (%s); using documented endpoints" % e,
            file=sys.stderr,
        )
        return dict(FALLBACK_METADATA)


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def generate_pkce():
    """Return (code_verifier, code_challenge_s256).

    Verifier: 86 URL-safe chars from the secrets module (within the RFC 7636
    43-128 char range). Challenge: base64url(SHA256(verifier)), no padding.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------

def register_client(metadata=None):
    """Register (or reuse) a public OAuth client; persist to .oauth_client.json.

    Returns the parsed registration record. Reuses the stored client_id if the
    file already exists. Never touches passwords.
    """
    if os.path.exists(CLIENT_FILE):
        record = _load_json(CLIENT_FILE)
        if record.get("client_id"):
            return record
    metadata = metadata or discover_metadata()
    registration_endpoint = metadata.get("registration_endpoint")
    if not registration_endpoint:
        raise OAuthError("no registration_endpoint in OAuth metadata")

    payload = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "token_endpoint_auth_method": "none",  # public client: no secret
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": SCOPE,
    }
    response, status = _http_json(registration_endpoint, payload)
    client_id = response.get("client_id")
    if not client_id:
        raise OAuthError(
            "registration returned HTTP %s without client_id: %s"
            % (status, json.dumps(response)[:500])
        )
    record = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "registered_at": _now(),
        "registration_response": response,
        # Filled in when an auth-url is generated; needed for the later
        # exchange_code() call.
        "code_verifier": None,
        "state": None,
    }
    _secure_write_json(CLIENT_FILE, record)
    return record


# ---------------------------------------------------------------------------
# authorization URL
# ---------------------------------------------------------------------------

def authorization_url(client_id, code_challenge, state, metadata=None):
    """Build the full URL Silas must open in his desktop browser."""
    metadata = metadata or discover_metadata()
    auth_endpoint = metadata.get("authorization_endpoint")
    if not auth_endpoint:
        raise OAuthError("no authorization_endpoint in OAuth metadata")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return auth_endpoint + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# token exchange / refresh / access
# ---------------------------------------------------------------------------

def exchange_code(code, code_verifier, metadata=None):
    """Exchange an authorization code for tokens; store to .tokens.json.

    Args:
        code: the code Silas pasted from his browser's address bar (the
            parent agent delivers it; the CLI never calls this).
        code_verifier: the PKCE verifier saved alongside client_id at
            auth-url generation time.
    Returns the token response dict. Never prints secrets.
    """
    metadata = metadata or discover_metadata()
    token_endpoint = metadata.get("token_endpoint")
    if not token_endpoint:
        raise OAuthError("no token_endpoint in OAuth metadata")
    if not os.path.exists(CLIENT_FILE):
        raise OAuthError("no registered client; run auth-url first")
    client_id = _load_json(CLIENT_FILE).get("client_id")

    fields = {
        "grant_type": "authorization_code",
        "code": code.strip(),
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    token_resp = _http_form(token_endpoint, fields)
    if "access_token" not in token_resp:
        raise OAuthError(
            "token exchange failed: %s" % json.dumps(token_resp)[:500]
        )
    _store_tokens(token_resp)
    return token_resp


def _store_tokens(token_resp):
    expires_in = int(token_resp.get("expires_in", 3600))
    record = {
        "token_type": token_resp.get("token_type", "Bearer"),
        "scope": token_resp.get("scope", SCOPE),
        "obtained_at": _now(),
        "expires_at": _now() + expires_in,
        "expires_in": expires_in,
        # secret values: never printed
        "access_token": token_resp["access_token"],
        "refresh_token": token_resp.get("refresh_token"),
    }
    _secure_write_json(TOKENS_FILE, record)


def refresh_tokens(metadata=None):
    """Use the stored refresh_token to obtain fresh tokens.

    Returns the new token record (without printing secrets). Raises
    OAuthError if there is no stored refresh token or the refresh fails.
    """
    if not os.path.exists(TOKENS_FILE):
        raise OAuthError("no stored tokens; authorize first")
    record = _load_json(TOKENS_FILE)
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise OAuthError("stored token record has no refresh_token")
    metadata = metadata or discover_metadata()
    token_endpoint = metadata.get("token_endpoint")
    if not token_endpoint:
        raise OAuthError("no token_endpoint in OAuth metadata")
    client_id = None
    if os.path.exists(CLIENT_FILE):
        client_id = _load_json(CLIENT_FILE).get("client_id")

    fields = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id or "",
    }
    token_resp = _http_form(token_endpoint, fields)
    if "access_token" not in token_resp:
        raise OAuthError(
            "token refresh failed: %s" % json.dumps(token_resp)[:500]
        )
    if not token_resp.get("refresh_token"):
        # server did not rotate; keep the old one
        token_resp["refresh_token"] = refresh_token
    _store_tokens(token_resp)
    return _load_json(TOKENS_FILE)


def get_valid_token():
    """Return a fresh access token, refreshing when expired (60s skew).

    The token value itself is returned to the caller (the MCP client module),
    never printed by this module's CLI.
    """
    if not os.path.exists(TOKENS_FILE):
        raise OAuthError("no stored tokens; authorize first")
    record = _load_json(TOKENS_FILE)
    if record.get("expires_at", 0) - _now() > TOKEN_REFRESH_SKEW_SECONDS:
        return record["access_token"]
    record = refresh_tokens()
    return record["access_token"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_auth_url():
    """Register if needed, generate PKCE, print ONLY the auth URL to stdout."""
    metadata = discover_metadata()
    record = register_client(metadata)
    client_id = record["client_id"]
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)

    # Persist the verifier + state for the later exchange_code() call.
    record["code_verifier"] = verifier
    record["state"] = state
    record["auth_url_generated_at"] = _now()
    _secure_write_json(CLIENT_FILE, record)

    url = authorization_url(client_id, challenge, state, metadata)
    sys.stdout.write(url + "\n")
    sys.stdout.flush()


def cmd_exchange(args):
    print(
        "STOP: code exchange requires Silas's pasted code delivered via the "
        "parent agent. Not performing exchange.",
        file=sys.stderr,
    )
    return 2


def cmd_status():
    """Print authorization status and token expiry; never secret values."""
    authorized = os.path.exists(TOKENS_FILE)
    info = {"authorized": "yes" if authorized else "no"}
    if authorized:
        try:
            record = _load_json(TOKENS_FILE)
            info["expires_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S %Z", time.localtime(record.get("expires_at", 0))
            )
            remaining = record.get("expires_at", 0) - _now()
            info["expired"] = "yes" if remaining <= 0 else "no"
            info["seconds_until_expiry"] = remaining
            info["has_refresh_token"] = (
                "yes" if record.get("refresh_token") else "no"
            )
        except Exception as e:
            info["error"] = "could not read token file: %r" % (e,)
    if os.path.exists(CLIENT_FILE):
        try:
            record = _load_json(CLIENT_FILE)
            info["client_registered"] = (
                "yes" if record.get("client_id") else "no"
            )
        except Exception:
            info["client_registered"] = "unknown"
    else:
        info["client_registered"] = "no"
    for key in (
        "authorized",
        "client_registered",
        "expired",
        "expires_at",
        "seconds_until_expiry",
        "has_refresh_token",
        "error",
    ):
        if key in info:
            print("%s: %s" % (key, info[key]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="OAuth 2.0 + PKCE client for Robinhood's Agentic MCP"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth-url", help="print the authorization URL (stdout only)")

    ex = sub.add_parser("exchange", help="refused: exchange requires Silas's code")
    ex.add_argument("--code", default=None, help="authorization code (never used)")

    sub.add_parser("status", help="show authorization status (no secrets)")

    args = parser.parse_args(argv)
    if args.command == "auth-url":
        try:
            cmd_auth_url()
        except OAuthError as e:
            print("ERROR: %s" % e, file=sys.stderr)
            return 1
        return 0
    if args.command == "exchange":
        return cmd_exchange(args)
    if args.command == "status":
        cmd_status()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
