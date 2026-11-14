"""Bind an actual downloaded PDF to its original delivery SMS, never a case/week."""
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse, parse_qs

TZ = timezone(timedelta(hours=8))

def timestamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)

def sms_timestamp(raw_date):
    """Decode Messages' Apple epoch; never replace a missing date with now."""
    if isinstance(raw_date, bool) or raw_date is None:
        raise ValueError('missing_message_date')
    try:
        seconds = Decimal(str(raw_date))
    except (InvalidOperation, ValueError):
        raise ValueError('invalid_message_date') from None
    if not seconds.is_finite() or seconds <= 0:
        raise ValueError('invalid_message_date')
    # Older Messages databases stored seconds; current databases store nanoseconds.
    try:
        if seconds > 10_000_000_000:
            seconds /= 1_000_000_000
        return (datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(seconds))).astimezone(TZ)
    except ArithmeticError:
        raise ValueError('invalid_message_date') from None

def message_delivery_keys(text):
    keys = set()
    for url in re.findall(r'https?://[^\s<>"，。；]+', text or ''):
        parsed = urlparse(url)
        if not parsed.hostname or not (parsed.hostname == 'court.gov.cn' or parsed.hostname.endswith('.court.gov.cn')):
            continue
        if 'pagesAjkj/app/wssd/index' not in parsed.fragment:
            continue
        values = parse_qs(parsed.fragment.split('?', 1)[-1])
        if values.get('qdbh') and values.get('sdbh'):
            keys.add(hashlib.sha256((values['qdbh'][0]+'|'+values['sdbh'][0]).encode()).hexdigest()[:20])
    return keys

def verified_binding(document, state):
    """Manifest path+SHA and delivery identifier must both match; return earliest SMS."""
    candidates = []
    errors = []
    document['receipt_binding_errors'] = errors
    def record_error(kind, manifest_path=None, rowid=None):
        # No exception strings or SMS contents: they can contain access-bearing URLs.
        item = {'kind': kind}
        if manifest_path is not None:
            item['manifest_path'] = str(manifest_path)
        if str(rowid or '').isdigit():
            item['source_rowid'] = int(rowid)
        if item not in errors:
            errors.append(item)
    jobs = state.get('message_jobs', {})
    if not isinstance(jobs, dict):
        record_error('invalid_message_jobs')
        return None
    for raw in document.get('paths', [document.get('source_path')]):
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
            manifest_path = path.parent / '.delivery_manifest.json'
        except (TypeError, ValueError, OSError):
            record_error('invalid_document_path')
            continue
        try:
            if not path.is_file() or not manifest_path.is_file():
                record_error('missing_download_manifest_or_pdf', manifest_path)
                continue
            manifest = json.loads(manifest_path.read_text())
            if not isinstance(manifest, dict) or not isinstance(manifest.get('documents'), list):
                raise ValueError('invalid_manifest_shape')
            matched = any(isinstance(item, dict) and isinstance(item.get('path'), str) and Path(item['path']).resolve() == path and item.get('sha256') == document['sha256'] for item in manifest['documents'])
        except (OSError, ValueError, TypeError):
            record_error('invalid_download_manifest', manifest_path)
            continue
        if not matched:
            record_error('manifest_document_mismatch', manifest_path)
            continue
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() != document['sha256']:
                record_error('document_hash_mismatch', manifest_path)
                continue
        except OSError:
            record_error('unreadable_document', manifest_path)
            continue
        for key, job in jobs.items():
            if not isinstance(job, dict) or not isinstance(job.get('message'), dict):
                record_error('invalid_message_job', manifest_path, key)
                continue
            message = job['message']
            try:
                if manifest.get('delivery_key') not in message_delivery_keys(message.get('text', '')):
                    continue
            except (TypeError, ValueError):
                record_error('invalid_sms_url', manifest_path, key)
                continue
            received_at = job.get('received_at')
            rowid = message.get('rowid', key)
            if isinstance(rowid, bool) or not str(rowid).isdigit() or int(rowid) <= 0 or str(key) != str(rowid):
                record_error('invalid_sms_rowid', manifest_path)
                continue
            try:
                received = timestamp(received_at)
                actual = sms_timestamp(message.get('date'))
            except (ValueError, TypeError, OverflowError):
                record_error('invalid_sms_timestamp', manifest_path, rowid)
                continue
            if received.date() != actual.date() or abs((received - actual).total_seconds()) > 0.001:
                record_error('sms_timestamp_mismatch', manifest_path, rowid)
                continue
            if received.date().isoformat() < '2026-01-01' or received > datetime.now(TZ):
                record_error('sms_timestamp_out_of_scope', manifest_path, rowid)
                continue
            candidates.append({'source_rowid': int(rowid), 'received_at': received_at, 'kind': 'delivery_sms',
                               'document_sha256': document['sha256'], 'source_doc': document['id'],
                               'binding_kind': 'download_manifest', 'binding_source': str(manifest_path)})
    return min(candidates, key=lambda x: timestamp(x['received_at'])) if candidates else None

def attach_binding(document, state):
    document['receipt_binding_verified'] = False
    document.pop('receipt_binding', None)
    binding = verified_binding(document, state)
    if binding:
        document['receipt_binding'] = binding
        document['receipt_binding_verified'] = True
        # Keep historical observations; only this exact binding is promoted to evidence.
        if not any(str(x.get('source_rowid')) == str(binding['source_rowid']) and x.get('received_at') == binding['received_at'] for x in document.get('deliveries', [])):
            document.setdefault('deliveries', []).append({'source_rowid': binding['source_rowid'], 'received_at': binding['received_at'], 'binding_verified': True})
    return binding
