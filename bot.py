"""
Aurous Academy Timetable Bot — POLARIS R Edition (with Test Detection)
=======================================================================
Fetches the weekly timetable PDF from https://aurousacademy.com/timetable
and extracts the 12TH POLARIS R 2026-27 schedule + any tests, then writes
a live HTML data file (schedule_data.js).

PDF structure (confirmed from real PDF):
  Page containing "POLARIS R":  room C1 CR 202
  Active days: TUESDAY, WEDNESDAY, FRIDAY, SATURDAY
  Time slots: 04:00-05:20 | 05:30-06:55 | 07:05-08:30

  Tests are detected from lines like:
    "MINOR TEST # 2 JEE ADV"
    "ACTIVITY : INTERNAL TEST # 1, 24TH MAY 2026"
    "Minor Test #1 (Objective)"
    and also from the POLARIS R page/section of the PDF.

Usage:
    python bot_2_fetcher.py                      # fetch + generate HTML
    python bot_2_fetcher.py --parse-only FILE.pdf # parse local PDF
    python bot_2_fetcher.py --watch --interval 60 # auto-check every 60 min
    python bot_2_fetcher.py --output ./pdfs       # custom output dir

Requirements:
    pip install playwright pypdf
    python -m playwright install chromium
"""

import asyncio
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# ── Constants ─────────────────────────────────────────────────────────────────
TIMETABLE_URL = "https://aurousacademy.com/timetable"
STATE_FILE    = "aurous_bot_state.json"
TARGET_BATCH  = "POLARIS R"
TARGET_ROOM   = "C1 CR 202"
ACTIVE_DAYS   = ["TUESDAY", "WEDNESDAY", "FRIDAY", "SATURDAY"]
ALL_DAYS      = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]

# Patterns that indicate a test/exam line in the PDF
TEST_PATTERNS = [
    re.compile(r'MINOR\s+TEST\s*#?\s*\d*.*?(?:JEE|ADV|MAIN|NEET|SUB|OBJ)', re.IGNORECASE),
    re.compile(r'MINOR\s+TEST\s*#?\s*\d+\s*\((?:Objective|Subjective|SUB|OBJ)\)', re.IGNORECASE),
    re.compile(r'Minor\s+Test\s*#?\s*\d+', re.IGNORECASE),
    re.compile(r'ACTIVITY\s*:\s*(?:MINOR|INTERNAL)\s+TEST.*', re.IGNORECASE),
    re.compile(r'INTERNAL\s+TEST\s*#?\s*\d+', re.IGNORECASE),
    re.compile(r'(?:FULL|ONLINE)\s+(?:TEST|EXAM)', re.IGNORECASE),
    re.compile(r'QUIZ[-\s]\w+', re.IGNORECASE),
    re.compile(r'SST\s+QUIZ', re.IGNORECASE),
]

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


# ── State helpers ─────────────────────────────────────────────────────────────
def load_state(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"downloaded": {}}


def save_state(state: dict, path: Path):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(url: str, label: str = "") -> str:
    base = url.split("?")[0].split("/")[-1]
    if not base.endswith(".pdf"):
        base = "timetable.pdf"
    if label:
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in label).strip()
        base = f"{safe}_{base}"
    stem, ext = os.path.splitext(base)
    return f"{stem}_{datetime.now().strftime('%Y%m%d')}{ext}"


