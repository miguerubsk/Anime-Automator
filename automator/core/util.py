"""Utilidades de texto y URLs sin dependencias del dominio."""

import re
import urllib.parse

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def compress_ranges(numbers) -> str:
    """[1,2,3,5,7,8] -> '1-3, 5, 7-8'"""
    nums = sorted(set(numbers))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"{nums[i]}" if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} min"
    hours = seconds / 3600
    return f"{hours:.1f} h".replace(".0 h", " h")


def safe_name(name: str) -> str:
    """Solo caracteres seguros para nombres de archivo temporales."""
    return _UNSAFE_RE.sub("_", name)


def url_host(url) -> str:
    try:
        return (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_japanpaw_url(url: str) -> bool:
    host = url_host(url)
    return host == "japan-paw.net" or host.endswith(".japan-paw.net")
