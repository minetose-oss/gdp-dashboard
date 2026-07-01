#!/usr/bin/env python3
"""Daily global stock-market brief, delivered by email (or LINE).

Fetches the latest close and daily % change for the major index of each
target market from the free Yahoo Finance chart API (no API key required),
adds top market headlines from free RSS feeds (CNBC, MarketWatch), builds a
Thai-language summary, and delivers it — by email via Gmail SMTP by default,
or to LINE via the Messaging API when LINE credentials are supplied instead.

Environment variables
---------------------
Email delivery (default):
    GMAIL_USER         : the Gmail address that sends the mail
    GMAIL_APP_PASSWORD : a Google "app password" (16 chars) for that account
    MAIL_TO            : recipient address (defaults to GMAIL_USER)

LINE delivery (used only if GMAIL_USER is not set):
    LINE_CHANNEL_ACCESS_TOKEN : long-lived channel access token
    LINE_TO                   : destination id (group / room / user)

Usage
-----
    python scripts/market_news_daily.py            # fetch + deliver
    python scripts/market_news_daily.py --dry-run  # fetch + print only
"""

from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.header import Header

import requests

# --- configuration ---------------------------------------------------------

# Bangkok time is used for the "as of" stamp so the header matches the
# morning the team reads it.
ICT = timezone(timedelta(hours=7))

YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# A browser-like UA keeps Yahoo from rejecting the request.
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-brief/1.0)"}

# Free RSS feeds for market headlines. Tried in order; any feed that fails or
# returns nothing is skipped, so a stale/wrong URL never breaks the brief.
NEWS_FEEDS: list[str] = [
    "https://www.cnbc.com/id/20910258/device/rss/rss.html",   # CNBC Markets
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Top News
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",  # MarketWatch
]

# How many headlines to include in the brief.
HEADLINE_LIMIT = 6


@dataclass
class Market:
    """One line in the brief: a flag/name and its Yahoo Finance symbol."""

    region: str      # grouping header, e.g. "สหรัฐฯ"
    flag: str        # emoji shown next to the index
    name: str        # index display name
    symbol: str      # Yahoo Finance ticker


# Order here is the order shown in the message.
MARKETS: list[Market] = [
    Market("สหรัฐฯ", "🇺🇸", "S&P 500", "^GSPC"),
    Market("สหรัฐฯ", "🇺🇸", "Nasdaq", "^IXIC"),
    Market("สหรัฐฯ", "🇺🇸", "Dow Jones", "^DJI"),
    Market("ยุโรป", "🇩🇪", "DAX (เยอรมนี)", "^GDAXI"),
    Market("ยุโรป", "🇫🇷", "CAC 40 (ฝรั่งเศส)", "^FCHI"),
    Market("ยุโรป", "🇪🇸", "IBEX 35 (สเปน)", "^IBEX"),
    Market("ยุโรป", "🇮🇹", "FTSE MIB (อิตาลี)", "FTSEMIB.MI"),
    Market("เอเชีย", "🇯🇵", "Nikkei 225", "^N225"),
    Market("เอเชีย", "🇰🇷", "KOSPI (เกาหลีใต้)", "^KS11"),
    Market("เอเชีย", "🇨🇳", "Shanghai Composite", "000001.SS"),
    Market("เอเชีย", "🇭🇰", "Hang Seng (ฮ่องกง)", "^HSI"),
    Market("เอเชีย", "🇹🇼", "TAIEX (ไต้หวัน)", "^TWII"),
    Market("เอเชีย", "🇮🇳", "Sensex (อินเดีย)", "^BSESN"),
    Market("เอเชีย", "🇮🇳", "Nifty 50 (อินเดีย)", "^NSEI"),
    Market("ตลาดเกิดใหม่", "🇧🇷", "Ibovespa (บราซิล)", "^BVSP"),
    Market("ตลาดเกิดใหม่", "🇮🇩", "JCI (อินโดนีเซีย)", "^JKSE"),
]


@dataclass
class Quote:
    close: float
    change_pct: float


