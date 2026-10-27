"""소스별 값을 한 행으로 병합한다 (스텝 5).

같은 항목을 국세청·공정위·NAVER·홈페이지 크롤링 네 곳에서 얻으므로
우선순위를 고정해 둔다. 순위는 실측 신선도에 따른다:

- 상호명 : 공정위(공식 등록명) > 근로복지공단(보험 가입명) > NAVER(간판명) > 입력값.
  (주) · 주식회사 · 유한회사 · 사단법인 같은 회사 표기는 빼고 저장한다 (namematch.strip_company_forms)
- 주소   : 공정위 도로명 > 근로복지공단 > NAVER 도로명 > NAVER 지번 > 공정위 지번
  (근로복지공단은 상호 없이 사업자번호만 받은 행에만 조회한다 — 10/16)
- 전화   : 홈페이지 크롤링 > 공정위
- 이메일 : 홈페이지 크롤링만 — 공정위 이메일은 대표자 개인 이메일이라 쓰지 않는다
- 사이트 : 크롤링에 성공한 URL > 정제된 후보 URL

전화·이메일에서 크롤링을 공정위보다 앞에 두는 근거는 스텝 4 실측이다.
공정위 신고 도메인의 연락처 확보율이 10%(1/10)로 NAVER 경유 47%(36/77)
보다 크게 낮았다 — 신고 시점 값이 갱신되지 않는다.

crawl_mobile 은 병합하지 않는다. 개인 휴대폰은 PRD 1.3 범위 밖이다.

NAVER 값은 상호가 매칭된(naver_status=matched) 업소일 때만 쓴다. low_score 는
점수 0.6 미만 — 다른 업소일 가능성이 높다. 그 업소의 링크를 크롤링해 얻은 연락처도
쓰지 않는다 (2026-10-08 파일럿에서 low_score 1,177건의 상호·주소·사이트가 결과에
섞인 것을 발견).

10/08 검증 후 추가 규칙:
- 지역 검증: 공정위·국민연금 주소(사업자번호로 확정)의 시·도와 NAVER 업소 시·도가 다르면
  NAVER 값을 쓰지 않는다 (상호 완전일치여도 48% 가 다른 지역이었다)
- 공유 URL: 같은 URL 을 SHARED_URL_MIN 곳 이상이 공유하면(프랜차이즈 본사 · 대행사 블로그)
  그 크롤링 전화·이메일은 이 사업자의 것이 아니다 — 쓰지 않고 사이트주소에 비고를 단다
- 실패 URL: 재시도까지 실패한(영구) URL 은 사이트주소에서 뺀다. robots 차단 · 연락처 미발견 ·
  일시 오류(재시도 대기)는 사이트가 있는 것이므로 유지
"""

from __future__ import annotations

import re

from .industry import classify as classify_industry
from .namematch import core, strip_company_forms
from .region import known_address, sido
from .urls import pick, sanitize

SHARED_URL_MIN = 5

# low_score 일 때 버리는 NAVER 값 (상태·점수·후보 수는 진단용으로 남긴다)
_NAVER_VALUES = ("naver_title", "naver_link", "naver_category", "naver_address",
                 "naver_road_address")
_CRAWL_VALUES = ("crawl_phone", "crawl_mobile", "crawl_email", "crawl_address",
                 "crawl_source_url", "crawl_url", "crawl_url_kind")


# 3글자 이하 상호는 한 글자만 같아도 유사도 0.67 이 나온다 — 더 높은 점수를 요구한다
SHORT_NAME_LEN = 3
SHORT_MIN_SCORE = 0.85


def naver_region(row: dict) -> str:
    """NAVER 업소 시·도 ↔ 확정 주소(공정위 > 국민연금) 시·도: 일치 · 불일치 · 검증불가 · ''(NAVER 없음)."""
    nav = sido(row.get("naver_road_address") or row.get("naver_address"))
    if not nav:
        return ""
    known = sido(known_address(row))
    if not known:
        return "검증불가"
    return "일치" if known == nav else "불일치"


