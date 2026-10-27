"""공개 명단 → 출처별 사업자번호 · 사업자명 파일 (ISMS · CISO 신고 · 산재 공표, 10/26).

배치 잡과 분리된 별도 작업이다. 잡 state.db · registry.db 는 읽기만 하고, 결과는
output/public_lists/ 에 출처별 파일로 낸다 (scripts/public_lists.py).

출처 (2026-10-26 확인)
  CISO    data.go.kr 15080766 중앙전파관리소 정보보호최고책임자 신고 업체현황 — CSV cp949 · 분기 갱신.
          사업자번호가 있다 (신고인이 법인이면 공란인 행 약 2.6%)
  ISMS    isms-p.or.kr 인증서 발급 현황 — HTML 10건/페이지. ISMS · ISMS-P (KISA · 금융보안원) 모두 들어 있다.
          주소 · 사업자번호 없음. isms.kisa.or.kr 은 사내망 DNS 에서 열리지 않는다 (NXDOMAIN)
  산안법  고용노동부 공지 › 공고 'YYYY년 산업재해 발생건수 등 공표' PDF (연 1회) — 제1~5호 · 원·하청 통합공표
  중처법  고용노동부 공고 'YYYY 상/하반기 중대산업재해 발생사실 공표' PDF (반기)
          보도자료(hwpx) 에는 명단이 없다 — 공고 첨부 PDF 를 받는다

사업자번호 찾기 (명단에 번호가 없을 때)
  참조: CISO 명단 · 근로복지공단 산재보험 가입 사업장 전체(약 349만, fetch_kcw) · 공정위 전체 다운로드
  (registry.ftc_registry, 약 268만) · 잡 상호(입력 · 공정위 · 근로복지공단).
  근로복지공단 API 는 사업장명 검색이 없다 (요청변수는 v_saeopjaDrno · opaBoheomFg 뿐 — 다른 이름은 조용히 무시되고
  전체가 온다). 대신 조건 없이 부르면 전체가 사업자번호 · 사업장명 · 주소와 함께 페이지로 와서 통째로 받는다
  (numOfRows 50,000 · 약 70회 · 10/26).
  국민연금 파일은 사업자번호가 앞 6자리뿐이라 쓰지 않는다.
  정규화 상호 완전일치 → 없으면 단어 앞부분(현장 · 공장 · 사업본부를 뗀 것, 4글자 이상) 일치.
  후보 사업자번호가 하나일 때만 채택하고, 여럿이면 소재지 시·도+시·군·구로 좁혀 하나일 때만 (산재).
  글자 유사도는 쓰지 않는다 — 이니셜 상호끼리 0.6~0.7 이 나와 다른 회사를 고른다 (nps_bplc, 10/22).
  3글자 이하 상호는 지역까지 맞을 때만. 산재 소재지는 대부분 재해 현장이라 본사 주소와 달라
  후보가 하나면 지역이 달라도 채택한다 (근거 컬럼에 남김).
"""

from __future__ import annotations

import csv
import html
import json
import logging
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .bizno import BiznoError, format_display, normalize as norm_bizno
from .config import PROJECT_ROOT, http_client
from .namematch import normalize, strip_company_forms
from .region import known_address, sido, sigungu

log = logging.getLogger(__name__)

EXT_DIR = PROJECT_ROOT / "data" / "external"
CISO_DIR = EXT_DIR / "ciso"
ISMS_DIR = EXT_DIR / "ismsp"
MOEL_DIR = EXT_DIR / "moel"
OUT_DIR = PROJECT_ROOT / "output" / "public_lists"

CISO_PK = "15080766"
CISO_MIN_ROWS = 10_000              # 20260731 판 28,153행
ISMS_URL = "https://www.isms-p.or.kr/sttus/cert/selectCrtfctIsueList.do"
ISMS_MAX_AGE_DAYS = 7
KCW_DIR = EXT_DIR / "kcw"
KCW_PAGE = 50_000
KCW_MAX_AGE_DAYS = 30
MOEL = "https://moel.go.kr"
MOEL_NOTICE = f"{MOEL}/news/notice/noticeList.do"
_DATE8 = re.compile(r"(20\d{6})")


# ── 받기 ────────────────────────────────────────────────────

def _latest(dirpath: Path, pattern: str) -> Path | None:
    files = sorted(dirpath.glob(pattern))
    return files[-1] if files else None


def fetch_ciso(*, force: bool = False) -> str:
    """data.go.kr CISO 신고 현황 — 포털 자료 날짜가 로컬보다 새로우면 받는다."""
    from .nps_file import BASE, remote_info

    CISO_DIR.mkdir(parents=True, exist_ok=True)
    have = _latest(CISO_DIR, "ciso_*.csv")
    have_date = _DATE8.search(have.name).group(1) if have else ""
    with http_client(timeout=120.0) as c:
        info = remote_info(c, CISO_PK)
        if have_date >= info["date"] and not force:
            return f"CISO 최신 ({have_date} · 다음 등록 예정 {info['next_regist'] or '?'})"
        final = CISO_DIR / f"ciso_{info['date']}.csv"
        part = final.with_suffix(".csv.part")
        r = c.get(f"{BASE}/cmm/cmm/fileDownload.do",
                  params={"atchFileId": info["atch_file_id"], "fileDetailSn": info["detail_sn"]})
        r.raise_for_status()
        part.write_bytes(r.content)
    rows = _read_ciso(part)
    if len(rows) < CISO_MIN_ROWS:
        part.unlink(missing_ok=True)
        raise ValueError(f"CISO 행 수 부족 {len(rows):,}")
    part.replace(final)
    return f"CISO 받음 {final.name} ({len(rows):,}행)"


