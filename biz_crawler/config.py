"""환경변수 로딩 — .env 를 단일 진입점에서만 읽는다."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    pass


def odcloud_service_key() -> str:
    """공공데이터포털 서비스키를 디코딩된 형태로 반환한다.

    포털이 발급하는 "일반 인증키(Encoding)" 는 %2F, %3D 가 섞인 URL 인코딩
    문자열이다. httpx 의 params= 에 그대로 넘기면 % 가 %25 로 재인코딩되어
    인증에 실패하므로, 여기서 한 번 unquote 해서 넘겨준다.
    """
    raw = os.getenv("ODCLOUD_SERVICE_KEY", "").strip()
    if not raw:
        raise ConfigError(
            "ODCLOUD_SERVICE_KEY 가 비어 있습니다. "
            ".env.example 을 .env 로 복사한 뒤 값을 채우세요."
        )
    return unquote(raw)


def ca_bundle() -> str | bool:
    """httpx 의 verify= 에 넘길 CA 번들 경로.

    사내망은 프록시가 TLS 를 재서명하므로 certifi 기본 번들로는 검증에
    실패한다. scripts/setup_ca.py 가 만든 번들이 있으면 그걸 쓰고, 없으면
    기본 검증으로 돌아간다 — 어느 경우에도 검증 자체를 끄지는 않는다.
    """
    override = os.getenv("CA_BUNDLE", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise ConfigError(f"CA_BUNDLE 경로가 없습니다: {path}")
        return str(path)

    local = PROJECT_ROOT / "certs" / "ca-bundle.pem"
    if local.exists():
        return str(local)
    return True


def http_client(**kwargs: object) -> "httpx.Client":
    """사내 CA 번들이 적용된 httpx.Client 를 만든다.

    verify= 를 빼먹으면 사내망에서 CERTIFICATE_VERIFY_FAILED 로 죽는다.
    모든 외부 호출은 이 팩토리를 거치게 해서 실수를 구조적으로 막는다.
    """
    import httpx

    kwargs.setdefault("verify", ca_bundle())
    kwargs.setdefault("timeout", 20.0)
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(**kwargs)  # type: ignore[arg-type]
