"""근로복지공단 고용·산재보험 가입 사업장 조회 — 사업자번호만 받은 목록의 상호 확보.

국세청 상태조회는 상호를 주지 않고, 공정위는 통신판매업자만 있다. 근로복지공단은
사업자번호 10자리로 그대로 검색되고(국민연금처럼 앞 6자리가 아니다) 사업장명 · 주소 ·
우편번호 · 산재 업종을 준다.

    GET http://apis.data.go.kr/B490001/gySjbPstateInfoService/getGySjBoheomBsshItem
        v_saeopjaDrno(사업자번호 10자리) · pageNo · numOfRows   — 응답은 XML

주의 (2026-10-16 실측):
  - 일 10,000회 (x-ratelimit-limit)
  - 사업자번호 1건에 사업장이 여러 개 온다 — 산재(opaBoheomFg=1) · 고용(2) 이 따로 있고
    지점 · 건설현장이 각자 사업장이다. 본사 이름이 가장 여러 번 나온다 → 최다 이름 →
    상시인원 → 성립일 순으로 대표 사업장을 고른다 (상호 아는 SME 125건 · 공개 샘플 9건 전부 적중)
  - 결과가 있는 비율: 계속사업자 67%, 근로자 없는 1인 사업자는 가입 대상이 아니다
  - 주소 90% 가 건물번호까지 있고 우편번호는 전건 5자리
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import http_client, odcloud_service_key

log = logging.getLogger(__name__)

# apis.data.go.kr 는 TLS 핸드셰이크를 거부한다 — http
API_URL = "http://apis.data.go.kr/B490001/gySjbPstateInfoService/getGySjBoheomBsshItem"
PAGE_ROWS = 100


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _int(v: str | None) -> int:
    try:
        return int(v or 0)
    except ValueError:
        return 0


def pick_site(items: list[dict]) -> dict | None:
    """대표 사업장 — 같은 이름이 가장 많은 것 → 상시인원 큰 것 → 성립일 이른 것."""
    if not items:
        return None
    names = Counter(i.get("saeopjangNm", "") for i in items if i.get("saeopjangNm"))
    cands = [i for i in items if i.get("saeopjangNm")]
    if not cands:
        return None
    return max(cands, key=lambda i: (names[i["saeopjangNm"]], _int(i.get("sangsiInwonCnt")),
                                     -_int(i.get("seongripDt") or "99999999")))


@dataclass(slots=True)
class KcwResult:
    status: str                 # matched / not_found / error
    name: str = ""
    address: str = ""
    zipcode: str = ""
    industry: str = ""          # 산재보험 업종명 (표준산업분류 아님)
    sites: int = 0              # 같은 사업자번호의 사업장 수 (산재 · 고용 각각)
    error: str = ""

    def to_row(self) -> dict:
        return {
            "kcw_status": self.status,
            "kcw_name": self.name,
            "kcw_address": self.address,
            "kcw_zip": self.zipcode,
            "kcw_industry": self.industry,
            "kcw_sites": str(self.sites) if self.sites else "",
            "kcw_error": self.error,
        }


class KcomwelClient:
    def __init__(self, *, delay: float = 0.05, max_attempts: int = 3) -> None:
        self._key = odcloud_service_key()
        self._delay = delay
        self._max_attempts = max_attempts
        self._client = http_client(timeout=30.0)
        self.calls = 0

    def __enter__(self) -> "KcomwelClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, b_no: str) -> list[dict]:
        @retry(stop=stop_after_attempt(self._max_attempts),
               wait=wait_exponential(multiplier=1, max=10),
               retry=retry_if_exception(_is_retryable), reraise=True)
        def _send() -> bytes:
            self.calls += 1
            resp = self._client.get(API_URL, params={
                "serviceKey": self._key, "v_saeopjaDrno": b_no,
                "pageNo": 1, "numOfRows": PAGE_ROWS})
            resp.raise_for_status()
            return resp.content

        body = _send()
        if self._delay:
            time.sleep(self._delay)
        root = ET.fromstring(body)
        # 게이트웨이 오류(키 미승인 등)는 <OpenAPI_ServiceResponse> 로 온다 — 0건으로 오인하지 않는다
        code = root.findtext(".//resultCode")
        if code is None:
            msg = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or root.tag
            raise RuntimeError(f"비정상 응답: {msg}"[:80])
        if code != "00":
            raise RuntimeError(f"{code} {root.findtext('.//resultMsg') or ''}"[:80])
        items = [{e.tag: (e.text or "").strip() for e in it} for it in root.iter("item")]
        # 검색은 정확 일치로 확인했지만(10/16) 혹시 모를 부분 일치를 거른다
        return [i for i in items if i.get("saeopjaDrno", "").replace("-", "") == b_no]

    def lookup(self, b_no: str) -> KcwResult:
        if len(b_no or "") != 10:
            return KcwResult(status="error", error="사업자번호 10자리 필요")
        try:
            items = self._get(b_no)
        except Exception as exc:
            return KcwResult(status="error", error=f"{type(exc).__name__}: {exc}"[:120])
        best = pick_site(items)
        if best is None:
            return KcwResult(status="not_found")
        # 업종은 대표 이름 사업장 중 값이 있는 것 (고용보험 쪽은 업종이 비어 있는 경우가 많다)
        same = [i for i in items if i.get("saeopjangNm") == best["saeopjangNm"]]
        industry = next((i["sjEopjongNm"] for i in [best, *same] if i.get("sjEopjongNm")), "")
        return KcwResult(status="matched", name=best["saeopjangNm"],
                         address=best.get("addr", ""), zipcode=best.get("post", ""),
                         industry=industry, sites=len(items))
