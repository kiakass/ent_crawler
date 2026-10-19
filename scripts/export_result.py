#!/usr/bin/env python3
"""최종 결과를 요청 컬럼 순서대로 내보낸다.

    사업자번호 · 상호명 · 이메일 · 전화 · 주소 · 사이트주소
    · 업종 대/중/소 (사내 업종분류표) · 분류 근거 · NAVER 카테고리 대/중/소
    · 국민연금 업종코드/업종명 (외부 원본값)
    · 사업자상태(국세청 계속/휴업/폐업) · 폐업일자 · NAVER 지역검증 · 사이트비고

사업자번호는 하이픈 표기로 쓴다. 순수 숫자 10자리를 Excel 이 열면
1.82E+09 로 바꿔 복구 불가가 되는 사고가 이미 있었다 (373건 손실).
하이픈이 있으면 Excel 이 텍스트로 취급한다.

    python scripts/export_result.py                  전건
    python scripts/export_result.py --only-collected  1개라도 확보된 행만

파일명에 실행 시각(YYYYMMDDHH24MI)을 붙여 이전 결과를 덮어쓰지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_crawler.config import PROJECT_ROOT

MASTER = PROJECT_ROOT / "data" / "master.csv"
OUT_DIR = PROJECT_ROOT / "output"

HEADER = ["사업자번호", "상호명", "이메일", "전화", "주소", "우편번호", "사이트주소",
          "업종대", "업종중", "업종소", "표준산업분류코드", "표준산업분류명", "표준산업분류근거", "업종분류근거",
          "NAVER업종_대", "NAVER업종_중", "NAVER업종_소",
          "국민연금업종코드", "국민연금업종명",
          "사업자상태", "폐업일자", "NAVER지역검증", "사이트비고", "우편번호출처", "주소비고"]

# 출력 컬럼 → 마스터 컬럼
MAP = {
    "사업자번호": "b_no_display",
    "상호명": "final_company_name",
    "이메일": "final_email",
    "전화": "final_phone",
    "주소": "final_address",
    "우편번호": "final_zipcode",
    "사이트주소": "final_homepage",
    "업종대": "ind_large",
    "업종중": "ind_mid",
    "업종소": "ind_small",
    "표준산업분류코드": "ksic_code",
    "표준산업분류명": "ksic_name",
    "표준산업분류근거": "ksic_basis",
    "업종분류근거": "ind_basis",
    "NAVER업종_대": "ind_naver_l1",
    "NAVER업종_중": "ind_naver_l2",
    "NAVER업종_소": "ind_naver_l3",
    "국민연금업종코드": "nps_code",
    "국민연금업종명": "nps_industry",
    "사업자상태": "nts_b_stt",          # 국세청: 계속사업자 · 휴업자 · 폐업자 (빈 값 = 미등록·미조회)
    "폐업일자": "nts_end_dt",
    "NAVER지역검증": "naver_region_check",
    "사이트비고": "site_note",
    "우편번호출처": "zip_source",
    "주소비고": "address_note",
}

COLLECTED = ("final_email", "final_phone", "final_address", "final_homepage")


def main() -> int:
    ap = argparse.ArgumentParser(description="최종 결과 CSV 내보내기")
    ap.add_argument("--only-collected", action="store_true",
                    help="이메일·전화·주소·사이트 중 하나라도 있는 행만")
    ap.add_argument("-o", "--out", type=Path,
                    help="기본값: output/수집결과_YYYYMMDDHH24MI.csv")
    args = ap.parse_args()

    if not MASTER.exists():
        raise SystemExit(f"마스터가 없습니다: {MASTER}")

    with MASTER.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not any((r.get("status") or "").strip() for r in rows):
        raise SystemExit("병합 결과가 비어 있습니다. 먼저 실행하세요: "
                         "python scripts/pipeline.py run 5")

    if args.only_collected:
        rows = [r for r in rows
                if any((r.get(c) or "").strip() for c in COLLECTED)]

    if args.out is None:
        args.out = OUT_DIR / f"수집결과_{datetime.now():%Y%m%d%H%M}.csv"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([(r.get(MAP[h]) or "").strip() for h in HEADER])

    n = len(rows)
    print(f"내보냄: {args.out.relative_to(PROJECT_ROOT)}  —  {n}건\n")
    for h in (h for h in HEADER[1:] if h not in ("업종분류근거", "NAVER지역검증", "사이트비고", "폐업일자", "우편번호출처", "주소비고", "표준산업분류명", "표준산업분류근거")):
        v = sum(1 for r in rows if (r.get(MAP[h]) or "").strip())
        print(f"  {h:6} {v:4}/{n}  ({v / n * 100:5.1f}%)" if n else f"  {h}: -")
    return 0


if __name__ == "__main__":
    sys.exit(main())
