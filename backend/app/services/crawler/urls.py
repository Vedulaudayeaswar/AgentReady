"""URL validation and normalization."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(r"^(utm_.*|gclid|fbclid|msclkid|mc_cid|mc_eid|ref|ref_src|_ga|yclid)$", re.I)
SKIP_EXTENSIONS = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|bmp|tiff|css|js|mjs|map|woff2?|ttf|eot|otf|mp4|mp3|wav|avi|mov|webm|"
    r"zip|gz|tar|rar|7z|exe|dmg|apk|iso|pdf|docx?|xlsx?|pptx?|csv)$",
    re.I,
)


class InvalidURL(ValueError):
    pass


def validate_public_url_syntax(raw: str) -> str:
    """Syntax-level validation of a merchant-supplied website URL."""
    raw = (raw or "").strip()
    if not raw:
        raise InvalidURL("URL is required")
    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(?!\d)", raw)
    if scheme_match and scheme_match.group(1).lower() not in ("http", "https"):
        raise InvalidURL("Only http and https URLs are supported")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise InvalidURL("Only http and https URLs are supported")
    try:
        parts.port
    except ValueError:
        raise InvalidURL("Invalid port")
    if not parts.hostname:
        raise InvalidURL("URL must include a hostname")
    if parts.username or parts.password:
        raise InvalidURL("URLs with embedded credentials are not allowed")
    host = parts.hostname
    if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.\-\[\]:]+", host):
        raise InvalidURL("Hostname contains invalid characters")
    return normalize_url(raw)


def normalize_url(url: str, base: str | None = None) -> str:
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    netloc = f"[{host}]" if ":" in host else host  # IPv6 literals need brackets
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def site_key(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, root: str) -> bool:
    a, b = urlsplit(url).hostname or "", urlsplit(root).hostname or ""
    return site_key(a) == site_key(b)


def is_crawlable_link(href: str) -> bool:
    if not href:
        return False
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:", "sms:")):
        return False
    path = urlsplit(href).path
    return not SKIP_EXTENSIONS.search(path)
