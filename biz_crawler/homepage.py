"""홈페이지 크롤링 — 대표전화·이메일 추출.

스텝 3에서 확보한 URL을 방문해 연락처를 찾는다. 전화·이메일의 사실상
유일한 출처이므로(NAVER telephone 은 전건 빈 값, 공정위는 53건뿐) 이
단계의 확보율이 전체 성공률을 좌우한다.

준수 사항 (PRD 5.1):
  - robots.txt 를 확인하고 Disallow 경로는 방문하지 않는다
  - robots.txt 는 우리 봇 이름(ROBOTS_AGENT)으로 판정한다
  - 운영자 연락처는 CRAWLER_CONTACT 환경변수가 있으면 From 헤더로 보낸다
  - 호스트당 요청 간 딜레이를 두고, 사이트당 요청 수를 제한한다
수집 범위 (PRD 1.3):
  - 휴대폰 번호는 대표자 개인 연락처와 구분이 불가능하므로 대표전화로
    채택하지 않고 별도 컬럼에 보관한다
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import http_client
from .urls import classify

log = logging.getLogger(__name__)

# UA 에 봇 토큰(biz-info-collector)을 넣으면 호스팅사 WAF 가 차단한다.
# 실패 39건 전수 측정에서 브라우저 UA 로만 살아나는 사이트가 5건이었다.
# 그래서 UA 는 순수 브라우저 문자열로 두고, 신원은 robots 판정과
# From 헤더로 남긴다.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
ROBOTS_AGENT = "biz-info-collector"


def request_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"}
    contact = os.getenv("CRAWLER_CONTACT", "").strip()
    if contact:
        headers["From"] = contact
    return headers

RESULT_FIELDS = ["crawl_phone", "crawl_mobile", "crawl_email", "crawl_address",
                 "crawl_source_url", "crawl_pages", "crawl_error"]

# 연락처가 있을 만한 페이지로 이어지는 링크 단서
_CONTACT_HINTS = (
    "contact", "about", "company", "info", "location", "map", "cs",
    "고객", "문의", "연락", "회사소개", "회사", "소개", "오시는", "위치", "약도", "지점",
)

_TAG_STRIP = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# 전화번호: 지역번호(02/0XX) · 대표번호(15XX/16XX/18XX) · 070 · 휴대폰
_PHONE = re.compile(
    r"(?<![\d\-])("
    r"0(?:2|[3-6][1-5]|70|505)[-.\s)]?\d{3,4}[-.\s]?\d{4}"
    r"|1(?:5|6|8)\d{2}[-.\s]?\d{4}"
    r"|01[016789][-.\s]?\d{3,4}[-.\s]?\d{4}"
    r")(?![\d\-])"
)
_MOBILE_PREFIX = ("010", "011", "016", "017", "018", "019")

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.A
)
# 이메일로 잡히지만 연락처가 아닌 것들
_EMAIL_JUNK = (
    "example.com", "domain.com", "yourdomain", "email.com", "test.com",
    "sentry.io", "wixpress.com", "godaddy", "u.kakao", "cdn.",
    "@2x", "sentry-next", "localhost",
)
_EMAIL_BAD_TLD = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
# 호스팅·도메인 업체 주소. 사이트가 에러 페이지를 200 으로 돌려줄 때 잡힌다.
_EMAIL_PROVIDER = (
    "gabia.com", "cafe24.com", "hosting.kr", "whois.co.kr", "megazone",
    "iwinv.kr", "dothome.co.kr", "nayana.com", "smileserv", "ivyro.net",
    "wix.com", "squarespace.com", "imweb.me", "godaddy.com",
)

# 푸터: <footer> 태그, 또는 class/id 에 footer·copyright 류가 붙은 블록.
_FOOTER_TAG = re.compile(r"<footer\b[^>]*>(.*?)</footer\s*>", re.S | re.I)
_FOOTER_ATTR = re.compile(
    r'<(?:div|section|address|aside)[^>]*(?:class|id)\s*=\s*["\'][^"\']*'
    r"(?:footer|copyright|company[-_]?info|site[-_]?info|bottom[-_]?area)"
    r'[^"\']*["\'][^>]*>(.*)', re.S | re.I)

# 한국 주소: 시·도 → 시·군·구 → 도로명/지번. 뒤의 건물명·호수까지 붙인다.
_SIDO = (r"서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|"
         r"충청북도|충청남도|전라북도|전라남도|경상북도|경상남도|"
         r"충북|충남|전북|전남|경북|경남|제주")
_ADDRESS = re.compile(
    r"((?:" + _SIDO + r")"
    r"(?:특별자치시|특별자치도|특별시|광역시|도)?\s"
    # '중구'처럼 2글자 구 이름이 있으므로 최소 1글자. 세종시처럼
    # 시·군·구 없이 바로 도로명이 오는 곳도 있어 전체를 선택적으로 둔다.
    r"(?:[^\s,;|]{1,12}(?:시|군|구)\s)?"
    r"[^<>\n]{2,70}?(?:로|길|동|리|가)\s?\d+[\d\-]*"
    # 건물명 + 호수가 따로 오는 경우가 많아 꼬리를 두 번까지 붙인다
    # ('... 퇴계로 97, 고려대연각빌딩 1501호').
    r"(?:[,\s][^<>\n,]{0,30}?(?:빌딩|타워|센터|층|호|아파트|동|관|A동|B동)){0,2})"
)


@dataclass(slots=True)
class CrawlResult:
    url: str
    phones: list[str] = field(default_factory=list)
    mobiles: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    source_url: str = ""
    pages: int = 0
    error: str = ""

    @property
    def found(self) -> bool:
        return bool(self.phones or self.emails)

    def to_row(self) -> dict:
        return {
            "crawl_phone": self.phones[0] if self.phones else "",
            "crawl_mobile": self.mobiles[0] if self.mobiles else "",
            "crawl_email": self.emails[0] if self.emails else "",
            "crawl_address": self.addresses[0] if self.addresses else "",
            "crawl_source_url": self.source_url,
            "crawl_pages": self.pages,
            "crawl_error": self.error,
        }


def _normalize_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw)
    if len(d) == 8 and d[0] == "1":                 # 1588-1234
        return f"{d[:4]}-{d[4:]}"
    if d.startswith("02"):
        return f"02-{d[2:-4]}-{d[-4:]}" if len(d) >= 9 else raw
    if len(d) in (10, 11):
        return f"{d[:3]}-{d[3:-4]}-{d[-4:]}"
    return raw


def footer_region(html: str) -> str:
    """연락처가 몰려 있는 하단 영역만 잘라낸다.

    실측상 대표전화·이메일·주소는 대부분 푸터에 함께 적혀 있다.
    <footer> 나 class="footer" 블록을 우선 쓰고, 둘 다 없으면 문서
    뒤쪽 30% 를 푸터로 본다. 중첩 태그 때문에 닫는 태그를 정확히
    맞추기 어려우므로 '여기서 문서 끝까지' 로 넉넉히 잡는다.
    """
    body = _TAG_STRIP.sub(" ", html)
    parts = [m.group(1) for m in _FOOTER_TAG.finditer(body)]
    if parts:
        return " ".join(parts)
    m = _FOOTER_ATTR.search(body)
    if m:
        return m.group(1)
    return body[int(len(body) * 0.7):]


def _tidy_address(raw: str) -> str:
    """꼬리 기호를 다듬고 짝 안 맞는 괄호를 닫는다."""
    a = raw.strip(" ,;|·")
    if a.count("(") > a.count(")"):
        a += ")"
    elif a.count(")") > a.count("("):
        a = a.replace(")", "", 1).strip()
    return a


def extract_address(html: str) -> list[str]:
    """푸터 우선으로 한국 주소를 뽑는다."""
    out: list[str] = []
    for chunk in (footer_region(html), html):
        text = _WS.sub(" ", _TAGS.sub(" ", _TAG_STRIP.sub(" ", chunk)))
        for m in _ADDRESS.finditer(text):
            a = _tidy_address(m.group(1))
            if a and a not in out:
                out.append(a)
        if out:
            break
    return out[:3]


def extract_contacts(html: str) -> tuple[list[str], list[str], list[str]]:
    """HTML 에서 (유선전화, 휴대폰, 이메일) 을 중복 제거해 반환한다.

    푸터를 먼저 훑고 그다음 전체 문서를 훑는다. 대표값으로 [0] 을
    쓰기 때문에 순서가 곧 우선순위다 — 본문에 박힌 담당자 번호보다
    푸터의 대표번호가 앞에 와야 한다.
    """
    phones: list[str] = []
    mobiles: list[str] = []
    emails: list[str] = []

    for chunk in (footer_region(html), html):
        text = _TAG_STRIP.sub(" ", chunk)
        # mailto: / tel: 는 태그 제거 전에 살려둔다
        hrefs = " ".join(
            re.findall(r'href=["\']((?:mailto|tel):[^"\']+)["\']', text, re.I))
        text = _WS.sub(" ", _TAGS.sub(" ", text) + " " + hrefs)

        for m in _PHONE.finditer(text):
            num = _normalize_phone(m.group(1))
            digits = re.sub(r"\D", "", num)
            bucket = mobiles if digits[:3] in _MOBILE_PREFIX else phones
            if num not in bucket:
                bucket.append(num)

        for m in _EMAIL.finditer(text):
            e = m.group(0).strip(".").lower()
            if any(j in e for j in _EMAIL_JUNK) or e.endswith(_EMAIL_BAD_TLD):
                continue
            if any(p in e.split("@")[-1] for p in _EMAIL_PROVIDER):
                continue          # 호스팅업체 에러 페이지에서 새어나온 값
            if e not in emails:
                emails.append(e)

    return phones[:5], mobiles[:3], emails[:5]


def _contact_links(html: str, base: str) -> list[str]:
    """루트 페이지에서 연락처로 이어질 만한 링크를 뽑는다."""
    base_host = (urlparse(base).hostname or "").lower()
    out: list[str] = []
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                  html, re.S | re.I):
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        blob = (href + " " + _TAGS.sub("", label)).lower()
        if not any(h in blob for h in _CONTACT_HINTS):
            continue
        url = urljoin(base, href)
        if (urlparse(url).hostname or "").lower() != base_host:
            continue
        if url not in out:
            out.append(url)
    return out


# kr 2단계 도메인. 'a.co.kr' 을 'co.kr' 로 줄이지 않기 위한 목록.
_KR_SLD = ("co", "or", "ne", "go", "re", "pe", "ac", "hs", "ms", "es", "sc", "kg")


def _registrable(url: str) -> str:
    """URL 에서 등록 가능 도메인만 뽑는다 (www 제거)."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) >= 3 and parts[-1] == "kr" and parts[-2] in _KR_SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# 호스팅사가 차단·에러 문서를 내주는 호스트. 9/28 실측: errdoc.gabia.io/403.html
