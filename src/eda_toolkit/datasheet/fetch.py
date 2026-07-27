"""Download datasheet PDFs with a content-addressed cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from ..util import EdaError, ensure_dir, human_size
from . import providers

MAX_BYTES = int(os.environ.get("EDA_DATASHEET_MAX_BYTES", str(80 * 1024 * 1024)))


def cache_dir() -> Path:
    return ensure_dir(os.environ.get("EDA_CACHE_DIR", str(Path.home() / ".cache" / "eda-toolkit")))


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return slug[:80] or "datasheet"


def _index_path() -> Path:
    return cache_dir() / "datasheets" / "index.json"


def _load_index() -> dict[str, Any]:
    path = _index_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_index(index: dict[str, Any]) -> None:
    path = _index_path()
    ensure_dir(path.parent)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def download(url: str, *, dest: str | os.PathLike[str] | None = None, force: bool = False) -> dict[str, Any]:
    """Download one URL, verifying it really is a PDF. Returns metadata."""
    index = _load_index()
    cached = index.get(url)
    if cached and not force and Path(cached["path"]).exists() and dest is None:
        cached = dict(cached)
        cached["cached"] = True
        return cached

    headers = {"User-Agent": providers.USER_AGENT, "Accept": "application/pdf,*/*"}
    try:
        resp = requests.get(url, headers=headers, timeout=providers.DEFAULT_TIMEOUT,
                            stream=True, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise EdaError(f"download failed for {url}: {exc}") from exc

    chunks = bytearray()
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > MAX_BYTES:
            raise EdaError(f"datasheet exceeds {human_size(MAX_BYTES)} limit: {url}")
    data = bytes(chunks)

    if not data.startswith(b"%PDF"):
        ctype = resp.headers.get("Content-Type", "?")
        preview = data[:200].decode("utf-8", "replace")
        raise EdaError(
            f"the response from {url} is not a PDF (Content-Type: {ctype}). "
            f"First bytes: {preview!r}"
        )

    digest = hashlib.sha256(data).hexdigest()
    name = _slug(Path(urllib.parse.urlparse(url).path).name or "datasheet")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"

    if dest is not None:
        out_path = Path(dest)
        if out_path.is_dir():
            out_path = out_path / name
        ensure_dir(out_path.parent)
    else:
        out_path = ensure_dir(cache_dir() / "datasheets") / f"{digest[:16]}_{name}"
    out_path.write_bytes(data)

    meta = {
        "url": url,
        "path": str(out_path),
        "bytes": len(data),
        "sha256": digest,
        "content_type": resp.headers.get("Content-Type", ""),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cached": False,
    }
    if dest is None:
        index[url] = meta
        _save_index(index)
    return meta


def fetch_part(
    part: str,
    *,
    dest: str | os.PathLike[str] | None = None,
    limit: int = 5,
    provider_names: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Search for a part and download the best candidate that actually is a PDF."""
    result = providers.search(part, limit=limit, providers=provider_names)
    attempts: list[dict[str, str]] = []
    for cand in result["candidates"]:
        try:
            meta = download(cand["url"], dest=dest, force=force)
        except EdaError as exc:
            attempts.append({"url": cand["url"], "error": str(exc)})
            continue
        meta["candidate"] = cand
        meta["part"] = part
        meta["failed_attempts"] = attempts
        meta["search"] = {"providers": result["providers"], "errors": result["errors"]}
        return meta
    raise EdaError(
        f"no downloadable datasheet found for {part!r}. "
        f"Tried {len(result['candidates'])} candidate(s): "
        + json.dumps(attempts, ensure_ascii=False)
    )