_ISMS_CELL = re.compile(
    r'<div class="td (comp_name|auth_number|auth_range|validity|maint_status)[^"]*">'
    r'.*?<div class="cont[^"]*">(.*?)</div>', re.S)      # 유지여부 칸은 class="cont blue"
_ISMS_FIELDS = {"comp_name": "업체명", "auth_number": "인증번호", "auth_range": "인증범위",
                "validity": "유효기간", "maint_status": "유지여부"}


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _parse_isms(page: str) -> list[dict]:
    rows, cur = [], {}
    for key, val in _ISMS_CELL.findall(page):
        if key == "comp_name" and cur:
            rows.append(cur)
            cur = {}
        cur[_ISMS_FIELDS[key]] = _text(val)
    if cur:
        rows.append(cur)
    return rows


def fetch_isms(*, force: bool = False) -> str:
    """ISMS-P 누리집 발급 현황 전체 (약 128페이지 · 페이지 사이 1초). 7일 안에 받은 파일이 있으면 건너뛴다."""
    ISMS_DIR.mkdir(parents=True, exist_ok=True)
    have = _latest(ISMS_DIR, "ismsp_*.csv")
    if have and not force:
        age = (date.today() - date(*map(int, re.findall(r"(\d{4})(\d{2})(\d{2})", have.name)[0]))).days
        if age < ISMS_MAX_AGE_DAYS:
            return f"ISMS 최신 ({have.name}, {age}일 전)"
    rows: list[dict] = []
    with http_client() as c:
        first = c.get(ISMS_URL, params={"pageIndex": 1}).text
        total = int(re.search(r"전체\s*(\d+)\s*건", _text(first)).group(1))
        last = max(map(int, re.findall(r"pageIndex=(\d+)", first) or ["1"]))
        rows += _parse_isms(first)
        for p in range(2, last + 1):
            time.sleep(1.0)
            rows += _parse_isms(c.get(ISMS_URL, params={"pageIndex": p}).text)
    if len(rows) != total:
        raise ValueError(f"ISMS 수집 {len(rows):,} / 전체 {total:,} — 화면 구조 변경?")
    out = ISMS_DIR / f"ismsp_{date.today():%Y%m%d}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(_ISMS_FIELDS.values()))
        w.writeheader()
        w.writerows(rows)
    return f"ISMS 받음 {out.name} ({len(rows):,}건)"


