#!/usr/bin/env python3
"""Daily "MARKET BRIEF" — a one-page dark-themed market image, emailed daily.

Pipeline:
  1. Fetch index levels + daily % change (Yahoo Finance, via market_news_daily).
  2. Fetch market headlines from free RSS feeds.
  3. Ask Claude to write the Thai-language analysis sections (overview, per-region
     notes, sectors to watch, event calendar, Thailand focus) from that data.
  4. Render a dark two-column HTML brief to PNG with headless Chromium.
  5. Email the PNG as an attachment.

Environment variables
---------------------
ANTHROPIC_API_KEY  : for the analysis step (falls back to data-only if unset)
GMAIL_USER         : Gmail address that sends the mail
GMAIL_APP_PASSWORD : Google app password for that account
MAIL_TO            : recipient (defaults to GMAIL_USER)
CHROMIUM_PATH      : optional explicit path to the Chromium binary (for sandboxes
                     where Playwright can't auto-discover it; unset on CI)

Usage
-----
    python scripts/market_brief.py            # build + email
    python scripts/market_brief.py --dry-run  # build PNG only, don't email
"""

from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header

from market_news_daily import fetch_quote, fetch_headlines, ICT

OUT_PNG = os.path.join(os.path.dirname(__file__), "market_brief.png")

THAI_MONTHS = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
               "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
THAI_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]


@dataclass
class Idx:
    region: str      # region grouping key
    cc: str          # 2-letter country code shown next to the name
    name: str        # display name
    symbol: str      # Yahoo Finance symbol


# Left-column markets, grouped by region. Order here is render order.
REGIONS = [
    ("ตลาดสหรัฐฯ", "#3b82f6", [
        Idx("us", "US", "S&P 500", "^GSPC"),
        Idx("us", "US", "Nasdaq", "^IXIC"),
        Idx("us", "US", "Dow Jones", "^DJI"),
    ]),
    ("ตลาดยุโรป", "#7c3aed", [
        Idx("eu", "DE", "DAX", "^GDAXI"),
        Idx("eu", "FR", "CAC 40", "^FCHI"),
        Idx("eu", "ES", "IBEX 35", "^IBEX"),
        Idx("eu", "IT", "FTSE MIB", "FTSEMIB.MI"),
    ]),
    ("ตลาดเอเชีย", "#0891b2", [
        Idx("asia", "JP", "Nikkei 225", "^N225"),
        Idx("asia", "KR", "KOSPI", "^KS11"),
        Idx("asia", "CN", "Shanghai", "000001.SS"),
        Idx("asia", "HK", "Hang Seng", "^HSI"),
        Idx("asia", "TW", "TAIEX", "^TWII"),
        Idx("asia", "IN", "Sensex", "^BSESN"),
        Idx("asia", "IN", "Nifty 50", "^NSEI"),
    ]),
    ("ตลาดเกิดใหม่", "#ea580c", [
        Idx("em", "BR", "Ibovespa", "^BVSP"),
        Idx("em", "ID", "JCI", "^JKSE"),
    ]),
]

# FX, commodities, and crypto — shown as a separate card in the right column.
EXTRAS = [
    Idx("x", "USD", "ทองคำ (Gold)", "GC=F"),
    Idx("x", "USD", "น้ำมัน Brent", "BZ=F"),
    Idx("x", "USD", "Bitcoin", "BTC-USD"),
    Idx("x", "฿", "USD/THB", "THB=X"),
    Idx("x", "$", "EUR/USD", "EURUSD=X"),
]

