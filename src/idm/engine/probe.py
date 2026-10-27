"""Probe a URL: learn its size, whether it supports byte ranges, and a filename."""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

import aiohttp


@dataclass
class ProbeResult:
    url: str
    final_url: str
    size: int | None
    resumable: bool
    filename: str
    content_type: str
    etag: str | None = None
    last_modified: str | None = None


_SAFE_NAME = re.compile(r'[\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    name = _SAFE_NAME.sub("_", name).strip(" .")
    return name or "download"


def filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    msg = Message()
    msg["content-disposition"] = value
    name = msg.get_filename()
    if name:
        return sanitize_filename(unquote(name))
    return None


def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = PurePosixPath(unquote(path)).name
    return sanitize_filename(name) if name else "download"


def _parse_content_range(value: str | None) -> int | None:
    # "bytes 0-0/12345"
    if not value:
        return None
    m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", value)
    if not m or m.group(1) == "*":
        return None
    return int(m.group(1))


async def probe(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> ProbeResult:
    """Issue a 1-byte ranged GET. A 206 reply proves range support and gives the total size."""
    req_headers = dict(headers or {})
    req_headers["Range"] = "bytes=0-0"
    async with session.get(url, headers=req_headers, allow_redirects=True, proxy=proxy or None) as resp:
        resp.raise_for_status()
        final_url = str(resp.url)
        content_type = resp.headers.get("Content-Type", "")
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        filename = filename_from_disposition(resp.headers.get("Content-Disposition")) or filename_from_url(final_url)

        if resp.status == 206:
            size = _parse_content_range(resp.headers.get("Content-Range"))
            resumable = True
        else:
            # Server ignored the Range header. Content-Length (if any) is the full size.
            cl = resp.headers.get("Content-Length")
            size = int(cl) if cl and cl.isdigit() else None
            resumable = resp.headers.get("Accept-Ranges", "").lower() == "bytes" and size is not None
        # Don't read the body; the context manager closes the connection.

    return ProbeResult(
        url=url,
        final_url=final_url,
        size=size,
        resumable=resumable,
        filename=filename,
        content_type=content_type,
        etag=etag,
        last_modified=last_modified,
    )
