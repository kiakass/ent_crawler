#!/usr/bin/env python3
"""스텝 단위 파이프라인 실행기.

사업자번호 1건 = 1행인 마스터 테이블(data/master.csv)에 스텝별 접두사 컬럼을
누적한다. 각 스텝은 자기 접두사 컬럼만 채우고, 이미 값이 있는 행은 건드리지
않으므로 재실행하면 빈 칸만 메워진다.

    python scripts/pipeline.py init            마스터 생성 (기존 스텝 1·2 결과 병합)
    python scripts/pipeline.py status          스텝별 진행률 · 게이트 판정
    python scripts/pipeline.py run 3           스텝 3 실행 (빈 칸만)
    python scripts/pipeline.py run 3 --limit 20
    python scripts/pipeline.py run 6           국민연금 업종 (병합 run 5 전에)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from biz_crawler.bizno import BiznoError, format_display, normalize
from biz_crawler.config import PROJECT_ROOT, ConfigError
from biz_crawler.logging_utils import install as install_logging
from biz_crawler.merge import crawl_target  # noqa: E402
from biz_crawler.urls import classify

load_dotenv(PROJECT_ROOT / ".env")

MASTER = PROJECT_ROOT / "data" / "master.csv"
SOURCE = PROJECT_ROOT / "기업리스트2.csv"
STEP12 = PROJECT_ROOT / "output" / "기업리스트2_ftc.csv"

from biz_crawler.schema import (ALL_FIELDS, BASE_FIELDS, CRAWL_FIELDS,  # noqa: E402
                                FINAL_FIELDS, FTC_FIELDS, NAVER_FIELDS,
                                NPS_FIELDS, NTS_FIELDS)


# ── 마스터 입출력 ────────────────────────────────────────────

def load_master() -> list[dict]:
    if not MASTER.exists():
        raise SystemExit(f"마스터가 없습니다. 먼저 init 을 실행하세요: {MASTER}")
    with MASTER.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def save_master(rows: list[dict]) -> None:
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    tmp = MASTER.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALL_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in ALL_FIELDS})
    tmp.replace(MASTER)


def _blank(value: object) -> bool:
    return not str(value or "").strip()


# ── init ────────────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> int:
    if MASTER.exists() and not args.force:
        raise SystemExit(f"이미 존재합니다: {MASTER}\n덮어쓰려면 --force")

    with SOURCE.open(encoding="utf-8-sig", newline="") as f:
        src = list(csv.DictReader(f))

    rows: list[dict] = []
    for i, s in enumerate(src, 1):
        raw = (s.get("IK_BRNO") or "").strip()
        try:
            b_no = normalize(raw)
        except BiznoError:
            b_no = ""
        rows.append({
            "row_no": i,
            "b_no": b_no,
            "b_no_display": format_display(b_no) if b_no else "",
            "input_name": (s.get("EPM_CUST_NM") or "").strip(),
        })

    merged = 0
    if STEP12.exists():
        with STEP12.open(encoding="utf-8-sig", newline="") as f:
            prev = list(csv.DictReader(f))
        if len(prev) != len(rows):
            print(f"  ! 행수 불일치 (마스터 {len(rows)} vs 기존 {len(prev)}) — 병합 생략")
        else:
            # 스텝 1 결과는 접두사 없이 저장돼 있어 이름을 옮겨준다.
            nts_map = {"registered": "nts_registered", "is_active": "nts_is_active",
                       "b_stt": "nts_b_stt", "b_stt_cd": "nts_b_stt_cd",
                       "tax_type": "nts_tax_type", "end_dt": "nts_end_dt",
                       "error": "nts_error"}
            for row, p in zip(rows, prev):
                if row["input_name"] and p.get("EPM_CUST_NM", "").strip() != row["input_name"]:
                    continue  # 정렬이 어긋난 행은 병합하지 않는다
                for old, new in nts_map.items():
                    row[new] = (p.get(old) or "").strip()
                for k in FTC_FIELDS:
                    if k in p:
                        row[k] = (p.get(k) or "").strip()
                merged += 1

    save_master(rows)
    print(f"마스터 생성: {MASTER}")
    print(f"  전체 {len(rows)}행 | 사업자번호 유효 {sum(1 for r in rows if r['b_no'])}건")
    print(f"  스텝 1·2 결과 병합: {merged}행")
    return 0


# ── status ──────────────────────────────────────────────────

def _ratio(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d * 100:.1f}%)" if d else f"{n}/0 (—)"


def cmd_status(args: argparse.Namespace) -> int:
    rows = load_master()
    total = len(rows)
    named = [r for r in rows if r["input_name"]]
    with_no = [r for r in rows if r["b_no"]]

    print(f"마스터: {MASTER.relative_to(PROJECT_ROOT)}  —  {total}행 "
          f"(사업자번호 유효 {len(with_no)}, 상호명 있음 {len(named)})\n")

    print(f"{'스텝':<4}{'내용':<26}{'채움':<20}{'게이트':<10}판정")
    print("─" * 84)

    # 스텝 1 — 국세청
    s1 = sum(1 for r in with_no if not _blank(r.get("nts_b_stt")))
    g1 = s1 / len(with_no) if with_no else 0
    _line("01", "국세청 상태조회", _ratio(s1, len(with_no)), "≥95%", g1 >= 0.95)

    # 스텝 2 — 공정위
    s2 = sum(1 for r in with_no if str(r.get("ftc_found")).strip() == "True")
    done2 = sum(1 for r in with_no if not _blank(r.get("ftc_found")) or not _blank(r.get("ftc_error")))
    _line("02", "공정위 통신판매사업자", _ratio(s2, len(with_no)), "보조소스",
          None, note=f"조회 완료 {done2}/{len(with_no)}")

    # 스텝 3 — NAVER
    done3 = sum(1 for r in named if not _blank(r.get("naver_status")))
    # 스텝 4의 입력은 두 소스의 합집합이므로 분모는 상호명 보유 전체로 잡는다.
    # 정제를 통과한 URL 만 센다 (도메인 아닌 자유입력값 제외).
    urls = [crawl_target(r)[0] for r in named]
    got_url = sum(1 for u in urls if u)
    g3 = got_url / len(named) if named else 0
    own = sum(1 for u in urls if u and classify(u) == "own_domain")
    _line("03", "NAVER 지역검색", _ratio(got_url, len(named)), "≥30%", g3 >= 0.30,
          note=f"조회 {done3}/{len(named)} · 자체도메인 {own}")

    # 스텝 4 — 크롤링
    # 게이트 분모는 자체 도메인만 — 플랫폼(블로그·SNS)은 정적 크롤링 대상이 아니다.
    targets = [r for r in named if r.get("crawl_url_kind") == "own_domain"] or [
        r for r in named
        if not _blank(r.get("naver_link")) or not _blank(r.get("ftc_domain"))]
    got4 = sum(1 for r in targets
               if not _blank(r.get("crawl_phone")) or not _blank(r.get("crawl_email")))
    g4 = got4 / len(targets) if targets else 0
    _line("04", "홈페이지 크롤링", _ratio(got4, len(targets)), "≥50%", g4 >= 0.50)

    # 스텝 5 — 병합
    s5 = sum(1 for r in rows if not _blank(r.get("status")))
    _line("05", "병합 · Export", _ratio(s5, total), "전건", s5 == total if total else False)

    print()
    nxt = _next_step(g1, g3, g4, done3, len(named))
    print(f"다음 작업: {nxt}")
    return 0


def _line(step: str, name: str, fill: str, gate: str,
          passed: bool | None, note: str = "") -> None:
    mark = "—" if passed is None else ("통과" if passed else "미달")
    print(f"{step:<4}{name:<26}{fill:<20}{gate:<10}{mark}" + (f"   {note}" if note else ""))


def _next_step(g1: float, g3: float, g4: float, done3: int, named: int) -> str:
    if g1 < 0.95:
        return "스텝 1 재실행 — 국세청 상태 확인율 미달"
    if done3 < named:
        return f"스텝 3 실행 — 미조회 {named - done3}건 (run 3)"
    if g3 < 0.30:
        return ("스텝 3 게이트 미달 — 스텝 4를 착수하지 말고 "
                "URL 확보 전략을 재설계할 것")
    if g4 < 0.50:
        return "스텝 4 실행 — 홈페이지 크롤링 (run 4)"
    return "스텝 5 실행 — 병합 · Export (run 5)"


# ── run 3 : NAVER 지역검색 ──────────────────────────────────

def step3_naver(rows: list[dict], args: argparse.Namespace) -> int:
    from biz_crawler.naver_local import NaverLocalClient

    retry_mode = getattr(args, "retry", False)
    if retry_mode:
        # 이미 matched 인 행은 건드리지 않는다. 재시도는 못 찾은 행만.
        targets = [r for r in rows
                   if r["input_name"]
                   and r.get("naver_status") in ("no_result", "low_score", "error")]
    else:
        targets = [r for r in rows
                   if r["input_name"] and _blank(r.get("naver_status"))]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("스텝 3: 채울 행이 없습니다"
              + (" (재시도 대상 없음)." if retry_mode else " (이미 전건 조회 완료)."))
        return 0

    print(f"스텝 3{'(재시도)' if retry_mode else ''} — NAVER 지역검색 | "
          f"대상 {len(targets)}건 (딜레이 {args.delay}s)")

    try:
        client = NaverLocalClient(delay=args.delay, min_score=args.min_score)
    except ConfigError as exc:
        raise SystemExit(str(exc))

    stats: dict[str, int] = {}
    link_hit = 0
    improved = 0
    new_link = 0
    with client:
        pairs = ((r["input_name"], _region_hint(r)) for r in targets)
        stream = (client.resolve_many_hard(pairs) if retry_mode
                  else client.resolve_many(pairs))
        for i, (row, res) in enumerate(zip(targets, stream), 1):
            if retry_mode:
                # 점수가 나아지지 않으면 기존 값을 지키다. 재시도가
                # 결과를 후퇴시키는 일이 없어야 한다.
                prev = float(row.get("naver_match_score") or 0)
                had_link = bool((row.get("naver_link") or "").strip())
                if res.score <= prev and not (res.item and res.item.link and not had_link):
                    stats["변화없음"] = stats.get("변화없음", 0) + 1
                    continue
                improved += 1
                if res.item and res.item.link and not had_link:
                    new_link += 1
            row.update(res.to_row())
            stats[res.status] = stats.get(res.status, 0) + 1
            if res.item and res.item.link:
                link_hit += 1
            if i % 25 == 0:
                print(f"  ... {i}/{len(targets)}  (개선 {improved}건)"
                      if retry_mode else
                      f"  ... {i}/{len(targets)}  (link {link_hit}건)")

    print("\n── 스텝 3 결과 ──")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}건")
    if retry_mode:
        print(f"  개선: {_ratio(improved, len(targets))}")
        print(f"  link 신규 확보: {new_link}건")
    else:
        print(f"  link 확보: {_ratio(link_hit, len(targets))}")
    return 0


def _region_hint(row: dict) -> str:
    """확보된 주소에서 '시/군/구' 한 토큰을 뽑아 지역 키워드로 쓴다."""
    addr = (row.get("ftc_road_address") or row.get("ftc_lot_address") or "").strip()
    for tok in addr.split():
        if tok.endswith(("시", "군", "구")) and len(tok) >= 3:
            return tok
    return ""


def step4_crawl(rows: list[dict], args: argparse.Namespace) -> int:
    """확보된 홈페이지를 방문해 대표전화·이메일을 추출한다."""
    from biz_crawler.homepage import HomepageCrawler

    candidates = []
    for r in rows:
        url, kind, _ = crawl_target(r)
        if not url or not _blank(r.get("crawl_pages")) or not _blank(r.get("crawl_error")):
            continue
        if args.kind != "all" and kind != args.kind:
            continue
        candidates.append((r, url, kind))

    if args.limit:
        candidates = candidates[: args.limit]
    if not candidates:
        print("스텝 4: 크롤링할 행이 없습니다.")
        return 0

    kinds = {}
    for _, _, k in candidates:
        kinds[k] = kinds.get(k, 0) + 1
    print(f"스텝 4 — 홈페이지 크롤링 | 대상 {len(candidates)}건 "
          f"({', '.join(f'{k} {v}' for k, v in kinds.items())})")
    print(f"  사이트당 최대 {args.max_pages}페이지 · 딜레이 {args.delay}s · robots.txt 준수")

    got_phone = got_email = got_any = 0
    errors: dict[str, int] = {}
    # 플랫폼(블로그 등)은 동적 렌더링이라 Playwright 로 따로 돈다.
    platform = None
    if any(k == "platform" for _, _, k in candidates):
        from biz_crawler.platform import PlatformCrawler
        platform = PlatformCrawler(timeout=max(args.timeout, 20.0))
    with HomepageCrawler(delay=args.delay, timeout=args.timeout,
                         max_pages=args.max_pages) as crawler:
        for i, (row, url, kind) in enumerate(candidates, 1):
            res = platform.crawl(url) if kind == "platform" else crawler.crawl(url)
            row["crawl_url"] = url
            row["crawl_url_kind"] = kind
            row.update(res.to_row())
            if res.phones:
                got_phone += 1
            if res.emails:
                got_email += 1
            if res.found:
                got_any += 1
            else:
                key = res.error.split(":")[0][:40] or "미발견"
                errors[key] = errors.get(key, 0) + 1
            if i % 25 == 0:
                print(f"  ... {i}/{len(candidates)}  (전화 {got_phone} / 이메일 {got_email})")
    if platform:
        platform.close()

    n = len(candidates)
    print("\n── 스텝 4 결과 ──")
    print(f"  전화 확보   : {_ratio(got_phone, n)}")
    print(f"  이메일 확보 : {_ratio(got_email, n)}")
    print(f"  최소 1개    : {_ratio(got_any, n)}")
    if errors:
        print("  실패 사유:")
        for k, v in sorted(errors.items(), key=lambda x: -x[1])[:8]:
            print(f"    {k}: {v}건")
    return 0


# ── run 6 : 국민연금 업종 ───────────────────────────────────
# 번호는 6이지만 병합(5)보다 먼저 돌려야 업종 분류에 반영된다.
# 다른 스텝을 기다리지 않는다 — 사업자번호와 상호명(입력값이라도)만 있으면 된다.

def step6_nps(rows: list[dict], args: argparse.Namespace) -> int:
    from biz_crawler.nps_bplc import NpsClient

    targets = [r for r in rows
               if r["b_no"] and r["input_name"] and _blank(r.get("nps_status"))]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("스텝 6: 채울 행이 없습니다 (이미 전건 조회 완료).")
        return 0

    print(f"스텝 6 — 국민연금 사업장 업종 | 대상 {len(targets)}건 (일 한도 10,000회 · 행당 약 2회)")
    stats: dict[str, int] = {}
    with NpsClient() as client:
        for i, row in enumerate(targets, 1):
            name = (row.get("final_company_name") or row["input_name"]).strip()
            res = client.lookup(row["b_no"], name)
            row.update(res.to_row())
            stats[res.status] = stats.get(res.status, 0) + 1
            if i % 50 == 0:
                print(f"  ... {i}/{len(targets)}  (matched {stats.get('matched', 0)} · 호출 {client.calls})")
        calls = client.calls

    print("\n── 스텝 6 결과 ──")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}건")
    print(f"  호출: {calls}회")
    return 0


# ── run 5 : 병합 ────────────────────────────────────────────

def step5_merge(rows: list[dict], args: argparse.Namespace) -> int:
    """소스별 값을 final_* 컬럼으로 병합한다."""
    from collections import Counter

    from biz_crawler.merge import merge_row

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    targets = rows if args.force else [r for r in rows if _blank(r.get("status"))]
    if not targets:
        print("스텝 5: 이미 전건 병합 완료 (--force 로 재계산).")
        return 0

    print(f"스텝 5 — 병합 | 대상 {len(targets)}건")
    stats: Counter[str] = Counter()
    field_hit: Counter[str] = Counter()
    from biz_crawler.batch.stages import shared_url_counts
    from biz_crawler.merge import url_key
    counts = shared_url_counts(rows)
    for row in targets:
        row["_shared_url"] = counts.get(url_key(crawl_target(row)[0]), 0)
        merged = merge_row(row)
        row.update(merged)
        row["collected_at"] = stamp
        stats[merged["status"]] += 1
        for f in ("final_company_name", "final_address", "final_phone",
                  "final_email", "final_homepage"):
            if merged[f]:
                field_hit[f] += 1

    # 우편번호 — 도로명주소 API (같은 주소는 캐시)
    from biz_crawler.batch.stages import resolve_zip
    from biz_crawler.batch.store import Registry
    from biz_crawler.juso import JusoClient
    reg = Registry()
    zip_targets = [r for r in targets if r.get("final_address") and r["final_address"] != r.get("zip_addr")]
    if zip_targets:
        with JusoClient() as juso:
            for r in zip_targets:
                r.update(resolve_zip(r, juso, reg, None) or {})
        print(f"  우편번호 조회 {len(zip_targets)}건 · 호출 {juso.calls}회")
    reg.close()
    field_hit["final_zipcode"] = sum(1 for r in targets if r.get("final_zipcode"))

    n = len(targets)
    print("\n── 스텝 5 결과 ──")
    labels = {"final_company_name": "상호명", "final_address": "주소",
              "final_phone": "대표전화", "final_email": "이메일",
              "final_homepage": "사이트주소", "final_zipcode": "우편번호"}
    for f, label in labels.items():
        print(f"  {label:6} {_ratio(field_hit[f], n)}")
    print("  상태: " + " · ".join(f"{k} {v}건" for k, v in stats.most_common()))
    return 0


STEPS = {3: step3_naver, 4: step4_crawl, 5: step5_merge, 6: step6_nps}


def cmd_reset(args: argparse.Namespace) -> int:
    """스텝 결과 컬럼을 비워 재실행 대상으로 되돌린다."""
    groups = {3: NAVER_FIELDS, 4: CRAWL_FIELDS, 5: FINAL_FIELDS, 6: NPS_FIELDS}
    fields = groups.get(args.step)
    if fields is None:
        raise SystemExit(f"reset 가능한 스텝: {', '.join(map(str, groups))}")
    rows = load_master()
    n = 0
    for r in rows:
        if any(not _blank(r.get(k)) for k in fields):
            n += 1
            for k in fields:
                r[k] = ""
    save_master(rows)
    print(f"스텝 {args.step} 결과 초기화: {n}행")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    fn = STEPS.get(args.step)
    if fn is None:
        raise SystemExit(f"스텝 {args.step}은 아직 구현되지 않았습니다. 구현된 스텝: "
                         + ", ".join(str(k) for k in sorted(STEPS)))
    rows = load_master()
    rc = fn(rows, args)
    save_master(rows)
    print(f"\n마스터 갱신: {MASTER.relative_to(PROJECT_ROOT)}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="사업자정보 수집 파이프라인")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="마스터 생성")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("status", help="스텝별 진행률 · 게이트 판정")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("reset", help="스텝 결과 초기화")
    p.add_argument("step", type=int)
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("run", help="스텝 실행")
    p.add_argument("step", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--delay", type=float, default=0.15)
    p.add_argument("--min-score", type=float, default=0.60)
    p.add_argument("--retry", action="store_true",
                   help="스텝 3: 미매칭(no_result·low_score·error) 행을 "
                        "질의 변형 래더로 재시도")
    p.add_argument("--kind", choices=["own_domain", "platform", "all"], default="all",
                   help="스텝 4: 크롤링할 URL 성격")
    p.add_argument("--max-pages", type=int, default=3, help="스텝 4: 사이트당 최대 요청")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--force", action="store_true",
                   help="스텝 5: 이미 병합된 행도 다시 계산")
    p.set_defaults(fn=cmd_run)

    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    install_logging(logging.DEBUG if args.verbose else logging.INFO)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
