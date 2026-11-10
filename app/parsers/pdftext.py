"""PDF to layout-preserving text.

Parsers work on the output of `pdftotext -layout` (poppler-utils), because the
column positions in the text are what tell debit, credit and balance apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class PdfTextError(RuntimeError):
    pass


def extract_layout_text(path: str | Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            check=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise PdfTextError("pdftotext is not installed (poppler-utils)") from exc
    except subprocess.CalledProcessError as exc:
        raise PdfTextError(
            f"pdftotext failed: {exc.stderr.decode(errors='replace').strip()}"
        ) from exc
    return result.stdout.decode("utf-8", errors="replace")
