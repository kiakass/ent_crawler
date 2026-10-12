#!/usr/bin/env python3
"""설치 점검 — 배치를 돌리기 전에 빠진 것이 없는지 본다 (외부 호출 0회).

    python scripts/setup_check.py           점검만
    python scripts/setup_check.py --init    .env 템플릿 복사 · 작업 폴더 생성까지

키 값은 출력하지 않는다 (채워졌는지만 본다).
종료 코드: 필수 항목이 하나라도 빠지면 1.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_KEYS = {
    "ODCLOUD_SERVICE_KEY": "국세청 · 공정위 · 국민연금 (data.go.kr)",
    "NCP_APIGW_KEY_ID": "NAVER 지역검색 (NCP)",
    "NCP_APIGW_KEY": "NAVER 지역검색 (NCP)",
    "JUSO_CONFM_KEY": "우편번호 (도로명주소)",
    "CRAWLER_CONTACT": "크롤링 From 헤더 (운영 메일)",
}
PACKAGES = ("httpx", "tenacity", "dotenv", "playwright")
WORK_DIRS = ("input", "data/external/nps", "data/external/ksic", "output", "logs")


def _env_values(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#"):
                out[k.strip()] = v.strip().strip("\"'")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="설치 점검")
    ap.add_argument("--init", action="store_true", help=".env 템플릿 복사 · 작업 폴더 생성")
    args = ap.parse_args()

    missing: list[str] = []      # 필수 — 없으면 배치가 실패한다
    warn: list[str] = []         # 권장 — 없으면 일부 항목이 덜 채워진다

    def check(ok: bool, label: str, fix: str, required: bool = True) -> None:
        mark = "OK  " if ok else ("없음" if required else "권장")
        print(f"  [{mark}] {label}" + ("" if ok else f" — {fix}"))
        if not ok:
            (missing if required else warn).append(label)

    if args.init:
        env = ROOT / ".env"
        if not env.exists():
            shutil.copy(ROOT / ".env.example", env)
            env.chmod(0o600)
            print("만듦: .env (.env.example 복사 — 키를 채우세요)")
        for d in WORK_DIRS:
            (ROOT / d).mkdir(parents=True, exist_ok=True)

    print("환경")
    check(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "3.10 이상 필요")
    check(Path(sys.prefix).resolve() == (ROOT / ".venv").resolve(), "가상환경 .venv 사용 중",
          "source .venv/bin/activate (자동 실행 스크립트가 .venv/bin/python 을 쓴다)")
    for pkg in PACKAGES:
        check(importlib.util.find_spec(pkg) is not None, f"패키지 {pkg}", "pip install -r requirements.txt")
    pw_cache = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", Path.home() / "Library/Caches/ms-playwright"))
    check(any(pw_cache.glob("chromium*")), "Playwright chromium",
          "python -m playwright install chromium (플랫폼 크롤링용)", required=False)

    print("\n키 (.env — 값은 표시하지 않음)")
    values = _env_values(ROOT / ".env")
    check((ROOT / ".env").exists(), ".env 파일", "cp .env.example .env 또는 --init")
    for key, use in REQUIRED_KEYS.items():
        check(bool(values.get(key)), f"{key} ({use})", "README 2절에서 발급")

    print("\n사내망 인증서")
    check((ROOT / "certs" / "ca-bundle.pem").exists(), "certs/ca-bundle.pem",
          "사내망이면 python scripts/setup_ca.py (사내망 밖이면 무시)", required=False)

    print("\n준비 파일 (README 3절)")
    check(any((ROOT / "design" / n).exists() for n in ("업종분류.csv", "업종분류.md")),
          "design/업종분류.md", "저장소에 포함된 파일 — git pull 또는 git checkout design/업종분류.md (병합 단계 필수)")
    check(any((ROOT / "data/external/nps").glob("*.csv")), "data/external/nps/*.csv",
          "data.go.kr 15083277 월별 CSV (없으면 국민연금 API 만 — 하루 약 4,700행)", required=False)
    check((ROOT / "data/external/ksic/ksic11_세세분류.csv").exists(), "data/external/ksic/ksic11_세세분류.csv",
          "python scripts/extract_ksic.py <통계청 xlsx> (없으면 표준산업분류 칸만 빔)", required=False)

    print()
    if missing:
        print(f"필수 {len(missing)}개 없음 — 채운 뒤 다시 실행하세요.")
        return 1
    print("필수 항목 모두 있음" + (f" · 권장 {len(warn)}개 없음" if warn else "")
          + " → 다음: python scripts/run_sample.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
