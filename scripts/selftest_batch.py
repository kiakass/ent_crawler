#!/usr/bin/env python3
"""배치 · 병합 규칙 회귀 시험 (2026-10-08). 외부 API(국세청 제외 · NAVER 1회 · 도로명주소 2회 · 공정위 1페이지)를 부른다.

    python scripts/selftest_batch.py        → 마지막 줄 'N/N 통과'
"""
import sys, shutil, json, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from biz_crawler.batch.store import JobStore, Registry, BATCH_ROOT
from biz_crawler.batch.quota import Quota
from biz_crawler.batch import stages
S = Path(tempfile.mkdtemp(prefix="selftest_")); ok = []
def check(name, cond):
    cond = bool(cond); ok.append(cond); print(("PASS " if cond else "FAIL ") + name)

job = "TEST_UNIT"; shutil.rmtree(BATCH_ROOT / job, ignore_errors=True)
st = JobStore(job)
st.import_rows([("1248100998", "가상상호A"), ("1248100998", "가상상호B"), ("", "번호없음")], source="unit")
r1, r2, r3 = st.select("1=1")
check("번호 없는 행은 국세청·공정위 skipped", r3["nts_state"] == "skipped" and r3["ftc_state"] == "skipped")

# 1) NAVER 가 블로그만 줌 → 크롤링 pending (URL 출처 무관하게 시작)
row = {**r1, "naver_link": "https://blog.naver.com/example"}
# 매칭 안 된(low_score) NAVER 링크는 크롤링 대상이 아니다
st.update(r1["row_no"], {"naver_link": row["naver_link"], "naver_status": "low_score", "naver_match_score": "0.4"})
stages.refresh_crawl_target(st, st.select("row_no=1")[0])
check("low_score NAVER 링크는 크롤링 안 함", st.select("row_no=1")[0]["crawl_state"] == "waiting")
st.update(r1["row_no"], {"naver_status": "matched", "naver_match_score": "0.95"})
stages.refresh_crawl_target(st, st.select("row_no=1")[0])
check("NAVER 링크만으로 크롤링 시작", st.select("row_no=1")[0]["crawl_state"] == "pending")
# 블로그로 크롤링 끝났다고 가정
st.update(1, {"crawl_state": "done", "crawl_url": "https://blog.naver.com/example", "crawl_phone": "02-000-0000"})
# 2) 공정위가 나중에 자체 도메인을 줌 → 우선순위 높은 URL → 다시 pending
st.update(1, {"ftc_domain": "nonexistent-domain-for-test.invalid"})
stages.refresh_crawl_target(st, st.select("row_no=1")[0])
check("늦게 온 자체 도메인으로 재크롤링 예약", st.select("row_no=1")[0]["crawl_state"] == "pending")
# 3) 새 URL 실패 → 이전 연락처 유지
res = stages.stage_crawl(st, max_rows=5, delay=0, timeout=3)
r = st.select("row_no=1")[0]
check("새 URL 실패해도 이전 전화 유지", r["crawl_phone"] == "02-000-0000" and res.extra["kept_previous"] == 1)
check("이전 값이 있으면 done 처리", r["crawl_state"] == "done")
# 4) 마켓 URL 은 크롤링 안 함
st.update(2, {"naver_link": "https://smartstore.naver.com/x", "naver_status": "matched", "naver_match_score": "1.0"})
stages.refresh_crawl_target(st, st.select("row_no=2")[0])
check("마켓 URL 은 skipped", st.select("row_no=2")[0]["crawl_state"] == "skipped")
# 5) 일시 오류 → failed_tmp, 두 번 뒤엔 failed
st.update(2, {"naver_link": "", "ftc_domain": "another-nonexistent.invalid", "crawl_state": "pending", "crawl_url": ""})
stages.stage_crawl(st, max_rows=1, delay=0, timeout=3)
r = st.select("row_no=2")[0]
check(f"DNS 실패는 일시 오류로 재시도 예약 ({r['crawl_state']} tries={r['crawl_tries']})",
      r["crawl_state"] == "failed_tmp" and r["crawl_tries"] == "1")
