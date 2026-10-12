#!/usr/bin/env python3
"""사내 TLS 프록시 환경용 CA 번들 생성.

사내망에서는 프록시가 TLS 를 재서명하므로 Python(certifi) 기본 번들로는
인증서 검증에 실패한다. macOS 키체인에 이미 신뢰돼 있는 루트 CA 를 꺼내
certifi 번들과 합친 파일을 만든다 — 검증을 끄지 않고(verify=False 금지)
정상적으로 신뢰 체인을 세우는 방식이다.

사용:
    python scripts/setup_ca.py                  # 자동 탐지
    python scripts/setup_ca.py --ca-name 회사CA명
"""

from __future__ import annotations

import argparse
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "certs" / "ca-bundle.pem"

# macOS 키체인에서 찾아볼 사내 루트 CA 후보
_KEYCHAINS = [
    "/Library/Keychains/System.keychain",
    "/System/Library/Keychains/SystemRootCertificates.keychain",
]


def _export_ca(name: str) -> str:
    """키체인에서 이름이 일치하는 CA 인증서를 PEM 으로 추출한다."""
    for kc in _KEYCHAINS:
        if not Path(kc).exists():
            continue
        proc = subprocess.run(
            ["security", "find-certificate", "-a", "-c", name, "-p", kc],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and "BEGIN CERTIFICATE" in proc.stdout:
            return proc.stdout
    return ""


def _detect_ca_name(host: str = "api.odcloud.kr") -> str:
    """대상 호스트의 인증서 체인에서 최상위 발급자 CN 을 읽어온다."""
    proc = subprocess.run(
        ["openssl", "s_client", "-connect", f"{host}:443", "-servername", host,
         "-showcerts"],
        input="", capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("i:") and "CN=" in line:
            cn = line.split("CN=", 1)[1].split(",")[0].strip()
            # 공인 CA 는 이미 certifi 에 있으므로 사내 CA 로 보이는 것만 반환
            if cn and "odcloud" not in cn.lower():
                return cn
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="사내 CA 를 포함한 CA 번들 생성")
    ap.add_argument("--ca-name", help="키체인에서 찾을 사내 루트 CA 이름 (미지정 시 자동 탐지)")
    args = ap.parse_args()

    if sys.platform != "darwin" and not args.ca_name:
        print("macOS 가 아니면 --ca-name 과 키체인 추출이 동작하지 않습니다.", file=sys.stderr)
        return 1

    name = args.ca_name or _detect_ca_name()
    if not name:
        print("사내 CA 를 탐지하지 못했습니다. --ca-name 으로 지정하세요.", file=sys.stderr)
        return 1
    print(f"사내 CA: {name}")

    pem = _export_ca(name)
    if not pem:
        print(f"키체인에서 '{name}' 인증서를 찾지 못했습니다.", file=sys.stderr)
        return 1

    base = Path(ssl.get_default_verify_paths().cafile or "")
    try:
        import certifi
        base = Path(certifi.where())
    except ImportError:
        pass
    if not base.exists():
        print("기본 CA 번들을 찾지 못했습니다.", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base, OUT)
    with OUT.open("a", encoding="utf-8") as f:
        f.write(f"\n# ── 사내 루트 CA: {name} ──\n")
        f.write(pem if pem.endswith("\n") else pem + "\n")

    count = OUT.read_text(encoding="utf-8").count("BEGIN CERTIFICATE")
    print(f"생성: {OUT.relative_to(ROOT)} (인증서 {count}개)")
    print("이후 클라이언트가 이 번들을 자동으로 사용합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
