#!/usr/bin/env python3
"""Daily global stock-market brief, formatted for LINE.

Fetches the latest close and daily % change for the major index of each
target market from the free Yahoo Finance chart API (no API key required),
builds a Thai-language summary, and pushes it to LINE via the Messaging API.

Environment variables
---------------------
LINE_CHANNEL_ACCESS_TOKEN : long-lived channel access token of the LINE
                            Official Account that sends the message.
LINE_TO                   : destination id (group id, room id, or user id)
                            the push message is sent to.

Usage
-----
    python scripts/market_news_daily.py            # fetch + send to LINE
    python scripts/market_news_daily.py --dry-run  # fetch + print only
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

import requests

# --- configuration ---------------------------------------------------------

# Bangkok time is used for the "as of" stamp so the header matches the
# morning the team reads it.
ICT = timezone(timedelta(hours=7))

YF_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# A browser-like UA keeps Yahoo from rejecting the request.
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-brief/1.0)"}


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


def format_number(value: float) -> str:
    """Thousands-separated, two decimals, no trailing noise."""
    return f"{value:,.2f}"


def build_message(quotes: dict[str, Quote | None]) -> str:
    """Assemble the LINE-friendly Thai brief from fetched quotes."""
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

    lines += [
        "",
        "หมายเหตุ: ตัวเลขคือราคาปิดล่าสุดของแต่ละตลาด (ต่างโซนเวลาปิดคนละช่วง)",
        "ที่มา: Yahoo Finance",
    ]
    return "\n".join(lines)


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
    parser = argparse.ArgumentParser(description="Daily global market brief for LINE")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and print the message without sending it to LINE",
    )
    args = parser.parse_args()

    quotes = {market.symbol: fetch_quote(market.symbol) for market in MARKETS}
    missing = [s for s, q in quotes.items() if q is None]
    if missing:
        # Warn but continue — the message degrades gracefully per market.
        print(f"WARNING: no data for {len(missing)} symbol(s): {', '.join(missing)}",
              file=sys.stderr)

    message = build_message(quotes)
    print(message)

    if args.dry_run:
        return 0

    send_to_line(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
