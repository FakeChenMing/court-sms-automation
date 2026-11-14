"""Build receipt/deadline pairs from reviewed material and exact transport evidence."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from receipt_binding import timestamp
from calendar_content import event_kind

def receipt_key(case, document):
    received = document if isinstance(document, str) else document['receipt_binding']['received_at']
    return case + '|收到材料|' + timestamp(received).date().isoformat()


def _union(values):
    result = []
    for value in values:
        if value not in result:
            result.append(deepcopy(value))
    return result


def combine_receipt_events(events):
    """Union reviewed members, retaining the earliest known receipt for each SHA.

    This does not validate a review. The worker and ledger must independently
    verify each member's binding and review_basis before writing any group.
    """
    groups, members = {}, {}
    for event in events:
        case = event['case_number']
        for member in event['receipt_documents']:
            identity = (case, member['sha256'])
            previous = members.get(identity)
            if previous is None:
                members[identity] = deepcopy(member)
                continue
            earliest, other = (member, previous) if timestamp(member['binding']['received_at']) < timestamp(previous['binding']['received_at']) else (previous, member)
            merged = deepcopy(earliest)
            for field in ('due_dates', 'pending_deadlines', 'attachment_paths'):
                merged[field] = _union(previous.get(field, []) + member.get(field, []))
            members[identity] = merged
    for (case, _sha), member in members.items():
        key = receipt_key(case, member['binding']['received_at'])
        groups.setdefault(key, {'case_number': case, 'members': []})['members'].append(member)
    result = []
    for key, group in groups.items():
        case, grouped = group['case_number'], group['members']
        source_events = [e for e in events if e['case_number'] == case and any(m['sha256'] in {v['sha256'] for v in grouped} for m in e['receipt_documents'])]
        event = deepcopy(source_events[0])
        first = grouped[0]
        received = key.rsplit('|', 1)[1]
        start = timestamp(received + 'T00:00:00+08:00')
        labels = _union(m['material_type'] for m in grouped)
        label = labels[0] if len(labels) == 1 else '、'.join(labels[:2]) + ('等' if len(labels) > 2 else '')
        if len(label) > 38:
            label = labels[0][:20] + '等材料'
        event.update(case_number=case, event_key=key, owner_doc=first['id'],
                     event_kind='receipt', basis_type='document_received', category='收到材料',
                     received_date=received, received_evidence=deepcopy(first['binding']),
                     receipt_documents=grouped, subject=label, action_label='收到' + label,
                     title=case + '｜收到' + label, start=start.isoformat(),
                     end=(start + timedelta(days=1)).isoformat(), all_day=True,
                     source_page=first['source_page'], source_quote=first['source_quote'])
        event['due_dates'] = _union(d for m in grouped for d in m.get('due_dates', []))
        # Same deadline key with contradictory dates requires explicit source review.
        dates = {}
        for due in event['due_dates']:
            due_key = due.get('event_key') or due['subject']
            if due_key in dates and dates[due_key] != due['date']:
                raise ValueError('收到组同一到期事项存在日期冲突：' + due_key)
            dates[due_key] = due['date']
        event['pending_deadlines'] = _union(p for m in grouped for p in m.get('pending_deadlines', []))
        event['attachment_paths'] = _union([m['source_path'] for m in grouped] + [p for m in grouped for p in m.get('attachment_paths', [])])
        event['previous_receipt_keys'] = _union([e.get('event_key') for e in source_events if e.get('event_key') and e.get('event_key') != key] + [case + '|收到材料|' + m['sha256'] for m in grouped])
        result.append(event)
    return result


def retained_receipt_events(case, incoming, state):
    """Recover members from the same day and earlier receipts of the same PDF."""
    keys = {e['event_key'] for e in incoming}
    hashes = {m['sha256'] for e in incoming for m in e['receipt_documents']}
    retained = []
    seen = set()
    saved = state.get('events', {})
    for group in state.get('receipt_groups', {}).values():
        for old in group.get('receipt_decision', {}).get('events', []):
            key = old.get('event_key', '')
            if old.get('case_number') != case or not old.get('receipt_documents'):
                continue
            if key not in keys and not hashes.intersection(m['sha256'] for m in old['receipt_documents']):
                continue
            event = deepcopy(old)
            if saved.get(key, {}).get('verified'):
                for member in event['receipt_documents']:
                    member.update(review_basis='verified_receipt', receipt_event_key=key)
            retained.append(event)
            seen.add(key)
    for docid, doc in state.get('documents', {}).items():
        for slot in ('decision', 'receipt_decision'):
            for old in doc.get(slot, {}).get('events', []):
                key = old.get('event_key', '')
                if key in seen or not key.startswith(case + '|收到材料|') or not saved.get(key, {}).get('verified'):
                    continue
                binding = old.get('received_evidence') or doc.get('receipt_binding')
                sha = doc.get('sha256') or (binding or {}).get('document_sha256')
                if not binding or not sha:
                    continue
                if receipt_key(case, binding['received_at']) not in keys and sha not in hashes:
                    continue
                source = next((p for p in old.get('attachment_paths', []) if p in doc.get('paths', [])), None) or next(iter(doc.get('paths', [])), None)
                if not source or not old.get('source_page') or not old.get('source_quote'):
                    continue
                event = deepcopy(old)
                event.update(case_number=case, owner_doc=docid, receipt_documents=[{
                    'id': docid, 'sha256': sha, 'source_path': source, 'binding': deepcopy(binding),
                    'source_page': old['source_page'], 'source_quote': old['source_quote'],
                    'material_type': old.get('subject') or Path(source).stem,
                    'review_basis': 'verified_receipt', 'receipt_event_key': key,
                    'due_dates': deepcopy(old.get('due_dates', [])),
                    'pending_deadlines': deepcopy(old.get('pending_deadlines', [])),
                    'attachment_paths': deepcopy(old.get('attachment_paths', [])),
                }])
                retained.append(event)
                seen.add(key)
    return retained

def add_receipt_nodes(result, documents):
    byid = {d['id']: d for d in documents}
    case = result['case_number']
    events = [e for e in result['events'] if event_kind(e) != 'receipt']
    materials = {m['owner_doc']: m for m in result.get('receipt_materials', [])}
    for event in events:
        if event_kind(event) == 'deadline':
            materials.setdefault(event['owner_doc'], {k: event[k] for k in ('owner_doc', 'source_page', 'source_quote')})
    for check in result.get('pending_checks', []):
        if check.get('deadline_related'):
            materials.setdefault(check['owner_doc'], {k: check[k] for k in ('owner_doc', 'source_page', 'source_quote')})
    for owner, material in materials.items():
        doc = byid[owner]
        binding = doc.get('receipt_binding')
        deadlines = [e for e in events if e['owner_doc'] == owner and event_kind(e) == 'deadline']
        if not binding:
            # A missing receipt never licenses a date guessed from a directory.
            # Keep the reviewed proposal pending so the known deadline isn't silently dropped.
            if deadlines:
                raise ValueError('已核实到期日，但本件收到记录未精确关联；须补核收到记录后登记双节点：' + owner)
            issue = '本件收到日期尚无精确下载清单/短信关联，不能以同案归档周代替'
            if not any(c.get('owner_doc') == owner and c.get('issue') == issue for c in result.get('pending_checks', [])):
                result.setdefault('pending_checks', []).append({'owner_doc': owner, 'category': '收到日期待核', 'subject': '文书收到日期', 'source_page': material['source_page'], 'source_quote': material['source_quote'], 'issue': issue, 'basis_type': 'internal_check'})
            continue
        received = timestamp(binding['received_at']).date().isoformat()
        key = receipt_key(case, doc)
        dates = []
        for event in deadlines:
            event['event_kind'] = 'deadline'
            event.setdefault('event_key', case + '|' + event['category'] + '|' + event['subject'])
            event.update(received_date=received, received_evidence=deepcopy(binding), due_date=event['start'][:10], paired_receipt_key=key)
            dates.append({'subject': event.get('action_label') or event['subject'], 'date': event['due_date'], 'event_key': event['event_key']})
        pending = [c.get('issue') or c.get('uncertainty') or c['subject'] for c in result.get('pending_checks', []) if c['owner_doc'] == owner and c.get('deadline_related')]
        label = material.get('document_label') or Path(doc['source_path']).stem
        if len(label) > 42:
            label = '期限材料'
        start = timestamp(received + 'T00:00:00+08:00')
        member = {'id': owner, 'sha256': doc['source_sha256'], 'source_path': doc['source_path'],
                  'binding': deepcopy(binding), 'source_page': material['source_page'],
                  'source_quote': material['source_quote'], 'material_type': label,
                  'review_basis': 'complete_document_review', 'due_dates': dates,
                  'pending_deadlines': pending,
                  'attachment_paths': _union(byid[e[field]['source_doc']]['source_path'] for e in deadlines for field in ('date_reference','anchor_evidence') if isinstance(e.get(field),dict))}
        events.append({'owner_doc': owner, 'case_number': case, 'category': '收到材料', 'subject': label,
                       'action_label': '收到' + label, 'title': case + '｜收到' + label,
                       'event_kind': 'receipt', 'basis_type': 'document_received',
                       'start': start.isoformat(), 'end': (start + timedelta(days=1)).isoformat(),
                       'all_day': True, 'location': '', 'event_key': key,
                       'received_date': received, 'received_evidence': deepcopy(binding),
                       'receipt_documents': [member],
                       'due_dates': dates, 'pending_deadlines': pending,
                       'source_page': material['source_page'], 'source_quote': material['source_quote'],
                       'explanation': '已核对本件PDF与送达通知短信下载清单；收到记录不替代法律送达认定'})
    result['events'] = [e for e in events if event_kind(e) != 'receipt'] + combine_receipt_events([e for e in events if event_kind(e) == 'receipt'])
    return result
