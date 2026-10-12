"""URL 정제 및 성격 분류.

공정위 `domnCn` 은 사업자가 자유 입력한 텍스트라 도메인이 아닌 값이 섞인다.
실측에서 나온 것들: '쿠팡', '네이버스토어팜', 'n', 한글 IDN('www.개별용달.kr'),
한 칸에 두 도메인('www.a.co.kr www.b.co.kr'). 크롤링 전에 걸러야 무의미한
DNS 조회로 시간을 버리지 않는다.

분류는 크롤링 기대값 순이다:
  own_domain   사업자 자체 홈페이지 — 연락처가 있을 가능성이 높다
  platform     블로그·SNS·홈페이지빌더 — 동적 렌더링이라 정적 크롤링이 어렵다
  marketplace  오픈마켓 입점 주소 — 마켓 대표번호만 나온다
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# 호스트명 규칙: 라벨(영문·숫자·하이픈 또는 punycode) + TLD 2자 이상
_HOSTNAME = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$"
)
_SPLIT = re.compile(r"[\s,;|]+")

# 블로그 · SNS · 홈페이지빌더
_PLATFORM = (
    "blog.naver.com", "cafe.naver.com", "post.naver.com", "m.blog.naver.com",
    "blog.me", "naver.me", "modoo.at", "pf.kakao.com", "open.kakao.com",
    "instagram.com", "facebook.com", "band.us", "tistory.com", "blog.daum.net",
    "youtube.com", "youtu.be", "x.com", "twitter.com", "threads.net",
    "wixsite.com", "blogspot.com", "imweb.me", "creatorlink.net",
    "cafe24.com", "godo.co.kr", "linktr.ee", "litt.ly",
)

# 오픈마켓 · 배달 · 예약 플랫폼
_MARKETPLACE = (
    "smartstore.naver.com", "shopping.naver.com", "brand.naver.com",
    "booking.naver.com", "map.naver.com", "place.naver.com", "m.place.naver.com",
    "coupang.com", "gmarket.co.kr", "auction.co.kr", "11st.co.kr",
    "interpark.com", "tmon.co.kr", "wemakeprice.com", "lotteon.com",
    "ssg.com", "aliexpress.com", "amazon.com", "etsy.com",
    "baemin.com", "yogiyo.co.kr", "passorder.co.kr", "catchtable.co.kr",
    "idus.com", "ohou.se", "kakaomaker.com",
)

KIND_PRIORITY = {"own_domain": 1, "platform": 2, "marketplace": 3}


def sanitize(raw: str | None) -> str:
    """자유 입력 도메인 문자열을 유효한 URL 하나로 정제한다.

    공백·쉼표로 여러 개가 들어오면 첫 번째 유효 값을 쓴다. 한글 도메인은
    punycode 로 변환한다. 도메인으로 보이지 않으면 빈 문자열을 돌려준다.
    """
    text = (raw or "").strip()
    if not text:
        return ""

    for token in _SPLIT.split(text):
        if not token:
            continue
        candidate = token if token.startswith(("http://", "https://")) else "http://" + token.lstrip("/")
        try:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").strip().lower().rstrip(".")
        except ValueError:
            # '[g마켓]' 처럼 대괄호로 싼 자유입력은 Python 3.14 urlparse 가 IPv6 로 보고
            # 예외를 던진다 (2026-10-08 미침투 SME 파일럿에서 발견). 도메인이 아니다.
            continue
        if not host or "." not in host:
            continue
        try:
            # 한글 등 비ASCII 도메인 → punycode
            ascii_host = host.encode("idna").decode("ascii")
        except (UnicodeError, UnicodeDecodeError):
            continue
        if not _HOSTNAME.match(ascii_host):
            continue
        rest = parsed.path if parsed.path not in ("", "/") else ""
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme}://{ascii_host}{rest}{query}"
    return ""


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def classify(url: str) -> str:
    if not url:
        return "none"
    host = host_of(url)
    if not host:
        return "invalid"
    if any(p in host for p in _MARKETPLACE):
        return "marketplace"
    if any(p in host for p in _PLATFORM):
        return "platform"
    return "own_domain"


def pick(naver_link: str | None, ftc_domain: str | None) -> tuple[str, str, str]:
    """두 소스 중 크롤링 기대값이 높은 URL 을 고른다.

    반환: (url, kind, source). 자체 도메인이 있으면 그쪽을 우선하고,
    둘 다 같은 등급이면 공정위 신고 도메인을 택한다 — 사업자가 직접
    신고한 주소라 더 정확하다.
    """
    naver, ftc = sanitize(naver_link), sanitize(ftc_domain)
    options = [(ftc, "ftc"), (naver, "naver")]
    ranked = [
        (KIND_PRIORITY.get(classify(u), 9), i, u, classify(u), src)
        for i, (u, src) in enumerate(options) if u
    ]
    if not ranked:
        return "", "none", ""
    _, _, url, kind, source = min(ranked)
    if naver and ftc:
        source = "both"
    return url, kind, source
