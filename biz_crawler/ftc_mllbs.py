"""공정거래위원회 통신판매사업자 등록상세 API 클라이언트.

사업자등록번호(brno)로 통신판매업 신고 정보를 조회한다. 국세청 API 가 주지
않는 상호명·주소·전화번호·이메일·홈페이지 도메인을 여기서 확보한다
(PRD 3장 우선순위 3 보강 + F-02 의 대체 소스).

주의: 통신판매업을 신고한 사업자만 등재돼 있어 전 업종을 커버하지 않는다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Iterable, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .bizno import BiznoError, normalize
from .config import http_client, odcloud_service_key

log = logging.getLogger(__name__)

API_URL = "https://apis.data.go.kr/1130000/MllBsDtl_3Service/getMllBsInfoDetail_3"

# 응답 항목 → 결과 필드 매핑.
# rprsvNm(대표자명)·rprsvEmladr(대표자 이메일)은 개인정보라 기본 수집 대상에서
# 빼고, 필요한 경우에만 include_rprsv 로 켠다 (PRD 1.3 비목표).
_FIELD_MAP = {
    "bzmnNm": "company_name",        # 상호
    "corpYnNm": "corp_yn",           # 법인여부
    "operSttusCdNm": "oper_status",  # 업소상태
    "crno": "corp_reg_no",           # 법인등록번호
    "telno": "phone",                # 전화번호
    "fxno": "fax",                   # 팩스번호
    "lctnRnAddr": "road_address",    # 소재지 도로명
    "lctnAddr": "lot_address",       # 소재지 지번
    "domnCn": "domain",              # 인터넷 도메인
    "ntslMthdNm": "sales_method",    # 판매방식
    "trtmntPrdlstNm": "product",     # 취급품목
    "dclrDate": "declare_date",      # 신고일자
    "clsbizDate": "close_date",      # 폐업일자
}

_RPRSV_MAP = {
    "rprsvNm": "rprsv_name",         # 대표자명 (개인정보)
    "rprsvEmladr": "email",          # 대표자 이메일 (개인정보)
}

# 공정위가 값 대신 주는 자리표시. 전화는 54%(268만 중 145만)가 '010-개인정보' 로 가려져 온다
# (2026-10-08 전수 확인) — 그대로 두면 대표전화 · 주소로 납품된다.
_PLACEHOLDER = ("N/A", "NA", "-", "--", "없음", "해당없음", "미등록", "null")


def clean_value(field: str, value: object) -> str:
    v = str(value or "").strip()
    if not v or v in _PLACEHOLDER or "개인정보" in v:
        return ""
    if field in ("phone", "fax") and sum(ch.isdigit() for ch in v) < 8:
        return ""
    return v


RESULT_FIELDS = ["ftc_found", *(f"ftc_{v}" for v in _FIELD_MAP.values()), "ftc_error"]
RESULT_FIELDS_WITH_RPRSV = [
    "ftc_found",
    *(f"ftc_{v}" for v in _FIELD_MAP.values()),
    *(f"ftc_{v}" for v in _RPRSV_MAP.values()),
    "ftc_error",
]


@dataclass(slots=True)
class MllBsResult:
    b_no: str
    found: bool = False
    fields: dict[str, str] | None = None
    error: str = ""

    def to_row(self, *, include_rprsv: bool = False) -> dict:
        names = list(_FIELD_MAP.values())
        if include_rprsv:
            names += list(_RPRSV_MAP.values())
        src = self.fields or {}
        row = {"ftc_found": self.found, "ftc_error": self.error}
        row.update({f"ftc_{n}": src.get(n, "") for n in names})
        return row


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class FtcMllBsClient:
    """통신판매사업자 등록상세 조회. 1건씩 조회하므로 딜레이를 둔다."""

    def __init__(
        self,
        service_key: str | None = None,
        *,
        delay: float = 0.1,
        max_attempts: int = 3,
        include_rprsv: bool = False,
    ) -> None:
        self._key = service_key or odcloud_service_key()
        self._delay = delay
        self._max_attempts = max_attempts
        self.calls = 0  # 재시도 포함 실제 HTTP 시도 수 (한도 장부용)
        self.include_rprsv = include_rprsv
        self._client = http_client()

    def __enter__(self) -> "FtcMllBsClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, b_no: str) -> dict:
        @retry(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        def _send() -> dict:
            self.calls += 1
            resp = self._client.get(
                API_URL,
                params={
                    "serviceKey": self._key,
                    "pageNo": 1,
                    "numOfRows": 10,
                    "resultType": "json",
                    "brno": b_no,
                },
            )
            resp.raise_for_status()
            return resp.json()

        return _send()

    def lookup(self, b_no: str) -> MllBsResult:
        """정규화된 사업자번호 1건 조회."""
        try:
            payload = self._get(b_no)
        except Exception as exc:
            return MllBsResult(b_no=b_no, error=f"{type(exc).__name__}: {exc}")

        if payload.get("resultCode") not in (None, "00"):
            return MllBsResult(
                b_no=b_no,
                error=f"{payload.get('resultCode')} {payload.get('resultMsg', '')}".strip(),
            )

        items = payload.get("items") or []
        if not items:
            return MllBsResult(b_no=b_no, found=False, error="통신판매업 미신고")

        item = _pick_best(items)
        mapping = dict(_FIELD_MAP)
        if self.include_rprsv:
            mapping.update(_RPRSV_MAP)
        fields = {v: clean_value(v, item.get(k)) for k, v in mapping.items()}
        return MllBsResult(b_no=b_no, found=True, fields=fields)

    def lookup_many(self, raw_values: Iterable[str]) -> Iterator[MllBsResult]:
        """원본 입력값들을 정규화해 순차 조회한다. 개별 실패는 건너뛰지 않고 보고."""
        first = True
        for raw in raw_values:
            try:
                b_no = normalize(raw)
            except BiznoError as exc:
                yield MllBsResult(b_no=(raw or "").strip(), error=str(exc))
                continue
            if not first and self._delay:
                time.sleep(self._delay)
            first = False
            yield self.lookup(b_no)


def _pick_best(items: list[dict]) -> dict:
    """같은 사업자번호로 여러 신고건이 있으면 영업중 + 최신 신고건을 고른다."""
    def score(it: dict) -> tuple[int, str]:
        active = 0 if (it.get("operSttusCdNm") or "").strip() == "폐업" else 1
        return (active, str(it.get("dclrDate") or ""))

    return max(items, key=score)
