#!/usr/bin/env python3
"""대량 일배치 실행기 (design/작업계획서.md §1 실행 계획 · §6 배치 설계).

    python scripts/batch.py import <csv> --job J [--limit N]   입력 적재 (인코딩 자동)
    python scripts/batch.py plan --job J                       호출량 · 예상 일수 (호출 0회)
    python scripts/batch.py daily --job J [--with-crawl]       하루치 실행 (launchd 00:10)
    python scripts/batch.py crawl-worker --job J [--once]      상주 크롤러 (launchd KeepAlive)
    python scripts/batch.py status --job J [--update-doc]      진행률 · 오늘 한도 · 실적 문서 갱신
    python scripts/batch.py export --job J                     스냅샷 · 오늘 델타
    python scripts/batch.py pause|resume --job J
    python scripts/batch.py ftc-dump [--max-pages N]           공정위 전체 다운로드 (이어받기)

단계는 서로 독립이다. 한 단계가 한도·장애로 멈춰도 다음 단계는 돈다.
일별 실적은 output/batch/<job>/daily_progress.csv 에 하루 1행 기록하고(집계값만),
작업계획서(design/작업계획서.md) §1.5 표의 실적 칸을 갱신한다.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import logging
import math
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from biz_crawler.batch import stages  # noqa: E402
from biz_crawler.batch.quota import DAILY_LIMIT, Quota  # noqa: E402
from biz_crawler.batch.store import (BATCH_ROOT, JobStore, Registry, now_kst,  # noqa: E402
                                     read_input, today_kst)
from biz_crawler.config import PROJECT_ROOT  # noqa: E402
from biz_crawler.logging_utils import RedactingFilter, install as install_logging  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")
log = logging.getLogger("batch")

OUT_ROOT = PROJECT_ROOT / "output" / "batch"
LOG_ROOT = PROJECT_ROOT / "logs" / "batch"

# 500건 실측 단가 (작업계획서 §6.4) — plan · eta 계산용
RATE = {"naver_target": 0.958, "p1_cpr": 1.024, "retry_rate": 0.306, "retry_cpr": 2.1,
        "crawl_rate": 0.316, "site_sec": 1.635}

PROGRESS_COLS = ["day", "date", "run_started", "run_ended", "run_status", "stop_reason",
                 "naver_calls", "naver_p1_done", "naver_retry_done", "naver_p1_cum",
                 "naver_retry_cum", "naver_matched_cum", "nps_api_calls", "crawl_done",
                 "crawl_found", "crawl_cum", "merged_rows", "fill_address", "fill_homepage",
                 "fill_phone", "fill_email", "fill_zipcode", "fill_industry", "deferred_rows", "failed_rows",
                 "plan_gap_naver_cum", "eta_date"]


# ── 공통 ────────────────────────────────────────────────────

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATEFMT = "%Y%m%d%H%M%S"   # YYYYMMDDHH24MISS


def _setup_logging(verbose: bool) -> None:
    install_logging(logging.DEBUG if verbose else logging.INFO)
    # 화면(·launchd stdout 로그)에도 시각을 찍는다
    for h in logging.getLogger().handlers:
        h.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_ROOT / f"{today_kst().replace('-', '')}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", LOG_DATEFMT))
    fh.addFilter(RedactingFilter())
    logging.getLogger().addHandler(fh)


class _Lock:
    """같은 잡의 같은 작업이 겹쳐 돌지 않게 한다 (launchd 재실행 · 수동 실행 겹침)."""

    def __init__(self, job: str, name: str) -> None:
        self.path = BATCH_ROOT / job / f"{name}.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = None

    def __enter__(self) -> "_Lock":
        self.fh = self.path.open("w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"이미 실행 중입니다: {self.path.name} — 중복 실행을 건너뜁니다")
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def _caps(values: list[str] | None) -> dict[str, int]:
    out = {}
    for v in values or []:
        api, _, n = v.partition("=")
        if api not in DAILY_LIMIT or not n.isdigit():
            raise SystemExit(f"--cap 형식: api=건수 (api: {', '.join(DAILY_LIMIT)}) — 받은 값 {v}")
        out[api] = int(n)
    return out


def _notify(title: str, message: str) -> None:
    try:
        subprocess.run(["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                       check=False, timeout=5, capture_output=True)
    except Exception:
        pass


def _run_stage(store: JobStore, name: str, fn) -> stages.StageResult:
    run_id = store.run_start(name)
    t = time.monotonic()
    try:
        res = fn()
    except Exception as exc:  # 한 단계의 예외가 다른 단계를 막지 않는다
        log.exception("단계 %s 예외", name)
        store.commit()
        res = stages.StageResult(name, status="error", stop_reason=f"{type(exc).__name__}: {exc}"[:200])
    store.run_end(run_id, rows_done=res.rows_done, calls=res.calls, status=res.status,
                  stop_reason=res.stop_reason, extra=res.extra)
    log.info("단계 %-8s %-7s 처리 %6d행 · 호출 %6d회 · %.0f초 %s %s", name, res.status, res.rows_done,
             res.calls, time.monotonic() - t, res.stop_reason, res.extra or "")
    return res


# ── import · plan ───────────────────────────────────────────

def cmd_import(args: argparse.Namespace) -> int:
    items = read_input(Path(args.input), bizno_col=args.bizno_col, name_col=args.name_col)
    if args.offset:
        items = items[args.offset:]
    if args.limit:
        items = items[: args.limit]
    store = JobStore(args.job)
    try:
        stats = store.import_rows(items, source=f"{Path(args.input).name} offset={args.offset} limit={args.limit}")
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    print(f"잡 {args.job} 적재: {stats['rows']:,}행 · 사업자번호 오류 {stats['bizno_invalid']:,} · "
          f"상호 빈 값 {stats['name_empty']:,}")
    print(f"  DB: {store.path.relative_to(PROJECT_ROOT)}")
    return 0


def _estimate(store: JobStore) -> dict:
    p1 = store.count("naver_state='pending'")
    rt = store.count("naver_state='retry_pending'")
    naver_calls = p1 * RATE["p1_cpr"] + (rt + p1 * RATE["retry_rate"]) * RATE["retry_cpr"]
    budget = math.floor(DAILY_LIMIT["naver"] * 0.95)
    days = math.ceil(naver_calls / budget) if naver_calls else 0
    return {"p1": p1, "rt": rt, "naver_calls": round(naver_calls), "days": days,
            "eta": (date.fromisoformat(today_kst()) + timedelta(days=max(days - 1, 0))).isoformat()}


def cmd_plan(args: argparse.Namespace) -> int:
    store = JobStore(args.job)
    n = store.count()
    e = _estimate(store)
    crawl = store.count("crawl_state IN ('pending','waiting')")
    print(f"잡 {args.job} — {n:,}행 (호출 없이 추정 · 단가는 500건 실측)")
    nts = store.count("nts_state='pending'")
    ftc = store.count("ftc_state='pending'")
    nps = store.count("nps_state IN ('pending','api_pending')")
    print(f"  국세청 대기 {nts:,}행 → {math.ceil(nts / 100):,}회")
    print(f"  공정위 대기 {ftc:,}행")
    print(f"  국민연금 대기 {nps:,}행 (파일 조인 우선)")
    print(f"  NAVER 1차 대기 {e['p1']:,}행 · 재시도 대기 {e['rt']:,}행 → 약 {e['naver_calls']:,}회 "
          f"= {e['days']}일 (하루 {math.floor(DAILY_LIMIT['naver'] * 0.95):,}회)")
    print(f"  크롤링 예상 약 {round(n * RATE['crawl_rate']):,}곳 · 순차 {n * RATE['crawl_rate'] * RATE['site_sec'] / 3600:.1f}시간 "
          f"(현재 대기·대상 미정 {crawl:,})")
    print(f"  예상 종료일 {e['eta']} (오늘 시작 기준)")
    return 0


# ── daily ───────────────────────────────────────────────────

def cmd_daily(args: argparse.Namespace) -> int:
    store = JobStore(args.job)
    if not store.meta("imported_at"):
        raise SystemExit(f"잡 {args.job} 이 적재되지 않았습니다. 먼저 import 하세요.")
    if store.meta("paused") == "1":
        print(f"잡 {args.job} 은 일시정지 상태입니다 (resume 으로 해제).")
        return 0
    with _Lock(args.job, "daily"):
        if not store.meta("start_date"):
            store.set_meta("start_date", today_kst())
        registry = Registry()
        quota = Quota(registry, caps=_caps(args.cap), safety=0.98 if args.use_reserve else 0.95)
        started = now_kst().isoformat(timespec="seconds")
        log.info("daily 시작 — 잡 %s · %s일차", args.job, _day_index(store))

        results = [
            _run_stage(store, "nts", lambda: stages.stage_nts(store, quota)),
            _run_stage(store, "ftc", lambda: stages.stage_ftc(store, registry, quota,
                                                               api_max=args.ftc_api_max)),
            _run_stage(store, "nps", lambda: stages.stage_nps(store, quota, use_api=not args.no_nps_api,
                                                               max_rows=args.max_rows)),
            _run_stage(store, "naver", lambda: stages.stage_naver(store, registry, quota,
                                                                   retry=not args.no_retry,
                                                                   max_rows=args.max_rows)),
        ]
        if args.with_crawl:
            results.append(_run_stage(store, "crawl", lambda: stages.stage_crawl(store, max_rows=args.max_rows)))
        results.append(_run_stage(store, "merge", lambda: stages.stage_merge(store)))
        results.append(_run_stage(store, "zip", lambda: stages.stage_zip(store, registry, quota)))
        results.append(_run_stage(store, "export", lambda: stages.stage_export(store, OUT_ROOT / args.job)))

        _write_progress(store, quota, started, results)
        doc = _doc_path(args.job)
        if doc:
            _update_doc(store, doc)
        errors = [r for r in results if r.status == "error"]
        if errors and args.notify:
            _notify(f"배치 {args.job} 오류", ", ".join(f"{r.stage}: {r.stop_reason[:40]}" for r in errors))
        registry.close()
    store.close()
    return 1 if errors else 0


# ── crawl-worker ────────────────────────────────────────────

def cmd_crawl_worker(args: argparse.Namespace) -> int:
    store = JobStore(args.job)
    with _Lock(args.job, "crawl"):
        last_merge = time.monotonic()
        while True:
            if store.meta("paused") == "1":
                if args.once:
                    break
                time.sleep(args.idle)
                continue
            try:
                res = stages.stage_crawl(store, max_rows=args.max_rows)
            except sqlite3.OperationalError as exc:
                # daily 가 쓰는 중이면 잠깐 기다렸다가 이어 간다 — 죽으면 점유 행이 30분 묶인다
                log.warning("크롤러 DB 대기: %s — 30초 후 재시도", exc)
                if store.conn.in_transaction:
                    store.conn.execute("ROLLBACK")
                time.sleep(30)
                continue
            if res.rows_done:
                run_id = store.run_start("crawl")
                store.run_end(run_id, rows_done=res.rows_done, extra=res.extra)
                log.info("크롤링 %d곳 %s", res.rows_done, res.extra)
            if time.monotonic() - last_merge > 3600 or (args.once and res.rows_done):
                _run_stage(store, "merge", lambda: stages.stage_merge(store))
                reg = Registry()
                _run_stage(store, "zip", lambda: stages.stage_zip(store, reg, Quota(reg)))
                reg.close()
                last_merge = time.monotonic()
            if args.once and (not res.rows_done or args.max_rows):
                break
            if not res.rows_done:
                time.sleep(args.idle)     # 새 URL 이 생길 때까지 대기
    store.close()
    return 0


# ── 일별 실적 ───────────────────────────────────────────────

def _day_index(store: JobStore) -> int:
    start = store.meta("start_date") or today_kst()
    return (date.fromisoformat(today_kst()) - date.fromisoformat(start)).days + 1


def _sum_extra(runs: list[dict], stage: str, key: str) -> int:
    import json
    return sum(int(json.loads(r["extra"] or "{}").get(key, 0)) for r in runs if r["stage"] == stage)


def _plan_cum(job: str, day: int) -> int | None:
    path = OUT_ROOT / job / "daily_plan.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r["day"] == f"D{day}":
                return int(r["naver_p1_cum"]) + int(r["naver_retry_cum"])
    return None


def _progress_row(store: JobStore, quota: Quota, started: str, results) -> dict:
    import json
    day = today_kst()
    runs_today = store.runs_on(day)
    all_runs = [dict(r) for r in store.conn.execute("SELECT * FROM runs")]
    p1_cum = store.count("naver_state IN ('retry_pending','done')")
    retry_cum = _sum_extra(all_runs, "naver", "retry")
    e = _estimate(store)
    plan = _plan_cum(store.job, _day_index(store))
    status = "ok"
    reasons = [f"{r.stage}:{r.stop_reason}" for r in results if r.stop_reason]
    if any(r.status == "error" for r in results):
        status = "error"
    elif any(r.status == "partial" for r in results):
        status = "partial"
    # 한 단계라도 '남은 처리'가 있는 행 수 (단계별 합이 아니라 행 기준 — 이전엔 합산해 전체 행보다 컸다)
    deferred = store.count("nts_state='pending' OR ftc_state='pending' OR nps_state IN ('pending','api_pending') "
                           "OR naver_state IN ('pending','retry_pending') OR crawl_state IN ('pending','failed_tmp')")
    # 크롤링은 상주 워커가 하므로 실행 기록 대신 최근 24시간에 처리(점유)한 행으로 센다
    since = (now_kst() - timedelta(hours=24)).isoformat(timespec="seconds")
    crawled = "crawl_claimed_at >= ? AND crawl_state IN ('done','failed','failed_tmp')"
    return {
        "day": f"D{_day_index(store)}", "date": day, "run_started": started,
        "run_ended": now_kst().isoformat(timespec="seconds"), "run_status": status,
        "stop_reason": " | ".join(reasons)[:300],
        "naver_calls": quota.used("naver"),
        "naver_p1_done": _sum_extra(runs_today, "naver", "p1"),
        "naver_retry_done": _sum_extra(runs_today, "naver", "retry"),
        "naver_p1_cum": p1_cum, "naver_retry_cum": retry_cum,
        "naver_matched_cum": store.count("naver_status='matched'"),
        "nps_api_calls": quota.used("nps"),
        "crawl_done": store.count(crawled, (since,)),
        "crawl_found": store.count(crawled + " AND (crawl_phone<>'' OR crawl_email<>'')", (since,)),
        "crawl_cum": store.count("crawl_state IN ('done','failed','failed_tmp')"),
        "merged_rows": sum(r["rows_done"] for r in runs_today if r["stage"] == "merge"),
        "fill_address": store.count("final_address<>''"),
        "fill_homepage": store.count("final_homepage<>''"),
        "fill_phone": store.count("final_phone<>''"),
        "fill_email": store.count("final_email<>''"),
        "fill_zipcode": store.count("final_zipcode<>''"),
        "fill_industry": store.count("ind_small<>'' AND ind_small<>'기타미분류'"),
        "deferred_rows": deferred,
        "failed_rows": store.count("crawl_state='failed'") + store.count("nts_error<>'' AND nts_state='pending'"),
        "plan_gap_naver_cum": "" if plan is None else (p1_cum + retry_cum) - plan,
        "eta_date": e["eta"],
    }


def _write_progress(store: JobStore, quota: Quota, started: str, results) -> None:
    path = OUT_ROOT / store.job / "daily_progress.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    new = _progress_row(store, quota, started, results)
    # 같은 날 다시 돌리면 그날 행을 교체한다 (하루 1행)
    rows = [r for r in rows if r.get("date") != new["date"]] + [new]
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PROGRESS_COLS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)
    log.info("일별 실적 기록: %s (%s)", path.relative_to(PROJECT_ROOT), new["day"])


DOC = PROJECT_ROOT / "design" / "작업계획서.md"


def _markers(job: str) -> tuple[str, str]:
    return f"<!-- daily-progress:{job}:start -->", f"<!-- daily-progress:{job}:end -->"


def _doc_path(job: str) -> Path | None:
    """작업계획서에 이 잡의 일별 실적 표(표시 구간)가 있으면 그 경로."""
    start, _ = _markers(job)
    return DOC if DOC.exists() and start in DOC.read_text(encoding="utf-8") else None


def _update_doc(store: JobStore, doc: Path) -> None:
    """작업계획서 §1.5 일별 표(표시 구간 안)의 실적 칸(마지막 4칸)을 daily_progress.csv 로 채운다."""
    path = OUT_ROOT / store.job / "daily_progress.csv"
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig", newline="") as f:
        prog = {r["day"]: r for r in csv.DictReader(f)}
    today_day = _day_index(store)
    budget = math.floor(DAILY_LIMIT["naver"] * 0.95)
    lines = doc.read_text(encoding="utf-8").splitlines()
    start, end = _markers(store.job)
    inside = False
    for i, line in enumerate(lines):
        if line.strip() == start:
            inside = True
            continue
        if line.strip() == end:
            break
        if not inside or not line.startswith("| D"):
            continue
        cells = line.split("|")
        day = cells[1].strip()
        if not day[1:].isdigit():
            continue
        p = prog.get(day)
        if p:
            gap = int(p["plan_gap_naver_cum"]) if p["plan_gap_naver_cum"] not in ("", None) else 0
            if p["run_status"] == "error":
                state = f"중단({p['stop_reason'][:30]})"
            elif gap < -budget // 2:
                state = f"지연({gap:,})"
            else:
                state = "정상"
            vals = [f"{int(p['naver_calls']):,}", f"{int(p['naver_p1_cum']):,} / {int(p['naver_retry_cum']):,}",
                    f"{int(p['crawl_cum']):,}", state]
        elif int(day[1:]) < today_day:
            vals = ["—", "—", "—", "미실행"]
        else:
            continue
        cells[-5:-1] = [f" {v} " for v in vals]
        lines[i] = "|".join(cells)
    doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("실적 문서 갱신: %s", doc.name)


# ── status · export · pause ─────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    store = JobStore(args.job)
    registry = Registry()
    quota = Quota(registry)
    n = store.count()
    print(f"잡 {args.job} — {n:,}행 · 시작 {store.meta('start_date') or '(미시작)'} · "
          f"{'일시정지' if store.meta('paused') == '1' else '실행 가능'}")
    print(f"\n{'단계':<10}상태별 행 수")
    for col in ("nts_state", "ftc_state", "nps_state", "naver_state", "crawl_state"):
        counts = store.conn.execute(f"SELECT {col} s, COUNT(*) c FROM rows GROUP BY {col} ORDER BY c DESC").fetchall()
        print(f"  {col[:-6]:<8}" + " · ".join(f"{r['s'] or '-'} {r['c']:,}" for r in counts))
    print("\n오늘 한도 (KST " + today_kst() + ")")
    for api in DAILY_LIMIT:
        print(f"  {api:<6} 사용 {quota.used(api):>7,} / 예산 {quota.daily_budget(api):>9,}")
    fills = [(k, store.count(f"{c}<>''")) for k, c in
             (("주소", "final_address"), ("사이트", "final_homepage"),
              ("전화", "final_phone"), ("이메일", "final_email"))]
    industry = store.count("ind_small<>'' AND ind_small<>'기타미분류'")
    print("\n채움: " + " · ".join(f"{k} {v:,}" for k, v in fills)
          + f" · 업종(기타미분류 제외) {industry:,}")
    e = _estimate(store)
    print(f"NAVER 남은 호출 약 {e['naver_calls']:,}회 → {e['days']}일 · 예상 종료 {e['eta']}")
    print("\n최근 실행")
    for r in store.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8"):
        print(f"  {r['started_at']} {r['stage']:<7} {r['status']:<8} {r['rows_done']:>6,}행 "
              f"{r['calls']:>6,}회 {r['stop_reason'] or ''}")
    if args.update_doc:
        doc = _doc_path(args.job)
        if doc:
            _update_doc(store, doc)
            print(f"\n문서 갱신: {doc.relative_to(PROJECT_ROOT)}")
        else:
            print(f"\n작업계획서에 {args.job} 일별 표가 없습니다 (표시 구간 {_markers(args.job)[0]})")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = JobStore(args.job)
    res = stages.stage_export(store, OUT_ROOT / args.job)
    print(f"내보냄: output/batch/{args.job}/ — {res.extra}")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    store = JobStore(args.job)
    store.set_meta("paused", "1" if args.cmd == "pause" else "0")
    print(f"잡 {args.job}: {'일시정지' if args.cmd == 'pause' else '재개'}")
    return 0


def cmd_ftc_dump(args: argparse.Namespace) -> int:
    registry = Registry()
    res = stages.ftc_dump(registry, Quota(registry), max_pages=args.max_pages)
    print(f"공정위 다운로드: {res.status} · {res.rows_done:,}건 · {res.calls}회 · {res.extra} {res.stop_reason}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="사업자정보 수집 일배치")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import")
    p.add_argument("input")
    p.add_argument("--job", required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--bizno-col", default="IK_BRNO")
    p.add_argument("--name-col", default="EPM_CUST_NM")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("plan")
    p.add_argument("--job", required=True)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("daily")
    p.add_argument("--job", required=True)
    p.add_argument("--with-crawl", action="store_true", help="크롤링도 이 프로세스에서 (워커 없이 돌릴 때)")
    p.add_argument("--no-retry", action="store_true", help="NAVER 재시도 생략 (D9 체크포인트 결정)")
    p.add_argument("--no-nps-api", action="store_true", help="국민연금 API 보완 끄기 (파일 조인만)")
    p.add_argument("--ftc-api-max", type=int, default=3_000,
                   help="공정위 대기 행이 이보다 적으면 건별 API, 많으면 전체 다운로드")
    p.add_argument("--cap", action="append", metavar="API=N",
                   help="일 예산 상한 (테스트·수동 운영). 예: --cap naver=500")
    p.add_argument("--notify", action="store_true", help="오류 시 macOS 알림")
    p.add_argument("--max-rows", type=int, help="NAVER · 국민연금 API · 크롤링을 이번 실행에 최대 N행만 (시험용)")
    p.add_argument("--use-reserve", action="store_true",
                   help="일 한도의 98%%까지 사용 (기본 95%%) — 시험 실행용, 실제 한도는 넘지 않음")
    p.set_defaults(fn=cmd_daily)

    p = sub.add_parser("crawl-worker")
    p.add_argument("--job", required=True)
    p.add_argument("--once", action="store_true", help="대상이 없으면 종료 (테스트용)")
    p.add_argument("--max-rows", type=int)
    p.add_argument("--idle", type=int, default=600, help="대상이 없을 때 대기 초")
    p.set_defaults(fn=cmd_crawl_worker)

    p = sub.add_parser("status")
    p.add_argument("--job", required=True)
    p.add_argument("--update-doc", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("export")
    p.add_argument("--job", required=True)
    p.set_defaults(fn=cmd_export)

    for name in ("pause", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--job", required=True)
        p.set_defaults(fn=cmd_pause)

    p = sub.add_parser("ftc-dump")
    p.add_argument("--max-pages", type=int)
    p.set_defaults(fn=cmd_ftc_dump)

    args = ap.parse_args()
    _setup_logging(args.verbose)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
