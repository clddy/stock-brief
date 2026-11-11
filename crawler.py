# -*- coding: utf-8 -*-
"""stock-brief crawler — 미국주식 워치리스트 시세+뉴스 수집기.

yfinance로 시세(1년 일봉)를 배치 수집하고,
급등락 종목·테마별 뉴스(Google News RSS)를 모아 data.js로 내보낸다.
대시보드(index.html)는 file:// 에서도 열리도록 JSON 대신 data.js를 읽는다.
"""
import concurrent.futures
import json
import re
import sys
import time
import traceback
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf

BASE = Path(__file__).resolve().parent
LOG = BASE / "crawler.log"

NEWS_PER_ITEM = 5       # 종목당 뉴스 개수(전 종목 수집)
TICKER_NEWS_H = 168     # 종목 뉴스 최대 나이(시간) = 7일
NEWS_WORKERS = 6        # 종목 뉴스 동시 요청 수
NAMES = BASE / "names.json"   # 티커 → 영문 회사명 캐시(뉴스 검색어용)
# 검색어로 쓰기 나쁜 공식명은 직접 지정
NAME_OVERRIDE = {"SPCX": "SpaceX", "BRK-B": "Berkshire Hathaway", "GOOGL": "Alphabet",
                 "META": "Meta Platforms", "EL": "Estee Lauder", "GE": "GE Aerospace",
                 "MOG-A": "Moog", "TSM": "TSMC", "ASML": "ASML"}
SECTOR_NEWS_N = 6       # 섹터/테마당 뉴스 개수
NEWS_MAX_AGE_H = 48     # 종목 뉴스 최대 나이(시간)
SECTOR_MAX_AGE_H = 72   # 섹터/테마 뉴스 최대 나이(시간)


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


HORIZONS = {"1m": 30, "3m": 91, "6m": 182, "1y": 365}   # 기간 키 → 달력일수


MFI_N = 14


def mfi14(frame, idx):
    """MFI(14) 시계열 → spark 인덱스에 맞춘 리스트(앞 14개는 None, 이후 0~100 정수; 마지막 값만 소수 1자리).

    전형가 TP=(H+L+C)/3, 자금흐름=TP×거래량. TP가 전일보다 오르면 양(+), 내리면 음(-) 흐름.
    MFI = 100 - 100/(1 + 14일 양흐름합/14일 음흐름합). 음흐름이 0이면 100.
    """
    try:
        f = frame.loc[idx, ["High", "Low", "Close", "Volume"]]
    except Exception:
        return None
    tp = (f["High"] + f["Low"] + f["Close"]) / 3
    flow = tp * f["Volume"]
    diff = tp.diff()
    pos = flow.where(diff > 0, 0.0).where(diff.notna())
    neg = flow.where(diff < 0, 0.0).where(diff.notna())
    ps = pos.rolling(MFI_N).sum()
    ns = neg.rolling(MFI_N).sum()
    out = []
    for p_, n_ in zip(ps.tolist(), ns.tolist()):
        if p_ != p_ or n_ != n_ or (p_ == 0 and n_ == 0):   # NaN 또는 거래 없음
            out.append(None)
        elif n_ == 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + p_ / n_))
    if not out or out[-1] is None:
        return None
    last = round(out[-1], 1)
    out = [None if v is None else int(round(v)) for v in out]
    out[-1] = last
    return out


def fetch_quotes(tickers):
    """1년 일봉 배치 다운로드 → {ticker: {price, chg, spark(1년), i1m/i3m/i6m/i1y, chg1m/3m/6m/1y, ...}}

    spark 는 1년 전체를 담고, 각 기간은 i<key> 인덱스부터 잘라 쓴다(대시보드 기간 토글).
    날짜는 용량 절감을 위해 시작일 d0 + 일수 오프셋 dd 로 저장한다.
    상장 얼마 안 된 종목은 히스토리가 기간의 90% 미만이면 해당 기간 등락률을 None 으로 둔다.
    """
    df = yf.download(
        tickers, period="1y", interval="1d",
        auto_adjust=True, group_by="ticker", threads=True, progress=False,
    )
    out = {}
    for t in tickers:
        try:
            closes = df[t]["Close"].dropna()
            if len(closes) < 2:
                continue
            idx = closes.index
            last = float(closes.iloc[-1])
            prev = float(closes.iloc[-2])
            span_days = (idx[-1] - idx[0]).days
            rec = {
                "price": round(last, 2),
                "chg": round((last - prev) / prev * 100, 2),
            }
            for key, days in HORIZONS.items():
                # 기간 시작 = 마지막 거래일 기준 days일 전 이후의 첫 거래일
                cutoff = idx[-1] - timedelta(days=days)
                i0 = next((i for i, d in enumerate(idx) if d >= cutoff), 0)
                base = float(closes.iloc[i0])
                rec["i" + key] = i0
                rec["chg" + key] = (round((last - base) / base * 100, 2)
                                    if base and span_days >= days * 0.9 else None)
            mfi = mfi14(df[t], idx)
            if mfi is not None:
                rec["mfi"] = mfi[-1]
                rec["mfis"] = mfi
            d0 = idx[0].date()
            rec["spark"] = [round(float(c), 2) for c in closes.tolist()]
            rec["d0"] = str(d0)
            rec["dd"] = [(d.date() - d0).days for d in idx]
            rec["asof"] = str(idx[-1].date())
            out[t] = rec
            try:
                vols = df[t]["Volume"].dropna()
                out[t]["vol"] = int(vols.mean()) if len(vols) else None
            except Exception:
                out[t]["vol"] = None
        except Exception as e:
            log(f"  quote fail {t}: {e}")
    return out


