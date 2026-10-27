"""국민연금 가입 사업장 내역 — 월별 파일 자동 다운로드 (data.go.kr 파일데이터 15083277).

    GET  /data/15083277/fileData.do           → fn_fileDataDown('15083277', 'uddi:…') 의 상세 키
    POST /tcs/dss/selectFileDataDownload.do   → atchFileId · 파일명(…_YYYYMMDD) · 다음 등록 예정일
    GET  /cmm/cmm/fileDownload.do?atchFileId= → CSV (cp949, 약 115MB)

로그인 없이 받는다 (2026-10-22 확인). 받은 파일은 머리줄 · 크기 · 행 수를 확인한 뒤에만
data/external/nps/nps_bplc_YYYYMMDD.csv 로 둔다 — 실패하면 기존 파일을 그대로 쓴다.
이전 달 파일은 지우지 않는다 (stages.latest_nps_file 가 이름순 마지막을 쓴다).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from .config import http_client

log = logging.getLogger(__name__)

BASE = "https://www.data.go.kr"
DATA_PK = "15083277"
MIN_ROWS = 300_000                  # 2026-09 파일 약 59만 사업장 — 크게 모자라면 잘린 파일
HEADER_KEYS = ("사업자등록번호", "사업장업종코드")
_DATE = re.compile(r"(20\d{6})")


def local_date(dest: Path) -> str:
    """로컬 최신 파일의 자료 날짜 YYYYMMDD (없으면 '')."""
    dates = [m.group(1) for p in dest.glob("nps_bplc_*.csv") if (m := _DATE.search(p.name))]
    return max(dates, default="")


def remote_info(client, data_pk: str = DATA_PK) -> dict:
    """포털의 현재 파일 — {'date', 'atch_file_id', 'detail_sn', 'next_regist', 'name'}.

    data.go.kr 파일데이터는 화면 구조가 같아 다른 데이터셋도 받는다 (public_lists 의 CISO 신고 현황).
    """
    page = client.get(f"{BASE}/data/{data_pk}/fileData.do")
    page.raise_for_status()
    m = re.search(rf"fn_fileDataDown\('{data_pk}',\s*'([^']+)',\s*'[^']*',\s*'(\d+)'", page.text)
    if not m:
        raise ValueError("포털 페이지에서 파일 키를 찾지 못함 (화면 구조 변경?)")
    detail_pk, sn = m.group(1), m.group(2)
    r = client.post(f"{BASE}/tcs/dss/selectFileDataDownload.do",
                    data={"publicDataPk": data_pk, "publicDataDetailPk": detail_pk, "fileDetailSn": sn})
    r.raise_for_status()
    j = r.json()
    reg = j.get("fileDataRegistVO") or {}
    info = j.get("dataSetFileDetailInfo") or {}
    name = reg.get("orginlFileNm") or reg.get("dataNm") or ""
    d = _DATE.search(name)
    if not j.get("atchFileId") or not d:
        raise ValueError(f"다운로드 정보 없음 (status={j.get('status')})")
    return {"date": d.group(1), "atch_file_id": j["atchFileId"], "detail_sn": sn,
            "next_regist": info.get("nextRegistPrarnde") or "", "name": name}


def _verify(path: Path, expected_size: int | None) -> None:
    size = path.stat().st_size
    if expected_size and size != expected_size:
        raise ValueError(f"크기 불일치 {size:,} / {expected_size:,}")
    with path.open(encoding="cp949", errors="replace", newline="") as f:
        header = f.readline()
        if not all(k in header for k in HEADER_KEYS):
            raise ValueError("머리줄이 국민연금 사업장 파일이 아님")
        rows = sum(1 for _ in f)
    if rows < MIN_ROWS:
        raise ValueError(f"행 수 부족 {rows:,} (< {MIN_ROWS:,})")


def fetch_latest(dest: Path, *, force: bool = False) -> dict:
    """포털 파일이 로컬보다 새로우면 받는다.

    돌려주는 값: {'status': 'downloaded' | 'current', 'file', 'remote_date', 'local_date', 'next_regist'}
    실패는 예외 — 부르는 쪽이 기존 파일로 계속 진행한다.
    """
    dest.mkdir(parents=True, exist_ok=True)
    have = local_date(dest)
    with http_client(timeout=120.0) as client:
        info = remote_info(client)
        out = {"remote_date": info["date"], "local_date": have, "next_regist": info["next_regist"]}
        if have >= info["date"] and not force:
            return {**out, "status": "current", "file": f"nps_bplc_{have}.csv"}
        final = dest / f"nps_bplc_{info['date']}.csv"
        part = final.with_suffix(".csv.part")          # *.csv 가 아니라 배치가 집지 않는다
        with client.stream("GET", f"{BASE}/cmm/cmm/fileDownload.do",
                           params={"atchFileId": info["atch_file_id"], "fileDetailSn": info["detail_sn"]}) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0) or None
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        try:
            _verify(part, size)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        part.replace(final)
        log.info("국민연금 파일 받음: %s (%s → %s)", final.name, have or "-", info["date"])
        return {**out, "status": "downloaded", "file": final.name}


def due(next_regist: str, today: str | None = None) -> bool:
    """다음 등록 예정일이 지났거나 모르면 확인할 때다."""
    if not next_regist:
        return True
    return (today or date.today().isoformat()) >= next_regist
