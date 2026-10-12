#!/usr/bin/env python3
"""launchd 없이 Python 만으로 매일 배치를 돌린다 (batch_launchd.sh 의 대안).

    python scripts/scheduler.py --job J                       매일 00:10 daily + 상주 크롤러
    python scripts/scheduler.py --job J --at 02:00,14:00      실행 시각 지정 (KST, 쉼표로 여러 개)
    python scripts/scheduler.py --job J --run-now             시작하자마자 daily 한 번 더
    python scripts/scheduler.py --job J --no-crawl            크롤러 없이 daily 만
    python scripts/scheduler.py --job J --daily-args="--no-retry"     (값이 -- 로 시작하므로 = 로 붙인다)

이 프로세스가 떠 있는 동안만 돈다. 터미널을 닫아도 돌게 하려면:
    nohup python scripts/scheduler.py --job J >> logs/batch/scheduler_J.log 2>&1 &

동작은 launchd 등록과 같다.
  - daily: 정해진 시각에 `batch.py daily --job J --notify` 를 새 프로세스로 (매번 최신 코드)
  - crawl: `batch.py crawl-worker --job J` 를 띄워 두고, 죽으면 60초 뒤 다시 띄운다
  - 잠자기로 시각을 지나쳤으면 깨어난 직후 한 번 돈다 (launchd 와 같음)
  - macOS 에서는 caffeinate 로 이 프로세스가 도는 동안 시스템 잠자기를 막는다
launchd 에 같은 잡이 등록돼 있으면 함께 쓰지 말 것 — 중복 실행은 잠금 파일이 막지만 크롤러가 둘 뜬다.
Ctrl+C (또는 SIGTERM) 로 멈추면 크롤러도 함께 멈춘다.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")
BATCH = [sys.executable, str(ROOT / "scripts" / "batch.py")]
TICK = 30            # 초 — 시각 확인 · 크롤러 생존 확인 간격
RESTART_DELAY = 60   # 초 — 크롤러가 죽은 뒤 다시 띄우기까지 (launchd ThrottleInterval 과 같음)


def log(msg: str) -> None:
    print(f"{datetime.now(KST):%Y%m%d%H%M%S} scheduler {msg}", flush=True)


def parse_times(spec: str) -> list[tuple[int, int]]:
    out = []
    for t in spec.split(","):
        h, _, m = t.strip().partition(":")
        if not (h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60):
            raise SystemExit(f"--at 형식: HH:MM[,HH:MM...] — 받은 값 {spec}")
        out.append((int(h), int(m)))
    return sorted(out)


def next_run(times: list[tuple[int, int]], after: datetime) -> datetime:
    for day in range(2):
        base = (after + timedelta(days=day)).replace(second=0, microsecond=0)
        for h, m in times:
            t = base.replace(hour=h, minute=m)
            if t > after:
                return t
    raise AssertionError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser(description="Python 일배치 스케줄러")
    ap.add_argument("--job", required=True)
    ap.add_argument("--at", default="00:10", help="daily 실행 시각 KST (기본 00:10 — 한도가 자정에 초기화)")
    ap.add_argument("--run-now", action="store_true", help="시작 직후 daily 한 번")
    ap.add_argument("--no-crawl", action="store_true", help="상주 크롤러를 띄우지 않음")
    ap.add_argument("--daily-args", default="", help="daily 에 덧붙일 인자 — = 로 붙여 쓴다 (예: --daily-args=\"--no-retry\")")
    args = ap.parse_args()

    times = parse_times(args.at)
    daily_cmd = [*BATCH, "daily", "--job", args.job, "--notify", *shlex.split(args.daily_args)]
    crawl_cmd = [*BATCH, "crawl-worker", "--job", args.job]

    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    awake = None
    if sys.platform == "darwin":   # 이 프로세스가 사는 동안 잠자기 방지 (-w: 이 pid 가 끝나면 함께 끝남)
        awake = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])

    crawl: subprocess.Popen | None = None
    crawl_died_at: float | None = None
    daily: subprocess.Popen | None = None
    due = datetime.now(KST) if args.run_now else next_run(times, datetime.now(KST))
    log(f"시작 — 잡 {args.job} · daily {args.at} KST · 크롤러 {'끔' if args.no_crawl else '켬'} · 다음 daily {due:%m-%d %H:%M}")

    try:
        while not stopping:
            now = datetime.now(KST)

            if daily is not None and daily.poll() is not None:
                log(f"daily 종료 — 코드 {daily.returncode}")
                daily = None
            if daily is None and now >= due:
                log("daily 실행")
                daily = subprocess.Popen(daily_cmd, cwd=ROOT)
                due = next_run(times, now)
                log(f"다음 daily {due:%m-%d %H:%M}")

            if not args.no_crawl:
                if crawl is not None and crawl.poll() is not None:
                    log(f"크롤러 종료 — 코드 {crawl.returncode} · {RESTART_DELAY}초 뒤 다시 시작")
                    crawl, crawl_died_at = None, time.monotonic()
                if crawl is None and (crawl_died_at is None or time.monotonic() - crawl_died_at >= RESTART_DELAY):
                    crawl = subprocess.Popen(crawl_cmd, cwd=ROOT)
                    log(f"크롤러 시작 — pid {crawl.pid}")

            for _ in range(TICK):
                if stopping:
                    break
                time.sleep(1)
    finally:
        log("멈추는 중 — 하위 프로세스 종료")
        for p in (crawl, daily):
            if p is not None and p.poll() is None:
                p.terminate()
        for p in (crawl, daily):
            if p is not None:
                try:
                    p.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    p.kill()
        if awake is not None:
            awake.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
