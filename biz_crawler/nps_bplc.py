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
  - 글자 유사도만으로 0.6 을 넘는 매칭은 대부분 다른 회사다 (10/22 사람 검토: '에이치' · '이앤씨' 같은
    영문 이니셜 상호끼리 0.6~0.7). judge() 는 완전일치 · 포함(짧은 쪽 4글자 이상) · 지점 제외 일치만 확정하고,
    글자 유사도로만 0.6 이상이면 두 이름이 업종 명사(어린이집 · 건설 …)를 공유할 때 업종만
    (industry_only — 주소 · 우편번호 미사용). 글자 유사도는 0.85 를 넘어도 확정하지 않는다
    ('가나다어린이집' · '가나라어린이집' 0.86 — 다른 어린이집)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import http_client, odcloud_service_key
from .namematch import core, normalize, similarity, strip_org_raw

log = logging.getLogger(__name__)

API_BASE = "https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2"
MIN_SCORE = 0.6

# 상호에 들어 있으면 업종을 알 수 있는 명사 (10/22 사용자 확정). 컴퍼니 · 코리아 · 산업 · 개발 · 테크 ·
# 이앤씨 같은 말은 업종을 알 수 없어 넣지 않는다. 긴 것부터 대조한다.
INDUSTRY_NOUNS = tuple(sorted(set("""
어린이집 유치원 어학원 학원 교습소 아카데미
의원 치과 한의원 병원 내과 외과 안과 이비인후과 피부과 약국 동물병원 조리원 기공소 한약
요양원 요양센터 요양병원 복지센터 재가센터 보호센터 아동센터 발달센터 상담센터 복지관 공동생활가정
건설 건축 토건 건업 조경 주택 인테리어 설비 전기 전력 통신 정보통신 플랜트 중기 철거 도장 방수
금속 정밀 기계 전자 식품 스틸 공업사 화학 섬유 인쇄 목재 가구 제약
물류 화물 운수 용달 택배 해운 로지스 익스프레스 렌트카 자동차 모터스 카센터 정비
대부 투자 자산관리 손해사정 보험 세무 회계 법률 특허 노무 컨설팅 감정평가 금고 신용 캐피탈
마트 유통 푸드 수산 축산 농산 식당 카페 커피 치킨 베이커리 제과 반찬 정육
시스템 소프트 솔루션 디자인 미디어 스튜디오 정보기술 광고 출판
부동산 중개 공인중개 아파트 관리 환경 에너지 태양광 청소 세탁 경비 미용 헤어 네일 피트니스 호텔 모텔 펜션 여행
""".split()), key=len, reverse=True))

RESULT_FIELDS = ["nps_status", "nps_wkpl_name", "nps_code", "nps_industry",
                 "nps_match_score", "nps_active", "nps_error", "nps_address"]

# 업종을 쓸 수 있는 판정 — industry_only 는 업종만 (주소 · 우편번호 · 지역 검증 기준으로는 안 쓴다)
NPS_USABLE = ("matched", "industry_only")

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


def shared_noun(a: str, b: str) -> str:
    """두 상호가 함께 가진 업종 명사 (없으면 '')."""
    na, nb = normalize(a), normalize(b)
    return next((w for w in INDUSTRY_NOUNS if w in na and w in nb), "")


def _same_name(a: str, b: str) -> bool:
    """완전일치 · 포함(짧은 쪽 4글자 이상) · 지점 표기 뗀 일치 — 글자 유사도가 아닌 일치."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 4 and short in long_:     # 3글자 이하는 '에이치' ⊂ '에이치디' 같은 우연 포함이 많다
        return True
    ca, cb = core(a), core(b)
    return bool(ca) and ca == cb


def judge(names, wkpl_name: str) -> tuple[str, float]:
    """우리 상호들(names) 과 사업장명 → (matched / industry_only / name_mismatch, 최고 유사도)."""
    names = [n for n in names if n]
    score = max((similarity(n, wkpl_name) for n in names), default=0.0)
    if any(_same_name(n, wkpl_name) for n in names):
        return "matched", score
    if score >= MIN_SCORE and any(shared_noun(n, wkpl_name) for n in names):
        return "industry_only", score
    return "name_mismatch", score


@dataclass(slots=True)
class NpsResult:
    status: str                 # matched / industry_only / name_mismatch / not_found / error
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

    def lookup(self, b_no: str, name: str, alt_names: tuple[str, ...] = ()) -> NpsResult:
        """사업자번호 앞 6자리 + 상호명으로 사업장을 찾고 업종을 가져온다.

        검색은 name 으로, 채점은 name · alt_names 중 최고점으로 (judge).
        """
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
        names = (name, *alt_names)
        best = max(items, key=lambda i: (judge(names, _site_name(i.get("wkplNm", "")))[1],
                                         i.get("wkplJnngStcd") == "1"))
        status, score = judge(names, _site_name(best.get("wkplNm", "")))
        if status == "name_mismatch":
            return NpsResult(status="name_mismatch", score=score)

        try:
            detail = self._get("getDetailInfoSearchV2", seq=best["seq"])
        except Exception as exc:
            return NpsResult(status="error", score=score,
                             error=f"{type(exc).__name__}: {exc}"[:120])
        d = detail[0] if detail else {}
        return NpsResult(status=status, wkpl_name=_site_name(best.get("wkplNm", "")),
                         code=str(d.get("wkplIntpCd") or ""),
                         industry=str(d.get("vldtVlKrnNm") or "").strip(),
                         score=score, active=str(best.get("wkplJnngStcd") or ""),
                         # 업종만 쓰는 매칭은 다른 회사일 수 있어 주소를 남기지 않는다 (지역 검증 · 병합에 안 쓰이게)
                         address="" if status == "industry_only" else
                         str(d.get("wkplRoadNmDtlAddr") or best.get("wkplRoadNmDtlAddr") or "").strip())
