"""플랫폼 페이지 크롤링 — 네이버 블로그 등 동적 렌더링 사이트.

정적 크롤러(homepage.py)는 블로그 본문이 iframe 과 스크립트로 그려져
빈 껍데기만 받는다. 여기서는 Playwright(헤드리스 Chromium)로 렌더링한
뒤 같은 사이트의 프레임 HTML 을 모아 homepage.py 의 추출기에 넘긴다.

대상 선정 (2026-10-06 실측, platform 53건):
  - blog.naver.com 34건만 실제로 수집 가능
  - instagram·facebook·cafe.naver 12건은 robots.txt 가 'User-agent: *
    Disallow: /' 라 방문하지 않는다
  - pf.kakao.com 은 로그인 페이지로, 사내망에서는 프록시 경고 페이지로
    떨어진다. 경고 페이지(사설 IP)가 HTTP 200 이라 그대로 두면 프록시
    관리자 이메일을 수집한다 — 착지 호스트로 거른다
"""

from __future__ import annotations

import ipaddress
import logging
import time
from urllib.parse import urlparse

from .homepage import (CrawlResult, HomepageCrawler, USER_AGENT,
                       extract_address, extract_contacts)

log = logging.getLogger(__name__)

# 착지했을 때 수집하면 안 되는 페이지 (로그인 · 폼 · 안내문)
_DEAD_END_HOSTS = ("accounts.kakao.com", "nid.naver.com", "form.naver.com",
                   "auth.band.us")


def _dead_end(url: str) -> str:
    """착지 URL 이 수집 대상이 아니면 사유를, 아니면 빈 문자열을 돌려준다."""
    host = (urlparse(url).hostname or "").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback):
        return "사설 IP 로 이동 (사내 프록시 차단 페이지)"
    if host in _DEAD_END_HOSTS or "/login" in url:
        return f"로그인·폼 페이지로 이동 ({host})"
    return ""


def _same_site(frame_url: str, landed: str) -> bool:
    fh = (urlparse(frame_url).hostname or "").lower()
    lh = (urlparse(landed).hostname or "").lower()
    return bool(fh) and (fh == lh or fh.endswith(".naver.com") and lh.endswith(".naver.com"))


class PlatformCrawler:
    """Playwright 로 렌더링한 페이지에서 연락처를 뽑는다.

    robots.txt 판정·캐시는 HomepageCrawler 의 것을 그대로 쓴다.
    """

    def __init__(self, *, timeout: float = 20.0, delay: float = 1.0) -> None:
        from playwright.sync_api import sync_playwright

        self._timeout_ms = int(timeout * 1000)
        self._delay = delay
        self._robots = HomepageCrawler(timeout=10.0, delay=0)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._ctx = self._browser.new_context(user_agent=USER_AGENT, locale="ko-KR")
        self._page = self._ctx.new_page()

    def __enter__(self) -> "PlatformCrawler":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        for fn in (self._ctx.close, self._browser.close, self._pw.stop,
                   self._robots.close):
            try:
                fn()
            except Exception:
                pass

    def crawl(self, url: str) -> CrawlResult:
        res = CrawlResult(url=url)
        # 플랫폼은 robots.txt 를 못 읽으면 차단으로 본다. 대형 플랫폼이
        # 읽히지 않는 건 대개 로그인 벽이고, 허용으로 두면 instagram 처럼
        # 'Disallow: /' 인 곳을 방문하게 된다.
        if not self._robots._allowed(url, strict=True):
            res.error = "robots.txt 차단"
            return res

        if self._delay:
            time.sleep(self._delay)
        try:
            self._page.goto(url, wait_until="networkidle", timeout=self._timeout_ms)
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {str(exc)[:90]}"
            return res

        landed = self._page.url
        reason = _dead_end(landed)
        if reason:
            res.error = reason
            return res

        # 본문 프레임(PostList·Prologue)이 앞에 오도록 큰 프레임부터 훑는다.
        # extract_contacts 는 [0] 을 대표값으로 쓰므로 순서가 곧 우선순위다.
        htmls: list[str] = []
        for frame in self._page.frames:
            if not _same_site(frame.url, landed):
                continue
            try:
                htmls.append(frame.content())
            except Exception:
                continue
        htmls.sort(key=len, reverse=True)

        res.pages = len(htmls)
        for html in htmls:
            p, m, e = extract_contacts(html)
            for src, dst in ((p, res.phones), (m, res.mobiles), (e, res.emails)):
                dst.extend(x for x in src if x not in dst)
            if not res.addresses:
                res.addresses = extract_address(html)
        res.phones, res.mobiles, res.emails = res.phones[:5], res.mobiles[:3], res.emails[:5]
        if res.found:
            res.source_url = landed
        else:
            res.error = "연락처 미발견"
        return res
