#!/usr/bin/env python3
"""국민연금 가입 사업장 내역 월별 파일 받기 (data.go.kr 15083277 · 로그인 · API 키 불필요).

    python scripts/fetch_nps_file.py            포털이 더 새로우면 받는다
    python scripts/fetch_nps_file.py --check    받지 않고 포털 · 로컬 날짜만 비교
    python scripts/fetch_nps_file.py --force    같은 날짜여도 다시 받는다

일배치(batch.py daily)가 다음 등록 예정일이 지나면 같은 일을 자동으로 한다. 받은 뒤 첫 daily 에서
못 찾은 행(상호 불일치 · 없음)을 새 파일로 다시 대조한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_crawler.batch.stages import NPS_FILE_DIR  # noqa: E402
from biz_crawler.config import http_client  # noqa: E402
from biz_crawler.nps_file import fetch_latest, local_date, remote_info  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="국민연금 월별 파일 받기")
    ap.add_argument("--check", action="store_true", help="날짜만 비교")
    ap.add_argument("--force", action="store_true", help="같은 날짜여도 다시 받기")
    args = ap.parse_args()
    if args.check:
        with http_client(timeout=60.0) as c:
            info = remote_info(c)
        have = local_date(NPS_FILE_DIR)
        print(f"포털 {info['date']} · 로컬 {have or '(없음)'} · 다음 등록 예정 {info['next_regist'] or '?'}"
              + (" — 새 파일 있음" if info["date"] > have else " — 최신"))
        return 0
    out = fetch_latest(NPS_FILE_DIR, force=args.force)
    print(f"{out['status']}: {out['file']} (포털 {out['remote_date']} · 이전 로컬 {out['local_date'] or '-'} · "
          f"다음 등록 예정 {out['next_regist'] or '?'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
