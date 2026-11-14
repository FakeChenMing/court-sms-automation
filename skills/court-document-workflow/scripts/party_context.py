"""Read-only same-case message context and conservative party-claim guards.

Historical context does not enqueue/download anything or advance the SMS cursor.
Only complete case-number matches (or existing document-delivery row IDs) survive.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import html
import re
import sqlite3
import unicodedata

TZ = timezone(timedelta(hours=8))
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
MIN_RECEIVED = datetime(2026, 1, 1, tzinfo=TZ)
CASE_RE = re.compile(r"\(\s*(20\d{2})\s*\)\s*([\u3400-\u9fffA-Za-z0-9\s]{3,40}?号)")
URL_RE = re.compile(r"https?://[^\s<>\"'，。；、（）【】]+")
ROLE_RE = re.compile(r"被申请执行人|申请执行人|被申请方|被申请人|申请方|申请人|被上诉人|上诉人|被告|原告|第三人|被执行人|执行申请人|保全担保人|担保人")


def normalized(value: str) -> str:
    return re.sub(r"[\s\u200b\ufeff]+", "", unicodedata.normalize("NFKC", value or ""))


def case_numbers(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "")
    return {"(" + y + ")" + normalized(suffix) for y, suffix in CASE_RE.findall(text)}


def message_received_at(raw_date) -> datetime | None:
    try:
        seconds = float(raw_date)
        if not seconds:
            return None
        if abs(seconds) > 10_000_000_000:
            seconds /= 1_000_000_000
        return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone(TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def decode_message_text(text, attributed_body) -> str:
    if text:
        return str(text)
    if not attributed_body:
        return ""
    # The existing downloader's NSString extraction; do not include detector
    # archives, duplicate URLs, or binary metadata as purported SMS evidence.
    decoded = bytes(attributed_body).decode("utf-8", errors="ignore")
    marker = decoded.find("NSString")
    if marker >= 0:
        payload = decoded[marker + len("NSString"):]
        start = payload.find("【")
        if start < 0:
            match = re.search(r"[\u3400-\u9fff]", payload)
            start = match.start() if match else -1
        if start >= 0:
            return html.unescape(re.split(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", payload[start:], maxsplit=1)[0].strip())
    return ""  # Unparsed attributed archives are not reliable quotations.


def _state_received(job: dict, message: dict) -> datetime | None:
    raw = message_received_at(message.get("date"))
    if raw is not None:
        return raw
    try:
        received = datetime.fromisoformat(str(job.get("received_at", "")).replace("Z", "+00:00"))
        return received.replace(tzinfo=TZ) if received.tzinfo is None else received.astimezone(TZ)
    except ValueError:
        return None


def _greeting_names(text: str) -> list[str]:
    # This is exclusion metadata, never a list of parties to add.
    return list(dict.fromkeys(re.findall(r"(?:^|】|[，。；\n])\s*([^，。；：:\n【】]{1,60}?)\s*(?:你好|您好)[，,！!。]", text)))


def collect_case_messages(case: str, state: dict, documents, chat_db: Path | str | None = None) -> dict:
    """Return {messages, source_status, errors}; all paths are read-only.

    documents may be a list or the document_review.json documents mapping.
    chat_db=None means state-only (explicitly reported as not_checked).
    Source errors remain visible to the worker and do not fabricate completeness.
    """
    wanted = normalized(case)
    if case_numbers(case) != {wanted}:
        raise ValueError("必须使用完整案号关联短信")
    docs = documents.values() if isinstance(documents, dict) else documents
    linked = {str(delivery["source_rowid"])
              for doc in docs if normalized(doc.get("case_number", "")) == wanted
              for delivery in doc.get("deliveries", [])
              if delivery.get("source_rowid") is not None}
    found = {}
    errors = []

    def add(rowid, text, received, source):
        if not rowid or not text or received is None or received < MIN_RECEIVED:
            return
        if wanted not in case_numbers(text) and str(rowid) not in linked:
            return
        rowid = str(rowid)
        sanitized = URL_RE.sub("[送达链接已省略]", text)
        item = found.get(rowid)
        if item:
            if source not in item["context_sources"]:
                item["context_sources"].append(source)
            # Current database is the owning source. State snapshots can have
            # earlier/later parser differences; keep the discrepancy visible.
            if source == "messages_database" and normalized(item["text"]) != normalized(sanitized):
                item["state_text_differs"] = True
                item["text"] = sanitized
                item["recipient_names_only"] = _greeting_names(sanitized)
            return
        found[rowid] = {"id": rowid, "received_at": received.isoformat(), "text": sanitized,
                        "context_sources": [source], "recipient_names_only": _greeting_names(sanitized)}

    for job in state.get("message_jobs", {}).values():
        message = job.get("message", {})
        if not isinstance(message, dict):
            continue
        add(message.get("rowid"), message.get("text", ""), _state_received(job, message), "download_state")
    state_count = len(found)
    status = {"download_state": "read", "download_state_matches": state_count,
              "messages_database": "not_checked", "history_min_received_at": MIN_RECEIVED.isoformat()}
    if chat_db is not None:
        try:
            # Numeric tail only narrows the query; it never establishes identity.
            serial = re.search(r"(\d+)号$", wanted)
            if not serial:
                raise ValueError("案号末尾序号缺失")
            needle = serial.group(1)
            wide_needle = "".join(chr(ord(c) + 0xFEE0) for c in needle)
            min_seconds = (MIN_RECEIVED - APPLE_EPOCH).total_seconds()
            conn = sqlite3.connect(Path(chat_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
            try:
                conn.execute("PRAGMA query_only=ON")
                # instr(BLOB, BLOB) is needed: LIKE on the text cast stops at
                # embedded NUL bytes in Apple's attributedBody archives.
                rows = conn.execute(
                    "SELECT ROWID,text,attributedBody,date FROM message WHERE is_from_me=0 "
                    "AND ((date>=? AND date<10000000000) OR date>=?) "
                    "AND (text LIKE ? OR instr(attributedBody,?)>0 OR text LIKE ? OR instr(attributedBody,?)>0) ORDER BY date,ROWID",
                    (min_seconds, min_seconds * 1_000_000_000, "%" + needle + "%", needle.encode("utf-8"),
                     "%" + wide_needle + "%", wide_needle.encode("utf-8")))
                candidates = 0
                for rowid, text, body, raw in rows:
                    candidates += 1
                    add(rowid, decode_message_text(text, body), message_received_at(raw), "messages_database")
                status.update(messages_database="read", database_candidates=candidates)
            finally:
                conn.close()
        except (OSError, sqlite3.Error, ValueError) as exc:
            status["messages_database"] = "unavailable"
            errors.append({"source": "messages_database", "type": type(exc).__name__,
                           "message": "历史短信只读查询失败；仅使用已保存短信，历史主体关联尚未完整核验"})
    messages = sorted(found.values(), key=lambda m: (m["received_at"], m["id"]))
    status["database_matches"] = sum("messages_database" in x["context_sources"] for x in messages)
    status["added_historical_messages"] = len(messages) - state_count
    status["total_matches"] = len(messages)
    return {"messages": messages, "source_status": status, "errors": errors}


def _notice_addressee(name: str, prefix: str) -> bool:
    """Match a name in the notification's address line immediately before body."""
    address = re.search(r"([^:。;]{1,200}):$", prefix)
    if not address:
        return False
    # The case-number/header boundary may precede the first addressee after OCR
    # joins lines. The separators also support several addressed defendants.
    return bool(re.search(r"(?:^|号|书|[#、,])" + re.escape(name) + r"(?=[、,:]|$)", address.group(1)))


