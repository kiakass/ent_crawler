"""국민연금 업종명 → 한국표준산업분류(KSIC) 11차 세세분류 코드 (2026-10-08).

국민연금 업종코드(wkplIntpCd)는 8차 기반 국민연금 자체 6자리라 현행 KSIC 와 다르다. 대신 업종명이
현행 세세분류명과 거의 같다 (파일럿: 11차 정확 일치 94% · 10차 95%). 그래서 이름으로 맞춘다.

    기준표: data/external/ksic/ksic11_세세분류.csv · ksic10_세세분류.csv
            (scripts/extract_ksic.py 가 통계청 엑셀에서 추출)

대조 순서
  1. 11차 세세분류명 정확 일치 (공백 · 가운뎃점 · 쉼표 · 괄호 무시)
  2. '통관 대리 … / 화물 운송 …' 처럼 '/' 로 묶인 업종명은 앞쪽부터 차례로
  3. 10차 세세분류명 일치 → 같은 코드가 11차에도 있을 때만 (개정으로 코드가 바뀐 업종은 붙이지 않는다)

국민연금 업종이 없거나 포괄 코드면 NAVER 카테고리 대응표로 보충한다 (10/19, 아래 classify).
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache

from .config import PROJECT_ROOT

KSIC_DIR = PROJECT_ROOT / "data" / "external" / "ksic"
RESULT_FIELDS = ["ksic_code", "ksic_name", "ksic_basis"]

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


def classify_nps(row: dict) -> dict[str, str]:
    """국민연금 업종이 쓸 수 있는 값일 때만 KSIC 코드를 붙인다."""
    from .nps_bplc import NO_INDUSTRY_PREFIX

    code = (row.get("nps_code") or "").strip()
    if row.get("nps_status") != "matched" or not code or code.startswith(NO_INDUSTRY_PREFIX):
        return {"ksic_code": "", "ksic_name": ""}
    k, n = lookup(row.get("nps_industry"))
    return {"ksic_code": k, "ksic_name": n}


# ── NAVER 카테고리 → KSIC (2026-10-19) ─────────────────────────
# 대응표: data/external/ksic/naver_ksic.csv (scripts/build_naver_ksic.py 가 만든다 · 상위 100 은 사람이 확정).
# 10/19 결정 — KSIC 명칭 기준으로 통일. 국민연금은 어린이집 · 요양 · 복지시설을 '그 외 기타 …' 포괄 업종으로
# 두는 일이 많아(어린이집 70~88% 가 96999), 국민연금 값이 포괄 코드이고 NAVER 대응이 구체 코드면 NAVER 쪽을 쓴다.
# 코드 길이는 대응 수준에 따라 2(중) · 3(소) · 4(세) · 5(세세) 자리다.

NAVER_MAP = KSIC_DIR / "naver_ksic.csv"
CATCH_ALL = re.compile(r"그 외 기타|달리 분류되지 않은|분류 안된")


@lru_cache(maxsize=1)
def _naver_map() -> dict[str, tuple[str, str]]:
    if not NAVER_MAP.exists():
        return {}
    with NAVER_MAP.open(encoding="utf-8-sig", newline="") as f:
        return {r["NAVER카테고리"]: (r["코드"], r["명칭"]) for r in csv.DictReader(f) if r.get("코드")}


def naver_lookup(row: dict) -> tuple[str, str]:
    """믿을 수 있는 NAVER 업소의 카테고리 → (KSIC 코드, 명칭). 없으면 ('', '')."""
    from .merge import naver_trusted     # merge 가 이 모듈을 함수 안에서 부르므로 여기서도 늦게 부른다

    cat = (row.get("naver_category") or "").strip()
    if not cat or not naver_trusted(row):
        return "", ""
    return _naver_map().get(cat, ("", ""))


def classify(row: dict) -> dict[str, str]:
    """국민연금 → (없거나 포괄 코드면) NAVER 카테고리 대응표. ksic_basis 로 출처를 남긴다."""
    nps = classify_nps(row)
    k, n = nps["ksic_code"], nps["ksic_name"]
    nk, nn = naver_lookup(row)
    # 보정은 NAVER 대응이 세세분류(5자리)일 때만 — 중분류 '42' 로 국민연금 '42499' 를 덮으면 오히려 거칠어진다
    if k and len(nk) == 5 and nk != k and CATCH_ALL.search(n) and not CATCH_ALL.search(nn):
        return {"ksic_code": nk, "ksic_name": nn, "ksic_basis": "NAVER보정"}
    if k:
        return {"ksic_code": k, "ksic_name": n, "ksic_basis": "국민연금"}
    if nk:
        return {"ksic_code": nk, "ksic_name": nn, "ksic_basis": "NAVER추정"}
    return {"ksic_code": "", "ksic_name": "", "ksic_basis": ""}