st.update(2, {"crawl_state": "failed_tmp", "crawl_tries": "2", "crawl_next_at": "2000-01-01T00:00:00+09:00"})
stages.stage_crawl(st, max_rows=1, delay=0, timeout=3)
check("재시도 2회 소진 후 failed", st.select("row_no=2")[0]["crawl_state"] == "failed")
# 6) 병합: 공정위 이메일은 채택 안 함
st.update(3, {"ftc_email": "someone@example.com", "ftc_phone": "02-111-2222"})
stages.stage_merge(st)
r = st.select("row_no=3")[0]
check("공정위 이메일 미채택 · 공정위 전화는 채택", r["final_email"] == "" and r["final_phone"] == "02-111-2222")
check("업종 기본값 기타미분류", r["ind_small"] == "기타미분류" and r["ind_basis"] == "기본값")

# 6b) 짧은 상호(3자 이하)는 0.85 미만이면 NAVER 값을 쓰지 않는다
from biz_crawler.merge import naver_trusted, merge_row
check("짧은 상호 0.67 매칭은 불신", not naver_trusted({"input_name": "가나다", "naver_status": "matched", "naver_match_score": "0.667"}))
check("짧은 상호 0.9 매칭은 신뢰", naver_trusted({"input_name": "가나다", "naver_status": "matched", "naver_match_score": "0.9"}))
m = merge_row({"input_name": "가상상호긴이름", "naver_status": "low_score", "naver_title": "다른업소",
               "naver_road_address": "서울 어딘가 1", "naver_link": "https://other.example.com",
               "crawl_url": "https://other.example.com", "crawl_phone": "02-999-9999"})
check("low_score 업소의 상호·주소·사이트·크롤링 연락처는 결과에서 제외",
      m["final_company_name"] == "가상상호긴이름" and m["final_address"] == "" and m["final_homepage"] == "" and m["final_phone"] == "")

# 6c) 지역 검증 · 공유 URL · 실패 URL · 공정위 자리표시 (10/08 검증 후)
base = {"input_name": "가상상호긴이름", "naver_status": "matched", "naver_match_score": "1.0",
        "naver_title": "가상상호긴이름", "naver_road_address": "부산광역시 해운대구 1", "naver_link": "https://own.example.com"}
m = merge_row({**base, "nps_address": "서울특별시 강남구 테헤란로 1"})
check("확정 주소와 시·도 다르면 NAVER 값 제외 · 검증=불일치",
      m["naver_region_check"] == "불일치" and m["final_address"] == "서울특별시 강남구 테헤란로 1"
      and m["address_note"] == "국민연금 주소" and m["final_homepage"] == "")
m = merge_row({**base, "nps_address": "부산광역시 해운대구 센텀로 2"})
check("시·도 같으면 NAVER 값 사용 · 검증=일치", m["naver_region_check"] == "일치" and m["final_address"].startswith("부산"))
m = merge_row({**base})
check("확정 주소 없으면 검증불가 (값은 사용)", m["naver_region_check"] == "검증불가" and m["final_homepage"])
m = merge_row({**base, "crawl_url": "https://own.example.com", "crawl_phone": "02-111-2222", "crawl_email": "a@own.example.com", "_shared_url": 7})
check("공유 URL(7곳) 연락처 미사용 · 사이트는 비고와 함께 유지",
      m["final_phone"] == "" and m["final_email"] == "" and m["final_homepage"] and m["site_note"].startswith("공유 사이트(7곳)"))
