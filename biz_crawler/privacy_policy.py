"""사이트의 개인정보 처리방침으로 개인정보 · 민감정보 처리 기업을 가린다 (배치와 별도, 10/27).

처리방침은 개인정보를 처리하는 사업자가 공개해야 하는 문서(개인정보보호법 제30조)라 수집 항목 · 민감정보 ·
고유식별정보가 여기에 적힌다 — 이용약관엔 보통 없다(처리방침이 없을 때만 본다).

1단계 표본(자체 도메인 30곳, 10/27) 실측:
  - 원문을 읽을 수 있는 곳 약 20% · 처리방침 링크 · 글자가 있는 곳 약 50%
  - cafe24 쇼핑몰은 robots.txt 가 /member/privacy.html 을 막는다 — 원문은 읽지 않고 쇼핑몰 빌더라는 사실(주문
    개인정보 처리)로 판정한다. robots 를 우회하지 않는다
  - 링크를 따라가 받은 문서 5개 중 2개는 처리방침이 아니었다 — 첫머리에 처리방침 제목이 있을 때만 인정
  - "민감정보를 수집하지 않습니다" 같은 부정문이 흔하다 — 문장 단위로 보고 부정문은 신호에서 뺀다

등급  A 민감정보 · 고유식별정보 / B 회원 · 주문 · 결제 · 위치 · 아동 / C 이름 · 연락처 정도 / D 확인 불가
근거수준  원문 / 링크만 / 빌더 / 플랫폼 / robots 차단 — 원문이 아닌 판정은 '처리한다' 까지만 알고 항목은 모른다
  - 사이트 전체를 robots 로 막은 곳(검색 · 쇼핑 봇만 허용)은 운영 중인 사이트라 C — '처리방침 없음' 의 D 와 구분 (10/27 사용자 결정)
"""

from __future__ import annotations

import csv
import faulthandler
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .bizno import format_display
from .config import PROJECT_ROOT
from .homepage import USER_AGENT, HomepageCrawler
from .namematch import strip_company_forms
from .urls import classify, host_of, sanitize

log = logging.getLogger(__name__)

PRIV_DIR = PROJECT_ROOT / "data" / "external" / "privacy"
DOC_DIR = PRIV_DIR / "docs"
CACHE = PRIV_DIR / "cache.json"
OUT_DIR = PROJECT_ROOT / "output" / "privacy"
DELAY = 1.0
TEMPLATE_MIN_SITES = 3                # 같은 본문이 이만큼 이상이면 템플릿
SITE_LIMIT = 60                       # 초 — 사이트 1곳 상한, 넘으면 '시간 초과'로 건너뛴다 (10/27 사용자 결정).
                                      # 페이지 JS 가 무한 루프면 page.content() 가 시간 제한을 무시하고 멈춘다

# ── 사이트 종류 ────────────────────────────────────────────
# 쇼핑몰 · 홈페이지 빌더에 올린 사이트는 자체 사이트처럼 본다 (urls._PLATFORM 에 있지만 블로그 · SNS 가 아니다)
BUILDER_HOSTS = ("cafe24.com", "godo.co.kr", "imweb.me", "wixsite.com", "creatorlink.net", "modoo.at")
# 오픈마켓 · 배달 → 주문 개인정보(이름 · 주소 · 연락처) 처리, 예약 플랫폼 → 예약자 이름 · 연락처
COMMERCE = ("smartstore.naver.com", "shopping.naver.com", "brand.naver.com", "coupang.com", "gmarket.co.kr",
            "auction.co.kr", "11st.co.kr", "interpark.com", "tmon.co.kr", "wemakeprice.com", "lotteon.com",
            "ssg.com", "baemin.com", "yogiyo.co.kr", "idus.com", "ohou.se", "kakaomaker.com", "passorder.co.kr")
BOOKING = ("booking.naver.com", "place.naver.com", "m.place.naver.com", "map.naver.com", "catchtable.co.kr")