def fetch_quote(symbol: str) -> Quote | None:
    """Return the latest close and daily % change, or None on any failure.

    Percentage change is computed from the last two daily closes in the
    series, which is more reliable than trusting a single meta field.
    """
    try:
        resp = requests.get(
            YF_CHART_URL.format(symbol=symbol),
            params={"interval": "1d", "range": "5d"},
            headers=HTTP_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if not closes:
            return None
        latest = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else result["meta"].get("chartPreviousClose")
        if prev in (None, 0):
            return Quote(close=latest, change_pct=0.0)
        return Quote(close=latest, change_pct=(latest - prev) / prev * 100.0)
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        return None


def _parse_feed_titles(xml_text: str) -> list[str]:
    """Extract item/entry titles from an RSS or Atom feed body."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    titles: list[str] = []
    # RSS 2.0: <item><title>; Atom: <entry><title> (namespaced).
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]  # strip any namespace
        if tag not in ("item", "entry"):
            continue
        for child in item:
            if child.tag.rsplit("}", 1)[-1] == "title" and child.text:
                titles.append(child.text.strip())
                break
    return titles


def fetch_headlines(feeds: list[str] = NEWS_FEEDS, limit: int = HEADLINE_LIMIT) -> list[str]:
    """Collect up to `limit` unique headlines across the configured feeds.

    Feeds are fetched in order and their headlines interleaved, so the brief
    stays diverse even when one source dominates. Any failing feed is skipped.
    """
    per_feed: list[list[str]] = []
    for url in feeds:
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=15)
            resp.raise_for_status()
            per_feed.append(_parse_feed_titles(resp.text))
        except requests.RequestException:
            continue

    headlines: list[str] = []
    seen: set[str] = set()
    # Round-robin across feeds for source diversity.
    for i in range(max((len(f) for f in per_feed), default=0)):
        for feed_titles in per_feed:
            if i >= len(feed_titles):
                continue
            title = feed_titles[i]
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            headlines.append(title)
            if len(headlines) >= limit:
                return headlines
    return headlines


def format_number(value: float) -> str:
    """Thousands-separated, two decimals, no trailing noise."""
    return f"{value:,.2f}"


def build_message(quotes: dict[str, Quote | None], headlines: list[str] | None = None) -> str:
    """Assemble the LINE-friendly Thai brief from fetched quotes and headlines."""
    today = datetime.now(ICT).strftime("%-d/%-m/%Y")
    lines: list[str] = [f"📊 สรุปตลาดหุ้นโลก — เช้าวันที่ {today}", ""]

    current_region: str | None = None
    for market in MARKETS:
        if market.region != current_region:
            current_region = market.region
            lines.append(f"▸ {market.region}")
        quote = quotes.get(market.symbol)
        if quote is None:
            lines.append(f"  {market.flag} {market.name}: ไม่มีข้อมูล")
            continue
        arrow = "🔺" if quote.change_pct > 0 else "🔻" if quote.change_pct < 0 else "▪️"
        sign = "+" if quote.change_pct > 0 else ""
        lines.append(
            f"  {market.flag} {market.name}: {format_number(quote.close)} "
            f"{arrow} {sign}{quote.change_pct:.2f}%"
        )

    if headlines:
        lines.append("")
        lines.append("📰 ข่าวเด่น (แหล่งฟรี)")
        lines += [f"  • {h}" for h in headlines]

    lines += [
        "",
        "หมายเหตุ: ตัวเลขคือราคาปิดล่าสุดของแต่ละตลาด (ต่างโซนเวลาปิดคนละช่วง)",
        "ที่มา: Yahoo Finance, CNBC, MarketWatch",
    ]
    return "\n".join(lines)


def send_email(message: str) -> None:
    """Send the brief as a plain-text email via Gmail SMTP."""
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO") or user
    if not user or not password:
        raise SystemExit(
            "ERROR: GMAIL_USER and GMAIL_APP_PASSWORD must both be set. "
            "Use --dry-run to preview without sending."
        )

    today = datetime.now(ICT).strftime("%-d/%-m/%Y")
    mime = MIMEText(message, "plain", "utf-8")
    mime["Subject"] = Header(f"📊 สรุปตลาดหุ้นโลก — {today}", "utf-8")
    mime["From"] = user
    mime["To"] = to

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [to], mime.as_string())
    print(f"Sent email to {to} successfully.")


def send_to_line(message: str) -> None:
    """Push the message to the configured LINE destination."""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    to = os.environ.get("LINE_TO")
    if not token or not to:
        raise SystemExit(
            "ERROR: LINE_CHANNEL_ACCESS_TOKEN and LINE_TO must both be set. "
            "Use --dry-run to preview without sending."
        )
    resp = requests.post(
        LINE_PUSH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"to": to, "messages": [{"type": "text", "text": message}]},
        timeout=20,
    )
    if resp.status_code != 200:
        raise SystemExit(f"LINE push failed ({resp.status_code}): {resp.text}")
    print("Sent to LINE successfully.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily global market brief")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and print the message without delivering it",
    )
    args = parser.parse_args()

    quotes = {market.symbol: fetch_quote(market.symbol) for market in MARKETS}
    missing = [s for s, q in quotes.items() if q is None]
    if missing:
        # Warn but continue — the message degrades gracefully per market.
        print(f"WARNING: no data for {len(missing)} symbol(s): {', '.join(missing)}",
              file=sys.stderr)

    headlines = fetch_headlines()
    if not headlines:
        print("WARNING: no headlines fetched from any feed", file=sys.stderr)

    message = build_message(quotes, headlines)
    print(message)

    if args.dry_run:
        return 0

    # Deliver by email when Gmail credentials are present; otherwise LINE.
    if os.environ.get("GMAIL_USER"):
        send_email(message)
    else:
        send_to_line(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
