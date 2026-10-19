"""NAVER 카테고리 → 표준산업분류(KSIC 11차) 대응표 초안 (2026-10-19).

국민연금 업종이 없는 행에 NAVER 카테고리로 KSIC 를 붙이기 위한 1단계다.
같은 행에 NAVER 카테고리와 국민연금 KSIC 가 둘 다 있는 행에서 카테고리별 최빈 KSIC 를 세고,
순도가 기준 이상인 가장 깊은 분류 수준(세세 5 → 세 4 → 소 3 → 중 2)을 제안한다.

    python scripts/build_naver_ksic.py --job SME1008

출력 (data/external/ksic/ — 커밋 제외 경로):
    naver_ksic_draft.csv          카테고리 전체 · 제안 코드 · 순도 · 이름 힌트
    naver_ksic_review_top100.csv  대상 행이 많은 상위 100개 — '확정코드' 를 고치면 다시 돌려도 유지된다
    naver_ksic.csv                병합이 읽는 최종 대응표 (biz_crawler/ksic.py)

카테고리 문자열과 건수만 다룬다. 고객 데이터(상호 · 번호 · 주소)는 쓰지 않는다.
외부 호출 0회.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from biz_crawler.ksic import KSIC_DIR, classify_nps  # noqa: E402
from biz_crawler.merge import naver_trusted  # noqa: E402

LEVELS = ((5, "세세"), (4, "세"), (3, "소"), (2, "중"))
MIN_ROWS = 5          # 학습 행이 이보다 적으면 제안하지 않는다
MIN_PURITY = 0.8      # 이 순도 이상인 가장 깊은 수준을 제안 (10/19 검증: 세세 89% · 중 91%)
TOP_N = 100

# 상위 카테고리 명칭 기준 제안 (10/19, KSIC 11차 명칭을 보고 고름). 빈 값 = 업종이 섞여 있어 대응 불가.
# 국민연금 업종과 다른 곳이 있다 — 예: 어린이집은 국민연금이 '그 외 기타 개인 서비스업'(96999) 으로 두지만
# KSIC 명칭으로는 보육시설 운영업(87210) 이다. 어느 쪽을 쓸지는 검토자가 '확정코드' 로 정한다.
SEED = {
    "서비스,산업>기업": "", "교육,학문>어린이집": "87210", "생활,편의>편의점": "47122",
    "건설업>전문건설업": "42", "병원,의원>치과": "86202", "시설,건물>주택": "",
    "건설업>전기공사": "42311", "건강,의료>요양원,요양센터": "87111", "교육,학문>국공립어린이집": "87210",
    "건설업>종합건설업": "41", "사회,복지>재가노인요양센터": "87293", "사회,복지>노인복지": "87",
    "서비스,산업>제조업": "", "건강,의료>약국": "47811", "서비스,산업>건설업": "",
    "교육,학문>유치원": "85110", "화물운송>용달": "4930", "여객,화물운송>화물운송": "4930",
    "제조업>전자,전기": "", "시설,건물>기업,빌딩": "", "전문,기술서비스>인테리어디자인": "42412",
    "임대,대여>중장비": "42600", "음식점>한식": "56111", "기계,장비제조>산업용기계제조": "29",
    "건설업>조경건설": "41226", "병원,의원>내과": "86201", "음식점>카페,디저트": "56221",
    "쇼핑,유통>슈퍼,마트": "47121", "전문,기술서비스>설계,엔지니어링": "7212", "병원,의원>한의원": "86203",
    "서비스,산업>물류,유통": "", "컴퓨터프로그래밍,정보서비스업>소프트웨어개발": "58222",
    "미용>미용실": "96112", "제조업>기계,장비제조": "29", "쇼핑,유통>종합도소매": "",
    "전문,기술서비스>경영컨설팅": "71531", "전문,기술서비스>세무사": "71202", "사회,복지>아동복지시설": "87131",
    "주택>아파트": "", "병원,의원>정형외과": "86201", "건설업>토목건설": "4122", "쇼핑,유통>패션": "",
    "금융,보험>금융서비스": "", "서비스,산업>이사": "4930", "지원,대행>포장이사": "4930",
    "제조업>전자부품제조": "262", "건설업>건물,구축물해체공사": "42110", "제조업>1차금속제조": "24",
    "병원,의원>피부과": "86201", "건설업>배관,냉난방공사": "42201", "부동산>개발,공급업": "6812",
    "건강,의료>심리상담": "", "아파트>관리사무소": "68211", "제조업>인쇄업": "1811", "서비스,산업>무역": "",
    "사회,복지>장애인복지시설": "8712", "교육,학문>민간어린이집": "87210", "교육,학문>가정어린이집": "87210",
    "쇼핑,유통>건축자재,용품": "", "쇼핑,유통>조경": "41226", "생활,편의>자동차정비,수리": "9521",
    "서비스,산업>임대업": "", "사회,복지": "87", "교육,학문>연구,연구소": "70", "건설업>건물건설": "411",
    "부동산>컨설팅": "68222", "제조업>금속가공제품제조": "25", "설계,엔지니어링>건축설계": "72111",
    "카페,디저트>카페": "56221", "건설업>미장,타일,방수공사": "42491", "쇼핑,유통>건축,토목": "",
    "부동산>중개업": "68221", "광고,마케팅>광고대행": "71310", "교통,운수>여객,화물운송": "",
    "한식>육류,고기요리": "56113", "교육,학문>지역아동센터": "87299", "병원,의원>이비인후과": "86201",
    "협회,단체": "", "서비스,산업>부동산": "68", "제조업>화학물질,제품제조": "20",
    "쇼핑,유통>건강,의료용품": "", "쇼핑,유통>휴대폰": "47312", "금융,보험>대출": "64919",
    "중식>중식당": "56121", "숙박>호텔": "55101", "쇼핑,유통>가구,인테리어": "", "건강,의료>병원,의원": "86",
    "행정복지센터": "84113", "미용>피부,체형관리": "96113", "패션>여성의류": "47412", "건설업>주택건설": "4111",
    "주택>오피스텔": "", "시설,건물>농공시설": "", "화물운송>택배": "49401", "건설업>건물설비설치공사": "4220",
    "건설업>유리,창호공사": "42420", "전문,기술서비스>법률사무소": "71101", "지원,대행>인력공급,고용알선": "751",
    "금융,보험>손해사정": "66201", "제조업>섬유제품제조": "13",
}

# 순도는 높아도 '그 외 기타' 같은 포괄 코드면 국민연금 분류 관행일 수 있다 — 자동으로 두지 않는다
CATCH_ALL = re.compile(r"그 외 기타|달리 분류되지 않은|분류 안된")
UNMAPPABLE = "불가"     # 확정코드에 이 값이면 대응표에 넣지 않는다

# 업종을 말하지 않는 범용 카테고리 — 마지막 토막이 이것이면 '대응불가 후보'
GENERIC = {"기업", "회사", "주택", "아파트", "건물", "빌딩", "시설", "단체", "협회", "사무소", "오피스텔", "상가"}


def ksic_names() -> dict[str, str]:
    """수준별 코드 → 명칭 (중 2 · 소 3 · 세 4 · 세세 5자리)."""
    out: dict[str, str] = {}
    with (KSIC_DIR / "ksic11_세세분류.csv").open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            for col in ("중분류", "소분류", "세분류", "세세분류"):
                out[r[f"{col}코드"]] = r[f"{col}명"]
    return out


def name_hints(cat: str, names: dict[str, str], limit: int = 2) -> str:
    """카테고리 마지막 토막이 세세분류명에 들어 있는 코드 (학습 행이 없는 카테고리용 힌트)."""
    last = re.split(r"[>,]", cat)[-1].strip()
    if len(last) < 2:
        return ""
    hits = [f"{c} {n}" for c, n in names.items() if len(c) == 5 and last in n]
    return " | ".join(hits[:limit])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--job", required=True)
    args = ap.parse_args()

    db = sqlite3.connect(ROOT / "data" / "batch" / args.job / "state.db")
    db.row_factory = sqlite3.Row
    pair: dict[str, Counter] = defaultdict(Counter)
    gap: Counter = Counter()
    for r in db.execute("SELECT * FROM rows"):
        r = dict(r)
        cat = (r.get("naver_category") or "").strip()
        if not cat or not naver_trusted(r):
            continue
        code = classify_nps(r)["ksic_code"]   # 학습은 국민연금 값만 (NAVER 추정값이 섞이면 안 된다)
        if code:
            pair[cat][code] += 1
        else:
            gap[cat] += 1

    names = ksic_names()
    rows = []
    for cat in set(pair) | set(gap):
        c = pair.get(cat, Counter())
        n = sum(c.values())
        rec = {"NAVER카테고리": cat, "대상행": gap.get(cat, 0), "학습행": n,
               "제안수준": "", "제안코드": "", "제안명": "", "순도": "",
               "1순위": "", "1순위비율": "", "2순위": "", "2순위비율": "",
               "이름힌트": "", "제안상태": ""}
        if n:
            ranked = c.most_common(2)
            rec["1순위"] = f"{ranked[0][0]} {names.get(ranked[0][0], '')}"
            rec["1순위비율"] = f"{ranked[0][1] / n:.2f}"
            if len(ranked) > 1:
                rec["2순위"] = f"{ranked[1][0]} {names.get(ranked[1][0], '')}"
                rec["2순위비율"] = f"{ranked[1][1] / n:.2f}"
        if n >= MIN_ROWS:
            for digits, label in LEVELS:
                lv = Counter()
                for code, v in c.items():
                    lv[code[:digits]] += v
                top, tv = lv.most_common(1)[0]
                if tv / n >= MIN_PURITY:
                    rec.update(제안수준=label, 제안코드=top, 제안명=names.get(top, ""), 순도=f"{tv / n:.2f}")
                    break
        last = re.split(r"[>,]", cat)[-1].strip()
        if last in GENERIC:
            rec["제안상태"] = "대응불가 후보"
        elif rec["제안코드"] and CATCH_ALL.search(rec["제안명"]):
            rec["제안상태"] = "검토(포괄코드)"
        elif rec["제안코드"]:
            rec["제안상태"] = "자동"
        else:
            rec["제안상태"] = "검토"
            rec["이름힌트"] = name_hints(cat, names)
        if cat in SEED:
            sc = SEED[cat]
            rec["명칭제안코드"] = sc
            rec["명칭제안명"] = names.get(sc, "") if sc else "대응 불가"
            dc = rec["제안코드"] or rec["1순위"].split(" ")[0]
            rec["데이터일치"] = ("" if not sc or not dc else
                              "일치" if dc.startswith(sc) or sc.startswith(dc) else "불일치")
        else:
            rec["명칭제안코드"] = rec["명칭제안명"] = rec["데이터일치"] = ""
        rows.append(rec)

    rows.sort(key=lambda x: (-x["대상행"], -x["학습행"]))
    fields = list(rows[0])
    out_all = KSIC_DIR / "naver_ksic_draft.csv"
    with out_all.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    out_top = KSIC_DIR / "naver_ksic_review_top100.csv"
    # 사람이 고친 확정코드 · 비고는 다시 돌려도 지킨다. 비어 있으면 명칭 제안으로 채운다 (10/19 결정: KSIC 명칭 기준)
    kept: dict[str, tuple[str, str]] = {}
    if out_top.exists():
        with out_top.open(encoding="utf-8-sig", newline="") as f:
            kept = {r["NAVER카테고리"]: (r.get("확정코드", ""), r.get("비고", "")) for r in csv.DictReader(f)}
    confirmed: dict[str, str] = {}
    with out_top.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["순위", *fields, "확정코드", "비고"])
        w.writeheader()
        for i, rec in enumerate(rows[:TOP_N], 1):
            code, note = kept.get(rec["NAVER카테고리"], ("", ""))
            if not code and rec["NAVER카테고리"] in SEED:
                code = SEED[rec["NAVER카테고리"]] or UNMAPPABLE
            if not code and rec["제안상태"] == "자동":
                code = rec["제안코드"]
            confirmed[rec["NAVER카테고리"]] = code
            w.writerow({"순위": i, **rec, "확정코드": code, "비고": note})

    # 병합이 읽는 최종 대응표 — 상위 100 은 확정코드, 나머지는 '자동' 제안만
    out_map = KSIC_DIR / "naver_ksic.csv"
    mapped = 0
    with out_map.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["NAVER카테고리", "코드", "명칭", "근거"])
        for rec in rows:
            cat = rec["NAVER카테고리"]
            if cat in confirmed:
                code, basis = confirmed[cat], "검토확정"
            elif rec["제안상태"] == "자동":
                code, basis = rec["제안코드"], f"자동(순도 {rec['순도']})"
            else:
                continue
            if not code or code == UNMAPPABLE or code not in names:
                continue
            w.writerow([cat, code, names[code], basis])
            mapped += rec["대상행"]

    # 요약 — 집계만
    total_gap = sum(gap.values())
    by_state = Counter()
    for rec in rows:
        by_state[rec["제안상태"]] += rec["대상행"]
    top = rows[:TOP_N]
    top_state = Counter()
    for rec in top:
        top_state[rec["제안상태"]] += rec["대상행"]
    lv = Counter(rec["제안수준"] for rec in rows if rec["제안상태"] == "자동")
    print(f"카테고리 {len(rows):,}개 · 대상행 {total_gap:,} · 학습행 {sum(sum(c.values()) for c in pair.values()):,}")
    print("전체 대상행 상태별:", {k: f"{v:,}" for k, v in by_state.items()})
    print(f"상위 {TOP_N}개 대상행 {sum(r['대상행'] for r in top):,} 상태별:", {k: f"{v:,}" for k, v in top_state.items()})
    print("자동 제안 수준별 카테고리 수:", dict(lv))
    agree = Counter(rec["데이터일치"] or ("대응불가" if rec["명칭제안명"] == "대응 불가" else "-") for rec in top)
    print("상위 100 명칭제안 vs 데이터 최빈:", dict(agree))
    bad = [c for c in SEED.values() if c and c not in names]
    print("SEED 에 없는 코드:", bad or "없음")
    print(f"최종 대응표가 덮는 대상행: {mapped:,}")
    print(f"→ {out_all.relative_to(ROOT)}\n→ {out_top.relative_to(ROOT)}\n→ {out_map.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
