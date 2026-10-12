"""로그에서 자격증명이 새지 않게 하는 필터.

httpx 는 요청 URL 을 INFO 로 그대로 찍는다. 상태조회 API 는 serviceKey 를
쿼리스트링으로 받으므로, 그대로 두면 .env 로 분리한 키가 로그·터미널·
CI 출력에 남는다. 값 부분만 마스킹한다.
"""

from __future__ import annotations

import logging
import re

_SENSITIVE_PARAMS = ("serviceKey", "ServiceKey", "apikey", "api_key", "client_secret")

_PATTERNS = tuple(
    re.compile(rf"({re.escape(p)}=)[^&\s\"']+") for p in _SENSITIVE_PARAMS
)


def redact(text: str) -> str:
    for pat in _PATTERNS:
        text = pat.sub(r"\1***REDACTED***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact(str(v)) for k, v in record.args.items()}
            else:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def install(level: int = logging.INFO) -> None:
    """루트 로거에 마스킹 필터를 걸고 httpx 요청 로그 소음을 줄인다."""
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    filt = RedactingFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(filt)

    # 보험: 필터를 못 타는 경로가 있어도 URL 이 노출되지 않도록 한 단계 올린다.
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(logging.WARNING)