m = merge_row({**base, "crawl_url": "https://own.example.com", "crawl_state": "failed", "crawl_error": "ConnectError: x"})
check("재시도 소진 실패 URL 은 사이트주소 제외", m["final_homepage"] == "" and m["site_note"] == "접속 실패로 제외")
m = merge_row({**base, "crawl_url": "https://own.example.com", "crawl_state": "failed_tmp", "crawl_error": "ConnectError: x"})
check("재시도 대기 URL 은 유지", m["final_homepage"] == "https://own.example.com")
m = merge_row({**base, "crawl_url": "https://own.example.com", "crawl_state": "failed", "crawl_error": "robots.txt 차단"})
check("robots 차단 URL 은 유지", m["final_homepage"] == "https://own.example.com")
m = merge_row({"input_name": "가상상호긴이름", "ftc_company_name": "가상상호긴이름", "ftc_phone": "010-개인정보", "ftc_road_address": "N/A",
               "ftc_lot_address": "서울특별시 중구 1"})
check("공정위 '010-개인정보' · 'N/A' 는 빈 값", m["final_phone"] == "" and m["final_address"] == "서울특별시 중구 1")

# 6d) 우편번호 — 도로명주소 API · 첫 결과 · 주소 바뀌면 다시
from biz_crawler.juso import JusoClient, search_keyword
with JusoClient() as jc:
    z = jc.zip_for("서울특별시 강남구 강남대로 390 (역삼동) 1층")
    check(f"도로명주소 우편번호 ({z.status})", z.zipcode == "06232")
    z = jc.zip_for("경기도 광주시 도척면 도척윗로")
    check(f"여러 건이면 첫 결과 ({z.status})", z.status in ("agree", "exact", "first") and len(z.zipcode) == 5)
check("검색어 정리 (층·호 제거)", search_keyword("서울특별시 중구 퇴계로 72 1층 105호") == "서울특별시 중구 퇴계로 72")
m = merge_row({"input_name": "가상상호긴이름", "ftc_company_name": "가상상호긴이름", "ftc_road_address": "서울특별시 중구 퇴계로 72",
               "final_zipcode": "04628", "zip_source": "도로명주소", "zip_addr": "서울특별시 중구 퇴계로 72"})
check("같은 주소면 우편번호 유지", m["final_zipcode"] == "04628")
m = merge_row({"input_name": "가상상호긴이름", "ftc_company_name": "가상상호긴이름", "ftc_road_address": "서울특별시 중구 퇴계로 80",
               "final_zipcode": "04628", "zip_source": "도로명주소", "zip_addr": "서울특별시 중구 퇴계로 72"})
check("주소가 바뀌면 우편번호 비움(다시 조회)", m["final_zipcode"] == "" and m["zip_addr"] == "")

# 6e) 완전한 주소 우선 · NAVER 검색 시 법인 표기 제거
m = merge_row({"input_name": "가상상호긴이름", "ftc_company_name": "가상상호긴이름", "ftc_road_address": "서울특별시 강남구 봉은사로",
               "naver_status": "matched", "naver_match_score": "1.0", "naver_title": "가상상호긴이름",
               "naver_road_address": "서울특별시 강남구 봉은사로 448"})
check("건물번호 없는 공정위 주소보다 완전한 NAVER 주소", m["final_address"] == "서울특별시 강남구 봉은사로 448")
m = merge_row({"input_name": "가상상호긴이름", "ftc_company_name": "가상상호긴이름", "ftc_road_address": "서울특별시 강남구 봉은사로"})
check("완전한 후보가 없으면 불완전 주소라도 사용", m["final_address"] == "서울특별시 강남구 봉은사로")
from biz_crawler.naver_local import NaverLocalClient
with NaverLocalClient(delay=0) as nc:
    r = nc.resolve("주식회사스타벅스커피코리아")
check(f"NAVER 1차 검색어에서 법인 표기 제거 (질의='{r.query}')", "주식회사" not in r.query)

