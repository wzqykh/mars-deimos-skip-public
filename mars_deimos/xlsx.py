from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from .bank import BankQuestion


MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF_RE = re.compile(r"([A-Z]+)")

HEADER_ALIASES = {
    "number": {"序号", "编号", "no", "number"},
    "qtype": {"题型", "类型", "type", "qtype"},
    "category": {"类别", "分类", "category"},
    "title": {"题目", "题干", "试题", "title", "question"},
    "options": {"选项", "options", "option"},
    "answers": {"答案", "正确答案", "answer", "answers"},
}


def _column_index(reference: str) -> int:
    match = _CELL_REF_RE.match(reference)
    if not match:
        return 0
    value = 0
    for char in match.group(1):
        value = value * 26 + ord(char) - ord("A") + 1
    return value - 1


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.iter(MAIN_NS + "t"))
        for item in root.findall(MAIN_NS + "si")
    ]


def _first_sheet_path(archive: ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook.find(f".//{MAIN_NS}sheet")
    if sheet is None:
        raise ValueError("workbook contains no worksheet")
    relationship_id = sheet.get(REL_NS + "id")
    if not relationship_id:
        return "xl/worksheets/sheet1.xml"
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for relation in rels.findall(PACKAGE_REL_NS + "Relationship"):
        if relation.get("Id") == relationship_id:
            target = relation.get("Target", "worksheets/sheet1.xml").lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    return "xl/worksheets/sheet1.xml"


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.get("t")
    value_node = cell.find(MAIN_NS + "v")
    if cell_type == "inlineStr":
        inline = cell.find(MAIN_NS + "is")
        if inline is None:
            return ""
        return "".join(node.text or "" for node in inline.iter(MAIN_NS + "t"))
    if value_node is None:
        return ""
    raw = value_node.text or ""
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


def _read_rows(path: Path) -> list[list[str]]:
    with ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheet_path = _first_sheet_path(archive)
        root = ET.fromstring(archive.read(sheet_path))
    rows: list[list[str]] = []
    for row in root.iter(MAIN_NS + "row"):
        values: dict[int, str] = {}
        for cell in row.findall(MAIN_NS + "c"):
            values[_column_index(cell.get("r", "A1"))] = _cell_value(cell, shared)
        width = max(values, default=-1) + 1
        rows.append([values.get(index, "") for index in range(width)])
    return rows


def _canonical_header(value: str) -> str | None:
    normalized = value.strip().lower().replace(" ", "")
    for canonical, aliases in HEADER_ALIASES.items():
        if normalized in {alias.lower() for alias in aliases}:
            return canonical
    return None


def _mapping_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    header_index = next(
        (index for index, row in enumerate(rows) if sum(bool(_canonical_header(cell)) for cell in row) >= 4),
        None,
    )
    if header_index is None:
        raise ValueError("could not locate a bank header row")
    header = rows[header_index]
    columns: dict[int, str] = {}
    for index, value in enumerate(header):
        canonical = _canonical_header(value)
        if canonical:
            columns[index] = canonical
    missing = {"qtype", "title", "options", "answers"} - set(columns.values())
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")
    result: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        mapped = {columns[index]: row[index] if index < len(row) else "" for index in columns}
        if any(str(value).strip() for value in mapped.values()):
            result.append(mapped)
    return result


def import_xlsx(path: str | Path) -> list[BankQuestion]:
    source = Path(path)
    rows = _read_rows(source)
    questions: list[BankQuestion] = []
    for row in _mapping_rows(rows):
        try:
            questions.append(BankQuestion.from_mapping(row, source="xlsx"))
        except ValueError:
            continue
    if not questions:
        raise ValueError("no valid questions found in workbook")
    return questions
