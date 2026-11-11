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
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
LOG = BASE / "crawler.log"
CHUNK = 25          # LLM 한 번에 처리할 헤드라인 수
CACHE = BASE / "enrich_cache.json"   # 제목 → {ko, why, engine, seen} (뉴스가 7일간 반복돼 재번역 방지)
CACHE_DAYS = 14                      # 이 기간 동안 안 보인 제목은 캐시에서 정리
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


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
LLM_PAUSE = 7.0          # LLM 요청 간격(초) — 무료 Gemini 분당 요청 제한 아래로(약 8회/분)
LLM_MAX_PER_RUN = 400    # 한 번 실행에 LLM 으로 처리할 최대 제목 수(키 넣은 첫날 밀린 분량을 며칠에 나눔)
_SKIP_MODEL = ("lite", "preview", "exp", "thinking", "tts", "image", "live", "audio", "embedding", "learnlm")


class Unavailable(Exception):
    """모델 과부하(503)·서버 오류 — 다른 모델로 넘길 대상."""


def _probe(key, model, timeout=25):
    """모델에 아주 작은 요청 1건 → (응답 여부, 코드). 목록에 있어도 404·과부하 타임아웃인 경우가 있어서."""
    body = {"contents": [{"parts": [{"text": 'Reply JSON [{"ok":1}]'}]}],
            "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 20}}
    try:
        r = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent",
                          headers={"x-goog-api-key": key}, json=body, timeout=timeout)
        return r.status_code == 200, r.status_code
    except Exception as e:
        return False, type(e).__name__


