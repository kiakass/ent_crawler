#!/usr/bin/env python3
"""공개 명단 → 출처별 사업자번호 · 사업자명 파일 (배치 잡과 별도, 10/26).

    python scripts/public_lists.py                 새 명단이 있으면 받고 → 파일 4개 만들기
    python scripts/public_lists.py --no-fetch      받지 않고 로컬 명단으로만
    python scripts/public_lists.py --refresh       ISMS · CISO 를 날짜와 상관없이 다시 받기

결과 (output/public_lists/)
    CISO_신고업체_<자료일>.csv            중앙전파관리소 정보보호최고책임자 신고 업체 (data.go.kr 15080766)
    ISMS_인증현황_<수집일>.csv            ISMS · ISMS-P 인증서 발급 현황 (isms-p.or.kr)
    산재공표_산업안전보건법_<만든날>.csv   고용노동부 'YYYY년 산업재해 발생건수 등 공표' (최근 3년)
    산재공표_중대재해처벌법_<만든날>.csv   고용노동부 '중대산업재해 발생사실 공표' (반기)

명단에 사업자번호가 없으면 CISO 명단 · 근로복지공단 산재보험 가입 사업장 전체(API 약 70회, 30일마다) ·
공정위 전체 다운로드 · 잡 상호로 찾는다. 같은 이름 후보가 여럿이면 국세청 상태조회(100건씩, 결과 캐시)로
공표 사유 당시 영업 중이던 후보가 하나뿐일 때 붙인다 (--no-fetch 면 캐시만) — 규칙은
biz_crawler/public_lists.py 상단. 잡 state.db 는 읽기만 한다. 터미널에는 건수만 찍는다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_crawler import public_lists as pl  # noqa: E402
from biz_crawler.logging_utils import RedactingFilter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="공개 명단 사업자번호 파일")
    ap.add_argument("--job", action="append", help="사업자번호 참조에 쓸 잡 (여러 번 가능, 기본 SME1008)")
    ap.add_argument("--no-fetch", action="store_true", help="명단을 받지 않는다")
    ap.add_argument("--refresh", action="store_true", help="ISMS · CISO 를 다시 받는다")
    ap.add_argument("--osha-years", type=int, default=3, help="산안법 공표 최근 몇 년 (기본 3)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter())

    if not args.no_fetch:
        for name, fn in (("CISO", lambda: pl.fetch_ciso(force=args.refresh)),
                         ("ISMS", lambda: pl.fetch_isms(force=args.refresh)),
                         ("근로복지공단", lambda: pl.fetch_kcw(force=args.refresh)),
                         ("고용노동부", lambda: pl.fetch_moel(osha_years=args.osha_years))):
            try:
                print(fn())
            except Exception as exc:      # 받기 실패 — 로컬 명단으로 계속
                print(f"{name} 받기 실패: {type(exc).__name__}: {exc} — 로컬 파일로 계속")

    report = pl.build(args.job or ["SME1008"], fetch_nts=not args.no_fetch)
    check = report.pop("_osha_check", {})
    nts = report.pop("_nts", {})
    print(f"국세청 상태로 좁혀 붙인 행 (후보 여러 곳 → 당시 영업 1곳): "
          + " · ".join(f"{k} 기준 {v:,}" for k, v in nts.items()))
    for fname, c in report.items():
        print(f"\n{pl.OUT_DIR.relative_to(pl.PROJECT_ROOT)}/{fname} — {c['전체']:,}행 · "
              f"사업자번호 있음 {c['사업자번호 있음']:,} · 없음 {c['사업자번호 없음']:,}")
        for k, v in sorted(((k, v) for k, v in c.items() if k.startswith("  ")), key=lambda x: -x[1]):
            print(f"  {k.strip():<24} {v:>7,}")
    for stem, subs in check.items():          # 소제목 '(N개소)' 와 추출한 표 행 수 비교
        bad = [k for k, (got, want) in subs.items() if got != want]
        print(f"\n{stem}: 소제목 {len(subs)}개 · 사업장 {sum(g for g, _ in subs.values()):,}/{sum(w for _, w in subs.values()):,}"
              + (" ✓" if not bad else " ⚠️ 불일치 " + " · ".join(f"{k[:40]} {subs[k][0]}/{subs[k][1]}" for k in bad)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
