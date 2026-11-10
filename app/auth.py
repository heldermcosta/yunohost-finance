"""Identify the signed-in user from the YunoHost SSO header.

SSOwat (YunoHost's login proxy) sets `Authorization: Basic base64(username:password)` on
requests to a protected app, with the password "-" unless the permission asks for a real
one. The app only uses the username. It must only be reachable through nginx/SSOwat;
never expose the app port directly, because anyone could send this header.
"""

from __future__ import annotations

import base64
import binascii


def username_from_authorization(header_value: str | None) -> str | None:
    if not header_value or not header_value.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header_value[len("Basic "):], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, _, _password = decoded.partition(":")
    username = username.strip()
    return username or None