def fetch_mcaps(tickers):
    """fast_info 로 시가총액 병렬 수집 → {ticker: mcap or None}."""
    def one(t):
        try:
            fi = yf.Ticker(t).fast_info
            try:
                mc = fi["market_cap"]
            except Exception:
                mc = getattr(fi, "market_cap", None)
            return t, (round(float(mc)) if mc else None)
        except Exception:
            return t, None
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for t, mc in ex.map(one, tickers):
            out[t] = mc
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


_SUFFIX = re.compile(
    r"(,?\s+(inc\.?|incorporated|corporation|corp\.?|company|companies|co\.?|plc|pbc|"
    r"ltd\.?|limited|n\.v\.|s\.a\.|new|class [a-c]|\(the\)|\(t.*))+\s*$", re.I)


def clean_name(n):
    n = re.sub(r"^the\s+", "", (n or "").strip(), flags=re.I)
    prev = None
    while prev != n:                       # "Estee Lauder Companies, Inc. (T" 처럼 겹친 접미사
        prev, n = n, _SUFFIX.sub("", n).strip().rstrip(",")
    return n


def load_names(tickers):
    """names.json 캐시 → {ticker: 검색용 회사명}. 없는 티커만 yfinance info 로 채운다."""
    try:
        raw = json.loads(NAMES.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    miss = [t for t in tickers if not raw.get(t)]
    if miss:
        def one(t):
            try:
                i = yf.Ticker(t).info
                return t, i.get("shortName") or i.get("longName") or ""
            except Exception:
                return t, ""
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for t, n in ex.map(one, miss):
                if n:
                    raw[t] = n
        NAMES.write_text(json.dumps(raw, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        log(f"names: +{sum(1 for t in miss if raw.get(t))} fetched")
    return {t: NAME_OVERRIDE.get(t) or clean_name(raw.get(t, "")) or t for t in tickers}


def _norm(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


# 시세 조회 페이지·종목 프로필 등 뉴스가 아닌 결과 + 기관 보유 변동(13F) 자동 기사
_JUNK = re.compile(r"(stock price, news, quote|quote price and forecast|stock quote|price, news, quote|"
                   r"stock price, quote|quote, news & history|symbol__|stock price today|share price & news|"
                   r"historical prices|stock chart|"
                   r"(shares?|stock|stake|position|holdings?) (purchased|sold|acquired|bought|trimmed|raised|lowered|cut|boosted|reduced) by|"
                   r"(acquires|buys|sells|purchases) (new )?(shares|stake|position) (of|in)|"
                   r"has \$[\d.,]+ (million|billion|thousand) (stock )?(position|stake|holdings))", re.I)


def ticker_news_one(t, name):
    """회사명으로 최근 7일 뉴스 검색 → 시세페이지·중복 제거 → 제목에 회사명/티커 있는 것만, 최신순 NEWS_PER_ITEM 개."""
    items = []
    for attempt in range(2):
        try:
            items = fetch_news(f'"{name}" stock when:7d', limit=15, max_age_h=TICKER_NEWS_H)
            break
        except Exception:
            time.sleep(1.5)
    seen, clean = set(), []
    for it in items:
        k = _norm(it["title"])
        if _JUNK.search(it["title"]) or k in seen:
            continue
        seen.add(k)
        clean.append(it)
    key = _norm(name.split()[0]) if name else ""
    alts = [re.escape(key)] if key else []
    if len(t) >= 3:
        alts.append(re.escape(t.lower()))
    pat = re.compile(r"\b(" + "|".join(alts) + r")\b") if alts else None
    picked = [it for it in clean if pat and pat.search(_norm(it["title"]))]
    picked.sort(key=lambda it: it.get("time") or "", reverse=True)
    return t, picked[:NEWS_PER_ITEM]


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

    mcaps = fetch_mcaps(list(quotes.keys()))
    for t, mc in mcaps.items():
        if t in quotes:
            quotes[t]["mcap"] = mc
    log(f"mcaps ok: {sum(1 for v in mcaps.values() if v)}/{len(mcaps)}")

    # 종목별 뉴스 — 전 종목, 영문 회사명으로 최근 7일
    names = load_names(list(quotes.keys()))
    ticker_news = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=NEWS_WORKERS) as ex:
        for t, items in ex.map(lambda t: ticker_news_one(t, names[t]), list(quotes.keys())):
            if items:
                ticker_news[t] = items
    log(f"ticker news: {len(ticker_news)}/{len(quotes)} tickers, "
        f"{sum(len(v) for v in ticker_news.values())} items")

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
