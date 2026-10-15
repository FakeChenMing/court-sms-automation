#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import hashlib
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
VENDOR_DIR = SCRIPT_DIR / "vendor"
if VENDOR_DIR.is_dir():
    sys.path.insert(0, str(VENDOR_DIR))

CONFIG_PATH = Path(os.environ.get("COURT_SMS_CONFIG", str(SCRIPT_DIR / "config.json")))
CONFIG = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
CHAT_DB = Path(CONFIG.get("chat_db", str(HOME / "Library/Messages/chat.db"))).expanduser()
ROOT = Path(CONFIG.get("output_dir", str(HOME / "Documents/CourtSMSMaterials"))).expanduser()
STATE_DIR = Path(CONFIG.get("state_dir", str(HOME / "Library/Application Support/CourtSMSAutomation/state"))).expanduser()
STATE_PATH = STATE_DIR / "state.json"
BACKUP_PATH = STATE_DIR / "state.previous.json"
MIN_RECEIVED_AT = datetime.fromisoformat(CONFIG.get("start_date", datetime.now().date().isoformat()))
LOCK_PATH = STATE_DIR / ".monitor.lock"
LOG_PATH = STATE_DIR / "monitor.log"
OCR_CACHE_PATH = STATE_DIR / "ocr-cache.json"
HELPER_PATH = SCRIPT_DIR / "court_apps_helper.js"
ATTACHMENT_HELPER_PATH = SCRIPT_DIR / "CourtEventAttachment.app/Contents/MacOS/court_event_attachment"
PYTHON_BIN = Path(sys.executable)
MINERU_BIN = Path(CONFIG.get("mineru_bin") or "/nonexistent/mineru-local").expanduser()
CALENDAR_NAME = CONFIG.get("calendar_name", "")
os.environ["COURT_REMINDER_LIST"] = CONFIG.get("reminder_list", "法院材料")
OCR_CACHE_VERSION = "mineru-pipeline-v1"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) CourtSMSMonitor/1.0"

COURT_CASE_RE = re.compile(r"[（(]\s*(20\d{2})\s*[）)]\s*([\u4e00-\u9fffA-Za-z0-9\s]{3,30}?号)")
URL_RE = re.compile(r"https?://[^\s<>\"'，。；、（）【】]+")
SUMMONS_TIME_RE = re.compile(
    r"应到时间\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*"
    r"(上午|下午|中午|晚上|凌晨)?\s*(\d{1,2})\s*(?:[:：时])\s*(\d{1,2})\s*分?"
)


def setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("court_sms_monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


LOGGER = logging.getLogger("court_sms_monitor")


def report_body_value(body: str, label: str) -> str:
    prefix = label + "："
    for line in body.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def report_processed_at(body: str) -> datetime:
    value = report_body_value(body, "处理时间")
    match = re.fullmatch(
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s+"
        r"(\d{1,2}):(\d{1,2}):(\d{1,2})",
        value,
    )
    if not match:
        return datetime.now()
    try:
        return datetime(*map(int, match.groups()))
    except ValueError:
        return datetime.now()


def legacy_report_entry(item: dict) -> dict:
    body = str(item.get("body", ""))
    materials = []
    reading_materials = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "材料：":
            reading_materials = True
            continue
        if stripped.startswith("下载结果："):
            reading_materials = False
        if not reading_materials:
            continue
        match = re.match(r"\d+\.\s*(.+)", stripped)
        if not match or match.group(1) == "材料清单未取得":
            continue
        value = match.group(1)
        if "：" in value:
            name, status = value.rsplit("：", 1)
        else:
            name, status = value, "状态待确认"
        materials.append({"name": name, "status": status})
    return {
        "cause": report_body_value(body, "案由"),
        "materials": materials,
        "download_result": report_body_value(body, "下载结果") or "状态待确认",
        "calendar_result": report_body_value(body, "日程结果") or "状态待确认",
        "case_folder": report_body_value(body, "保存位置"),
    }


def preferred_case_cause(results: list[dict]) -> str:
    candidates = []
    for result in results:
        value = clean_case_cause(str(result.get("cause", "")))
        if (
            value
            and "待识别" not in value
            and re.search(
                r"纠纷$|争议$|执行异议$|执行异议之诉$|国家赔偿$|罪$|"
                r"保全$|审查$|强制清算$|破产$",
                value,
            )
        ):
            candidates.append(value)
    if not candidates:
        return ""
    return max(
        candidates,
        key=lambda value: (
            bool(re.search(r"纠纷$|争议$|执行异议$|国家赔偿$|罪$", value)),
            len(value),
        ),
    )


def build_sms_processing_report(
    processed_at: datetime,
    message_rowid: int | str,
    case_number: str | None,
    results: list[dict],
) -> tuple[str, str]:
    date_text = chinese_datetime(processed_at)
    case_text = case_number or "案号待识别"
    cause = preferred_case_cause(results)
    title = (
        f"法院短信处理结果｜{processed_at.year}年{processed_at.month}月"
        f"{processed_at.day}日｜{case_text}｜短信{message_rowid}"
    )

    material_statuses: dict[str, str] = {}
    for result in results:
        for material in result.get("materials", []):
            name = str(material.get("name") or "材料名称待识别")
            status = str(material.get("status") or "状态待确认")
            previous = material_statuses.get(name)
            if previous is None or (
                status.startswith("成功") and not previous.startswith("成功")
            ):
                material_statuses[name] = status

    download_success = 0
    download_failure = 0
    calendar_success = 0
    calendar_failure = 0
    calendar_no_action = 0
    attention = []
    detail_lines = []
    case_folders = []
    for index, result in enumerate(results, start=1):
        download_result = str(result.get("download_result") or "状态待确认")
        calendar_result = str(result.get("calendar_result") or "状态待确认")
        if download_result.startswith(("成功", "未重复下载")):
            download_success += 1
        elif download_result.startswith("部分成功"):
            download_success += 1
            download_failure += 1
            attention.append(f"事项{index}下载部分成功：{download_result}")
        elif download_result.startswith("失败"):
            download_failure += 1
            attention.append(f"事项{index}下载失败：{download_result}")

        if calendar_result.startswith("成功"):
            calendar_success += 1
        elif calendar_result.startswith("部分成功"):
            calendar_success += 1
            calendar_failure += 1
            attention.append(f"事项{index}日程部分成功：{calendar_result}")
        elif calendar_result.startswith("失败"):
            calendar_failure += 1
            attention.append(f"事项{index}日程失败：{calendar_result}")
        else:
            calendar_no_action += 1

        detail_lines.append(
            f"{index}. 下载：{download_result}\n   日程：{calendar_result}"
        )
        case_folder = str(result.get("case_folder") or "")
        if case_folder and case_folder not in case_folders:
            case_folders.append(case_folder)

    if download_failure and download_success:
        overall_download = (
            f"部分成功（{download_success}个事项成功或部分成功，"
            f"{download_failure}个事项存在失败）"
        )
    elif download_failure:
        overall_download = f"失败（{download_failure}个事项未成功）"
    elif download_success:
        overall_download = f"成功（{download_success}个事项成功或无需重复下载）"
    else:
        overall_download = "未执行或状态待确认"

    if calendar_failure and calendar_success:
        overall_calendar = (
            f"部分成功（{calendar_success}个事项成功，"
            f"{calendar_failure}个事项存在失败）"
        )
    elif calendar_failure:
        overall_calendar = f"失败（{calendar_failure}个事项添加失败）"
    elif calendar_success:
        overall_calendar = f"成功（{calendar_success}个事项已添加或更新）"
    elif calendar_no_action:
        overall_calendar = "未添加（本短信未产生需要新增的日程，详见处理明细）"
    else:
        overall_calendar = "未执行或状态待确认"

    lines = [
        f"处理时间：{date_text}",
        f"短信记录：{message_rowid}",
        f"案号：{case_text}",
        f"案由：{cause or '待识别（现有短信及材料未能可靠提取）'}",
        "材料：",
    ]
    if material_statuses:
        for index, (name, status) in enumerate(material_statuses.items(), start=1):
            lines.append(f"{index}. {name}：{status}")
    else:
        lines.append("1. 材料清单未取得")
    lines.extend([
        f"下载总体结果：{overall_download}",
        f"日程总体结果：{overall_calendar}",
        "处理明细：",
        *(detail_lines or ["1. 未发现可处理链接"]),
        "需要注意事项：",
    ])
    if not cause:
        attention.append("案由尚未可靠识别，请结合案件材料核对。")
    if attention:
        lines.extend(f"{index}. {value}" for index, value in enumerate(attention, start=1))
    else:
        lines.append("无。")
    if case_folders:
        lines.append("保存位置：")
        lines.extend(f"{index}. {value}" for index, value in enumerate(case_folders, start=1))
    return title, "\n".join(lines)


def collapse_pending_reports_by_sms(state: dict) -> None:
    reports = state.get("pending_reports", [])
    groups: dict[str, list[dict]] = {}
    passthrough = []
    order = []
    for item in reports:
        match = re.match(
            r"(?:delivery|unsupported|sms)-(\d+)(?:-|$)",
            str(item.get("report_id", "")),
        )
        if not match:
            passthrough.append(item)
            continue
        rowid = match.group(1)
        if rowid not in groups:
            groups[rowid] = []
            order.append(rowid)
        groups[rowid].append(item)

    collapsed = list(passthrough)
    for rowid in order:
        group = groups[rowid]
        if len(group) == 1 and group[0].get("report_id") == f"sms-{rowid}":
            collapsed.append(group[0])
            continue
        first = group[0]
        first_body = str(first.get("body", ""))
        processed_at = min(report_processed_at(str(item.get("body", ""))) for item in group)
        case_number = report_body_value(first_body, "案号") or None
        results = [legacy_report_entry(item) for item in group]
        title, body = build_sms_processing_report(
            processed_at, rowid, case_number, results
        )
        collapsed.append({
            "report_id": f"sms-{rowid}",
            "title": title,
            "body": body,
            "attempts": 0,
            "next_retry_at": datetime.now().isoformat(timespec="seconds"),
        })
    state["pending_reports"] = collapsed


def validate_state(state: dict) -> dict:
    if not isinstance(state, dict) or type(state.get("last_rowid")) is not int or state["last_rowid"] < 0:
        raise ValueError("Invalid durable scan cursor")
    for key in ("processed_rowids", "downloaded_urls", "pending_summons", "pending_reminders", "pending_reports", "manual_review"):
        state.setdefault(key, [])
        if not isinstance(state[key], list):
            raise ValueError("Invalid queue: " + key)
    state.setdefault("message_jobs", {})
    if not isinstance(state["message_jobs"], dict):
        raise ValueError("Invalid message ledger")
    return state


def load_state() -> dict:
    # Never turn an unreadable/missing state into a fresh installation.
    for attempt in range(3):
        try:
            return validate_state(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except OSError:
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))
                continue
        except (ValueError, TypeError):
            break
    try:
        recovered = validate_state(json.loads(BACKUP_PATH.read_text(encoding="utf-8")))
    except Exception:
        raise RuntimeError("状态及恢复副本均不可用；停止处理，禁止从零重扫") from None
    recovered["recovery_warning"] = datetime.now().isoformat(timespec="seconds")
    LOGGER.error("主状态读取失败，使用已验证恢复副本；需检查状态存储")
    return recovered


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_state(state: dict) -> None:
    validate_state(state)
    state["processed_rowids"] = state["processed_rowids"][-500:]
    state["last_updated"] = datetime.now().isoformat(timespec="seconds")
    # Rotate only a validated primary; never overwrite the good backup with corruption.
    try:
        previous = validate_state(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        previous = None
    if previous is not None:
        atomic_json(BACKUP_PATH, previous)
    atomic_json(STATE_PATH, state)


def decode_message_text(text: str | None, attributed_body: bytes | None) -> str:
    if text:
        return text
    if not attributed_body:
        return ""
    decoded = attributed_body.decode("utf-8", errors="ignore")

    # Messages stores rich-text bodies as an NSAttributedString typed stream.
    # The first NSString payload is the visible SMS; later payloads repeat links
    # and data-detector metadata.  Returning the entire decoded archive makes a
    # single court URL look like several different URLs.
    marker = decoded.find("NSString")
    if marker >= 0:
        payload = decoded[marker + len("NSString"):]
        start = payload.find("【")
        if start < 0:
            first_cjk = re.search(r"[\u3400-\u9fff]", payload)
            start = first_cjk.start() if first_cjk else -1
        if start >= 0:
            candidate = re.split(
                r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
                payload[start:],
                maxsplit=1,
            )[0].strip()
            if candidate:
                return html.unescape(candidate)

    decoded = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", decoded)
    return decoded


def fetch_new_messages(last_rowid: int) -> list[dict]:
    if not CHAT_DB.exists():
        raise FileNotFoundError(f"找不到信息数据库：{CHAT_DB}")
    uri = f"file:{CHAT_DB}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        rows = connection.execute(
            """
            SELECT ROWID, text, attributedBody, date
            FROM message
            WHERE ROWID > ? AND is_from_me = 0 AND date >= ?
            ORDER BY ROWID ASC
            """,
            (last_rowid, int((MIN_RECEIVED_AT.timestamp() - 978307200) * 1_000_000_000)),
        ).fetchall()
    finally:
        connection.close()
    return [
        {"rowid": rowid, "text": decode_message_text(text, body), "date": date}
        for rowid, text, body, date in rows
    ]


def extract_urls(text: str) -> list[str]:
    urls = []
    for match in URL_RE.findall(text):
        url = match.rstrip("，。；;、）)]}>。")
        try:
            parsed = urlparse(url)
            if not parsed.hostname:
                continue
        except ValueError:
            continue
        if url not in urls:
            urls.append(url)
    return urls