_ERROR_HOSTS = ("gabia.io",)
_ERROR_HOST_PREFIX = ("errdoc.", "error.", "errors.", "block.", "warning.")
# 짧은 본문에 이런 문구만 있으면 사이트가 아니라 에러 문서다
_ERROR_TEXT = re.compile(
    r"\b(?:403|404|500|502|503)\b|forbidden|not found|access denied|"
    r"접근(?:이)?\s*(?:권한|차단|거부)|차단된|서비스\s*준비\s*중|"
    r"페이지를\s*찾을\s*수\s*없|사용\s*중지|일시\s*중지|도메인\s*만료",
    re.I,
)
_ERROR_DOC_MAX = 10_000


def _error_landing(want: str, resp: httpx.Response) -> str:
    """도메인을 넘어간 착지 페이지가 수집 대상이 아니면 사유를 돌려준다.

    정상적인 도메인 이전(학교 누리집 이전, 쇼핑몰의 cafe24 서브도메인,
    기관 도메인 변경)은 통과시키고, 아래만 막는다.
      - 호스팅사 에러·차단 문서 (HTTP 200 으로 온다)
      - 사설·루프백 IP (사내 프록시 경고 페이지)
      - 마켓·배달·이벤트 페이지 (그 회사 연락처를 수집하게 된다)
      - 짧은 본문에 에러 문구만 있는 페이지
    """
    landed = str(resp.url)
    if _registrable(landed) == want:
        return ""
    host = (resp.url.host or "").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback):
        return "사설 IP 로 이동 (프록시 차단 페이지)"
    if (host.startswith(_ERROR_HOST_PREFIX)
            or any(host == h or host.endswith("." + h) for h in _ERROR_HOSTS)):
        return f"호스팅사 에러 문서로 이동 ({host})"
    if classify(landed) == "marketplace":
        return f"마켓 페이지로 이동 ({host})"
    if len(resp.text) < _ERROR_DOC_MAX:
        text = _WS.sub(" ", _TAGS.sub(" ", _TAG_STRIP.sub(" ", resp.text)))
        if _ERROR_TEXT.search(text):
            return f"에러 문서로 이동 ({host})"
    return ""


