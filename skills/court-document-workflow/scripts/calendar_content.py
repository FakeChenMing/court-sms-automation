"""Calendar-facing content; evidence and model audit stay in the review ledger."""
from pathlib import Path
import hashlib
import re

BLOCK = '【法院材料自动登记】'
END_BLOCK = '【自动登记结束】'
POLICY_VERSION = 4
DEADLINE_ALARM_MINUTES = -7 * 24 * 60


def title_action(event, party_names):
    """Keep a verified activity specific without copying party or manual text."""
    action = (event.get('action_label') or '').strip()
    if action:
        if (len(action) > 48 or re.search(r'[\r\n｜|]|（20\d{2}）', action)
                or any(name in action for name in party_names)):
            raise ValueError('action_label只填写经来源核对的具体办理事项，不含案号、姓名或人工标题')
        return action
    # Never infer the action from the current free-form title. The caller may
    # supply an unchanged generated title whose activity has been source-checked.
    # Extract only activity wording, so even an old mistaken name is not copied.
    activity = re.compile(
        r'(?:摇号|抽签|随机)(?:选取|选定|选择)鉴定机构'
        r'|(?:一审|二审|再审)?(?:新|补充)?证据(?:举证|提交)(?:截止|到期|期限届满)?'
        r'|(?:鉴定费|诉讼费|案件受理费|保全费)(?:缴纳|交纳|缴费)(?:截止|到期|期限届满)?'
        r'|(?:进场鉴定|现场勘验|证据交换|开庭|调查|询问|听证)'
    )
    for candidate in (event.get('previous_verified_title', ''), event.get('subject', '')):
        match = activity.search(candidate)
        if match:
            return match.group()
    return event['category']


def calendar_title(event, parties):
    """Build the scan-readable title from already source-verified parties."""
    roles = {}
    for party in parties:
        names = roles.setdefault(party.get('role') or '当事人（角色未明）', [])
        if party['name'] not in names:
            names.append(party['name'])
    names = list(dict.fromkeys(name for group in roles.values() for name in group))
    short = lambda group: group[0] + ('等' if len(group) > 1 else '')
    label = ''
    for left, right, separator in [('原告', '被告', '诉'), ('上诉人', '被上诉人', '与'),
                                   ('申请人', '被申请人', '与'), ('申请方', '被申请方', '与'),
                                   ('申请执行人', '被执行人', '与')]:
        if roles.get(left) and roles.get(right):
            label = short(roles[left]) + separator + short(roles[right])
            others = [name for name in names if name not in roles[left] + roles[right]]
            if others:
                label += '；另涉' + short(others)
            break
    if not label:
        label = '与'.join(names[:2]) + ('等' if len(names) > 2 else '') if names else '当事人未确认'
        if names and (len(names) == 1 or set(roles) <= {'原告', '被告', '被传唤人', '上诉人', '被上诉人'} and len(roles) == 1):
            label = short(names) + '（另一方未确认）'
    action = title_action(event, names)
    if event_kind(event) == 'deadline' and not re.search(r'届满|截止|期限|到期', action):
        action += '截止'
    return event['case_number'] + '｜' + label + '｜' + action


def preserve_title_additions(title, current, previous):
    """Only replace the known generated portion; never erase an edited title."""
    if not current or current == title or current == previous:
        return title
    if previous and current.count(previous) == 1:
        return current.replace(previous, title, 1)
    raise ValueError('日程标题已人工修改，无法可靠分离自动内容，须核对后合并')


def event_kind(event):
    if event.get('event_kind') == 'receipt':
        return 'receipt'
    wording = event.get('category', '') + ' ' + event.get('subject', '')
    if re.search(r'届满|截止|期限|续保申请|续封申请', wording):
        return 'deadline'
    kind = event.get('event_kind')
    if kind in ('appointment', 'deadline'):
        return kind
    # Compatibility for existing, already source-verified decisions.
    return 'deadline' if re.search(r'期限|届满|截止|续保|续封|举证|补证|缴费|履行|异议|复议|上诉', wording) else 'appointment'


