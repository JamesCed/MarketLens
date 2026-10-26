"""
app/utils/upload_checks.py
----------------------------
Looks INSIDE an uploaded file before anything reads it.

The upload form only accepts .xlsx, .xls and .csv names, but a name is a
label anyone can type. These checks confirm the bytes match the label --
an .xlsx is really a workbook, an .xls really an Excel 97-2003 file, a
.csv really text -- and refuse the things a spreadsheet upload never
needs: macros, executables, PDFs or web pages renamed to .csv, and
compressed "zip bombs" that unpack to gigabytes.

Each check returns None when the file is acceptable, or a message for the
uploader explaining what is wrong.
"""

import os
import zipfile

ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FOREIGN_SIGNATURES = (
    (b"MZ", "a Windows program"),
    (b"\x7fELF", "a program"),
    (b"%PDF", "a PDF document"),
    (ZIP_MAGIC, "a compressed (ZIP or Excel) file"),
    (OLE_MAGIC, "an Office document"),
    (b"\x89PNG", "an image"),
    (b"\xff\xd8\xff", "an image"),
    (b"GIF8", "an image"),
)
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_COMPRESSION_RATIO = 150
MAX_ZIP_ENTRIES = 2000


def check_upload_content(path):
    """None if the file's contents match its extension, else why not."""
    extension = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536)
    except OSError:
        return "The uploaded file could not be read. Please upload it again."
    if not head:
        return "The uploaded file is empty."
    if extension in (".xlsx", ".xlsm"):
        return _check_xlsx(path, head)
    if extension == ".xls":
        if not head.startswith(OLE_MAGIC):
            return ("This file is named .xls but is not an Excel 97-2003 workbook. Open it in Excel and "
                    "save it as .xlsx or CSV.")
        return None
    if extension == ".csv":
        return _check_csv(head)
    return "Only Excel (.xlsx, .xls) or CSV (.csv) files are accepted."


def _check_xlsx(path, head):
    if head.startswith(OLE_MAGIC):
        return ("This workbook is password-protected (encrypted). Remove the password in Excel "
                "(File > Info > Protect Workbook) and upload it again.")
    if not head.startswith(ZIP_MAGIC) or not zipfile.is_zipfile(path):
        return "This file is named .xlsx but is not an Excel workbook. Save it from Excel as .xlsx or CSV."
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError):
        return "This Excel file is damaged and cannot be opened. Save it again from Excel and retry."
    names = {entry.filename for entry in entries}
    if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
        return "This file is named .xlsx but is not an Excel workbook. Save it from Excel as .xlsx or CSV."
    if any(name.lower().endswith("vbaproject.bin") for name in names):
        return ("This workbook contains macros, which are not accepted. Save it as a plain Excel "
                "Workbook (.xlsx) or as CSV and upload that file.")
    if len(entries) > MAX_ZIP_ENTRIES:
        return "This workbook has an unusual internal structure and was refused. Save it as CSV and upload that."
    total = sum(entry.file_size for entry in entries)
    packed = sum(entry.compress_size for entry in entries) or 1
    if total > MAX_UNCOMPRESSED_BYTES or total / packed > MAX_COMPRESSION_RATIO:
        return ("This workbook expands to far more data than a register needs and was refused. "
                "Save the data sheet as CSV and upload that instead.")
    return None


def _check_csv(head):
    for signature, what in _FOREIGN_SIGNATURES:
        if head.startswith(signature):
            return f"This file is named .csv but is actually {what}. Upload a real CSV (comma-separated text) file."
    lowered = head[:512].lstrip().lower()
    if lowered.startswith((b"<!doctype", b"<html", b"<?xml", b"<script")):
        return "This file is named .csv but is a web page or XML document. Upload a real CSV file."
    if b"\x00" in head:
        return ("This file is named .csv but contains binary data. If it came from Excel, use "
                "File > Save As > CSV UTF-8 and upload that file.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = head.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return "This CSV file's text could not be read. Save it as 'CSV UTF-8' and upload it again."
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if "," not in first_line and ";" not in first_line and "\t" not in first_line:
        return ("The first line of this CSV file is not a row of column headers separated by commas. "
                "Download the template to see the expected layout.")
    return None
