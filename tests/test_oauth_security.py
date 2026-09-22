"""OAuth tests use dummy data, isolated temporary paths and mocked HTTP."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

import oauth_client as oauth


class OAuthSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="copypasta-oauth-test-")
        self.directory = Path(self.tmp.name).resolve() / "private"
        self.directory.mkdir(mode=0o700)
        self.patches = [patch.object(oauth, "CREDENTIAL_DIR", self.directory),
                        patch.object(oauth, "CLIENT_FILE", self.directory / "oauth_client.json"),
                        patch.object(oauth, "TOKENS_FILE", self.directory / "tokens.json")]
        for item in self.patches:
            item.start()
        # Any unexpected networking is a hard failure, never a real request.
        self.network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Unexpected network"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def seed_client(self):
        oauth._secure_write_json(oauth.CLIENT_FILE, {"client_id": "dummy-client", "redirect_uri": oauth.REDIRECT_URI})

    def begin(self):
        self.seed_client()
        url = oauth.begin_authorization(dict(oauth.FALLBACK_METADATA))
        return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]

    def callback(self, state):
        return oauth.REDIRECT_URI + "?" + urllib.parse.urlencode({"code": "dummy-code", "state": state})

    def test_private_atomic_files_outside_repo(self):
        oauth._secure_write_json(oauth.TOKENS_FILE, {"access_token": "dummy-access"})
        self.assertEqual(stat.S_IMODE(oauth.TOKENS_FILE.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(oauth._load_json(oauth.TOKENS_FILE)["access_token"], "dummy-access")
        oauth._secure_write_json(oauth.TOKENS_FILE, {"access_token": "new-dummy"})
        self.assertEqual(oauth._load_json(oauth.TOKENS_FILE)["access_token"], "new-dummy")
        self.assertFalse(list(self.directory.glob(".pending-*")))
        with patch.object(oauth, "CREDENTIAL_DIR", oauth.WORKDIR / "credentials"):
            with self.assertRaisesRegex(oauth.OAuthError, "repository"):
                oauth._secure_write_json(oauth.WORKDIR / "credentials/token.json", {})

    def test_file_symlink_and_hardlink_not_followed(self):
        outside = Path(self.tmp.name) / "outside"
        outside.write_text("sentinel")
        outside.chmod(0o600)
        oauth.TOKENS_FILE.symlink_to(outside)
        for action in (lambda: oauth._secure_write_json(oauth.TOKENS_FILE, {}),
                       lambda: oauth._load_json(oauth.TOKENS_FILE)):
            with self.assertRaises(oauth.OAuthError):
                action()
        self.assertEqual(outside.read_text(), "sentinel")
        oauth.TOKENS_FILE.unlink()
        os.link(outside, oauth.TOKENS_FILE)
        with self.assertRaises(oauth.OAuthError):
            oauth._secure_write_json(oauth.TOKENS_FILE, {})
        self.assertEqual(outside.read_text(), "sentinel")

    def test_symlink_ancestor_and_permissive_storage_refused(self):
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(self.directory)
        with patch.object(oauth, "CREDENTIAL_DIR", alias):
            with self.assertRaisesRegex(oauth.OAuthError, "symlink"):
                oauth._secure_write_json(alias / "token.json", {})
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(oauth.OAuthError, "0700"):
            oauth._secure_write_json(oauth.TOKENS_FILE, {})

    def test_permissive_file_refused_without_read(self):
        oauth.TOKENS_FILE.write_text('{"access_token":"dummy"}')
        oauth.TOKENS_FILE.chmod(0o644)
        with self.assertRaises(oauth.OAuthError):
            oauth._load_json(oauth.TOKENS_FILE)

    def test_special_file_refused_without_blocking(self):
        os.mkfifo(oauth.TOKENS_FILE, 0o600)
        with self.assertRaises(oauth.OAuthError):
            oauth._load_json(oauth.TOKENS_FILE)

    def test_discovery_endpoint_substitution_rejected(self):
        for key in ("registration_endpoint", "authorization_endpoint", "token_endpoint"):
            bad = dict(oauth.FALLBACK_METADATA)
            bad[key] = "https://evil.test/steal"
            with self.subTest(key=key), self.assertRaisesRegex(oauth.OAuthError, "approved"):
                oauth._validate_metadata(bad)
        with self.assertRaisesRegex(oauth.OAuthError, "Unapproved"):
            oauth._http_form("http://api.robinhood.com/oauth2/token/", {"code": "dummy"})

    def test_redirect_is_refused(self):
        with self.assertRaisesRegex(oauth.OAuthError, "redirect refused"):
            oauth._NoRedirect().redirect_request(None, None, 307, "", {}, "https://evil.test")

    def test_body_url_and_exception_redacted(self):
        error = urllib.error.HTTPError("https://example.invalid/?code=dummy-code", 500, "dummy-token", {},
                                       io.BytesIO(b"dummy-access-token and instructions"))
        opener = Mock()
        opener.open.side_effect = error
        with patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(oauth.OAuthError) as caught:
                oauth._http_form(oauth.FALLBACK_METADATA["token_endpoint"], {"code": "dummy-code"})
        self.assertEqual(str(caught.exception), "OAuth request failed")

    def test_pkce_state_saved_and_verifier_not_in_authorization_url(self):
        state = self.begin()
        pending = oauth._load_json(oauth.CLIENT_FILE)["pending"]
        self.assertEqual(pending["state"], state)
        self.assertGreaterEqual(len(pending["verifier"]), 43)
        self.assertGreater(len(state), 30)
        url = oauth.authorization_url("dummy-client", "challenge", "state", dict(oauth.FALLBACK_METADATA))
        self.assertNotIn(pending["verifier"], url)

    def test_bare_code_wrong_state_and_wrong_callback_refused_before_http(self):
        state = self.begin()
        bad_callbacks = ["dummy-code", self.callback("wrong"), "https://evil.test/callback?code=x&state=" + state,
                         self.callback(state) + "&state=duplicate", self.callback(state) + "#fragment",
                         self.callback(state).replace("127.0.0.1", "localhost")]
        with patch.object(oauth, "_http_form") as http:
            for value in bad_callbacks:
                with self.subTest(value=value), self.assertRaises(oauth.OAuthError):
                    oauth.exchange_code(value, dict(oauth.FALLBACK_METADATA))
            http.assert_not_called()
        self.assertIn("pending", oauth._load_json(oauth.CLIENT_FILE))

    def test_expired_or_future_state_refused(self):
        state = self.begin()
        record = oauth._load_json(oauth.CLIENT_FILE)
        with patch.object(oauth, "_now", return_value=10000), patch.object(oauth, "_http_form") as http:
            for created in (9000, 10001):
                record["pending"]["created_at"] = created
                oauth._secure_write_json(oauth.CLIENT_FILE, record)
                with self.assertRaises(oauth.OAuthError):
                    oauth.exchange_code(self.callback(state), dict(oauth.FALLBACK_METADATA))
            http.assert_not_called()

    def test_callback_single_use_even_when_network_fails(self):
        state = self.begin()
        with patch.object(oauth, "_http_form", side_effect=oauth.OAuthError("request failed")) as http:
            with self.assertRaises(oauth.OAuthError):
                oauth.exchange_code(self.callback(state), dict(oauth.FALLBACK_METADATA))
            self.assertNotIn("pending", oauth._load_json(oauth.CLIENT_FILE))
            with self.assertRaises(oauth.OAuthError):
                oauth.exchange_code(self.callback(state), dict(oauth.FALLBACK_METADATA))
            self.assertEqual(http.call_count, 1)

    def test_valid_callback_uses_saved_verifier_and_returns_no_tokens(self):
        state = self.begin()
        saved = oauth._load_json(oauth.CLIENT_FILE)["pending"]["verifier"]
        response = {"access_token": "dummy-access", "refresh_token": "dummy-refresh", "expires_in": 300}
        with patch.object(oauth, "_http_form", return_value=response) as http:
            result = oauth.exchange_code(self.callback(state), dict(oauth.FALLBACK_METADATA))
        self.assertEqual(result, {"authorized": True})
        self.assertEqual(http.call_args.args[1]["code_verifier"], saved)
        self.assertEqual(oauth._load_json(oauth.TOKENS_FILE)["access_token"], "dummy-access")
        self.assertNotIn("pending", oauth._load_json(oauth.CLIENT_FILE))

    def test_malformed_token_response_not_written_or_echoed(self):
        for response in ({"access_token": "dummy-secret"},
                         {"access_token": "dummy-secret", "expires_in": True},
                         {"access_token": "dummy-secret", "expires_in": -1}):
            with self.assertRaises(oauth.OAuthError) as caught:
                oauth._store_tokens(response)
            self.assertNotIn("dummy-secret", str(caught.exception))
        self.assertFalse(oauth.TOKENS_FILE.exists())

    def test_cli_accepts_callback_only_stdin_and_outputs_no_secrets(self):
        with patch.object(oauth, "exchange_code", return_value={"authorized": True}) as exchange:
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(self.callback("dummy-state") + "\n")), contextlib.redirect_stdout(output):
                self.assertEqual(oauth.main(["exchange"]), 0)
            self.assertEqual(exchange.call_args.args[0].strip(), self.callback("dummy-state"))
            self.assertNotIn("dummy", output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            oauth.main(["exchange", "--code", "dummy-code"])


if __name__ == "__main__":
    unittest.main()
