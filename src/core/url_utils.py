"""
URL normalization helpers used by service configs.
"""

from typing import Any
from urllib.parse import urlparse, urlunparse


def normalize_base_url(raw_url: Any, default_scheme: str = "https") -> str:
    """
    Normalize a user-provided base URL.

    - trims surrounding whitespace
    - prepends a default scheme when missing
    - removes a trailing slash

    Raises:
        ValueError: when the URL still does not contain a valid scheme/host
    """
    text = str(raw_url or "").strip()
    if not text:
        return ""

    scheme = str(default_scheme or "https").strip() or "https"
    if "://" not in text:
        text = f"{scheme}://{text}"

    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"无效的 base_url: {raw_url!r}")

    normalized = parsed._replace(
        path=(parsed.path or "").rstrip("/"),
        params="",
        fragment="",
    )
    return urlunparse(normalized).rstrip("/")