def naver_trusted(row: dict) -> bool:
    """NAVER 업소를 이 사업자로 믿어도 되는가 — matched · 짧은 상호면 0.85 이상 · 지역 불일치 아님."""
    if (row.get("naver_status") or "") != "matched":
        return False
    if naver_region(row) == "불일치":
        return False
    # 질의에 쓴 상호 기준 (입력 상호가 없으면 공정위 · 근로복지공단 상호로 검색했다)
    name = row.get("input_name") or row.get("ftc_company_name") or row.get("kcw_name") or ""
    if len(core(name)) <= SHORT_NAME_LEN:
        try:
            return float(row.get("naver_match_score") or 0) >= SHORT_MIN_SCORE
        except ValueError:
            return False
    return True


def crawl_target(row: dict) -> tuple[str, str, str]:
    """크롤링할 URL — NAVER 링크는 매칭된 업소일 때만 후보가 된다."""
    return pick(row.get("naver_link") if naver_trusted(row) else "", row.get("ftc_domain"))


def url_key(url: str | None) -> str:
    """공유 URL 판정 키 — 정제 · 소문자 · 끝 '/' 제거."""
    u = sanitize(url or "") or (url or "").strip()
    return u.lower().rstrip("/")


def _crawl_failed_for_good(row: dict) -> bool:
    """사이트가 없다고 볼 만한 크롤링 실패인가 (재시도 소진 · 에러 문서 · 4xx 등)."""
    from .batch.store import is_transient_crawl_error

    err = (row.get("crawl_error") or "").strip()
    if not err or (row.get("crawl_phone") or row.get("crawl_email")):
        return False
    if any(k in err for k in ("연락처 미발견", "robots", "로그인")):
        return False
    state = row.get("crawl_state") or ""
    if state == "failed_tmp":
        return False                     # 재시도 대기 — 아직 모른다
    if state == "failed":
        return True                      # 배치: 일시 오류도 재시도 2회 소진
    return not is_transient_crawl_error(err)   # 500건 파이프라인(재시도 없음): 일시 오류는 유지


def trusted_view(row: dict) -> dict:
    """병합에 쓸 값만 남긴 사본. 믿을 수 없는 NAVER 값 · 그 링크의 크롤링 결과 · 공유 URL 연락처를 비운다."""
    shared = int(row.get("_shared_url") or 0) >= SHARED_URL_MIN
    if naver_trusted(row) and not shared:
        return row
    view = dict(row)
    if shared:
        for k in ("crawl_phone", "crawl_mobile", "crawl_email"):
            view[k] = ""
    if naver_trusted(row):
        return view
    for k in _NAVER_VALUES:
        view[k] = ""
    crawled = sanitize(row.get("crawl_url") or "") or (row.get("crawl_url") or "").strip()
    if crawled and crawled == sanitize(row.get("naver_link") or "") \
            and crawled != sanitize(row.get("ftc_domain") or ""):
        for k in _CRAWL_VALUES:
            view[k] = ""
    return view

# 건물번호 · 번지까지 있는 '완전한' 주소인가. 공정위 도로명주소는 60%(251만 중 151만)가
# '…봉은사로' 처럼 건물번호 없이 신고돼 있다 (10/08 전수 확인).
_ADDR_COMPLETE = re.compile(r"(로|길)\s?\d|(동|리|가)\s?(산\s?)?\d|\d+\s*번지")


def address_complete(addr: str | None) -> bool:
    return bool(_ADDR_COMPLETE.search(addr or ""))


# (출력 필드, 후보 컬럼 우선순위, 출처 라벨)
RULES: list[tuple[str, tuple[str, ...], dict[str, str]]] = [
    ("final_company_name",
     ("ftc_company_name", "kcw_name", "naver_title", "input_name"),
     {"ftc_company_name": "공정위", "kcw_name": "근로복지공단", "naver_title": "NAVER",
      "input_name": "입력"}),
    ("final_address",
     ("ftc_road_address", "kcw_address", "naver_road_address", "crawl_address",
      "naver_address", "ftc_lot_address", "nps_address"),
     {"ftc_road_address": "공정위", "kcw_address": "근로복지공단", "naver_road_address": "NAVER",
      "crawl_address": "홈페이지", "naver_address": "NAVER",
      "ftc_lot_address": "공정위", "nps_address": "국민연금"}),
    ("final_phone",
     ("crawl_phone", "ftc_phone"),
     {"crawl_phone": "홈페이지", "ftc_phone": "공정위"}),
    # 공정위 이메일은 대표자 이메일(rprsvEmladr)뿐이라 채택하지 않는다 (2026-10-08 결정).
    ("final_email",
     ("crawl_email",),
     {"crawl_email": "홈페이지"}),
]

