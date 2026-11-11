# -*- coding: utf-8 -*-
"""stock-brief signals — MFI 과매도인데 뚜렷한 악재 뉴스가 없는 종목 찾기.

파이프라인: crawler → enrich → **signals** → financials → notify_telegram → publish
data.json 을 읽어 data["signals"]["oversold"] 를 넣고 data.js / data.json 을 다시 쓴다.

판정 (2026-11-11 사용자 확정 기준)
  - 과매도: MFI(14) ≤ 25
  - 최근 7일 종목 뉴스 '제목'(영문 원문)을 규칙으로 분류
      · 중대 악재(SEVERE) — 하나라도 있으면 후보에서 제외
      · 참고(MINOR) — 목표가 하향·법무법인 소송 홍보·'왜 떨어졌나' 기사: 제외하지 않고 함께 표시
  - AI 키가 없어서 제목 키워드 기반 1차 필터다. 알림엔 헤드라인을 같이 보여줘 사람이 최종 판단.
  - 🆕 = 직전 날짜 결과에 없다가 오늘 새로 들어온 종목 (signals_state.json 에 날짜별 기록)

이 단계가 실패해도 아침 브리핑은 계속 나가야 하므로, 예외는 로그 + data 에 error 표시 후 exit 0.
사용: python signals.py          (파이프라인: 계산·반영)
      python signals.py --dry 30  (확인용: 기준만 바꿔 출력, 파일 안 씀)
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
MFI_MAX = 25          # 과매도 기준: MFI(14) 이 값 이하
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


def oversold_screen(data, mfi_max=MFI_MAX):
    """MFI ≤ mfi_max 종목을 clean(악재 없음) / flagged(악재 있음)로 나눈다.
    항목: {t, kr, group, mfi, chg, chg1m, news_n, thin, severe:[[카테고리, 제목, 시각]], minor:[...]}"""
    tn = data.get("ticker_news", {}) or {}
    clean, flagged = [], []
    for g in data.get("groups", []):
        for x in g["tickers"]:
            m = x.get("mfi")
            if m is None or m > mfi_max:
                continue
            sev, mnr = [], []
            for n in tn.get(x["t"], []):
                s, mi = classify(n.get("title", ""))
                shown = n.get("ko") or n.get("title", "")
                sev += [[k, shown, n.get("time", "")] for k in s]
                mnr += [[k, shown, n.get("time", "")] for k in mi]
            news_n = len(tn.get(x["t"], []))
            etf = bool(x.get("etf"))
            if etf:
                # ETF 는 개별 악재 개념이 없다 → 상위 보유종목(워치리스트에 있는 것)의 중대 악재 뉴스를 '참고'로만 붙임
                for sym, _nm, pct in (x["etf"].get("top") or []):
                    for n in tn.get(sym, []):
                        s, _ = classify(n.get("title", ""))
                        for k in s:
                            mnr.append([f"보유종목 {sym}({pct:g}%) {k}", n.get("ko") or n.get("title", ""), n.get("time", "")])
            rec = {"t": x["t"], "kr": x["kr"], "group": g["name"], "mfi": m, "chg": x.get("chg"),
                   "chg1m": x.get("chg1m"), "news_n": news_n, "thin": (news_n <= THIN_NEWS) and not etf,
                   "etf": etf, "severe": sev, "minor": mnr}
            (flagged if sev else clean).append(rec)
    clean.sort(key=lambda r: r["mfi"])
    flagged.sort(key=lambda r: r["mfi"])
    return {"mfi_max": mfi_max, "clean": clean, "flagged": flagged}


def mark_new(screen, today):
    """직전 날짜(오늘 제외 가장 최근) 결과에 없던 clean 종목에 new=True. 상태는 날짜별로 덮어씀."""
    try:
        hist = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    prev_dates = sorted(d for d in hist if d < today)
    prev = set(hist[prev_dates[-1]]) if prev_dates else None
    for r in screen["clean"]:
        r["new"] = prev is not None and r["t"] not in prev
    hist[today] = [r["t"] for r in screen["clean"]]
    keep = sorted(hist)[-KEEP_DAYS:]
    STATE.write_text(json.dumps({d: hist[d] for d in keep}, ensure_ascii=False, indent=1), encoding="utf-8")
    screen["first_run"] = prev is None


def write_data(data):
    js = "window.STOCK_DATA = " + json.dumps(data, ensure_ascii=False) + ";"
    (BASE / "data.js").write_text(js, encoding="utf-8")
    (BASE / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    data = json.loads((BASE / "data.json").read_text(encoding="utf-8"))
    try:
        sc = oversold_screen(data)
        all_t = [x for g in data["groups"] for x in g["tickers"]]
        today = (all_t[0].get("asof") if all_t else None) or datetime.now().strftime("%Y-%m-%d")
        mark_new(sc, today)
        sc["asof"] = today
        data.setdefault("signals", {})["oversold"] = sc
        log(f"oversold MFI≤{sc['mfi_max']}: clean {[r['t'] for r in sc['clean']]} · "
            f"flagged {[r['t'] for r in sc['flagged']]}")
    except Exception:
        log("FAIL (브리핑은 계속):\n" + traceback.format_exc())
        data.setdefault("signals", {})["oversold"] = {"error": True}
    write_data(data)


def dry(mx):
    d = json.loads((BASE / "data.json").read_text(encoding="utf-8"))
    r = oversold_screen(d, mx)
    print(f"MFI ≤ {mx}: 악재 없음 {len(r['clean'])} · 악재 있음 {len(r['flagged'])}")
    for tag, rows in (("✅ 악재 없음", r["clean"]), ("⛔ 악재 있음", r["flagged"])):
        for x in rows:
            print(f"\n{tag} {x['t']} {x['kr']} MFI {x['mfi']} · 1개월 {x['chg1m']}% · 뉴스 {x['news_n']}건")
            for k, t, _ in x["severe"]:
                print(f"    [악재:{k}] {t[:90]}")
            for k, t, _ in x["minor"]:
                print(f"    [참고:{k}] {t[:90]}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dry":
        dry(float(sys.argv[2]) if len(sys.argv) > 2 else MFI_MAX)
    else:
        main()
