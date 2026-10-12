"""국민연금 가입 사업장 조회 — 업종코드 확보 (업종 분류 1순위 소스).

국세청·공정위는 업종을 주지 않는다. 국민연금은 직원이 가입한 사업장마다
업종코드(wkplIntpCd, 6자리)와 업종명(vldtVlKrnNm)을 준다. 코드 앞 2자리는
한국표준산업분류(8차) 중분류다.

    GET https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2
        /getBassInfoSearchV2    bzowrRgstNo(사업자번호 앞 6자리) · wkplNm(부분일치)
        /getDetailInfoSearchV2  seq → 업종코드 · 업종명

주의 (2026-10-08 실측):
  - 파라미터는 camelCase 다. 문서식 snake_case(bzowr_rgst_no)는 조용히 무시되고
    0건이 온다
  - 사업자번호는 앞 6자리만 받는다. 같은 6자리에 수백 개 사업장이 걸리므로
    상호명 유사도로 확정한다 (공사 현장명 '상호-(일용)현장명' 은 앞부분만 비교)
  - 일 10,000회 (x-ratelimit-limit). 행당 약 2.1회 — 100만건 배치는 API 가
    아니라 월별 파일데이터로 조인해야 한다 (design/작업계획서.md §3.2)
  - 업종명 'BIZ_NO미존재사업장'(코드 99…)·'해당없음'(00…) 은 업종이 없는 것이다
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import http_client, odcloud_service_key
from .namematch import similarity, strip_org_raw

log = logging.getLogger(__name__)

API_BASE = "https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2"
MIN_SCORE = 0.6

RESULT_FIELDS = ["nps_status", "nps_wkpl_name", "nps_code", "nps_industry",
                 "nps_match_score", "nps_active", "nps_error", "nps_address"]

# 업종이 아닌 코드 (앞 2자리)
NO_INDUSTRY_PREFIX = ("00", "94", "99")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _site_name(raw: str) -> str:
    """'상호(주)-(일용)OO공사' · '상호/상용/OO' 에서 상호 부분만."""
    return (raw or "").split("-")[0].split("/")[0].strip()


@dataclass(slots=True)
class NpsResult:
    status: str                 # matched / name_mismatch / not_found / error
    wkpl_name: str = ""
    code: str = ""
    industry: str = ""
    score: float = 0.0
    active: str = ""            # 1 가입 · 2 탈퇴
    error: str = ""
    address: str = ""           # 사업장 도로명 주소 (지역 검증용)

    def to_row(self) -> dict:
        return {
            "nps_status": self.status,
            "nps_wkpl_name": self.wkpl_name,
            "nps_code": self.code,
            "nps_industry": self.industry,
            "nps_match_score": f"{self.score:.2f}" if self.score else "",
            "nps_active": self.active,
            "nps_error": self.error,
            "nps_address": self.address,
        }


class NpsClient:
    def __init__(self, *, delay: float = 0.05, max_attempts: int = 3) -> None:
        self._key = odcloud_service_key()
        self._delay = delay
        self._max_attempts = max_attempts
        self._client = http_client(timeout=30.0)
        self.calls = 0

    def __enter__(self) -> "NpsClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, op: str, **params: object) -> list[dict]:
        @retry(stop=stop_after_attempt(self._max_attempts),
               wait=wait_exponential(multiplier=1, max=10),
               retry=retry_if_exception(_is_retryable), reraise=True)
        def _send() -> dict:
            self.calls += 1
            resp = self._client.get(f"{API_BASE}/{op}", params={
                "serviceKey": self._key, "dataType": "JSON", **params})
            resp.raise_for_status()
            return resp.json()

        payload = _send()
        if self._delay:
            time.sleep(self._delay)
        # 게이트웨이 오류는 'response' 없이 온다 — 0건(미등록)으로 오인하지 않는다
        if "response" not in payload:
            raise RuntimeError(f"비정상 응답: {str(payload)[:80]}")
        resp = payload["response"]
        code = resp.get("header", {}).get("resultCode")
        if code not in (None, "00"):
            raise RuntimeError(f"{code} {resp.get('header', {}).get('resultMsg', '')}")
        items = (resp.get("body") or {}).get("items") or {}
        items = items.get("item", []) if isinstance(items, dict) else items
        return [items] if isinstance(items, dict) else list(items)

    def lookup(self, b_no: str, name: str) -> NpsResult:
        """사업자번호 앞 6자리 + 상호명으로 사업장을 찾고 업종을 가져온다."""
        name = (name or "").strip()
        if len(b_no or "") != 10 or not name:
            return NpsResult(status="error", error="사업자번호·상호명 필요")
        prefix = b_no[:6]
        query = (strip_org_raw(name) or name)[:20]
        try:
            items = self._get("getBassInfoSearchV2", bzowrRgstNo=prefix,
                              wkplNm=query, numOfRows=50, pageNo=1)
            if not items:
                # 상호 표기가 달라 부분일치가 안 될 때 — 6자리만으로 넓힌다
                items = self._get("getBassInfoSearchV2", bzowrRgstNo=prefix,
                                  numOfRows=100, pageNo=1)
        except Exception as exc:
            return NpsResult(status="error", error=f"{type(exc).__name__}: {exc}"[:120])
        if not items:
            return NpsResult(status="not_found")

        # 상호 유사도가 같으면 가입 중(1)인 사업장을 우선
        best = max(items, key=lambda i: (similarity(name, _site_name(i.get("wkplNm", ""))),
                                         i.get("wkplJnngStcd") == "1"))
        score = similarity(name, _site_name(best.get("wkplNm", "")))
        if score < MIN_SCORE:
            return NpsResult(status="name_mismatch", score=score)

        try:
            detail = self._get("getDetailInfoSearchV2", seq=best["seq"])
        except Exception as exc:
            return NpsResult(status="error", score=score,
                             error=f"{type(exc).__name__}: {exc}"[:120])
        d = detail[0] if detail else {}
        return NpsResult(status="matched", wkpl_name=_site_name(best.get("wkplNm", "")),
                         code=str(d.get("wkplIntpCd") or ""),
                         industry=str(d.get("vldtVlKrnNm") or "").strip(),
                         score=score, active=str(best.get("wkplJnngStcd") or ""),
                         address=str(d.get("wkplRoadNmDtlAddr") or best.get("wkplRoadNmDtlAddr") or "").strip())
