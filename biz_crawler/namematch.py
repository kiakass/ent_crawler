"""상호명 정규화 및 유사도 매칭.

내부 상호명과 외부 소스(공정위·NAVER)의 상호명은 법인 표기·공백·지점명
때문에 원문 그대로는 거의 일치하지 않는다. 실측으로 공정위 대조 시
원문 일치 26% → 정규화 후 85% 였다. 조인·후보 선택은 항상 정규화 키로 한다.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# 법인·단체 표기. 긴 것부터 지워야 '주식회사'가 '(주)'보다 먼저 걸린다.
_ORG_FORMS = (
    "유한책임회사", "사회복지법인", "영농조합법인", "재단법인", "사단법인",
    "의료법인", "학교법인", "종교법인", "특수법인", "협동조합",
    "주식회사", "유한회사", "합자회사", "합명회사",
    "㈜", "㈔", "㈐", "(주)", "（주）", "(유)", "(재)", "(사)", "(합)", "(의)",
    "co.,ltd", "co., ltd", "co.ltd", "ltd.", "ltd", "inc.", "inc", "corp.", "corp",
)

_TAG = re.compile(r"<[^>]+>")
_PUNCT = re.compile(r"[\s\-_.,·‧・/\\'\"’”()\[\]{}（）「」【】!?&+*~^:;|]")
# 지점 표기: 끝에 붙는 '○○점', '○○지점', '○○영업소' 등
_BRANCH = re.compile(r"(지점|영업소|출장소|대리점|점)$")


def strip_tags(text: str | None) -> str:
    """NAVER 응답 title 의 <b> 강조 태그 제거."""
    return _TAG.sub("", text or "").strip()


def normalize(name: str | None) -> str:
    """비교용 정규화 키. 법인 표기·공백·기호를 제거하고 소문자화한다."""
    s = strip_tags(name).lower()
    for form in _ORG_FORMS:
        s = s.replace(form.lower(), "")
    return _PUNCT.sub("", s)


def core(name: str | None) -> str:
    """정규화 키에서 지점 표기까지 떼어낸 핵심 상호.

    '하삼동커피거제수월점' 처럼 지점명이 붙은 경우, 본점명만으로도
    매칭을 시도할 수 있게 한다. 지점 정보가 사라지므로 동일 브랜드
    다른 지점과 혼동될 수 있어 점수를 낮춰 쓴다.
    """
    return _BRANCH.sub("", normalize(name))


def similarity(a: str | None, b: str | None) -> float:
    """두 상호명의 유사도 0.0~1.0.

    완전일치 > 포함관계 > 문자열 유사도 순으로 판정한다. 포함관계를
    별도로 두는 건 '○○의원' vs '○○의원 강남점' 처럼 한쪽이 더 긴
    케이스가 흔하기 때문이다.
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.90
    ca, cb = core(a), core(b)
    if ca and cb and ca == cb:
        return 0.85
    return round(SequenceMatcher(None, na, nb).ratio(), 3)


def strip_org_raw(name: str | None) -> str:
    """법인 표기만 지우고 원문 형태(대소문자·공백)는 살린다.

    normalize() 는 비교용 키라 공백까지 지워 검색어로 쓸 수 없다.
    검색 질의용 변형을 만들 때는 이쪽을 쓴다.
    """
    s = strip_tags(name)
    low = s.lower()
    for form in _ORG_FORMS:
        f = form.lower()
        while f in low:
            i = low.index(f)
            s = s[:i] + s[i + len(f):]
            low = s.lower()
    return " ".join(s.split())


def strip_branch_raw(name: str | None) -> str:
    """원문에서 끝에 붙은 지점 표기를 뗀다."""
    return _BRANCH.sub("", strip_tags(name)).strip()