# 쇼핑몰 빌더 흔적 — 주문 · 결제를 받는 사이트
SHOP_BUILDERS = {"cafe24": r"cafe24|ec-base-", "makeshop": r"makeshop", "godomall": r"godomall|godo\.co\.kr",
                 "sixshop": r"sixshop", "imweb-shop": r"imweb[^\"']*/shop|/shop_cart|_shop_"}
_SHOP_TEXT = re.compile(r"장바구니|주문\s*조회|주문\s*배송\s*조회")

# ── 링크 · 문서 ────────────────────────────────────────────
_A = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_NOISE = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.S | re.I)
PRIV_TITLE = re.compile(r"개인\s*정보\s*(?:처리|취급|보호)\s*방침|privacy\s*policy", re.I)
_PRIV_HREF = re.compile(r"privacy|private|personal|개인정보", re.I)
_TERMS = re.compile(r"이용\s*약관|terms|agreement", re.I)

# ── 항목 신호 (문장 단위, 부정문 제외) ─────────────────────
# '건강' 만 쓰면 건강기능식품 쇼핑몰이 다 걸린다 — 정보 · 상태 · 기록과 붙은 말만
SIGNALS = {
    "민감정보": r"민감\s*정보|건강\s*(?:정보|상태)|병력|진료\s*(?:기록|정보|내역)|장애\s*(?:여부|정보|등급)|유전\s*정보"
                r"|생체\s*(?:정보|인식)|지문|홍채|안면\s*(?:정보|인식)|사상\s*[·ㆍ,]?\s*신념|노동\s*조합|정당의?\s*가입"
                r"|정치적\s*견해|성생활|성적\s*지향|범죄\s*경력",
    "고유식별정보": r"고유\s*식별\s*정보|주민\s*등록\s*번호|여권\s*번호|운전\s*면허\s*번호|외국인\s*등록\s*번호",
    "결제정보": r"카드\s*번호|신용\s*카드\s*정보|계좌\s*번호|은행\s*계좌|결제\s*(?:정보|기록)",
    "위치정보": r"위치\s*정보",
    "아동": r"만\s*14\s*세\s*미만|법정\s*대리인",
    "회원정보": r"회원\s*가입|아이디|비밀\s*번호|ID\s*/\s*PW",
    "일반정보": r"성명|이름|휴대\s*(?:전화|폰)|전화\s*번호|연락처|이메일|e-?mail|주소",
    "수탁": r"위탁|수탁",
    "제3자제공": r"제\s*3\s*자\s*(?:에게\s*)?제공",
    "국외이전": r"국외\s*(?:로\s*)?이전|해외\s*(?:로\s*)?이전",
    "보호책임자": r"(?:개인\s*정보\s*)?(?:보호|관리)\s*책임자",
}
_SIGNAL_RE = {k: re.compile(v, re.I) for k, v in SIGNALS.items()}
_NEG = re.compile(r"않(?:습니다|으며|고|음|는다)|아니(?:합니다|함)|금지|없습니다|없음|불가")
_SENT = re.compile(r"(?<=[.。!?다])\s+|\n+|(?=\s\d+\.\s)|(?=\s[①-⑳])")


def visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAGS.sub(" ", _NOISE.sub(" ", html or ""))).strip()


def signals(text: str) -> dict[str, int]:
    """신호별로 부정문이 아닌 문장 수."""
    out = {k: 0 for k in SIGNALS}
    for s in _SENT.split(text):
        if len(s) < 4:
            continue
        neg = bool(_NEG.search(s))
        for k, rx in _SIGNAL_RE.items():
            if rx.search(s) and not neg:
                out[k] += 1
    return out


def grade(sig: dict[str, int] | None, basis: str, shop: bool) -> str:
    if basis == "원문" and sig:
        if sig["민감정보"] or sig["고유식별정보"]:
            return "A"
        if sig["결제정보"] or sig["회원정보"] or sig["위치정보"] or sig["아동"] or shop:
            return "B"
        return "C"
    if basis in ("빌더", "플랫폼-주문") or shop:
        return "B"
    if basis in ("링크만", "플랫폼-예약", "robots 차단"):
        return "C"
    return "D"