# 6f) 국민연금 주소 최후 순위 · 비고 · 우편번호
m = merge_row({"input_name": "가상상호긴이름", "nps_status": "matched", "nps_address": "서울특별시 강남구 봉은사로", "nps_zip": "06164"})
check("국민연금 주소 최후 순위로 사용 · 비고", m["final_address"] == "서울특별시 강남구 봉은사로" and m["address_note"] == "국민연금 주소(건물번호 없음)")
m = merge_row({"input_name": "가상상호긴이름", "nps_address": "서울특별시 강남구 봉은사로", "ftc_lot_address": "서울특별시 강남구 삼성동 114"})
check("완전한 주소가 있으면 국민연금 주소 안 씀", m["final_address"] == "서울특별시 강남구 삼성동 114" and m["address_note"] == "")
from biz_crawler.batch.stages import resolve_zip
zr = resolve_zip({"final_address": "서울특별시 강남구 봉은사로", "nps_address": "서울특별시 강남구 봉은사로", "nps_zip": "06164"}, None, None, None)
check("국민연금 주소면 국민연금 우편번호 (API 호출 없음)", zr["final_zipcode"] == "06164" and zr["zip_source"] == "국민연금")

# 6g) 표준산업분류(KSIC 11차) — 국민연금 업종명 대조
from biz_crawler.ksic import lookup as ksic_lookup
check("KSIC 11차 정확 일치", ksic_lookup("커피 전문점") == ("56221", "커피 전문점"))
check("'/' 로 묶인 업종명은 앞쪽부터", ksic_lookup("통관 대리 및 관련서비스업 / 화물 운송 중개  대리")[0] == "52991")
m = merge_row({"input_name": "가상상호긴이름", "nps_status": "matched", "nps_code": "722000", "nps_industry": "응용 소프트웨어 개발 및 공급업"})
check("병합 결과에 표준산업분류코드", m["ksic_code"] == "58222")
m = merge_row({"input_name": "가상상호긴이름", "nps_status": "matched", "nps_code": "999999", "nps_industry": "BIZ_NO미존재사업장"})
check("업종 아닌 국민연금 값은 코드 없음", m["ksic_code"] == "")

# 7) 공정위 다운로드 1페이지 (임시 레지스트리) → 조인
for f in S.glob("tmp_registry.db*"): f.unlink()
reg = Registry(S / "tmp_registry.db"); q = Quota(reg)
d = stages.ftc_dump(reg, q, max_pages=1)
n = reg.conn.execute("select count(*) from ftc_registry").fetchone()[0]
check(f"다운로드 1페이지 적재 {n:,}건 · 체크포인트 page={reg.get('ftc_dump_page')} · 전체 {reg.get('ftc_dump_pages')}페이지",
      n > 9000 and reg.get("ftc_dump_page") == "1" and d.status == "partial")
check("미완료 다운로드로는 조인하지 않음", not reg.get("ftc_dump_done"))
brno = reg.conn.execute("select brno from ftc_registry limit 1").fetchone()[0]
j2 = "TEST_UNIT2"; shutil.rmtree(BATCH_ROOT / j2, ignore_errors=True)
st2 = JobStore(j2); st2.import_rows([(brno, "임의"), ("1234567890", "없는번호")], source="unit")
reg.put("ftc_dump_done", "2026-10-08")      # 완료로 표시하고 조인 경로 검증
jr = stages.stage_ftc(st2, reg, q)
a, b = st2.select("1=1")
check(f"조인 모드 ({jr.extra.get('mode')}) — 등록 사업자 적중 · 미등록은 미신고",
      jr.extra.get("mode") == "join" and a["ftc_found"] == "True" and b["ftc_error"] == "통신판매업 미신고")
check("조인 결과에 대표자 필드 없음", "rprsv" not in json.dumps(reg.conn.execute("select fields from ftc_registry limit 1").fetchone()[0]))

# 8) 근로복지공단 — 상호 없이 사업자번호만 받은 행 (10/16, 외부 호출 1회)
from biz_crawler.kcomwel import pick_site
items = [{"saeopjangNm": "가상(주) 현장A", "sangsiInwonCnt": "50", "seongripDt": "20200101"},
         {"saeopjangNm": "가상(주)", "sangsiInwonCnt": "3", "seongripDt": "20100101"},
         {"saeopjangNm": "가상(주)", "sangsiInwonCnt": "3", "seongripDt": "20100101"}]
