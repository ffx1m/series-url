import re
from urllib.parse import urlparse

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9ก-๙._ -]{1,120}$")


def validate_name(value, label):
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label} is required")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{label} contains invalid path characters")
    if not SAFE_NAME_RE.match(value):
        raise ValueError(f"{label} contains unsupported characters")
    return value


def validate_http_url(value):
    value = (value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be http or https")
    return value
