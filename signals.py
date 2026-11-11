# -*- coding: utf-8 -*-
"""stock-brief signals — MFI(14) 기준선 신호 + 뉴스 악재 확인.

파이프라인: crawler → enrich → **signals** → financials → notify_telegram → publish
data.json 을 읽어 data["signals"]["mfi"] 를 넣고 data.js / data.json 을 다시 쓴다.

판정 (2026-11-11 사용자 재확정 기준)
  ① 지속 과매도: MFI(14) ≤ 30 인 날이 오늘까지 3거래일 이상 연속
  ② 오늘 회복: 어제 MFI ≤ 30 → 오늘 30 초과
  두 그룹 모두 최근 7일 종목 뉴스 '제목'(영문 원문)을 규칙으로 분류
      · 중대 악재(SEVERE) — 하나라도 있으면 '제외(악재)' 로 분리
      · 참고(MINOR) — 목표가 하향·법무법인 소송 홍보·'왜 떨어졌나' 기사: 제외하지 않고 함께 표시
      · ETF 는 개별 악재 대신 상위 보유종목의 중대 악재를 '참고'로
  판정에는 crawler 가 남긴 최근 15일 정확값(mfi_r, 소수1) 을 쓴다(정수 시계열은 30 경계에서 틀릴 수 있음).

이 단계가 실패해도 아침 브리핑은 계속 나가야 하므로, 예외는 로그 + data 에 error 표시 후 exit 0.
사용: python signals.py           (파이프라인: 계산·반영)
      python signals.py --dry      (확인용: 출력만, 파일 안 씀)
"""
import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "crawler.log"
STATE = BASE / "signals_state.json"
LOW = 30              # 과매도 기준선: MFI(14) 이 값 이하
MIN_DAYS = 3          # 지속 과매도: 기준선 이하 연속 거래일 수
RECOVER_WINDOW = 1    # 회복: 오늘(최근 1거래일) 기준선 위로 올라온 종목만 — 2026-11-11 사용자 지정
THIN_NEWS = 1         # 뉴스가 이 개수 이하면 '근거 약함' 표시
KEEP_DAYS = 14

