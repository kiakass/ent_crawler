"""주소에서 시·도와 시·군·구를 뽑는다 — NAVER 지역 키워드와 지역 검증용.

NAVER 는 상호만으로 찾으면 같은 이름의 다른 지역 업소를 자주 고른다 (2026-10-08 파일럿:
국민연금 주소와 시·도 불일치 51%, 상호 완전일치여도 48%). 사업자번호로 확정되는 공정위·
국민연금 주소가 있으면 그 시·도로 NAVER 결과를 검증한다. 사업자번호 앞 3자리(세무서)는
지역 적중률이 43% 라 쓰지 않는다.
"""

from __future__ import annotations

# 긴 이름을 먼저 본다 ('충청북도' 가 '충북' 보다 먼저)
_SIDO = (
    ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"), ("광주", "광주"),
    ("대전", "대전"), ("울산", "울산"), ("세종", "세종"), ("경기", "경기"), ("강원", "강원"),
    ("충청북", "충북"), ("충북", "충북"), ("충청남", "충남"), ("충남", "충남"),
    ("전라북", "전북"), ("전북", "전북"), ("전라남", "전남"), ("전남", "전남"),
    ("경상북", "경북"), ("경북", "경북"), ("경상남", "경남"), ("경남", "경남"), ("제주", "제주"),
)
_EMPTY = ("N/A", "-", "--", "없음")


def sido(address: str | None) -> str:
    """'경기도 광주시 ...' → '경기'. 모르면 ''."""
    a = (address or "").strip()
    if not a or a in _EMPTY:
        return ""
    head = a.split()[0]
    for prefix, name in _SIDO:
        if head.startswith(prefix):
            return name
    return ""


def sigungu(address: str | None) -> str:
    """주소에서 '시/군/구' 한 토큰 (NAVER 지역 키워드). 모르면 ''."""
    a = (address or "").strip()
    if not a or a in _EMPTY:
        return ""
    for tok in a.split()[1:]:
        if tok.endswith(("시", "군", "구")) and len(tok) >= 3:   # 기존 지역 키워드 규칙과 같게 (캐시 키 일관)
            return tok
    return ""


def known_address(row: dict) -> str:
    """사업자번호로 확정된 주소 — 공정위 > 근로복지공단 > 국민연금."""
    for col in ("ftc_road_address", "ftc_lot_address", "kcw_address", "nps_address"):
        v = (row.get(col) or "").strip()
        if v and v not in _EMPTY:
            return v
    return ""
