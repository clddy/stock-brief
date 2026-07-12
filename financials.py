# -*- coding: utf-8 -*-
"""stock-brief financials — 종목별 매출구조·재무제표 수집기.

yfinance로 연간(최근 4개 회계연도) 손익계산서 + 재무상태표 + 최근 분기를
받아 financials.js (window.STOCK_FIN)로 내보낸다. 재무는 분기마다 바뀌므로
종목별 캐시를 두고 REFRESH_DAYS 보다 오래됐을 때만 다시 받는다.
"""
import json
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

BASE = Path(__file__).resolve().parent
CACHE = BASE / "fin_cache"
LOG = BASE / "crawler.log"
REFRESH_DAYS = 7
YEARS = 4
SCHEMA = 2   # 캐시 스키마 버전 — 바뀌면 강제 재수집


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] fin: {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _num(x):
    """NaN/inf/None -> None, else rounded float (백만 단위로는 프론트에서 처리)."""
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 2)
    except (TypeError, ValueError):
        return None


def _row(df, *names):
    """여러 후보 라벨 중 존재하는 첫 행을 컬럼 순서대로 리스트로."""
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return [_num(v) for v in df.loc[n].tolist()]
    return None


def _fy_label(col):
    """회계연도 라벨: 9월 결산이면 'FY24'처럼, 대부분은 연도."""
    return str(col.year)


def fetch_one(ticker):
    t = yf.Ticker(ticker)
    inc = t.income_stmt          # 연간 손익계산서 (최신이 첫 컬럼)
    bs = t.balance_sheet         # 연간 재무상태표
    qi = t.quarterly_income_stmt  # 분기 손익계산서

    if inc is None or inc.empty:
        return None

    # 최신 -> 과거 순서를 과거 -> 최신으로 뒤집고 YEARS개만
    cols = list(inc.columns)[:YEARS][::-1]
    idx = [list(inc.columns).index(c) for c in cols]

    def annual(*names):
        full = _row(inc, *names)
        if full is None:
            return [None] * len(cols)
        return [full[i] for i in idx]

    periods = [_fy_label(c) for c in cols]
    annual_data = {
        "periods": periods,
        "revenue": annual("Total Revenue", "Operating Revenue"),
        "costOfRevenue": annual("Cost Of Revenue", "Reconciled Cost Of Revenue"),
        "grossProfit": annual("Gross Profit"),
        "rnd": annual("Research And Development"),
        "sga": annual("Selling General And Administration"),
        "opex": annual("Operating Expense"),
        "operatingIncome": annual("Operating Income", "Total Operating Income As Reported"),
        "tax": annual("Tax Provision"),
        "netIncome": annual("Net Income", "Net Income Common Stockholders"),
        "eps": annual("Diluted EPS", "Basic EPS"),
    }

    # 최근 분기
    quarter = None
    if qi is not None and not qi.empty:
        qcol = qi.columns[0]
        q = qi[qcol]
        month = qcol.month
        qnum = (month - 1) // 3 + 1
        def qval(*names):
            for n in names:
                if n in q.index:
                    return _num(q[n])
            return None
        quarter = {
            "period": f"{qcol.year} Q{qnum}",
            "asof": str(qcol.date()),
            "revenue": qval("Total Revenue", "Operating Revenue"),
            "grossProfit": qval("Gross Profit"),
            "operatingIncome": qval("Operating Income", "Total Operating Income As Reported"),
            "netIncome": qval("Net Income", "Net Income Common Stockholders"),
            "eps": qval("Diluted EPS", "Basic EPS"),
        }

    # 재무상태표 (최신 컬럼)
    balance = None
    if bs is not None and not bs.empty:
        bcol = bs.columns[0]
        b = bs[bcol]
        def bval(*names):
            for n in names:
                if n in b.index:
                    return _num(b[n])
            return None
        balance = {
            "period": _fy_label(bcol),
            "asof": str(bcol.date()),
            "totalAssets": bval("Total Assets"),
            "totalLiabilities": bval(
                "Total Liabilities Net Minority Interest", "Total Liabilities"),
            "equity": bval(
                "Stockholders Equity", "Total Equity Gross Minority Interest"),
            "cash": bval(
                "Cash And Cash Equivalents",
                "Cash Cash Equivalents And Short Term Investments"),
        }

    return {
        "schema": SCHEMA,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "annual": annual_data,
        "quarter": quarter,
        "balance": balance,
    }


def is_fresh(path):
    if not path.exists():
        return False
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("schema") != SCHEMA:
            return False
        ts = datetime.fromisoformat(d["fetched_at"])
        age = (datetime.now(timezone.utc) - ts).days
        return age < REFRESH_DAYS
    except Exception:
        return False


def main():
    CACHE.mkdir(exist_ok=True)
    cfg = json.loads((BASE / "tickers.json").read_text(encoding="utf-8"))
    tickers = [it["t"] for g in cfg["groups"] for it in g["tickers"]]
    log(f"financials for {len(tickers)} tickers (refresh>{REFRESH_DAYS}d)")

    out = {}
    fetched = cached = failed = 0
    for tk in tickers:
        cpath = CACHE / f"{tk}.json"
        if is_fresh(cpath):
            out[tk] = json.loads(cpath.read_text(encoding="utf-8"))
            cached += 1
            continue
        try:
            data = fetch_one(tk)
            if data:
                cpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                out[tk] = data
                fetched += 1
            else:
                failed += 1
                if cpath.exists():  # 최신 실패 시 이전 캐시라도 사용
                    out[tk] = json.loads(cpath.read_text(encoding="utf-8"))
            time.sleep(0.3)
        except Exception as e:
            failed += 1
            log(f"  fail {tk}: {e}")
            if cpath.exists():
                out[tk] = json.loads(cpath.read_text(encoding="utf-8"))

    js = "window.STOCK_FIN = " + json.dumps(out, ensure_ascii=False) + ";"
    (BASE / "financials.js").write_text(js, encoding="utf-8")

    # 수작업 부문별 매출(segments.json) → segments.js 로 동기화
    seg_path = BASE / "segments.json"
    if seg_path.exists():
        try:
            seg = json.loads(seg_path.read_text(encoding="utf-8"))
            seg.pop("_note", None)
            (BASE / "segments.js").write_text(
                "window.STOCK_SEG = " + json.dumps(seg, ensure_ascii=False) + ";",
                encoding="utf-8")
            log(f"segments: {len(seg)} tickers")
        except Exception as e:
            log(f"segments fail: {e}")

    log(f"done: {fetched} fetched, {cached} cached, {failed} failed, {len(out)} total")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
