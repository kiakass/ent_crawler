# CLAUDE.md — ent_crawler (사업자정보 수집기)

Claude Code 가 이 저장소에서 작업할 때 따르는 지침. 사람용 설명은 [README.md](README.md) 에 있다 — 명령 · 옵션 · 결과 파일은 README 가 기준이다.

## 무엇을 하는 프로젝트인가

사업자번호 + 상호명 CSV → 국세청 · 공정위 · 국민연금 · NAVER 지역검색 · 도로명주소 API + 홈페이지 크롤링으로
상호 · 주소 · 우편번호 · 전화 · 이메일 · 사이트 · 업종을 채운다. 대량은 API 일 한도 때문에 **일배치**(매일 00:10, 며칠~몇 주)로 돈다.
상태는 잡별 SQLite(`data/batch/<잡>/state.db`), 한도 장부 · 캐시는 공용 `data/batch/registry.db`.

## 처음 받은 사람의 환경을 만들 때 — 이 순서로

1. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python -m playwright install chromium`
   — venv 이름은 반드시 `.venv` (`batch_launchd.sh` 가 `.venv/bin/python` 을 쓴다)
2. `python scripts/setup_check.py --init` — `.env` 템플릿 복사 · 폴더 생성 · 빠진 것 목록 (외부 호출 0회, 키 값은 출력 안 함)
3. **사용자가 직접 할 일**을 목록으로 안내하고 기다린다. Claude 가 대신할 수 없다:
   - API 키 발급 후 `.env` 입력 (README 2절). 키는 **사용자 본인 계정으로 새로 발급** — 남의 키를 쓰면 한도 장부가 따로 놀아 함께 초과한다
   - (권장) 국민연금 월별 CSV — `python scripts/fetch_nps_file.py` 로 받는다(로그인 불필요 · Claude 가 실행 가능, 약 115MB) · (선택) 통계청 KSIC xlsx → `python scripts/extract_ksic.py <xlsx>`
   - `sudo pmset repeat wakeorpoweron MTWRFSU 00:05:00` (sudo 라 사용자가 `! ` 로 직접)
4. 사내망이면 `python scripts/setup_ca.py` → `export SSL_CERT_FILE=$PWD/certs/ca-bundle.pem`
5. `python scripts/setup_check.py` 가 종료 코드 0 이 될 때까지 반복
6. `python scripts/run_sample.py` — 샘플 10건 전 과정. 기대치: 사업자상태 9 · 주소 9 · 업종소 9 · 미등록 1건 (README 4절 표).
   어긋나면 README 4절의 "0 이면 ~ 키 문제" 표로 원인을 좁힌다
7. 실제 목록: 사용자가 `input/` 에 CSV 를 두면 `python scripts/start_batch.py --job <잡>` (점검 → 적재 → 추정 → launchd 등록).
   **launchd 등록 · 대량 import 는 외부 API 한도를 쓰고 매일 자동으로 도는 일이라 실행 전에 사용자에게 확인**한다.
   먼저 `--mode none` 이나 `batch.py plan` 으로 예상 일수를 보여주는 것이 좋다

## 매일 운영 점검 (사용자가 "배치 확인" 을 부탁하면)

```bash
source .venv/bin/activate && export SSL_CERT_FILE=$PWD/certs/ca-bundle.pem
python scripts/batch.py status --job <잡>
cat output/batch/<잡>/daily_progress.csv | tail -3
launchctl print gui/$(id -u)/kr.lguplus.bizcrawler.<잡>.crawl | grep -E "runs|state"     # runs 가 급증하면 크롤러가 죽고 있다
grep -c "database is locked" logs/batch/kr.lguplus.bizcrawler.<잡>.crawl.err.log
```

- `partial` = 그날 한도를 다 써서 정상 정지. 오류는 `error` 만
- launchd KeepAlive 가 죽은 크롤러를 살려서 겉보기엔 정상이다 — `runs` 증가와 err 로그 건수를 꼭 본다
- 결과가 비어 보이면 실행 실패라고 단정하기 전에 `output/batch/<잡>/` 경로가 맞는지부터 본다

## 반드시 지킬 규칙

### 개인정보 — 최우선
- **응답 · 로그 · 보고에 고객 데이터(상호명 · 사업자번호 · 연락처 · 주소)를 쓰지 않는다. 집계 · 비율 · 분포로만 보고한다.**
  개별 행은 사용자가 먼저 그 행을 지목했을 때만 최소 범위로. 터미널 출력 스크립트도 원문을 덤프하지 않게 짠다
- 대표자명 · 대표자 이메일 · 휴대폰(`crawl_mobile`)은 결과로 내보내지 않는다 — 대표전화로 병합하지 않는 이유
- 이 저장소는 **public** 이다. `.env` · `certs/` · `input/` · `data/` · `output/` · `logs/` · CSV/XLSX · `design/`(내부 문서) · `work_hist/`(세션 인계) · `todo/` 를 커밋하지 않는다
  (예외: `samples/sample_*.csv` — 사업자가 스스로 공개한 번호만 · `design/업종분류기준/업종분류.md` — 업종분류표, 병합 필수). 커밋 전 `git status` 와 staged 파일 목록을 확인

### 코드
- 외부 HTTP 는 **반드시 `biz_crawler.config.http_client()`** 로 만든다. `httpx.Client` 를 직접 만들면 사내 CA 를 빼먹는다. `verify=False` 금지
- `https://apis.data.go.kr` 는 TLS 핸드셰이크를 거부한다 — 이 호스트만 `http://`
- data.go.kr 키는 Encoding 값을 `.env` 에 두고 `config.odcloud_service_key()` 가 unquote 한다 (그대로 넘기면 `%25` 이중 인코딩)
- 로그에 키가 남지 않게 `logging_utils.RedactingFilter` 를 건다 (httpx 는 URL 을 INFO 로 찍는다)
- **외부 API 를 부르는 동안 SQLite 트랜잭션을 쥐지 않는다.** API 단계는 행마다 커밋(`_Tx(hold=False)`), 로컬 처리만 묶어 커밋.
  트랜잭션을 연 채 API 재시도가 길어지면 상주 크롤러가 `database is locked` 로 죽는다 (실제로 나흘간 164회)