class HomepageCrawler:
    """사이트 단위 크롤러. 호스트별 robots.txt 와 결과를 캐시한다."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        delay: float = 0.5,
        max_pages: int = 3,
        respect_robots: bool = True,
    ) -> None:
        self._delay = delay
        self._max_pages = max_pages
        self._respect_robots = respect_robots
        self._client = http_client(
            timeout=timeout,
            headers=request_headers(),
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._cache: dict[str, CrawlResult] = {}

    def __enter__(self) -> "HomepageCrawler":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── robots ──────────────────────────────────────────────

    def _robot(self, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        rp: RobotFileParser | None = None
        try:
            # RobotFileParser.read() 는 자체 urlopen 을 쓰므로 사내 CA 를
            # 타지 못한다. 우리 클라이언트로 받아서 parse() 에 먹인다.
            # robots.txt 는 봇 이름으로 받는다. 브라우저 UA 로 받으면
            # instagram 처럼 로그인 HTML(650KB)을 돌려주는 곳이 있어
            # '못 읽음 → 허용' 으로 새어나간다 (2026-10-06 실측).
            resp = self._client.get(f"{origin}/robots.txt",
                                    headers={"User-Agent": f"{ROBOTS_AGENT}/0.1"})
            ctype = resp.headers.get("content-type", "")
            if (resp.status_code == 200 and "html" not in ctype
                    and len(resp.text) < 500_000):
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
        except Exception:
            rp = None
        self._robots[origin] = rp
        return rp

    def _allowed(self, url: str, *, strict: bool = False) -> bool:
        """strict=True 면 robots.txt 를 못 읽었을 때 차단으로 본다 (플랫폼용)."""
        if not self._respect_robots:
            return True
        rp = self._robot(url)
        if rp is None:
            return not strict  # 자체 홈페이지는 robots.txt 가 없는 게 보통이다
        try:
            return rp.can_fetch(ROBOTS_AGENT, url)
        except Exception:
            return True

    # ── 크롤 ────────────────────────────────────────────────

    def _get(self, url: str) -> str:
        resp = self._client.get(url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            raise ValueError(f"HTML 아님 ({ctype.split(';')[0] or 'unknown'})")

        # 호스팅업체 WAF 는 차단을 알리는 에러 문서를 200 으로 돌려준다
        # (예: 요청 도메인 → errdoc.<호스팅사>/403.html). 그대로 두면
        # 호스팅사 연락처를 사업자 연락처로 수집하게 된다. 도메인이 바뀐
        # 것만으로 막으면 정상 이전까지 잃으므로(9/28 순효과 마이너스)
        # 착지 페이지가 무엇인지로 판정한다.
        reason = _error_landing(_registrable(url), resp)
        if reason:
            raise ValueError(reason)
        return resp.text

    def crawl(self, url: str) -> CrawlResult:
        """URL 1건을 크롤링한다. 같은 호스트는 캐시된 결과를 재사용한다."""
        host = (urlparse(url).hostname or "").lower()
        if host and host in self._cache:
            cached = self._cache[host]
            return CrawlResult(url=url, phones=cached.phones, mobiles=cached.mobiles,
                               emails=cached.emails, addresses=cached.addresses,
                               source_url=cached.source_url,
                               pages=0, error=cached.error)

        res = CrawlResult(url=url)
        if not self._allowed(url):
            res.error = "robots.txt 차단"
            if host:
                self._cache[host] = res
            return res

        try:
            html = self._get(url)
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {str(exc)[:90]}"
            if host:
                self._cache[host] = res
            return res

        res.pages = 1
        phones, mobiles, emails = extract_contacts(html)
        res.addresses = extract_address(html)
        if phones or emails:
            res.phones, res.mobiles, res.emails = phones, mobiles, emails
            res.source_url = url

        # 루트에서 못 찾았거나 이메일이 없으면 연락처 페이지를 따라간다
        if not (res.phones and res.emails):
            for link in _contact_links(html, url)[: self._max_pages - 1]:
                if res.pages >= self._max_pages:
                    break
                if not self._allowed(link):
                    continue
                if self._delay:
                    time.sleep(self._delay)
                try:
                    sub = self._get(link)
                except Exception:
                    continue
                res.pages += 1
                p, m, e = extract_contacts(sub)
                if p and not res.phones:
                    res.phones, res.source_url = p, link
                if m and not res.mobiles:
                    res.mobiles = m
                if e and not res.emails:
                    res.emails = e
                    res.source_url = res.source_url or link
                if not res.addresses:
                    res.addresses = extract_address(sub)
                if res.phones and res.emails:
                    break

        if not res.found and not res.error:
            res.error = "연락처 미발견"
        if host:
            self._cache[host] = res
        return res