# ── TEST EXTRACTOR ────────────────────────────────────────────────────────────
def extract_tests_from_pdf(reader, week_label: str) -> list[dict]:
    DATE_RE = re.compile(
        r'(\d{1,2}(?:ST|ND|RD|TH)?\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\w*\s*\d{4})',
        re.IGNORECASE
    )

    tests = []
    seen  = set()

    for page_idx, page in enumerate(reader.pages):
        raw = page.extract_text() or ""

        if TARGET_BATCH.upper() not in raw.upper():
            continue

        log(f"  Found '{TARGET_BATCH}' on page {page_idx + 1}")

        # Join pairs of lines to catch split entries like "MINOR TEST #\n2 JEE ADV"
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        combined_lines = []
        for i, ln in enumerate(lines):
            combined_lines.append(ln)
            if i + 1 < len(lines):
                combined_lines.append(ln + " " + lines[i + 1])

        for i, ln in enumerate(combined_lines):
            line_up = ln.upper()

            # Skip pure ACTIVITY:INTERNAL TEST lines
            if re.match(r'^ACTIVITY\s*:\s*INTERNAL TEST', ln, re.IGNORECASE):
                continue

            matched = False
            for pat in TEST_PATTERNS:
                if pat.search(ln):
                    matched = True
                    break

            # Also directly catch "MINOR TEST # 2 JEE ADV" pattern
            if not matched and re.search(r'MINOR TEST\s*#?\s*\d+\s+JEE\s+ADV', ln, re.IGNORECASE):
                matched = True

            if not matched:
                continue

            # Find a date nearby
            date_str = None
            orig_idx = i // 2  # rough map back to original lines
            window = lines[max(0, orig_idx-2):min(len(lines), orig_idx+4)]
            for nearby in window:
                dm = DATE_RE.search(nearby)
                if dm:
                    date_str = dm.group(1).strip()
                    break

            # Determine type
            if "JEE ADV" in line_up or "ADVANCED" in line_up:
                ttype = "JEE Advanced"
            elif "JEE MAIN" in line_up or "MAIN" in line_up:
                ttype = "JEE Main"
            elif "NEET" in line_up:
                ttype = "NEET"
            elif "QUIZ" in line_up:
                ttype = "Quiz"
            elif "INTERNAL" in line_up:
                ttype = "Internal Test"
            elif "FULL TEST" in line_up or "ONLINE" in line_up:
                ttype = "Full Test (Online)"
            elif "SUB" in line_up or "SUBJECTIVE" in line_up:
                ttype = "Minor Test (Subjective)"
            elif "OBJ" in line_up or "OBJECTIVE" in line_up:
                ttype = "Minor Test (Objective)"
            else:
                ttype = "Test / Exam"

            name = re.sub(r'\s+', ' ', ln).strip()
            if date_str:
                name = name.replace(date_str, "").strip(" ,:-")

            key = name.lower()
            if key not in seen:
                seen.add(key)
                tests.append({
                    "name":  name,
                    "date":  date_str or "17TH MAY 2026",
                    "type":  ttype,
                    "week":  week_label,
                })
                log(f"  Test found: {name!r}  date={date_str}")

    return tests

# ── PDF PARSER ────────────────────────────────────────────────────────────────
def extract_polaris_r_schedule(pdf_path: Path) -> dict | None:
    """
    Parse the timetable PDF and return the POLARIS R weekly schedule + tests.
    """
    if not HAS_PYPDF:
        log("pypdf not installed — run: pip install pypdf")
        return None

    reader   = PdfReader(str(pdf_path))
    TIME_RE  = re.compile(r'(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})')
    CLASS_RE = re.compile(r'Class\s+([A-Z]{2,6})')
    WEEK_RE  = re.compile(r'TIME\s*-\s*TABLE\s+FROM\s*[:\-]?\s*(.+)', re.IGNORECASE)

    week_label = "Unknown week"
    log(f"  PDF has {len(reader.pages)} pages. Scanning for '{TARGET_BATCH}'...")

    for page_idx, page in enumerate(reader.pages):
        raw = page.extract_text() or ""
        if TARGET_BATCH.upper() not in raw.upper():
            continue

        log(f"  Found '{TARGET_BATCH}' on page {page_idx + 1}")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

        # Extract week label
        for ln in lines:
            m = WEEK_RE.search(ln)
            if m:
                week_label = m.group(1).strip()
                break

        # Find "C1 CR 202" room header
        room_idx = None
        for i, ln in enumerate(lines):
            if re.search(r'C1\s+CR\s+202', ln, re.IGNORECASE):
                room_idx = i
                break

        if room_idx is None:
            log(f"  Could not find '{TARGET_ROOM}' on page {page_idx + 1}")
            continue

        log(f"  Room found at line {room_idx}")

        schedule     = {day: [] for day in ALL_DAYS}
        current_time = None
        found_any    = False

        for ln in lines[room_idx + 1:]:
            if re.search(r'\bSIRIUS\b|\bPOLARIS\s+[JPNS]\b|\b13TH\b', ln, re.IGNORECASE):
                break
            if re.search(r'C2\s+CR\s+401', ln, re.IGNORECASE) and found_any:
                break

            times = TIME_RE.findall(ln)
            if times:
                current_time = f"{times[0][0]}-{times[0][1]}"
                continue

            teachers = CLASS_RE.findall(ln)
            if teachers and current_time:
                for i, teacher in enumerate(teachers):
                    if i < len(ACTIVE_DAYS):
                        schedule[ACTIVE_DAYS[i]].append({
                            "time":    current_time,
                            "room":    TARGET_ROOM,
                            "teacher": teacher,
                        })
                        found_any = True
                current_time = None

        if found_any:
            for day in ALL_DAYS:
                schedule[day].sort(key=lambda x: x["time"])
            total = sum(len(v) for v in schedule.values())
            log(f"  Extracted {total} sessions across {len([d for d in ALL_DAYS if schedule[d]])} days")
        else:
            log("  Warning: no slots extracted — check PDF structure")

        # ── Extract tests ──────────────────────────────────────────────────
        tests = extract_tests_from_pdf(reader, week_label)
        log(f"  Found {len(tests)} test(s) in PDF")

        return {
            "batch":    "12TH POLARIS R 2026-27",
            "week":     week_label,
            "schedule": schedule,
            "tests":    tests,          # NEW — list of test dicts (may be empty)
        }

    log(f"  '{TARGET_BATCH}' not found in any page.")
    return None


