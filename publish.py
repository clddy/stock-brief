# -*- coding: utf-8 -*-
"""stock-brief publish — 버전 기록 + 깃헙 자동 배포.

1) version.json 의 build 번호를 +1 하고 오늘 날짜를 기록 → version.js 생성
   (대시보드 푸터에 'vN · 날짜 업데이트' 로 표시)
2) 변경분이 있으면 git commit + push (origin 이 있을 때만). 실패해도 배치는 안 죽음.

.gitignore 로 config.json / favorites.js(보유종목) / 로그 / 캐시는 제외되어
깃헙(공개)에는 워치리스트·뉴스·재무만 올라간다.
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
LOG = BASE / "crawler.log"


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] publish: {msg}"
    print(line)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def git(*args, check=False):
    """git 명령 실행 → (returncode, stdout+stderr)."""
    r = subprocess.run(["git", "-C", str(BASE), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.returncode, (r.stdout + r.stderr).strip()


def bump_version():
    vpath = BASE / "version.json"
    v = {"build": 0}
    if vpath.exists():
        try:
            v = json.loads(vpath.read_text(encoding="utf-8"))
        except Exception:
            pass
    now = datetime.now()
    v["build"] = int(v.get("build", 0)) + 1
    v["date"] = now.strftime("%Y-%m-%d")
    v["updated_at"] = now.isoformat(timespec="seconds")
    vpath.write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")
    (BASE / "version.js").write_text(
        "window.STOCK_VERSION = " + json.dumps(
            {"build": v["build"], "date": v["date"]}, ensure_ascii=False) + ";",
        encoding="utf-8")
    return v["build"], v["date"]


def main():
    build, date = bump_version()
    log(f"version v{build} ({date})")

    if not (BASE / ".git").exists():
        log("git 저장소 아님 - 배포 건너뜀(setup 후 활성화)")
        return

    git("add", "-A")
    _, status = git("status", "--porcelain")
    if not status:
        log("변경 없음 - 커밋 안 함")
        return

    rc, out = git("commit", "-m", f"brief v{build} · {date} 자동 업데이트")
    if rc != 0:
        log(f"commit 실패: {out}")
        return
    log(f"commit v{build}")

    rc, out = git("remote", "get-url", "origin")
    if rc != 0:
        log("origin 리모트 없음 - push 건너뜀")
        return

    rc, out = git("push", "origin", "HEAD")
    if rc != 0:
        log(f"push 실패(자격증명 확인): {out[:200]}")
        return
    log("push 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {type(e).__name__}: {e}")
        sys.exit(0)  # 배포 실패가 배치 전체를 막지 않도록