# Single-stock watchlist (global movers). `cc` is the ticker key that Claude
# uses to attach a one-line news note.
STOCKS = [
    Idx("stk", "NVDA", "Nvidia", "NVDA"),
    Idx("stk", "TSM", "TSMC", "TSM"),
    Idx("stk", "AAPL", "Apple", "AAPL"),
    Idx("stk", "MSFT", "Microsoft", "MSFT"),
    Idx("stk", "TSLA", "Tesla", "TSLA"),
    Idx("stk", "META", "Meta", "META"),
]

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "array", "items": {"type": "string"}},
        "us_notes": {"type": "array", "items": {"type": "string"}},
        "europe_notes": {"type": "array", "items": {"type": "string"}},
        "asia_notes": {"type": "array", "items": {"type": "string"}},
        "emerging_notes": {"type": "array", "items": {"type": "string"}},
        "sectors": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "badge": {"type": "string"},
                "tone": {"type": "string", "enum": ["hi", "re", "ne"]},
                "text": {"type": "string"},
            },
            "required": ["name", "badge", "tone", "text"],
            "additionalProperties": False,
        }},
        "events": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "when": {"type": "string"},
                "star": {"type": "boolean"},
                "text": {"type": "string"},
            },
            "required": ["when", "star", "text"],
            "additionalProperties": False,
        }},
        "stocks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["ticker", "note"],
            "additionalProperties": False,
        }},
        "headlines_th": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "us_notes", "europe_notes", "asia_notes",
                 "emerging_notes", "sectors", "stocks", "events", "headlines_th"],
    "additionalProperties": False,
}

ANALYSIS_SYSTEM = (
    "คุณเป็นนักวิเคราะห์ตลาดทุนที่เขียนสรุปตลาดหุ้นโลกประจำวันเป็นภาษาไทย "
    "กระชับ เป็นทางการ เหมาะกับนักลงทุนมืออาชีพ "
    "ต้องอ้างอิงจาก 'ข่าวจริงที่ค้นมา' และ 'ตัวเลขจริง' ที่ให้ไว้เท่านั้น "
    "ห้ามกุข่าว/ตัวเลข/เหตุการณ์/ชื่อหุ้น ที่ไม่ปรากฏในข้อมูลที่ให้มาเด็ดขาด "
    "ถ้าไม่มีข่าวยืนยันสาเหตุ ให้เขียนเชิงคุณภาพตามทิศทางราคาจริง (เช่น 'ปรับขึ้นตามแรงซื้อกลุ่มเทค') "
    "ข้อความทุกส่วนสั้น กระชับ (สูงสุด ~2 บรรทัด)"
)


def _web_search_news(client, date_str: str) -> str:
    """Use Claude's web_search tool to gather real, current market-moving news."""
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    query = (
        f"วันนี้คือ {date_str} ช่วยค้นข่าวตลาดหุ้นล่าสุดของวันนี้/เมื่อคืนที่ขับเคลื่อนตลาดหุ้น "
        "สหรัฐฯ · ยุโรป · เอเชีย รวมถึงหุ้นเทคใหญ่ (Nvidia, TSMC, Apple, Microsoft, Tesla, Meta), "
        "ทองคำ, น้ำมัน, Bitcoin และตัวเลข/เหตุการณ์เศรษฐกิจสำคัญ. "
        "สรุปเป็นข้อเท็จจริงสั้นๆ เป็นข้อๆ พร้อมตัวเลข/เหตุการณ์จริงเท่าที่ค้นพบ ห้ามเดา "
        "ระบุด้วยว่าอะไรทำให้แต่ละตลาด/หุ้นขึ้นหรือลง."
    )
    messages = [{"role": "user", "content": query}]
    try:
        resp = None
        for _ in range(4):  # follow the server-side search loop across pause_turn
            resp = client.messages.create(
                model="claude-opus-4-8", max_tokens=3000, tools=tools, messages=messages,
            )
            if resp.stop_reason == "pause_turn":
                messages = [messages[0], {"role": "assistant", "content": resp.content}]
                continue
            break
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: web search failed: {exc}", file=sys.stderr)
        return ""


