#!/usr/bin/env python3
"""Sign trade-watcher alerts for the copy-trader admission gate.

Reads the trade-post-watcher JSON ({"status":..., "alerts":[...], ...}) from
stdin or a file, and for each alert that carries all admission FIELDS as
strings, attaches:
  - source_id: the expected producer identity for the alert's handle
  - signature: HMAC-SHA256 hex over admission.signed_bytes(alert), keyed by the
    source key at ~/.local/share/copypasta/source_key (0600, outside the repo).

Alerts that are malformed are passed through UNSIGNED so admission rejects
them fail-closed with a clear reason. Never prints the key.

This is the authenticated producer path: only alerts signed with the local
source key can pass admission in live mode.
"""
import hashlib
import hmac
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from admission import FIELDS, signed_bytes

DEFAULT_KEY_PATH = os.path.expanduser("~/.local/share/copypasta/source_key")

# Expected producer identity per handle. The handle itself is the stable
# identity; the signature binds every field, so this is a second factor.
SOURCE_IDS = {
    "cassytrades": "cassytrades",
    "clintoptions": "clintoptions",
}


def load_key(path=None):
    path = path or os.environ.get("COPYTRADER_SOURCE_KEY_FILE", DEFAULT_KEY_PATH)
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        raise SystemExit("source key file is not hex")
    if len(key) < 32:
        raise SystemExit("source key too short")
    return key


REQUIRED = FIELDS - {"source_id", "signature"}

# X article chrome: the scraped text carries a header (display name / @handle /
# relative-time lines) and the post body often has trailing commentary and an
# engagement count ("1.3K"). The header trips the resolver's entry-prefix
# check and the trailer looks like a second price, so reduce to the trade
# instruction: the first content line after the header. The trade line
# (ticker/strike/side/expiry/premium) is conventionally one line; anything
# else falls through to the resolver, which fails closed on ambiguity.
# Conservative: if the header pattern does not clearly match, or the first
# content line is empty, return text unchanged and let the resolver decide.
_TIMESTAMP_RE = re.compile(r"^(\d+[smh]|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2})$")


def clean_text(text):
    lines = text.split("\n")
    start = 0
    # Skip ALL leading header blocks (display name / @handle / timestamp);
    # X article views can stack several (e.g. repost + post). Do not break
    # after the first or the "first content line" below lands mid-chrome.
    for i, line in enumerate(lines[:10]):
        if line.startswith("@") and i + 1 < len(lines) and _TIMESTAMP_RE.match(lines[i + 1].strip()):
            start = i + 2
    first = "\n".join(lines[start:]).strip().split("\n", 1)[0].strip()
    return first or text


def signable(alert):
    return (
        isinstance(alert, dict)
        and REQUIRED <= set(alert)
        and all(isinstance(alert[k], str) for k in REQUIRED)
        and alert.get("type") == "entry"
        and alert["handle"].lstrip("@").lower() in SOURCE_IDS
    )


def sign_alerts(data, key):
    alerts = data.get("alerts")
    if not isinstance(alerts, list):
        raise ValueError("invalid watcher payload: alerts must be a list")
    signed = 0
    out = []
    for alert in alerts:
        if not signable(alert):
            out.append(alert)
            continue
        handle = alert["handle"].lstrip("@").lower()
        alert = dict(alert)
        alert["text"] = clean_text(alert["text"])
        alert["source_id"] = SOURCE_IDS[handle]
        alert["signature"] = hmac.new(key, signed_bytes(alert), hashlib.sha256).hexdigest()
        signed += 1
        out.append(alert)
    return dict(data, alerts=out), signed


def main(argv=None):
    args = (argv or sys.argv[1:])
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    raw = sys.stdin.read(1024 * 1024 + 1) if not args else open(args[0], encoding="utf-8").read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise SystemExit("input too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise SystemExit("invalid JSON input")
    if not isinstance(data, dict):
        raise SystemExit("invalid watcher payload")
    key = load_key()
    signed_data, n = sign_alerts(data, key)
    sys.stdout.write(json.dumps(signed_data))
    print(f"signed {n} alert(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
