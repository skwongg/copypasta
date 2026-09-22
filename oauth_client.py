"""OAuth PKCE with pinned endpoints, private storage and state-bound callbacks.

Run `auth-url`, open the printed URL locally, then pipe the complete redirected
loopback URL to `exchange` via stdin. Never paste credentials into an agent chat
or put callback URLs in shell arguments. No network occurs at import time.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WORKDIR = Path(__file__).resolve().parent
CREDENTIAL_DIR = Path(os.environ.get("COPYTRADER_CREDENTIAL_DIR", str(Path.home() / ".local/share/copypasta/credentials")))
CLIENT_FILE = CREDENTIAL_DIR / "oauth_client.json"
TOKENS_FILE = CREDENTIAL_DIR / "tokens.json"
DISCOVERY_URL = "https://agent.robinhood.com/.well-known/oauth-authorization-server"
FALLBACK_METADATA = {
    "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
    "authorization_endpoint": "https://robinhood.com/oauth",
    "token_endpoint": "https://api.robinhood.com/oauth2/token/",
    "code_challenge_methods_supported": ["S256"],
    "scopes_supported": ["internal"],
}
REDIRECT_URI = "http://127.0.0.1:8743/callback"
SCOPE = "internal"
CLIENT_NAME = "copypasta-local-pkce"
TOKEN_REFRESH_SKEW_SECONDS = 60
PENDING_MAX_AGE_SECONDS = 600
MAX_RESPONSE_BYTES = 1024 * 1024


class OAuthError(Exception):
    """Intentionally generic: never include server bodies or secret values."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OAuthError("OAuth redirect refused")


def _validate_metadata(metadata):
    if not isinstance(metadata, dict) or any(metadata.get(k) != FALLBACK_METADATA[k]
            for k in ("registration_endpoint", "authorization_endpoint", "token_endpoint")):
        raise OAuthError("OAuth metadata endpoints do not match the approved endpoints")
    if "S256" not in metadata.get("code_challenge_methods_supported", []):
        raise OAuthError("OAuth metadata does not support required PKCE")
    return metadata


def _request_json(url, data=None, content_type=None, timeout=30):
    approved = {DISCOVERY_URL, FALLBACK_METADATA["registration_endpoint"], FALLBACK_METADATA["token_endpoint"]}
    if url not in approved:
        raise OAuthError("Unapproved OAuth endpoint")
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url or not 200 <= response.status < 300:
                raise OAuthError("OAuth redirect or unsuccessful response refused")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OAuthError("OAuth response exceeds size limit")
            result = json.loads(raw.decode("utf-8"))
            if type(result) is not dict:
                raise OAuthError("Invalid OAuth response")
            return result, response.status
    except OAuthError:
        raise
    except urllib.error.HTTPError as exc:
        exc.close()
        raise OAuthError("OAuth request failed") from None
    except Exception:
        # urllib exceptions may include a URL, credential-bearing response body,
        # or header. None of those cross the logging/notification boundary.
        raise OAuthError("OAuth request failed") from None


def _http_json(url, payload=None, timeout=30):
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    return _request_json(url, data, "application/json" if payload is not None else None, timeout)


def _http_form(url, fields, timeout=30):
    if url != FALLBACK_METADATA["token_endpoint"]:
        raise OAuthError("Unapproved token endpoint")
    return _request_json(url, urllib.parse.urlencode(fields).encode(),
                         "application/x-www-form-urlencoded", timeout)[0]


def _credential_path(path):
    directory = Path(CREDENTIAL_DIR)
    path = Path(path)
    if not directory.is_absolute() or not path.is_absolute() or path.parent != directory:
        raise OAuthError("Credential paths must use the configured absolute private directory")
    if ".." in directory.parts:
        raise OAuthError("Credential directory traversal refused")
    if directory == WORKDIR or WORKDIR in directory.parents:
        raise OAuthError("Credential storage inside the repository is forbidden")
    # Reject symlinks in every existing component, not just the final filename.
    for component in [*reversed(directory.parents), directory]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OAuthError("Credential directory must not traverse symlinks")
        if (component / ".git").exists():
            raise OAuthError("Credential storage inside a Git checkout is forbidden")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = directory.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise OAuthError("Credential directory must be owned by the current user with mode 0700")
    return path


