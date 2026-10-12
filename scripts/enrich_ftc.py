#!/usr/bin/env python3
"""CSV 에 공정위 통신판매사업자 등록상세 정보를 덧붙인다.

국세청 상태조회 결과 CSV(또는 사업자번호가 있는 아무 CSV)를 입력받아
상호명·주소·전화·이메일·도메인 컬럼을 ftc_ 접두사로 추가한다.

사용 예:
    python scripts/enrich_ftc.py output/기업리스트2_status.csv
    python scripts/enrich_ftc.py 기업리스트2.csv --limit 60      # 커버리지 표본 측정
    python scripts/enrich_ftc.py ... --include-rprsv             # 대표자명/이메일 포함
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_crawler.config import ConfigError
from biz_crawler.ftc_mllbs import (
    RESULT_FIELDS,
    RESULT_FIELDS_WITH_RPRSV,
    FtcMllBsClient,
)
from biz_crawler.logging_utils import install as install_logging

_BIZNO_CANDIDATES = ("ik_brno", "brno", "b_no", "사업자등록번호", "사업자번호", "business_id")


def _pick_column(header: list[str], candidates: tuple[str, ...]) -> str:
    lowered = {h.lower().strip(): h for h in header}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    raise SystemExit(f"사업자번호 컬럼을 찾을 수 없습니다. 헤더: {header}")


def main() -> int:
    ap = argparse.ArgumentParser(description="공정위 통신판매사업자 정보 덧붙이기")
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--bizno-col")
    ap.add_argument("--limit", type=int, help="앞에서 N건만 (커버리지 표본 측정용)")
    ap.add_argument("--delay", type=float, default=0.1, help="요청 간 딜레이 초 (기본 0.1)")
    ap.add_argument("--include-rprsv", action="store_true",
                    help="대표자명·대표자 이메일 포함 (개인정보 — PRD 1.3 비목표 항목)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    install_logging(logging.DEBUG if args.verbose else logging.INFO)

    if not args.input.exists():
        raise SystemExit(f"입력 파일이 없습니다: {args.input}")

    with args.input.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        col = args.bizno_col or _pick_column(header, _BIZNO_CANDIDATES)
        rows = list(reader)
    if args.limit:
        rows = rows[: args.limit]

    out_fields = RESULT_FIELDS_WITH_RPRSV if args.include_rprsv else RESULT_FIELDS
    new_fields = [f for f in out_fields if f not in header]
    out_path = args.output or Path("output") / f"{args.input.stem}_ftc.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"입력 {len(rows)}건 | 사업자번호 컬럼: {col}"
          + (" | 대표자 정보 포함" if args.include_rprsv else ""))

    try:
        client = FtcMllBsClient(delay=args.delay, include_rprsv=args.include_rprsv)
    except ConfigError as exc:
        raise SystemExit(str(exc))

    stats: Counter[str] = Counter()
    filled: Counter[str] = Counter()

    with client, out_path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=header + new_fields)
        writer.writeheader()
        results = client.lookup_many(r.get(col, "") for r in rows)
        for i, (row, res) in enumerate(zip(rows, results), 1):
            extra = res.to_row(include_rprsv=args.include_rprsv)
            writer.writerow({**row, **extra})

            stats["조회성공" if res.found else (res.error or "실패")] += 1
            if res.found:
                for k in ("company_name", "phone", "email", "road_address", "domain"):
                    if (res.fields or {}).get(k):
                        filled[k] += 1
            if i % 100 == 0:
                print(f"  ... {i}/{len(rows)}")

    total = len(rows)
    hit = stats["조회성공"]
    print("\n── 결과 요약 ──")
    for label, cnt in stats.most_common():
        print(f"  {label}: {cnt}건")
    print(f"\n커버리지: {hit}/{total} ({hit/total*100:.1f}%)")
    if hit:
        print("확보 항목 (조회성공 건 기준):")
        for k in ("company_name", "phone", "email", "road_address", "domain"):
            print(f"  {k}: {filled[k]}건 ({filled[k]/hit*100:.0f}%)")
    print(f"\n출력: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
