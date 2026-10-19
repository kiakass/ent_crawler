"""마스터 테이블 컬럼 정의 — pipeline.py(CSV)와 배치(SQLite)가 함께 쓴다.

사업자번호 1건 = 1행. 스텝마다 접두사(nts_ ftc_ naver_ crawl_ nps_ final_)
컬럼만 채운다.
"""

BASE_FIELDS = ["row_no", "b_no", "b_no_display", "input_name"]

NTS_FIELDS = ["nts_registered", "nts_is_active", "nts_b_stt", "nts_b_stt_cd",
              "nts_tax_type", "nts_end_dt", "nts_error"]

FTC_FIELDS = ["ftc_found", "ftc_company_name", "ftc_corp_yn", "ftc_oper_status",
              "ftc_phone", "ftc_fax", "ftc_road_address", "ftc_lot_address",
              "ftc_domain", "ftc_email", "ftc_error"]

NAVER_FIELDS = ["naver_status", "naver_title", "naver_link", "naver_category",
                "naver_address", "naver_road_address", "naver_match_score",
                "naver_candidates"]

CRAWL_FIELDS = ["crawl_url", "crawl_url_kind", "crawl_phone", "crawl_mobile",
                "crawl_email", "crawl_address", "crawl_source_url",
                "crawl_pages", "crawl_error"]

# 국민연금 사업장 — 업종코드 (biz_crawler/nps_bplc.py)
NPS_FIELDS = ["nps_status", "nps_wkpl_name", "nps_code", "nps_industry",
              "nps_match_score", "nps_active", "nps_error",
              "nps_address",          # 사업장 도로명 주소(건물번호 없음) — NAVER 지역 키워드 · 지역 검증용 (10/08)
              "nps_zip"]              # 사업장 우편번호 — 도로명주소 조회 실패 시 보조

# 근로복지공단 고용·산재 가입 사업장 — 상호 없이 사업자번호만 받은 행의 상호 · 주소 (biz_crawler/kcomwel.py, 10/16)
KCW_FIELDS = ["kcw_status", "kcw_name", "kcw_address", "kcw_zip", "kcw_industry",
              "kcw_sites", "kcw_error"]

FINAL_FIELDS = ["final_company_name", "final_address", "final_phone",
                "final_email", "final_homepage", "confidence_score",
                "source", "status", "collected_at",
                # 업종 (biz_crawler/industry.py) — 병합 때 함께 계산
                "ind_naver_l1", "ind_naver_l2", "ind_naver_l3",
                "ind_large", "ind_mid", "ind_small", "ind_basis",
                # 10/08 검증 후 추가 — NAVER 지역 검증 결과 · 사이트주소 비고(공유 · 실패)
                "naver_region_check", "site_note",
                # 우편번호 (10/08) — zip_addr 는 우편번호를 찾은 주소 (주소가 바뀌면 다시 찾는다)
                "final_zipcode", "zip_source", "zip_addr",
                "address_note",       # 주소비고: 건물번호 없음 · 국민연금 주소 (10/08)
                "ksic_code", "ksic_name",   # 표준산업분류 11차 세세분류 (국민연금 업종명 대조, 10/08)
                "ksic_basis"]              # 표준산업분류근거: 국민연금 · NAVER보정 · NAVER추정 (10/19)

ALL_FIELDS = (BASE_FIELDS + NTS_FIELDS + FTC_FIELDS + NAVER_FIELDS
              + CRAWL_FIELDS + NPS_FIELDS + KCW_FIELDS + FINAL_FIELDS)