- 배치 코드를 고친 뒤엔 상주 크롤러를 재시작한다: `launchctl kickstart -k gui/$(id -u)/kr.lguplus.bizcrawler.<잡>.crawl`.
  KeepAlive 워커는 이전 코드를 계속 쥐고 있어 새 컬럼과 섞여 실패한다. daily 는 매번 새 프로세스라 괜찮다
- 바꾼 뒤 `python scripts/selftest_batch.py` (외부 API 소량) 와 `python scripts/run_sample.py --reset` 로 확인

### 데이터 출력
- 사업자번호는 항상 하이픈 표기(`bizno.format_display()`). 숫자 10자리는 Excel 이 `1.82E+09` 로 바꿔 복구 불가 — 500건 중 373건을 잃은 적이 있다.
  입력 쪽 `bizno.normalize()` 는 과학적 표기를 추측하지 않고 `BiznoError`
- 기업명(상호명)을 저장할 때는 회사 표기를 뺀다 — `(주)` `㈜` `주식회사` `유한회사` `(유)` `합자회사` `합명회사` `유한책임회사` `사단법인` `재단법인` `(사)` `(재)`.
  **`namematch.strip_company_forms()`** 로 한다 (병합 `final_company_name` 에 적용, 새 출처의 기업명도 같은 함수로). 10/26 사용자 결정.
  `협동조합` `영농조합법인` `사회복지법인` `의료법인` `학교법인` 은 기관 성격이라 **남긴다**.
  비교용 키 `normalize()` · 검색어용 `strip_org_raw()` 와는 다른 함수다 — 그 둘은 단체 표기 · 영문 `Ltd` 까지 지운다

## 소스별 함정 (실측 — 코드에 이미 반영됨, 고칠 때 되돌리지 말 것)

**NAVER 지역검색** — NCP API HUB(`naverapihub.apigw.ntruss.com/search/v1/local`, 헤더 `X-NCP-APIGW-API-KEY-ID`/`-KEY`).
`total` 은 항상 5(판정은 `items`) · `telephone` 은 전건 빈 값 · 우편번호 없음. 상호만으로 찾으면 **다른 지역 동명 업소를 51% 고른다** —
사업자번호로 확정된 공정위 · 국민연금 주소의 시·군·구를 질의에 붙이고, 시·도가 같을 때만 믿는다. 질의에서 `주식회사` 등 법인 표기를 떼고 채점은 원문으로.
3글자 이하 상호는 유사도 0.85 이상만. 점수 0.6 미만 업소의 값은 버린다. 5곳 이상이 공유하는 `link`(프랜차이즈 본사 · 대행사 블로그)의 연락처는 그 업소 것이 아니다.

**공정위** — 전화 54% 가 `010-개인정보` 자리표시(숫자 8자리 이상만 인정) · `N/A` `-` 도 빈 값. 도로명주소 60% 가 건물번호 없음 —
건물번호 있는 다른 출처 주소를 먼저. 이메일은 대표자 개인 메일뿐이라 미채택. 건별 조회는 일 1만이라 대량은 전체 다운로드(277페이지) 후 로컬 조인.
도메인에 `[g마켓]` 같은 대괄호 값 → `urlparse` ValueError, 감싸서 버린다.

