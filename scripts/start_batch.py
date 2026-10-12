#!/usr/bin/env python3
"""input/ 의 목록으로 본 배치를 시작한다 — 점검 → 적재 → 일정 추정 → 매일 자동 실행 등록.

    python scripts/start_batch.py                          input/ 의 CSV 1개 · 잡 이름 자동 (JOB<YYMMDD>)
    python scripts/start_batch.py input/목록.csv --job SME1101
    python scripts/start_batch.py ... --mode python        launchd 대신 scheduler.py 로 (지금 이 터미널에서 계속 돈다)
    python scripts/start_batch.py ... --mode none          적재 · 추정까지만 (실행은 직접)
    python scripts/start_batch.py ... --bizno-col 사업자번호 --name-col 상호

--mode launchd(기본, macOS): scripts/batch_launchd.sh install — 매일 00:10 daily + 상주 크롤러.
이미 적재된 잡이면 적재를 건너뛰고 등록만 다시 한다 (같은 명령을 다시 쳐도 안전).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def step(title: str, cmd: list[str]) -> int:
    print(f"\n── {title}\n$ {' '.join(Path(c).name if c == PY else c for c in cmd)}", flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="본 배치 시작")
    ap.add_argument("input", nargs="?", help="입력 CSV (생략 시 input/ 의 CSV 1개)")
    ap.add_argument("--job", help="잡 이름 — 영문·숫자 (기본 JOB<YYMMDD>)")
    ap.add_argument("--mode", choices=["launchd", "python", "none"],
                    default="launchd" if sys.platform == "darwin" else "python")
    ap.add_argument("--bizno-col", default="IK_BRNO")
    ap.add_argument("--name-col", default="EPM_CUST_NM")
    args = ap.parse_args()

    if args.input:
        src = Path(args.input)
    else:
        found = sorted((ROOT / "input").glob("*.csv"))
        if len(found) != 1:
            names = ", ".join(p.name for p in found) or "없음"
            raise SystemExit(f"input/ 에 CSV 가 {len(found)}개입니다 ({names}) — 파일을 인자로 지정하세요")
        src = found[0]
    if not src.exists():
        raise SystemExit(f"입력 파일이 없습니다: {src}")
    job = args.job or f"JOB{datetime.now(ZoneInfo('Asia/Seoul')):%y%m%d}"
    if not job.isalnum():
        raise SystemExit(f"잡 이름은 영문·숫자만: {job}")
    print(f"입력 {src.name} · 잡 {job} · 실행 방식 {args.mode}")

    if step("1. 설치 점검", [PY, "scripts/setup_check.py"]):
        return 1

    if (ROOT / "data" / "batch" / job / "state.db").exists():
        print(f"\n── 2. 적재 — 잡 {job} 이 이미 있어 건너뜁니다")
    elif step("2. 적재", [PY, "scripts/batch.py", "import", str(src), "--job", job,
                          "--bizno-col", args.bizno_col, "--name-col", args.name_col]):
        print("\n컬럼 이름이 다르면 --bizno-col · --name-col 로 지정하세요")
        return 1

    step("3. 일정 추정 (API 호출 0회)", [PY, "scripts/batch.py", "plan", "--job", job])

    if args.mode == "launchd":
        if step("4. 매일 자동 실행 등록 (launchd)", ["scripts/batch_launchd.sh", "install", job]):
            return 1
        print(f"\n등록 완료 — 매일 00:10 daily + 상주 크롤러.\n"
              f"  확인: python scripts/batch.py status --job {job}\n"
              f"  해제: scripts/batch_launchd.sh uninstall {job}\n"
              f"  Mac 조건(잠자기 방지 · 로그인 유지 · 00:05 깨우기)은 README 6절")
        return 0
    if args.mode == "python":
        print("\n── 4. Python 스케줄러 시작 — 이 터미널을 닫으면 멈춘다 (계속 돌리려면 README 6.2 의 nohup)")
        return step("scheduler", [PY, "scripts/scheduler.py", "--job", job])
    print(f"\n적재 완료. 하루치 실행: python scripts/batch.py daily --job {job} --with-crawl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