def _open_directory(path):
    path = _credential_path(path)
    # Walk from / with openat/O_NOFOLLOW so an ancestor cannot be swapped for a
    # symlink between the path inspection and opening the credential directory.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except Exception:
        os.close(fd)
        raise OAuthError("Unable to securely open credential directory") from None
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise OAuthError("Unsafe credential directory")
    return path, fd


def _check_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise OAuthError("Credential files must be private regular files with mode 0600")


def _secure_write_json(path, obj):
    """Atomic 0600 write anchored to a verified directory; never follow links."""
    path, directory_fd = _open_directory(path)
    temp_name = ".pending-" + secrets.token_hex(16)
    try:
        try:
            _check_file(os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False))
        except FileNotFoundError:
            pass
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OAuthError:
        raise
    except Exception:
        raise OAuthError("Unable to securely save credentials") from None
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _load_json(path):
    path, directory_fd = _open_directory(path)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            _check_file(os.fstat(stream.fileno()))
            raw = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OAuthError("Credential file exceeds size limit")
            data = json.loads(raw)
            if type(data) is not dict:
                raise OAuthError("Invalid credential record")
            return data
    except FileNotFoundError:
        raise OAuthError("Credentials unavailable; authorize locally first") from None
    except OAuthError:
        raise
    except Exception:
        raise OAuthError("Unable to securely read credentials") from None
    finally:
        os.close(directory_fd)


@contextlib.contextmanager
def _pending_lock():
    """A process lock makes pending-state validation/consumption single use."""
    import fcntl
    path, directory_fd = _open_directory(CLIENT_FILE)
    fd = None
    try:
        fd = os.open("oauth.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        _check_file(os.fstat(fd))
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OAuthError:
        raise
    except Exception:
        raise OAuthError("OAuth state operation failed") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory_fd)


def _now():
    return int(time.time())


def discover_metadata():
    metadata, _ = _http_json(DISCOVERY_URL)
    return _validate_metadata(metadata)


def generate_pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def register_client(metadata=None):
    metadata = _validate_metadata(metadata if metadata is not None else discover_metadata())
    if os.path.lexists(CLIENT_FILE):
        record = _load_json(CLIENT_FILE)
        if type(record.get("client_id")) is str and record["client_id"]:
            return record
        raise OAuthError("Invalid stored client record")
    response, _ = _http_json(metadata["registration_endpoint"], {
        "client_name": CLIENT_NAME, "redirect_uris": [REDIRECT_URI],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "scope": SCOPE})
    if type(response.get("client_id")) is not str or not response["client_id"]:
        raise OAuthError("OAuth registration failed")
    record = {"client_id": response["client_id"], "redirect_uri": REDIRECT_URI,
              "scope": SCOPE, "registered_at": _now()}
    _secure_write_json(CLIENT_FILE, record)
    return record


def authorization_url(client_id, code_challenge, state, metadata=None):
    metadata = _validate_metadata(metadata if metadata is not None else discover_metadata())
    return metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
        "scope": SCOPE, "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256"})


def begin_authorization(metadata=None):
    metadata = _validate_metadata(metadata if metadata is not None else discover_metadata())
    with _pending_lock():
        record = register_client(metadata)
        verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(32)
        record["pending"] = {"verifier": verifier, "state": state, "created_at": _now()}
        _secure_write_json(CLIENT_FILE, record)
    return authorization_url(record["client_id"], challenge, state, metadata)