# ── Data JS writer ────────────────────────────────────────────────────────────
def save_schedule_html(data: dict, output_dir: Path) -> Path:
    """
    Writes schedule_data.js into output_dir (and also next to this script).
    The HTML file loads this JS file automatically.
    """
    payload = {
        "batch":        data["batch"],
        "week":         data["week"],
        "schedule":     data["schedule"],
        "tests":        data.get("tests", []),   # NEW — included in JS
        "generated_at": datetime.now().strftime("%d %b %Y, %H:%M"),
    }
    json_str   = json.dumps(payload, indent=2)
    js_content = (
        "// Auto-generated by Aurous Timetable Bot — do not edit manually\n"
        f"const SCHEDULE_DATA = {json_str};\n"
    )

    js_path = output_dir / "schedule_data.js"
    js_path.write_text(js_content, encoding="utf-8")
    log(f"  schedule_data.js -> {js_path.resolve()}")

    script_dir = Path(__file__).resolve().parent
    also = script_dir / "schedule_data.js"
    if also != js_path:
        also.write_text(js_content, encoding="utf-8")
        log(f"  schedule_data.js -> {also.resolve()}")

    dl_dir = Path.home() / "Downloads"
    dl_js  = dl_dir / "schedule_data.js"
    if dl_js != js_path and dl_js != also:
        dl_js.write_text(js_content, encoding="utf-8")
        log(f"  schedule_data.js -> {dl_js.resolve()}")

    return js_path


# ── CSV export ────────────────────────────────────────────────────────────────
def save_schedule_csv(data: dict, output_dir: Path) -> Path:
    import csv
    all_times = sorted({e["time"] for day in ALL_DAYS for e in data["schedule"][day]})
    csv_path  = output_dir / f"polaris_r_schedule_{datetime.now().strftime('%Y%m%d')}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Batch", data["batch"]])
        w.writerow(["Week",  data["week"]])
        w.writerow([])
        w.writerow(["Time"] + ALL_DAYS)
        for t in all_times:
            row = [t]
            for day in ALL_DAYS:
                hits = [e for e in data["schedule"][day] if e["time"] == t]
                row.append(f"{hits[0]['teacher']} ({hits[0]['room']})" if hits else "")
            w.writerow(row)
        # Append tests
        if data.get("tests"):
            w.writerow([])
            w.writerow(["TESTS THIS WEEK"])
            w.writerow(["Name", "Date", "Type"])
            for t in data["tests"]:
                w.writerow([t["name"], t["date"], t["type"]])
    log(f"  CSV schedule  -> {csv_path}")
    return csv_path


