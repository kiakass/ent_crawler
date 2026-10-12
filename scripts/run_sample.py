#!/usr/bin/env python3
"""샘플 10건으로 배치 전 과정을 한 번 돌려 본다 — 설치 확인용.

    python scripts/run_sample.py            적재 → daily(크롤링 포함) → 결과 요약
    python scripts/run_sample.py --reset    이전 샘플 잡을 지우고 처음부터
    python scripts/run_sample.py --use-reserve   같은 키의 본 배치가 오늘 예산(95%)을 다 썼을 때

입력: samples/sample_input.csv — 사업자번호를 스스로 공개한 대기업 9곳 + 미등록 번호 1건(오류 처리 확인용).
외부 호출: 국세청 1회 · 공정위 약 10회 · 국민연금 약 20회 · NAVER 약 20회 · 도로명주소 약 10회 + 홈페이지 크롤링.
본 배치와 같은 명령(scripts/batch.py)을 그대로 부르므로 여기서 통과하면 본 배치도 같은 경로로 돈다.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "sample_input.csv"
BATCH = [sys.executable, str(ROOT / "scripts" / "batch.py")]

# 채움 확인 칸 — 요약은 건수만 출력한다 (행 내용은 snapshot.csv 에서 직접 본다)
FILL = ["사업자상태", "주소", "우편번호", "사이트주소", "전화", "이메일", "업종소", "표준산업분류코드"]


def run(*args: str) -> int:
    print(f"\n$ python scripts/batch.py {' '.join(args)}", flush=True)
    return subprocess.call([*BATCH, *args], cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="샘플 10건 시험 실행")
    ap.add_argument("--job", default="SAMPLE")
    ap.add_argument("--reset", action="store_true", help="이전 샘플 잡의 DB · 결과를 지우고 다시")
    ap.add_argument("--use-reserve", action="store_true",
                    help="한도 여유분(98%%까지) 사용 — 같은 키로 본 배치가 오늘 예산을 다 썼을 때")
    args = ap.parse_args()

    if not args.job.upper().startswith("SAMPLE"):
        raise SystemExit("--reset 이 잡 폴더를 지우므로 잡 이름은 SAMPLE 로 시작해야 합니다")
    job_db = ROOT / "data" / "batch" / args.job
    if args.reset:
        for d in (job_db, ROOT / "output" / "batch" / args.job):
            shutil.rmtree(d, ignore_errors=True)
        print(f"지움: 잡 {args.job} (API 한도 장부 · 캐시는 그대로)")

    if not (job_db / "state.db").exists():
        if run("import", str(SAMPLE), "--job", args.job):
            return 1
    else:
        print(f"잡 {args.job} 이 이미 있어 적재를 건너뜁니다 (처음부터: --reset)")

    code = run("daily", "--job", args.job, "--with-crawl", *(["--use-reserve"] if args.use_reserve else []))
    run("status", "--job", args.job)

    snap = ROOT / "output" / "batch" / args.job / "snapshot.csv"
    if not snap.exists():
        print("\nsnapshot.csv 가 없습니다 — 위 로그의 오류를 확인하세요")
        return 1
    with snap.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"\n샘플 결과 ({len(rows)}행) — {snap.relative_to(ROOT)}")
    for col in FILL:
        n = sum(1 for r in rows if r.get(col) and r.get(col) != "기타미분류")
        print(f"  {col:<10} {n:>2} / {len(rows)}")
    unregistered = sum(1 for r in rows if r.get("사업자번호") and not r.get("사업자상태"))
    print(f"  국세청 미등록 {unregistered}건 (샘플의 미등록 번호 1건이 여기 잡히면 정상)")
    print("\n기대치: 사업자상태 9 · 주소 · 업종소 8 이상. 전화 · 이메일은 사이트 구성 · robots 에 따라 적을 수 있다.")
    print("일부 단계가 partial · skipped 여도 오류(error)가 없으면 정상입니다.")
    return code


if __name__ == "__main__":
    sys.exit(main())