def find_links(html: str, base: str) -> tuple[list[str], list[str]]:
    """(처리방침 링크, 약관 링크) — 같은 사이트 · http(s) 만, 순서 유지."""
    priv, terms = [], []
    for href, label in _A.findall(html or ""):
        url = urljoin(base, href.strip())
        if not url.startswith(("http://", "https://")):
            continue
        text = visible_text(label)
        if PRIV_TITLE.search(text) or _PRIV_HREF.search(href):
            priv.append(url)
        elif _TERMS.search(text) or _TERMS.search(href):
            terms.append(url)
    return list(dict.fromkeys(priv)), list(dict.fromkeys(terms))


def shop_builder(html: str) -> str:
    for name, rx in SHOP_BUILDERS.items():
        if re.search(rx, html or "", re.I):
            return name
    return "쇼핑몰" if _SHOP_TEXT.search(visible_text(html)[:20000]) else ""


# ── 수집 ──────────────────────────────────────────────────

class _Renderer:
    """Playwright — 링크가 스크립트로만 그려지는 사이트용. 필요할 때만 띄운다."""

    def __init__(self) -> None:
        self._pw = self._browser = self._ctx = None

    def html(self, url: str) -> str:
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self._ctx = self._browser.new_context(user_agent=USER_AGENT, locale="ko-KR")
            self._ctx.set_default_timeout(15000)
        page = self._ctx.new_page()
        # alert() · confirm() 창이 뜨면 content() 가 한없이 기다린다 (10/27 전체 수집이 70분 멈춤)
        page.on("dialog", lambda d: d.dismiss())
        try:
            try:
                page.goto(url, timeout=20000, wait_until="networkidle")
            except Exception:
                pass                     # networkidle 이 안 오는 사이트 — 그려진 데까지 쓴다
            parts = [page.content()]
            for f in page.frames[1:]:
                try:
                    parts.append(f.content())
                except Exception:
                    pass
        except Exception:
            page.close()
            raise
        # _SiteTimeout(BaseException) 이면 닫지 않는다 — 멈춘 페이지는 close() 도 멈춘다, abandon() 이 정리
        page.close()
        return "\n".join(parts)

    def close(self) -> None:
        for obj in (self._ctx, self._browser):
            if obj is not None:
                obj.close()
        if self._pw is not None:
            self._pw.stop()

    def abandon(self) -> None:
        """멈춘 브라우저 — 정상 종료도 멈추므로 드라이버 · 브라우저 프로세스를 죽이고 다음 호출에 새로 띄운다."""
        ps = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout.split()
        kids: dict[int, list[int]] = {}
        for pid, ppid in zip(ps[::2], ps[1::2]):
            kids.setdefault(int(ppid), []).append(int(pid))
        todo = list(kids.get(os.getpid(), []))
        while todo:
            pid = todo.pop()
            todo += kids.get(pid, [])
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._pw = self._browser = self._ctx = None


class _SiteTimeout(BaseException):
    """BaseException — crawl_site 의 except Exception 에 삼켜지지 않게."""


def _alarm(signum, frame):
    raise _SiteTimeout()


def _fetch_doc(hc: HomepageCrawler, url: str) -> tuple[str, str]:
    """(본문 텍스트, 실패 사유)."""
    if not hc._allowed(url):
        return "", "robots 차단"
    try:
        html = hc._get(url)
    except Exception as exc:
        return "", type(exc).__name__
    return visible_text(html), ""


