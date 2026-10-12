#!/usr/bin/env python3
"""확보된 홈페이지 URL을 스텝 4 크롤링 입력용 CSV로 내보낸다.

마스터의 naver_link 와 ftc_domain 을 합집합으로 모으고, URL 성격을 분류해
크롤링 우선순위를 매긴다. 정제·분류 규칙은 biz_crawler.urls 에 있다.

    python scripts/export_links.py
    python scripts/export_links.py --kind own_domain    자체 도메인만
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biz_crawler.config import PROJECT_ROOT
from biz_crawler.urls import KIND_PRIORITY, classify, host_of, pick, sanitize

MASTER = PROJECT_ROOT / "data" / "master.csv"

FIELDS = [
    "row_no", "b_no_display", "input_name", "official_name",
    "homepage_url", "url_source", "url_kind", "crawl_priority",
    "naver_link", "ftc_domain", "naver_match_score",
    "address", "nts_b_stt", "ftc_phone", "ftc_email",
]


def main() -> int:
    ap = argparse.ArgumentParser(description="확보 링크 내보내기")
    ap.add_argument("-o", "--output", type=Path,
                    default=PROJECT_ROOT / "output" / "확보링크.csv")
    ap.add_argument("--kind",
                    choices=["own_domain", "platform", "marketplace", "all"],
                    default="all")
    args = ap.parse_args()

    if not MASTER.exists():
        raise SystemExit(f"마스터가 없습니다: {MASTER}")
    with MASTER.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    out: list[dict] = []
    dropped = 0
    for r in rows:
        url, kind, source = pick(r.get("naver_link"), r.get("ftc_domain"))
        if not url:
            # 원본에 값이 있었는데 정제에서 탈락한 경우를 센다
            if (r.get("naver_link") or r.get("ftc_domain") or "").strip():
                dropped += 1
            continue
        out.append({
            "row_no": r.get("row_no", ""),
            "b_no_display": r.get("b_no_display", ""),
            "input_name": r.get("input_name", ""),
            "official_name": r.get("ftc_company_name") or r.get("naver_title", ""),
            "homepage_url": url,
            "url_source": source,
            "url_kind": kind,
            "crawl_priority": KIND_PRIORITY.get(kind, 9),
            "naver_link": sanitize(r.get("naver_link")),
            "ftc_domain": sanitize(r.get("ftc_domain")),
            "naver_match_score": r.get("naver_match_score", ""),
            "address": (r.get("ftc_road_address")
                        or r.get("naver_road_address")
                        or r.get("naver_address", "")),
            "nts_b_stt": r.get("nts_b_stt", ""),
            "ftc_phone": r.get("ftc_phone", ""),
            "ftc_email": r.get("ftc_email", ""),
        })

    if args.kind != "all":
        out = [o for o in out if o["url_kind"] == args.kind]
    out.sort(key=lambda o: (o["crawl_priority"], int(o["row_no"] or 0)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)

    print(f"내보냄: {args.output.relative_to(PROJECT_ROOT)}  —  {len(out)}건")
    if dropped:
        print(f"  정제 탈락 (도메인 아님): {dropped}건")
    print("\nURL 성격:")
    for k, v in sorted(Counter(o["url_kind"] for o in out).items(),
                       key=lambda x: KIND_PRIORITY.get(x[0], 9)):
        print(f"  {k}: {v}건")
    print("\n출처:")
    for k, v in Counter(o["url_source"] for o in out).most_common():
        print(f"  {k}: {v}건")
    hosts = Counter(host_of(o["homepage_url"]) for o in out)
    print(f"\n고유 호스트 {len(hosts)}개 | 2건 이상 공유 "
          f"{sum(1 for v in hosts.values() if v > 1)}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