# 값이 여러 개 들어 있을 때(크롤링 결과) 대표 1건만 쓴다.
_MULTI = "; "


def _first(value: str | None, col: str = "") -> str:
    """'a; b; c' 형태에서 첫 값만 취한다. 공정위 자리표시('N/A' · '010-개인정보')는 빈 값."""
    v = (value or "").strip()
    if not v:
        return ""
    v = v.split(_MULTI)[0].strip() if _MULTI in v else v
    if col.startswith("ftc_"):
        from .ftc_mllbs import clean_value
        v = clean_value(col[4:], v)
    return v


def merge_row(row: dict) -> dict:
    """한 행의 final_* 필드를 채운 dict 를 돌려준다 (행은 수정하지 않음)."""
    region_check = naver_region(row)     # 원본 기준 (trusted_view 가 NAVER 주소를 비우기 전)
    row = trusted_view(row)
    out: dict[str, str] = {}
    sources: list[str] = []

    for field, candidates, labels in RULES:
        vals = [(col, _first(row.get(col), col)) for col in candidates]
        vals = [(col, v) for col, v in vals if v]
        if field == "final_address":
            # 우선순위 안에서 건물번호·번지가 있는 주소를 먼저, 없을 때만 불완전한 주소
            vals = [x for x in vals if address_complete(x[1])] + [x for x in vals if not address_complete(x[1])]
        if vals:
            col, val = vals[0]
            if field == "final_company_name":
                val = strip_company_forms(val)      # (주) · 주식회사 등은 빼고 저장 (10/26)
            out[field] = val
            if field == "final_address":
                # 국민연금 주소는 건물번호가 없다 — 최후 순위 · 비고로 구분 (10/08 사용자 결정)
                complete = address_complete(val)
                if col == "nps_address":
                    out["address_note"] = "국민연금 주소" if complete else "국민연금 주소(건물번호 없음)"
                else:
                    out["address_note"] = "" if complete else "건물번호 없음"
            label = labels[col]
            if label not in sources and label != "입력":
                sources.append(label)
        else:
            out[field] = ""
            if field == "final_address":
                out["address_note"] = ""

    # 사이트 주소: 실제로 방문한 URL 이 있으면 그것을, 없으면 정제 후보를.
    site = (row.get("crawl_url") or "").strip()
    note = ""
    if site and _crawl_failed_for_good(row):
        site, note = "", "접속 실패로 제외"
    if not site and not note:
        site = crawl_target(row)[0]
    shared = int(row.get("_shared_url") or 0)
    if site and shared >= SHARED_URL_MIN:
        note = f"공유 사이트({shared}곳) — 연락처 미사용"
    out["final_homepage"] = site
    out["site_note"] = note
    out["naver_region_check"] = region_check

    # 우편번호: 이미 이 주소로 찾은 값이 있으면 유지. 주소가 바뀌었으면 비워서 다시 찾게 한다
    # (조회는 batch.stages.stage_zip · pipeline run 5 가 도로명주소 API 로 한다).
    if out["final_address"] and out["final_address"] == (row.get("zip_addr") or ""):
        out["final_zipcode"] = row.get("final_zipcode") or ""
        out["zip_source"] = row.get("zip_source") or ""
        out["zip_addr"] = row.get("zip_addr") or ""
    else:
        out["final_zipcode"] = out["zip_source"] = out["zip_addr"] = ""

    # 입력 상호명을 그대로 돌려준 것은 '수집'이 아니므로 점수에서 뺀다.
    echoed_name = (out["final_company_name"]
                   and out["final_company_name"] == strip_company_forms(row.get("input_name"))
                   and "공정위" not in sources and "NAVER" not in sources)
    scored = ("final_company_name", "final_address", "final_phone",
              "final_email", "final_homepage")
    filled = sum(1 for f in scored if out.get(f))
    earned = filled - (1 if echoed_name else 0)
    out["confidence_score"] = f"{earned / len(scored):.2f}"
    out["source"] = ",".join(sources)
    out["status"] = ("완료" if filled == len(scored) else
                     "부분" if earned else "미확보")

    # 업종은 신뢰도 점수에 넣지 않는다 — 수집 항목이 아니라 파생값이다.
    out.update(classify_industry({**row, **out}))
    from .ksic import classify as classify_ksic
    out.update(classify_ksic(row))
    return out
