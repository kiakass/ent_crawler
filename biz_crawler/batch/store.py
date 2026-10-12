"""배치 상태 저장소 — 잡별 SQLite + 잡 공용 레지스트리.

CSV 마스터(data/master.csv)는 저장할 때마다 통째로 다시 쓰고 여러 프로세스가
동시에 쓸 수 없다. 100만행 · 일배치 · 상주 크롤러 구성을 위해 SQLite(WAL)로
옮긴다. 컬럼은 CSV 마스터와 같고(biz_crawler.schema), 단계별 상태 컬럼을
더한다 (design/작업계획서.md §6.2 · §6.3).

    data/batch/<job>/state.db   잡 상태 (rows · meta · runs)
    data/batch/registry.db      잡 공용 (API 한도 장부 · 공정위 다운로드 · NAVER 캐시)

한도 장부를 잡 공용 DB 에 두는 이유: 한도는 잡이 아니라 API 키 단위다.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from ..bizno import BiznoError, format_display, normalize
from ..config import PROJECT_ROOT
from ..schema import ALL_FIELDS

KST = ZoneInfo("Asia/Seoul")
BATCH_ROOT = PROJECT_ROOT / "data" / "batch"
REGISTRY_DB = BATCH_ROOT / "registry.db"

# 단계 상태. 값의 뜻은 작업계획서 §6.2
#   waiting  필수 선행 항목이 아직 없음      pending  진행 가능
#   deferred 제약으로 오늘 못 함            done     끝 · skipped 대상 아님
#   failed   실패(사유는 각 *_error)
STATE_FIELDS = [
    "nts_state", "ftc_state", "nps_state",
    "naver_state",          # pending → retry_pending → done (matched 면 바로 done)
    "crawl_state",          # waiting · pending · running · done · failed_tmp · failed · skipped
    "crawl_tries", "crawl_next_at", "crawl_claimed_at",
    "dirty", "updated_at",
]

_TRANSIENT_CRAWL = ("ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError",
                    "PoolTimeout", "WriteTimeout")


def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst() -> str:
    return now_kst().strftime("%Y-%m-%d")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def read_input(path: Path, *, bizno_col: str = "IK_BRNO",
               name_col: str = "EPM_CUST_NM") -> list[tuple[str, str]]:
    """입력 CSV → (원본 사업자번호, 상호명). 인코딩은 UTF-8(BOM) → cp949 순으로 판별."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"인코딩을 판별할 수 없습니다 (UTF-8 · cp949 아님): {path}")
    reader = csv.DictReader(text.splitlines())
    missing = {bizno_col, name_col} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"입력 컬럼 없음: {', '.join(sorted(missing))} (있는 컬럼: {reader.fieldnames})")
    return [((r.get(bizno_col) or "").strip(), (r.get(name_col) or "").strip()) for r in reader]


