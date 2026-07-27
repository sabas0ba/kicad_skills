"""Datasheet search providers.

Every provider turns a part number into candidate PDF URLs.  Providers that need
credentials are silently skipped when the credentials are absent, so the chain
degrades to plain web search.

Credentials are read from the environment (pass them through with
``EDA_ENV_PASSTHROUGH`` when using ``bin/eda``):

* ``MOUSER_API_KEY``                        - Mouser search API
* ``DIGIKEY_CLIENT_ID`` / ``DIGIKEY_CLIENT_SECRET`` - Digi-Key product search v4
* ``NEXAR_TOKEN``                           - Nexar/Octopart GraphQL (bearer token)
* ``SEARXNG_URL``                           - self-hosted SearXNG instance (JSON API)
"""

from __future__ import annotations

import dataclasses
import os
import re
import urllib.parse
from typing import Any, Iterable

import requests

from ..util import EdaError

USER_AGENT = os.environ.get(
    "EDA_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 eda-toolkit/0.1",
)
DEFAULT_TIMEOUT = int(os.environ.get("EDA_HTTP_TIMEOUT", "30"))


@dataclasses.dataclass
class Candidate:
    """A possible datasheet for a part."""

    url: str
    source: str
    title: str = ""
    manufacturer: str = ""
    part_number: str = ""
    description: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def _looks_like_pdf_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(".pdf") or "datasheet" in url.lower() or "/ds/" in path


def score_candidate(cand: Candidate, part: str) -> float:
    """Heuristic ranking: exact part token in URL/title, .pdf suffix, known hosts."""
    score = 0.0
    part_l = part.lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", part_l) if len(t) >= 3]
    url_l = cand.url.lower()
    title_l = (cand.title + " " + cand.description + " " + cand.part_number).lower()

    if part_l in url_l:
        score += 3.0
    if part_l in title_l:
        score += 2.0
    score += sum(1.0 for t in tokens if t in url_l)
    score += sum(0.5 for t in tokens if t in title_l)
    if urllib.parse.urlparse(cand.url).path.lower().endswith(".pdf"):
        score += 2.0
    host = urllib.parse.urlparse(cand.url).netloc.lower()
    if any(h in host for h in _VENDOR_HOSTS):
        score += 2.5
    if any(h in host for h in _AGGREGATOR_HOSTS):
        score += 1.0
    if any(h in host for h in _NOISE_HOSTS):
        score -= 3.0
    return score


_VENDOR_HOSTS = (
    "ti.com", "analog.com", "st.com", "nxp.com", "infineon.com", "microchip.com",
    "onsemi.com", "renesas.com", "rohm.com", "toshiba.com", "diodes.com",
    "vishay.com", "murata.com", "tdk.com", "nichicon.com", "panasonic.com",
    "skyworksinc.com", "maximintegrated.com", "silabs.com", "espressif.com",
    "nordicsemi.com", "cirrus.com", "monolithicpower.com", "richtek.com",
    "littelfuse.com", "bourns.com", "kemet.com", "samsung-semi.com", "yageo.com",
    "semtech.com", "melexis.com", "sensirion.com", "bosch-sensortec.com",
)
_AGGREGATOR_HOSTS = (
    "mouser.com", "digikey.com", "farnell.com", "element14.com", "rs-online.com",
    "lcsc.com", "arrow.com", "octopart.com", "datasheets.com",
)
_NOISE_HOSTS = ("pdf1.alldatasheet", "datasheetspdf.com", "pinterest.", "scribd.com")


class Provider:
    name = "provider"

    def available(self) -> bool:
        return True

    def search(self, part: str, limit: int) -> list[Candidate]:  # pragma: no cover - interface
        raise NotImplementedError


