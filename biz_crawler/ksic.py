"""국민연금 업종명 → 한국표준산업분류(KSIC) 11차 세세분류 코드 (2026-10-08).

국민연금 업종코드(wkplIntpCd)는 8차 기반 국민연금 자체 6자리라 현행 KSIC 와 다르다. 대신 업종명이
현행 세세분류명과 거의 같다 (파일럿: 11차 정확 일치 94% · 10차 95%). 그래서 이름으로 맞춘다.

    기준표: data/external/ksic/ksic11_세세분류.csv · ksic10_세세분류.csv
            (scripts/extract_ksic.py 가 통계청 엑셀에서 추출)

대조 순서
  1. 11차 세세분류명 정확 일치 (공백 · 가운뎃점 · 쉼표 · 괄호 무시)
  2. '통관 대리 … / 화물 운송 …' 처럼 '/' 로 묶인 업종명은 앞쪽부터 차례로
  3. 10차 세세분류명 일치 → 같은 코드가 11차에도 있을 때만 (개정으로 코드가 바뀐 업종은 붙이지 않는다)
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache

from .config import PROJECT_ROOT

KSIC_DIR = PROJECT_ROOT / "data" / "external" / "ksic"
RESULT_FIELDS = ["ksic_code", "ksic_name"]

_NORM = re.compile(r"[\s·ㆍ,()]")


def _norm(name: str | None) -> str:
    return _NORM.sub("", name or "")


@lru_cache(maxsize=2)
def _table(version: int) -> dict[str, tuple[str, str]]:
    """정규화 명칭 → (코드, 원래 명칭). 파일이 없으면 빈 표."""
    path = KSIC_DIR / f"ksic{version}_세세분류.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {_norm(r["세세분류명"]): (r["세세분류코드"], r["세세분류명"]) for r in csv.DictReader(f)}


@lru_cache(maxsize=2)
def _codes(version: int) -> dict[str, str]:
    return {code: name for code, name in _table(version).values()}


def lookup(industry: str | None) -> tuple[str, str]:
    """국민연금 업종명 → (11차 세세분류 코드, 명칭). 못 찾으면 ('', '')."""
    if not industry:
        return "", ""
    k11, k10, c11 = _table(11), _table(10), _codes(11)
    parts = [p for p in (industry.split("/")) if p.strip()]
    for part in parts:                       # 1·2
        hit = k11.get(_norm(part))
        if hit:
            return hit
    for part in parts:                       # 3
        hit = k10.get(_norm(part))
        if hit and hit[0] in c11:
            return hit[0], c11[hit[0]]
    return "", ""


def classify(row: dict) -> dict[str, str]:
    """국민연금 업종이 쓸 수 있는 값일 때만 KSIC 코드를 붙인다."""
    from .nps_bplc import NO_INDUSTRY_PREFIX

    code = (row.get("nps_code") or "").strip()
    if row.get("nps_status") != "matched" or not code or code.startswith(NO_INDUSTRY_PREFIX):
        return {"ksic_code": "", "ksic_name": ""}
    k, n = lookup(row.get("nps_industry"))
    return {"ksic_code": k, "ksic_name": n}