def crawl_site(hc: HomepageCrawler, rd: _Renderer, url: str) -> dict:
    """사이트 1곳 → {상태, 빌더, 처리방침URL, 근거수준, 신호, 본문해시, 비고}."""
    r = {"url": url, "빌더": "", "처리방침URL": "", "근거수준": "", "신호": None, "해시": "", "비고": ""}
    if not hc._allowed(url):
        r["근거수준"], r["비고"] = "robots 차단", "메인 robots 차단"
        return r
    try:
        html = hc._get(url)
    except Exception as exc:
        r["비고"] = f"접속 실패({type(exc).__name__})"
        return r
    priv, terms = find_links(html, url)
    if not priv and (len(visible_text(html)) < 1500 or re.search(r"<i?frame\b", html, re.I)):
        try:                                       # 링크가 스크립트 · 프레임 안에 있다
            html2 = rd.html(url)
            p2, t2 = find_links(html2, url)
            priv, terms, html = p2 or priv, terms or t2, html + html2
        except Exception as exc:
            r["비고"] = f"렌더 실패({type(exc).__name__})"
    r["빌더"] = shop_builder(html)
    blocked = False
    for link in (priv[:3] + terms[:2]):
        time.sleep(DELAY)
        text, err = _fetch_doc(hc, link)
        if err:
            blocked = blocked or err == "robots 차단"
            continue
        is_priv = bool(PRIV_TITLE.search(text[:3000]))
        if not is_priv and link in terms and "개인정보" not in text:
            continue                               # 개인정보 조항 없는 약관
        if not is_priv and link in priv:
            continue                               # 처리방침 링크인데 처리방침이 아니다
        r.update(처리방침URL=link, 근거수준="원문", 신호=signals(text),
                 해시=hashlib.sha1(re.sub(r"\s+", "", text).encode()).hexdigest()[:16])
        r["비고"] = "" if is_priv else "이용약관의 개인정보 조항"
        DOC_DIR.mkdir(parents=True, exist_ok=True)
        (DOC_DIR / f"{host_of(url)}.txt").write_text(text, encoding="utf-8")
        return r
    if priv or PRIV_TITLE.search(visible_text(html)):
        r["처리방침URL"] = priv[0] if priv else ""
        r["근거수준"] = "링크만"
        r["비고"] = r["비고"] or ("처리방침 robots 차단" if blocked else "처리방침 원문 못 읽음")
    elif r["빌더"]:
        r["근거수준"] = "빌더"
    else:
        r["비고"] = r["비고"] or "처리방침 없음"
    return r


def site_kind(url: str) -> str:
    """'자체' · '플랫폼-주문' · '플랫폼-예약' · 'SNS' · ''."""
    if not url:
        return ""
    h = host_of(url)
    if any(h == d or h.endswith("." + d) for d in BOOKING):
        return "플랫폼-예약"
    if any(h == d or h.endswith("." + d) for d in COMMERCE):
        return "플랫폼-주문"
    if any(h == d or h.endswith("." + d) for d in BUILDER_HOSTS):
        return "자체"
    return "자체" if classify(url) == "own_domain" else "SNS"


# ── 실행 ──────────────────────────────────────────────────

HEADER = ["사업자번호", "사업자명", "업종대", "등급", "근거수준", "민감정보", "고유식별정보", "결제정보", "위치정보",
          "아동", "회원정보", "일반정보", "수탁", "제3자제공", "국외이전", "보호책임자", "사이트주소", "사이트종류",
          "처리방침URL", "빌더", "템플릿", "판정근거", "비고", "수집일"]


