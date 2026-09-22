"""Strict admission. Source text and producer flags never grant authority."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from urllib.parse import urlparse


class AdmissionError(ValueError):
    pass


FIELDS = {'id', 'handle', 'source_id', 'text', 'posted_at', 'url', 'type'}


def signed_bytes(alert):
    return json.dumps({k: alert[k] for k in sorted(FIELDS)}, ensure_ascii=True,
                      sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def admit(alert, mode, policy, now):
    if not isinstance(alert, dict) or set(alert) - (FIELDS | {'signature'}) or not FIELDS <= set(alert):
        raise AdmissionError('invalid_alert_schema')
    if any(not isinstance(alert[key], str) for key in FIELDS):
        raise AdmissionError('invalid_alert_field')
    if alert['type'] != 'entry' or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', alert['id']):
        raise AdmissionError('invalid_alert_identity')
    handle = alert['handle'].lstrip('@').lower()
    if not re.fullmatch(r'[a-z0-9_]{1,32}', handle) or handle not in policy.sources:
        raise AdmissionError('unapproved_source')
    if not 0 < len(alert['text']) <= 2000 or len(alert['url']) > 256:
        raise AdmissionError('invalid_alert_size')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', alert['source_id']):
        raise AdmissionError('invalid_source_identity')
    try:
        url = urlparse(alert['url'])
    except ValueError:
        raise AdmissionError('invalid_source_url') from None
    if url.scheme != 'https' or url.netloc != 'x.com' or url.params or url.query or url.fragment:
        raise AdmissionError('invalid_source_url')
    if url.path != f'/{handle}/status/{alert["id"]}':
        raise AdmissionError('source_url_mismatch')
    try:
        posted = datetime.fromisoformat(alert['posted_at'])
        if posted.tzinfo is None or now.tzinfo is None:
            raise ValueError()
        age = (now.astimezone(timezone.utc) - posted.astimezone(timezone.utc)).total_seconds()
    except (ValueError, TypeError):
        raise AdmissionError('invalid_alert_timestamp') from None
    if not 0 <= age <= policy.max_age_seconds:
        raise AdmissionError('stale_or_future_alert')
    expected_id = policy.sources[handle]
    if expected_id is not None and not hmac.compare_digest(alert['source_id'], expected_id):
        raise AdmissionError('source_identity_mismatch')
    if mode == 'live':
        if not expected_id or not isinstance(policy.source_key, bytes) or len(policy.source_key) < 32:
            raise AdmissionError('verified_source_channel_required')
        signature = alert.get('signature')
        if not isinstance(signature, str) or not re.fullmatch(r'[0-9a-f]{64}', signature):
            raise AdmissionError('invalid_source_signature')
        expected = hmac.new(policy.source_key, signed_bytes(alert), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise AdmissionError('invalid_source_signature')
    return dict(alert, handle=handle)
