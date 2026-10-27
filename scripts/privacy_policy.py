#!/usr/bin/env python3
"""사이트 개인정보 처리방침 → 개인정보 · 민감정보 처리 기업 파일 (배치 잡과 별도, 10/27).

    python scripts/privacy_policy.py                                   SME1008 최신 delta
    python scripts/privacy_policy.py output/batch/SME1008/delta_20261027.csv
    python scripts/privacy_policy.py --refresh                         캐시 무시하고 다시 수집

결과  output/privacy/개인정보처리_<delta 날짜>.csv
원문  data/external/privacy/docs/<호스트>.txt · 캐시 data/external/privacy/cache.json (이미 본 사이트는 건너뛴다)
등급 · 근거수준 규칙은 biz_crawler/privacy_policy.py 상단. 외부 API 한도는 쓰지 않는다(사이트 직접 접속, 1초 간격,
robots.txt 준수). 터미널에는 건수만 찍는다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_crawler import privacy_policy as pp  # noqa: E402
from biz_crawler.config import PROJECT_ROOT  # noqa: E402
from biz_crawler.logging_utils import RedactingFilter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="개인정보 처리방침 판정")
    ap.add_argument("delta", nargs="?", help="delta CSV (기본: SME1008 최신)")
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 다시 수집")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)

    delta = Path(args.delta) if args.delta else max((PROJECT_ROOT / "output/batch/SME1008").glob("delta_*.csv"))
    path, stats = pp.run(delta, refresh=args.refresh,
                         progress=lambda i, n: print(f"  {i}/{n}", flush=True) if i % 25 == 0 or i == n else None)
    print(f"\n{path.relative_to(PROJECT_ROOT)} — 자체 사이트 {stats.pop('자체 사이트 수집'):,}곳 수집")
    for k, v in sorted(stats.items()):
        print(f"  {k:<24} {v:>6,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