def resolve_gemini_models(key, preferred, want=3):
    """시험 호출에 응답하는 Flash 모델을 최대 want 개, 우선순위대로 반환(설정 모델 → 최신 정식 Flash).
    2026-11-11 실측: 종료 모델도 GET models/<이름> 은 200, 목록에 있어도 '신규 사용자 제공 중단' 404,
    최신 3.7·3.8 은 40초 타임아웃, 3.6 은 작은 요청만 되고 큰 요청은 503(수요 폭주) → 목록만 믿지 않는다."""
    try:
        r = requests.get(f"{GEMINI_BASE}/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": key}, timeout=20)
        r.raise_for_status()
        usable = [m.get("name", "").split("/")[-1] for m in r.json().get("models", [])
                  if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception as e:
        log(f"gemini 모델 목록 실패 ({type(e).__name__}) — '{preferred}' 만 시도")
        return [preferred] if preferred else []
    cands = []
    for name in usable:
        if "flash" not in name or any(x in name for x in _SKIP_MODEL):
            continue
        v = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        if v:
            cands.append((float(v.group(1)), -len(name), name))
    order = ([preferred] if preferred in usable else []) + \
            [n for _, _, n in sorted(cands, reverse=True) if n != preferred]
    ok_models, tried = [], []
    for name in order[:7]:
        ok, code = _probe(key, name)
        tried.append(f"{name}={'OK' if ok else code}")
        if ok:
            ok_models.append(name)
            if len(ok_models) >= want:
                break
    log(f"gemini 모델 시험: {', '.join(tried)} → 사용 순서 {ok_models or '없음'}")
    return ok_models


# 항목마다 번호 i 를 받아서, 몇 개가 빠지거나 순서가 바뀌어도 번호로 맞춘다
PROMPT_IDX = (
    "\n중요: 각 객체에 입력 번호를 \"i\" 로 넣으세요. 형식: [{\"i\":0,\"ko\":\"...\",\"why\":\"...\"}, ...]\n\n"
)


def _index_results(arr, n):
    """LLM 출력 → {입력 인덱스: 객체}. i 가 있으면 i 로, 없고 개수가 같으면 순서대로."""
    out = {}
    if not isinstance(arr, list):
        return out
    if all(isinstance(a, dict) and isinstance(a.get("i"), int) for a in arr):
        for a in arr:
            if 0 <= a["i"] < n and a.get("ko"):
                out[a["i"]] = a
    elif len(arr) == n:
        for i, a in enumerate(arr):
            if isinstance(a, dict) and a.get("ko"):
                out[i] = a
    return out


def gemini_chunk(titles, key, model):
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    body = {
        "contents": [{"parts": [{"text": PROMPT_HEAD + PROMPT_IDX + numbered}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.3,
            "maxOutputTokens": 8192,
        },
    }
    # 키는 URL 이 아니라 헤더로 — 오류 메시지(URL 포함)가 로그에 남아도 키가 새지 않게
    try:
        r = requests.post(f"{GEMINI_BASE}/models/{model}:generateContent",
                          headers={"x-goog-api-key": key}, json=body, timeout=120)
    except requests.exceptions.Timeout:
        raise Unavailable("timeout")
    if r.status_code == 429:
        ra = r.headers.get("Retry-After")
        raise RateLimited(float(ra) if ra and ra.replace(".", "", 1).isdigit() else 40.0)
    if r.status_code in (500, 502, 503, 504):
        raise Unavailable(f"HTTP {r.status_code}")
    if r.status_code >= 400:
        raise RuntimeError(f"gemini HTTP {r.status_code}")
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _index_results(_parse_json_array(text), len(titles))


def claude_chunk(titles, key):
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    resp = client.messages.create(
        model=CLAUDE_MODEL, max_tokens=8000,
        messages=[{"role": "user", "content": PROMPT_HEAD + PROMPT_IDX + numbered}])
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text")
    return _index_results(_parse_json_array(text), len(titles))


def llm_partial(titles, fns, engine, pause=LLM_PAUSE):
    """titles 를 CHUNK 단위로 처리. fns = [(이름, fn)] 우선순위 목록(모델 순환).
    반환: {인덱스: {ko, why, engine}} — 묶음에서 일부만 와도 받은 만큼 저장.
      · 429: 한 번 기다렸다 재시도, 또 막히면 전체 중단
      · 503/타임아웃(과부하): 그 모델을 뒤로 미루고 다음 모델로 같은 묶음 재시도
      · 그 외 실패: 그 묶음 건너뜀. 묶음 연속 2번 실패(0건)면 중단"""
    got, fails, fns = {}, 0, list(fns)
    for n, s in enumerate(range(0, len(titles), CHUNK)):
        if fails >= 2 or not fns:
            log(f"{engine} 중단 (연속 실패 또는 쓸 모델 없음) — 성공 {len(got)}건")
            break
        part = titles[s:s + CHUNK]
        if n:
            time.sleep(pause)
        res, waited, tries, rested = None, False, 0, False
        while fns and tries < 2 * len(fns) + 1:
            if tries == len(fns) and not rested:
                rested = True                        # 모든 모델이 과부하 → 수요 폭주는 보통 짧아서 30초 쉬고 한 바퀴 더
                log(f"{engine} 모든 모델 과부하 — 30초 대기 후 한 바퀴 더")
                time.sleep(30)
            name, fn = fns[0]
            tries += 1
            try:
                res = fn(part)
                break
            except RateLimited as e:
                if waited:
                    log(f"{engine} 429 반복 — 중단 (성공 {len(got)}건, 나머지는 다음 엔진/다음 실행)")
                    return got
                waited = True
                log(f"{engine} 429 — {e.wait:.0f}초 대기 후 재시도")
                time.sleep(e.wait)
            except Unavailable as e:
                log(f"{engine} {name} 과부하({e}) — 다음 모델로")
                fns.append(fns.pop(0))          # 뒤로 미룸
                time.sleep(3)
            except Exception as e:
                log(f"{engine} 묶음 실패 ({type(e).__name__}: {str(e)[:120]}) — 이 묶음 건너뜀")
                break
        if not res:
            fails += 1
            continue
        fails = 0
        if len(res) < len(part):
            log(f"{engine} 묶음 {len(res)}/{len(part)}건만 응답 — 받은 것만 저장")
        for i, a in res.items():
            got[s + i] = {"ko": a.get("ko", ""), "why": a.get("why", ""), "engine": engine}
    return got


GT_URL = "https://translate.googleapis.com/translate_a/single"
GT_BATCH = 30        # 한 요청에 묶는 제목 수(줄바꿈으로 이어서 번역 → 요청 수 1/30)
GT_PAUSE = 1.0       # 요청 사이 간격(초) — 무료 엔드포인트 차단(429) 방지


class RateLimited(Exception):
    def __init__(self, wait=30.0):
        super().__init__(f"rate limited ({wait:.0f}s)")
        self.wait = wait


def _gt(text):
    """구글 무료 번역 1회. 429면 RateLimited."""
    r = requests.get(GT_URL, params={"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text},
                     timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code == 429:
        raise RateLimited()
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])


def google_translate(titles):
    """무료 구글 번역 폴백 — [{ko, why:'', ok}] (입력 순서 유지).

    제목 GT_BATCH 개를 줄바꿈으로 묶어 한 번에 번역하고 줄 단위로 다시 나눈다.
    줄 수가 안 맞으면 그 묶음만 한 건씩 번역. 429(차단)를 받으면 1회 대기 후 재시도,
    또 막히면 나머지는 원문 유지(ok=False → 캐시 안 됨 → 다음 실행 때 재시도).
    """
    out = [{"ko": t, "why": "", "ok": False} for t in titles]
    blocked = False

    def call(text):
        nonlocal blocked
        for wait in (0, 30):
            if blocked:
                return None
            if wait:
                time.sleep(wait)
            try:
                res = _gt(text)
                time.sleep(GT_PAUSE)
                return res
            except RateLimited:
                continue
            except Exception:
                return None
        blocked = True
        return None

    for s in range(0, len(titles), GT_BATCH):
        if blocked:
            break
        chunk = [t.replace("\n", " ") for t in titles[s:s + GT_BATCH]]
        res = call("\n".join(chunk))
        lines = [x.strip() for x in res.split("\n")] if res else []
        if res and len(lines) == len(chunk):
            for i, ko in enumerate(lines):
                if ko:
                    out[s + i] = {"ko": ko, "why": "", "ok": True}
        else:
            for i, t in enumerate(chunk):
                ko = call(t)
                if ko:
                    out[s + i] = {"ko": ko, "why": "", "ok": True}
    if blocked:
        log(f"free translate: 429 차단 — {sum(1 for o in out if not o['ok'])}건 원문 유지, 다음 실행 때 재시도")
    return out


def enrich(titles, cfg):
    """→ titles 와 같은 순서의 [{ko, why, ok, engine}]. LLM(키 있을 때) 성공분 우선, 나머지는 무료 번역."""
    gkey = (cfg.get("gemini_api_key") or "").strip()
    akey = (cfg.get("anthropic_api_key") or "").strip()
    out = [None] * len(titles)
    llm_idx = list(range(min(len(titles), LLM_MAX_PER_RUN)))

    if gkey and llm_idx:
        models = resolve_gemini_models(gkey, (cfg.get("gemini_model") or "").strip())
        if models:
            fns = [(m, (lambda p, m=m: gemini_chunk(p, gkey, m))) for m in models]
            got = llm_partial([titles[i] for i in llm_idx], fns, "gemini")
            for j, r in got.items():
                out[llm_idx[j]] = dict(r, ok=True)
            log(f"gemini ok ({len(got)}/{len(llm_idx)}, models={models})")
            llm_idx = [i for i in llm_idx if out[i] is None]

    if akey and llm_idx:
        got = llm_partial([titles[i] for i in llm_idx], [("claude", lambda p: claude_chunk(p, akey))], "claude", pause=1.0)
        for j, r in got.items():
            out[llm_idx[j]] = dict(r, ok=True)
        log(f"claude ok ({len(got)}/{len(llm_idx)})")

    rest = [i for i in range(len(titles)) if out[i] is None]
    if rest:
        arr = google_translate([titles[i] for i in rest])
        for i, r in zip(rest, arr):
            out[i] = dict(r, engine="translate")
        log(f"free translate ok ({sum(1 for a in arr if a.get('ok'))}/{len(arr)})")
    return out


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

    # 캐시: 같은 엔진 품질 이상으로 이미 처리한 제목은 재사용. LLM 키가 생기면 무료번역분은 다시 처리(요약 붙이기 위해)
    llm_on = bool((cfg.get("gemini_api_key") or "").strip() or (cfg.get("anthropic_api_key") or "").strip())
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    today = datetime.now().strftime("%Y-%m-%d")
    def reusable(c):
        return c and c.get("ko") and (c.get("engine") != "translate" or not llm_on)
    todo = [t for t in titles if not reusable(cache.get(t))]
    log(f"cache hit {len(titles) - len(todo)}, to process {len(todo)}")

    if todo:
        arr = enrich(todo, cfg)
        for t, r in zip(todo, arr):
            if not r or not r.get("ok", True):
                continue                     # 실패분은 캐시하지 않음(다음날 재시도)
            cache[t] = {"ko": r.get("ko", ""), "why": r.get("why", ""), "engine": r.get("engine", "translate")}
    # 대표 엔진: 지금 제목 중 LLM 처리 비율이 절반 이상이면 그 엔진, 아니면 translate (텔레그램 안내문용)
    engs = [cache[t].get("engine") for t in titles if t in cache]
    llm_n = sum(1 for e in engs if e in ("gemini", "claude"))
    engine = (max(("gemini", "claude"), key=engs.count) if llm_n * 2 >= max(1, len(engs)) else "translate")
    for t in titles:
        if t in cache:
            cache[t]["seen"] = today
    cutoff = datetime.now().timestamp() - CACHE_DAYS * 86400
    cache = {k: v for k, v in cache.items()
             if datetime.strptime(v.get("seen", today), "%Y-%m-%d").timestamp() >= cutoff}
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    mapping = {t: cache[t] for t in titles if t in cache}

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