**국민연금** — 파라미터는 camelCase(`bzowrRgstNo`, `wkplNm`; snake_case 는 조용히 0건). 사업자번호는 **앞 6자리만** 검색된다 →
`nps_bplc.judge()` 로 판정 — **확정은 완전일치 · 포함(짧은 쪽 4글자 이상) · 지점 제외 일치만**. 글자 유사도(SequenceMatcher)는 0.85 를 넘어도 확정 아님 —
영문 이니셜 상호(`에이치` · `이앤씨`)끼리 0.6~0.7, `○○어린이집` 끼리 0.86 이 나와 다른 회사를 고른다(10/22 사람 검토). 글자 유사도 0.6 이상 +
업종 명사(`INDUSTRY_NOUNS`) 공유면 `industry_only` — 업종만 쓰고 주소 · 우편번호 · NAVER 지역 기준으로는 안 쓴다(근거 `국민연금(유사상호)`).
채점 상호는 입력 · 공정위 · 근로복지공단(`_nps_names`) — 병합 상호엔 NAVER 업소명이 들어가 쓰지 않는다. 업종명 `BIZ_NO미존재사업장`(99…) · `해당없음`(00…) · `94…`(프리랜서 · 간병인 · 보험설계사 등 자체 코드) 은 업종이 아니다.
업종명이 KSIC 와 다르게 괄호 부기(`…(건축사)`) · 세분류 이름(`주거용 건물 건설업` → 4자리)으로 오기도 한다 — `ksic.lookup()` 이 처리. 대량은 월별 CSV 조인(API 와 업종코드 95% 일치).
**10/22 부터 파일 기반** — API 보완은 기본 꺼짐(잡 meta `nps_api`, `batch.py nps-api --job J on` 으로 다시 켬). API 로만 얻는 행은 7.6%(파일에 없는 사업장)뿐인데
하루 4,700행이 한계라 배치 전체를 붙잡았다. 못 찾은 행은 `api_pending` 으로 보존. `daily` 의 `nps_file` 단계가 포털 다음 등록 예정일 뒤 새 파일을 받고,
`stage_nps` 가 meta `nps_file` 과 이름이 다르면 못 찾은 행을 새 파일로 재대조한다.

**크롤링** — 한국 호스팅사 WAF 는 차단을 **HTTP 200 에러 페이지**로 준다(예: 호스팅사 403 문서로 리다이렉트). 착지 페이지가 에러 문서인지로 판정하고,
"도메인이 바뀌면 실패" 로 막지 말 것(정상 이전까지 버려 결과가 줄었다). 사내 프록시 차단 페이지(사설 IP 로 이동 후 200)도 실패 처리(`platform.py::_dead_end`).
robots.txt 는 봇 UA 로 받는다 — 브라우저 UA 로 받으면 일부 SNS 가 로그인 HTML 을 줘서 fail-open 이 Disallow 를 뚫는다.
html 응답은 robots 로 파싱하지 않고, 플랫폼(블로그 · SNS)은 fail-closed, 자체 홈페이지는 fail-open.

**공개 명단** (`scripts/public_lists.py`, 배치와 별도 · 10/26) — CISO 신고 명단만 사업자번호가 있고 ISMS · 산재는 상호로 찾는다.
근로복지공단 API 는 사업장명 검색이 없다(다른 파라미터는 조용히 무시되고 전체가 온다) — 조건 없이 `numOfRows=50000` 으로 산재 가입 사업장
349만 건을 70회에 통째로 받아 참조로 쓴다 (`fetch_kcw`, 한도 장부 kcw 기록). 이걸 넣고 검증 정확도 91% → 98%.
후보가 여럿이면 국세청 상태로 기준일 전 폐업 · 미등록 후보를 빼고 1곳 남을 때만 채택(`narrow_by_nts`, 검증 97%) — 기준일 뒤 폐업한 후보는 빼지 않는다(공표 뒤 폐업일 수 있다). 후보 50곳 초과는 조회하지 않는다.
유일 후보만 채택 · 회사 표기 상호는 법인 번호(가운데 81~88)만 · 앞부분 일치는 지역까지 맞을 때만 (검증 7건 중 5건 오답) · 글자 유사도 금지.
ISMS 는 후보가 여럿이면 CISO 신고 업체 하나를 고른다. 산안법 PDF 는 `○ ’YY년 … (N개소)` 소제목이 단위 — 머리말 없는 舊 시행령
'중대재해 발생 사업장' 구간이 있다. 소재지는 재해 현장이라 본사와 다르다.
사업장명 칸은 `parse_workplace()` — `(원청)` `(하청)` 꼬리표로 회사를 나누고(닫는 괄호 빠진 원문 있음), `(주)○○` 로 시작하는 줄은 현장명이 아니라 회사다.
중처법 PDF 는 회사가 '공표제목' 칸(번호 뒤 · '형 확정' 앞, `(도급인)` `(수급인)` · `(舊 …)`)에 있고 '사업장 명칭' 칸은 현장 이름이다 (`sapa_companies()`, 10/27 사용자 검토). 보도자료 hwpx 엔 명단이 없고 공고 PDF 에 있다.

## 구조 요약

`biz_crawler/` 소스 클라이언트 · 병합 · 업종 / `biz_crawler/batch/` store(SQLite) · quota(한도 장부, 일 한도 × 95%) · stages(단계 실행기) /
`scripts/batch.py` 일배치 CLI · `batch_launchd.sh` launchd · `scheduler.py` Python 스케줄러 · `start_batch.py` 시작 · `setup_check.py` 점검 · `run_sample.py` 샘플.
단계 상태 값과 흐름은 `biz_crawler/batch/store.py` 상단 주석, 단계 원칙은 `stages.py` 상단 docstring.