def fetch_analysis(quotes: dict, headlines: list[str]) -> dict | None:
    """Ask Claude to write the editorial sections. None if unavailable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY not set — building without analysis",
              file=sys.stderr)
        return None
    try:
        import anthropic
    except ImportError:
        print("WARNING: anthropic package not installed — skipping analysis",
              file=sys.stderr)
        return None

    data_lines = []
    for _, _, items in REGIONS:
        for idx in items:
            q = quotes.get(idx.symbol)
            if q is None:
                data_lines.append(f"{idx.name} ({idx.cc}): ไม่มีข้อมูล")
            else:
                data_lines.append(
                    f"{idx.name} ({idx.cc}): {fmt_num(q.close)} ({q.change_pct:+.2f}%)")
    for idx in EXTRAS:
        q = quotes.get(idx.symbol)
        if q is not None:
            data_lines.append(f"{idx.name}: {fmt_num(q.close)} ({q.change_pct:+.2f}%)")
    stock_lines = []
    for idx in STOCKS:
        q = quotes.get(idx.symbol)
        if q is not None:
            stock_lines.append(f"{idx.cc} ({idx.name}): {q.change_pct:+.2f}%")
    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: cannot init Anthropic client: {exc}", file=sys.stderr)
        return None

    # Step 1: fetch real, current news from the web.
    date_str = datetime.now(ICT).strftime("%-d %B %Y")
    news_context = _web_search_news(client, date_str)
    if news_context:
        news_section = "\n\nข่าวจริงที่ค้นจากเว็บวันนี้ (ใช้เป็นหลักในการเขียน):\n" + news_context
    else:
        news_section = "\n\nพาดหัวข่าวล่าสุด (อังกฤษ):\n" + "\n".join(f"- {h}" for h in headlines)

    prompt = (
        f"วันนี้: {date_str}\n"
        "ข้อมูลดัชนีวันนี้:\n" + "\n".join(data_lines) +
        "\n\nหุ้นรายตัว (% วันนี้):\n" + "\n".join(stock_lines) +
        news_section +
        "\n\nช่วยเขียนสรุปตามโครงสร้าง JSON โดยอ้างอิงจากข่าวจริง/ตัวเลขจริงข้างต้นเท่านั้น:\n"
        "- overview: 2 บูลเล็ตภาพรวมตลาดวันนี้\n"
        "- us_notes / europe_notes / asia_notes / emerging_notes: บูลเล็ตอธิบาย "
        "'สาเหตุ/ข่าว' ที่ทำให้ดัชนีในภูมิภาคนั้นขึ้นหรือลง (2-4 บูลเล็ตต่อภูมิภาค — "
        "สหรัฐฯ ให้ละเอียดสุด เช่น หุ้น/กลุ่มที่นำตลาด, เหตุการณ์สำคัญ, ปัจจัยกดดัน) "
        "อิงจากตัวเลขจริง + พาดหัวข่าว ห้ามกุเหตุการณ์เฉพาะที่ไม่มีในข่าว\n"
        "- sectors: 2-3 เซกเตอร์จับตา แต่ละอันมี name, badge (ป้ายสั้นๆ เช่น 'ผันผวนสูง'), "
        "tone ('hi'=ลบ/เสี่ยง, 're'=บวก/ฟื้น, 'ne'=กลาง), text (บทวิเคราะห์สั้น)\n"
        "- stocks: ข่าว/ความเคลื่อนไหวหุ้นรายตัวสั้นๆ (1 บรรทัด) ให้ครบทุกตัวในลิสต์ "
        "โดย ticker ต้องตรงกับที่ให้มา (NVDA, TSM, AAPL, MSFT, TSLA, META) "
        "อิงจาก % จริง + บริบทกลุ่ม/ข่าว ห้ามกุเหตุการณ์เฉพาะเจาะจงที่ไม่มีในข่าว\n"
        "- events: 3-4 เหตุการณ์จับตาวันนี้/สัปดาห์นี้ แต่ละอันมี when (เช่น 'พฤ. 2 ก.ค.'), "
        "star (true เฉพาะอันสำคัญสุด), text\n"
        "- headlines_th: แปล/สรุปข่าวเด่นจริง 3 อันเป็นไทยสั้นๆ"
    )
    # Step 2: structure the analysis as JSON (no tools, so the schema is honored).
    try:
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=2500,
            system=ANALYSIS_SYSTEM,
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully on any API error
        print(f"WARNING: analysis step failed: {exc}", file=sys.stderr)
        return None


def _e(text: str) -> str:
    return html.escape(text or "")


def fmt_num(value: float) -> str:
    """Whole numbers for large levels (indices, BTC); 2 decimals for small (oil, FX)."""
    return f"{value:,.2f}" if abs(value) < 1000 else f"{value:,.0f}"


def _row_html(idx: Idx, quotes: dict) -> str:
    q = quotes.get(idx.symbol)
    if q is None:
        return (f'<div class="row"><div class="nm">{_e(idx.name)} '
                f'<span class="cc">{idx.cc}</span></div>'
                f'<div class="val">—</div><div class="pct flat">ไม่มีข้อมูล</div></div>')
    cls = "up" if q.change_pct > 0 else "down" if q.change_pct < 0 else "flat"
    arrow = "▲" if q.change_pct > 0 else "▼" if q.change_pct < 0 else "▪"
    return (f'<div class="row"><div class="nm">{_e(idx.name)} '
            f'<span class="cc">{idx.cc}</span></div>'
            f'<div class="val">{fmt_num(q.close)}</div>'
            f'<div class="pct {cls}">{arrow} {q.change_pct:+.2f}%</div></div>')


def _bullets(items: list) -> str:
    return "".join(f'<div class="bullet">{_e(x)}</div>' for x in (items or []) if x)


def _us_cards(items: list, quotes: dict) -> str:
    """Big highlight cards for the headline US indices."""
    cards = ""
    for idx in items:
        q = quotes.get(idx.symbol)
        if q is None:
            val, pct, cls = "—", "ไม่มีข้อมูล", "flat"
        else:
            cls = "up" if q.change_pct > 0 else "down" if q.change_pct < 0 else "flat"
            arrow = "▲" if q.change_pct > 0 else "▼" if q.change_pct < 0 else "▪"
            val = fmt_num(q.close)
            pct = f"{q.change_pct:+.2f}% {arrow}"
        cards += (f'<div class="uscard"><div class="un">{_e(idx.name)}</div>'
                  f'<div class="uv">{val}</div>'
                  f'<div class="up2 {cls}">{pct}</div></div>')
    return f'<div class="usgrid">{cards}</div>'


def build_html(quotes: dict, analysis: dict | None) -> str:
    now = datetime.now(ICT)
    date_str = f"{now.day} {THAI_MONTHS[now.month]} {now.year}"
    day_str = f"เช้าวัน{THAI_DAYS[now.weekday()]}"
    a = analysis or {}

    # left column: index tables, each followed by "why it moved" bullets
    notes = {"us": a.get("us_notes"), "eu": a.get("europe_notes"),
             "asia": a.get("asia_notes"), "em": a.get("emerging_notes")}
    left = []
    for title, color, items in REGIONS:
        region_key = items[0].region
        head = (f'<div class="sec-head">'
                f'<span class="sec-dot" style="background:{color}"></span>'
                f'<span class="sec-title">{title}</span></div>')
        if region_key == "us":
            body = _us_cards(items, quotes)
        else:
            body = '<div class="rows">' + "".join(_row_html(i, quotes) for i in items) + "</div>"
        block = f'<div class="card">{head}{body}{_bullets(notes.get(region_key))}</div>'
        left.append(block)

    # overview
    ov = a.get("overview") or []
    overview_html = "<br>".join(f"• {_e(x)}" for x in ov) or "—"

    # right column
    sectors = a.get("sectors") or []
    sec_cards = ""
    for s in sectors:
        sec_cards += (f'<div class="sector"><div class="sh">'
                      f'<span class="nm2">{_e(s.get("name",""))}</span>'
                      f'<span class="badge {_e(s.get("tone","ne"))}">{_e(s.get("badge",""))}</span>'
                      f'</div><div class="sb">{_e(s.get("text",""))}</div></div>')
    sec_block = (f'<div class="card"><div class="sec-head">'
                 f'<span class="sec-dot" style="background:#22c55e"></span>'
                 f'<span class="sec-title">เซกเตอร์จับตา</span></div>{sec_cards}</div>'
                 ) if sec_cards else ""

    events = a.get("events") or []
    tl = ""
    for ev in events:
        star = '<span class="star">★</span> ' if ev.get("star") else ""
        tl += (f'<div class="tl"><div class="when">{star}{_e(ev.get("when",""))}</div>'
               f'<div class="what">{_e(ev.get("text",""))}</div></div>')
    ev_block = (f'<div class="card"><div class="sec-head">'
                f'<span class="sec-dot" style="background:#fbbf24"></span>'
                f'<span class="sec-title">จับตาวันนี้ &amp; สัปดาห์นี้</span></div>{tl}</div>'
                ) if tl else ""

    hls = a.get("headlines_th") or []
    hl_block = ""
    if hls:
        items = "".join(f'<div class="bullet" style="margin-top:4px">{_e(h)}</div>' for h in hls)
        hl_block = (f'<div class="card"><div class="sec-head">'
                    f'<span class="sec-dot" style="background:#38bdf8"></span>'
                    f'<span class="sec-title">ข่าวเด่น</span></div>{items}</div>')

    extras_rows = "".join(_row_html(i, quotes) for i in EXTRAS)
    extras_block = (f'<div class="card"><div class="sec-head">'
                    f'<span class="sec-dot" style="background:#f59e0b"></span>'
                    f'<span class="sec-title">ค่าเงิน · โภคภัณฑ์ · คริปโต</span></div>'
                    f'<div class="rows">{extras_rows}</div></div>')

    # single-stock news card
    stock_notes = {s.get("ticker", ""): s.get("note", "") for s in (a.get("stocks") or [])}
    stk_items = ""
    for idx in STOCKS:
        q = quotes.get(idx.symbol)
        if q is None:
            continue
        cls = "up" if q.change_pct > 0 else "down" if q.change_pct < 0 else "flat"
        arrow = "▲" if q.change_pct > 0 else "▼" if q.change_pct < 0 else "▪"
        note = stock_notes.get(idx.cc, "")
        note_html = f'<div class="stk-note">{_e(note)}</div>' if note else ""
        stk_items += (f'<div class="stk"><div class="stk-top">'
                      f'<span class="stk-nm">{_e(idx.name)} <span class="cc">{idx.cc}</span></span>'
                      f'<span class="pct {cls}">{arrow} {q.change_pct:+.2f}%</span></div>'
                      f'{note_html}</div>')
    stk_block = (f'<div class="card"><div class="sec-head">'
                 f'<span class="sec-dot" style="background:#a78bfa"></span>'
                 f'<span class="sec-title">หุ้นเด่นรายตัว</span></div>{stk_items}</div>'
                 ) if stk_items else ""

    right = sec_block + stk_block + ev_block + extras_block + hl_block

    return _TEMPLATE.format(
        date=date_str, day=day_str, overview=overview_html,
        left="".join(left), right=right)


def render_png(html_text: str, out_path: str) -> None:
    """Render the HTML to a PNG using headless Chromium."""
    from playwright.sync_api import sync_playwright

    launch_kwargs = {}
    chromium_path = os.environ.get("CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1500, "height": 1000},
                                device_scale_factor=2)
        page.set_content(html_text, wait_until="networkidle")
        page.wait_for_timeout(400)
        page.screenshot(path=out_path, full_page=True)
        browser.close()


def send_email_with_image(image_path: str) -> None:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not password:
        raise SystemExit("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must both be set.")

    date_str = datetime.now(ICT).strftime("%-d/%-m/%Y")
    msg = MIMEMultipart()
    msg["Subject"] = Header(f"📊 MARKET BRIEF — {date_str}", "utf-8")
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText("สรุปตลาดหุ้นโลกประจำวัน (ดูภาพแนบ) — ส่งต่อเข้ากลุ่ม LINE ทีมได้เลย",
                        "plain", "utf-8"))
    with open(image_path, "rb") as f:
        img = MIMEImage(f.read(), _subtype="png")
    img.add_header("Content-Disposition", "attachment", filename="market-brief.png")
    msg.attach(img)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [to], msg.as_string())
    print(f"Sent brief to {to} successfully.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily MARKET BRIEF image")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the PNG but do not email it")
    args = parser.parse_args()

    symbols = [idx.symbol for _, _, items in REGIONS for idx in items]
    symbols += [idx.symbol for idx in EXTRAS]
    symbols += [idx.symbol for idx in STOCKS]
    quotes = {s: fetch_quote(s) for s in symbols}
    missing = [s for s, q in quotes.items() if q is None]
    if missing:
        print(f"WARNING: no data for {len(missing)} symbol(s): {', '.join(missing)}",
              file=sys.stderr)

    headlines = fetch_headlines(limit=8)
    analysis = fetch_analysis(quotes, headlines)

    html_text = build_html(quotes, analysis)
    render_png(html_text, OUT_PNG)
    print(f"Rendered {OUT_PNG}")

    if args.dry_run:
        return 0

    send_email_with_image(OUT_PNG)
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:1500px; font-family:"Loma","Noto Sans Thai",sans-serif; background:#080c18; color:#e6eaf4; }}
  .page {{ width:1500px; padding:38px 44px 30px; background:radial-gradient(1200px 600px at 80% -10%, #14224a 0%, #080c18 55%); }}
  .top {{ display:flex; justify-content:space-between; align-items:flex-end; padding-bottom:22px; border-bottom:1px solid #1e2942; }}
  .brand {{ font-size:40px; font-weight:700; letter-spacing:2px; }}
  .brand .dot {{ color:#3b82f6; }}
  .brand-sub {{ font-size:19px; color:#8b95ac; margin-top:6px; letter-spacing:.5px; }}
  .date {{ font-size:27px; font-weight:700; text-align:right; }}
  .date .day {{ font-size:18px; color:#8b95ac; font-weight:400; margin-top:4px; }}
  .overview {{ margin-top:20px; background:#0f1730; border:1px solid #212b47; border-radius:14px; padding:16px 22px; }}
  .overview .oh {{ font-size:19px; font-weight:700; color:#cfd6e6; margin-bottom:8px; }}
  .overview .ob {{ font-size:18px; color:#aab3c8; line-height:1.55; }}
  .cols {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-top:22px; }}
  .col {{ display:flex; flex-direction:column; gap:20px; }}
  .sec-head {{ display:flex; align-items:center; gap:11px; margin-bottom:13px; }}
  .sec-dot {{ width:11px; height:11px; border-radius:50%; }}
  .sec-title {{ font-size:23px; font-weight:700; color:#eef1f8; }}
  .usgrid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px; }}
  .uscard {{ background:#101a34; border:1px solid #26406e; border-radius:14px; padding:16px 18px; }}
  .uscard .un {{ font-size:16px; font-weight:600; color:#9aa4ba; }}
  .uscard .uv {{ font-size:30px; font-weight:700; color:#f2f5fb; margin:7px 0 5px; }}
  .uscard .up2 {{ font-size:17px; font-weight:700; }}
  .rows {{ display:flex; flex-direction:column; }}
  .row {{ display:grid; grid-template-columns:1fr auto auto; align-items:center; gap:18px; padding:12px 4px; border-bottom:1px solid #161f36; }}
  .row:last-child {{ border-bottom:none; }}
  .nm {{ font-size:20px; font-weight:600; color:#dbe1ee; }}
  .cc {{ font-size:14px; color:#7c86a0; font-weight:600; margin-left:5px; letter-spacing:.5px; }}
  .val {{ font-size:21px; font-weight:700; color:#f2f5fb; text-align:right; min-width:110px; }}
  .pct {{ font-size:19px; font-weight:700; text-align:right; min-width:96px; }}
  .up {{ color:#22c55e; }} .down {{ color:#f6465d; }} .flat {{ color:#8b95ac; }}
  .bullet {{ font-size:16.5px; color:#9aa4ba; line-height:1.5; margin-top:11px; padding-left:20px; position:relative; }}
  .bullet::before {{ content:"▸"; position:absolute; left:0; color:#3b82f6; }}
  .card {{ background:#0f1730; border:1px solid #212b47; border-radius:16px; padding:20px 24px; }}
  .thai {{ background:linear-gradient(135deg,#12213f,#0f1730); border:1px solid #26406e; border-radius:14px; padding:15px 20px; margin-top:12px; }}
  .thai .th-h {{ font-size:17px; font-weight:700; color:#7fb0ff; margin-bottom:7px; }}
  .thai .th-h .tag {{ background:#1d3357; color:#9cc2ff; font-size:13px; padding:2px 9px; border-radius:20px; margin-right:8px; }}
  .thai .th-b {{ font-size:16.5px; color:#aab3c8; line-height:1.55; }}
  .sector {{ background:#101a34; border:1px solid #212b47; border-radius:14px; padding:16px 20px; margin-bottom:14px; }}
  .sector:last-child {{ margin-bottom:0; }}
  .sector .sh {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; }}
  .sector .sh .nm2 {{ font-size:19px; font-weight:700; color:#eef1f8; }}
  .badge {{ font-size:13px; padding:2px 11px; border-radius:20px; font-weight:600; }}
  .badge.hi {{ background:#3a1d2a; color:#f78ba3; }}
  .badge.re {{ background:#173a2a; color:#6ee7a8; }}
  .badge.ne {{ background:#2a3350; color:#a9b6d6; }}
  .sector .sb {{ font-size:16.5px; color:#a3adc4; line-height:1.55; }}
  .tl {{ display:flex; gap:16px; padding:13px 0; border-bottom:1px solid #161f36; }}
  .tl:last-child {{ border-bottom:none; }}
  .tl .when {{ font-size:16px; font-weight:700; color:#7fb0ff; min-width:104px; }}
  .tl .what {{ font-size:16.5px; color:#a3adc4; line-height:1.5; }}
  .star {{ color:#fbbf24; }}
  .stk {{ padding:11px 2px; border-bottom:1px solid #161f36; }}
  .stk:last-child {{ border-bottom:none; }}
  .stk-top {{ display:flex; justify-content:space-between; align-items:center; }}
  .stk-nm {{ font-size:19px; font-weight:600; color:#dbe1ee; }}
  .stk-note {{ font-size:15.5px; color:#8b95ac; line-height:1.45; margin-top:5px; }}
  .foot {{ margin-top:26px; padding-top:16px; border-top:1px solid #1e2942; display:flex; justify-content:space-between; align-items:flex-start; }}
  .foot .src {{ font-size:15px; color:#7c86a0; line-height:1.5; max-width:1150px; }}
  .foot .logo {{ font-size:22px; font-weight:700; letter-spacing:2px; color:#2e3c5e; }}
  .disc {{ font-size:14px; color:#5c667e; margin-top:8px; line-height:1.5; }}
</style></head><body><div class="page">
  <div class="top">
    <div><div class="brand"><span class="dot">📊</span> MARKET BRIEF</div>
      <div class="brand-sub">สรุปตลาดหุ้นโลก · ประจำวัน</div></div>
    <div class="date">{date}<div class="day">{day}</div></div>
  </div>
  <div class="overview"><div class="oh">🌐 ภาพรวมตลาด</div><div class="ob">{overview}</div></div>
  <div class="cols">
    <div class="col">{left}</div>
    <div class="col">{right}</div>
  </div>
  <div class="foot">
    <div class="src">แหล่งอ้างอิง: Yahoo Finance · CNBC · MarketWatch · Reuters
      <div class="disc">หมายเหตุ: ตัวเลขเป็นราคาปิดล่าสุด/ระหว่างวันของแต่ละตลาด (ต่างโซนเวลา) · จัดทำเพื่อให้ข้อมูลเท่านั้น มิใช่คำแนะนำการลงทุน</div>
    </div>
    <div class="logo">MARKET BRIEF</div>
  </div>
</div></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
