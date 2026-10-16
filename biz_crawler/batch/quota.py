"""API 일일 한도 장부 (design/작업계획서.md §6.3).

한도는 API 키 단위라 잡 공용 DB(registry.db)의 api_usage 에 KST 날짜별로 센다.
행 수가 아니라 **실제 HTTP 시도 수**(tenacity 재시도 포함)를 센다 — 각 클라이언트의
`calls` 카운터 증가분을 기록한다.

예산 = 일 한도 × 95% − 오늘 사용량. 나머지 5% 는 수동 확인과 오차용이다.
같은 날 배치를 여러 번 돌려도 장부를 공유하므로 한도를 넘지 않는다.
"""

from __future__ import annotations

import math
import os

from .store import Registry, today_kst

# 일 한도 (2026-10-08 확인 — 작업계획서 §3.1)
DAILY_LIMIT = {
    "naver": 25_000,      # NCP 지역검색 (문서)
    "ftc": 10_000,        # 공정위 통신판매 (x-ratelimit-limit 헤더)
    "nps": 10_000,        # 국민연금 사업장 (x-ratelimit-limit 헤더)
    "kcw": 10_000,        # 근로복지공단 고용·산재 (x-ratelimit-limit 헤더, 10/16)
    "nts": 1_000_000,     # 국세청 상태조회 (문서)
    "juso": 200_000,      # 도로명주소 검색 (개발키 — 한도 미공개·헤더 없음, 보수적 자체 상한)
}
SAFETY = 0.95
# 다른 키로 시험할 때(.env.test) 장부를 키별로 따로 센다: QUOTA_TAG_NAVER=test1016 → 'naver@test1016'.
# 한도 자체(DAILY_LIMIT)는 같다 — 키마다 같은 상품이라서다.


def ledger_name(api: str) -> str:
    tag = os.getenv(f"QUOTA_TAG_{api.upper()}", "").strip()
    return f"{api}@{tag}" if tag else api


# 이만큼 연속으로 429 가 나면 그날 그 API 를 멈춘다 (장부와 실제 한도가 어긋난 것)
STOP_AFTER_429 = 20


class QuotaExhausted(RuntimeError):
    pass


class Quota:
    def __init__(self, registry: Registry, *, caps: dict[str, int] | None = None,
                 safety: float = SAFETY) -> None:
        self._reg = registry
        # safety: 일 한도 대비 쓸 비율. 시험 실행만 여유분(--use-reserve, 0.98)을 쓴다 — 실제 한도는 넘지 않는다
        self._safety = min(safety, 0.99)
        # caps: 테스트·수동 운영용 상한. 기본 예산보다 크게 줄 수는 없다
        self._caps = caps or {}
        self._streak_429: dict[str, int] = {}
        self.stopped: dict[str, str] = {}

    def daily_budget(self, api: str) -> int:
        base = math.floor(DAILY_LIMIT[api] * self._safety)
        cap = self._caps.get(api)
        return min(base, cap) if cap is not None else base

    def used(self, api: str, day: str | None = None) -> int:
        row = self._reg.conn.execute("SELECT calls FROM api_usage WHERE day=? AND api=?",
                                     (day or today_kst(), ledger_name(api))).fetchone()
        return int(row["calls"]) if row else 0

    def remaining(self, api: str) -> int:
        return max(0, self.daily_budget(api) - self.used(api))

    def can(self, api: str, n: int = 1) -> bool:
        return api not in self.stopped and self.remaining(api) >= n

    def record(self, api: str, calls: int, *, e429: bool = False, error: bool = False) -> None:
        if calls <= 0 and not (e429 or error):
            return
        self._reg.conn.execute(
            "INSERT INTO api_usage(day, api, calls, e429, errors) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(day, api) DO UPDATE SET calls=calls+excluded.calls, "
            "e429=e429+excluded.e429, errors=errors+excluded.errors",
            (today_kst(), ledger_name(api), max(0, calls), int(e429), int(error)))
        if e429:
            self._streak_429[api] = self._streak_429.get(api, 0) + 1
            if self._streak_429[api] >= STOP_AFTER_429:
                self.stopped[api] = f"429 연속 {STOP_AFTER_429}회 — 한도 초과로 보고 오늘 정지"
        else:
            self._streak_429[api] = 0
