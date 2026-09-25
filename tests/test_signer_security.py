"""Authenticated producer path: signer, policy source-ID config, hook wiring.

These tests pin the runtime behavior of the entry pipeline's authenticated
producer path: watch.py -> sign_alerts.py -> fire_entries.py -> admission.
The fail-closed requirement: in live mode, an entry alert is admitted only
when it carries a valid HMAC signature over admission.signed_bytes, produced
with the local source key, and its source_id matches the policy's expected
identity for its handle.
"""
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import admission
import config
import sign_alerts

NOW = datetime(2026, 9, 22, 16, 0, 0, tzinfo=timezone.utc)
KEY = bytes.fromhex("ab" * 32)
SOURCE_IDS = {"cassytrades": "cassytrades", "clintoptions": "clintoptions"}


def policy(sources=None, key=KEY):
    return config.Policy(sources=sources or dict(SOURCE_IDS), source_key=key)


def entry_alert(handle="cassytrades", text="$QQQ 734c .56 0DTE"):
    return {"id": "123", "handle": handle, "text": text,
            "posted_at": NOW.isoformat(), "url": f"https://x.com/{handle}/status/123",
            "type": "entry"}


def signed(alert, key=KEY, sources=None):
    alert = dict(alert)
    handle = alert["handle"].lstrip("@").lower()
    alert["source_id"] = (sources or SOURCE_IDS)[handle]
    alert["signature"] = hmac.new(key, admission.signed_bytes(alert),
                                  hashlib.sha256).hexdigest()
    return alert


class SignerTests(unittest.TestCase):
    def test_signs_entry_alerts_with_source_id_and_hmac(self):
        data = {"status": "alert", "alerts": [entry_alert()]}
        out, n = sign_alerts.sign_alerts(data, KEY)
        self.assertEqual(n, 1)
        alert = out["alerts"][0]
        self.assertEqual(alert["source_id"], "cassytrades")
        self.assertRegex(alert["signature"], r"^[0-9a-f]{64}$")
        # The signature binds the exact bytes admission verifies.
        self.assertEqual(alert["signature"], signed(entry_alert())["signature"])
        # fire_entries downstream can admit the signed alert in live mode.
        admitted = admission.admit(alert, "live", policy(), NOW)
        self.assertEqual(admitted["handle"], "cassytrades")

    def test_exit_alerts_pass_through_unsigned(self):
        alert = entry_alert()
        alert["type"] = "exit"
        data = {"status": "alert", "alerts": [alert]}
        out, n = sign_alerts.sign_alerts(data, KEY)
        self.assertEqual(n, 0)
        self.assertNotIn("signature", out["alerts"][0])
        self.assertNotIn("source_id", out["alerts"][0])

    def test_malformed_alerts_pass_through_unsigned(self):
        bad = {"id": "1", "handle": "cassytrades", "text": 123}  # missing fields, wrong type
        data = {"status": "alert", "alerts": [bad]}
        out, n = sign_alerts.sign_alerts(data, KEY)
        self.assertEqual(n, 0)
        self.assertEqual(out["alerts"][0], bad)

    def test_tampered_text_breaks_signature(self):
        alert = signed(entry_alert())
        alert["text"] = "$QQQ 734c 99.99 0DTE"  # tampered after signing
        with self.assertRaises(admission.AdmissionError) as ctx:
            admission.admit(alert, "live", policy(), NOW)
        self.assertEqual(str(ctx.exception), "invalid_source_signature")

    def test_clean_text_skips_stacked_chrome_headers(self):
        # Real X article views can stack several header blocks
        # (display name / @handle / timestamp), e.g. repost + post.
        # 2026-09-23: two stacked headers made clean_text return "Cass 🌙",
        # so the resolver failed closed on a genuine $SPY 770 PUTS entry.
        raw = ("Cass 🌙\n@CassyTrades\n17s\n"
               "Cass 🌙\n@CassyTrades\n17m\n"
               "$SPY 770 PUTS .30\n7")
        self.assertEqual(sign_alerts.clean_text(raw), "$SPY 770 PUTS .30")
        # Single header still works.
        single = "Cass 🌙\n@CassyTrades\n45s\n$SPY 770 PUTS .30\n3\n3\n1K"
        self.assertEqual(sign_alerts.clean_text(single), "$SPY 770 PUTS .30")
        # No chrome passes through unchanged.
        self.assertEqual(sign_alerts.clean_text("$SPY 770 PUTS .30"),
                         "$SPY 770 PUTS .30")

    def test_unsigned_alert_rejected_in_live_mode(self):
        # Missing the whole authenticated-producer envelope fails at schema.
        with self.assertRaises(admission.AdmissionError) as ctx:
            admission.admit(entry_alert(), "live", policy(), NOW)
        self.assertEqual(str(ctx.exception), "invalid_alert_schema")
        # Present source identity but no signature: signature check fails.
        alert = dict(entry_alert(), source_id="cassytrades")
        with self.assertRaises(admission.AdmissionError) as ctx:
            admission.admit(alert, "live", policy(), NOW)
        self.assertEqual(str(ctx.exception), "invalid_source_signature")

    def test_wrong_key_signature_rejected(self):
        alert = signed(entry_alert(), key=bytes.fromhex("cd" * 32))
        with self.assertRaises(admission.AdmissionError) as ctx:
            admission.admit(alert, "live", policy(), NOW)
        self.assertEqual(str(ctx.exception), "invalid_source_signature")

    def test_source_id_mismatch_rejected(self):
        alert = signed(entry_alert(), sources={"cassytrades": "impostor"})
        with self.assertRaises(admission.AdmissionError) as ctx:
            admission.admit(alert, "live", policy(), NOW)
        self.assertEqual(str(ctx.exception), "source_identity_mismatch")

    def test_missing_source_key_fails_closed(self):
        with self.assertRaises(admission.AdmissionError) as ctx:
            admission.admit(signed(entry_alert()), "live", policy(key=None), NOW)
        self.assertEqual(str(ctx.exception), "verified_source_channel_required")