def run(delta: Path, *, refresh: bool = False, progress=None) -> tuple[Path, Counter]:
    """delta 파일 → output/privacy/개인정보처리_<날짜>.csv. 돌려주는 값은 (파일, 집계)."""
    rows = list(csv.DictReader(delta.open(encoding="utf-8-sig")))
    cache: dict[str, dict] = {} if refresh or not CACHE.exists() else json.loads(CACHE.read_text())
    PRIV_DIR.mkdir(parents=True, exist_ok=True)

    todo = []
    for r in rows:
        url = sanitize(r.get("사이트주소"))
        r["_url"], r["_kind"] = url, site_kind(url)
        if (r["_kind"] == "자체" and r.get("사업자상태") == "계속사업자"
                and not (r.get("사이트비고") or "").startswith(("접속 실패", "공유 사이트"))):
            todo.append(url)
    hosts = list(dict.fromkeys(host_of(u) for u in todo))
    first = {host_of(u): u for u in reversed(todo)}
    with HomepageCrawler(timeout=12.0, delay=DELAY) as hc:
        rd = _Renderer()
        old = signal.signal(signal.SIGALRM, _alarm)
        try:
            for i, h in enumerate(hosts, 1):
                if h in cache:
                    continue
                faulthandler.dump_traceback_later(SITE_LIMIT - 10, file=sys.stderr)   # 멈춘 자리를 로그에
                signal.alarm(SITE_LIMIT)
                try:
                    cache[h] = crawl_site(hc, rd, first[h])
                except _SiteTimeout:
                    rd.abandon()
                    cache[h] = {"url": first[h], "빌더": "", "처리방침URL": "", "근거수준": "", "신호": None,
                                "해시": "", "비고": "시간 초과"}
                finally:
                    signal.alarm(0)
                    faulthandler.cancel_dump_traceback_later()
                cache[h]["수집일"] = f"{date.today():%Y-%m-%d}"
                CACHE.write_text(json.dumps(cache, ensure_ascii=False))
                if progress:
                    progress(i, len(hosts))
                time.sleep(DELAY)
        finally:
            signal.signal(signal.SIGALRM, old)
            rd.close()
            CACHE.write_text(json.dumps(cache, ensure_ascii=False))

    dup = Counter(c["해시"] for c in cache.values() if c.get("해시"))
    out, stats = [], Counter()
    for r in rows:
        kind, url = r["_kind"], r["_url"]
        item = {"사업자번호": r.get("사업자번호", ""), "사업자명": strip_company_forms(r.get("상호명")),
                "업종대": r.get("업종대", ""), "사이트주소": url, "사이트종류": kind or "없음"}
        if "-" not in item["사업자번호"]:
            item["사업자번호"] = format_display(item["사업자번호"])
        status = r.get("사업자상태", "")
        c = cache.get(host_of(url)) if kind == "자체" else None
        if status != "계속사업자":
            basis, sig, note = "", None, f"{status or '상태 미상'} — 제외"
        elif kind.startswith("플랫폼"):
            basis, sig, note = kind, None, ("오픈마켓 · 배달 판매자 — 주문자 정보" if kind == "플랫폼-주문"
                                            else "예약 플랫폼 — 예약자 정보")
        elif c:
            basis, sig, note = c["근거수준"], c["신호"], c["비고"]
            if not basis and note == "메인 robots 차단":     # 10/27 이전 캐시
                basis = "robots 차단"
            item.update(처리방침URL=c["처리방침URL"], 빌더=c["빌더"], 수집일=c.get("수집일", ""),
                        템플릿="Y" if c.get("해시") and dup[c["해시"]] >= TEMPLATE_MIN_SITES else "")
        else:
            basis, sig = "", None
            note = {"": "사이트 없음", "SNS": "블로그 · SNS — 처리방침 없음"}.get(kind) or (r.get("사이트비고") or "미수집")
        item["등급"] = grade(sig, basis, bool(item.get("빌더")))
        item["근거수준"] = basis
        if sig:
            for k in SIGNALS:
                item[k] = "Y" if sig[k] else "N"
            item["판정근거"] = " · ".join(f"{k} {v}문장" for k, v in sig.items() if v and k not in ("일반정보", "수탁"))
        item["비고"] = note
        out.append(item)
        stats[f"등급 {item['등급']}"] += 1
        stats[f"근거 {basis or '없음'}"] += 1
        if sig:
            stats["원문 판정"] += 1
            for k in ("민감정보", "고유식별정보", "결제정보", "위치정보", "아동"):
                stats[f"  원문 {k} Y"] += bool(sig[k])
        if item.get("템플릿"):
            stats["템플릿 본문"] += 1
    stats["자체 사이트 수집"] = len(hosts)
    m = re.search(r"(\d{8})", delta.name)
    path = OUT_DIR / f"개인정보처리_{m.group(1) if m else f'{date.today():%Y%m%d}'}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    return path, stats
