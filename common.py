#!/usr/bin/env python3
"""
Shared helpers for the finance pipeline.

Single source of truth for the bits that were previously copy-pasted across
parse.py / categorize.py / reconcile.py / app.py:
  * project paths (BASE / DOWNLOADS / DATA / RULES)
  * the canonical category list and the non-spend set
  * pdftext() (Poppler primary, PyMuPDF/pypdf fallback), money() (money-string -> float)
  * load_json() (read-a-JSON-or-default)

Import from here rather than redefining, so the pipeline stays consistent.
"""
import json
import os
import subprocess
import sys
import tempfile
import hashlib
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath

MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 15)
if not MIN_PYTHON <= sys.version_info[:2] < MAX_PYTHON:
    running = ".".join(map(str, sys.version_info[:3]))
    raise RuntimeError(
        f"Ka-ching requires Python 3.10 through 3.14; running Python {running}. "
        "See GETTING_STARTED.md for the isolated virtual-environment setup."
    )

BASE = Path(__file__).resolve().parent
# Statement source folders (ChaseBank/CSP/VX/Venmo) live INSIDE the repo so the
# whole project is self-contained and travels with the code. Historically these
# sat one level up (BASE.parent); they were moved in and this repointed to BASE.
DOWNLOADS = BASE
IMPORTS = BASE / "imports"
DATA = BASE / "data"
RULES = BASE / "rules"
DEFAULT_SOURCE_FOLDERS = {
    "chase": "ChaseBank",
    "csp": "CSP",
    "vx": "VX",
    "venmo": "Venmo",
}

# The canonical, ordered category list. The dashboard renders these and every
# override endpoint validates against them. One definition, imported elsewhere.
CATEGORIES = [
    "Dining", "Coffee/Boba/Bakery", "Groceries", "Travel", "Transportation",
    "Shopping", "Entertainment", "Fees",
    "Cash & ATM", "Miscellaneous", "Savings & Investing", "Income", "Transfers",
]
# Categories that do NOT count toward discretionary "spending" totals (and are
# kept out of trip totals too).
NON_SPEND = {"Income", "Transfers", "Savings & Investing"}


def _pdftext_pymupdf(path) -> str:
    """Extract sorted text through the PyMuPDF fallback."""
    import pymupdf as fitz

    def page_text(page):
        # ``get_text(..., sort=True)`` still follows PDF text blocks, which can
        # place an end-of-section marker between words from a table row. Rebuild
        # physical lines from word coordinates so statement columns and section
        # boundaries retain their visual reading order.
        words = sorted(page.get_text("words"), key=lambda word: (word[1], word[0]))
        lines = []
        for word in words:
            if not lines or abs(word[1] - lines[-1][0]) > 2:
                lines.append([word[1], [word]])
            else:
                lines[-1][1].append(word)
        rendered = []
        for _, line in lines:
            line.sort(key=lambda word: word[0])
            parts, previous = [], None
            for word in line:
                if previous is None:
                    parts.append(word[4])
                else:
                    gap = word[0] - previous[2]
                    # Preserve a visible column gap without emitting unbounded
                    # whitespace on large-format statement pages.
                    parts.append(" " * max(1, min(40, round(gap / 3.5))) + word[4])
                previous = word
            rendered.append("".join(parts))
        return "\n".join(rendered)

    try:
        document = fitz.open(path)
        try:
            text = "\n\f\n".join(page_text(page) for page in document)
        finally:
            document.close()
    except Exception as exc:
        raise ValueError(f"PyMuPDF could not read {path}") from exc
    if not text.strip():
        raise ValueError(f"PyMuPDF produced no text for {path}")
    return text


