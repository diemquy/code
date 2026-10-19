import json
import os
import re
import sys
import unicodedata
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

OUTPUT = Path(__file__).resolve().with_name("codes.json")
WIKI = "https://blox-fruits.fandom.com/api.php"
TGDD = (
    "https://www.thegioididong.com/game-app/"
    "cach-nhap-nhan-blox-fruit-moi-nhat-co-san-bo-code-1307151"
)


def label(text):
    text = unicodedata.normalize("NFKD", text.casefold().replace("đ", "d"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def code_value(text):
    text = text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", text):
        raise ValueError(f"Unexpected code cell: {text[:80]!r}")
    return text


def code_map(values):
    result = {}
    for value in values:
        value = code_value(value)
        result.setdefault(value.casefold(), value)
    return result


def validate_source(active, expired):
    if not 5 <= len(active) <= 200:
        raise ValueError(f"Implausible active code count: {len(active)}")
    if active.keys() & expired:
        raise ValueError("Source labels the same code active and expired")
    return {"active": active, "expired": expired}


def parse_wiki(html):
    soup = BeautifulSoup(html, "html.parser")
    groups = {"working codes": {}, "expired codes": {}}
    seen = set()
    for table in soup.find_all("table"):
        heading = table.find_previous(["h2", "h3", "h4"])
        section = label(heading.get_text(" ", strip=True)) if heading else ""
        section = section.replace("[ ]", "").strip()
        if section not in groups:
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [label(c.get_text(" ", strip=True)) for c in rows[0].find_all(["th", "td"], recursive=False)]
        if "code" not in headers or "reward" not in headers:
            continue
        seen.add(section)
        index = headers.index("code")
        for row in rows[1:]:
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            if len(cells) != len(headers):
                raise ValueError("Wiki code table shape changed")
            code = code_value(cells[index].get_text("", strip=True))
            groups[section].setdefault(code.casefold(), code)
    if seen != set(groups):
        raise ValueError("Wiki working/expired sections were not both found")
    return validate_source(groups["working codes"], set(groups["expired codes"]))


def parse_tgdd(html):
    soup = BeautifulSoup(html, "html.parser")
    active, expired = {}, set()
    found_summary = False
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [label(c.get_text(" ", strip=True)) for c in rows[0].find_all(["th", "td"], recursive=False)]
        if not headers or not headers[0].startswith("code blox fruit") or "trang thai" not in headers:
            continue
        summary = headers[0] == "code blox fruit moi nhat"
        found_summary |= summary
        status_index = headers.index("trang thai")
        for row in rows[1:]:
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            if len(cells) != len(headers):
                raise ValueError("TGDD code table shape changed")
            status = label(cells[status_index].get_text(" ", strip=True))
            code = code_value(cells[0].get_text("", strip=True))
            if status == "het han":
                expired.add(code.casefold())
            elif status in {"moi nhat", "moi", "con han", "con dung duoc"}:
                if summary:
                    active.setdefault(code.casefold(), code)
            else:
                raise ValueError(f"Unrecognized TGDD status: {status!r}")
    if not found_summary:
        raise ValueError("TGDD latest-code summary not found")
    return validate_source(active, expired)


def make_session():
    session = requests.Session()
    session.headers["User-Agent"] = "MZAT-BloxFruits-Codes/1.0 (public code list updater)"
    retry = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"], respect_retry_after_header=False)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_wiki(session):
    response = session.get(WIKI, params={"action": "parse", "page": "Codes", "prop": "text", "format": "json"}, timeout=(10, 30))
    response.raise_for_status()
    return parse_wiki(response.json()["parse"]["text"]["*"])


def fetch_tgdd(session):
    response = session.get(TGDD, timeout=(10, 30))
    response.raise_for_status()
    return parse_tgdd(response.content)


def merge_codes(previous, results):
    if not results:
        raise ValueError("All sources failed; keeping codes.json unchanged")
    merged = code_map(previous) if len(results) < 2 else {}
    for name in ("tgdd", "wiki"):
        if name in results:
            source = results[name]
            for key in source["expired"]:
                merged.pop(key, None)
            merged.update(source["active"])
    codes = sorted(merged.values(), key=str.casefold)
    if not codes:
        raise ValueError("Refusing to publish an empty code list")
    if previous and len(codes) < len(previous) / 2:
        raise ValueError("More than half the codes disappeared; keeping old data for review")
    return codes


def save_if_changed(path, previous, codes):
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(timespec="seconds")
    codes_changed = previous.get("codes") != codes

    data = {
        "updated_at": now if codes_changed else previous.get("updated_at"),
        "checked_at": now,
        "codes": codes,
    }

    if data == previous:
        print(f"No changes ({len(codes)} codes)")
        return False

    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    print(f"Checked {len(codes)} codes at {now}")
    return True

def main():
    previous = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {"codes": []}
    if not isinstance(previous, dict) or not isinstance(previous.get("codes"), list):
        raise ValueError("Invalid existing codes.json; refusing to overwrite")
    code_map(previous["codes"])
    results, errors = {}, []
    with make_session() as session:
        for name, fetch in (("wiki", fetch_wiki), ("tgdd", fetch_tgdd)):
            try:
                results[name] = fetch(session)
                print(f"{name}: {len(results[name]['active'])} active, {len(results[name]['expired'])} expired")
            except (requests.RequestException, ValueError, KeyError, TypeError) as error:
                message = f"{name}: {error}"
                errors.append(message)
                print(f"::warning::{message}", file=sys.stderr)
    codes = merge_codes(previous["codes"], results)
    save_if_changed(OUTPUT, previous, codes)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as report:
            report.write(f"Published list: **{len(codes)} codes**. Successful sources: {', '.join(results)}.\n\n")
            if errors:
                report.write("A source failed. Previous entries were retained unless explicitly expired. See workflow warnings.\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TypeError, OSError) as error:
        print(f"::error::{error}", file=sys.stderr)
        sys.exit(1)
