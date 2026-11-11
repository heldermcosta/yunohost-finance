"""PDF to layout-preserving text.

Parsers work on the output of `pdftotext -layout` (poppler-utils), because the
column positions in the text are what tell debit, credit and balance apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class PdfTextError(RuntimeError):
    pass


class PdfPasswordError(PdfTextError):
    """The PDF is protected, and the password given (or none) did not open it."""


def extract_layout_text(path: str | Path, password: str | None = None) -> str:
    command = ["pdftotext", "-layout", "-enc", "UTF-8"]
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
        raise PdfTextError(f"pdftotext failed: {message}") from exc
    return result.stdout.decode("utf-8", errors="replace")