def _notice_relationship_roles(name: str, source: str) -> set[str]:
    """Recognize express 'A sues you'/'you sue B' in court notices only.

    These are stated relationships, not guesses from the order of two names.
    The cited full page supplies the notice type and identifies who 'you' is.
    """
    roles = set()
    if "应诉通知书" in source:
        for match in re.finditer(r"本院(?:已经|已)?受理([^。;]{1,220}?)诉你", source):
            if name in match.group(1):
                roles.add("原告")
            if _notice_addressee(name, source[:match.start()]):
                roles.add("被告")
    if "受理通知书" in source:
        for match in re.finditer(r"你诉([^。;]{1,220}?)(?:一案|的?起诉状)", source):
            # Require the acceptance/receipt context, not a hypothetical quoted
            # sentence appearing in an unrelated part of the notice.
            continuation = source[match.start():match.end() + 100]
            if not re.search(r"(?:起诉状[^。;]{0,30}(?:已经|已)?收到|本院(?:已经|已)?受理)", continuation):
                continue
            if name in match.group(1):
                roles.add("被告")
            if _notice_addressee(name, source[:match.start()]):
                roles.add("原告")
    return roles


def party_claim_issue(party: dict, source_text: str) -> str | None:
    """Conservative validation in addition to source quote/name existence.

    It accepts an explicit role or the express sues-you/you-sue relationship in
    a court notification, and rejects demonstrated recipient/role mistakes.
    Pass the complete cited SMS or cited page, not just a shortened quotation.
    """
    role = normalized(party.get("role", ""))
    name = normalized(party.get("name", ""))
    quote = normalized(party.get("source_quote", ""))
    source = normalized(source_text)
    if re.search(r"推定|推测|猜测|表述顺序|顺序判断|依顺序", role):
        return "当事人角色不能按排列顺序或推测确定"
    if not name or name not in source or name not in quote:
        return "姓名缺少原文支持"
    # A greeting identifies the SMS recipient; without a separate party clause
    # it cannot establish even '当事人（角色未明）'. No hardcoded user name.
    if party.get("source_message_id"):
        for recipient in _greeting_names(source_text):
            if name in normalized(recipient):
                without_greeting = re.sub(re.escape(normalized(recipient)) + r"(?:你好|您好)[,，!！。]?", "", source)
                if name not in without_greeting:
                    return "短信称呼仅证明收件人，不能据此列为当事人"
    tokens = ROLE_RE.findall(role)
    if tokens:
        # Don't let 被告 satisfy 原告, 被申请方 satisfy 申请方, etc.
        source_roles = set(ROLE_RE.findall(quote))
        if not party.get("source_message_id"):
            source_roles.update(_notice_relationship_roles(name, source))
        if not all(token in source_roles for token in tokens):
            return "所引原文未明确支持该角色；申请方、被申请方不能擅自改写为原被告"
    return None
