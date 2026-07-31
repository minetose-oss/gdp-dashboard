#!/usr/bin/env python3
"""Daily "MARKET BRIEF" — a one-page dark-themed market report, emailed daily.

Pipeline:
  1. Fetch index / FX / commodity / single-stock levels and daily % change
     (Yahoo Finance, via market_news_daily).
  2. Ask Claude to search the web for the day's real market-moving news.
  3. Ask Claude to write the Thai-language brief from that grounded context:
     a headline, per-region "why it moved" bullets, sectors, stocks, events.
  4. Render the layout with headless Chromium to BOTH a PNG (share into chat)
     and a PDF (vector text — stays sharp when zoomed on a phone or tablet).
  5. Email both as attachments.

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
    python scripts/market_brief.py --dry-run  # build files only, don't email
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
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header

from market_news_daily import fetch_quote, fetch_headlines, ICT

PAGE_WIDTH = 1500  # CSS px; the layout and both outputs are sized to this
OUT_PNG = os.path.join(os.path.dirname(__file__), "market_brief.png")
OUT_PDF = os.path.join(os.path.dirname(__file__), "market_brief.pdf")

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
        "headline": {"type": "string"},
        "subhead": {"type": "string"},
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
    "required": ["headline", "subhead", "us_notes", "europe_notes", "asia_notes",
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
        "- headline: พาดหัวข่าวสรุปเรื่องเด่นที่สุดของวัน สั้นกระชับแบบหัวข่าวหนังสือพิมพ์ "
        "ไม่เกิน ~60 ตัวอักษร ใส่ตัวเลขสำคัญได้ (เช่น 'หุ้นเทคสหรัฐฯ นำตลาดโลกฟื้น Nasdaq +2.07%')\n"
        "- subhead: ขยายความพาดหัว 1-2 ประโยค บอกสาเหตุหลักและสิ่งที่สวนทาง\n"
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


def _chip(idx: Idx, quotes: dict) -> str:
    """Compact heat chip for the at-a-glance strip: name + signed %, tinted by sign."""
    q = quotes.get(idx.symbol)
    if q is None:
        return (f'<div class="chip" style="background:rgba(139,149,172,.08);'
                f'border-color:rgba(139,149,172,.2)">'
                f'<div class="cn">{_e(idx.name)}<span class="cc">{idx.cc}</span></div>'
                f'<div class="cp flat">—</div></div>')
    p = q.change_pct
    cls = "up" if p > 0 else "down" if p < 0 else "flat"
    arrow = "▲" if p > 0 else "▼" if p < 0 else "▪"
    tint = ("rgba(20,184,166,.13)" if p > 0 else
            "rgba(246,70,93,.13)" if p < 0 else "rgba(139,149,172,.10)")
    bd = ("rgba(20,184,166,.34)" if p > 0 else
          "rgba(246,70,93,.34)" if p < 0 else "rgba(139,149,172,.25)")
    return (f'<div class="chip" style="background:{tint};border-color:{bd}">'
            f'<div class="cn">{_e(idx.name)}<span class="cc">{idx.cc}</span></div>'
            f'<div class="cp {cls}">{arrow} {p:+.2f}%</div></div>')


def build_html(quotes: dict, analysis: dict | None) -> str:
    now = datetime.now(ICT)
    date_str = (f"{THAI_DAYS[now.weekday()]} {now.day} "
                f"{THAI_MONTHS[now.month]} {now.year}")
    a = analysis or {}

    # --- lead: headline + tally of markets closing up ---
    live = [q for idx in (i for _, _, its in REGIONS for i in its)
            if (q := quotes.get(idx.symbol)) is not None]
    ups, tot = sum(1 for q in live if q.change_pct > 0), len(live)
    headline = _e(a.get("headline") or "สรุปตลาดหุ้นโลกประจำวัน")
    subhead = _e(a.get("subhead") or "")
    sub_html = f'<div class="sub">{subhead}</div>' if subhead else ""
    tally = (f'<div class="tally"><b>{ups}/{tot}</b>'
             f'<span>ตลาดหลักปิดบวก</span></div>') if tot else ""

    # --- at-a-glance heat strip, grouped by region ---
    strip = ""
    for title, _color, items in REGIONS:
        cells = "".join(_chip(i, quotes) for i in items)
        strip += (f'<div class="hgroup"><div class="hglbl">{title}</div>'
                  f'<div class="hgrid">{cells}</div></div>')

    # --- the story: why each region moved ---
    notes = [("สหรัฐฯ", "#3b82f6", a.get("us_notes")),
             ("ยุโรป", "#7c3aed", a.get("europe_notes")),
             ("เอเชีย", "#0891b2", a.get("asia_notes")),
             ("ตลาดเกิดใหม่", "#ea580c", a.get("emerging_notes"))]
    story = ""
    for title, color, items in notes:
        lis = "".join(f"<li>{_e(x)}</li>" for x in (items or []) if x)
        if not lis:
            continue
        story += (f'<div class="sblk"><div class="sh">'
                  f'<span class="sdot" style="background:{color}"></span>{title}</div>'
                  f'<ul class="why">{lis}</ul></div>')

    # --- supporting table: every index, compact ---
    tbl = "".join(_row_html(i, quotes) for _, _, its in REGIONS for i in its)

    # --- right rail ---
    stock_notes = {s.get("ticker", ""): s.get("note", "") for s in (a.get("stocks") or [])}
    stk = ""
    for idx in STOCKS:
        q = quotes.get(idx.symbol)
        if q is None:
            continue
        cls = "up" if q.change_pct > 0 else "down" if q.change_pct < 0 else "flat"
        arrow = "▲" if q.change_pct > 0 else "▼" if q.change_pct < 0 else "▪"
        note = stock_notes.get(idx.cc, "")
        nh = f'<div class="snote">{_e(note)}</div>' if note else ""
        stk += (f'<div class="s"><div class="stop">'
                f'<span class="sn">{_e(idx.name)}<span class="cc">{idx.cc}</span></span>'
                f'<span class="sp {cls}">{arrow} {q.change_pct:+.2f}%</span></div>{nh}</div>')
    stk_block = (f'<div class="card"><div class="sectitle">หุ้นเด่นรายตัว</div>'
                 f'{stk}</div>') if stk else ""

    sec = ""
    for s in (a.get("sectors") or []):
        sec += (f'<div class="s"><div class="stop">'
                f'<span class="sn">{_e(s.get("name",""))}</span>'
                f'<span class="badge {_e(s.get("tone","ne"))}">{_e(s.get("badge",""))}</span>'
                f'</div><div class="snote">{_e(s.get("text",""))}</div></div>')
    sec_block = (f'<div class="card"><div class="sectitle">เซกเตอร์จับตา</div>'
                 f'{sec}</div>') if sec else ""

    extras = "".join(_row_html(i, quotes) for i in EXTRAS)
    ex_block = (f'<div class="card"><div class="sectitle">'
                f'ค่าเงิน · โภคภัณฑ์ · คริปโต</div>{extras}</div>')

    evs = ""
    for ev in (a.get("events") or []):
        star = '<span class="star">★</span> ' if ev.get("star") else ""
        evs += (f'<div class="ev"><span class="evw">{star}{_e(ev.get("when",""))}</span>'
                f'<span class="evt">{_e(ev.get("text",""))}</span></div>')
    ev_block = (f'<div class="card"><div class="sectitle">จับตาสัปดาห์นี้</div>'
                f'{evs}</div>') if evs else ""

    hls = "".join(f'<li>{_e(h)}</li>' for h in (a.get("headlines_th") or []))
    hl_block = (f'<div class="card"><div class="sectitle">ข่าวเด่น</div>'
                f'<ul class="why">{hls}</ul></div>') if hls else ""

    return _TEMPLATE.format(
        date=date_str, headline=headline, sub=sub_html, tally=tally,
        strip=strip, story=story, table=tbl,
        rail=stk_block + sec_block + ex_block + ev_block + hl_block)


def render(html_text: str, png_path: str, pdf_path: str) -> None:
    """Render the brief once, to a PNG (inline preview) and a PDF (vector text).

    The PDF keeps text as embedded vector glyphs, so it stays sharp at any zoom —
    which is how the brief is actually read, on a phone or tablet.
    """
    from playwright.sync_api import sync_playwright

    launch_kwargs = {}
    chromium_path = os.environ.get("CHROMIUM_PATH")
    if chromium_path:
        launch_kwargs["executable_path"] = chromium_path

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": PAGE_WIDTH, "height": 1000},
                                device_scale_factor=3)
        page.set_content(html_text, wait_until="networkidle")
        page.wait_for_timeout(400)
        page.screenshot(path=png_path, full_page=True)
        # One tall page, sized to the content, so the PDF never splits mid-section.
        height = page.evaluate("document.body.scrollHeight")
        page.pdf(path=pdf_path, width=f"{PAGE_WIDTH}px", height=f"{height}px",
                 print_background=True,
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        browser.close()


def send_email(png_path: str, pdf_path: str) -> None:
    """Email the brief with both attachments: PDF to read, PNG to share."""
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not password:
        raise SystemExit("ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must both be set.")

    date_str = datetime.now(ICT).strftime("%-d/%-m/%Y")
    msg = MIMEMultipart()
    msg["Subject"] = Header(f"\U0001F4CA MARKET BRIEF \u2014 {date_str}", "utf-8")
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(
        "สรุปตลาดหุ้นโลกประจำวัน\n\n"
        "\u2022 market-brief.pdf \u2014 เปิดอ่าน/ซูมบนมือถือหรือ iPad ตัวหนังสือคมทุกระดับ\n"
        "\u2022 market-brief.png \u2014 รูปภาพ สำหรับส่งต่อเข้ากลุ่ม LINE ทีม\n",
        "plain", "utf-8"))

    with open(pdf_path, "rb") as f:
        pdf = MIMEApplication(f.read(), _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="market-brief.pdf")
    msg.attach(pdf)

    with open(png_path, "rb") as f:
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
                        help="build the files but do not email them")
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
    render(html_text, OUT_PNG, OUT_PDF)
    print(f"Rendered {OUT_PNG} and {OUT_PDF}")

    if args.dry_run:
        return 0

    send_email(OUT_PNG, OUT_PDF)
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="th"><head><meta charset="UTF-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ width:1500px; font-family:"Loma","Noto Sans Thai",sans-serif;
    background:#080c18; color:#eef1f8; }}
  .page {{ padding:40px 48px 30px;
    background:radial-gradient(1000px 460px at 82% -14%, #152449 0%, #080c18 60%); }}
  .top {{ display:flex; justify-content:space-between; align-items:baseline;
    padding-bottom:16px; border-bottom:1px solid #212b47; }}
  .brand {{ font-size:29px; font-weight:700; letter-spacing:2.5px; }}
  .brand span {{ color:#3b82f6; }}
  .dt {{ font-size:18px; color:#aab3c8; font-weight:600; }}
  /* lead */
  .lead {{ padding:26px 0 22px; border-bottom:1px solid #212b47; }}
  .kicker {{ font-size:14px; letter-spacing:2px; color:#7fb0ff; font-weight:700;
    margin-bottom:11px; }}
  h1 {{ font-size:41px; line-height:1.24; font-weight:700; letter-spacing:-.3px;
    max-width:1200px; }}
  .sub {{ font-size:19px; color:#aab3c8; line-height:1.55; margin-top:13px;
    max-width:1200px; }}
  .tally {{ display:inline-flex; align-items:baseline; gap:9px; margin-top:16px;
    background:rgba(20,184,166,.12); border:1px solid rgba(20,184,166,.3);
    border-radius:999px; padding:7px 17px; }}
  .tally b {{ font-size:22px; color:#14b8a6; font-weight:700; }}
  .tally span {{ font-size:15px; color:#aab3c8; }}
  /* heat strip */
  .strip {{ display:grid; grid-template-columns:repeat(4,1fr); gap:22px;
    padding:22px 0 24px; border-bottom:1px solid #212b47; align-items:start; }}
  .hglbl {{ font-size:13px; letter-spacing:1.5px; text-transform:uppercase;
    color:#7c86a0; font-weight:700; margin-bottom:10px; }}
  .hgrid {{ display:flex; flex-direction:column; gap:7px; }}
  .chip {{ display:flex; justify-content:space-between; align-items:center;
    border:1px solid; border-radius:9px; padding:8px 12px; }}
  .cn {{ font-size:16.5px; font-weight:600; color:#eef1f8; }}
  .cp {{ font-size:16px; font-weight:700; font-variant-numeric:tabular-nums; }}
  /* body */
  .body {{ display:grid; grid-template-columns:1.22fr 1fr; gap:34px; padding-top:24px; }}
  .sectitle {{ font-size:14px; font-weight:700; color:#7c86a0; letter-spacing:1.7px;
    text-transform:uppercase; margin-bottom:14px; }}
  .sblk {{ margin-bottom:19px; }}
  .sh {{ display:flex; align-items:center; gap:9px; font-size:19px; font-weight:700;
    margin-bottom:7px; }}
  .sdot {{ width:9px; height:9px; border-radius:50%; }}
  .why {{ list-style:none; }}
  .why li {{ font-size:17px; color:#aab3c8; line-height:1.55; padding:5px 0 5px 21px;
    position:relative; }}
  .why li::before {{ content:""; position:absolute; left:3px; top:14px; width:7px;
    height:7px; border-radius:50%; background:#31456f; }}
  /* rows (index table + extras) */
  .row {{ display:grid; grid-template-columns:1fr auto 96px; gap:12px;
    align-items:baseline; padding:8px 0; border-bottom:1px solid #1b2440; }}
  .row:last-child {{ border-bottom:none; }}
  .nm {{ font-size:17px; font-weight:600; color:#dbe1ee; }}
  .cc {{ font-size:11.5px; color:#7c86a0; font-weight:600; margin-left:7px;
    letter-spacing:.5px; }}
  .val {{ font-size:16.5px; color:#7c86a0; text-align:right;
    font-variant-numeric:tabular-nums; }}
  .pct {{ font-size:16.5px; font-weight:700; text-align:right;
    font-variant-numeric:tabular-nums; }}
  .up {{ color:#14b8a6; }} .down {{ color:#f6465d; }} .flat {{ color:#8b95ac; }}
  .tbl {{ display:grid; grid-template-columns:repeat(2,1fr); gap:0 26px; }}
  /* right rail */
  .card {{ background:#0f1730; border:1px solid #212b47; border-radius:15px;
    padding:19px 22px; margin-bottom:18px; }}
  .s {{ padding:10px 0; border-bottom:1px solid #1b2440; }}
  .s:last-child {{ border-bottom:none; }}
  .stop {{ display:flex; justify-content:space-between; align-items:baseline; gap:10px; }}
  .sn {{ font-size:18px; font-weight:600; }}
  .sp {{ font-size:17px; font-weight:700; font-variant-numeric:tabular-nums; }}
  .snote {{ font-size:15px; color:#7c86a0; margin-top:3px; line-height:1.42; }}
  .badge {{ font-size:12.5px; padding:2px 10px; border-radius:20px; font-weight:600;
    white-space:nowrap; }}
  .badge.hi {{ background:#3a1d2a; color:#f78ba3; }}
  .badge.re {{ background:#123b33; color:#5fd6bd; }}
  .badge.ne {{ background:#2a3350; color:#a9b6d6; }}
  .ev {{ display:flex; gap:14px; padding:10px 0; border-bottom:1px solid #1b2440; }}
  .ev:last-child {{ border-bottom:none; }}
  .evw {{ font-size:15px; font-weight:700; color:#7fb0ff; min-width:104px; }}
  .evt {{ font-size:15.5px; color:#aab3c8; line-height:1.42; }}
  .star {{ color:#fbbf24; }}
  .foot {{ margin-top:22px; padding-top:14px; border-top:1px solid #212b47;
    display:flex; justify-content:space-between; font-size:13px; color:#5c667e; }}
</style></head><body><div class="page">
  <div class="top">
    <div class="brand"><span>&#9670;</span> MARKET BRIEF</div>
    <div class="dt">{date}</div>
  </div>

  <div class="lead">
    <div class="kicker">สรุปตลาดหุ้นโลกวันนี้</div>
    <h1>{headline}</h1>
    {sub}
    {tally}
  </div>

  <div class="strip">{strip}</div>

  <div class="body">
    <div>
      <div class="sectitle">อะไรทำให้ตลาดขึ้น–ลง</div>
      {story}
      <div class="sectitle" style="margin-top:26px">ดัชนีทั้งหมด</div>
      <div class="tbl">{table}</div>
    </div>
    <div>{rail}</div>
  </div>

  <div class="foot">
    <span>Yahoo Finance &middot; CNBC &middot; MarketWatch &middot; Reuters</span>
    <span>ราคาปิดล่าสุดของแต่ละตลาด (ต่างโซนเวลา) &middot; มิใช่คำแนะนำการลงทุน</span>
  </div>
</div></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
