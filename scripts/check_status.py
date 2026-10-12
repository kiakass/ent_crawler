#!/usr/bin/env python3
"""CSV 의 사업자등록번호를 국세청 상태조회 API로 확인해 결과 CSV를 만든다.

사용 예:
    python scripts/check_status.py 기업리스트.csv
    python scripts/check_status.py 기업리스트.csv -o output/결과.csv --active-only
    python scripts/check_status.py 기업리스트.csv --bizno-col IK_BRNO --name-col EPM_CUST_NM
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_crawler.bizno import format_display
from biz_crawler.logging_utils import install as install_logging
from biz_crawler.config import ConfigError
from biz_crawler.nts_status import NtsStatusClient, StatusResult

# 사업자번호 컬럼 자동 탐지용 후보 (대소문자 무시)
_BIZNO_CANDIDATES = ("ik_brno", "brno", "b_no", "사업자등록번호", "사업자번호", "business_id")
_NAME_CANDIDATES = ("epm_cust_nm", "cust_nm", "상호명", "상호", "business_name", "company")

_OUT_FIELDS = [
    "b_no_display",
    "registered",
    "is_active",
    "b_stt",
    "b_stt_cd",
    "tax_type",
    "end_dt",
    "tax_type_change_dt",
    "error",
]


def _pick_column(header: list[str], candidates: tuple[str, ...], kind: str) -> str:
    lowered = {h.lower().strip(): h for h in header}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    raise SystemExit(
        f"{kind} 컬럼을 찾을 수 없습니다. 헤더: {header}\n"
        f"  --bizno-col / --name-col 로 직접 지정하세요."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="사업자등록번호 상태조회 (국세청 API)")
    ap.add_argument("input", type=Path, help="입력 CSV 경로")
    ap.add_argument("-o", "--output", type=Path, help="출력 CSV 경로 (기본: output/<입력파일명>_status.csv)")
    ap.add_argument("--bizno-col", help="사업자번호 컬럼명 (미지정 시 자동 탐지)")
    ap.add_argument("--name-col", help="상호명 컬럼명 (미지정 시 자동 탐지)")
    ap.add_argument("--limit", type=int, help="앞에서 N건만 처리 (테스트용)")
    ap.add_argument("--active-only", action="store_true", help="계속사업자만 출력")
    ap.add_argument("--checksum", action="store_true", help="호출 전 체크섬 검증으로 오타 선별")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    install_logging(logging.DEBUG if args.verbose else logging.INFO)

    if not args.input.exists():
        raise SystemExit(f"입력 파일이 없습니다: {args.input}")

    with args.input.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        bizno_col = args.bizno_col or _pick_column(header, _BIZNO_CANDIDATES, "사업자번호")
        name_col = args.name_col
        if name_col is None:
            try:
                name_col = _pick_column(header, _NAME_CANDIDATES, "상호명")
            except SystemExit:
                name_col = None  # 상호명은 없어도 진행
        rows = list(reader)

    if args.limit:
        rows = rows[: args.limit]

    out_path = args.output or Path("output") / f"{args.input.stem}_status.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"입력 {len(rows)}건 | 사업자번호 컬럼: {bizno_col}"
          + (f" | 상호명 컬럼: {name_col}" if name_col else ""))

    try:
        client = NtsStatusClient()
    except ConfigError as exc:
        raise SystemExit(str(exc))

    stats: Counter[str] = Counter()
    written = 0

    with client, out_path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=header + _OUT_FIELDS)
        writer.writeheader()

        results = client.check_many(
            (r.get(bizno_col, "") for r in rows),
            validate_checksum=args.checksum,
        )
        for row, res in zip(rows, results):
            stats[_bucket(res)] += 1
            if args.active_only and not res.is_active:
                continue
            writer.writerow({
                **row,
                "b_no_display": format_display(res.b_no),
                "registered": res.registered,
                "is_active": res.is_active,
                "b_stt": res.b_stt,
                "b_stt_cd": res.b_stt_cd,
                "tax_type": res.tax_type,
                "end_dt": res.end_dt,
                "tax_type_change_dt": res.tax_type_change_dt,
                "error": res.error,
            })
            written += 1

    print("\n── 결과 요약 ──")
    for label, cnt in stats.most_common():
        print(f"  {label}: {cnt}건")
    print(f"\n출력: {out_path} ({written}건 기록)")
    return 0


def _bucket(res: StatusResult) -> str:
    if res.error:
        if "지수표기" in res.error:
            return "입력손상(지수표기)"
        if "미등록" in res.error:
            return "국세청 미등록"
        if "체크섬" in res.error:
            return "체크섬 불일치"
        if "빈 값" in res.error:
            return "빈 값"
        if "자릿수" in res.error:
            return "자릿수 오류"
        return f"조회실패({res.error[:40]})"
    return res.b_stt or "상태미상"


if __name__ == "__main__":
    sys.exit(main())
