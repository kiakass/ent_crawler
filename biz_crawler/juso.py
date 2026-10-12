"""행정안전부 도로명주소 검색 API — 주소 → 우편번호 (2026-10-08 도입).

    GET https://business.juso.go.kr/addrlink/addrLinkApi.do
        ?confmKey=<JUSO_CONFM_KEY>&keyword=<주소>&currentPage=1&countPerPage=10&resultType=json

실측 (10/08): 호출당 약 0.05초 · 응답에 zipNo(5자리) · 잔여 한도 헤더 없음.
건물번호까지 있는 주소는 1건으로 확정된다. 건물번호가 없으면 수백 건이 나오므로
상위 결과의 우편번호가 모두 같거나, 도로명+건물번호가 정확히 같은 결과가 하나면 그것을 쓰고,
그래도 여럿이면 **첫 결과**를 쓴다(10/08 사용자 결정 — 출처에 '첫 결과' 로 표시).

NAVER 지역검색은 우편번호를 주지 않는다 (응답 9개 필드 확인). 공정위 lctnRnOzip 은 대부분 'N/A'.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import ConfigError, http_client

API_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
PAGE = 10

# 도로명(…로/길 + 건물번호) 또는 지번(…동/리/가 + 번지)까지만 남긴다 — 층·호수·건물명은 검색을 흐린다
_ROAD = re.compile(r"^(.*?(?:로|길)\s?\d+(?:-\d+)?)")
_JIBUN = re.compile(r"^(.*?(?:동|리|가)\s?(?:산\s?)?\d+(?:-\d+)?)")
_PAREN = re.compile(r"\([^)]*\)")


def search_keyword(address: str) -> str:
    a = _PAREN.sub(" ", address or "")
    a = re.sub(r"[,]", " ", a)
    a = re.sub(r"\s+", " ", a).strip()
    m = _ROAD.match(a) or _JIBUN.match(a)
    return (m.group(1) if m else a)[:80]


def _match_key(address: str) -> str:
    """정확 일치 비교용 — 시·도 표기('서울' · '서울특별시') 차이를 없애고 공백 제거."""
    toks = _PAREN.sub(" ", address or "").split()
    if toks and toks[0].endswith(("시", "도")) or (toks and len(toks[0]) <= 4 and toks[0][:2] in _SIDO2):
        toks = toks[1:]
    return "".join(toks)


_SIDO2 = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
          "충북", "충남", "전북", "전남", "경북", "경남", "제주")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


@dataclass(slots=True)
class ZipResult:
    zipcode: str = ""
    status: str = ""        # unique · agree · exact · first(여러 개 중 첫 결과) · not_found · error
    error: str = ""


class JusoClient:
    def __init__(self, *, delay: float = 0.02, max_attempts: int = 3) -> None:
        self._key = os.getenv("JUSO_CONFM_KEY", "").strip()
        if not self._key:
            raise ConfigError("JUSO_CONFM_KEY 가 비어 있습니다 (.env)")
        self._delay = delay
        self._max_attempts = max_attempts
        self._client = http_client(timeout=15.0)
        self.calls = 0

    def __enter__(self) -> "JusoClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._client.close()

    def zip_for(self, address: str) -> ZipResult:
        keyword = search_keyword(address)
        if len(keyword) < 4:
            return ZipResult(status="not_found", error="주소가 너무 짧음")

        @retry(stop=stop_after_attempt(self._max_attempts), wait=wait_exponential(multiplier=1, max=8),
               retry=retry_if_exception(_is_retryable), reraise=True)
        def _send() -> dict:
            self.calls += 1
            r = self._client.get(API_URL, params={"confmKey": self._key, "currentPage": 1,
                                                  "countPerPage": PAGE, "keyword": keyword,
                                                  "resultType": "json"})
            r.raise_for_status()
            return r.json()

        try:
            payload = _send()
        except Exception as exc:
            return ZipResult(status="error", error=f"{type(exc).__name__}: {exc}"[:120])
        finally:
            if self._delay:
                time.sleep(self._delay)
        results = payload.get("results") or {}
        common = results.get("common") or {}
        if str(common.get("errorCode")) != "0":
            msg = f"{common.get('errorCode')} {common.get('errorMessage', '')}".strip()
            # E0006 등 '검색어 부적합' 류는 주소 문제 — 오류가 아니라 미발견으로 본다
            return ZipResult(status="error" if str(common.get("errorCode")) in ("-999", "E0001") else "not_found",
                             error=msg[:120])
        items = results.get("juso") or []
        zips = {str(it.get("zipNo") or "") for it in items if it.get("zipNo")}
        if not zips:
            return ZipResult(status="not_found")
        if len(items) == 1:
            return ZipResult(zipcode=zips.pop(), status="unique")
        if len(zips) == 1:
            return ZipResult(zipcode=zips.pop(), status="agree")
        # '…로72' 검색에 '…로72번길' 등이 섞여 온다 — 도로명+건물번호(또는 지번)가 정확히 같은 것만
        key = _match_key(keyword)
        exact = {str(it.get("zipNo")) for it in items
                 if _match_key(it.get("roadAddrPart1") or "") == key
                 or _match_key(it.get("jibunAddr") or "").startswith(key)}
        if len(exact) == 1:
            return ZipResult(zipcode=exact.pop(), status="exact")
        # 그래도 여럿이면 첫 결과 (API 정렬 = 정확도 순) — 사용자 결정 2026-10-08
        return ZipResult(zipcode=str(items[0].get("zipNo") or ""), status="first")
