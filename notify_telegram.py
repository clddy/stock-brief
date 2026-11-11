# -*- coding: utf-8 -*-
"""stock-brief 텔레그램 알림 — 매일 아침 워치리스트 브리핑을 보낸다.

crawler.py + enrich.py 가 만든 data.json 을 읽어
급등락 종목 + 주요 뉴스(한국어)를 한 통의 메시지로 요약해 텔레그램에 전송.
기존 C:\\ohai\\telegram-notify 모듈(봇·chat_id 세팅 완료)을 재사용한다.
"""
import html
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "crawler.log"

# 기존 텔레그램 알림 모듈 재사용
sys.path.insert(0, r"C:\ohai\telegram-notify")
try:
    from notify import send
except Exception:
    send = None

MOVER_MIN = 2.0      # 알림에 포함할 최소 등락률(%)
MAX_MOVERS = 8       # 급등락 최대 표시 종목
MAX_NEWS = 6         # 뉴스 최대 표시 개수
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] tg: {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def esc(s):
    return html.escape(str(s), quote=False)


def build_message(data):
    all_t = [t for g in data["groups"] for t in g["tickers"]]
    up = sum(1 for x in all_t if x["chg"] > 0)
    down = sum(1 for x in all_t if x["chg"] < 0)
    asof = all_t[0]["asof"] if all_t else ""

    now = datetime.now()
    lines = [
        f"📈 <b>미국주식 브리핑</b> · {now.month}/{now.day}({WEEKDAYS[now.weekday()]})",
        f"{asof} 장마감 기준 · 🔴{up} 🔵{down}",
        "",
    ]

    # 급등락 종목 (등락률 절댓값 순)
    movers = sorted(
        (x for x in all_t if abs(x["chg"]) >= MOVER_MIN),
        key=lambda x: -abs(x["chg"]),
    )[:MAX_MOVERS]
    if movers:
        lines.append("〈오늘의 급등락〉")
        for x in movers:
            mark = "🔴" if x["chg"] > 0 else "🔵"
            sign = "+" if x["chg"] > 0 else ""
            lines.append(f"{mark} <b>{esc(x['t'])}</b> {esc(x['kr'])} {sign}{x['chg']:.1f}%")
        lines.append("")

    # MFI 신호 (signals.py 결과): ① 30↓ 3일+ 지속 ② 오늘 30↑ 회복 — 각각 악재 없음 / 제외(악재)
    mf = (data.get("signals") or {}).get("mfi")
    if mf:
        if mf.get("error"):
            lines.append("〈MFI 신호〉 계산 실패 (crawler.log 확인)")
            lines.append("")
        else:
            def block(head, grp, info):
                lines.append(head)
                if grp["clean"]:
                    for r in grp["clean"]:
                        new = "🆕 " if r.get("new") else ""
                        tail = " · ETF" if r.get("etf") else (" · 뉴스 적음" if r.get("thin") else "")
                        lines.append(f"{new}<b>{esc(r['t'])}</b> {esc(r['kr'])} · MFI {r['mfi']:.1f} · "
                                     f"{info(r)} · 1개월 {r['chg1m']:+.1f}%{tail}")
                        seen_k = set()
                        for k, ttl, _ in r["minor"]:
                            if k in seen_k:
                                continue
                            seen_k.add(k)
                            lines.append(f"   참고({esc(k)}): {esc(ttl[:70])}")
                else:
                    lines.append("해당 종목 없음")
                if grp["flagged"]:
                    ex = " · ".join(f"{r['t']}({r['severe'][0][0]})" for r in grp["flagged"])
                    lines.append(f"<i>제외(악재): {esc(ex)}</i>")
                lines.append("")
            block(f"〈MFI {mf['low_max']}↓ {mf['min_days']}일 이상 지속 · 악재 없음〉", mf["low"],
                  lambda r: f"{r['days']}일째{'+' if r.get('capped') else ''}")
            block(f"〈MFI {mf['low_max']}↑ 오늘 회복 · 악재 없음〉", mf["up"],
                  lambda r: f"직전 {r['low_days']}일 과매도(최저 {r['low_min']:g})")
            lines.append("<i>※ 뉴스 제목 키워드 기준 1차 필터 — 헤드라인 직접 확인 권장</i>")
            lines.append("")

    # 주요 뉴스 — 태그(테마/섹터)별로 골고루 라운드로빈, 한국어 우선
    buckets = []  # [(tag, [items...]), ...]
    for th in data.get("theme_news", []):
        if th.get("items"):
            buckets.append((th["name"], list(th["items"])))
    for gname, items in data.get("group_news", {}).items():
        if items:
            buckets.append((gname, list(items)))

    news, seen = [], set()
    rnd = 0
    while len(news) < MAX_NEWS and any(len(b[1]) > rnd for b in buckets):
        for tag, items in buckets:
            if rnd < len(items):
                it = items[rnd]
                t = it.get("title", "")
                if t and t not in seen:
                    seen.add(t)
                    news.append((tag, it))
                    if len(news) >= MAX_NEWS:
                        break
        rnd += 1

    if news:
        lines.append("〈주요 뉴스〉")
        for tag, it in news:
            title = it.get("ko") or it.get("title", "")
            url = it.get("url", "")
            head = f"• [{esc(tag)}] <a href=\"{esc(url)}\">{esc(title)}</a>"
            lines.append(head)
            if it.get("why"):
                lines.append(f"   → {esc(it['why'])}")
        lines.append("")

    engine = data.get("enrich_engine", "")
    if engine == "translate":
        lines.append("<i>※ 뉴스는 자동 번역(요약 없음) 상태. Gemini 무료 키를 넣으면 요약이 붙습니다.</i>")

    msg = "\n".join(lines)
    return msg[:4000]  # 텔레그램 4096자 제한 여유


def main():
    if send is None:
        log("notify 모듈 로드 실패 (C:\\ohai\\telegram-notify 확인)")
        sys.exit(1)
    data = json.loads((BASE / "data.json").read_text(encoding="utf-8"))
    msg = build_message(data)
    ok = send(msg, parse_mode="HTML")
    log("sent" if ok else "send failed")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
