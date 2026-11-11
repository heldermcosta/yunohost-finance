"""Word positions from a PDF, and OCR of table rows whose text layer is missing.

Some statements draw a row's date and description as a bitmap rather than as
text. `pdftotext -layout` then yields only the amount for that row. Others
print the description on the same visual line as the amount but emit it as a
separate text line, so the layout text separates them. Both cases are
repaired here, using the word positions reported by `pdftotext -bbox`:

- `positional_lines` groups words into visual lines (same height on the page).
- `ocr_crop` reads one rectangle of a page (a row drawn as a bitmap) with tesseract.

The parsers keep working on the `-layout` text; these functions only fill in
what that text leaves out.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .pdftext import PdfPasswordError, PdfTextError

WORD_RE = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>'
)
LINE_TOLERANCE_PT = 3.0


@dataclass(frozen=True)
class PosLine:
    page: int
    top: float
    bottom: float
    # (x_min, x_max, y_min, y_max, text), left to right
    words: tuple[tuple[float, float, float, float, str], ...]

    @property
    def text(self) -> str:
        return " ".join(w[4] for w in self.words)


def positional_lines(path: str | Path, password: str | None = None) -> list[PosLine]:
    """Visual lines of every page, in reading order, with their positions."""
    command = ["pdftotext", "-bbox", "-enc", "UTF-8"]
    if password is not None:
        command += ["-upw", password]
    command += [str(path), "-"]
    try:
        result = subprocess.run(command, capture_output=True, check=True, timeout=60)
    except FileNotFoundError as exc:
        raise PdfTextError("pdftotext is not installed (poppler-utils)") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode(errors="replace").strip()
        if "password" in message.lower() or "encrypt" in message.lower():
            raise PdfPasswordError("PDF is password protected") from exc
        raise PdfTextError(f"pdftotext -bbox failed: {message}") from exc
    return _group_lines(result.stdout.decode("utf-8", errors="replace"))


def _group_lines(bbox_html: str) -> list[PosLine]:
    lines: list[PosLine] = []
    for page_no, page_html in enumerate(bbox_html.split("<page ")[1:], start=1):
        words = []
        for x0, y0, x1, y1, raw in WORD_RE.findall(page_html):
            words.append((float(y0), float(x0), float(x1), float(y1), html.unescape(raw)))
        words.sort()
        current: list[tuple[float, float, float, float, str]] = []
        for word in words:
            if current and abs(word[0] - current[0][0]) > LINE_TOLERANCE_PT:
                lines.append(_make_line(page_no, current))
                current = []
            current.append(word)
        if current:
            lines.append(_make_line(page_no, current))
    return lines


def _make_line(page_no: int, words: list[tuple[float, float, float, float, str]]) -> PosLine:
    ordered = sorted(words, key=lambda w: w[1])
    return PosLine(
        page=page_no,
        top=min(w[0] for w in ordered),
        bottom=max(w[3] for w in ordered),
        words=tuple((w[1], w[2], w[0], w[3], w[4]) for w in ordered),
    )


def ocr_crop(pdf_path: str | Path, page: int, x0: float, x1: float, top: float, bottom: float,
             password: str | None = None, dpi: int = 300) -> str:
    """Read one rectangle of a PDF page with tesseract (Portuguese, then English).

    Coordinates are PDF points, measured from the top-left of the page.
    Returns the recognised text on one line, or "" if nothing was read.
    """
    if shutil.which("tesseract") is None or shutil.which("pdftoppm") is None:
        return ""
    scale = dpi / 72.0
    with tempfile.TemporaryDirectory(prefix="pdfocr-") as tmp:
        prefix = str(Path(tmp) / "crop")
        command = [
            "pdftoppm", "-png", "-singlefile", "-r", str(dpi),
            "-f", str(page), "-l", str(page),
            "-x", str(max(0, int(x0 * scale))), "-y", str(max(0, int(top * scale))),
            "-W", str(max(1, int((x1 - x0) * scale))), "-H", str(max(1, int((bottom - top) * scale))),
        ]
        if password is not None:
            command += ["-upw", password]
        command += [str(pdf_path), prefix]
        try:
            subprocess.run(command, capture_output=True, check=True, timeout=60)
            result = subprocess.run(
                ["tesseract", prefix + ".png", "-", "--psm", "7", "-l", "por+eng"],
                capture_output=True, check=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return ""
    return " ".join(result.stdout.decode("utf-8", errors="replace").split())