check("대표 사업장은 최다 이름 (상시인원보다 우선)", pick_site(items)["saeopjangNm"] == "가상(주)")
j3 = "TEST_UNIT3"; shutil.rmtree(BATCH_ROOT / j3, ignore_errors=True)
st3 = JobStore(j3)
st3.import_rows([("1248100998", ""), ("1248100998", ""), ("1248100998", "가상상호")], source="unit")
a, b, c = st3.select("1=1")
check("상호 빈 행만 근로복지공단 대상 · 국민연금 · NAVER 는 대기",
      a["kcw_state"] == "pending" and c["kcw_state"] == "skipped"
      and a["nps_state"] == "pending" and a["naver_state"] == "pending")
st3.update(1, {"ftc_state": "done"}); st3.update(2, {"ftc_state": "done", "ftc_company_name": "가상공정위상호"})
# 근로복지공단 조회 전에는 이름 없는 행을 국민연금이 건너뛰지 않는다
stages.stage_nps(st3, q, use_api=False)
check("근로복지공단 대기 중이면 국민연금 skipped 로 확정하지 않음", st3.select("row_no=1")[0]["nps_state"] != "skipped")
kr = stages.stage_kcw(st3, q)
a, b = st3.select("row_no IN (1,2)")
check(f"근로복지공단 상호 확보 · 공정위 상호 있으면 조회 안 함 (호출 {kr.calls}회)",
      a["kcw_status"] == "matched" and a["kcw_name"] and len(a["kcw_zip"]) == 5
      and b["kcw_state"] == "skipped" and kr.calls == 1)
check("국민연금 · NAVER 상호에 근로복지공단 상호 사용",
      stages._known_name(a) == a["kcw_name"] and stages._query_name(a) == a["kcw_name"])
m = merge_row({"kcw_name": "근복상호", "kcw_address": "서울특별시 중구 세종대로 110", "kcw_zip": "04524"})
check("병합: 근로복지공단 상호 · 주소", m["final_company_name"] == "근복상호" and "근로복지공단" in m["source"])
m = merge_row({"ftc_company_name": "공정위상호", "kcw_name": "근복상호"})
check("병합: 공정위 상호가 근로복지공단보다 먼저", m["final_company_name"] == "공정위상호")
z = stages.resolve_zip({"final_address": "서울특별시 중구 세종대로 110", "kcw_address": "서울특별시 중구 세종대로 110",
                        "kcw_zip": "04524"}, None, reg, None)
check("최종 주소가 근로복지공단 주소면 그 우편번호 (API 호출 없음)", z["final_zipcode"] == "04524" and z["zip_source"] == "근로복지공단")
st3.close()

# 9) NAVER 키 2개 — 키1 예산이 0 이면 키2 로 (호출 없음, 가짜 키)
import os
_env = {k: os.environ.get(k) for k in ("NCP_APIGW_KEY_ID_2", "NCP_APIGW_KEY_2")}
os.environ.update({"NCP_APIGW_KEY_ID_2": "dummy-id", "NCP_APIGW_KEY_2": "dummy-key"})
with stages._NaverPool(Quota(reg, caps={"naver": 0})) as pool:
    slot = pool.take(2)
    check("키1 예산 소진 시 키2 장부(naver@2)로 넘어감", slot is not None and slot[0] == "naver@2")
with stages._NaverPool(Quota(reg, caps={"naver": 0, "naver@2": 0})) as pool:
    check("두 키 모두 소진이면 정지", pool.take(2) is None and "키 2개" in pool.stop_reason())
for k, v in _env.items():
    if v is None: os.environ.pop(k, None)
    else: os.environ[k] = v
st.close(); st2.close(); reg.close()
shutil.rmtree(BATCH_ROOT / job); shutil.rmtree(BATCH_ROOT / j2); shutil.rmtree(BATCH_ROOT / j3)
print(f"\n{sum(ok)}/{len(ok)} 통과")