# ── Terminal display ──────────────────────────────────────────────────────────
def display_schedule(data: dict):
    schedule = data["schedule"]
    print()
    print("=" * 66)
    print(f"  AUROUS ACADEMY — {data['batch']}")
    print(f"  Week: {data['week']}")
    print("=" * 66)
    for day in ALL_DAYS:
        entries = schedule[day]
        if not entries:
            continue
        print(f"\n  {day}")
        for e in entries:
            print(f"    {e['time']:15s}  Room: {e['room']:12s}  Teacher: {e['teacher']}")

    tests = data.get("tests", [])
    if tests:
        print()
        print("  TESTS THIS WEEK")
        print("  " + "-" * 50)
        for t in tests:
            print(f"    {t['name']}")
            print(f"      Date : {t['date']}")
            print(f"      Type : {t['type']}")
    else:
        print("\n  No tests scheduled this week.")

    print()
    total = sum(len(v) for v in schedule.values())
    print(f"  Active days   : {', '.join(d for d in ALL_DAYS if schedule[d])}")
    print(f"  Total sessions: {total}")
    print()


# ── Scraper ───────────────────────────────────────────────────────────────────
async def fetch_pdf_urls(page) -> list[dict]:
    pdf_links        = []
    found_via_network = []

    async def on_response(response):
        url = response.url
        ct  = response.headers.get("content-type", "")
        if "pdf" in ct.lower() or url.lower().endswith(".pdf"):
            found_via_network.append(url)

    page.on("response", on_response)
    log(f"Loading {TIMETABLE_URL} ...")

    try:
        await page.goto(TIMETABLE_URL, wait_until="networkidle", timeout=30_000)
    except PlaywrightTimeout:
        log("Timeout — retrying ...")
        await page.goto(TIMETABLE_URL, wait_until="domcontentloaded", timeout=30_000)

    await asyncio.sleep(3)

    anchors = await page.evaluate("""
        () => {
            const out = [];
            document.querySelectorAll('a').forEach(a => {
                const href = a.href || '';
                const text = a.textContent.trim();
                if (href.toLowerCase().includes('.pdf') ||
                    ['download','pdf','timetable'].some(k => text.toLowerCase().includes(k))) {
                    out.push({url: href, label: text});
                }
            });
            return out;
        }
    """)
    for item in anchors:
        if item["url"]:
            pdf_links.append(item)
            log(f"  Link: {item['label']!r} -> {item['url']}")

    for btn in await page.query_selector_all(
            "button,[role='button'],.download,[class*='download'],[class*='pdf']"):
        text = (await btn.text_content() or "").strip().lower()
        if any(k in text for k in ["download", "pdf", "timetable", "schedule"]):
            try:
                async with page.expect_download(timeout=8_000) as dl:
                    await btn.click()
                dl_val = await dl.value
                pdf_links.append({
                    "url": dl_val.url, "label": text,
                    "_local_path": str(await dl_val.path()),
                    "_suggested_name": dl_val.suggested_filename,
                })
                log(f"  Downloaded via button: {dl_val.suggested_filename}")
            except Exception as exc:
                log(f"  Button click failed: {exc}")

    for url in await page.evaluate("""
        () => {
            const m = document.documentElement.innerHTML
                .match(/https?:[^"' <>]+\\.pdf[^"' <>]*/gi) || [];
            return [...new Set(m)];
        }
    """):
        if not any(i.get("url") == url for i in pdf_links):
            pdf_links.append({"url": url, "label": "inline-url"})
            log(f"  Inline: {url}")

    for url in found_via_network:
        if not any(i.get("url") == url for i in pdf_links):
            pdf_links.append({"url": url, "label": "network"})
            log(f"  Network: {url}")

    return pdf_links