class MouserProvider(Provider):
    name = "mouser"
    endpoint = "https://api.mouser.com/api/v1/search/keyword"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("MOUSER_API_KEY", "")

    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, part: str, limit: int) -> list[Candidate]:
        body = {"SearchByKeywordRequest": {"keyword": part, "records": max(limit, 5), "startingRecord": 0}}
        resp = _session().post(
            self.endpoint, params={"apiKey": self.api_key}, json=body, timeout=DEFAULT_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        parts = (data.get("SearchResults") or {}).get("Parts") or []
        out = []
        for p in parts:
            url = p.get("DataSheetUrl") or ""
            if not url:
                continue
            out.append(
                Candidate(
                    url=url,
                    source=self.name,
                    title=p.get("Description", ""),
                    manufacturer=p.get("Manufacturer", ""),
                    part_number=p.get("ManufacturerPartNumber", ""),
                    description=p.get("Category", ""),
                )
            )
        return out


class DigikeyProvider(Provider):
    name = "digikey"
    token_url = "https://api.digikey.com/v1/oauth2/token"
    search_url = "https://api.digikey.com/products/v4/search/keyword"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None) -> None:
        self.client_id = client_id or os.environ.get("DIGIKEY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("DIGIKEY_CLIENT_SECRET", "")

    def available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _token(self, sess: requests.Session) -> str:
        resp = sess.post(
            self.token_url,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def search(self, part: str, limit: int) -> list[Candidate]:
        sess = _session()
        token = self._token(sess)
        resp = sess.post(
            self.search_url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": self.client_id,
                "X-DIGIKEY-Locale-Site": os.environ.get("DIGIKEY_SITE", "US"),
                "X-DIGIKEY-Locale-Language": "en",
            },
            json={"Keywords": part, "Limit": max(limit, 5), "Offset": 0},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        out = []
        for p in resp.json().get("Products", []):
            url = p.get("DatasheetUrl") or ""
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            out.append(
                Candidate(
                    url=url,
                    source=self.name,
                    title=p.get("Description", {}).get("ProductDescription", ""),
                    manufacturer=(p.get("Manufacturer") or {}).get("Name", ""),
                    part_number=p.get("ManufacturerProductNumber", ""),
                )
            )
        return out


class NexarProvider(Provider):
    """Octopart/Nexar GraphQL. Needs a pre-issued bearer token in NEXAR_TOKEN."""

    name = "nexar"
    endpoint = "https://api.nexar.com/graphql"
    query = """
    query ($q: String!, $limit: Int!) {
      supSearchMpn(q: $q, limit: $limit) {
        results { part { mpn manufacturer { name } shortDescription
                        bestDatasheet { url name } } }
      }
    }
    """

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("NEXAR_TOKEN", "")

    def available(self) -> bool:
        return bool(self.token)

    def search(self, part: str, limit: int) -> list[Candidate]:
        resp = _session().post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"query": self.query, "variables": {"q": part, "limit": max(limit, 5)}},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        results = (
            ((resp.json().get("data") or {}).get("supSearchMpn") or {}).get("results") or []
        )
        out = []
        for r in results:
            p = r.get("part") or {}
            ds = p.get("bestDatasheet") or {}
            if not ds.get("url"):
                continue
            out.append(
                Candidate(
                    url=ds["url"],
                    source=self.name,
                    title=ds.get("name") or p.get("shortDescription", ""),
                    manufacturer=(p.get("manufacturer") or {}).get("name", ""),
                    part_number=p.get("mpn", ""),
                )
            )
        return out


class SearxngProvider(Provider):
    """Self-hosted SearXNG instance - the privacy friendly way to web search."""

    name = "searxng"

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get("SEARXNG_URL", "")).rstrip("/")

    def available(self) -> bool:
        return bool(self.base_url)

    def search(self, part: str, limit: int) -> list[Candidate]:
        resp = _session().get(
            f"{self.base_url}/search",
            params={"q": f"{part} datasheet filetype:pdf", "format": "json"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        out = []
        for r in resp.json().get("results", [])[: max(limit * 3, 15)]:
            url = r.get("url", "")
            if url:
                out.append(
                    Candidate(url=url, source=self.name, title=r.get("title", ""),
                              description=r.get("content", ""))
                )
        return out


class DuckDuckGoProvider(Provider):
    """HTML endpoint of DuckDuckGo - no API key, but rate limited and brittle."""

    name = "duckduckgo"
    endpoint = "https://html.duckduckgo.com/html/"

    def search(self, part: str, limit: int) -> list[Candidate]:
        from bs4 import BeautifulSoup

        queries = [f"{part} datasheet filetype:pdf", f'"{part}" datasheet pdf']
        seen: set[str] = set()
        out: list[Candidate] = []
        sess = _session()
        failures: list[str] = []
        for q in queries:
            try:
                resp = sess.post(self.endpoint, data={"q": q}, timeout=DEFAULT_TIMEOUT)
                resp.raise_for_status()
            except requests.RequestException as exc:
                failures.append(str(exc))
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a.result__a, a.result__url, a[href]"):
                href = a.get("href") or ""
                url = _unwrap_ddg(href)
                if not url.startswith("http") or url in seen:
                    continue
                if not _looks_like_pdf_url(url):
                    continue
                seen.add(url)
                out.append(Candidate(url=url, source=self.name, title=a.get_text(strip=True)))
            if len(out) >= limit * 3:
                break
        if not out and len(failures) == len(queries):
            # every request failed - report it instead of pretending there were
            # simply no results (the usual cause is a blocked container network)
            raise requests.RequestException(failures[0])
        return out


def _unwrap_ddg(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            return urllib.parse.unquote(qs["uddg"][0])
    return href


ALL_PROVIDERS: tuple[type[Provider], ...] = (
    MouserProvider,
    DigikeyProvider,
    NexarProvider,
    SearxngProvider,
    DuckDuckGoProvider,
)


def build_providers(names: Iterable[str] | None = None) -> list[Provider]:
    wanted = {n.strip().lower() for n in names} if names else None
    providers: list[Provider] = []
    for cls in ALL_PROVIDERS:
        if wanted and cls.name not in wanted:
            continue
        inst = cls()
        if inst.available():
            providers.append(inst)
    return providers


def search(part: str, limit: int = 5, providers: Iterable[str] | None = None) -> dict[str, Any]:
    """Query every available provider and return ranked, de-duplicated candidates."""
    chain = build_providers(providers)
    if not chain:
        raise EdaError(
            "no datasheet search provider is available. Set MOUSER_API_KEY / "
            "DIGIKEY_CLIENT_ID+DIGIKEY_CLIENT_SECRET / NEXAR_TOKEN / SEARXNG_URL, "
            "or allow the duckduckgo fallback."
        )
    candidates: dict[str, Candidate] = {}
    errors: list[dict[str, str]] = []
    for provider in chain:
        try:
            found = provider.search(part, limit)
        except Exception as exc:  # provider failures must not kill the search
            errors.append({"provider": provider.name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for cand in found:
            cand.score = score_candidate(cand, part)
            existing = candidates.get(cand.url)
            if existing is None or cand.score > existing.score:
                candidates[cand.url] = cand
    ranked = sorted(candidates.values(), key=lambda c: c.score, reverse=True)[:limit]
    result = {
        "part": part,
        "providers": [p.name for p in chain],
        "errors": errors,
        "candidates": [c.to_dict() for c in ranked],
    }
    if not ranked:
        result["hint"] = (
            "no candidate found. Check that the container has network access "
            "(bin/eda enables it only for 'datasheet search|fetch', override with "
            "EDA_NETWORK=1), that any proxy is exported, or supply a distributor API "
            "key (MOUSER_API_KEY / DIGIKEY_CLIENT_ID+SECRET / NEXAR_TOKEN / SEARXNG_URL). "
            "You can always pass a known URL: eda datasheet fetch --url <pdf-url>."
        )
    return result