class SourceKeyConfigTests(unittest.TestCase):
    def write_key(self, contents):
        tmp = tempfile.NamedTemporaryFile("w", delete=False)
        tmp.write(contents)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_load_key_requires_32_bytes_of_hex(self):
        self.assertEqual(sign_alerts.load_key(self.write_key("ab" * 32)), KEY)
        with self.assertRaises(SystemExit):
            sign_alerts.load_key(self.write_key("ab" * 31))
        with self.assertRaises(SystemExit):
            sign_alerts.load_key(self.write_key("not-hex"))

    def test_config_loads_source_key_from_file(self):
        path = self.write_key("ab" * 32)
        with mock.patch.dict(os.environ, {"COPYTRADER_SOURCE_KEY_FILE": path,
                                           "COPYTRADER_CONFIG": ""}):
            os.environ.pop("COPYTRADER_CONFIG", None)
            p = config.load_policy()
        self.assertEqual(p.source_key, KEY)

    def test_config_missing_key_file_gives_none(self):
        with mock.patch.dict(os.environ, {"COPYTRADER_SOURCE_KEY_FILE": "/nonexistent/key"}):
            p = config.load_policy()
        self.assertIsNone(p.source_key)

    def test_runtime_default_policy_requires_source_config(self):
        # The shipped default Policy has sources={handle: None}. In live mode
        # admission requires an expected source identity, so deploying without
        # configuring policy.sources fails closed instead of trading.
        p = config.Policy(source_key=KEY)
        with self.assertRaises(admission.AdmissionError) as ctx:
            admission.admit(signed(entry_alert()), "live", p, NOW)
        self.assertEqual(str(ctx.exception), "verified_source_channel_required")

    def test_configured_sources_match_signer_identities(self):
        # The deployment-time policy must map each handle to the same source_id
        # the signer attaches; otherwise every signed alert is rejected.
        p = policy()
        self.assertEqual(set(p.sources), set(sign_alerts.SOURCE_IDS))
        self.assertEqual(p.sources, sign_alerts.SOURCE_IDS)

    def test_policy_rejects_duplicate_or_unknown_source_ids(self):
        with self.assertRaises(ValueError):
            config.Policy(sources={"cassytrades": "x", "clintoptions": "x",
                                   "capricekayem": "y"})
        with self.assertRaises(ValueError):
            config.Policy(sources={"unknown_handle": "x"})

    def test_key_never_printed_by_signer_main(self):
        import io
        data = {"status": "alert", "alerts": [entry_alert()]}
        infile = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        infile.write(json.dumps(data))
        infile.close()
        self.addCleanup(os.unlink, infile.name)
        path = self.write_key("ab" * 32)
        with mock.patch.dict(os.environ, {"COPYTRADER_SOURCE_KEY_FILE": path}):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
                self.assertEqual(sign_alerts.main([infile.name]), 0)
        self.assertNotIn("ab" * 32, out.getvalue())
        self.assertNotIn("ab" * 32, err.getvalue())
        written = json.loads(out.getvalue())
        self.assertIn("signature", written["alerts"][0])


# The guarded runner sandboxes HOME but exports the real one here.
_REAL_HOME = os.environ.get("COPYTRADER_TEST_ORIGINAL_HOME") or str(
    Path(__file__).resolve().parents[3])
HOOK_PATH = Path(_REAL_HOME) / "hooks/scripts/trade-post-watcher.sh"


@unittest.skipIf(not HOOK_PATH.exists(),
                 "trade-post-watcher.sh not installed at the real home (sandboxed run)")
@unittest.skipIf(os.environ.get("COPYTRADER_TEST_WORKER") == "1",
                 "guarded runner blocks reads outside the isolated HOME")
class HookWiringTests(unittest.TestCase):
    def hook(self):
        # Anchor on the repo checkout, not $HOME: the guarded test runner
        # sandboxes HOME, but the hook under test lives at the real home.
        return HOOK_PATH.read_text()

    def test_hook_signs_before_firing_entries(self):
        text = self.hook()
        sign_pos = text.find("sign_alerts.py")
        # The real invocation, not the comment mentioning fire_entries.py.
        fire_pos = text.find('"$HOME/workspace/copy-trader/fire_entries.py"')
        self.assertGreater(sign_pos, 0)
        self.assertGreater(fire_pos, sign_pos,
                           "signer must run before the entry engine in the pipe")

    def test_hook_only_fires_when_armed(self):
        text = self.hook()
        armed_pos = text.find("workspace/copy-trader/ARMED")
        fire_pos = text.find('"$HOME/workspace/copy-trader/fire_entries.py"')
        self.assertGreater(armed_pos, 0)
        self.assertGreater(fire_pos, armed_pos,
                           "entry engine must be gated on the ARMED marker")

    def test_hook_uses_live_mode(self):
        self.assertIn("--mode live", self.hook())

    def test_hook_does_not_bypass_signer(self):
        # The pipe into fire_entries must come from the signer, not raw $RESULT.
        text = self.hook()
        line = next(l for l in text.splitlines() if "fire_entries.py" in l)
        self.assertNotIn("$RESULT", line)


if __name__ == "__main__":
    unittest.main()
