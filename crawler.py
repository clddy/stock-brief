# -*- coding: utf-8 -*-
"""stock-brief crawler — 미국주식 워치리스트 시세+뉴스 수집기.

yfinance로 시세(1개월 일봉)를 배치 수집하고,
급등락 종목·테마별 뉴스(Google News RSS)를 모아 data.js로 내보낸다.
대시보드(index.html)는 file:// 에서도 열리도록 JSON 대신 data.js를 읽는다.
"""
import json
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf

BASE = Path(__file__).resolve().parent
LOG = BASE / "crawler.log"

MOVER_THRESHOLD = 2.5   # |등락률| 이 이 값 이상이면 개별 뉴스 수집
MOVER_NEWS_MAX = 20     # 개별 뉴스 수집 최대 종목 수
NEWS_PER_ITEM = 3       # 종목당 뉴스 개수
SECTOR_NEWS_N = 6       # 섹터/테마당 뉴스 개수
NEWS_MAX_AGE_H = 48     # 종목 뉴스 최대 나이(시간)
SECTOR_MAX_AGE_H = 72   # 섹터/테마 뉴스 최대 나이(시간)


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_quotes(tickers):
    """1개월 일봉 배치 다운로드 → {ticker: {price, chg, spark, ...}}"""
    df = yf.download(
        tickers, period="1mo", interval="1d",
        auto_adjust=True, group_by="ticker", threads=True, progress=False,
    )
    out = {}
    for t in tickers:
        try:
            closes = df[t]["Close"].dropna()
            if len(closes) < 2:
                continue
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            first = float(closes.iloc[0])
            out[t] = {
                "price": round(last, 2),
                "chg": round((last - prev) / prev * 100, 2),
                "chg1m": round((last - first) / first * 100, 2),
                "spark": [round(float(c), 2) for c in closes.tolist()],
                "dates": [str(d.date()) for d in closes.index],
                "asof": str(closes.index[-1].date()),
            }
        except Exception as e:
            log(f"  quote fail {t}: {e}")
    return out


def _parse_rss(xml_text, limit, max_age_h):
    """Google News RSS → [{title, url, source, time}]"""
    items = []
    root = ET.fromstring(xml_text)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src = item.findtext("{https://news.google.com/rss}source") or ""
        if not src:
            el = item.find("source")
            src = el.text.strip() if el is not None and el.text else ""
        try:
            dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
        if dt and dt < cutoff:
            continue
        # 구글뉴스 제목 말미의 " - 매체명" 제거
        title = re.sub(r"\s+-\s+[^-]+$", "", title)
        items.append({
            "title": title, "url": link, "source": src,
            "time": dt.isoformat() if dt else "",
        })
        if len(items) >= limit:
            break
    return items


def fetch_news(query, limit=NEWS_PER_ITEM, max_age_h=NEWS_MAX_AGE_H):
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    r = requests.get(url, params=params, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return _parse_rss(r.text, limit, max_age_h)


def main():
    log("=== crawl start ===")
    cfg = json.loads((BASE / "tickers.json").read_text(encoding="utf-8"))

    meta = {}   # ticker -> {kr, group}
    for g in cfg["groups"]:
        for item in g["tickers"]:
            meta[item["t"]] = {"kr": item["kr"], "group": g["name"]}
    tickers = list(meta.keys())
    log(f"tickers: {len(tickers)}")

    quotes = fetch_quotes(tickers)
    missing = [t for t in tickers if t not in quotes]
    if missing:
        log(f"retrying {len(missing)}: {missing}")
        quotes.update(fetch_quotes(missing))
    log(f"quotes ok: {len(quotes)}/{len(tickers)}")

    # 급등락 종목 개별 뉴스
    movers = sorted(
        (t for t, q in quotes.items() if abs(q["chg"]) >= MOVER_THRESHOLD),
        key=lambda t: -abs(quotes[t]["chg"]),
    )[:MOVER_NEWS_MAX]
    ticker_news = {}
    for t in movers:
        try:
            items = fetch_news(f'"{t}" stock', limit=NEWS_PER_ITEM)
            if items:
                ticker_news[t] = items
        except Exception as e:
            log(f"  news fail {t}: {e}")
    log(f"mover news: {len(ticker_news)} tickers")

    # 섹터(그룹) 뉴스 — 주가에 영향 줄 만한 섹터 단위 뉴스·발표
    group_news = {}
    for g in cfg["groups"]:
        q = g.get("news_query")
        if not q:
            continue
        try:
            items = fetch_news(q, limit=SECTOR_NEWS_N, max_age_h=SECTOR_MAX_AGE_H)
            if items:
                group_news[g["name"]] = items
        except Exception as e:
            log(f"  group news fail {g['name']}: {e}")
    log(f"group news: {len(group_news)} groups")

    # 테마 뉴스
    theme_news = []
    for th in cfg.get("theme_news", []):
        try:
            items = fetch_news(th["query"], limit=SECTOR_NEWS_N,
                               max_age_h=SECTOR_MAX_AGE_H)
            theme_news.append({"name": th["name"], "items": items})
        except Exception as e:
            log(f"  theme news fail {th['name']}: {e}")
    log(f"theme news: {len(theme_news)} themes")

    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "groups": [
            {
                "name": g["name"],
                "tickers": [
                    {"t": it["t"], "kr": it["kr"], **quotes[it["t"]]}
                    for it in g["tickers"] if it["t"] in quotes
                ],
            }
            for g in cfg["groups"]
        ],
        "ticker_news": ticker_news,
        "group_news": group_news,
        "theme_news": theme_news,
    }

    js = "window.STOCK_DATA = " + json.dumps(data, ensure_ascii=False) + ";"
    (BASE / "data.js").write_text(js, encoding="utf-8")
    (BASE / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log("=== crawl done ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
