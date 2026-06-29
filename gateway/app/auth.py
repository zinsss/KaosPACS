from __future__ import annotations

from hmac import compare_digest
from typing import Mapping


AUTH_SCHEME = "Bearer"


def is_auth_enabled(token: str | None) -> bool:
    return bool(token)


def is_authorized(headers: Mapping[str, str], token: str | None) -> bool:
    if not is_auth_enabled(token):
        return True

    authorization = headers.get("Authorization", "")
    scheme, separator, supplied_token = authorization.partition(" ")
    if separator != " " or scheme != AUTH_SCHEME or not supplied_token:
        return False

    return compare_digest(supplied_token, token)
