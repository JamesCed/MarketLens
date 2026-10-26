"""
app/utils/xlsx_reader.py
--------------------------
A fast, read-only .xlsx reader for the Gov't Data Upload.

openpyxl builds a Python object for every cell and style, which made it
the slowest step of an upload: about 1.7 s for the 10,442-row sample on a
laptop, many times that on the deployment's fraction of a CPU. This reads
the same values straight from the workbook's XML with the C expat parser
-- shared strings, numbers, booleans, inline strings and dates (from the
cell's number format) -- in about a third of the time, and returns rows
exactly as openpyxl's iter_rows(values_only=True) would.

Anything it does not understand raises, and the caller falls back to
openpyxl, so an unusual workbook is slower to read but never misread.
"""

import posixpath
import zipfile
from xml.parsers import expat

from openpyxl.styles.numbers import builtin_format_code, is_date_format, is_timedelta_format
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, from_excel, from_ISO8601


class _Stop(Exception):
    pass


def _local(name):
    return name.rsplit(":", 1)[-1]


def _parse(data, start=None, end=None, text=None):
    parser = expat.ParserCreate()
    parser.buffer_text = True
    if start:
        parser.StartElementHandler = start
    if end:
        parser.EndElementHandler = end
    if text:
        parser.CharacterDataHandler = text
    parser.Parse(data, True)


def _shared_strings(archive, names):
    path = "xl/sharedStrings.xml"
    if path not in names:
        return []
    strings, parts = [], []
    state = {"text": False, "phonetic": 0}

    def start(name, _attrs):
        tag = _local(name)
        if tag == "si":
            parts.clear()
        elif tag == "t":
            state["text"] = True
        elif tag == "rPh":
            state["phonetic"] += 1

    def end(name):
        tag = _local(name)
        if tag == "t":
            state["text"] = False
        elif tag == "rPh":
            state["phonetic"] -= 1
        elif tag == "si":
            strings.append("".join(parts))

    def text(data):
        if state["text"] and not state["phonetic"]:
            parts.append(data)

    _parse(archive.read(path), start, end, text)
    return strings


def _date_styles(archive, names):
    """{cell style index: True for a duration, False for a date} for the
    styles whose number format shows a date or time -- decided by
    openpyxl's own format rules, so both readers agree on every cell."""
    path = "xl/styles.xml"
    if path not in names:
        return {}
    custom, formats = {}, []
    state = {"xfs": False}

    def start(name, attrs):
        tag = _local(name)
        if tag == "numFmt":
            custom[int(attrs.get("numFmtId", "0"))] = attrs.get("formatCode", "")
        elif tag == "cellXfs":
            state["xfs"] = True
        elif tag == "xf" and state["xfs"]:
            formats.append(int(attrs.get("numFmtId", "0")))

    def end(name):
        if _local(name) == "cellXfs":
            state["xfs"] = False

    _parse(archive.read(path), start, end)
    styles = {}
    for index, fid in enumerate(formats):
        code = custom[fid] if fid in custom else builtin_format_code(fid)
        if is_date_format(code):
            styles[index] = bool(is_timedelta_format(code))
    return styles


def _sheet_paths(archive, names):
    """(worksheet paths in workbook order, date epoch)."""
    targets = {}

    def rel(name, attrs):
        if _local(name) == "Relationship":
            target = attrs.get("Target", "")
            targets[attrs.get("Id")] = (target.lstrip("/") if target.startswith("/")
                                        else posixpath.normpath(posixpath.join("xl", target)))

    _parse(archive.read("xl/_rels/workbook.xml.rels"), rel)
    sheets = []
    epoch = [CALENDAR_WINDOWS_1900]

    def sheet(name, attrs):
        tag = _local(name)
        if tag == "sheet":
            rid = next((value for key, value in attrs.items() if _local(key) == "id"), None)
            if rid in targets:
                sheets.append(targets[rid])
        elif tag == "workbookPr" and str(attrs.get("date1904", "")).lower() in ("1", "true"):
            epoch[0] = CALENDAR_MAC_1904

    _parse(archive.read("xl/workbook.xml"), sheet)
    return [path for path in sheets if path in names], epoch[0]