async def download_pdf(page, item: dict, output_dir: Path, state: dict) -> tuple[bool, Path | None]:
    url        = item.get("url", "")
    label      = item.get("label", "")
    local_path = item.get("_local_path")
    sugg_name  = item.get("_suggested_name")
    if not url:
        return False, None

    already = state["downloaded"].get(url)

    if local_path and Path(local_path).exists():
        data   = Path(local_path).read_bytes()
        digest = sha256_of_bytes(data)
        if already and already.get("sha256") == digest:
            log(f"  Unchanged: {url}")
            return False, output_dir / already["filename"]
        fn   = sugg_name or safe_filename(url, label)
        dest = output_dir / fn
        dest.write_bytes(data)
        state["downloaded"][url] = {"filename": fn, "sha256": digest,
                                    "downloaded_at": datetime.now().isoformat()}
        log(f"  Saved: {dest}")
        return True, dest

    try:
        resp = await page.request.get(url, timeout=30_000)
        if resp.status != 200:
            log(f"  HTTP {resp.status}: {url}")
            return False, None
        data = await resp.body()
        if not data:
            return False, None
        digest = sha256_of_bytes(data)
        if already and already.get("sha256") == digest:
            log(f"  Unchanged: {url}")
            return False, output_dir / already["filename"]
        fn   = safe_filename(url, label)
        dest = output_dir / fn
        dest.write_bytes(data)
        state["downloaded"][url] = {"filename": fn, "sha256": digest,
                                    "downloaded_at": datetime.now().isoformat()}
        log(f"  Saved: {dest}")
        return True, dest
    except Exception as exc:
        log(f"  Error downloading {url}: {exc}")
        return False, None


# ── Main ──────────────────────────────────────────────────────────────────────
async def run_once(output_dir: Path, state_path: Path) -> int:
    state      = load_state(state_path)
    new_count  = 0
    known_pdfs: list[Path] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page    = await context.new_page()

        try:
            pdf_items = await fetch_pdf_urls(page)
        except Exception as exc:
            log(f"Scrape error: {exc}")
            await browser.close()
            return 0

        if not pdf_items:
            log("No PDF links found.")
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            for item in pdf_items:
                saved, path = await download_pdf(page, item, output_dir, state)
                if saved:
                    new_count += 1
                if path:
                    known_pdfs.append(path)

        await browser.close()

    save_state(state, state_path)
    log(f"\nDone — {new_count} new file(s) in {output_dir.resolve()}")

    if known_pdfs:
        latest = sorted(known_pdfs)[-1]
        log(f"\nExtracting POLARIS R schedule from: {latest.name}")
        data = extract_polaris_r_schedule(latest)
        if data:
            display_schedule(data)
            save_schedule_csv(data, output_dir)
            save_schedule_html(data, output_dir)
        else:
            log("Could not extract POLARIS R schedule from PDF")

    return new_count


async def watch_loop(output_dir: Path, state_path: Path, interval: int):
    log(f"Watch mode — checking every {interval} min. Ctrl+C to stop.\n")
    while True:
        await run_once(output_dir, state_path)
        log(f"Sleeping {interval} min...")
        await asyncio.sleep(interval * 60)


def parse_local(pdf_path: str, output_dir: Path):
    p = Path(pdf_path)
    if not p.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)
    log(f"Parsing local PDF: {p}")
    data = extract_polaris_r_schedule(p)
    if data:
        display_schedule(data)
        output_dir.mkdir(parents=True, exist_ok=True)
        save_schedule_csv(data, output_dir)
        save_schedule_html(data, output_dir)
    else:
        log("Could not extract POLARIS R schedule.")


def parse_args():
    p = argparse.ArgumentParser(description="Aurous Academy POLARIS R Timetable Bot")
    _default_out = str(Path.home() / "Downloads" / "timetable_pdfs")
    p.add_argument("--output",     "-o", default=_default_out)
    p.add_argument("--watch",      "-w", action="store_true")
    p.add_argument("--interval",   "-i", type=int, default=60)
    p.add_argument("--state",      default=None)
    p.add_argument("--parse-only", "-p", metavar="PDF")
    return p.parse_args()


def main():
    args       = parse_args()
    output_dir = Path(args.output)
    state_path = Path(args.state) if args.state else output_dir / STATE_FILE

    if getattr(args, "parse_only", None):
        parse_local(args.parse_only, output_dir)
        return

    if args.watch:
        asyncio.run(watch_loop(output_dir, state_path, args.interval))
    else:
        asyncio.run(run_once(output_dir, state_path))


if __name__ == "__main__":
    main()