def exchange_code(callback_url, metadata=None):
    """Accept ONLY the full state-bound callback URL; never a bare code/verifier."""
    if type(callback_url) is not str or len(callback_url) > 16384:
        raise OAuthError("A complete local callback URL is required")
    try:
        parsed = urllib.parse.urlsplit(callback_url.strip())
        expected = urllib.parse.urlsplit(REDIRECT_URI)
        if (parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path) or parsed.fragment:
            raise ValueError
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if set(params) != {"code", "state"} or any(len(v) != 1 or not v[0] for v in params.values()):
            raise ValueError
    except (ValueError, TypeError):
        raise OAuthError("Invalid OAuth callback") from None
    with _pending_lock():
        record = _load_json(CLIENT_FILE)
        pending = record.get("pending")
        if (type(pending) is not dict or type(pending.get("state")) is not str
                or type(pending.get("verifier")) is not str or not 43 <= len(pending["verifier"]) <= 128
                or type(pending.get("created_at")) is not int
                or not 0 <= _now() - pending["created_at"] <= PENDING_MAX_AGE_SECONDS
                or not secrets.compare_digest(params["state"][0], pending["state"])):
            raise OAuthError("OAuth callback state is missing, expired or mismatched")
        # Consume before any network request. A failed exchange requires new auth.
        record.pop("pending")
        _secure_write_json(CLIENT_FILE, record)
    metadata = _validate_metadata(metadata if metadata is not None else discover_metadata())
    response = _http_form(metadata["token_endpoint"], {
        "grant_type": "authorization_code", "code": params["code"][0], "redirect_uri": REDIRECT_URI,
        "client_id": record["client_id"], "code_verifier": pending["verifier"]})
    _store_tokens(response)
    return {"authorized": True}  # Do not return secrets to CLI/agent callers.


def _store_tokens(response):
    if (type(response) is not dict or type(response.get("access_token")) is not str or not response["access_token"]
            or type(response.get("token_type", "Bearer")) is not str
            or response.get("token_type", "Bearer").lower() != "bearer"
            or type(response.get("expires_in")) is not int or response["expires_in"] <= 0
            or (response.get("refresh_token") is not None and type(response["refresh_token"]) is not str)):
        raise OAuthError("OAuth token response failed validation")
    _secure_write_json(TOKENS_FILE, {
        "access_token": response["access_token"], "refresh_token": response.get("refresh_token"),
        "token_type": "Bearer", "scope": response.get("scope", SCOPE),
        "obtained_at": _now(), "expires_at": _now() + response["expires_in"]})


def refresh_tokens(metadata=None):
    record = _load_json(TOKENS_FILE)
    if type(record.get("refresh_token")) is not str or not record["refresh_token"]:
        raise OAuthError("No refresh token available")
    client = _load_json(CLIENT_FILE)
    metadata = _validate_metadata(metadata if metadata is not None else discover_metadata())
    response = _http_form(metadata["token_endpoint"], {
        "grant_type": "refresh_token", "refresh_token": record["refresh_token"], "client_id": client["client_id"]})
    if not response.get("refresh_token"):
        response["refresh_token"] = record["refresh_token"]
    _store_tokens(response)
    return _load_json(TOKENS_FILE)


def get_valid_token():
    record = _load_json(TOKENS_FILE)
    if type(record.get("expires_at")) is not int:
        raise OAuthError("Invalid stored token expiry")
    if record["expires_at"] - _now() <= TOKEN_REFRESH_SKEW_SECONDS:
        record = refresh_tokens()
    token = record.get("access_token")
    if type(token) is not str or not token:
        raise OAuthError("Invalid stored access token")
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local OAuth PKCE flow; callback accepted only through stdin")
    parser.add_argument("command", choices=["auth-url", "exchange", "status"])
    args = parser.parse_args(argv)
    try:
        if args.command == "auth-url":
            print(begin_authorization())
        elif args.command == "exchange":
            callback = sys.stdin.readline(16385)
            exchange_code(callback)
            print("Authorization stored securely.")
        else:
            print("Credential file present: " + ("yes" if os.path.lexists(TOKENS_FILE) else "no"))
        return 0
    except OAuthError as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
