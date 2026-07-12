# -*- coding: utf-8 -*-
"""stock-brief enrich — 뉴스 영어 헤드라인을 한국어로 번역 + 요약.

crawler.py 가 만든 data.json 을 읽어 모든 뉴스 제목을 모아
엔진 우선순위대로 처리한다:
  1) Gemini (config.gemini_api_key) — 무료·카드 불필요. 번역 + 1~2문장 요약
  2) Claude (config.anthropic_api_key) — 유료. 번역 + 요약
  3) 무료 구글 번역 — 제목 번역만(요약 없음), 키 불필요 폴백
결과(ko / why)를 각 뉴스 항목에 넣고 data.js / data.json 을 다시 쓴다.
"""
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
LOG = BASE / "crawler.log"
CHUNK = 25          # LLM 한 번에 처리할 헤드라인 수
CLAUDE_MODEL = "claude-opus-4-8"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] enrich: {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def collect_titles(data):
    """(유일 제목 리스트, 실제 항목 dict 리스트) — 항목에 ko/why 를 주입하기 위함."""
    items = []
    for lst in data.get("ticker_news", {}).values():
        items.extend(lst)
    for g in data.get("group_news", {}).values():
        items.extend(g)
    for th in data.get("theme_news", []):
        items.extend(th.get("items", []))
    uniq = {}
    for it in items:
        t = it.get("title", "").strip()
        if t and t not in uniq:
            uniq[t] = None
    return list(uniq.keys()), items


PROMPT_HEAD = (
    "다음은 미국 주식 관련 영어 뉴스 헤드라인 목록입니다. 각 항목에 대해:\n"
    "1) ko: 자연스러운 한국어 제목 번역\n"
    "2) why: 이 뉴스가 관련 종목/섹터 주가에 왜 중요한지 투자 초보자도 이해하도록 "
    "1~2문장으로 쉽게 요약(과장·추측 금지, 사실 위주)\n"
    "반드시 JSON 배열만 출력하세요. 형식: [{\"ko\":\"...\",\"why\":\"...\"}, ...]. "
    "입력과 같은 순서, 같은 개수로.\n\n"
)


def _parse_json_array(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip("` \n")
    return json.loads(text)


def gemini_chunk(titles, key, model):
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    body = {
        "contents": [{"parts": [{"text": PROMPT_HEAD + numbered}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.3,
            "maxOutputTokens": 8192,
        },
    }
    r = requests.post(url, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_json_array(text)


def claude_chunk(titles, key):
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=8000,
        messages=[{"role": "user", "content": PROMPT_HEAD + numbered}])
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text")
    return _parse_json_array(text)


def llm_all(titles, fn):
    """titles 를 CHUNK 단위로 fn 에 넘겨 [{ko,why}] 를 이어붙인다."""
    out = []
    for i in range(0, len(titles), CHUNK):
        part = titles[i:i + CHUNK]
        arr = fn(part)
        if len(arr) != len(part):
            raise ValueError(f"chunk count mismatch {len(arr)} vs {len(part)}")
        out.extend(arr)
    return out


def google_translate(titles):
    """무료 구글 번역 폴백 — [{ko, why:''}]. 실패 항목은 원문 유지."""
    out = []
    sess = requests.Session()
    for t in titles:
        ko = t
        try:
            r = sess.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": t},
                timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            ko = "".join(seg[0] for seg in r.json()[0])
        except Exception:
            pass
        out.append({"ko": ko, "why": ""})
    return out


def enrich(titles, cfg):
    gkey = (cfg.get("gemini_api_key") or "").strip()
    akey = (cfg.get("anthropic_api_key") or "").strip()
    model = cfg.get("gemini_model") or "gemini-2.0-flash"

    if gkey:
        try:
            arr = llm_all(titles, lambda p: gemini_chunk(p, gkey, model))
            log(f"gemini ok ({len(arr)})")
            return arr, "gemini"
        except Exception as e:
            log(f"gemini fail ({type(e).__name__}: {e}), 다음 엔진")

    if akey:
        try:
            arr = llm_all(titles, lambda p: claude_chunk(p, akey))
            log(f"claude ok ({len(arr)})")
            return arr, "claude"
        except Exception as e:
            log(f"claude fail ({type(e).__name__}: {e}), 무료 번역으로")

    arr = google_translate(titles)
    log(f"free translate ok ({len(arr)})")
    return arr, "translate"


def main():
    data = json.loads((BASE / "data.json").read_text(encoding="utf-8"))
    titles, all_items = collect_titles(data)
    if not titles:
        log("no titles")
        return
    log(f"{len(titles)} unique titles")

    cfg = {}
    cpath = BASE / "config.json"
    if cpath.exists():
        cfg = json.loads(cpath.read_text(encoding="utf-8"))

    arr, engine = enrich(titles, cfg)
    mapping = {titles[i]: arr[i] for i in range(min(len(arr), len(titles)))}

    for it in all_items:
        m = mapping.get(it.get("title", "").strip())
        if m:
            it["ko"] = m.get("ko", "")
            it["why"] = m.get("why", "")

    data["enriched_at"] = datetime.now().isoformat(timespec="seconds")
    data["enrich_engine"] = engine

    js = "window.STOCK_DATA = " + json.dumps(data, ensure_ascii=False) + ";"
    (BASE / "data.js").write_text(js, encoding="utf-8")
    (BASE / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"done (engine={engine})")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
