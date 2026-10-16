"""배치 단계 실행기 — 기존 클라이언트를 그대로 쓰고 상태 전이 · 한도만 더한다.

원칙 (design/작업계획서.md §6.1 · §6.2):
  - 단계는 서로 기다리지 않는다. 각 단계는 자기 대상(pending)만 처리하고,
    한도가 모자라면 남은 행을 그대로 두고(다음 실행이 이어받음) 사유를 돌려준다
  - 다음 단계는 "필요한 항목이 하나라도 확보되면" 시작한다. 크롤링은 공정위
    도메인이든 NAVER 링크든 URL 이 생기는 순간 pending 이 된다
  - 늦게 온 값으로 더 나은 URL 이 생기면 다시 크롤링한다. 새 URL 에서 연락처를
    못 찾으면 이전 값을 지우지 않는다
  - 값이 바뀐 행은 dirty=1 → 병합 단계가 그 행만 다시 계산한다

모든 단계는 StageResult 를 돌려주고, 호출 수는 클라이언트 `calls` 증가분으로 센다.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..config import PROJECT_ROOT, http_client, odcloud_service_key
from ..merge import crawl_target
from ..namematch import similarity
from ..region import known_address, sigungu
from ..schema import ALL_FIELDS
from .quota import Quota
from .store import JobStore, Registry, is_transient_crawl_error, now_kst, today_kst

log = logging.getLogger(__name__)

COMMIT_EVERY = 100
COMMIT_SECONDS = 5.0
LOG_EVERY = 500          # 진행 로그 간격 (행)
LOG_SECONDS = 60.0       # 진행 로그 간격 (초) — 둘 중 먼저 오는 쪽
CLOSED = ("휴업자", "폐업자")
NPS_FILE_DIR = PROJECT_ROOT / "data" / "external" / "nps"
FTC_DUMP_PAGE_ROWS = 10_000          # 공정위 API 페이지 최대 (2026-10-08 실측)
FTC_DUMP_MAX_AGE_DAYS = 30
CRAWL_RETRY_DELAYS = (timedelta(days=1), timedelta(days=7))   # 일시 오류 재시도 간격
CRAWL_CLAIM_TIMEOUT = timedelta(minutes=30)

_COLUMNS = set(ALL_FIELDS)


@dataclass
class StageResult:
    stage: str
    rows_done: int = 0
    calls: int = 0
    status: str = "ok"            # ok · partial(제약으로 남김) · skipped · error
    stop_reason: str = ""
    extra: dict = field(default_factory=dict)


def _schema_only(d: dict) -> dict:
    return {k: v for k, v in d.items() if k in _COLUMNS}


class _Progress:
    """긴 단계의 진행 로그 — LOG_EVERY 행 또는 LOG_SECONDS 초마다 한 줄."""

    def __init__(self, label: str = "", total: int = 0, info=None) -> None:
        self.label, self.total, self.info = label, total, info
        self.n, self.t0, self.last = 0, time.monotonic(), time.monotonic()

    def tick(self, k: int = 1) -> None:
        self.n += k
        now = time.monotonic()
        if not self.label or (self.n % LOG_EVERY and now - self.last < LOG_SECONDS
                              and self.n != self.total):
            return
        self.last = now
        rate = self.n / max(now - self.t0, 1e-6)
        left = (self.total - self.n) / rate / 60 if self.total and rate else 0
        pct = f" ({self.n / self.total:.0%})" if self.total else ""
        extra = f" · {self.info()}" if self.info else ""
        el = int(now - self.t0)
        elapsed = f"{el // 3600}:{el % 3600 // 60:02d}:{el % 60:02d}"
        log.info("  %s %s/%s%s · 경과 %s · %.1f건/초 · 남은 약 %.0f분%s", self.label, f"{self.n:,}",
                 f"{self.total:,}" if self.total else "?", pct, elapsed, rate, left, extra)


class _Tx:
    """100행 또는 5초마다 커밋한다. 중간에 죽어도 잃는 것은 마지막 100행 이하.

    API 를 부르는 동안 쓰기 잠금을 오래 쥐면 상주 크롤러가 기다리므로 시간으로도 끊는다.
    label 을 주면 진행 로그도 남긴다 (phase() 로 단계 안에서 바꿀 수 있다).

    hold=False: 외부 API 를 부르는 단계용. 트랜잭션을 열지 않고 행마다 바로 커밋한다.
    (10/09~12 운영: 트랜잭션을 연 채 API 재시도가 60초를 넘겨 크롤러가 'database is locked' 로 164회 죽었다)
    """

    def __init__(self, store: JobStore, label: str = "", total: int = 0, info=None,
                 hold: bool = True) -> None:
        self.store, self.n, self.t, self.hold = store, 0, 0.0, hold
        self.prog = _Progress(label, total, info)

    def __enter__(self) -> "_Tx":
        if self.hold:
            self.store.begin()
        self.t = time.monotonic()
        return self

    def phase(self, label: str, total: int, info=None) -> None:
        self.prog = _Progress(label, total, info or self.prog.info)

    def tick(self) -> None:
        self.n += 1
        self.prog.tick()
        if not self.hold:
            return
        if self.n % COMMIT_EVERY == 0 or time.monotonic() - self.t > COMMIT_SECONDS:
            self.store.commit()
            self.store.begin()
            self.t = time.monotonic()

    def __exit__(self, *exc: object) -> None:
        self.store.commit()


# ── 크롤링 대상 갱신 (항목 기준 진행 · 늦게 온 값 반영) ──────────

def refresh_crawl_target(store: JobStore, row: dict) -> None:
    """공정위·NAVER 값이 바뀐 행의 크롤링 상태를 다시 정한다."""
    url, kind, _ = crawl_target(row)
    state = row.get("crawl_state") or "waiting"
    if not url:
        return                                    # 아직 URL 없음 — waiting 유지
    if kind == "marketplace":
        if state in ("waiting", ""):
            store.update(row["row_no"], {"crawl_state": "skipped", "crawl_url_kind": kind})
        return
    if url != (row.get("crawl_url") or "") and state != "running":
        # 처음 생긴 URL 이거나, 더 우선순위 높은 URL 이 늦게 왔다
        store.update(row["row_no"], {"crawl_state": "pending", "crawl_tries": "0",
                                     "crawl_next_at": ""})


# ── 01 국세청 ───────────────────────────────────────────────

def stage_nts(store: JobStore, quota: Quota) -> StageResult:
    from ..nts_status import MAX_BATCH, NtsStatusClient

    res = StageResult("nts")
    targets = store.select("nts_state='pending'")
    if not targets:
        res.status = "skipped"
        return res
    with NtsStatusClient() as client, _Tx(store, "국세청", len(targets), hold=False) as tx:
        for i in range(0, len(targets), MAX_BATCH):
            chunk = targets[i:i + MAX_BATCH]
            if not quota.can("nts", 1):
                res.status, res.stop_reason = "partial", quota.stopped.get("nts", "국세청 일 예산 소진")
                break
            before = client.calls
            results = client.check_batch([r["b_no"] for r in chunk])
            quota.record("nts", client.calls - before)
            for row, st in zip(chunk, results):
                store.update(row["row_no"], {
                    "nts_registered": st.registered, "nts_is_active": st.is_active,
                    "nts_b_stt": st.b_stt, "nts_b_stt_cd": st.b_stt_cd,
                    "nts_tax_type": st.tax_type, "nts_end_dt": st.end_dt,
                    "nts_error": st.error,
                    # 네트워크 오류면 다음 실행에서 다시 — 미등록·정상 응답이면 끝
                    "nts_state": "pending" if st.error and "HTTP" in st.error else "done",
                })
                tx.tick()
                res.rows_done += 1
        res.calls = client.calls
    return res


# ── 02 공정위 — 전체 다운로드 · 조인 · (소규모면) 건별 조회 ─────────

def _ftc_fields(item: dict) -> dict:
    from ..ftc_mllbs import _FIELD_MAP, clean_value   # 대표자 필드(_RPRSV_MAP)는 쓰지 않는다

    return {v: clean_value(v, item.get(k)) for k, v in _FIELD_MAP.items()}


def ftc_dump(registry: Registry, quota: Quota, *, max_pages: int | None = None) -> StageResult:
    """공정위 전체를 페이지 단위로 받아 ftc_registry 에 쌓는다. 페이지마다 체크포인트."""
    from ..ftc_mllbs import API_URL

    res = StageResult("ftc_dump")
    last_done = registry.get("ftc_dump_done")
    if last_done and (now_kst().date() - date.fromisoformat(last_done)).days < FTC_DUMP_MAX_AGE_DAYS:
        res.status = "skipped"
        res.extra["done_at"] = last_done
        return res
    page = int(registry.get("ftc_dump_page", "0")) + 1
    total_pages = int(registry.get("ftc_dump_pages", "0")) or None
    key = odcloud_service_key()
    fetched = 0
    with http_client(timeout=120) as client:
        while total_pages is None or page <= total_pages:
            if max_pages is not None and fetched >= max_pages:
                res.status, res.stop_reason = "partial", f"max_pages={max_pages}"
                break
            if not quota.can("ftc", 1):
                res.status, res.stop_reason = "partial", quota.stopped.get("ftc", "공정위 일 예산 소진")
                break
            try:
                r = client.get(API_URL, params={"serviceKey": key, "pageNo": page,
                                                "numOfRows": FTC_DUMP_PAGE_ROWS,
                                                "resultType": "json"})
                quota.record("ftc", 1, e429=r.status_code == 429)
                res.calls += 1
                r.raise_for_status()
                payload = r.json()
            except Exception as exc:
                res.status, res.stop_reason = "partial", f"{type(exc).__name__}: {exc}"[:120]
                break
            if payload.get("resultCode") not in (None, "00"):
                res.status, res.stop_reason = "partial", f"resultCode {payload.get('resultCode')}"
                break
            if total_pages is None:
                total = int(payload.get("totalCount") or 0)
                total_pages = -(-total // FTC_DUMP_PAGE_ROWS)
                registry.put("ftc_dump_pages", str(total_pages))
                registry.put("ftc_dump_total", str(total))
            rows = []
            for it in payload.get("items") or []:
                brno = "".join(ch for ch in str(it.get("brno") or "") if ch.isdigit())
                if len(brno) != 10:
                    continue
                active = 0 if (it.get("operSttusCdNm") or "").strip() == "폐업" else 1
                rows.append((brno, active, str(it.get("dclrDate") or ""),
                             json.dumps(_ftc_fields(it), ensure_ascii=False)))
            # 같은 사업자의 신고가 여러 건이면 영업중 · 최신 신고건 (ftc_mllbs._pick_best 와 같은 기준)
            registry.conn.execute("BEGIN")
            registry.conn.executemany(
                "INSERT INTO ftc_registry(brno, active, dclr_date, fields) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(brno) DO UPDATE SET active=excluded.active, "
                "dclr_date=excluded.dclr_date, fields=excluded.fields "
                "WHERE (excluded.active, excluded.dclr_date) > (ftc_registry.active, ftc_registry.dclr_date)",
                rows)
            registry.put("ftc_dump_page", str(page))
            registry.conn.execute("COMMIT")
            res.rows_done += len(rows)
            fetched += 1
            if page % 10 == 0 or page == total_pages:
                log.info("  공정위 다운로드 %d/%d페이지 · 누적 %s건", page, total_pages, f"{res.rows_done:,}")
            page += 1
    if total_pages is not None and page > total_pages:
        registry.put("ftc_dump_done", today_kst())
        registry.put("ftc_dump_page", "0")          # 다음 갱신은 처음부터
        res.extra["complete"] = True
    res.extra.update(page=page - 1, total_pages=total_pages)
    return res


def stage_ftc(store: JobStore, registry: Registry, quota: Quota, *,
              api_max: int = 3_000) -> StageResult:
    """다운로드가 완료돼 있으면 로컬 조인, 대상이 적으면 건별 API, 아니면 다운로드."""
    from ..ftc_mllbs import FtcMllBsClient, clean_value

    res = StageResult("ftc")
    targets = store.select("ftc_state='pending'")
    if not targets:
        res.status = "skipped"
        return res

    if registry.get("ftc_dump_done"):
        res.extra["mode"] = "join"
        with _Tx(store, "공정위 조인", len(targets)) as tx:
            for row in targets:
                hit = registry.conn.execute("SELECT fields FROM ftc_registry WHERE brno=?",
                                            (row["b_no"],)).fetchone()
                if hit:
                    f = json.loads(hit["fields"])
                    # 다운로드가 자리표시 정리 이전 것이어도 조인 시점에 다시 거른다
                    upd = _schema_only({"ftc_found": True,
                                        **{f"ftc_{k}": clean_value(k, v) for k, v in f.items()}})
                    upd["ftc_error"] = ""
                else:
                    upd = {"ftc_found": False, "ftc_error": "통신판매업 미신고"}
                store.update(row["row_no"], {**upd, "ftc_state": "done"})
                refresh_crawl_target(store, {**row, **upd})
                tx.tick()
                res.rows_done += 1
        return res

    if len(targets) > api_max:
        # 대규모 잡: 다운로드를 먼저 끝낸다. 오늘 못 끝내면 다음 실행이 이어받는다.
        dump = ftc_dump(registry, quota)
        res.calls, res.extra["mode"] = dump.calls, "dump"
        res.extra.update(dump.extra)
        if dump.extra.get("complete"):
            joined = stage_ftc(store, registry, quota, api_max=api_max)
            res.rows_done = joined.rows_done
        else:
            res.status, res.stop_reason = "partial", dump.stop_reason or "다운로드 진행 중"
        return res

    res.extra["mode"] = "api"
    with FtcMllBsClient(delay=0.1) as client, \
            _Tx(store, "공정위 건별", len(targets), lambda: f"남은 예산 {quota.remaining('ftc'):,}",
                hold=False) as tx:
        for row in targets:
            if not quota.can("ftc", 3):
                res.status, res.stop_reason = "partial", quota.stopped.get("ftc", "공정위 일 예산 소진")
                break
            before = client.calls
            r = client.lookup(row["b_no"])
            quota.record("ftc", client.calls - before, e429="429" in r.error)
            transient = r.error and ("HTTP" in r.error or "Error" in r.error) and not r.found \
                and "미신고" not in r.error
            upd = _schema_only(r.to_row())
            store.update(row["row_no"], {**upd, "ftc_state": "pending" if transient else "done"})
            refresh_crawl_target(store, {**row, **upd})
            tx.tick()
            res.rows_done += 1
        res.calls = client.calls
    return res


# ── 02a 근로복지공단 — 상호 없는 행의 상호 · 주소 ─────────────────────

def stage_kcw(store: JobStore, quota: Quota, *, max_rows: int | None = None) -> StageResult:
    """입력 상호가 비어 있고 공정위도 상호를 주지 못한 행만 조회한다 (일 10,000회라 아낀다)."""
    from ..kcomwel import KcomwelClient

    res = StageResult("kcw")
    # 공정위가 끝난 행만 — 공정위 상호가 있으면 조회하지 않는다
    store.conn.execute("UPDATE rows SET kcw_state='skipped' WHERE kcw_state='pending' "
                       "AND ftc_state IN ('done','skipped') AND ftc_company_name<>''")
    targets = store.select("kcw_state='pending' AND ftc_state IN ('done','skipped')", limit=max_rows)
    if not targets:
        res.status = "skipped"
        return res
    hit = 0
    with KcomwelClient() as client, \
            _Tx(store, "근로복지공단", len(targets), lambda: f"찾음 {hit:,} · 남은 예산 {quota.remaining('kcw'):,}",
                hold=False) as tx:
        for row in targets:
            if not quota.can("kcw", 3):
                res.status, res.stop_reason = "partial", quota.stopped.get("kcw", "근로복지공단 일 예산 소진")
                break
            before = client.calls
            r = client.lookup(row["b_no"])
            quota.record("kcw", client.calls - before, e429="429" in r.error, error=r.status == "error")
            upd = r.to_row()
            store.update(row["row_no"], {**upd, "kcw_state": "pending" if r.status == "error" else "done"})
            hit += r.status == "matched"
            tx.tick()
            res.rows_done += 1
        res.calls = client.calls
    res.extra["matched"] = hit
    return res


# ── 02b 국민연금 — 월별 파일 조인 · API 보완 ───────────────────────

def latest_nps_file() -> Path | None:
    files = sorted(NPS_FILE_DIR.glob("*.csv"))
    return files[-1] if files else None


def _nps_index(path: Path) -> dict[str, list[tuple[str, str, str, str, str]]]:
    idx: dict[str, list[tuple[str, str, str, str, str]]] = {}
    with path.open(encoding="cp949", errors="replace", newline="") as f:
        rd = csv.reader(f)
        next(rd, None)
        # 컬럼: 1 사업장명 · 2 사업자등록번호(앞 6자리) · 3 가입상태(1 등록 2 탈퇴) ·
        #       5 지번주소 · 6 도로명주소 · 13 업종코드 · 14 업종코드명
        for r in rd:
            if len(r) > 14:
                idx.setdefault(r[2], []).append((r[1], r[3], r[13], r[14].strip(),
                                                 (r[6] or r[5]).strip(), r[4].strip()))
    return idx


def stage_nps(store: JobStore, quota: Quota, *, use_api: bool = True,
              max_rows: int | None = None) -> StageResult:
    from ..nps_bplc import MIN_SCORE, NpsClient, _site_name

    res = StageResult("nps")
    pending = store.select("nps_state='pending'")
    path = latest_nps_file()
    # 주소 컬럼 추가(10/08) 이전에 매칭된 행은 파일에서 주소만 채운다
    backfill = store.select("nps_status='matched' AND (nps_address='' OR nps_zip='')") if path else []
    if (pending or backfill) and path:
        idx = _nps_index(path)
        if backfill:
            with _Tx(store, "국민연금 주소 보충", len(backfill)) as tx:
                for row in backfill:
                    for c in idx.get(row["b_no"][:6], []):
                        if _site_name(c[0]) == row["nps_wkpl_name"]:
                            store.update(row["row_no"], {"nps_address": c[4], "nps_zip": c[5]})
                            break
                    tx.tick()
            res.extra["address_backfill"] = len(backfill)
        res.extra["file"] = path.name
        hit = 0
        with _Tx(store, "국민연금 파일", len(pending)) as tx:
            for row in pending:
                name = _known_name(row)
                if not name:        # 앞 6자리만으로는 확정할 수 없다 · 근로복지공단 대기 중이면 다음 실행에
                    if row.get("kcw_state") != "pending":
                        store.update(row["row_no"], {"nps_state": "skipped"}, dirty=False)
                    tx.tick()
                    continue
                cands = idx.get(row["b_no"][:6], [])
                best = max(cands, key=lambda c: (similarity(name, _site_name(c[0])), c[1] == "1"),
                           default=None)
                score = similarity(name, _site_name(best[0])) if best else 0.0
                if best and score >= MIN_SCORE:
                    upd = {"nps_status": "matched", "nps_wkpl_name": _site_name(best[0]),
                           "nps_code": best[2], "nps_industry": best[3],
                           "nps_match_score": f"{score:.2f}", "nps_active": best[1],
                           "nps_error": "", "nps_address": best[4], "nps_zip": best[5],
                           "nps_state": "done"}
                    hit += 1
                else:
                    upd = {"nps_status": "name_mismatch" if best else "not_found",
                           "nps_match_score": f"{score:.2f}" if score else "",
                           "nps_state": "api_pending" if use_api else "done"}
                store.update(row["row_no"], upd)
                tx.tick()
                res.rows_done += 1
        res.extra["file_hit"] = hit
    elif pending and not path:
        # 파일이 없으면 전부 API 로
        store.conn.execute("UPDATE rows SET nps_state='api_pending' WHERE nps_state='pending'")

    if not use_api:
        return res
    api_targets = store.select("nps_state='api_pending'", limit=max_rows)
    if not api_targets:
        return res
    api_hit = 0
    with NpsClient() as client, \
            _Tx(store, "국민연금 API", len(api_targets), lambda: f"남은 예산 {quota.remaining('nps'):,}",
                hold=False) as tx:
        for row in api_targets:
            if not quota.can("nps", 3):
                res.status, res.stop_reason = "partial", quota.stopped.get("nps", "국민연금 API 일 예산 소진")
                break
            name = _known_name(row)
            if not name:
                if row.get("kcw_state") != "pending":
                    store.update(row["row_no"], {"nps_state": "skipped"}, dirty=False)
                tx.tick()
                continue
            before = client.calls
            r = client.lookup(row["b_no"], name)
            quota.record("nps", client.calls - before, e429="429" in r.error)
            upd = r.to_row()
            if r.status == "matched":
                api_hit += 1
            elif row.get("nps_status") and r.status != "error":
                upd = {"nps_status": row["nps_status"]}          # 파일 판정 유지
            store.update(row["row_no"], {**upd,
                                         "nps_state": "api_pending" if r.status == "error" else "done"})
            tx.tick()
            res.rows_done += 1
        res.calls = client.calls
    res.extra["api_hit"] = api_hit
    return res


# ── 03 NAVER — 1차 · 재시도 ─────────────────────────────────

def _known_name(row: dict) -> str:
    """국민연금 채점용 상호 — 병합 결과 > 입력 > 공정위 > 근로복지공단 (사업자번호만 받은 목록)."""
    return (row.get("final_company_name") or row.get("input_name")
            or row.get("ftc_company_name") or row.get("kcw_name") or "").strip()


def _query_name(row: dict) -> str:
    """NAVER 질의 상호 — 입력 상호, 없으면 사업자번호로 확정된 공정위 · 근로복지공단 상호."""
    return (row.get("input_name") or row.get("ftc_company_name") or row.get("kcw_name") or "").strip()


def _region_hint(row: dict) -> str:
    """NAVER 지역 키워드 — 사업자번호로 확정된 주소(공정위 > 국민연금)의 시·군·구."""
    return sigungu(known_address(row))


class _NaverPool:
    """NAVER 키 여러 개 — 앞 키의 예산을 다 쓰면 다음 키로. 장부는 키별 (naver · naver@2)."""

    def __init__(self, quota: Quota, **kw) -> None:
        from ..naver_local import NaverLocalClient, naver_keys

        self._quota = quota
        self._slots = [(api, NaverLocalClient(kid, key, **kw)) for api, kid, key in naver_keys()]

    def __enter__(self) -> "_NaverPool":
        return self

    def __exit__(self, *exc: object) -> None:
        for _, c in self._slots:
            c.close()

    def take(self, n: int):
        """n 회를 쓸 수 있는 (장부 이름, 클라이언트). 모두 소진이면 None."""
        return next(((api, c) for api, c in self._slots if self._quota.can(api, n)), None)

    def remaining(self) -> int:
        return sum(self._quota.remaining(api) for api, _ in self._slots)

    def stop_reason(self) -> str:
        return next((self._quota.stopped[api] for api, _ in self._slots if api in self._quota.stopped),
                    f"NAVER 일 예산 소진 (키 {len(self._slots)}개)")

    @property
    def calls(self) -> int:
        return sum(c.calls for _, c in self._slots)


def stage_naver(store: JobStore, registry: Registry, quota: Quota, *, max_rows: int | None = None,
                retry: bool = True, delay: float = 0.15, min_score: float = 0.60) -> StageResult:
    from ..naver_local import query_variants

    res = StageResult("naver")
    # 휴·폐업으로 확인된 행은 대상에서 뺀다. 상태를 모르는 행은 기다리지 않고 진행.
    skipped = store.conn.execute(
        "UPDATE rows SET naver_state='skipped' WHERE naver_state IN ('pending','retry_pending') "
        f"AND nts_b_stt IN ({','.join('?' * len(CLOSED))})", CLOSED).rowcount
    res.extra["skipped_closed"] = skipped
    # 계속사업자 먼저, 상태 불명은 뒤로
    order = "CASE WHEN nts_b_stt='계속사업자' THEN 0 ELSE 1 END, row_no"
    p1 = store.select("naver_state='pending'", order=order, limit=max_rows)
    stats = {"p1": 0, "retry": 0, "cache_hit": 0, "matched": 0, "improved": 0}

    naver_info = lambda: (f"매칭 {stats['matched']:,} · 캐시 {stats['cache_hit']:,} · "  # noqa: E731
                          f"남은 예산 {pool.remaining():,}")
    with _NaverPool(quota, delay=delay, min_score=min_score) as pool, \
            _Tx(store, "NAVER 1차", len(p1), naver_info, hold=False) as tx:
        for row in p1:
            slot = pool.take(2)
            if slot is None:
                res.status, res.stop_reason = "partial", pool.stop_reason()
                break
            api, client = slot
            name, region = _query_name(row), _region_hint(row)
            if not name:
                # 공정위 · 근로복지공단이 끝났는데도 상호가 없으면 검색할 말이 없다. 대기 중이면 다음 실행에
                if row.get("ftc_state") in ("done", "skipped") \
                        and (row.get("kcw_state") or "skipped") in ("done", "skipped"):
                    store.update(row["row_no"], {"naver_state": "skipped"}, dirty=False)
                tx.tick()
                continue
            key = f"p1|{name}|{region}"
            cached = registry.cache_get(key)
            if cached is not None:
                out = cached
                stats["cache_hit"] += 1
            else:
                before = client.calls
                r = client.resolve(name, region=region)
                quota.record(api, client.calls - before, e429="429" in r.error,
                             error=r.status == "error")
                out = r.to_row()
                if r.status != "error":
                    registry.cache_put(key, out)
            state = "done" if out.get("naver_status") == "matched" else (
                "pending" if out.get("naver_status") == "error" else "retry_pending")
            store.update(row["row_no"], {**out, "naver_state": state})
            refresh_crawl_target(store, {**row, **out})
            stats["p1"] += 1
            stats["matched"] += out.get("naver_status") == "matched"
            tx.tick()

        # 재시도는 1차가 모두 끝난 뒤에만 (일정표 D9~)
        if retry and res.status == "ok" and store.count("naver_state='pending'") == 0 \
                and (max_rows is None or len(p1) < max_rows):
            retry_rows = store.select("naver_state='retry_pending'", order=order,
                                      limit=(max_rows - len(p1)) if max_rows else None)
            tx.phase("NAVER 재시도", len(retry_rows),
                     lambda: f"개선 {stats['improved']:,} · 남은 예산 {pool.remaining():,}")
            for row in retry_rows:
                name, region = _query_name(row), _region_hint(row)
                need =max(1, len(query_variants(name, region)))
                slot = pool.take(need)
                if slot is None:
                    res.status, res.stop_reason = "partial", pool.stop_reason()
                    break
                api, client = slot
                key = f"hard|{name}|{region}"
                cached = registry.cache_get(key)
                if cached is not None:
                    out = cached
                    stats["cache_hit"] += 1
                else:
                    before = client.calls
                    r = client.resolve_hard(name, region=region)
                    quota.record(api, client.calls - before, e429="429" in r.error,
                                 error=r.status == "error")
                    out = r.to_row()
                    if r.status != "error":
                        registry.cache_put(key, out)
                # 결과가 후퇴하지 않게: 점수가 오르거나 없던 링크가 생길 때만 덮어쓴다
                prev = float(row.get("naver_match_score") or 0)
                new = float(out.get("naver_match_score") or 0)
                gained_link = bool(out.get("naver_link")) and not (row.get("naver_link") or "").strip()
                if new > prev or gained_link:
                    store.update(row["row_no"], {**out, "naver_state": "done"})
                    refresh_crawl_target(store, {**row, **out})
                    stats["improved"] += 1
                else:
                    store.update(row["row_no"], {"naver_state": "done"}, dirty=False)
                stats["retry"] += 1
                tx.tick()
        res.calls = pool.calls
    res.rows_done = stats["p1"] + stats["retry"]
    res.extra.update(stats)
    return res


# ── 04 크롤링 (상주 워커가 반복 호출) ──────────────────────────

def _claim(store: JobStore, limit: int) -> list[dict]:
    now = now_kst()
    stale = (now - CRAWL_CLAIM_TIMEOUT).isoformat(timespec="seconds")
    store.begin()
    # 점유 후 30분이 지나도록 결과가 없으면 워커가 죽은 것 — 되돌린다
    store.conn.execute("UPDATE rows SET crawl_state='pending' WHERE crawl_state='running' "
                       "AND crawl_claimed_at < ?", (stale,))
    rows = store.select("crawl_state='pending' OR (crawl_state='failed_tmp' AND crawl_next_at <= ?)",
                        (now.isoformat(timespec="seconds"),), limit=limit)
    for r in rows:
        store.conn.execute("UPDATE rows SET crawl_state='running', crawl_claimed_at=? WHERE row_no=?",
                           (now.isoformat(timespec="seconds"), r["row_no"]))
    store.commit()
    return rows


def stage_crawl(store: JobStore, *, max_rows: int | None = None, delay: float = 0.5,
                timeout: float = 10.0, max_pages: int = 3) -> StageResult:
    """pending 크롤링 행을 100행씩 점유해 처리한다. 대상이 없으면 바로 돌아온다."""
    from ..homepage import HomepageCrawler

    res = StageResult("crawl")
    stats = {"found": 0, "kept_previous": 0, "failed_tmp": 0, "failed": 0}
    total = store.count("crawl_state='pending'")
    prog = _Progress("크롤링", min(total, max_rows) if max_rows else total,
                     lambda: f"연락처 확보 {stats['found']:,} · 실패 {stats['failed']:,}")
    platform = None
    try:
        with HomepageCrawler(delay=delay, timeout=timeout, max_pages=max_pages) as crawler:
            while max_rows is None or res.rows_done < max_rows:
                batch = _claim(store, min(COMMIT_EVERY, (max_rows or 10**9) - res.rows_done))
                if not batch:
                    break
                for row in batch:
                    url, kind, _ = crawl_target(row)
                    if not url or kind == "marketplace":
                        store.update(row["row_no"], {"crawl_state": "skipped"}, dirty=False)
                        continue
                    if kind == "platform":
                        if platform is None:
                            from ..platform import PlatformCrawler
                            platform = PlatformCrawler(timeout=max(timeout, 20.0))
                        r = platform.crawl(url)
                    else:
                        r = crawler.crawl(url)
                    out = r.to_row()
                    had = (row.get("crawl_phone") or row.get("crawl_email") or "").strip()
                    upd: dict = {"crawl_url": url, "crawl_url_kind": kind}
                    if r.found or not had:
                        upd.update(out)
                        stats["found"] += r.found
                    else:
                        # 새 URL 에서 못 찾음 — 이전 URL 에서 얻은 연락처를 지우지 않는다
                        upd["crawl_error"] = out["crawl_error"]
                        stats["kept_previous"] += 1
                    tries = int(row.get("crawl_tries") or 0)
                    if r.found or had:
                        upd["crawl_state"] = "done"
                    elif is_transient_crawl_error(r.error) and tries < len(CRAWL_RETRY_DELAYS):
                        upd["crawl_state"] = "failed_tmp"
                        upd["crawl_tries"] = str(tries + 1)
                        upd["crawl_next_at"] = (now_kst() + CRAWL_RETRY_DELAYS[tries]).isoformat(
                            timespec="seconds")
                        stats["failed_tmp"] += 1
                    else:
                        upd["crawl_state"] = "failed" if r.error != "연락처 미발견" else "done"
                        stats["failed"] += upd["crawl_state"] == "failed"
                    store.begin()
                    store.update(row["row_no"], upd)
                    store.commit()
                    res.rows_done += 1
                    prog.tick()
    finally:
        if platform:
            platform.close()
    res.extra.update(stats)
    return res


# ── 05 병합 ────────────────────────────────────────────────

def shared_url_counts(rows) -> dict[str, int]:
    """URL 별 공유 사업자 수 — 프랜차이즈 본사 · 대행사 블로그 판정용."""
    from collections import Counter

    from ..merge import url_key
    return Counter(k for k in (url_key(crawl_target(r)[0]) for r in rows) if k)


def stage_merge(store: JobStore) -> StageResult:
    from ..merge import SHARED_URL_MIN, merge_row, url_key

    res = StageResult("merge")
    stamp = now_kst().strftime("%Y-%m-%d %H:%M:%S")
    everyone = store.select("1=1")
    counts = shared_url_counts(everyone)
    # 바뀐 행 + 공유 여부가 바뀐 행 (다른 행이 같은 URL 을 얻어 기준을 넘긴 경우)
    # 사이트주소가 비워진 행(접속 실패로 제외 등)은 공유 비고가 붙지 않으므로 비교에서 뺀다 — 안 그러면 매시간 같은 행을 다시 병합
    rows = [r for r in everyone
            if r.get("dirty") == "1"
            or (r.get("final_homepage")
                and (counts.get(url_key(crawl_target(r)[0]), 0) >= SHARED_URL_MIN)
                != ("공유" in (r.get("site_note") or "")))]
    res.extra["shared_urls"] = sum(1 for v in counts.values() if v >= SHARED_URL_MIN)
    with _Tx(store, "병합" if len(rows) >= LOG_EVERY else "", len(rows)) as tx:
        for row in rows:
            row["_shared_url"] = counts.get(url_key(crawl_target(row)[0]), 0)
            merged = merge_row(row)
            store.update(row["row_no"], {**merged, "collected_at": stamp, "dirty": "0"}, dirty=False)
            tx.tick()
            res.rows_done += 1
    return res


# ── 05b 우편번호 ──────────────────────────────────────────

def resolve_zip(row: dict, client, registry: Registry, quota: Quota | None) -> dict | None:
    """최종 주소의 우편번호. 도로명주소 API(캐시) → 실패 시 같은 시·군·구의 국민연금 우편번호.

    한도가 모자라면 None (다음 실행에서 다시).
    """
    from ..juso import search_keyword
    from ..region import sido

    addr = (row.get("final_address") or "").strip()
    kcw_zip = (row.get("kcw_zip") or "").strip()
    if kcw_zip and addr == (row.get("kcw_address") or "").strip():
        return {"final_zipcode": kcw_zip, "zip_source": "근로복지공단", "zip_addr": addr}
    nps_zip = (row.get("nps_zip") or "").strip()
    if nps_zip and addr == (row.get("nps_address") or "").strip():
        # 최종 주소가 국민연금 주소면 국민연금 우편번호가 정확하다 (건물번호 없는 주소로 검색하면 '첫 결과' 추정)
        return {"final_zipcode": nps_zip, "zip_source": "국민연금", "zip_addr": addr}
    kw = search_keyword(addr)
    hit = registry.zip_get(kw)
    if hit is None:
        if quota is not None and not quota.can("juso", 3):
            return None
        before = client.calls
        r = client.zip_for(addr)
        if quota is not None:
            quota.record("juso", client.calls - before, error=r.status == "error")
        if r.status != "error":
            registry.zip_put(kw, r.zipcode, r.status)
        hit = (r.zipcode, r.status)
    zipcode, status = hit
    if zipcode:
        src = "도로명주소(첫 결과)" if status == "first" else "도로명주소"
        return {"final_zipcode": zipcode, "zip_source": src, "zip_addr": addr}
    nps_zip, nps_addr = (row.get("nps_zip") or "").strip(), row.get("nps_address") or ""
    if nps_zip and sido(nps_addr) == sido(addr) and sigungu(nps_addr) and sigungu(nps_addr) == sigungu(addr):
        return {"final_zipcode": nps_zip, "zip_source": "국민연금", "zip_addr": addr}
    return {"final_zipcode": "", "zip_source": f"찾지 못함({status})", "zip_addr": addr}


def stage_zip(store: JobStore, registry: Registry, quota: Quota) -> StageResult:
    """주소가 있고 그 주소로 아직 우편번호를 찾지 않은 행만."""
    from ..juso import JusoClient

    res = StageResult("zip")
    rows = store.select("final_address<>'' AND final_address<>zip_addr")
    if not rows:
        res.status = "skipped"
        return res
    found = 0
    with JusoClient() as client, \
            _Tx(store, "우편번호", len(rows), lambda: f"찾음 {found:,} · 남은 예산 {quota.remaining('juso'):,}",
                hold=False) as tx:
        for row in rows:
            upd = resolve_zip(row, client, registry, quota)
            if upd is None:
                res.status, res.stop_reason = "partial", quota.stopped.get("juso", "도로명주소 일 예산 소진")
                break
            found += bool(upd["final_zipcode"])
            store.update(row["row_no"], upd, dirty=False)
            tx.tick()
            res.rows_done += 1
        res.calls = client.calls
    res.extra["found"] = found
    return res


# ── 06 Export ─────────────────────────────────────────────

def stage_export(store: JobStore, out_dir: Path) -> StageResult:
    """누적 스냅샷(snapshot.csv) + 오늘 바뀐 행(delta_YYYYMMDD.csv)."""
    from scripts.export_result import HEADER, MAP

    res = StageResult("export")
    out_dir.mkdir(parents=True, exist_ok=True)
    day = today_kst()
    targets = {
        out_dir / "snapshot.csv": store.select("1=1"),
        out_dir / f"delta_{day.replace('-', '')}.csv": store.select("substr(updated_at,1,10)=?", (day,)),
    }
    for path, rows in targets.items():
        tmp = path.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            for r in rows:
                w.writerow([(r.get(MAP[h]) or "").strip() for h in HEADER])
        tmp.replace(path)
        res.extra[path.name] = len(rows)
    res.rows_done = len(targets[out_dir / "snapshot.csv"])
    return res