SEVERE = [
    ("실적·가이던스 악화", r"\b(miss(es|ed)?|double[- ]miss|shortfall|falls? short|"
                          r"(cuts?|lowers?|slashes|reduces|withdraws?|pulls?) (its |full[- ]year |annual )?(guidance|outlook|forecast)|"
                          r"(weak|soft|disappointing) (guidance|outlook|forecast|results|quarter)|profit warning|warns)\b"),
    ("투자의견 하향", r"\b(downgrades?|downgraded|cut to (sell|underperform|underweight|neutral|hold|market perform))\b"),
    ("증자·희석", r"\b((stock|share|equity|secondary|public) offering|dilut(ion|ive)|share sale|"
                  r"convertible (notes?|debt|senior notes?)|at[- ]the[- ]market|prices? .{0,30}offering)\b"),
    ("규제·수사", r"\b(sec (charges|probe|investigation|subpoena)|doj|department of justice|ftc sues|subpoena|"
                  r"indict(ed|ment)|fraud|antitrust (suit|lawsuit|probe)|raided|sanction(s|ed))\b"),
    ("경영진 이탈·스캔들", r"\b((ceo|cfo|chief executive|chief financial officer|founder) (resigns?|steps? down|departs?|exits?|ousted|fired|leaving|to leave)|"
                          r"scandal|affair|misconduct|harassment)\b"),
    ("존속 리스크", r"\b(bankruptcy|chapter 11|going concern|delist(ed|ing)?|default(s|ed)? on|restate(s|ment)?)\b"),
    ("사고·기타", r"\b(recall(s|ed)?|short[- ]seller|short report|trading halt|halted|data breach|hack(ed)?|"
                  r"export (ban|curbs?|restrictions?)|(loses?|lost|cancels?|cancell?ed) (contract|deal|customer|order))\b"),
]
MINOR = [
    ("목표가 하향", r"\b(price targets?|pt)\b.{0,25}\b(cut|cuts|trim(s|med)?|lower(s|ed)?|slash(es|ed)?|reduce[sd]?)\b|"
                    r"\b(cut|cuts|trim(s|med)?|lower(s|ed)?|slash(es|ed)?)\b.{0,25}\bprice targets?\b"),
    ("법무법인 소송 홍보", r"\b(investigation initiated|investigates? (the )?(officers|directors)|shareholder (alert|rights)|"
                          r"investor (alert|notice)|class action|securities (fraud )?(lawsuit|litigation)|"
                          r"law firm|rosen|pomerantz|levi & korsinsky|kahn swick)\b"),
    ("하락 이유 기사", r"\bwhy\b.{0,60}\b(fell|falls|falling|down|plung\w*|sink\w*|slid\w*|tumbl\w*|crash\w*|drop\w*)\b|"
                      r"\b(plunge[sd]?|tumble[sd]?|sinks?|crash(es|ed)?)\b"),
]
_SEV = [(k, re.compile(p, re.I)) for k, p in SEVERE]
_MIN = [(k, re.compile(p, re.I)) for k, p in MINOR]


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] signals: {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def classify(title):
    """제목 → (중대 악재 카테고리 리스트, 참고 카테고리 리스트). 중대가 있으면 참고는 생략."""
    sev = [k for k, r in _SEV if r.search(title)]
    mnr = [k for k, r in _MIN if r.search(title)] if not sev else []
    return sev, mnr


def news_flags(x, tn):
    """종목 뉴스 → (중대 악재, 참고). ETF 는 상위 보유종목의 중대 악재를 참고로."""
    sev, mnr = [], []
    for n in tn.get(x["t"], []):
        s, mi = classify(n.get("title", ""))
        shown = n.get("ko") or n.get("title", "")
        sev += [[k, shown, n.get("time", "")] for k in s]
        mnr += [[k, shown, n.get("time", "")] for k in mi]
    if x.get("etf"):
        for sym, _nm, pct in (x["etf"].get("top") or []):
            for n in tn.get(sym, []):
                s, _ = classify(n.get("title", ""))
                for k in s:
                    mnr.append([f"보유종목 {sym}({pct:g}%) {k}", n.get("ko") or n.get("title", ""), n.get("time", "")])
    return sev, mnr


def mfi_state(x):
    """최근 정확값(mfi_r)으로 (기준선 이하 연속일수, 회복 정보 or None). 데이터 없으면 None."""
    r = x.get("mfi_r") or []
    if not r or r[-1] is None:
        return None
    run = 0
    for v in reversed(r):
        if v is not None and v <= LOW:
            run += 1
        else:
            break
    cross = None
    n = len(r)
    if r[-1] > LOW:
        for d in range(n - 1, max(0, n - RECOVER_WINDOW) - 1, -1):
            if d >= 1 and r[d - 1] is not None and r[d - 1] <= LOW and all(v is not None and v > LOW for v in r[d:]):
                prior = 0
                for v in reversed(r[:d]):
                    if v is not None and v <= LOW:
                        prior += 1
                    else:
                        break
                cross = {"days_ago": n - 1 - d, "low_days": prior, "low_min": min(r[d - prior:d])}
                break
    return run, cross


def mfi_screen(data):
    """→ {"low": {clean, flagged}, "up": {clean, flagged}}
    항목 공통: {t, kr, group, mfi, chg, chg1m, news_n, thin, etf, severe, minor}
      low 추가: days(연속일수, mfi_r 길이가 상한), capped(상한 도달 여부)
      up  추가: days_ago(0=오늘), low_days(회복 전 연속일수), low_min(그 기간 최저)"""
    tn = data.get("ticker_news", {}) or {}
    out = {"low": {"clean": [], "flagged": []}, "up": {"clean": [], "flagged": []}}
    for g in data.get("groups", []):
        for x in g["tickers"]:
            st = mfi_state(x)
            if not st:
                continue
            run, cross = st
            if run >= MIN_DAYS:
                kind, extra = "low", {"days": run, "capped": run >= len(x.get("mfi_r") or [])}
            elif cross:
                kind, extra = "up", cross
            else:
                continue
            sev, mnr = news_flags(x, tn)
            news_n = len(tn.get(x["t"], []))
            rec = {"t": x["t"], "kr": x["kr"], "group": g["name"], "mfi": x.get("mfi"), "chg": x.get("chg"),
                   "chg1m": x.get("chg1m"), "news_n": news_n, "thin": (news_n <= THIN_NEWS) and not x.get("etf"),
                   "etf": bool(x.get("etf")), "severe": sev, "minor": mnr, **extra}
            out[kind]["flagged" if sev else "clean"].append(rec)
    for k in ("clean", "flagged"):
        out["low"][k].sort(key=lambda r: (-r["days"], r["mfi"]))
        out["up"][k].sort(key=lambda r: (r["days_ago"], -r["mfi"]))
    return out


def mark_new(sc, today):
    """low: 직전 날짜 결과에 없던 종목 new=True (3일째 막 진입). up: days_ago==0 이면 new=True."""
    try:
        hist = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    prev_dates = sorted(d for d in hist if d < today)
    prev = hist[prev_dates[-1]] if prev_dates else None
    prev_low = set(prev.get("low", []) if isinstance(prev, dict) else (prev or []))
    for r in sc["low"]["clean"] + sc["low"]["flagged"]:
        r["new"] = prev is not None and r["t"] not in prev_low
    for r in sc["up"]["clean"] + sc["up"]["flagged"]:
        r["new"] = r["days_ago"] == 0
    hist[today] = {"low": [r["t"] for r in sc["low"]["clean"] + sc["low"]["flagged"]],
                   "up": [r["t"] for r in sc["up"]["clean"] + sc["up"]["flagged"]]}
    keep = sorted(hist)[-KEEP_DAYS:]
    STATE.write_text(json.dumps({d: hist[d] for d in keep}, ensure_ascii=False, indent=1), encoding="utf-8")


def write_data(data):
    js = "window.STOCK_DATA = " + json.dumps(data, ensure_ascii=False) + ";"
    (BASE / "data.js").write_text(js, encoding="utf-8")
    (BASE / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    data = json.loads((BASE / "data.json").read_text(encoding="utf-8"))
    sig = data.setdefault("signals", {})
    sig.pop("oversold", None)                       # 구 기준(MFI≤25 단발) 결과 제거
    try:
        sc = mfi_screen(data)
        all_t = [x for g in data["groups"] for x in g["tickers"]]
        today = (all_t[0].get("asof") if all_t else None) or datetime.now().strftime("%Y-%m-%d")
        mark_new(sc, today)
        sig["mfi"] = {"asof": today, "low_max": LOW, "min_days": MIN_DAYS, "recover_window": RECOVER_WINDOW, **sc}
        log(f"MFI≤{LOW} {MIN_DAYS}일+: clean {[r['t'] for r in sc['low']['clean']]} flagged {[r['t'] for r in sc['low']['flagged']]} | "
            f"회복: clean {[r['t'] for r in sc['up']['clean']]} flagged {[r['t'] for r in sc['up']['flagged']]}")
    except Exception:
        log("FAIL (브리핑은 계속):\n" + traceback.format_exc())
        sig["mfi"] = {"error": True}
    write_data(data)


def dry():
    d = json.loads((BASE / "data.json").read_text(encoding="utf-8"))
    sc = mfi_screen(d)
    for kind, title in (("low", f"① MFI ≤ {LOW} {MIN_DAYS}거래일 이상 지속"), ("up", f"② {LOW} 위로 회복 (최근 {RECOVER_WINDOW}거래일)")):
        print(f"\n===== {title}: 악재 없음 {len(sc[kind]['clean'])} · 악재 있음 {len(sc[kind]['flagged'])} =====")
        for tag, rows in (("✅", sc[kind]["clean"]), ("⛔", sc[kind]["flagged"])):
            for x in rows:
                info = (f"{x['days']}일째{'+' if x['capped'] else ''}" if kind == "low"
                        else f"{'오늘' if x['days_ago'] == 0 else str(x['days_ago']) + '일 전'} 회복(그전 {x['low_days']}일 과매도, 최저 {x['low_min']})")
                print(f"{tag} {x['t']} {x['kr']} MFI {x['mfi']} · {info} · 1개월 {x['chg1m']}%{' · ETF' if x['etf'] else ''}")
                for k, t, _ in x["severe"]:
                    print(f"     [악재:{k}] {t[:80]}")
                for k, t, _ in x["minor"][:3]:
                    print(f"     [참고:{k}] {t[:80]}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dry":
        dry()
    else:
        main()
