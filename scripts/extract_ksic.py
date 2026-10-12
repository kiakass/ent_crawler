#!/usr/bin/env python3
"""통계청 한국표준산업분류 엑셀에서 세세분류(5자리) 기준표를 뽑는다.

    python scripts/extract_ksic.py [엑셀 경로]

입력: design/7.한국표준산업분류 제11차 개정 분류체계(대-중-소-세-세세 구분) _*.xlsx
      시트 '10차개정한국표준산업분류' · '11차개정한국표준산업분류'
      열 A~J = 대분류 코드·명 · 중분류 · 소분류 · 세분류 · 세세분류 (상위 분류는 처음 행에만 적힘)
출력: data/external/ksic/ksic11_세세분류.csv · ksic10_세세분류.csv (UTF-8 BOM)
      세세분류 1행마다 상위 분류를 채워 넣는다.

openpyxl 없이 표준 라이브러리(zip + XML)로 읽는다 — 의존성을 늘리지 않기 위해.
"""

from __future__ import annotations

import csv
import re
import sys
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "external" / "ksic"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
LEVELS = ["대분류", "중분류", "소분류", "세분류", "세세분류"]
HEADER = [f"{lv}{k}" for lv in LEVELS for k in ("코드", "명")]
# 코드 자릿수: 대분류는 알파벳 1자, 이후 2·3·4·5자리 숫자
_CODE_OK = (re.compile(r"^[A-Z]$"), re.compile(r"^\d{2}$"), re.compile(r"^\d{3}$"),
            re.compile(r"^\d{4}$"), re.compile(r"^\d{5}$"))


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_sheets(path: Path) -> dict[str, list[list[str]]]:
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    out = {}
    for sh in wb.find("m:sheets", NS):
        rid = sh.get(f"{{{NS['r']}}}id")
        xml = ET.fromstring(z.read("xl/" + target[rid].lstrip("/").removeprefix("xl/")))
        grid = []
        for row in xml.find("m:sheetData", NS).findall("m:row", NS):
            cells = {}
            for c in row.findall("m:c", NS):
                v = c.find("m:v", NS)
                if v is not None:
                    val = shared[int(v.text)] if c.get("t") == "s" else v.text
                else:
                    is_ = c.find("m:is", NS)
                    val = "".join(t.text or "" for t in is_.iter(f"{{{NS['m']}}}t")) if is_ is not None else ""
                cells[_col_index(c.get("r"))] = (val or "").strip()
            grid.append([cells.get(i, "") for i in range(10)])
        out[sh.get("name")] = grid
    return out


def extract(grid: list[list[str]]) -> list[list[str]]:
    """세세분류 행마다 상위 분류를 채운 행 목록."""
    cur = [""] * 10
    rows = []
    for r in grid:
        if not re.fullmatch(r"\d{5}", r[8] or ""):
            continue                       # 제목·헤더·빈 행
        for lv in range(4):                # 상위 분류는 새 값이 나오면 바뀌고, 하위 값은 그 아래로 초기화
            code, name = r[lv * 2], r[lv * 2 + 1]
            if code:
                cur[lv * 2], cur[lv * 2 + 1] = code, name
                for k in range(lv * 2 + 2, 8):
                    cur[k] = ""
        cur[8], cur[9] = r[8], r[9]
        rows.append(list(cur))
    return rows


def validate(rows: list[list[str]]) -> list[str]:
    bad = []
    for row in rows:
        for lv, pat in enumerate(_CODE_OK):
            if not pat.match(row[lv * 2]):
                bad.append(f"{row[8]} {LEVELS[lv]} 코드 형식 '{row[lv * 2]}'")
        # 하위 코드는 상위 코드로 시작해야 한다 (대분류 알파벳 제외)
        for lv in range(2, 5):
            if not row[lv * 2].startswith(row[(lv - 1) * 2]):
                bad.append(f"{row[8]} {LEVELS[lv]} 가 상위 코드로 시작하지 않음")
    codes = [r[8] for r in rows]
    if len(codes) != len(set(codes)):
        bad.append("세세분류 코드 중복")
    return bad


def main() -> int:
    # macOS 는 한글 파일명을 NFD 로 저장한다 — glob 패턴(NFC)이 안 맞으므로 정규화해서 찾는다
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else next(
        (p for p in (ROOT / "design").glob("*.xlsx")
         if all(k in unicodedata.normalize("NFC", p.name) for k in ("한국표준산업분류", "11차"))), None)
    if not src or not src.exists():
        raise SystemExit("엑셀 파일을 찾을 수 없습니다 (design/*한국표준산업분류*11차*.xlsx)")
    sheets = read_sheets(src)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name, grid in sheets.items():
        m = re.match(r"(\d+)차", name)
        if not m:
            continue
        rows = extract(grid)
        # 엑셀 2행의 '세세분류(1,196)' 같은 공식 건수와 대조
        declared = re.search(r"세세분류\(([\d,]+)\)", " ".join(grid[1]) if len(grid) > 1 else "")
        want = int(declared.group(1).replace(",", "")) if declared else None
        problems = validate(rows)
        out = OUT_DIR / f"ksic{m.group(1)}_세세분류.csv"
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
        counts = {lv: len({r[i * 2] for r in rows}) for i, lv in enumerate(LEVELS)}
        ok = want is None or want == len(rows)
        print(f"{name}: 세세분류 {len(rows):,}건 (공식 {want if want else '?'}) {'일치' if ok else '불일치 ⚠️'} · "
              + " · ".join(f"{k} {v}" for k, v in counts.items()))
        print(f"  → {out.relative_to(ROOT)}")
        for p in problems[:5]:
            print(f"  ⚠️ {p}")
        if problems or not ok:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