class JobStore:
    """잡 1개의 상태 DB."""

    def __init__(self, job: str) -> None:
        self.job = job
        self.dir = BATCH_ROOT / job
        self.path = self.dir / "state.db"
        self.conn = _connect(self.path)
        self._ensure_schema()

    # ── 스키마 ──────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        cols = [f"{_q(f)} TEXT DEFAULT ''" for f in ALL_FIELDS if f != "row_no"]
        cols += [f"{_q(f)} TEXT DEFAULT ''" for f in STATE_FIELDS]
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS rows (row_no INTEGER PRIMARY KEY, {', '.join(cols)})")
        # 컬럼이 나중에 늘어난 경우 (schema.py 변경) 빠진 것만 추가한다
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(rows)")}
        for f in [*ALL_FIELDS, *STATE_FIELDS]:
            if f not in have:
                self.conn.execute(f"ALTER TABLE rows ADD COLUMN {_q(f)} TEXT DEFAULT ''")
        for f in ("nts_state", "ftc_state", "nps_state", "naver_state", "crawl_state",
                  "dirty", "updated_at"):
            self.conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{f} ON rows({_q(f)})")
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT,"
            " stage TEXT, started_at TEXT, ended_at TEXT, rows_done INTEGER DEFAULT 0,"
            " calls INTEGER DEFAULT 0, status TEXT, stop_reason TEXT, extra TEXT)")

    # ── meta ────────────────────────────────────────────────

    def meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ── import ──────────────────────────────────────────────

    def import_rows(self, items: Iterable[tuple[str, str]], *, source: str) -> dict[str, int]:
        if self.meta("imported_at"):
            raise RuntimeError(f"잡 {self.job} 은 이미 적재돼 있습니다 ({self.meta('imported_at')})")
        stats = {"rows": 0, "bizno_invalid": 0, "name_empty": 0}
        stamp = now_kst().isoformat(timespec="seconds")
        self.conn.execute("BEGIN")
        for i, (raw_no, name) in enumerate(items, 1):
            try:
                b_no = normalize(raw_no)
            except BiznoError:
                b_no = ""
                stats["bizno_invalid"] += 1
            if not name:
                stats["name_empty"] += 1
            row = {
                "b_no": b_no, "b_no_display": format_display(b_no) if b_no else "",
                "input_name": name,
                # 사업자번호가 없으면 국세청·공정위·국민연금은 대상이 아니다
                "nts_state": "pending" if b_no else "skipped",
                "ftc_state": "pending" if b_no else "skipped",
                "nps_state": "pending" if b_no and name else "skipped",
                "naver_state": "pending" if name else "skipped",
                "crawl_state": "waiting", "crawl_tries": "0",
                "dirty": "1", "updated_at": stamp,
            }
            keys = ["row_no", *row]
            self.conn.execute(
                f"INSERT INTO rows ({', '.join(map(_q, keys))}) VALUES ({', '.join('?' * len(keys))})",
                [i, *row.values()])
            stats["rows"] += 1
        self.set_meta("imported_at", stamp)
        self.set_meta("source", source)
        self.set_meta("total_rows", str(stats["rows"]))
        self.conn.execute("COMMIT")
        return stats

    # ── 행 조회 · 갱신 ──────────────────────────────────────

    def select(self, where: str, params: tuple = (), *, limit: int | None = None,
               order: str = "row_no") -> list[dict]:
        sql = f"SELECT * FROM rows WHERE {where} ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def count(self, where: str = "1=1", params: tuple = ()) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM rows WHERE {where}", params).fetchone()[0]

    def update(self, row_no: int, fields: dict, *, dirty: bool = True) -> None:
        data = {k: ("" if v is None else str(v)) for k, v in fields.items()}
        if dirty:
            data["dirty"] = "1"
        data["updated_at"] = now_kst().isoformat(timespec="seconds")
        sets = ", ".join(f"{_q(k)}=?" for k in data)
        self.conn.execute(f"UPDATE rows SET {sets} WHERE row_no=?", [*data.values(), row_no])

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    # ── 실행 기록 ───────────────────────────────────────────

    def run_start(self, stage: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(day, stage, started_at, status) VALUES(?, ?, ?, 'running')",
            (today_kst(), stage, now_kst().isoformat(timespec="seconds")))
        return int(cur.lastrowid)

    def run_end(self, run_id: int, *, rows_done: int = 0, calls: int = 0,
                status: str = "ok", stop_reason: str = "", extra: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at=?, rows_done=?, calls=?, status=?, stop_reason=?, extra=? "
            "WHERE id=?", (now_kst().isoformat(timespec="seconds"), rows_done, calls, status,
                           stop_reason, json.dumps(extra or {}, ensure_ascii=False), run_id))

    def runs_on(self, day: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM runs WHERE day=? ORDER BY id", (day,))]

    def close(self) -> None:
        self.commit()
        self.conn.close()


def is_transient_crawl_error(error: str) -> bool:
    """다시 시도할 가치가 있는 크롤링 실패인가 (작업계획서 §6.2)."""
    head = (error or "").split(":")[0].strip()
    if head in _TRANSIENT_CRAWL:
        return True
    return head == "HTTPStatusError" and any(f" {c}" in error for c in ("500", "502", "503", "504"))


class Registry:
    """잡 공용 DB — 한도 장부 · 공정위 다운로드 · NAVER 질의 캐시."""

    def __init__(self, path: Path = REGISTRY_DB) -> None:
        self.conn = _connect(path)
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS api_usage (day TEXT, api TEXT, calls INTEGER DEFAULT 0,"
                  " e429 INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, PRIMARY KEY(day, api))")
        c.execute("CREATE TABLE IF NOT EXISTS ftc_registry (brno TEXT PRIMARY KEY, active INTEGER,"
                  " dclr_date TEXT, fields TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS naver_cache (key TEXT PRIMARY KEY, row TEXT,"
                  " fetched_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS zip_cache (keyword TEXT PRIMARY KEY, zipcode TEXT,"
                  " status TEXT, fetched_at TEXT)")

    def get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def put(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ── NAVER 캐시 ──────────────────────────────────────────

    def cache_get(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT row FROM naver_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row["row"]) if row else None

    def cache_put(self, key: str, row: dict) -> None:
        self.conn.execute(
            "INSERT INTO naver_cache(key, row, fetched_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET row=excluded.row, fetched_at=excluded.fetched_at",
            (key, json.dumps(row, ensure_ascii=False), now_kst().isoformat(timespec="seconds")))

    # ── 우편번호 캐시 (같은 주소는 한 번만 조회) ─────────────

    def zip_get(self, keyword: str) -> tuple[str, str] | None:
        row = self.conn.execute("SELECT zipcode, status FROM zip_cache WHERE keyword=?", (keyword,)).fetchone()
        return (row["zipcode"], row["status"]) if row else None

    def zip_put(self, keyword: str, zipcode: str, status: str) -> None:
        self.conn.execute("INSERT INTO zip_cache(keyword, zipcode, status, fetched_at) VALUES(?, ?, ?, ?) "
                          "ON CONFLICT(keyword) DO UPDATE SET zipcode=excluded.zipcode, status=excluded.status",
                          (keyword, zipcode, status, now_kst().isoformat(timespec="seconds")))

    def close(self) -> None:
        self.conn.close()
