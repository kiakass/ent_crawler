# ent_crawler — 사업자정보 수집기

사업자번호 + 상호명 목록을 넣으면 **상호 · 주소 · 우편번호 · 대표전화 · 이메일 · 사이트주소 · 업종(대/중/소) · 표준산업분류**를
공공 API(국세청 · 공정위 · 국민연금 · 도로명주소), NAVER 지역검색, 홈페이지 크롤링으로 채운다.

대량 목록(수만~수십만 건)은 **일배치**로 돌린다. API 마다 하루 한도가 있어서 한 번에 끝나지 않고,
매일 정해진 시각에 한도만큼 처리하고 다음 날 이어 간다.

- 실행 환경: **macOS** (자동 실행은 launchd 또는 Python 스케줄러) · **Python 3.10 이상** (개발은 3.14)
- 저장소에는 코드 · 업종분류표 · 샘플 10건만 있다. API 키 · 실제 입력 목록 · 수집 결과 · 로그는 없고 들어가서도 안 된다 ([10절](#10-주의--개인정보--보안))
- Claude Code 로 작업한다면 저장소의 [`CLAUDE.md`](CLAUDE.md) 를 Claude Code 가 자동으로 읽는다 — "README 대로 설치하고 샘플을 돌려줘" 라고 하면 된다

---

## 빠른 시작 — 전체 흐름

```bash
git clone https://github.com/kiakass/ent_crawler.git && cd ent_crawler
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && python -m playwright install chromium
python scripts/setup_check.py --init        # .env 템플릿 · 작업 폴더 생성 → 빠진 것 목록
#   → .env 에 키 입력 (2절) · (권장) 국민연금 월별 CSV (3절) · 사내망이면 python scripts/setup_ca.py
python scripts/setup_check.py               # '필수 항목 모두 있음' 이 나올 때까지
python scripts/run_sample.py                # 샘플 10건으로 전 과정 시험 (4절)

# 실제 목록을 input/ 에 넣고
python scripts/start_batch.py --job SME1101 # 점검 → 적재 → 일정 추정 → 매일 00:10 자동 실행 등록 (5절)
python scripts/batch.py status --job SME1101   # 매일 확인 (7절)
```

---

## 목차

1. [처음 설치](#1-처음-설치)
2. [API 키 발급 · `.env`](#2-api-키-발급--env)
3. [준비 파일](#3-준비-파일)
4. [샘플로 시험](#4-샘플로-시험)
5. [실제 목록으로 배치 시작](#5-실제-목록으로-배치-시작)
6. [매일 자동 실행 — launchd / Python 스케줄러](#6-매일-자동-실행--launchd--python-스케줄러)
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

python3 -m venv .venv                 # 이름은 반드시 .venv — 자동 실행 스크립트가 .venv/bin/python 을 쓴다
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium # 플랫폼(블로그·스마트스토어 등) 크롤링용

python scripts/setup_check.py --init  # .env 를 .env.example 에서 복사(권한 600) · input/ data/ output/ logs/ 생성
```

`setup_check.py` 는 외부 호출 없이 Python · 패키지 · 키(값은 표시 안 함) · 인증서 · 준비 파일을 점검하고 빠진 것과 해결 방법을 보여준다.
필수 항목이 빠지면 종료 코드 1.

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

**키는 모두 본인 계정으로 새로 발급받는다.** 한도는 키 단위이고 사용량 장부는 각 PC 의 로컬 DB(`data/batch/registry.db`)에 있어서,
다른 사람과 같은 키를 쓰면 서로의 사용량을 모르고 함께 한도를 넘긴다. 새 키는 하루 한도를 처음부터 온전히 쓴다.

| `.env` 변수 | 발급처 · 절차 | 용도 · 일 한도 |
| --- | --- | --- |
| `ODCLOUD_SERVICE_KEY` | [data.go.kr](https://www.data.go.kr) 가입 → 아래 3개 API 를 **각각** 활용신청 (자동승인) → 마이페이지의 **일반 인증키(Encoding)** | 3개 API 공용 키 |
| | ① 국세청_사업자등록정보 진위확인 및 상태조회 서비스 | 국세청 100만 |
| | ② 공정거래위원회_통신판매사업자 등록상세 제공 서비스 | 공정위 1만 |
| | ③ 국민연금공단_국민연금 가입 사업장 내역 (V2) | 국민연금 1만 |
| | ④ (선택) 근로복지공단_고용·산재보험 가입현황 정보 — 상호 없이 사업자번호만 있는 목록용 | 근로복지공단 1만 |
| `NCP_APIGW_KEY_ID` · `NCP_APIGW_KEY` | [console.ncloud.com](https://console.ncloud.com) → Services → Application Service → NAVER API HUB 이용 신청 → Application 등록 → 인증 정보 팝업의 Client ID · Client Secret | NAVER 2.5만 · 월 77.5만 |
| `NCP_APIGW_KEY_ID_2` · `NCP_APIGW_KEY_2` | (선택) 같은 절차로 Application 을 하나 더 등록한 키 | 키1 예산을 다 쓰면 배치가 키2 로 넘어간다 — 하루 예산 2배. 장부는 `naver@2` 로 따로 센다 |
| `JUSO_CONFM_KEY` | [business.juso.go.kr](https://business.juso.go.kr) → 도로명주소 검색 API 신청 → 승인키 | 우편번호 (개발키는 만료일이 있다 — 장기 운영은 운영키 신청) |
| `CRAWLER_CONTACT` | 운영 공용 메일 주소 (개인 메일 X) | 크롤링 요청의 `From` 헤더 — 사이트 운영자가 연락할 창구 |
| `CA_BUNDLE` | (선택) 비워둔다 | 비워두면 `certs/ca-bundle.pem` → certifi 순 |

- data.go.kr 키는 **Encoding 값을 그대로** 붙여넣는다 (디코딩은 코드가 한다).
- data.go.kr 는 활용신청 직후 키가 반영되기까지 **1~2시간** 걸릴 수 있다. 그 사이엔 인증 오류가 난다.
- NAVER 는 developers.naver.com 이 아니라 **NCP(네이버 클라우드)** 키다. 개발자센터 검색 API 는 종료 예정이다.
- 키를 채운 뒤 `python scripts/setup_check.py` 로 확인하고, 실제로 통하는지는 4절 샘플 시험으로 본다.

---

## 3. 준비 파일

| 파일 | 필수 | 없으면 | 구하는 법 |
| --- | --- | --- | --- |
| `design/업종분류.md` | **필수** | 병합 단계가 파일 없음으로 실패 | **저장소에 포함돼 있다** — 사내 업종분류표 (CSV 형식: `업종대,업종중,업종소`). 분류를 바꿀 때만 고친다 |
| `data/external/nps/*.csv` | 권장 | 국민연금 업종을 못 채움 (API 는 기본 꺼짐) | data.go.kr 파일데이터 **15083277** "국민연금공단_국민연금 가입 사업장 내역" 월별 CSV(cp949). `python scripts/fetch_nps_file.py` 로 받는다 (로그인 · 키 불필요). 이후엔 `daily` 가 포털의 다음 등록 예정일이 지나면 새 파일을 자동으로 받는다. 여러 개면 이름순 마지막 파일을 쓴다 |
| `data/external/ksic/ksic11_세세분류.csv` · `ksic10_세세분류.csv` | 선택 | 표준산업분류 코드 칸이 비어 있음 (다른 항목은 정상) | 통계청 "한국표준산업분류 제11차 개정 분류체계(대-중-소-세-세세 구분)" xlsx 를 받아 `python scripts/extract_ksic.py <xlsx 경로>` |
| `data/external/ksic/naver_ksic.csv` | 선택 | 표준산업분류를 국민연금 업종으로만 채움 (NAVER 카테고리 보충 없음) | 국민연금 매칭이 쌓인 잡에서 `python scripts/build_naver_ksic.py --job <잡>` — 상위 100 카테고리는 `naver_ksic_review_top100.csv` 의 `확정코드` 로 고칠 수 있다 |

공정위 데이터는 따로 받을 필요 없다. 목록이 3,000건을 넘으면 배치가 처음 돌 때 전체(약 277페이지)를 받아 로컬에 쌓는다.

---

## 4. 샘플로 시험

`samples/sample_input.csv` — 사업자번호를 스스로 공개한 대기업 9곳 + **일부러 넣은 미등록 번호 1건**(오류 처리 확인용).
실제 배치와 같은 명령(`scripts/batch.py`)을 그대로 부르므로 여기서 통과하면 실제 목록도 같은 경로로 돈다.

```bash
source .venv/bin/activate
export SSL_CERT_FILE=$PWD/certs/ca-bundle.pem   # 사내망일 때

python scripts/run_sample.py            # 적재 → daily(크롤링 포함) → 상태 → 결과 요약 (약 1분)
python scripts/run_sample.py --reset    # 처음부터 다시
```

외부 호출은 API 마다 수십 회 이하다. 마지막에 이런 요약이 나오면 정상이다 (2026-10 실측):

```text
샘플 결과 (10행) — output/batch/SAMPLE/snapshot.csv
  사업자상태       9 / 10
  주소          9 / 10
  우편번호        9 / 10
  사이트주소       9 / 10
  전화          8 / 10
  이메일         2 / 10
  업종소         9 / 10
  표준산업분류코드    7 / 10      ← ksic 기준표가 없으면 0
  국세청 미등록 1건 (샘플의 미등록 번호 1건이 여기 잡히면 정상)
```

- 사업자상태가 0 이면 data.go.kr 키 문제 (반영 대기 · Encoding 키 아님 · 국세청 활용신청 누락)
- 주소 · 사이트가 크게 적으면 NAVER(NCP) 키 문제
- 우편번호가 0 이면 도로명주소 키 문제
- 결과 파일은 `output/batch/SAMPLE/snapshot.csv` 에서 직접 열어 본다

외부 API 를 소량 부르는 회귀 시험도 있다: `python scripts/selftest_batch.py` → 마지막 줄 `N/N 통과`.

---

## 5. 실제 목록으로 배치 시작

### 5.1 입력 파일

`input/` 에 CSV 한 개를 둔다 (`.gitignore` 로 커밋되지 않는다). 인코딩은 **UTF-8(BOM 포함 가능) 또는 cp949** 를 자동 판별한다.

| 컬럼 | 기본 이름 | 다르면 |
| --- | --- | --- |
| 사업자번호 (하이픈 유무 무관) | `IK_BRNO` | `--bizno-col 컬럼명` |
| 상호명 | `EPM_CUST_NM` | `--name-col 컬럼명` |

형식은 `samples/sample_input.csv` 와 같다. 다른 컬럼이 더 있어도 된다.

**사업자번호만 있는 목록**도 된다 — 상호명 컬럼은 두되 값을 비운다. 상호는 공정위 → 근로복지공단(④ 활용신청 필요) 순으로
사업자번호로 찾아 국민연금 · NAVER 에 쓴다. 두 곳 모두 없는 행(직원 없는 1인 사업자 · 폐업자가 많다)은 국세청 상태만 남는다.

> Excel 에서 만든 목록은 사업자번호 칸이 `1.82E+09` 처럼 바뀌어 있지 않은지 먼저 본다. 그런 행은 복구가 안 되고 적재 때 오류로 센다.

### 5.2 한 번에 시작 — `start_batch.py`

```bash
python scripts/start_batch.py --job SME1101                         # input/ 의 CSV 1개를 자동으로 집는다
python scripts/start_batch.py input/목록.csv --job SME1101           # 파일 지정
python scripts/start_batch.py ... --bizno-col 사업자번호 --name-col 상호
python scripts/start_batch.py ... --mode python                     # launchd 대신 Python 스케줄러 (6.2)
python scripts/start_batch.py ... --mode none                       # 적재 · 추정까지만
```

순서: ① `setup_check.py` (빠진 게 있으면 멈춤) → ② `batch.py import` → ③ `batch.py plan`(예상 일수 · 종료일, 호출 0회)
→ ④ 매일 00:10 자동 실행 등록 (`--mode launchd`, macOS 기본). 이미 적재된 잡이면 적재를 건너뛰고 등록만 다시 하므로 같은 명령을 다시 쳐도 안전하다.

잡(job) 이름은 영문 · 숫자로 짓는다 (생략하면 `JOB<YYMMDD>`). 목록 하나 = 잡 하나이고, 잡마다 상태 DB 가 따로 생긴다.
대량 목록은 NAVER 한도 때문에 **약 10만 건에 1주일** 정도 걸린다.

### 5.3 단계별로 직접 — `batch.py`

```bash
python scripts/batch.py import input/목록.csv --job SME1101 [--limit 100]   # 적재 (처음엔 --limit 로 소량 시험 권장)
python scripts/batch.py plan   --job SME1101                                 # 일정 추정 (호출 0회)
python scripts/batch.py daily  --job SME1101 --with-crawl                    # 하루치 실행 (크롤링까지 이 프로세스에서)
```

`daily` 는 아래 순서로 돌고, 각 단계는 **그날 남은 한도만큼** 처리하고 멈춘다. 남은 행은 다음 실행이 이어받는다.

| 순서 | 단계 | 하는 일 |
| --- | --- | --- |
| 1 | `nts` 국세청 | 계속/휴업/폐업 상태 · 폐업일 (100건/호출) |
| 2 | `ftc` 공정위 | 통신판매업 신고 정보 — 상호 · 주소 · 전화 · 도메인 |
| 2b | `kcw` 근로복지공단 | 상호가 비고 공정위에도 없는 행만 — 상호 · 주소 · 우편번호 (사업자번호 10자리 정확 일치) |
| 3a | `nps_file` 국민연금 파일 | 포털에 새 월별 파일이 올라왔으면 받는다 (다음 등록 예정일 전에는 묻지 않음 · 실패하면 기존 파일) |
| 3 | `nps` 국민연금 | 월별 파일 조인 — 업종코드 · 주소 · 우편번호. 새 파일이면 못 찾은 행을 다시 대조. 못 찾은 행은 `api_pending` 으로 남고, `nps-api on` 일 때만 API 로 보완 |
| 4 | `naver` NAVER | 지역검색으로 업소 매칭 — 주소 · 홈페이지 링크 · 카테고리. 1차 → 미매칭은 질의를 바꿔 재시도 |
| 5 | `crawl` 크롤링 | 확보된 홈페이지에서 전화 · 이메일 (`--with-crawl` 일 때만. 자동 실행에서는 상주 크롤러가 함) |
| 6 | `merge` 병합 | 소스별 값을 골라 최종 칸 · 업종 대/중/소 계산 (값이 바뀐 행만) |
| 7 | `zip` 우편번호 | 최종 주소 → 도로명주소 API |
| 8 | `export` 내보내기 | `snapshot.csv` · `delta_YYYYMMDD.csv` · 실적 1행 |

한 단계가 오류나 한도로 멈춰도 다음 단계는 돈다. 같은 날 여러 번 돌려도 한도 장부를 공유하므로 한도를 넘지 않는다.
NAVER 키2 가 있으면 키1 예산(95%)을 다 쓴 뒤 키2 로 이어서 돈다 (`status` 에 `naver` · `naver@2` 가 따로 보인다).
상태가 `partial` 이면 **하루 한도를 다 써서 정상적으로 멈춘 것**이다.

---

## 6. 매일 자동 실행 — launchd / Python 스케줄러

둘 중 **하나만** 쓴다. 같은 잡에 둘 다 쓰면 크롤러가 둘 뜬다 (daily 중복은 잠금 파일이 막는다).

| | launchd (권장, macOS) | Python 스케줄러 |
| --- | --- | --- |
| 명령 | `scripts/batch_launchd.sh install <잡>` | `python scripts/scheduler.py --job <잡>` |
| 재부팅 · 로그아웃 후 | 로그인하면 자동으로 다시 돈다 | 다시 실행해야 한다 |
| 터미널 | 필요 없음 | 떠 있어야 한다 (또는 `nohup`) |
| 플랫폼 | macOS | macOS · Linux (Python 만 있으면) |

두 방식 모두 매일 정한 시각(기본 **00:10** — 한도가 KST 자정에 초기화)에 `daily` 를 새 프로세스로 돌리고,
상주 크롤러를 띄워 두었다가 죽으면 60초 뒤 다시 띄운다. 잠자기로 시각을 지나쳤으면 깨어난 직후 한 번 돈다.

### 6.1 launchd

```bash
scripts/batch_launchd.sh install   SME1101    # 등록 (sudo 불필요 — 사용자 LaunchAgent)
scripts/batch_launchd.sh status    SME1101    # 등록 상태 + 진행률
scripts/batch_launchd.sh uninstall SME1101    # 해제 (데이터는 그대로)
scripts/batch_launchd.sh print     SME1101    # plist 내용만 출력 (등록 안 함)
```

| 등록되는 작업 | 실행 | 내용 |
| --- | --- | --- |
| `kr.lguplus.bizcrawler.<잡>.daily` | 매일 00:10 | `daily --notify` — 5.3 의 단계 1~8. 오류가 나면 macOS 알림 |
| `kr.lguplus.bizcrawler.<잡>.crawl` | 로그인 시 시작 · 죽으면 재시작 | 상주 크롤러 — URL 이 생긴 행을 계속 크롤링, 1시간마다 병합 · 우편번호 |

등록 옵션 (환경변수):

```bash
SCHEDULE="02:00"        scripts/batch_launchd.sh install SME1101   # 실행 시각 변경 (쉼표로 여러 개)
DAILY_ARGS="--no-retry" scripts/batch_launchd.sh install SME1101   # daily 에 인자 추가

# 예약 실행 시험 — 시험용 잡으로, 끝나면 반드시 uninstall
SCHEDULE="14:05" DAILY_ONLY=1 DAILY_ARGS="--max-rows 100 --with-crawl" scripts/batch_launchd.sh install SAMPLE
```

### 6.2 Python 스케줄러

```bash
python scripts/scheduler.py --job SME1101                          # 매일 00:10 daily + 상주 크롤러 (Ctrl+C 로 멈춤)
python scripts/scheduler.py --job SME1101 --at 02:00,14:00         # 실행 시각 (KST, 쉼표로 여러 개)
python scripts/scheduler.py --job SME1101 --run-now                # 시작하자마자 daily 한 번 더
python scripts/scheduler.py --job SME1101 --no-crawl               # 크롤러 없이 daily 만
python scripts/scheduler.py --job SME1101 --daily-args="--no-retry"   # 값이 -- 로 시작하므로 = 로 붙인다

# 터미널을 닫아도 계속 돌게
nohup python scripts/scheduler.py --job SME1101 >> logs/batch/scheduler_SME1101.log 2>&1 &
pkill -f "scheduler.py --job SME1101"                              # 멈춤 (크롤러도 함께 멈춘다)
```

macOS 에서는 스케줄러가 도는 동안 `caffeinate` 로 시스템 잠자기를 막는다.

### 6.3 Mac 조건 — 이걸 안 지키면 그날 배치가 안 돈다

| 조건 | 설정 |
| --- | --- |
| 전원 연결 · 덮개 열어둠 · **로그인 상태 유지** | 시스템 설정 → 배터리 → "디스플레이가 꺼져 있을 때 자동으로 잠자기 방지" |
| 00:05 자동 깨우기 (권장) | `sudo pmset repeat wakeorpoweron MTWRFSU 00:05:00` |
| 사내망 연결 | 1절의 `certs/ca-bundle.pem` 이 있어야 한다 (launchd plist 에 `SSL_CERT_FILE` 로 들어감) |
| 디스크 여유 2GB 이상 | 공정위 전체 데이터 · 잡 DB |

### 6.4 코드를 `git pull` 로 갱신한 뒤

상주 크롤러는 **시작할 때의 코드를 계속 쥐고 있다.** 갱신 후 재시작해야 새 코드가 적용된다. daily 는 매번 새로 떠서 자동 반영된다.

```bash
launchctl kickstart -k gui/$(id -u)/kr.lguplus.bizcrawler.SME1101.crawl   # launchd
# Python 스케줄러는 멈췄다가 다시 실행
```

---

## 7. 매일 확인 · 결과 파일

```bash
python scripts/batch.py status --job SME1101
```

단계별 상태(`pending` 대기 · `done` 완료 · `retry_pending` 재시도 대기 · `failed_tmp` 일시 실패(1일·7일 뒤 재시도) · `failed` 실패 · `skipped` 대상 아님),
오늘 API 별 사용량/예산, 항목별 채움 수, 예상 종료일, 최근 실행 8건을 보여준다.

| 위치 | 내용 |
| --- | --- |
| `output/batch/<잡>/snapshot.csv` | **전체 행 누적 결과** (daily 마다 갱신 · 납품 컬럼 순서) |
| `output/batch/<잡>/delta_YYYYMMDD.csv` | 직전 날 마지막 내보내기 이후 바뀐 행 (상주 크롤러가 내보내기 뒤 바꾼 행도 다음 날 delta 에 들어간다 · 같은 날 다시 내보내도 기준 유지) |
| `output/batch/<잡>/daily_progress.csv` | 하루 1행 실적 — 집계값만 (호출 수 · 누적 처리 · 채움 수 · 예상 종료일) |
| `logs/batch/YYYYMMDD.log` | 그날 실행 로그 (API 키는 마스킹) |
| `logs/batch/kr.lguplus.bizcrawler.<잡>.{daily,crawl}.err.log` | launchd 실행 로그 |
| `data/batch/<잡>/state.db` | 잡 상태 DB (SQLite — 소스별 원본값 전부) |
| `data/batch/registry.db` | 잡 공용 — API 한도 장부 · 공정위 데이터 · NAVER 캐시 |

`snapshot.csv` 컬럼 (UTF-8 BOM — Excel 에서 바로 열림):

```text
사업자번호 · 상호명 · 이메일 · 전화 · 주소 · 우편번호 · 사이트주소 · 업종대 · 업종중 · 업종소
· 표준산업분류코드 · 표준산업분류명 · 표준산업분류근거(국민연금/국민연금(유사상호)/NAVER보정/NAVER추정) · 업종분류근거 · NAVER업종_대/중/소 · 국민연금업종코드 · 국민연금업종명
· 사업자상태(계속/휴업/폐업) · 폐업일자 · NAVER지역검증 · 사이트비고 · 우편번호출처 · 주소비고
```

사업자번호는 하이픈 표기(`123-45-67890`)다. 숫자 10자리를 Excel 이 열면 `1.82E+09` 로 바뀌어 복구할 수 없기 때문이다.
특정 시점 결과를 다시 뽑으려면 `python scripts/batch.py export --job <잡>`.

---

## 8. 명령 레퍼런스

### 시작 · 점검 · 자동 실행

| 명령 | 설명 |
| --- | --- |
| `scripts/setup_check.py [--init]` | 설치 점검 (호출 0회). `--init`: `.env` 템플릿 복사 · 작업 폴더 생성 |
| `scripts/run_sample.py [--reset] [--use-reserve]` | 샘플 10건 전 과정 시험 |
| `scripts/start_batch.py [csv] --job J [--mode launchd\|python\|none]` | 점검 → 적재 → 추정 → 자동 실행 |
| `scripts/scheduler.py --job J [--at HH:MM,..] [--run-now] [--no-crawl] [--daily-args=..]` | Python 스케줄러 |
| `scripts/batch_launchd.sh install\|status\|uninstall\|print J` | launchd 등록 |

### `scripts/batch.py` — 일배치

| 명령 | 설명 |
| --- | --- |
| `import <csv> --job J [--limit N] [--offset N] [--bizno-col C] [--name-col C]` | 입력 적재 |
| `plan --job J` | 호출 없이 일정 추정 |
| `daily --job J [옵션]` | 하루치 실행 (아래 옵션) |
| `crawl-worker --job J [--once] [--max-rows N] [--idle 초]` | 상주 크롤러 (`--once`: 대상이 없으면 종료) |
| `status --job J` | 진행률 · 오늘 한도 · 최근 실행 |
| `export --job J` | `snapshot.csv` · `delta_오늘.csv` 다시 내보내기 |
| `pause --job J` / `resume --job J` | 일시정지 / 재개 (자동 실행은 등록된 채로 건너뜀) |
| `ftc-dump [--max-pages N]` | 공정위 전체 다운로드만 따로 (이어받기 됨) |
| `nps-api --job J [on\|off]` | 국민연금 API 보완 켜기 / 끄기 (기본 off — 파일 기반). 끈 동안 API 대기 행은 보존된다 |
| `nps-rejudge --job J` | 국민연금 매칭을 현재 판정 기준으로 다시 판정 (API 0회) · 주소가 바뀐 행은 NAVER 재검색 예약 |
| `report --job J [--date D]` | `일별실행결과_YYYYMMDD.md` 다시 만들기 (`daily` 끝에 자동) |

`daily` 옵션:

| 옵션 | 설명 |
| --- | --- |
| `--with-crawl` | 크롤링도 이 프로세스에서 (상주 크롤러 없이 돌릴 때) |
| `--no-retry` | NAVER 재시도 생략 (1차만 — 기간 단축, 매칭은 줄어듦) |
| `--no-nps-api` | 국민연금 API 보완 끄기 (`nps-api on` 이어도 이번 실행만) |
| `--ftc-api-max N` | 공정위 건별 API 로 처리할 최대 행 (기본 3,000 — 넘으면 전체 다운로드) |
| `--cap API=N` | 오늘 API 상한 (예: `--cap naver=1000`). 기본 예산보다 크게 줄 수는 없다 |
| `--max-rows N` | NAVER · 국민연금 API · 크롤링을 N행만 (시험용) |
| `--use-reserve` | 한도의 98% 까지 사용 (기본 95% — 나머지는 수동 확인용 여유) |
| `--notify` | 오류 시 macOS 알림 |

### 그 밖의 도구

| 명령 | 설명 |
| --- | --- |
| `scripts/selftest_batch.py` | 배치 · 병합 규칙 회귀 시험 (외부 API 소량) |
| `scripts/fetch_nps_file.py [--check] [--force]` | 국민연금 월별 파일 받기 (`--check`: 포털 · 로컬 날짜 비교만) |
| `scripts/check_status.py <csv> [--active-only]` | 국세청 상태조회만 단독 실행 |
| `scripts/enrich_ftc.py <csv>` | 공정위 정보만 덧붙이기 |
| `scripts/pipeline.py init / status / run <스텝>` | 소량(수백 건)용 CSV 파이프라인 (`data/master.csv`). 대량은 `batch.py` 를 쓴다 |
| `scripts/export_result.py [--only-collected]` | `pipeline.py` 결과를 납품 컬럼 순서로 → `output/수집결과_YYYYMMDDHH24MI.csv` |
| `scripts/export_links.py` | 확보된 홈페이지 URL 목록 |
| `scripts/extract_ksic.py <xlsx>` | 표준산업분류 기준표 생성 (3절) |
| `scripts/build_naver_ksic.py --job <잡>` | NAVER 카테고리 → 표준산업분류 대응표 생성 (3절) |
| `scripts/setup_ca.py [--ca-name 이름]` | 사내 CA 묶음 생성 (1절) |

---

## 9. 문제 해결

| 증상 | 원인 · 조치 |
| --- | --- |
| `CERTIFICATE_VERIFY_FAILED` | 사내망 — `python scripts/setup_ca.py` 후 `export SSL_CERT_FILE=$PWD/certs/ca-bundle.pem` |
| `ODCLOUD_SERVICE_KEY 가 비어 있습니다` | `.env` 가 없거나 값이 빔 — 2절 · `setup_check.py` |
| 국세청 · 공정위 · 국민연금 인증 오류 | 활용신청 직후면 1~2시간 대기. 3개 API **각각** 활용신청했는지 · Encoding 키인지 확인 |
| `입력 컬럼 없음: …` | 컬럼 이름이 다름 — 오류에 나온 '있는 컬럼' 을 보고 `--bizno-col` · `--name-col` |
| `인코딩을 판별할 수 없습니다` | UTF-8 · cp949 가 아님 — Excel 에서 "CSV UTF-8" 로 다시 저장 |
| `이미 실행 중입니다: daily.lock` | 같은 잡의 daily 가 이미 돌고 있음 (자동 실행과 수동 실행이 겹침) — 끝나길 기다린다 |
| 상태가 `partial` | 하루 한도를 다 써서 멈춘 것 — 정상. 다음 날 이어서 돈다 |
| NAVER 가 그날 멈춤 (`429 연속 20회`) | 장부보다 실제 사용량이 많음 — 같은 키를 다른 곳에서 쓰는지 확인 |
| 크롤러 로그에 `database is locked` | daily 와 크롤러가 동시에 씀 — 크롤러가 30초 뒤 자동으로 이어 간다. 계속 늘면 로그 확인 |
| 배치가 그날 안 돎 | Mac 이 잠들었거나 로그아웃 — 6.3 · `scripts/batch_launchd.sh status <잡>` |
| `pull` 했는데 크롤러 동작이 그대로 | 상주 크롤러 재시작 — 6.4 |
| 업종분류 관련 `FileNotFoundError` | `design/업종분류.md` 없음 — `git pull` 로 받거나 `git checkout design/업종분류.md` |

---

## 10. 주의 — 개인정보 · 보안

- **커밋 금지:** `.env`(키) · `certs/` · `input/` · `data/` · `output/` · `logs/` · 모든 CSV/XLSX(`samples/sample_*.csv` 만 예외).
  `.gitignore` 에 들어 있지만 `git add` 전에 `git status` 로 한 번 더 확인한다. 이 저장소는 **공개(public)** 다.
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
  setup_check.py · run_sample.py · start_batch.py · scheduler.py   점검 · 샘플 · 시작 · Python 스케줄러
  batch.py · batch_launchd.sh                                  일배치 CLI · launchd 등록
  pipeline.py · export_result.py · export_links.py             소량 CSV 파이프라인 · 내보내기
  setup_ca.py · check_status.py · enrich_ftc.py · extract_ksic.py · selftest_batch.py   도구 · 시험
samples/sample_input.csv                                       샘플 10건 (공개 사업자번호)
```