def normalize_case_number(text: str) -> str | None:
    match = COURT_CASE_RE.search(text)
    if not match:
        return None
    suffix = re.sub(r"\s+", "", match.group(2))
    return f"（{match.group(1)}）{suffix}"


def is_court_message(text: str, urls: list[str]) -> bool:
    lowered = text.lower()
    trusted_url = any(
        urlparse(url).hostname and (
            (urlparse(url).hostname == "court.gov.cn" or urlparse(url).hostname.endswith(".court.gov.cn"))
            or "rmfy" in urlparse(url).hostname.lower()
        )
        for url in urls
    )
    return trusted_url or ("法院" in text and bool(urls)) or "人民法院" in text or "送达文书" in text


def sanitize_filename(name: str) -> str:
    name = name.replace("/", "／").replace(":", "：").replace("\x00", "").strip()
    return name[:220] or "未命名法院文件"


def http_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Origin": "https://zxfw.court.gov.cn",
            "Referer": "https://zxfw.court.gov.cn/zxfw/",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urlopen(request, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def download_file(url: str, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + ".part")
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=70) as response, temp_path.open("wb") as output:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                output.write(chunk)
        if temp_path.stat().st_size == 0:
            raise ValueError("下载结果为空")
        content = temp_path.read_bytes()
        if destination.suffix.lower() == ".pdf" and not content.lstrip().startswith(b"%PDF-"):
            raise ValueError("下载结果不是有效PDF文件头，禁止标记成功")
        if destination.exists():
            old = destination.read_bytes()
            if old == content:
                return False
            # Preserve the previous version even if the same delivery later changes.
            archive = destination.parent / ".versions" / hashlib.sha256(old).hexdigest()[:20] / destination.name
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                shutil.copy2(destination, archive)
        os.replace(temp_path, destination)
        return True
    finally:
        if temp_path.exists():
            temp_path.unlink()


def parse_zxfw_params(url: str) -> dict | None:
    parsed = urlparse(url)
    if not parsed.hostname or not (parsed.hostname == "court.gov.cn" or parsed.hostname.endswith(".court.gov.cn")):
        return None
    if "pagesAjkj/app/wssd/index" not in parsed.fragment:
        return None
    query = parsed.fragment.split("?", 1)[1] if "?" in parsed.fragment else ""
    values = parse_qs(query)
    required = ("qdbh", "sdbh")
    if not all(values.get(key) for key in required):
        return None
    return {key: values.get(key, [""])[0] for key in ("qdbh", "sdbh", "sdsin", "mm")}


def select_delivery_urls(urls: list[str]) -> list[str]:
    """Prefer unique actionable delivery links over informational site links."""
    actionable: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    for url in urls:
        params = parse_zxfw_params(url)
        if not params:
            continue
        key = tuple(params.get(name, "") for name in ("qdbh", "sdbh", "sdsin", "mm"))
        if key in seen:
            continue
        seen.add(key)
        actionable.append(url)
    return actionable or urls


def message_received_at(raw_date: int | float | None) -> datetime:
    if not raw_date:
        return datetime.now()
    seconds = float(raw_date)
    if abs(seconds) > 10_000_000_000:
        seconds /= 1_000_000_000
    return (datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).astimezone().replace(tzinfo=None)


def weekly_folder(received_at: datetime) -> Path:
    monday = (received_at - timedelta(days=received_at.weekday())).date()
    sunday = monday + timedelta(days=6)
    name = (
        f"{monday.year}年{monday.month}月{monday.day}日-"
        f"{sunday.year}年{sunday.month}月{sunday.day}日"
    )
    return ROOT / name


