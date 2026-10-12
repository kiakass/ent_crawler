"""국세청 사업자등록정보 상태조회 API 클라이언트.

공공데이터포털(api.odcloud.kr) 의 nts-businessman/v1/status 엔드포인트를
감싼다. 사업자등록번호만으로 계속사업자/휴업자/폐업자 여부, 과세유형,
폐업일자를 조회한다 (PRD 3장 우선순위 3 — 휴폐업 필터링용).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Iterable, Iterator, Sequence

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .bizno import BiznoError, normalize
from .config import ca_bundle, odcloud_service_key

log = logging.getLogger(__name__)

API_URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"

# API 가 한 번에 받는 사업자번호 최대 개수 (문서 기준 100건)
MAX_BATCH = 100

# 국세청에 등록되지 않은 번호일 때 b_stt 가 빈 문자열로 오므로
# tax_type 문구로 구분한다.
_NOT_REGISTERED = "등록되지 않은"

_STATUS_LABEL = {
    "01": "계속사업자",
    "02": "휴업자",
    "03": "폐업자",
}


@dataclass(slots=True)
class StatusResult:
    """상태조회 1건의 결과."""

    b_no: str                      # 정규화된 10자리
    registered: bool               # 국세청 등록 여부
    b_stt: str = ""                # 계속사업자 / 휴업자 / 폐업자
    b_stt_cd: str = ""             # 01 / 02 / 03
    tax_type: str = ""             # 과세유형
    end_dt: str = ""               # 폐업일자 (YYYYMMDD)
    tax_type_change_dt: str = ""   # 과세유형 전환일자
    error: str = ""                # 조회 자체가 실패한 경우의 사유

    @property
    def is_active(self) -> bool:
        """계속사업자인지 — 휴폐업 필터링의 기준."""
        return self.b_stt_cd == "01"


def _is_retryable(exc: BaseException) -> bool:
    """네트워크 오류와 5xx/429 만 재시도한다.

    401/403(키 오류)이나 400(요청 형식 오류)은 재시도해도 동일하게 실패하므로
    즉시 올려보내 원인을 빨리 드러낸다.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


class NtsStatusClient:
    """상태조회 API 클라이언트. 컨텍스트 매니저로 쓴다."""

    def __init__(
        self,
        service_key: str | None = None,
        *,
        timeout: float = 15.0,
        max_attempts: int = 3,
    ) -> None:
        self._service_key = service_key or odcloud_service_key()
        self._max_attempts = max_attempts
        self.calls = 0  # 재시도 포함 실제 HTTP 시도 수 (한도 장부용)
        self._client = httpx.Client(
            timeout=timeout,
            verify=ca_bundle(),
            headers={"Content-Type": "application/json"},
        )

    def __enter__(self) -> "NtsStatusClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── 내부: 단일 배치 POST ──────────────────────────────────

    def _post_batch(self, b_nos: Sequence[str]) -> dict:
        @retry(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        def _send() -> dict:
            self.calls += 1
            resp = self._client.post(
                API_URL,
                params={"serviceKey": self._service_key},
                json={"b_no": list(b_nos)},
            )
            resp.raise_for_status()
            return resp.json()

        return _send()

    # ── 공개 API ──────────────────────────────────────────────

    def check_batch(self, b_nos: Sequence[str]) -> list[StatusResult]:
        """정규화된 사업자번호 목록(최대 100건)을 한 번에 조회한다."""
        if not b_nos:
            return []
        if len(b_nos) > MAX_BATCH:
            raise ValueError(f"한 배치는 {MAX_BATCH}건까지입니다 (요청: {len(b_nos)}건)")

        try:
            payload = self._post_batch(b_nos)
        except httpx.HTTPStatusError as exc:
            reason = f"HTTP {exc.response.status_code}"
            if exc.response.status_code in (401, 403):
                reason += " (서비스키 확인 필요 — 인코딩 키를 디코딩했는지 점검)"
            log.error("배치 조회 실패: %s", reason)
            return [StatusResult(b_no=n, registered=False, error=reason) for n in b_nos]
        except Exception as exc:  # 네트워크/파싱 오류
            reason = f"{type(exc).__name__}: {exc}"
            log.error("배치 조회 실패: %s", reason)
            return [StatusResult(b_no=n, registered=False, error=reason) for n in b_nos]

        # 응답은 요청 순서를 보장하지 않으므로 b_no 로 매핑한다.
        by_no = {item.get("b_no", ""): item for item in payload.get("data", [])}
        results: list[StatusResult] = []
        for n in b_nos:
            item = by_no.get(n)
            if item is None:
                results.append(
                    StatusResult(b_no=n, registered=False, error="응답에 해당 번호 없음")
                )
                continue
            results.append(_parse_item(n, item))
        return results

    def check_many(
        self,
        raw_values: Iterable[str],
        *,
        validate_checksum: bool = False,
    ) -> Iterator[StatusResult]:
        """원본 입력값들을 정규화·배치 분할해 순차 조회한다.

        정규화에 실패한 값은 API 를 호출하지 않고 error 가 채워진 결과로
        그 자리에서 내보낸다 — 개별 실패가 전체 배치를 멈추지 않게 한다
        (PRD 5.3).
        """
        from .bizno import is_valid_checksum

        pending: list[str] = []
        original: dict[str, str] = {}

        def flush() -> Iterator[StatusResult]:
            if not pending:
                return
            for res in self.check_batch(pending):
                yield res
            pending.clear()

        for raw in raw_values:
            try:
                b_no = normalize(raw)
            except BiznoError as exc:
                yield StatusResult(b_no=(raw or "").strip(), registered=False, error=str(exc))
                continue

            if validate_checksum and not is_valid_checksum(b_no):
                yield StatusResult(b_no=b_no, registered=False, error="체크섬 불일치 (오타 의심)")
                continue

            pending.append(b_no)
            original[b_no] = raw
            if len(pending) >= MAX_BATCH:
                yield from flush()

        yield from flush()


def _parse_item(b_no: str, item: dict) -> StatusResult:
    tax_type = (item.get("tax_type") or "").strip()
    b_stt_cd = (item.get("b_stt_cd") or "").strip()

    if _NOT_REGISTERED in tax_type or (not b_stt_cd and not item.get("b_stt")):
        return StatusResult(
            b_no=b_no,
            registered=False,
            tax_type=tax_type,
            error="국세청 미등록 번호",
        )

    return StatusResult(
        b_no=b_no,
        registered=True,
        b_stt=(item.get("b_stt") or _STATUS_LABEL.get(b_stt_cd, "")).strip(),
        b_stt_cd=b_stt_cd,
        tax_type=tax_type,
        end_dt=(item.get("end_dt") or "").strip(),
        tax_type_change_dt=(item.get("tax_type_change_dt") or "").strip(),
    )


def result_to_row(res: StatusResult) -> dict:
    """CSV/Excel 출력용 dict 변환."""
    row = asdict(res)
    row["is_active"] = res.is_active
    return row