def _pdftext_pypdf(path) -> str:
    """Extract readable text through the final pure-Python fallback."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        pages = []
        for page in reader.pages:
            try:
                page_text = page.extract_text(extraction_mode="layout")
            except Exception:
                # Some statement fonts (notably SymbolEncoding) are unsupported
                # by pypdf's layout mode even though plain extraction succeeds.
                # Fall back one page at a time so a decorative page cannot hide
                # the transaction pages behind it.
                page_text = page.extract_text()
            pages.append(page_text or "")
        text = "\n\f\n".join(pages)
    except Exception as exc:
        raise ValueError(f"pypdf could not read {path}") from exc
    if not text.strip():
        raise ValueError(f"pypdf produced no text for {path}")
    return text


def _pdftext_fallback(path) -> str:
    """Use the in-process extractors in preferred order."""
    try:
        return _pdftext_pymupdf(path)
    except (ImportError, ValueError):
        return _pdftext_pypdf(path)


@lru_cache(maxsize=1)
def _pdftotext_available():
    """Return whether the optional Poppler executable can actually start."""
    try:
        result = subprocess.run(
            ["pdftotext", "-v"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pdftext(path) -> str:
    """Extract layout-preserving text, using Poppler then a Python fallback.

    Poppler is the primary extractor because its output is the most stable for
    the existing PDF adapters and templates. If Poppler is not installed,
    PyMuPDF provides a local in-process extractor; pypdf is a final fallback
    for documents PyMuPDF cannot open. A failed Poppler run still propagates:
    retrying corrupted or encrypted PDFs with another extractor can hide a real
    import problem.
    """
    if not _pdftotext_available():
        return _pdftext_fallback(path)
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=120,
        )
    except OSError:
        # Missing, non-executable, or otherwise broken local installations
        # should not disable the in-process extractors.
        return _pdftext_fallback(path)
    if out.returncode:
        raise subprocess.CalledProcessError(
            out.returncode, out.args, output=out.stdout, stderr=out.stderr,
        )
    if not out.stdout.strip():
        try:
            return _pdftext_fallback(path)
        except (ImportError, ValueError) as exc:
            raise ValueError(
                f"pdftotext produced no text for {path}; this may be a scanned PDF. "
                "OCR the file locally, then import the OCRed copy."
            ) from exc
    return out.stdout


def money(s: str) -> float:
    """'$1,299.38' / '- $5.00' -> float (drops $, commas, spaces; keeps sign)."""
    return float(s.replace("$", "").replace(",", "").replace(" ", ""))


def file_digest(path, chunk_size=1024 * 1024):
    """Return a SHA-256 digest without loading a source document into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class JsonFileError(ValueError):
    """A JSON file exists but cannot safely be read."""


def source_folders():
    """Return configured source directories, relative to the project root.

    ``rules/sources.json`` may override the directory for any built-in parser,
    for example ``{"chase": "Checking", "csp": "Sapphire"}``. Parser types
    remain format-specific; changing a folder name does not make an unrelated
    bank's statement compatible.
    """
    overrides = load_json(RULES / "sources.json", {})
    if not isinstance(overrides, dict):
        raise JsonFileError("rules/sources.json must be an object")
    unknown = set(overrides) - set(DEFAULT_SOURCE_FOLDERS)
    if unknown:
        names = ", ".join(repr(name) for name in sorted(unknown))
        raise JsonFileError(f"rules/sources.json has unsupported source key(s): {names}")
    folders = dict(DEFAULT_SOURCE_FOLDERS)
    for source in folders:
        value = overrides.get(source, folders[source])
        if not isinstance(value, str) or not value.strip():
            raise JsonFileError(f"rules/sources.json has an invalid {source!r} folder")
        raw_folder = value.strip()
        windows_folder = PureWindowsPath(raw_folder)
        folder = PurePosixPath(raw_folder.replace("\\", "/"))
        if (
            windows_folder.drive
            or windows_folder.root
            or folder.root
            or folder == PurePosixPath(".")
            or ".." in folder.parts
        ):
            raise JsonFileError(f"rules/sources.json folder for {source!r} must be relative")
        folders[source] = folder.as_posix()
    if len({folder.casefold() for folder in folders.values()}) != len(folders):
        raise JsonFileError("rules/sources.json folders must be distinct")
    return folders


def load_json(p, default):
    """Read JSON from path p, returning default only when the file is missing.

    Existing malformed content is deliberately an error, not an empty overlay:
    treating it as ``default`` lets the next mutation overwrite the only copy of
    a user's rules. ``write_json`` makes corruption unlikely, but preserves
    existing data when it does occur so the file can be repaired or restored."""
    p = Path(p)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as exc:
        raise JsonFileError(f"Unable to read JSON file: {p}") from exc
    try:
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JsonFileError(f"Invalid JSON in {p}") from exc


def write_json(p, obj):
    """Atomically write obj as pretty JSON to path p.

    Writes to a temp file in the same directory, then os.replace()s it into
    place on the same filesystem, so a reader never sees a half-written file and
    an interrupted write can't corrupt the existing one (write_text truncates
    first, which is exactly the failure mode this avoids)."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(obj, indent=2))
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_bytes(p, content):
    """Atomically replace ``p`` with bytes, preserving the old file on failure."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