def handle_zxfw_delivery(
    url: str,
    case_number: str | None,
    received_at: datetime,
    outcome: dict | None = None,
) -> tuple[list[Path], list[Path], str | None]:
    if outcome is not None:
        outcome.clear()
        outcome["materials"] = []
    params = parse_zxfw_params(url)
    if not params:
        return [], [], None
    response = http_json(
        "https://zxfw.court.gov.cn/yzw/yzw-zxfw-sdfw/api/v1/sdfw/getWsListBySdbhNew",
        params,
    )
    if response.get("code") != 200 or not isinstance(response.get("data"), list):
        raise RuntimeError(f"法院接口未返回材料清单：{response.get('msg', '未知错误')}")
    folder_name = case_number or datetime.now().strftime("%Y年%m月%d日")
    delivery_key = hashlib.sha256((params["qdbh"] + "|" + params["sdbh"]).encode()).hexdigest()[:20]
    case_folder = weekly_folder(received_at) / folder_name / ("送达-" + delivery_key)
    manifest = {"delivery_key": delivery_key, "verified_at": datetime.now().isoformat(timespec="seconds"), "documents": []}
    if outcome is not None:
        outcome["folder_name"] = folder_name
        outcome["case_folder"] = str(case_folder)
    new_files: list[Path] = []
    available_files: list[Path] = []
    prepared_items = []
    for index, item in enumerate(response["data"]):
        base_name = sanitize_filename(str(item.get("c_wsmc") or "法院材料"))
        extension = str(item.get("c_wjgs") or "").lower().lstrip(".")
        if extension and not base_name.lower().endswith("." + extension):
            base_name += "." + extension
        doc_identity = str(item.get("c_wsbh") or item.get("c_stbh") or (str(index) + "|" + base_name))
        doc_key = hashlib.sha256(doc_identity.encode()).hexdigest()[:20]
        destination = case_folder / ("文书-" + doc_key) / base_name
        material = {
            "name": base_name,
            "path": str(destination),
            "status": "未处理",
            "document_key": doc_key,
        }
        if outcome is not None:
            outcome["materials"].append(material)
        prepared_items.append((item.get("wjlj"), destination, material))

    if len({str(entry[1]) for entry in prepared_items}) != len(prepared_items):
        raise ValueError("送达清单文书标识重复，需人工核对")
    for file_url, destination, material in prepared_items:
        if not file_url:
            material["status"] = "失败（法院接口未提供下载地址）"
            continue
        try:
            downloaded = download_file(file_url, destination)
        except Exception as exc:
            material["status"] = f"失败（{compact_error(exc)}）"
            for _, _, pending_material in prepared_items:
                if pending_material["status"] == "未处理":
                    pending_material["status"] = "未处理（下载流程中断）"
            raise
        if downloaded:
            new_files.append(destination)
            material["status"] = "成功（本次新下载）"
        elif destination.exists() and destination.stat().st_size > 0:
            material["status"] = "成功（文件已存在，未重复下载）"
        if destination.exists() and destination.stat().st_size > 0:
            available_files.append(destination)
            manifest["documents"].append({"document_key": material["document_key"], "name": destination.name, "path": str(destination), "size": destination.stat().st_size, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
            atomic_json(case_folder / ".delivery_manifest.json", manifest)
    if outcome is not None:
        outcome["new_files"] = [str(path) for path in new_files]
        outcome["available_files"] = [str(path) for path in available_files]
    return new_files, available_files, folder_name


def add_reminders(case_number: str, files: list[Path]) -> None:
    if not files:
        return
    command = ["/usr/bin/osascript", "-l", "JavaScript", str(HELPER_PATH), "reminders", case_number]
    command.extend(str(path) for path in files)
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise RuntimeError(f"提醒事项写入失败：{result.stderr.strip()}")


def add_manual_reminder(title: str, body: str) -> None:
    result = subprocess.run(
        ["/usr/bin/osascript", "-l", "JavaScript", str(HELPER_PATH), "manual", title, body],
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode != 0:
        LOGGER.error("人工处理提醒写入失败：%s", result.stderr.strip())


def add_processing_report_reminder(report_id: str, title: str, body: str) -> None:
    result = subprocess.run(
        [
            "/usr/bin/osascript", "-l", "JavaScript", str(HELPER_PATH),
            "report", report_id, title, body,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=12,
    )
    if result.returncode != 0:
        raise RuntimeError(f"处理结果提醒写入失败：{result.stderr.strip()}")


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except Exception:
        LOGGER.exception("PDF 文本提取失败：%s", path)
        return ""


def compact_pdf_text(text: str) -> str:
    """Remove layout whitespace inserted between glyphs in court PDFs."""
    return re.sub(r"\s+", "", text)


def compact_error(error: Exception | str) -> str:
    value = re.sub(r"https?://\S+", "[URL omitted]", str(error))
    value = re.sub(r"\s+", " ", value).strip()
    return value[:500] or "未知错误"


def clean_case_cause(value: str) -> str:
    value = re.sub(r"\s+", "", value or "").strip("：:，,；;。")
    value = re.split(
        r"被传唤人|传唤事由|被传事由|应到时间|原告|被告|上诉人|被上诉人|"
        r"申请人|被申请人|审判员|书记员|注意事项",
        value,
        maxsplit=1,
    )[0]
    if not 2 <= len(value) <= 50:
        return ""
    if any(fragment in value for fragment in ("人民法院", "传票", "判决书", "裁定书", "通知书")):
        return ""
    return value


def extract_case_cause(text: str, case_number: str | None = None) -> str:
    compact_text = compact_pdf_text(text)
    explicit = re.search(
        r"案由[:：]?(.{2,50}?)(?=被传唤人|传唤事由|被传事由|应到时间|"
        r"原告|被告|上诉人|被上诉人|申请人|被申请人|审判员|书记员|注意事项)",
        compact_text,
    )
    if explicit:
        cause = clean_case_cause(explicit.group(1))
        if cause:
            return cause

    labelled = re.search(
        r"案由[:：]?([\u4e00-\u9fff、]{2,40}?(?:纠纷|争议|执行异议|国家赔偿|罪))",
        compact_text,
    )
    if labelled:
        cause = clean_case_cause(labelled.group(1))
        if cause:
            return cause

    if case_number:
        compact_case_number = compact_pdf_text(case_number)
        adjacent = re.search(
            re.escape(compact_case_number)
            + r"([\u4e00-\u9fff、]{2,40}?(?:纠纷|争议|执行异议|国家赔偿|罪))"
              r"(?=被传唤人|传唤事由|被传事由|应到时间|原告|被告|上诉人|申请人)",
            compact_text,
        )
        if adjacent:
            cause = clean_case_cause(adjacent.group(1))
            if cause:
                return cause

    generic = re.search(
        r"([\u4e00-\u9fff、]{2,35}?(?:合同纠纷|责任纠纷|权纠纷|劳动争议|"
        r"执行异议|国家赔偿|行政争议|纠纷))一案",
        compact_text,
    )
    return clean_case_cause(generic.group(1)) if generic else ""


def extract_pdf_preview(path: Path, max_pages: int = 2) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:max_pages])
    except Exception:
        LOGGER.exception("PDF 案由提取失败：%s", path)
        return ""


def identify_case_cause(case_number: str | None, files: list[Path]) -> str:
    preferred_fragments = ("传票", "受理", "起诉状", "判决", "裁定", "调解")
    candidates = sorted(
        (path for path in files if path.suffix.lower() == ".pdf" and path.exists()),
        key=lambda path: (
            0 if any(fragment in path.name for fragment in preferred_fragments) else 1,
            path.name,
        ),
    )
    for path in candidates[:8]:
        cause = extract_case_cause(extract_pdf_preview(path), case_number)
        if cause:
            return cause
    return ""


def extract_compact_field(text: str, label: str, end_labels: tuple[str, ...]) -> str:
    end_pattern = "|".join(re.escape(item) for item in end_labels)
    match = re.search(re.escape(label) + rf"(.*?)(?={end_pattern})", text)
    return match.group(1).strip() if match else ""


def clean_party_name(value: str) -> str:
    value = re.sub(r"\s+", "", value or "")
    value = re.sub(r"^(?:被传唤人)?姓名[:：]?", "", value)
    value = re.sub(r"^[（(](?:单位|个人|自然人)[）)]", "", value)
    value = re.sub(
        r"^(?:原审原告|原审被告|原告|被告|上诉人|被上诉人|"
        r"申请人|被申请人|第三人)+",
        "",
        value,
    )
    value = re.split(
        r"送达地址|送达确认地址|地址确认书地址|联系地址|"
        r"(?:工作)?单位或(?:住址|地址)|单位或(?:住址|地址)|工作单位|"
        r"住所|住址|地址|被传事由|传唤事由|应到时间",
        value,
        maxsplit=1,
    )[0]
    value = re.sub(r"(?:工作)?单位或$", "", value)
    return re.sub(r"送达$", "", value).strip("：:，,；;")


def party_name_key(value: str) -> str:
    value = clean_party_name(value)
    value = re.sub(r"[\s（）()【】\[\]·•，,。._\-—]", "", value)
    return re.sub(
        r"(?:股份有限公司|有限责任公司|有限公司|集团公司|公司)$",
        "",
        value,
    )


def compatible_party_names(parsed_party: str, filename_party: str) -> bool:
    parsed_key = party_name_key(parsed_party)
    filename_key = party_name_key(filename_party)
    if not parsed_key or not filename_key:
        return False
    if parsed_key == filename_key:
        return True
    shorter, longer = sorted((parsed_key, filename_key), key=len)
    return len(shorter) >= 4 and longer.startswith(shorter)


def party_from_filename(path: Path) -> str:
    stem = path.stem
    for left, right in (("（", "）"), ("(", ")")):
        start = stem.find(left)
        end = stem.rfind(right)
        if start >= 0 and end > start:
            suffix = stem[end + 1:].strip()
            suffix = re.sub(r"^[\s_\-—]+", "", suffix)
            suffix = re.sub(r"[\s_\-—]*(?:带章|盖章|签章|扫描件)$", "", suffix).strip()
            suffix_candidate = clean_party_name(suffix)
            if suffix_candidate:
                return suffix_candidate
            candidate = clean_party_name(stem[start + 1:end])
            if candidate and not re.fullmatch(r"20\d{2}.*号", candidate):
                return candidate
    return ""


def suspicious_party_name(value: str) -> bool:
    if not value or len(value) > 60:
        return True
    bad_fragments = (
        "姓名", "单位或", "工作单位", "住址", "住所", "被传唤人",
        "送达地址", "被传事由", "传唤事由", "应到时间", "或开庭",
    )
    return value.endswith("送达") or any(fragment in value for fragment in bad_fragments)


def suspicious_location(value: str) -> bool:
    return not value or any(
        fragment in value
        for fragment in ("适用程序", "注意事项", "被传事由", "应到时间")
    )


def load_ocr_cache() -> dict:
    try:
        data = json.loads(OCR_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_ocr_cache(cache: dict) -> None:
    cache = dict(list(cache.items())[-500:])
    OCR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".court_ocr_cache.", suffix=".tmp", dir=ROOT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2)
        os.replace(temp_name, OCR_CACHE_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def ocr_cache_key(path: Path) -> str:
    stat = path.stat()
    raw = f"{OCR_CACHE_VERSION}|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_mineru_markdown(markdown: str) -> dict:
    fields: dict[str, str] = {}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", markdown, flags=re.I | re.S)
    for row in rows:
        cells = [
            re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.I | re.S)
        ]
        if not cells:
            continue
        for index, cell in enumerate(cells[:-1]):
            if "被传唤人" in cell and "姓名" in cell:
                fields["party"] = clean_party_name(cells[index + 1])
            elif cell in ("应到时间", "到庭时间", "开庭时间"):
                fields["time"] = cells[index + 1]
            elif cell in ("应到处所", "应到场所", "到庭地点", "开庭地点", "法庭"):
                fields["location"] = cells[index + 1]
        first_cell = cells[0]
        if "号" in first_cell and "cause" not in fields:
            possible_cause = first_cell.rsplit("号", 1)[-1]
            if 1 < len(possible_cause) <= 40:
                fields["cause"] = possible_cause
    return fields


def extract_mineru_fields(path: Path) -> dict:
    if not MINERU_BIN.is_file():
        LOGGER.error("MinerU OCR 不可用：%s", MINERU_BIN)
        return {}
    key = ocr_cache_key(path)
    cache = load_ocr_cache()
    cached = cache.get(key)
    if isinstance(cached, dict):
        return cached
    try:
        with tempfile.TemporaryDirectory(prefix="court-summons-ocr-") as temp_dir:
            temp_root = Path(temp_dir)
            temp_pdf = temp_root / path.name
            output_dir = temp_root / "output"
            shutil.copy2(path, temp_pdf)
            result = subprocess.run(
                [str(MINERU_BIN), "-p", str(temp_pdf), "-o", str(output_dir), "-b", "pipeline"],
                check=False,
                capture_output=True,
                text=True,
                timeout=240,
            )
            if result.returncode != 0:
                LOGGER.error("MinerU OCR 失败 path=%s stderr=%s", path, result.stderr[-2000:])
                return {}
            markdown_files = sorted(output_dir.rglob("*.md"))
            if not markdown_files:
                LOGGER.error("MinerU OCR 未生成 Markdown：%s", path)
                return {}
            fields = parse_mineru_markdown(markdown_files[0].read_text(encoding="utf-8"))
    except Exception:
        LOGGER.exception("MinerU OCR 异常：%s", path)
        return {}
    if fields:
        cache[key] = fields
        save_ocr_cache(cache)
        LOGGER.info("MinerU OCR 字段修复成功 path=%s fields=%s", path, sorted(fields))
    return fields


def parse_summons_datetime(text: str) -> datetime | None:
    match = SUMMONS_TIME_RE.search(compact_pdf_text(text))
    if not match:
        return None
    try:
        year, month, day, period, hour, minute = match.groups()
        hour_value = int(hour)
        if period in ("下午", "中午", "晚上") and hour_value < 12:
            hour_value += 12
        elif period == "凌晨" and hour_value == 12:
            hour_value = 0
        return datetime(int(year), int(month), int(day), hour_value, int(minute))
    except ValueError:
        return None


def parse_summons(path: Path, case_number: str) -> dict | None:
    text = extract_pdf_text(path)
    compact_text = compact_pdf_text(text)
    start = parse_summons_datetime(compact_text)
    party = clean_party_name(extract_compact_field(
        compact_text,
        "被传唤人",
        (
            "工作单位或住址", "单位或住址", "工作单位或地址", "单位或地址",
            "送达地址", "送达确认地址", "地址确认书地址", "联系地址",
            "工作单位", "住所", "住址", "被传事由", "传唤事由", "应到时间",
        ),
    ))
    location = ""
    location_text = compact_text.split("注意事项", 1)[0] + "注意事项"
    location_end_labels = (
        "适用程序", "注意事项", "主审法官", "审判员", "书记员",
        "联系电话", "法院地址", "本院地址",
    )
    for location_label in ("应到处所", "应到场所", "到庭地点", "开庭地点"):
        location = extract_compact_field(
            location_text,
            location_label,
            location_end_labels,
        )
        if location:
            break
    location = location.replace("杭州州市", "杭州市").replace("（法庭地址：", "（")
    cause = extract_compact_field(
        compact_text, "案由", ("被传唤人", "住所", "住址", "传唤事由")
    )
    if not cause:
        compact_case_number = compact_pdf_text(case_number)
        cause_match = re.search(
            re.escape(compact_case_number) + r"(.{2,40}?)(?=被传唤人)", compact_text
        )
        cause = cause_match.group(1).strip("：:") if cause_match else ""

    filename_party = party_from_filename(path)
    needs_ocr = (
        start is None
        or suspicious_party_name(party)
        or not cause
        or suspicious_location(location)
    )
    if needs_ocr:
        ocr_fields = extract_mineru_fields(path)
        ocr_party = clean_party_name(ocr_fields.get("party", ""))
        if ocr_party and not suspicious_party_name(ocr_party):
            party = ocr_party
        if ocr_fields.get("cause"):
            cause = ocr_fields["cause"]
        if ocr_fields.get("location"):
            location = ocr_fields["location"]
        if start is None and ocr_fields.get("time"):
            start = parse_summons_datetime("应到时间" + ocr_fields["time"])

    if filename_party and not suspicious_party_name(filename_party):
        if suspicious_party_name(party):
            party = filename_party
        elif not compatible_party_names(party, filename_party):
            LOGGER.error(
                "传票姓名与文件名冲突，停止自动创建日程 path=%s parsed=%s filename=%s",
                path, party, filename_party,
            )
            return None

    party = clean_party_name(party)
    cause = clean_case_cause(cause)
    if start is None or suspicious_party_name(party) or suspicious_location(location):
        LOGGER.error(
            "传票关键字段校验失败，停止创建日程 path=%s party=%s start=%s location=%s",
            path, party, start, location,
        )
        return None

    end = start + timedelta(hours=1)
    court_match = re.search(r"([\u4e00-\u9fff]{2,40}人民法院)", compact_text)
    court = court_match.group(1) if court_match else ""
    return {
        "title": f"{party}开庭",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "location": location,
        "description": f"案号：{case_number}\n案由：{cause}\n法院：{court}",
        "cause": cause,
        "start_timestamp": str(start.timestamp()),
        "end_timestamp": str(end.timestamp()),
    }


def add_calendar_from_summons(
    case_number: str,
    files: list[Path],
    outcome: dict | None = None,
    emit_manual_reminders: bool = True,
) -> None:
    if outcome is not None:
        outcome.clear()
        outcome.update({
            "summons_count": 0,
            "successful_count": 0,
            "failed_files": [],
            "causes": [],
        })
    if not CONFIG.get("calendar_enabled", False):
        return
    parse_failures: list[Path] = []
    for path in files:
        if "传票" not in path.name:
            continue
        if outcome is not None:
            outcome["summons_count"] += 1
        event = parse_summons(path, case_number)
        if not event:
            if emit_manual_reminders:
                add_manual_reminder("法院传票待核对 - " + path.name, f"{case_number}\n{path}")
            parse_failures.append(path)
            if outcome is not None:
                outcome["failed_files"].append(path.name)
            continue
        if outcome is not None and event.get("cause"):
            outcome["causes"].append(event["cause"])
        calendar_result = subprocess.run(
            [
                str(ATTACHMENT_HELPER_PATH), "upsert", CALENDAR_NAME,
                event["title"], event["start_timestamp"], event["end_timestamp"],
                event["location"], event["description"], str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if calendar_result.returncode != 0:
            if outcome is not None:
                outcome["failed_files"].append(path.name)
            raise RuntimeError(f"日历及传票附件写入失败：{calendar_result.stderr.strip()}")
        if outcome is not None:
            outcome["successful_count"] += 1
    if parse_failures:
        raise RuntimeError("传票关键字段识别失败：" + "、".join(path.name for path in parse_failures))


def chinese_datetime(value: datetime) -> str:
    return (
        f"{value.year}年{value.month}月{value.day}日 "
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    )


def processing_report_id(kind: str, rowid: int | str, source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{rowid}-{digest}"


def summarize_download_result(
    materials: list[dict], new_files: list[Path], available_files: list[Path]
) -> str:
    successful = [item for item in materials if item.get("status", "").startswith("成功")]
    unsuccessful = [item for item in materials if not item.get("status", "").startswith("成功")]
    if not materials:
        return "失败（未取得可下载材料清单）"
    if unsuccessful and successful:
        return (
            f"部分成功（共{len(materials)}份，成功{len(successful)}份，"
            f"未成功{len(unsuccessful)}份；本次新下载{len(new_files)}份）"
        )
    if unsuccessful:
        return f"失败（共{len(materials)}份，均未下载成功）"
    existing_count = max(0, len(available_files) - len(new_files))
    if new_files:
        return (
            f"成功（共{len(materials)}份，本次新下载{len(new_files)}份，"
            f"已有文件{existing_count}份）"
        )
    return f"成功（{len(available_files)}份材料均已存在，未重复下载）"


def build_processing_report(
    processed_at: datetime,
    case_number: str | None,
    cause: str,
    materials: list[dict],
    download_result: str,
    calendar_result: str,
    case_folder: str = "",
    title_prefix: str = "法院材料处理结果",
) -> tuple[str, str]:
    date_text = chinese_datetime(processed_at)
    case_text = case_number or "案号待识别"
    title = f"{title_prefix}｜{date_text}｜{case_text}"
    lines = [
        f"处理时间：{date_text}",
        f"案号：{case_text}",
        f"案由：{cause or '待识别（现有短信及材料未能可靠提取）'}",
        "材料：",
    ]
    if materials:
        for index, item in enumerate(materials, start=1):
            lines.append(
                f"{index}. {item.get('name') or '材料名称待识别'}："
                f"{item.get('status') or '状态待确认'}"
            )
    else:
        lines.append("1. 材料清单未取得")
    lines.extend([
        f"下载结果：{download_result}",
        f"日程结果：{calendar_result}",
    ])
    if case_folder:
        lines.append(f"保存位置：{case_folder}")
    return title, "\n".join(lines)


def queue_processing_report(
    state: dict,
    report_id: str,
    processed_at: datetime,
    case_number: str | None,
    cause: str,
    materials: list[dict],
    download_result: str,
    calendar_result: str,
    case_folder: str = "",
    title_prefix: str = "法院材料处理结果",
) -> None:
    title, body = build_processing_report(
        processed_at,
        case_number,
        cause,
        materials,
        download_result,
        calendar_result,
        case_folder,
        title_prefix,
    )
    pending = state.setdefault("pending_reports", [])
    for item in pending:
        if item.get("report_id") == report_id:
            item.update({"title": title, "body": body})
            return
    pending.append({
        "report_id": report_id,
        "title": title,
        "body": body,
        "attempts": 0,
        "next_retry_at": datetime.now().isoformat(timespec="seconds"),
    })


def queue_sms_processing_report(
    state: dict,
    message_rowid: int | str,
    processed_at: datetime,
    case_number: str | None,
    results: list[dict],
) -> None:
    report_id = f"sms-{message_rowid}"
    title, body = build_sms_processing_report(
        processed_at, message_rowid, case_number, results
    )
    pending = state.setdefault("pending_reports", [])
    for item in pending:
        if item.get("report_id") == report_id:
            item.update({"title": title, "body": body})
            return
    pending.append({
        "report_id": report_id,
        "title": title,
        "body": body,
        "attempts": 0,
        "next_retry_at": datetime.now().isoformat(timespec="seconds"),
    })


def retry_pending_reports(state: dict) -> None:
    now = datetime.now()
    pending = state.setdefault("pending_reports", [])
    pending.sort(key=lambda item: int(re.search(r"sms-(\d+)", item.get("report_id", "")).group(1)) if re.search(r"sms-(\d+)", item.get("report_id", "")) else 0, reverse=True)
    due = [item for item in pending if item.get("next_retry_at", "") <= now.isoformat(timespec="seconds")][:100]
    if not due:
        return
    fd, name = tempfile.mkstemp(prefix="reports-", suffix=".json", dir=STATE_DIR)
    receipt_path = Path(name + ".receipt")
    confirmed = set()
    failure = None
    receipts = {}
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(due, handle, ensure_ascii=False)
        helper = SCRIPT_DIR / "CourtReminderAgent.app/Contents/MacOS/reminder_bridge"
        result = subprocess.run([str(helper), name, str(receipt_path)], capture_output=True, text=True, timeout=90)
        if result.returncode:
            failure = "ReminderBridgeExit" + str(result.returncode)
    except Exception as exc:
        failure = type(exc).__name__
    try:
        if receipt_path.exists():
            outcome = json.loads(receipt_path.read_text())
            confirmed = set(outcome.get("confirmed", []))
            receipts = outcome.get("receipts", {})
            if not confirmed.issubset({i["report_id"] for i in due}) or not all(receipts.get(k) for k in confirmed):
                confirmed = set()
                failure = "InvalidReminderReceipt"
        if len(confirmed) < len(due) and not failure:
            failure = "IncompleteReminderReceipt"
        state["pending_reports"] = [i for i in pending if i["report_id"] not in confirmed]
        if confirmed:
            state["last_report_success_at"] = datetime.now().isoformat(timespec="seconds")
            state["last_report_batch_count"] = len(confirmed)
            for key in confirmed:
                state.setdefault("report_receipts", {})[key] = {"at": state["last_report_success_at"], "reminder_id": receipts[key]}
            LOGGER.info("提醒批量核验成功 count=%s remaining=%s", len(confirmed), len(state["pending_reports"]))
        if failure:
            for item in due:
                if item["report_id"] in confirmed:
                    continue
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["next_retry_at"] = (now + timedelta(minutes=min(60, 2 ** min(item["attempts"], 6)))).isoformat(timespec="seconds")
            state["last_report_error"] = {"at": now.isoformat(timespec="seconds"), "type": failure}
            LOGGER.error("提醒批量发送未全部完成 type=%s；未完成任务保留", failure)
    finally:
        Path(name).unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)


def queue_pending_summons(state: dict, case_number: str, files: list[Path]) -> None:
    pending = state.setdefault("pending_summons", [])
    existing = {(item.get("case_number"), item.get("path")) for item in pending}
    for path in files:
        if "传票" not in path.name:
            continue
        key = (case_number, str(path))
        if key in existing:
            continue
        pending.append({
            "case_number": case_number,
            "path": str(path),
            "attempts": 0,
            "next_retry_at": (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds"),
        })
        existing.add(key)


def register_manual_task(state: dict, item: dict, reason: str) -> None:
    key = "calendar-review-" + hashlib.sha256(str(item.get("path", "")).encode()).hexdigest()[:20]
    record = dict(item, reason=reason, review_id=key)
    if not any(i.get("review_id") == key for i in state.setdefault("manual_review", [])):
        state["manual_review"].append(record)
    queue_processing_report(state, key, datetime.now(), item.get("case_number"), "", [],
        "请核对已保存材料", reason + "；文件：" + str(item.get("path", "")),
        title_prefix="法院传票待人工核对")


def retry_pending_summons(state: dict) -> None:
    now = datetime.now()
    remaining = []
    attempted = 0
    for item in state.setdefault("pending_summons", []):
        if attempted >= 2:
            remaining.append(item)
            continue
        path = Path(item.get("path", ""))
        try:
            next_retry_at = datetime.fromisoformat(item.get("next_retry_at", ""))
        except ValueError:
            next_retry_at = now
        if next_retry_at > now:
            remaining.append(item)
            continue
        if not path.exists():
            register_manual_task(state, item, "待重试文件不存在")
            continue
        case_number = item.get("case_number", path.parent.name)
        calendar_outcome: dict = {}
        attempted += 1
        try:
            add_calendar_from_summons(
                case_number,
                [path],
                calendar_outcome,
                emit_manual_reminders=False,
            )
        except Exception:
            attempts = int(item.get("attempts", 0)) + 1
            delay_minutes = min(60, 5 * (2 ** min(attempts, 4)))
            item["attempts"] = attempts
            item["next_retry_at"] = (
                now + timedelta(minutes=delay_minutes)
            ).isoformat(timespec="seconds")
            if attempts >= 6:
                register_manual_task(state, item, "日历重试达到上限，需人工核对")
            else:
                remaining.append(item)
            LOGGER.exception("传票日历自动重试失败 case=%s path=%s", item.get("case_number"), path)
            continue
        LOGGER.info("传票日历自动重试成功 case=%s path=%s", case_number, path)
    state["pending_summons"] = remaining


def queue_pending_reminders(state: dict, case_number: str, files: list[Path]) -> None:
    pending = state.setdefault("pending_reminders", [])
    key = (case_number, tuple(str(path) for path in files))
    existing = {
        (item.get("case_number"), tuple(item.get("paths", [])))
        for item in pending
    }
    if files and key not in existing:
        pending.append({
            "case_number": case_number,
            "paths": [str(path) for path in files],
            "attempts": 0,
            "next_retry_at": (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds"),
        })


def retry_pending_reminders(state: dict) -> None:
    now = datetime.now()
    remaining = []
    for item in state.setdefault("pending_reminders", []):
        try:
            next_retry_at = datetime.fromisoformat(item.get("next_retry_at", ""))
        except ValueError:
            next_retry_at = now
        if next_retry_at > now:
            remaining.append(item)
            continue
        files = [Path(path) for path in item.get("paths", []) if Path(path).exists()]
        if not files:
            LOGGER.error("待重试提醒的文件均不存在 case=%s", item.get("case_number"))
            continue
        try:
            add_reminders(item.get("case_number", files[0].parent.name), files)
            LOGGER.info("提醒事项自动重试成功 case=%s", item.get("case_number"))
        except Exception:
            attempts = int(item.get("attempts", 0)) + 1
            delay_minutes = min(60, 5 * (2 ** min(attempts, 4)))
            item["attempts"] = attempts
            item["next_retry_at"] = (
                now + timedelta(minutes=delay_minutes)
            ).isoformat(timespec="seconds")
            remaining.append(item)
            LOGGER.exception("提醒事项自动重试失败 case=%s", item.get("case_number"))
    state["pending_reminders"] = remaining


def process_message(message: dict, state: dict) -> None:
    text = message["text"]
    all_urls = extract_urls(text)
    if not is_court_message(text, all_urls):
        return
    urls = select_delivery_urls(all_urls)
    case_number = normalize_case_number(text)
    report_case_number = case_number
    received_at = message_received_at(message.get("date"))
    processed_at = datetime.now()
    report_results: list[dict] = []
    download_failed = False

    if not urls:
        state.setdefault("manual_messages", {})[str(message["rowid"])] = "短信没有可自动下载链接，需人工阅读"
        report_results.append({
            "cause": extract_case_cause(text, case_number),
            "materials": [],
            "download_result": "失败（短信中未发现可处理链接）",
            "calendar_result": "未添加（未取得可用于创建日程的传票）",
            "case_folder": "",
        })

    for url in urls:
        delivery_outcome: dict = {}
        try:
            new_files, available_files, folder_name = handle_zxfw_delivery(
                url, case_number, received_at, delivery_outcome
            )
        except Exception as exc:
            download_failed = True
            LOGGER.error("法院材料处理失败 rowid=%s type=%s", message["rowid"], type(exc).__name__)
            materials = delivery_outcome.get("materials", [])
            partial_new_files = [
                Path(item["path"])
                for item in materials
                if item.get("status") == "成功（本次新下载）" and item.get("path")
            ]
            partial_available_files = [
                Path(item["path"])
                for item in materials
                if item.get("status", "").startswith("成功") and item.get("path")
            ]
            cause = extract_case_cause(text, case_number)
            if not cause:
                cause = identify_case_cause(case_number, partial_available_files)
            download_result = summarize_download_result(
                materials, partial_new_files, partial_available_files
            )
            download_result += f"；处理异常：{compact_error(exc)}"
            report_results.append({
                "cause": cause,
                "materials": materials,
                "download_result": download_result,
                "calendar_result": "未添加（下载流程未完整完成，未进入日程添加步骤）",
                "case_folder": delivery_outcome.get("case_folder", ""),
            })
            continue
        if folder_name is None:
            state.setdefault("manual_messages", {})[str(message["rowid"])] = "该平台链接暂不支持自动处理"
            report_results.append({
                "cause": extract_case_cause(text, case_number),
                "materials": [],
                "download_result": "失败（链接需要网页交互、验证码或暂不支持后台直连）",
                "calendar_result": "未添加（尚未取得可用于创建日程的传票）",
                "case_folder": "",
            })
            continue
        effective_case_number = case_number or folder_name
        if report_case_number is None:
            report_case_number = effective_case_number
        calendar_outcome: dict = {}
        calendar_error: Exception | None = None
        try:
            add_calendar_from_summons(
                effective_case_number,
                available_files,
                calendar_outcome,
                emit_manual_reminders=False,
            )
        except Exception as exc:
            calendar_error = exc
            queue_pending_summons(state, effective_case_number, available_files)
            for pending_item in state["pending_summons"]:
                if pending_item.get("path") in [str(p) for p in available_files]:
                    pending_item["source_rowid"] = message["rowid"]
                    pending_item["received_at"] = received_at.isoformat()
            LOGGER.exception("传票日历写入失败，已加入自动重试队列 case=%s", effective_case_number)

        summons_count = int(calendar_outcome.get("summons_count", 0))
        successful_calendar_count = int(calendar_outcome.get("successful_count", 0))
        if not CONFIG.get("calendar_enabled", False):
            calendar_result = "未添加（未启用自动日历；请人工核对文件）"
        elif summons_count == 0:
            calendar_result = "未添加（本批材料未发现传票）"
        elif calendar_error is None:
            calendar_result = (
                f"成功（{successful_calendar_count}份传票对应日程已添加或更新）"
            )
        elif successful_calendar_count:
            calendar_result = (
                f"部分成功（成功{successful_calendar_count}份，至少1份失败；"
                f"失败材料已加入自动重试；原因：{compact_error(calendar_error)}）"
            )
        else:
            calendar_result = (
                f"失败（已加入自动重试；原因：{compact_error(calendar_error)}）"
            )

        if any(not item.get("status", "").startswith("成功") for item in delivery_outcome.get("materials", [])) or not available_files:
            download_failed = True
        else:
            if url not in state["downloaded_urls"]:
                state["downloaded_urls"].append(url)
        LOGGER.info("已核验本次送达材料 rowid=%s case=%s new_files=%s", message["rowid"], effective_case_number, len(new_files))
        cause = extract_case_cause(text, effective_case_number)
        if not cause:
            cause = next(iter(calendar_outcome.get("causes", [])), "")
        if not cause:
            cause = identify_case_cause(effective_case_number, available_files)
        materials = delivery_outcome.get("materials", [])
        report_results.append({
            "cause": cause,
            "materials": materials,
            "download_result": summarize_download_result(
                materials, new_files, available_files
            ),
            "calendar_result": calendar_result,
            "case_folder": delivery_outcome.get("case_folder", ""),
        })

    try:
        queue_sms_processing_report(
            state,
            message["rowid"],
            processed_at,
            report_case_number,
            report_results,
        )
    except Exception:
        raise RuntimeError("无法持久登记短信处理报告") from None
    for report in state["pending_reports"]:
        if report.get("report_id") == f"sms-{message['rowid']}":
            summary = URL_RE.sub("[链接请在信息App原短信中打开]", text)
            summary = re.sub(r"(验证码[：:为是\s]*)([A-Za-z0-9]{4,10})", r"\1[已隐藏]", summary)
            report["body"] += "\n\n短信接收时间：" + received_at.isoformat(sep=" ", timespec="seconds")
            report["body"] += "\n短信摘要：" + summary[:600]
            reason = state.get("manual_messages", {}).get(str(message["rowid"]))
            if reason:
                report["body"] += "\n需人工处理：" + reason
            break
    if download_failed:
        raise RuntimeError("下载未完整完成，保留短信任务重试")


def main() -> int:
    if not CONFIG_PATH.is_file() or not CONFIG.get("start_date"):
        print("Missing config/start_date; run prepare first", file=sys.stderr)
        return 64
    setup_logging()
    ROOT.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            state = load_state()
        except Exception as exc:
            LOGGER.error("状态不可用，已安全停止 type=%s", type(exc).__name__)
            return 3
        try:
            messages = fetch_new_messages(state["last_rowid"])
        except Exception:
            LOGGER.error("读取短信数据库失败，未推进扫描进度")
            return 2
        jobs = state["message_jobs"]
        for message in messages:
            received_at = message_received_at(message.get("date"))
            if received_at >= MIN_RECEIVED_AT:
                # Persist before executing any network or Calendar/Reminders side effects.
                jobs.setdefault(str(message["rowid"]), {"message": message, "status": "pending", "attempts": 0, "received_at": received_at.isoformat(), "next_retry_at": ""})
            state["last_rowid"] = message["rowid"]
        state["last_scan_at"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        attempted = 0
        for key in sorted(jobs, key=int, reverse=True):
            job = jobs[key]
            if job["status"] not in ("pending", "retry") or job.get("next_retry_at", "") > datetime.now().isoformat(timespec="seconds"):
                continue
            if attempted >= 10:
                break
            attempted += 1
            try:
                process_message(job["message"], state)
                job["status"] = "manual" if key in state.get("manual_messages", {}) else "done"
                job["completed_at"] = datetime.now().isoformat(timespec="seconds")
            except Exception as exc:
                job["attempts"] += 1
                job["status"] = "manual" if job["attempts"] >= 6 else "retry"
                job["error_type"] = type(exc).__name__
                job["next_retry_at"] = (datetime.now() + timedelta(minutes=min(60, 2 ** job["attempts"]))).isoformat(timespec="seconds")
                LOGGER.error("短信任务保留 rowid=%s status=%s type=%s", key, job["status"], type(exc).__name__)
            save_state(state)
        retry_pending_reports(state)
        save_state(state)
        retry_pending_summons(state)
        retry_pending_reminders(state)
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