def _rows(handle, strings, dates, epoch=CALENDAR_WINDOWS_1900, max_rows=None):
    """Every row of a worksheet as openpyxl's iter_rows(values_only=True)
    gives it: missing rows and cells are None, each row padded to the
    sheet's width."""
    rows = []
    width = [0]
    tags = {}
    row = []
    rnum = 0
    col = -1
    ctype = None
    cstyle = 0
    buf = []
    capture = False

    def tag_of(name):
        tag = tags.get(name)
        if tag is None:
            tag = tags[name] = _local(name)
        return tag

    def start(name, attrs):
        nonlocal row, rnum, col, ctype, cstyle, buf, capture
        tag = tags.get(name) or tag_of(name)
        if tag == "c":
            ref = attrs.get("r")
            if ref:
                n = 0
                for ch in ref:
                    code = ord(ch)
                    if 65 <= code <= 90:
                        n = n * 26 + code - 64
                    elif 97 <= code <= 122:
                        n = n * 26 + code - 96
                    else:
                        break
                col = n - 1
            else:
                col += 1
            ctype = attrs.get("t")
            style = attrs.get("s")
            cstyle = int(style) if style else 0
            buf = []
        elif tag == "v" or (tag == "t" and ctype == "inlineStr"):
            capture = True
        elif tag == "dimension":
            ref = attrs.get("ref", "")
            last = ref.split(":")[-1]
            n = 0
            for ch in last:
                if ch.isalpha():
                    n = n * 26 + ord(ch.upper()) - 64
                else:
                    break
            width[0] = n
        elif tag == "row":
            r = attrs.get("r")
            number = int(r) if r else rnum + 1
            while rnum + 1 < number:
                rows.append([])
                rnum += 1
            rnum = number
            row = []
            col = -1

    def end(name):
        nonlocal row, capture
        tag = tags.get(name) or tag_of(name)
        if tag == "c":
            raw = "".join(buf)
            value = None
            if raw != "":
                if ctype is None or ctype == "n":
                    value = float(raw) if ("." in raw or "e" in raw or "E" in raw) else int(raw)
                    if cstyle in dates:
                        value = from_excel(value, epoch, timedelta=dates[cstyle])
                elif ctype == "s":
                    value = strings[int(raw)]
                elif ctype == "b":
                    value = bool(int(raw))
                elif ctype == "d":
                    value = from_ISO8601(raw)
                else:
                    value = raw
            if col >= len(row):
                row.extend([None] * (col - len(row) + 1))
            row[col] = value
        elif tag == "v" or tag == "t":
            capture = False
        elif tag == "row":
            rows.append(row)
            row = []
            if max_rows and len(rows) >= max_rows:
                raise _Stop

    def text(data):
        if capture:
            buf.append(data)

    parser = expat.ParserCreate()
    parser.buffer_text = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.ParseFile(handle)
    except _Stop:
        pass
    size = max([width[0]] + [len(r) for r in rows]) if rows else 0
    for r in rows:
        if len(r) < size:
            r.extend([None] * (size - len(r)))
    return rows


def read_data_sheet(path, is_data_header):
    """(header, rows) of the first sheet whose header row satisfies
    is_data_header(header_cells), else of the first sheet."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        sheets, epoch = _sheet_paths(archive, names)
        if not sheets:
            raise ValueError("workbook has no worksheets")
        strings = _shared_strings(archive, names)
        dates = _date_styles(archive, names)
        chosen = sheets[0]
        for sheet in sheets:
            with archive.open(sheet) as handle:
                first = _rows(handle, strings, dates, epoch, max_rows=1)
            if first and is_data_header(first[0]):
                chosen = sheet
                break
        with archive.open(chosen) as handle:
            rows = _rows(handle, strings, dates, epoch)
    if not rows:
        return [], []
    return rows[0], rows[1:]
