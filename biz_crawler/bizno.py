"""사업자등록번호 정규화 및 검증."""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\D")
_SCI_NOTATION = re.compile(r"^\d(?:\.\d+)?[Ee][+-]?\d+$")

# 사업자등록번호 검증용 가중치 (앞 9자리에 적용)
_WEIGHTS = (1, 3, 7, 1, 3, 7, 1, 3, 5)


class BiznoError(ValueError):
    """정규화 불가능한 사업자등록번호."""


def normalize(value: str | None) -> str:
    """입력값을 하이픈 없는 10자리 숫자 문자열로 변환한다.

    '123-45-67890', '123 45 67890', 1234567890 등을 모두 받아준다.
    복구 불가능한 값은 BiznoError 를 던진다 — 특히 Excel 이 10자리 숫자를
    '1.23E+09' 로 반올림해 저장한 경우는 원본 자릿수가 이미 소실된 상태라
    추정으로 채우지 않고 명시적으로 실패시킨다.
    """
    text = (value or "").strip()
    if not text:
        raise BiznoError("빈 값")

    if _SCI_NOTATION.match(text):
        raise BiznoError(
            f"지수표기({text}) — Excel 저장 과정에서 자릿수가 소실됨. "
            "원본을 '텍스트' 서식으로 다시 내보내야 합니다"
        )

    digits = _DIGITS.sub("", text)
    if not digits:
        raise BiznoError(f"숫자 없음({text})")
    if len(digits) != 10:
        raise BiznoError(f"자릿수 오류({len(digits)}자리, 10자리여야 함)")
    return digits


def is_valid_checksum(b_no: str) -> bool:
    """국세청 사업자등록번호 체크섬 검증.

    앞 9자리에 가중치를 곱해 합산하고, 9번째 자리 * 5 의 십의 자리를 더한 뒤
    10 의 보수를 취한 값이 마지막 자리와 일치해야 한다. API 호출 전에
    오타를 걸러내 불필요한 요청을 줄이는 용도다.
    """
    if not re.fullmatch(r"\d{10}", b_no):
        return False
    d = [int(c) for c in b_no]
    total = sum(x * w for x, w in zip(d[:9], _WEIGHTS))
    total += (d[8] * 5) // 10
    return (10 - total % 10) % 10 == d[9]


def format_display(b_no: str) -> str:
    """'1234567890' -> '123-45-67890'"""
    if not re.fullmatch(r"\d{10}", b_no):
        return b_no
    return f"{b_no[:3]}-{b_no[3:5]}-{b_no[5:]}"
