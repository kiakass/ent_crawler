"""NAVER 지역검색(Local) 클라이언트 — NAVER API HUB (NCP) 경유.

이 단계의 목적은 전화번호가 아니라 **방문할 웹주소(link) 확보**다.
telephone 필드는 스펙에 남아 있으나 실측 전건 빈 값이므로 쓰지 않는다
(설계서 3.1 참고). link 는 스텝 4 홈페이지 크롤링의 입력이 된다.

개발자센터(openapi.naver.com)는 2026-07-31 신규 발급 종료 → NCP 로 이관됨.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Iterable, Iterator

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from .config import ConfigError, http_client
from .namematch import similarity, strip_branch_raw, strip_org_raw, strip_tags

log = logging.getLogger(__name__)

API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/local"

# 지역검색은 한 번에 최대 5건만 돌려준다.
MAX_DISPLAY = 5

# 이 점수 미만이면 다른 업소로 보고 채택하지 않는다.
DEFAULT_MIN_SCORE = 0.60

RESULT_FIELDS = [
    "naver_status",
    "naver_title",
    "naver_link",
    "naver_category",
    "naver_address",
    "naver_road_address",
    "naver_match_score",
    "naver_candidates",
]


@dataclass(slots=True)
class LocalItem:
    title: str
    link: str
    category: str
    address: str
    road_address: str

    @classmethod
    def parse(cls, raw: dict) -> "LocalItem":
        return cls(
            title=strip_tags(raw.get("title")),
            link=(raw.get("link") or "").strip(),
            category=(raw.get("category") or "").strip(),
            address=(raw.get("address") or "").strip(),
            road_address=(raw.get("roadAddress") or "").strip(),
        )


@dataclass(slots=True)
class NaverResult:
    query: str
    status: str                  # matched / low_score / no_result / error
    item: LocalItem | None = None
    score: float = 0.0
    candidates: int = 0
    error: str = ""

    @property
    def matched(self) -> bool:
        return self.status == "matched"

    def to_row(self) -> dict:
        it = self.item
        return {
            "naver_status": self.status,
            "naver_title": it.title if it else "",
            "naver_link": it.link if it else "",
            "naver_category": it.category if it else "",
            "naver_address": it.address if it else "",
            "naver_road_address": it.road_address if it else "",
            "naver_match_score": self.score or "",
            "naver_candidates": self.candidates,
        }


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        # 429 는 50 RPS 초과. 백오프로 해소된다.
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def naver_keys() -> list[tuple[str, str, str]]:
    """NAVER 키 목록 — (한도 장부 이름, KEY_ID, KEY). 키1 은 'naver', 키2 는 'naver@2'.

    키2(NCP_APIGW_KEY_ID_2 · NCP_APIGW_KEY_2)는 선택이다. 장부를 키별로 따로 세고
    배치는 키1 예산을 다 쓰면 키2 로 넘어간다 (10/16).
    """
    keys = [("naver", os.getenv("NCP_APIGW_KEY_ID", "").strip(), os.getenv("NCP_APIGW_KEY", "").strip())]
    kid2, key2 = os.getenv("NCP_APIGW_KEY_ID_2", "").strip(), os.getenv("NCP_APIGW_KEY_2", "").strip()
    if kid2 and key2:
        keys.append(("naver@2", kid2, key2))
    return keys


class NaverLocalClient:
    def __init__(
        self,
        key_id: str | None = None,
        key: str | None = None,
        *,
        delay: float = 0.15,
        max_attempts: int = 3,
        min_score: float = DEFAULT_MIN_SCORE,
    ) -> None:
        self._kid = key_id or os.getenv("NCP_APIGW_KEY_ID", "").strip()
        self._key = key or os.getenv("NCP_APIGW_KEY", "").strip()
        if not self._kid or not self._key:
            raise ConfigError(
                "NCP_APIGW_KEY_ID / NCP_APIGW_KEY 가 비어 있습니다. .env 를 확인하세요."
            )
        self._delay = delay
        self._max_attempts = max_attempts
        self.calls = 0  # 재시도 포함 실제 HTTP 시도 수 (한도 장부용)
        self.min_score = min_score
        self._client = http_client(
            headers={
                "X-NCP-APIGW-API-KEY-ID": self._kid,
                "X-NCP-APIGW-API-KEY": self._key,
            }
        )

    def __enter__(self) -> "NaverLocalClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def search(self, query: str, *, display: int = MAX_DISPLAY) -> list[LocalItem]:
        """지역검색 원시 호출. 결과 없으면 빈 리스트."""

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
                params={"query": query, "display": min(display, MAX_DISPLAY)},
            )
            resp.raise_for_status()
            return resp.json()

        payload = _send()
        # total 은 항상 5로 고정되어 오므로 성공 판정에 쓸 수 없다. items 로만 본다.
        return [LocalItem.parse(x) for x in (payload.get("items") or [])]

    def resolve(self, name: str, *, region: str = "") -> NaverResult:
        """상호명으로 검색해 가장 그럴듯한 업소 1건을 고른다.

        region 이 있으면 질의에 붙여 정밀도를 높인다(PRD 3.1). 후보 중
        상호명 유사도가 가장 높은 것을 고르고, 동점이면 주소에 region 을
        포함한 쪽을 우선한다.
        """
        name = (name or "").strip()
        if not name:
            return NaverResult(query="", status="error", error="상호명 없음")

        # '주식회사에이제이팜' 은 NAVER 에서 0건, '에이제이팜' 은 적중 (10/08 사용자 확인) — 법인 표기를 떼고 검색.
        # 채점은 아래에서 원문 상호로 한다.
        base = strip_org_raw(name) or name
        query = f"{base} {region}".strip() if region else base
        try:
            items = self.search(query)
        except Exception as exc:
            return NaverResult(query=query, status="error",
                               error=f"{type(exc).__name__}: {exc}")

        # 지역 키워드를 붙여 0건이면 상호명 단독으로 한 번 더 시도한다.
        if not items and region:
            try:
                query = base
                items = self.search(query)
            except Exception as exc:
                return NaverResult(query=query, status="error",
                                   error=f"{type(exc).__name__}: {exc}")

        if not items:
            return NaverResult(query=query, status="no_result", candidates=0)

        def rank(it: LocalItem) -> tuple[float, int]:
            bonus = 1 if region and region in (it.road_address or it.address) else 0
            return (similarity(name, it.title), bonus)

        best = max(items, key=rank)
        score = similarity(name, best.title)
        status = "matched" if score >= self.min_score else "low_score"
        return NaverResult(query=query, status=status, item=best,
                           score=score, candidates=len(items))

    def resolve_many(
        self, pairs: Iterable[tuple[str, str]]
    ) -> Iterator[NaverResult]:
        """(상호명, 지역키워드) 목록을 순차 조회한다."""
        first = True
        for name, region in pairs:
            if not first and self._delay:
                time.sleep(self._delay)
            first = False
            yield self.resolve(name, region=region)

    def resolve_hard(self, name: str, *, region: str = "") -> NaverResult:
        """여러 질의 변형을 모두 돌려 후보를 모은 뒤 한 번만 채점한다.

        resolve() 로 못 찾은 행의 재시도용. 호출 수가 변형 개수만큼
        늘어나지만(최대 8회) 무료 한도가 월 775,000 건이라 문제 없다.
        채점 기준은 항상 원문 상호명이므로 resolve() 보다 나쁜 결과가
        나올 수는 없다.
        """
        name = (name or "").strip()
        if not name:
            return NaverResult(query="", status="error", error="상호명 없음")

        variants = query_variants(name, region)
        pool = _Pool()
        last_error = ""
        for i, q in enumerate(variants):
            if i and self._delay:
                time.sleep(self._delay)
            try:
                pool.extend(self.search(q))
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("변형 질의 실패 (%s): %s", q, last_error)

        if not len(pool):
            status = "error" if last_error and not pool.n_queries else "no_result"
            return NaverResult(query=variants[0] if variants else name,
                               status=status, candidates=0, error=last_error)

        best, score = pool.best(name, region)
        status = "matched" if score >= self.min_score else "low_score"
        return NaverResult(query=variants[0], status=status, item=best,
                           score=score, candidates=len(pool))

    def resolve_many_hard(
        self, pairs: Iterable[tuple[str, str]]
    ) -> Iterator[NaverResult]:
        for name, region in pairs:
            yield self.resolve_hard(name, region=region)



# ── 재시도용 질의 변형 ───────────────────────────────────────

def query_variants(name: str, region: str = "") -> list[str]:
    """한 상호명에서 검색 질의 후보들을 만든다.

    지역검색은 공백 없는 긴 합성 상호명에 약하다(미매칭 150건 중 93%가
    공백 없음). 법인 표기 제거·지점 표기 제거·앞부분 절단을 차례로
    시도해 후보를 늘린다. 절단형은 브랜드 앞부분만 남기므로 엉뚱한
    업소가 섞이는데, 채점은 항상 원문 전체로 하므로 위험하지 않다.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(q: str) -> None:
        q = " ".join((q or "").split())
        if len(q) >= 2 and q not in seen:
            seen.add(q)
            out.append(q)

    base = (name or "").strip()
    if not base:
        return out

    if region:
        add(f"{base} {region}")
    add(base)

    no_org = strip_org_raw(base)
    add(no_org)
    if region and no_org != base:
        add(f"{no_org} {region}")

    add(strip_branch_raw(no_org))

    # 공백이 있으면 첫 토큰(브랜드)만으로도 본다.
    if " " in no_org:
        add(no_org.split()[0])

    # 공백 없는 긴 이름은 앞부분 절단으로 브랜드를 노린다.
    compact = no_org.replace(" ", "")
    if len(compact) >= 8:
        add(compact[:6])
        add(compact[:4])
    elif len(compact) >= 6:
        add(compact[:4])

    return out


class _Pool:
    """여러 질의의 후보를 모아 원문 기준으로 한 번만 채점한다."""

    __slots__ = ("_by_key", "n_queries")

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], LocalItem] = {}
        self.n_queries = 0

    def extend(self, items: Iterable[LocalItem]) -> None:
        self.n_queries += 1
        for it in items:
            self._by_key.setdefault((it.title, it.road_address or it.address), it)

    def __len__(self) -> int:
        return len(self._by_key)

    def best(self, name: str, region: str) -> tuple[LocalItem | None, float]:
        if not self._by_key:
            return None, 0.0

        def rank(it: LocalItem) -> tuple[float, int, int]:
            bonus = 1 if region and region in (it.road_address or it.address) else 0
            # link 가 스텝 4의 입력이므로 동점이면 link 있는 쪽을 택한다.
            has_link = 1 if it.link else 0
            return (similarity(name, it.title), bonus, has_link)

        best = max(self._by_key.values(), key=rank)
        return best, similarity(name, best.title)