def fetch_kcw(*, force: bool = False) -> str:
    """근로복지공단 산재보험 가입 사업장 전체 → data/external/kcw/kcw_sj_YYYYMMDD.csv.gz.

    한도 장부(kcw)에 호출을 기록하고, 한도가 모자라거나 끊기면 받은 페이지까지 .part 로 두고 다음 실행에 이어 받는다.
    30일 안에 받은 파일이 있으면 건너뛴다.
    """
    import gzip
    import xml.etree.ElementTree as ET

    from .batch.quota import Quota
    from .batch.store import Registry
    from .config import odcloud_service_key
    from .kcomwel import API_URL

    KCW_DIR.mkdir(parents=True, exist_ok=True)
    have = _latest(KCW_DIR, "kcw_sj_*.csv.gz")
    if have and not force:
        age = (date.today() - date(*map(int, re.findall(r"(\d{4})(\d{2})(\d{2})", have.name)[0]))).days
        if age < KCW_MAX_AGE_DAYS:
            return f"근로복지공단 최신 ({have.name}, {age}일 전)"
    part = KCW_DIR / "kcw_sj.part.csv"
    state = KCW_DIR / "kcw_sj.part.json"
    done = json.loads(state.read_text())["page"] if state.exists() and part.exists() else 0
    if not done:
        part.write_text("saeopjaDrno,saeopjangNm,addr,post,seongripDt,sangsiInwonCnt\n", encoding="utf-8")
    fields = ("saeopjaDrno", "saeopjangNm", "addr", "post", "seongripDt", "sangsiInwonCnt")
    reg = Registry()
    quota = Quota(reg)
    key = odcloud_service_key()
    total_pages, calls = None, 0
    try:
        with http_client(timeout=180.0) as c, part.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            page = done + 1
            while total_pages is None or page <= total_pages:
                if not quota.can("kcw", 1):
                    return f"근로복지공단 한도 소진 — {page - 1}페이지까지 받음, 다음 실행에 이어 받음"
                for attempt in range(3):
                    try:
                        r = c.get(API_URL, params={"serviceKey": key, "pageNo": page,
                                                   "numOfRows": KCW_PAGE, "opaBoheomFg": "1"})
                        calls += 1
                        r.raise_for_status()
                        root = ET.fromstring(r.content)
                        if root.findtext(".//resultCode") != "00":
                            raise RuntimeError(root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or "비정상 응답")
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(5 * (attempt + 1))
                total_pages = -(-int(root.findtext(".//totalCount") or 0) // KCW_PAGE)
                for it in root.iter("item"):
                    w.writerow([(it.findtext(k) or "").strip() for k in fields])
                f.flush()
                state.write_text(json.dumps({"page": page, "total_pages": total_pages}))
                quota.record("kcw", 1)
                page += 1
    finally:
        reg.close()
    final = KCW_DIR / f"kcw_sj_{date.today():%Y%m%d}.csv.gz"
    with part.open("rb") as src, gzip.open(final, "wb") as dst:
        dst.writelines(src)
    rows = sum(1 for _ in gzip.open(final, "rt", encoding="utf-8")) - 1
    part.unlink()
    state.unlink(missing_ok=True)
    return f"근로복지공단 받음 {final.name} ({rows:,}행 · {total_pages}페이지 · 이번 호출 {calls}회)"


# 공고 제목 → 로컬 파일 이름
_MOEL_TITLES = (
    (re.compile(r"(\d{4})년 산업재해 발생건수 등 공표"), lambda m: f"osha_{m.group(1)}.pdf"),
    (re.compile(r"(\d{4}) ?(상|하)반기 중대산업재해 발생사실 공표"),
     lambda m: f"sapa_{m.group(1)}H{1 if m.group(2) == '상' else 2}.pdf"),
)


def fetch_moel(*, osha_years: int = 3) -> str:
    """고용노동부 공고 게시판에서 공표 PDF 를 찾아 없는 것만 받는다 (산안법은 최근 osha_years 년)."""
    MOEL_DIR.mkdir(parents=True, exist_ok=True)
    got, seen = [], Counter()
    with http_client(timeout=60.0) as c:
        for keyword in ("산업재해 발생건수 등 공표", "중대산업재해 발생사실 공표"):
            page = c.get(MOEL_NOTICE, params={"searchField": "1", "searchText": keyword, "pageUnit": "50"}).text
            for seq, title in re.findall(r'href="/news/notice/noticeView\.do\?bbs_seq=(\d+)"[^>]*>(.*?)</a>', page, re.S):
                title = _text(title)
                for pat, name_of in _MOEL_TITLES:
                    m = pat.search(title)
                    if not m:
                        continue
                    kind = name_of(m)[:4]
                    seen[kind] += 1
                    if kind == "osha" and seen[kind] > osha_years:
                        continue
                    dest = MOEL_DIR / name_of(m)
                    if dest.exists():
                        continue
                    view = c.get(f"{MOEL}/news/notice/noticeView.do", params={"bbs_seq": seq}).text
                    link = re.search(r'href="(/common/downloadFile\.do\?[^"]*file_ext=pdf)"', view)
                    if not link:
                        log.warning("PDF 첨부 없음: %s", title)
                        continue
                    r = c.get(MOEL + html.unescape(link.group(1)))
                    r.raise_for_status()
                    if not r.content.startswith(b"%PDF"):
                        raise ValueError(f"PDF 아님: {title}")
                    dest.write_bytes(r.content)
                    got.append(dest.name)
    return f"고용노동부 공고 PDF 받음 {len(got)}건 {got}" if got else "고용노동부 공고 PDF 최신"


# ── 읽기 ────────────────────────────────────────────────────

def _read_ciso(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = list(csv.DictReader(text.splitlines()))
    if rows and not {"상호명", "사업자등록번호", "신고일자"} <= set(rows[0]):
        raise ValueError(f"CISO 머리줄이 다름: {list(rows[0])}")
    return rows


def _clean(c: str | None) -> str:
    return re.sub(r"\s+", " ", c or "").strip()


_CORP_MARK = re.compile(r"㈜|\(주\)|주식회사|\(유\)|유한회사")
_CORP_HEAD = re.compile(r"^[(（]\s*(?:주|유|사|재|합)\s*[)）]")          # '(주)○○' — 현장명 괄호가 아니다
_ROLE = re.compile(r"[(（]\s*(원청|하청)\s*(?:[)）]|(?=[(（]))")      # 닫는 괄호가 빠진 원문도 있다
_UNIT = re.compile(r"(?:공장|지점|사업장|사업소|영업소|출장소|연구소|센터|본부|현장)$")


def _is_paren_line(ln: str) -> bool:
    return ln.startswith(("(", "（")) and not _CORP_HEAD.match(ln)


def _lines(cell: str) -> list[str]:
    """줄바꿈을 괄호 짝 단위로 다시 묶는다 — 긴 상호 · 현장명이 칸 안에서 줄바꿈된다."""
    lines, buf = [], ""
    for ln in (cell or "").split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        buf = f"{buf}{ln}" if buf else ln
        if buf.count("(") + buf.count("（") <= buf.count(")") + buf.count("）"):
            lines.append(buf)
            buf = ""
    if buf:
        lines.append(buf)
    return lines


def _party(text: str, role: str) -> dict:
    """'가나㈜(다라, 마바㈜)' → 상호 '가나㈜' · 공동수급 ['다라', '마바㈜'] / '가나㈜ 다라공장' → 사업장 '다라공장'."""
    text = text.strip(" ,")
    partners: list[str] = []
    m = re.search(r"[(（]([^()（）]+)[)）]$", text)
    if m and not _CORP_HEAD.match(m.group(0)) and ("," in m.group(1) or _CORP_MARK.search(m.group(1))):
        partners = [x.strip() for x in m.group(1).split(",") if x.strip()]
        text = text[:m.start()].strip()
    unit = ""
    corp = list(_CORP_MARK.finditer(text))
    tail = text[corp[-1].end():].strip() if corp else ""
    if tail and _UNIT.search(tail) and corp[-1].start() > 0:          # '가나㈜ 다라공장' · '가나㈜다라공장'
        unit, text = tail, text[:corp[-1].end()].strip()
    elif not corp and " " in text and _UNIT.search(text.rsplit(" ", 1)[1]):
        text, unit = text.rsplit(" ", 1)
    return {"상호": text, "원하청": role, "공동수급": partners, "사업장": unit}


def parse_workplace(cell: str) -> tuple[list[dict], str]:
    """산안법 '사업장명(현장명)' 칸 → ([{상호, 원하청, 공동수급, 사업장}, …], 공사·현장명).

    원·하청을 함께 공표하면 회사마다 '(원청)' '(하청)' 꼬리표를 단다 — 꼬리표로 회사를 나누고 뒤에 남은
    괄호가 현장명. 꼬리표가 없으면 줄 단위: '(' 로 시작하는 마지막 줄은 현장명('(주)○○' 은 회사),
    회사 줄이 여럿이고 모두 법인 표기면 회사마다, 아니면 긴 상호가 줄바꿈된 것이라 한 이름으로 붙인다.
    """
    lines = _lines(cell)
    text = "".join(lines)
    parts = _ROLE.split(text)
    if len(parts) > 1:
        parties = [_party(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
        site = parts[-1]
    else:
        site = lines.pop() if len(lines) > 1 and _is_paren_line(lines[-1]) else ""
        names = [ln for ln in lines if not _is_paren_line(ln)]
        site = site or " ".join(ln for ln in lines if _is_paren_line(ln))
        if len(names) > 1 and not all(_CORP_MARK.search(n) for n in names):
            names = ["".join(names)]
        parties = [_party(n, "") for n in names]
    site = site.strip()
    if site[:1] in "(（" and site[-1:] in ")）":
        site = site[1:-1]
    return [p for p in parties if p["상호"]], site.strip()


# 산안법 공표 사유 — 'N. (제N호) …' 머리말과 '○ ’YY년 … (K개소)' 소제목 (2023~2025년 공표 PDF 확인).
# 제5호와 원·하청 통합공표 사이(2025 은 그 뒤)에 舊 시행령 제8조의4 '중대재해 발생 사업장'(2020년까지 재해)
# 구간이 머리말 없이 소제목으로만 온다 — 소제목 문구로 사유를 가른다.
OSHA_REASON = {"1": "제1호 사망자 연간 2명 이상", "2": "제2호 사망만인율 업종평균 이상",
               "3": "제3호 중대산업사고", "4": "제4호 산재 발생 은폐",
               "5": "제5호 산재 보고의무 2회 이상 위반", "6": "원·하청 통합공표",
               "old": "舊 시행령 중대재해 발생 (2020년까지 재해)"}
_OSHA_HEAD = r"\(제\s*[1-5]\s*호\)|원\s*[·ㆍ]\s*하청\s*산업재해\s*통합공표"
_OSHA_SUB = r"○\s*(?:[‘’'`]\s*\d{2}\s*년|최근|원청)[^\n○]{0,80}?\(\s*\d+\s*개소\s*\)"


def read_osha(path: Path) -> tuple[list[dict], dict[str, tuple[int, int]]]:
    """산안법 공표 PDF → 사업장 행 목록, 소제목별 (추출 행 수, 소제목 개소 수) — 검증용."""
    import pdfplumber

    year = re.search(r"(\d{4})", path.name).group(1)
    items: list[dict] = []
    check: dict[str, list[int]] = {}
    section, sub, period, reason, cols, carry = "", "", "", "", {}, {}
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            events = [(h["top"], "head", h["text"]) for h in page.search(_OSHA_HEAD)]
            events += [(h["top"], "sub", h["text"]) for h in page.search(_OSHA_SUB)]
            events += [(t.bbox[1], "table", t) for t in page.find_tables()]
            for _, kind, obj in sorted(events, key=lambda e: e[0]):
                if kind == "head":
                    section = "6" if "통합" in obj else re.sub(r"\D", "", obj)
                    sub, cols, carry = "", {}, {}
                    continue
                if kind == "sub":
                    sub = f"{section}:{_clean(obj)}"
                    check.setdefault(sub, [0, int(re.findall(r"(\d+)\s*개소", obj)[-1])])
                    yy = re.search(r"[‘’'`]\s*(\d{2})\s*년", obj)
                    span = re.search(r"\(([^()]*~[^()]*)\)", obj)
                    period = f"20{yy.group(1)}" if yy and not span else (span.group(1) if span else "")
                    reason = OSHA_REASON["old"] if "중대재해 발생" in obj else OSHA_REASON.get(section, "")
                    cols, carry = {}, {}
                    continue
                for raw in obj.extract():
                    flat = [(_clean(c)).replace(" ", "") for c in raw]
                    if any("사업장명" in c for c in flat):
                        cols = {c: i for i, c in enumerate(flat) if c}
                        continue
                    if not cols:
                        continue
                    get = {}
                    for h, i in cols.items():
                        v = raw[i] if i < len(raw) else None
                        if v is None and h in ("지역", "업종명", "규모"):
                            v = carry.get(h, "")             # 병합 칸 — 위 행 값
                        get[h] = v or ""
                        if h in ("지역", "업종명", "규모") and i < len(raw) and raw[i] is not None:
                            carry[h] = raw[i]
                    name_col = next(h for h in cols if "사업장명" in h)
                    if not _clean(get[name_col]):
                        continue
                    addr_col = next((h for h in cols if "소재지" in h), "")
                    parties, site = parse_workplace(get[name_col])
                    if sub:
                        check[sub][0] += 1
                    detail = "; ".join(f"{h}={_clean(get[h])}" for h in cols
                                       if h not in (name_col, addr_col, "지역", "업종명", "규모") and _clean(get[h]))
                    role_names = {r: " · ".join(strip_company_forms(p["상호"]) for p in parties if p["원하청"] == r)
                                  for r in ("원청", "하청")}
                    for p in parties:
                        items.append({"공표연도": year, "공표사유": reason, "재해연도": period,
                                      "명단상호": p["상호"], "원하청": p["원하청"] or ("함께 공표" if len(parties) > 1 else ""),
                                      "원청사": role_names["원청"], "하청사": role_names["하청"],
                                      "공동수급사": " · ".join(strip_company_forms(x) for x in p["공동수급"]),
                                      "사업장": p["사업장"], "공사·현장명": site,
                                      "지역": _clean(get.get("지역")), "업종": _clean(get.get("업종명")),
                                      "규모": _clean(get.get("규모")), "소재지": _clean(get.get(addr_col)),
                                      "세부": detail, "명단원문": _clean(get[name_col])})
    return items, {k: (v[0], v[1]) for k, v in check.items()}


_SAPA_ROLE = re.compile(r"[(（]\s*(도급인|수급인)\s*[)）]")
_SAPA_OLD = re.compile(r"[(（]\s*舊\s*([^()（）]+)[)）]")


def sapa_companies(title: str) -> list[dict]:
    """중처법 '공표제목' 칸 → [{상호, 도급수급, 구상호}, …].

    '13. ○○건설 주식회사 형 확정 통보에 따른 중대산업재해 발생사실의 공표' — 회사는 번호 뒤 · '형 확정' 앞.
    도급인 · 수급인을 함께 공표하면 '(도급인)' '(수급인)' 꼬리표, 옛 이름은 '(舊 …)'.
    (사업장 명칭 칸은 공사 · 현장 이름이라 사업자명이 아니다 — 10/27 사용자 검토)
    """
    t = re.sub(r"^\s*\d+\s*\.\s*", "", _clean(title))
    t = re.split(r"\s*형\s*확정", t)[0]
    parts = _SAPA_ROLE.split(t)
    pairs = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)] if len(parts) > 1 else [(t, "")]
    out = []
    for text, role in pairs:
        old = _SAPA_OLD.search(text)
        name = _SAPA_OLD.sub("", text).strip(" ,")
        if name:
            out.append({"상호": name, "도급수급": role, "구상호": old.group(1).strip() if old else ""})
    return out


def read_sapa(path: Path) -> list[dict]:
    """중대재해처벌법 공표 PDF → 회사 행 목록 (공표 건에 회사가 둘이면 두 행)."""
    import pdfplumber

    half = re.search(r"(\d{4})H(\d)", path.name)
    label = f"{half.group(1)} {'상' if half.group(2) == '1' else '하'}반기"
    items = []
    with pdfplumber.open(path) as pdf:
        notice = re.search(r"고용노동부\s*공고\s*제\s*([\d-]+)\s*호", pdf.pages[0].extract_text() or "")
        for page in pdf.pages:
            for tbl in page.extract_tables():
                cols = {}
                for raw in tbl:
                    flat = [_clean(c).replace(" ", "") for c in raw]
                    if any("사업장명칭" in c for c in flat):
                        cols = {c: i for i, c in enumerate(flat) if c}
                        continue
                    if not cols:
                        continue
                    def col(key):
                        i = next((i for h, i in cols.items() if key in h), None)
                        return _clean(raw[i]) if i is not None and i < len(raw) else ""
                    site, title = col("사업장명칭"), col("공표제목")
                    if not site and not title:
                        continue
                    companies = sapa_companies(title) or [{"상호": site, "도급수급": "", "구상호": ""}]
                    for c in companies:
                        items.append({"공표반기": label, "공고번호": notice.group(1) if notice else "",
                                      "명단상호": c["상호"], "도급수급": c["도급수급"], "구상호": c["구상호"],
                                      "사업장명": site, "발생일시": col("발생일시"), "발생장소": col("발생장소"),
                                      "재해자수": col("입은사람"), "최근5년발생": col("최근5년"),
                                      "공표제목": title})
    return items


# ── 사업자번호 찾기 ─────────────────────────────────────────

def name_keys(name: str) -> list[tuple[str, str]]:
    """(일치 종류, 정규화 키) — 완전 > 괄호 뗀 것 > 단어 앞부분(긴 것부터, 4글자 이상)."""
    full = normalize(name)
    base_raw = re.sub(r"[(（\[][^)）\]]*[)）\]]", " ", strip_company_forms(name))
    base = normalize(base_raw)
    keys = [("완전", full)]
    if base and base != full:
        keys.append(("괄호제외", base))
    toks = base_raw.split()
    for k in range(len(toks) - 1, 0, -1):
        p = normalize(" ".join(toks[:k]))
        if len(p) >= 4 and p not in (full, base):
            keys.append(("앞부분", p))
    return [(kind, k) for kind, k in keys if len(k) >= 2]


@dataclass
class BiznoIndex:
    """필요한 키만 담은 상호 → {사업자번호: 주소} 색인 (공정위 268만건을 통째로 올리지 않는다)."""
    wanted: set[str]
    by_key: dict[str, dict[str, set[str]]] = field(default_factory=lambda: defaultdict(dict))   # 키 → {번호: 주소들}
    sources: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add(self, name: str, bno: str, addr: str, source: str) -> None:
        k = normalize(name)
        if k in self.wanted and bno:
            addrs = self.by_key[k].setdefault(bno, set())
            if addr:
                addrs.add(addr)            # 근로복지공단은 현장 · 지점이 같은 번호로 여럿 — 하나라도 맞으면 지역 일치
            self.sources[bno].add(source)

    def resolve(self, name: str, addr: str = "", prefer: str = "") -> dict:
        """→ {'bno', '근거', '후보수'} — 못 찾으면 bno ''.

        prefer: 후보가 여럿일 때 이 출처(예: CISO)에 있는 후보가 하나뿐이면 그것 — ISMS 인증 기업은
        정보통신서비스 제공자라 CISO 신고 대상이다 (ISMS 후보 여러 곳 384 중 337 이 CISO 후보 1곳, 10/26).
        """
        s, g = sido(addr), sigungu(addr)
        corp = bool(_CORP_FORM.search(name or ""))
        for kind, key in name_keys(name):
            cands = self.by_key.get(key)
            if corp and cands:
                cands = {b: a for b, a in cands.items() if is_corp_bno(b)}
            if not cands:
                continue
            local = {b for b, addrs in cands.items()
                     if s and any(sido(a) == s and (not g or not sigungu(a) or sigungu(a) == g) for a in addrs)}
            if kind == "앞부분" and len(local) != 1:
                continue                  # 앞부분은 지역까지 맞을 때만 (검증: 7건 중 5건 오답, 10/26)
            if len(cands) == 1 and (len(key) > 3 or local):
                b = next(iter(cands))
                how = "상호+지역" if local else "상호"
            elif len(local) == 1:
                b, how = next(iter(local)), "상호+지역"
            elif prefer and len(pref := {b for b in cands if prefer in self.sources[b]}) == 1:
                b, how = next(iter(pref)), f"상호+{prefer}신고"
            else:
                return {"bno": "", "근거": f"후보 여러 곳({kind})", "후보수": len(cands),
                        "후보": sorted(local if len(local) > 1 else cands), "종류": kind,
                        "지역": len(local) > 1}
            src = "·".join(sorted(self.sources[b]))
            return {"bno": b, "근거": f"{how} {kind}일치({src})", "후보수": len(cands)}
        return {"bno": "", "근거": "못 찾음", "후보수": 0}


# 명단 상호에 회사 · 법인 표기가 있으면 후보를 법인 사업자번호(가운데 81~88)로 좁힌다.
# CISO 명단 실측: 표기 있는 상호 22,256 중 법인번호 22,249 (10/26). 검증 정확도 89.7% → 91.5%
_CORP_FORM = re.compile(r"㈜|\(\s*주\s*\)|주식회사|\(\s*유\s*\)|유한회사|유한책임회사|합자회사|합명회사"
                        r"|사단법인|재단법인|\(\s*[사재]\s*\)")


def is_corp_bno(bno: str) -> bool:
    return len(bno) == 10 and 81 <= int(bno[3:5]) <= 88


def build_index(names: list[str], jobs: list[str], ciso_rows: list[dict]) -> BiznoIndex:
    wanted = {k for n in names for _, k in name_keys(n)}
    idx = BiznoIndex(wanted)
    for r in ciso_rows:
        try:
            idx.add(r["상호명"], norm_bizno(r["사업자등록번호"]), "", "CISO")
        except BiznoError:
            pass
    for job in jobs:
        db = PROJECT_ROOT / "data" / "batch" / job / "state.db"
        if not db.exists():
            continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for r in con.execute("select b_no, input_name, ftc_company_name, kcw_name, ftc_road_address,"
                             " ftc_lot_address, kcw_address, nps_address, final_address from rows"):
            r = dict(r)
            addr = known_address(r) or (r["final_address"] or "")
            for n in (r["input_name"], r["ftc_company_name"], r["kcw_name"]):
                if n:
                    idx.add(n, r["b_no"], addr, job)
        con.close()
    kcw = _latest(KCW_DIR, "kcw_sj_*.csv.gz")
    if kcw:
        import gzip
        with gzip.open(kcw, "rt", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if normalize(r["saeopjangNm"]) in wanted:
                    idx.add(r["saeopjangNm"], r["saeopjaDrno"].replace("-", ""), r["addr"], "근로복지공단")
    reg = PROJECT_ROOT / "data" / "batch" / "registry.db"
    if reg.exists():
        con = sqlite3.connect(f"file:{reg}?mode=ro", uri=True)
        for bno, fields in con.execute("select brno, fields from ftc_registry"):
            if '"company_name"' not in fields:
                continue
            j = json.loads(fields)
            name = j.get("company_name") or ""
            if normalize(name) in wanted:
                idx.add(name, bno, j.get("road_address") or j.get("lot_address") or "", "공정위")
        con.close()
    return idx


# ── 내보내기 ────────────────────────────────────────────────

def _write(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:     # Excel 에서 한글 깨지지 않게 BOM
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _with_bno(item: dict, idx: BiznoIndex, addr: str = "", prefer: str = "") -> dict:
    hit = idx.resolve(item["명단상호"], addr, prefer)
    return {**item, "사업자번호": format_display(hit["bno"]) if hit["bno"] else "",
            "사업자명": strip_company_forms(item["명단상호"]),
            "사업자번호근거": hit["근거"], "후보수": hit["후보수"] or "", "_hit": hit}


# ── 후보 여러 곳 → 국세청 상태로 좁히기 ─────────────────────
# 동명 후보 중 공표 사유가 생긴 때(재해연도 · 발생일 · 인증 시작 · 신고일)에 이미 폐업했거나 국세청에 없는 번호를 뺀다.
# 그때 영업 중이던 후보가 하나뿐이면 채택. 뒤에 폐업한 후보는 남긴다 — 공표 뒤 폐업한 회사일 수 있다.
NTS_CACHE = EXT_DIR / "nts" / "status.json"
NTS_MAX_CANDS = 50                    # 후보가 이보다 많으면 흔한 이름 — 좁혀도 믿기 어렵고 호출만 쓴다


def _nts_status(bnos: set[str], fetch: bool) -> dict[str, dict]:
    """번호 → {'stt': 01/02/03/'' (미등록), 'end': 폐업일}. 캐시에 없는 것만 조회 (100건씩, 한도 장부 nts)."""
    cache = json.loads(NTS_CACHE.read_text()) if NTS_CACHE.exists() else {}
    todo = sorted(b for b in bnos if b not in cache)
    if todo and fetch:
        from .batch.quota import Quota
        from .batch.store import Registry
        from .nts_status import MAX_BATCH, NtsStatusClient

        quota = Quota(Registry())
        with NtsStatusClient() as client:
            for i in range(0, len(todo), MAX_BATCH):
                if not quota.can("nts", 1):
                    log.warning("국세청 한도 부족 — %d건 남기고 멈춤", len(todo) - i)
                    break
                res = client.check_batch(todo[i:i + MAX_BATCH])
                quota.record("nts", 1)
                for r in res:
                    if r.registered or r.error == "국세청 미등록 번호":      # 조회 실패는 캐시하지 않는다
                        cache[r.b_no] = {"stt": r.b_stt_cd if r.registered else "", "end": r.end_dt}
        NTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NTS_CACHE.write_text(json.dumps(cache))
    return cache


def narrow_by_nts(rows: list[dict], since: str, fetch: bool) -> int:
    """'후보 여러 곳' 행을 국세청 상태로 좁힌다. since = 기준 연도 · 날짜가 든 컬럼. 돌려주는 값은 새로 붙인 행 수."""
    todo = [r for r in rows if not r["사업자번호"] and r.get("_hit", {}).get("후보")
            and len(r["_hit"]["후보"]) <= NTS_MAX_CANDS]
    status = _nts_status({b for r in todo for b in r["_hit"]["후보"]}, fetch)
    added = 0
    for r in todo:
        cands = r["_hit"]["후보"]
        if any(b not in status for b in cands):
            continue                     # 모르는 후보가 있으면 판정하지 않는다
        m = re.search(r"(\d{4})\D?(\d{2})?\D?(\d{2})?", r.get(since) or "")
        cutoff = f"{m.group(1)}{m.group(2) or '01'}{m.group(3) or '01'}" if m else ""
        alive = [b for b in cands if status[b]["stt"] and
                 not (status[b]["stt"] == "03" and cutoff and status[b]["end"] and status[b]["end"] < cutoff)]
        if len(alive) == 1:
            hit = r["_hit"]
            r["사업자번호"] = format_display(alive[0])
            r["사업자번호근거"] = (f"상호{'+지역' if hit['지역'] else ''} {hit['종류']}일치"
                               f"+국세청(당시 영업 {len(cands)}곳 중 1곳)")
            added += 1
    return added


def build(jobs: list[str], *, fetch_nts: bool = True) -> dict[str, Counter]:
    """출처별 파일 4개를 만든다. 돌려주는 값은 파일별 사업자번호 근거 집계 (터미널 보고용 — 값 없음)."""
    stamp = f"{date.today():%Y%m%d}"
    ciso_path = _latest(CISO_DIR, "ciso_*.csv")
    isms_path = _latest(ISMS_DIR, "ismsp_*.csv")
    ciso = _read_ciso(ciso_path) if ciso_path else []
    isms = list(csv.DictReader(isms_path.open(encoding="utf-8-sig"))) if isms_path else []
    osha, osha_check = [], {}
    for p in sorted(MOEL_DIR.glob("osha_*.pdf")):
        items, osha_check[p.stem] = read_osha(p)
        osha += items
    sapa = [i for p in sorted(MOEL_DIR.glob("sapa_*.pdf")) for i in read_sapa(p)]

    ciso_nobno = [r for r in ciso if not _bno_or_blank(r["사업자등록번호"])]
    names = ([r["상호명"] for r in ciso_nobno] + [r["업체명"] for r in isms]
             + [i["명단상호"] for i in osha] + [i["명단상호"] for i in sapa])
    idx = build_index(names, jobs, ciso)
    report: dict[str, Counter] = {}

    # CISO — 명단 번호 그대로, 공란만 찾는다
    out = []
    for r in ciso:
        b = _bno_or_blank(r["사업자등록번호"])
        item = {"명단상호": r["상호명"], "신고일자": r["신고일자"], "명단사업자번호": r["사업자등록번호"]}
        if b:
            out.append({**item, "사업자번호": format_display(b), "사업자명": strip_company_forms(r["상호명"]),
                        "사업자번호근거": "명단"})
        else:
            out.append(_with_bno(item, idx))
    if ciso_path:
        report.setdefault("_nts", Counter())["신고일자"] = narrow_by_nts(out, "신고일자", fetch_nts)
        f = OUT_DIR / f"CISO_신고업체_{_DATE8.search(ciso_path.name).group(1)}.csv"
        _write(f, ["사업자번호", "사업자명", "신고일자", "명단상호", "명단사업자번호", "사업자번호근거", "후보수"], out)
        report[f.name] = _summary(out)

    # ISMS
    out = []
    for r in isms:
        start, _, end = (r.get("유효기간") or "").partition("~")
        kind = "ISMS-P" if r["인증번호"].startswith("ISMS-P") else "ISMS"
        issuer = r["인증번호"].split("-")[-3] if r["인증번호"].count("-") >= 3 else ""
        out.append(_with_bno({"명단상호": r["업체명"], "인증종류": kind, "발급기관": issuer,
                              "인증번호": r["인증번호"], "인증범위": r["인증범위"],
                              "유효기간시작": start.strip(), "유효기간종료": end.strip(),
                              "유지여부": r["유지여부"]}, idx, prefer="CISO"))
    if isms_path:
        report.setdefault("_nts", Counter())["유효기간시작"] = narrow_by_nts(out, "유효기간시작", fetch_nts)
        f = OUT_DIR / f"ISMS_인증현황_{_DATE8.search(isms_path.name).group(1)}.csv"
        _write(f, ["사업자번호", "사업자명", "인증종류", "발급기관", "인증번호", "인증범위", "유효기간시작",
                   "유효기간종료", "유지여부", "명단상호", "사업자번호근거", "후보수"], out)
        report[f.name] = _summary(out)

    # 산안법
    if osha:
        out = [_with_bno(i, idx, i["소재지"]) for i in osha]
        report.setdefault("_nts", Counter())["재해연도"] = narrow_by_nts(out, "재해연도", fetch_nts)
        f = OUT_DIR / f"산재공표_산업안전보건법_{stamp}.csv"
        _write(f, ["사업자번호", "사업자명", "원하청", "원청사", "하청사", "공동수급사", "사업장", "공사·현장명",
                   "공표연도", "공표사유", "재해연도", "지역", "업종", "규모", "소재지", "세부",
                   "명단상호", "명단원문", "사업자번호근거", "후보수"], out)
        report[f.name] = _summary(out)
        report["_osha_check"] = osha_check          # type: ignore[assignment]

    # 중처법
    if sapa:
        out = [_with_bno(i, idx, i["발생장소"]) for i in sapa]
        report.setdefault("_nts", Counter())["발생일시"] = narrow_by_nts(out, "발생일시", fetch_nts)
        f = OUT_DIR / f"산재공표_중대재해처벌법_{stamp}.csv"
        _write(f, ["사업자번호", "사업자명", "사업장명", "도급수급", "구상호", "공표반기", "공고번호", "발생일시",
                   "발생장소", "재해자수", "최근5년발생", "명단상호", "공표제목", "사업자번호근거", "후보수"], out)
        report[f.name] = _summary(out)
    return report


def _bno_or_blank(v: str) -> str:
    try:
        return norm_bizno(v)
    except BiznoError:
        return ""          # 공란 · 해외 법인 번호(USA-… 등)


def _summary(rows: list[dict]) -> Counter:
    c = Counter()
    for r in rows:
        how = r.get("사업자번호근거", "")
        c["전체"] += 1
        c["사업자번호 있음" if r.get("사업자번호") else "사업자번호 없음"] += 1
        c["  " + re.sub(r"\(.*\)$", "", how).strip()] += 1
    return c
