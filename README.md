# ent_crawler — 사업자정보 수집기

사업자번호 + 상호명 목록을 넣으면 **상호 · 주소 · 우편번호 · 대표전화 · 이메일 · 사이트주소 · 업종(대/중/소) · 표준산업분류**를
공공 API(국세청 · 공정위 · 국민연금 · 도로명주소), NAVER 지역검색, 홈페이지 크롤링으로 채운다.

대량 목록(수만~수십만 건)은 **일배치**로 돌린다. API 마다 하루 한도가 있어서 한 번에 끝나지 않고,
매일 정해진 시각에 한도만큼 처리하고 다음 날 이어 간다.

- 실행 환경: **macOS** (자동 실행은 launchd) · **Python 3.10 이상** (개발은 3.14)
- 저장소에는 코드만 있다. API 키 · 입력 목록 · 수집 결과 · 로그는 들어 있지 않고 들어가서도 안 된다 ([10. 주의](#10-주의--개인정보--보안))

---

## 목차

1. [처음 설치](#1-처음-설치)
2. [API 키 발급 · `.env`](#2-api-키-발급--env)
3. [저장소에 없는 준비 파일](#3-저장소에-없는-준비-파일)
4. [동작 확인](#4-동작-확인)
5. [배치 돌리기 — 수동](#5-배치-돌리기--수동)
6. [배치 돌리기 — 매일 자동 (launchd)](#6-배치-돌리기--매일-자동-launchd)
7. [매일 확인 · 결과 파일](#7-매일-확인--결과-파일)
8. [명령 레퍼런스](#8-명령-레퍼런스)
9. [문제 해결](#9-문제-해결)
10. [주의 — 개인정보 · 보안](#10-주의--개인정보--보안)
11. [코드 구성](#11-코드-구성)

---

## 1. 처음 설치

```bash
git clone https://github.com/kiakass/ent_crawler.git
cd ent_crawler

python3 -m venv .venv                 # 이름은 반드시 .venv — 자동 실행 스크립트가 이 경로를 쓴다
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium # 플랫폼(블로그·스마트스토어 등) 크롤링용

cp .env.example .env                  # 2절에서 키를 채운다
chmod 600 .env
```

### 사내망(TLS 재서명 프록시)에서 쓸 때

사내 프록시가 HTTPS 인증서를 다시 서명하기 때문에 Python 기본 인증서 묶음(certifi)으로는
`CERTIFICATE_VERIFY_FAILED` 가 난다. macOS 키체인의 사내 루트 CA 를 합친 묶음을 만든다.

```bash
python scripts/setup_ca.py                  # 자동 탐지 → certs/ca-bundle.pem
python scripts/setup_ca.py --ca-name 회사CA명  # 자동 탐지가 실패하면 이름 지정
export SSL_CERT_FILE=$PWD/certs/ca-bundle.pem
```

모든 외부 호출은 `biz_crawler/config.py` 의 `http_client()` 를 거치며 `certs/ca-bundle.pem` 이 있으면 자동으로 쓴다.
인증서 검증을 끄는(`verify=False`) 방식은 쓰지 않는다. 사내망 밖에서는 이 단계를 건너뛰어도 된다.

---

## 2. API 키 발급 · `.env`

**키는 각자 발급받아 쓴다.** 한도는 키 단위인데, 사용량 장부는 각 PC 의 로컬 DB(`data/batch/registry.db`)에 있다.
두 사람이 같은 키로 따로 돌리면 서로의 사용량을 모르고 함께 한도를 넘긴다.

| `.env` 변수 | 발급처 | 용도 · 일 한도 |
| --- | --- | --- |
| `ODCLOUD_SERVICE_KEY` | data.go.kr — "국세청_사업자등록정보 진위확인 및 상태조회 서비스" 활용신청. 같은 키로 "공정위 통신판매사업자 등록상세" · "국민연금공단 가입 사업장 내역(V2)" 도 활용신청 | 국세청 100만 · 공정위 1만 · 국민연금 1만 |
| `NCP_APIGW_KEY_ID` · `NCP_APIGW_KEY` | console.ncloud.com — API Gateway / Maps 이용 신청 → Application 등록 (지역검색) | NAVER 2.5만 · 월 77.5만 |
| `JUSO_CONFM_KEY` | business.juso.go.kr — 도로명주소 검색 API 승인키 | 우편번호 (개발키는 만료일 있음) |
| `CRAWLER_CONTACT` | 운영 공용 메일 주소 | 크롤링 요청의 `From` 헤더 (사이트 운영자 연락 창구) |
| `CA_BUNDLE` | (선택) | 비워두면 `certs/ca-bundle.pem` → certifi 순 |

data.go.kr 의 키는 **"일반 인증키(Encoding)" 값을 그대로** 붙여넣는다 (디코딩은 코드가 한다).

---

## 3. 저장소에 없는 준비 파일

| 파일 | 필수 | 없으면 | 구하는 법 |
| --- | --- | --- | --- |
| `design/업종분류.md` | **필수** | 병합 단계가 파일 없음으로 실패 | 사내 업종분류표 (CSV 형식: `업종대,업종중,업종소`). 담당자에게 받아 `design/` 에 둔다 |
| `data/external/nps/*.csv` | 권장 | 국민연금 업종을 API 로만 채워 하루 약 4,700행밖에 못 함 | data.go.kr 파일데이터 **15083277** "국민연금공단_국민연금 가입 사업장 내역" 월별 CSV(cp949) 를 받아 그대로 둔다. 여러 개면 이름순 마지막 파일을 쓴다 |
| `data/external/ksic/ksic11_세세분류.csv` · `ksic10_세세분류.csv` | 선택 | 표준산업분류 코드 칸이 비어 있음 (다른 항목은 정상) | 통계청 "한국표준산업분류 제11차 개정 분류체계(대-중-소-세-세세 구분)" xlsx 를 받아 `python scripts/extract_ksic.py <xlsx 경로>` |

공정위 데이터는 따로 받을 필요 없다. 배치가 처음 돌 때 전체(약 277페이지)를 받아 로컬에 쌓는다.

---

## 4. 동작 확인

```bash
source .venv/bin/activate
export SSL_CERT_FILE=$PWD/certs/ca-bundle.pem   # 사내망일 때

python scripts/selftest_batch.py                 # 마지막 줄 'N/N 통과'
```

외부 API 를 소량(NAVER 1회 · 도로명주소 2회 · 공정위 1페이지) 부른다. 키가 틀리면 여기서 드러난다.

---

## 5. 배치 돌리기 — 수동

### 5.1 입력 파일

CSV 한 개. 인코딩은 **UTF-8(BOM 포함 가능) 또는 cp949** 를 자동 판별한다.

| 컬럼 | 기본 이름 | 다르면 |
| --- | --- | --- |
| 사업자번호 (하이픈 유무 무관) | `IK_BRNO` | `--bizno-col 컬럼명` |
| 상호명 | `EPM_CUST_NM` | `--name-col 컬럼명` |

입력 파일은 `input/` 에 두는 것을 권장한다 (`.gitignore` 로 커밋되지 않는다 — CSV 는 모두 제외).

### 5.2 잡(job) 만들기

목록 하나 = **잡** 하나. 잡 이름은 영문·숫자로 짓는다 (예: `SME1101`). 잡마다 상태 DB 가 따로 생긴다.

```bash
# 1) 적재 — 처음엔 소량으로 시험해 보는 것을 권장
python scripts/batch.py import input/목록.csv --job TEST100 --limit 100
python scripts/batch.py import input/목록.csv --job SME1101          # 전체

# 2) 일정 추정 — API 호출 0회
python scripts/batch.py plan --job SME1101
```

`plan` 은 단계별 대기 행 수, NAVER 예상 호출 수와 일수, 예상 종료일을 보여준다.
대량 목록은 NAVER 한도(하루 23,750회 사용) 때문에 **약 10만 건에 1주일** 정도 걸린다.

### 5.3 하루치 실행

```bash
python scripts/batch.py daily --job TEST100 --with-crawl     # 크롤링까지 이 프로세스에서
```

`daily` 는 아래 순서로 돌고, 각 단계는 **그날 남은 한도만큼** 처리하고 멈춘다. 남은 행은 다음 실행이 이어받는다.

| 순서 | 단계 | 하는 일 |
| --- | --- | --- |
| 1 | `nts` 국세청 | 계속/휴업/폐업 상태 · 폐업일 (100건/호출) |
| 2 | `ftc` 공정위 | 통신판매업 신고 정보 — 상호 · 주소 · 전화 · 도메인 |
| 3 | `nps` 국민연금 | 월별 파일 조인 → 안 되는 행만 API — 업종코드 · 주소 · 우편번호 |
| 4 | `naver` NAVER | 지역검색으로 업소 매칭 — 주소 · 홈페이지 링크 · 카테고리. 1차 → 미매칭은 질의를 바꿔 재시도 |
| 5 | `crawl` 크롤링 | 확보된 홈페이지에서 전화 · 이메일 (`--with-crawl` 일 때만. 자동 실행에서는 상주 워커가 함) |
| 6 | `merge` 병합 | 소스별 값을 골라 최종 칸 · 업종 대/중/소 계산 (값이 바뀐 행만) |
| 7 | `zip` 우편번호 | 최종 주소 → 도로명주소 API |
| 8 | `export` 내보내기 | `snapshot.csv` · `delta_YYYYMMDD.csv` · 실적 1행 |

한 단계가 오류나 한도로 멈춰도 다음 단계는 돈다. 같은 날 여러 번 돌려도 한도 장부를 공유하므로 한도를 넘지 않는다.
상태가 `partial` 이면 **하루 한도를 다 써서 정상적으로 멈춘 것**이다.

시험할 때 유용한 옵션:

```bash
python scripts/batch.py daily --job TEST100 --with-crawl --max-rows 20     # NAVER·국민연금 API·크롤링을 20행만
python scripts/batch.py daily --job TEST100 --cap naver=500                 # NAVER 를 오늘 500회까지만
```

---

## 6. 배치 돌리기 — 매일 자동 (launchd)

대량 잡은 Mac 의 launchd 로 등록해 매일 자동으로 돌린다. 사용자 LaunchAgent 라 `sudo` 가 필요 없다.

```bash
scripts/batch_launchd.sh install   SME1101    # 등록
scripts/batch_launchd.sh status    SME1101    # 등록 상태 + 진행률
scripts/batch_launchd.sh uninstall SME1101    # 해제 (데이터는 그대로)
scripts/batch_launchd.sh print     SME1101    # plist 내용만 출력 (등록 안 함)
```

등록되는 작업 두 개:

| 작업 | 실행 | 내용 |
| --- | --- | --- |
| `kr.lguplus.bizcrawler.<잡>.daily` | 매일 **00:10** (한도가 KST 자정에 초기화) | `daily --notify` — 5.3 의 단계 1~8. 오류가 나면 macOS 알림 |
| `kr.lguplus.bizcrawler.<잡>.crawl` | 로그인 시 시작 · 죽으면 자동 재시작 | 상주 크롤러 — URL 이 생긴 행을 계속 크롤링, 1시간마다 병합 · 우편번호 |

등록 옵션 (환경변수):

```bash
SCHEDULE="02:00"                 scripts/batch_launchd.sh install SME1101   # 실행 시각 변경 (쉼표로 여러 개)
DAILY_ARGS="--no-retry"          scripts/batch_launchd.sh install SME1101   # daily 에 인자 추가

# 예약 실행 시험 — 시험용 잡으로, 끝나면 반드시 uninstall
SCHEDULE="14:05" DAILY_ONLY=1 DAILY_ARGS="--max-rows 100 --use-reserve --with-crawl" \
  scripts/batch_launchd.sh install TEST100
```

### Mac 조건 — 이걸 안 지키면 그날 배치가 안 돈다

| 조건 | 설정 |
| --- | --- |
| 전원 연결 · 덮개 열어둠 · **로그인 상태 유지** | 시스템 설정 → 배터리 → "디스플레이가 꺼져 있을 때 자동으로 잠자기 방지" |
| 00:05 자동 깨우기 (권장) | `sudo pmset repeat wakeorpoweron MTWRFSU 00:05:00` |
| 사내망 연결 | 1절의 `certs/ca-bundle.pem` 이 있어야 한다 (plist 에 `SSL_CERT_FILE` 로 들어감) |
| 디스크 여유 2GB 이상 | 공정위 전체 데이터 · 잡 DB |

### 코드를 `git pull` 로 갱신한 뒤

상주 크롤러는 **시작할 때의 코드를 계속 쥐고 있다.** 갱신 후 재시작해야 새 코드가 적용된다.

```bash
launchctl kickstart -k gui/$(id -u)/kr.lguplus.bizcrawler.SME1101.crawl
```

daily 는 매번 새로 실행되므로 다음 실행부터 자동 반영된다.

---

## 7. 매일 확인 · 결과 파일

```bash
python scripts/batch.py status --job SME1101
```

단계별 상태(`pending` 대기 · `done` 완료 · `retry_pending` 재시도 대기 · `failed_tmp` 일시 실패(1일·7일 뒤 재시도) · `failed` 실패 · `skipped` 대상 아님),
오늘 API 별 사용량/예산, 항목별 채움 수, 예상 종료일, 최근 실행 8건을 보여준다.

| 위치 | 내용 |
| --- | --- |
| `output/batch/<잡>/snapshot.csv` | 전체 행 누적 결과 (매 daily 마다 갱신) |
| `output/batch/<잡>/delta_YYYYMMDD.csv` | 그날 값이 바뀐 행만 |
| `output/batch/<잡>/daily_progress.csv` | 하루 1행 실적 — 집계값만 (호출 수 · 누적 처리 · 채움 수 · 예상 종료일) |
| `logs/batch/YYYYMMDD.log` | 그날 실행 로그 (API 키는 마스킹) |
| `logs/batch/kr.lguplus.bizcrawler.<잡>.{daily,crawl}.err.log` | launchd 실행 로그 |
| `data/batch/<잡>/state.db` | 잡 상태 DB (SQLite) |
| `data/batch/registry.db` | 잡 공용 — API 한도 장부 · 공정위 데이터 · NAVER 캐시 |

결과 CSV 의 주요 칸: `b_no_display`(사업자번호, 하이픈) · `final_company_name` · `final_address` · `final_zipcode` ·
`final_phone` · `final_email` · `final_homepage` · `ind_large/mid/small`(업종) · `ksic_code/name` · `nts_b_stt`(사업자 상태).
접두사(`nts_` `ftc_` `nps_` `naver_` `crawl_`)가 붙은 칸은 소스별 원본값이다.

> **Excel 로 열 때:** 사업자번호는 하이픈 표기(`b_no_display`)를 쓴다. 숫자 10자리 칸을 Excel 이 열면
> `1.82E+09` 로 바뀌어 복구할 수 없다.

---

## 8. 명령 레퍼런스

### `scripts/batch.py` — 일배치

| 명령 | 설명 |
| --- | --- |
| `import <csv> --job J [--limit N] [--offset N] [--bizno-col C] [--name-col C]` | 입력 적재 |
| `plan --job J` | 호출 없이 일정 추정 |
| `daily --job J [옵션]` | 하루치 실행 (아래 옵션) |
| `crawl-worker --job J [--once] [--max-rows N] [--idle 초]` | 상주 크롤러 (`--once`: 대상이 없으면 종료) |
| `status --job J [--update-doc]` | 진행률 · 오늘 한도 · 최근 실행 |
| `export --job J` | `snapshot.csv` · `delta_오늘.csv` 다시 내보내기 |
| `pause --job J` / `resume --job J` | 일시정지 / 재개 (자동 실행은 등록된 채로 건너뜀) |
| `ftc-dump [--max-pages N]` | 공정위 전체 다운로드만 따로 (이어받기 됨) |

`daily` 옵션:

| 옵션 | 설명 |
| --- | --- |
| `--with-crawl` | 크롤링도 이 프로세스에서 (상주 워커 없이 돌릴 때) |
| `--no-retry` | NAVER 재시도 생략 (1차만) |
| `--no-nps-api` | 국민연금 API 보완 끄기 (파일 조인만) |
| `--ftc-api-max N` | 공정위 건별 API 로 처리할 최대 행 (기본 3,000 — 넘으면 전체 다운로드) |
| `--cap API=N` | 오늘 API 상한 (예: `--cap naver=1000`). 기본 예산보다 크게 줄 수는 없다 |
| `--max-rows N` | NAVER · 국민연금 API · 크롤링을 N행만 (시험용) |
| `--use-reserve` | 한도의 98% 까지 사용 (기본 95% — 나머지는 수동 확인용 여유) |
| `--notify` | 오류 시 macOS 알림 |

### 그 밖의 도구

| 명령 | 설명 |
| --- | --- |
| `scripts/check_status.py <csv> [--active-only]` | 국세청 상태조회만 단독 실행 |
| `scripts/enrich_ftc.py <csv>` | 공정위 정보만 덧붙이기 |
| `scripts/pipeline.py init / status / run <스텝>` | 소량(수백 건)용 CSV 파이프라인 (`data/master.csv`). 대량은 `batch.py` 를 쓴다 |
| `scripts/export_result.py [--only-collected]` | `pipeline.py` 결과를 납품 컬럼 순서로 → `output/수집결과_YYYYMMDDHH24MI.csv` |
| `scripts/export_links.py` | 확보된 홈페이지 URL 목록 |
| `scripts/extract_ksic.py <xlsx>` | 표준산업분류 기준표 생성 (3절) |
| `scripts/setup_ca.py` | 사내 CA 묶음 생성 (1절) |

---

## 9. 문제 해결

| 증상 | 원인 · 조치 |
| --- | --- |
| `CERTIFICATE_VERIFY_FAILED` | 사내망 — `python scripts/setup_ca.py` 후 `export SSL_CERT_FILE=$PWD/certs/ca-bundle.pem` |
| `ODCLOUD_SERVICE_KEY 가 비어 있습니다` | `.env` 가 없거나 값이 빔 — 2절 |
| 국세청·공정위 인증 실패 | data.go.kr 에서 해당 API **각각** 활용신청했는지, Encoding 키를 넣었는지 확인 |
| `이미 실행 중입니다: daily.lock` | 같은 잡의 daily 가 이미 돌고 있음 (자동 실행과 수동 실행이 겹침) — 끝나길 기다린다 |
| 상태가 `partial` | 하루 한도를 다 써서 멈춘 것 — 정상. 다음 날 이어서 돈다 |
| NAVER 가 그날 멈춤 (`429 연속 20회`) | 장부보다 실제 사용량이 많음 — 같은 키를 다른 곳에서 쓰는지 확인 |
| 크롤러 로그에 `database is locked` | daily 와 크롤러가 동시에 씀 — 워커가 30초 뒤 자동으로 이어 간다. 계속 늘면 로그 확인 |
| 배치가 그날 안 돎 | Mac 이 잠들었거나 로그아웃 — 6절 Mac 조건 · `scripts/batch_launchd.sh status <잡>` |
| `pull` 했는데 크롤러 동작이 그대로 | 상주 크롤러 재시작 — `launchctl kickstart -k …crawl` (6절) |
| 업종분류 관련 `FileNotFoundError` | `design/업종분류.md` 없음 — 3절 |

---

## 10. 주의 — 개인정보 · 보안

- **커밋 금지:** `.env`(키) · `certs/` · `input/` · `data/` · `output/` · `logs/` · 모든 CSV/XLSX. `.gitignore` 에 들어 있지만
  `git add` 전에 `git status` 로 한 번 더 확인한다. 이 저장소는 **공개(public)** 다.
- 수집 결과에는 사업자 연락처가 들어 있다. 메일 · 메신저 · 이슈 · 보고서에는 **집계값만** 쓰고 개별 행을 붙이지 않는다.
- 대표자명 · 대표자 이메일 · 휴대폰은 결과로 내보내지 않는다.
- 크롤링은 robots.txt 를 지키고, 호스트당 1요청 · 딜레이를 두며, `From` 헤더로 연락처를 밝힌다. 이 설정을 끄지 않는다.

---

## 11. 코드 구성

```text
biz_crawler/
  config.py · logging_utils.py · schema.py                     환경 · 로그 마스킹 · 컬럼 정의
  bizno.py · namematch.py · urls.py · region.py                사업자번호 · 상호 유사도 · URL 정제 · 지역
  nts_status.py · ftc_mllbs.py · naver_local.py · nps_bplc.py · juso.py   소스별 API 클라이언트
  homepage.py · platform.py                                    크롤링 (정적 · Playwright)
  merge.py · industry.py · ksic.py                             병합 · 업종 분류 · 표준산업분류
  batch/  store.py · quota.py · stages.py                      일배치 (SQLite · 한도 장부 · 단계 실행기)
scripts/
  batch.py · batch_launchd.sh                                  일배치 CLI · Mac 자동 실행
  pipeline.py · export_result.py · export_links.py             소량 CSV 파이프라인 · 내보내기
  setup_ca.py · check_status.py · enrich_ftc.py · extract_ksic.py · selftest_batch.py   도구 · 시험
```