def source_attachments(event, owner, documents):
    ids = [owner]
    paths = []
    # A group can include a previously reviewed document outside this model batch.
    # Every member must still resolve to the original PDF bytes, never just notes.
    for member in event.get('receipt_documents', []):
        path = Path(member['source_path'])
        if path.suffix.lower() != '.pdf' or not path.is_file():
            raise ValueError('收到组缺少可读取PDF原件：' + path.name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != member['sha256']:
            raise ValueError('收到组PDF内容与核验SHA不符：' + path.name)
        if str(path) not in paths:
            paths.append(str(path))
    for field in ('date_reference', 'anchor_evidence'):
        if isinstance(event.get(field), dict):
            ids.append(event[field]['source_doc'])
    for docid in dict.fromkeys(ids):
        doc = documents[docid]
        path = Path(doc.get('calendar_pdf_path') or doc['source_path'])
        if path.suffix.lower() != '.pdf':
            raise ValueError('日程来源须提供PDF副本后再登记：' + path.name)
        if str(path) not in paths:
            paths.append(str(path))
    for value in event.get('attachment_paths', []):
        # Cross-document supporting PDFs retained from a verified group are
        # re-hashed by the ledger attachment_manifest before Calendar mutation.
        path = Path(value)
        if path.suffix.lower() != '.pdf' or not path.is_file():
            raise ValueError('日程附件PDF不可读取：' + path.name)
        if str(path) not in paths:
            paths.append(str(path))
    return paths


def preserved_notes(old_notes):
    if BLOCK not in old_notes:
        return old_notes.rstrip()
    before, managed = old_notes.split(BLOCK, 1)
    if END_BLOCK in managed:
        after = managed.split(END_BLOCK, 1)[1].strip()
    else:
        # The old format had no end marker. Keep user additions after its ID,
        # excluding only the two known generated trailing fields.
        lines = managed.splitlines()
        marker = next((i for i, line in enumerate(lines) if line.startswith('法院材料事项ID：')), None)
        tail = lines[marker + 1:] if marker is not None else []
        after = '\n'.join(line for line in tail if not line.startswith(('所指排期来源：', '结束时间为日历预留'))).strip()
    return '\n\n'.join(x for x in (before.rstrip(), after) if x)


def concise_notes(event, parties, owner, documents, old_notes=''):
    """Keep manual text outside the managed block, never dump evidence JSON."""
    preserved = preserved_notes(old_notes) if old_notes else ''
    lines = [BLOCK, '案号：' + event['case_number']]
    roles = {}
    for party in parties:
        role = party.get('role') or '当事人（角色未明）'
        if role in ('未知', 'unknown'):
            role = '当事人（角色未明）'
        names = roles.setdefault(role, [])
        if party['name'] not in names:
            names.append(party['name'])
    if roles:
        lines.append('当事人信息')
    lines.extend(role + '：' + '、'.join(names) for role, names in roles.items())
    if not roles:
        lines.append('当事人：现有材料未确认')
    kind = event_kind(event)
    if kind in ('receipt', 'deadline'):
        lines.append('收到日期：' + event.get('received_date', '待核') + '（对应文书送达通知短信收到日）')
    if kind == 'receipt':
        for due in event.get('due_dates', []):
            lines.append('到期日期：' + due['date'] + '（' + due['subject'] + '）')
        if event.get('pending_deadlines') or not event.get('due_dates'):
            # The full legal/source audit stays in the pending list. Truncating
            # that text here exposes tool ids and can cut a sentence in half.
            lines.append('到期日期：待核（核实起算日期及具体期限后补录）')
    if kind == 'deadline':
        lines.append('到期日期：' + event['start'][:10] + ('（全天）' if event.get('all_day') else ' ' + event['start'][11:16]))
        lines.append('提醒：提前7日')
    # This field is deliberately separate from the internal legal explanation.
    summary = (event.get('action_summary') or '').strip()
    if summary:
        if len(summary) > 160 or '\n' in summary:
            raise ValueError('办理要点须为160字以内的一段')
        lines.append('办理：' + summary)
    members = event.get('receipt_documents', [])
    if members:
        lines.append('收到材料：' + '；'.join(dict.fromkeys(m['material_type'] for m in members)))
        lines.append('依据：' + '；'.join(dict.fromkeys(Path(m['source_path']).name + f' 第{m["source_page"]}页' for m in members)) + '（见附件）')
    else:
        source = Path(documents[owner]['source_path']).name
        lines.append(f'依据：{source} 第{event["source_page"]}页（见附件）')
    if event.get('date_reference'):
        ref = event['date_reference']
        lines.append('日期依据：' + Path(documents[ref['source_doc']]['source_path']).name + f' 第{ref["source_page"]}页（见附件）')
    sources = []
    for party in parties:
        if party.get('source_message_id'):
            label = '同案历次短信'
        else:
            doc = documents.get(party.get('source_doc'))
            if not doc:
                continue
            label = Path(doc['source_path']).name + f' 第{party["source_page"]}页'
        if label not in sources:
            sources.append(label)
    if sources:
        lines.append('主体依据：' + '；'.join(sources))
    if not event.get('all_day'):
        lines.append('结束时间为日历预留。')
    if event.get('uncertainty'):
        # Detailed uncertainties remain in the ledger; do not bury the appointment.
        lines.append('另有待核事项，见待核清单。')
    lines.append('法院材料事项ID：' + event['event_key'])
    lines.append(END_BLOCK)
    return (preserved + '\n\n' if preserved else '') + '\n'.join(lines)
