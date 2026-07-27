import json

import pytest
import responses

from eda_toolkit.datasheet import fetch
from eda_toolkit.util import EdaError

PDF_BYTES = b"%PDF-1.4\n%fake pdf for tests\n"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("EDA_CACHE_DIR", str(tmp_path / "cache"))
    yield


@responses.activate
def test_download_writes_and_caches():
    responses.add(responses.GET, "https://ti.com/lm321.pdf", body=PDF_BYTES,
                  content_type="application/pdf")
    meta = fetch.download("https://ti.com/lm321.pdf")
    assert meta["cached"] is False
    assert meta["bytes"] == len(PDF_BYTES)
    assert open(meta["path"], "rb").read() == PDF_BYTES

    again = fetch.download("https://ti.com/lm321.pdf")
    assert again["cached"] is True
    assert again["path"] == meta["path"]
    assert len(responses.calls) == 1  # served from the cache


@responses.activate
def test_download_to_a_directory(tmp_path):
    responses.add(responses.GET, "https://ti.com/lm321.pdf", body=PDF_BYTES)
    meta = fetch.download("https://ti.com/lm321.pdf", dest=tmp_path)
    assert meta["path"].endswith("lm321.pdf")
    assert (tmp_path / "lm321.pdf").exists()


@responses.activate
def test_html_error_page_is_rejected():
    responses.add(responses.GET, "https://example.com/x.pdf",
                  body=b"<html><body>404 not found</body></html>",
                  content_type="text/html")
    with pytest.raises(EdaError, match="not a PDF"):
        fetch.download("https://example.com/x.pdf")


@responses.activate
def test_http_error_is_reported():
    responses.add(responses.GET, "https://example.com/x.pdf", status=500)
    with pytest.raises(EdaError, match="download failed"):
        fetch.download("https://example.com/x.pdf")


@responses.activate
def test_size_limit(monkeypatch):
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    responses.add(responses.GET, "https://example.com/x.pdf", body=PDF_BYTES * 10)
    with pytest.raises(EdaError, match="exceeds"):
        fetch.download("https://example.com/x.pdf")


@responses.activate
def test_fetch_part_falls_through_to_the_next_candidate(monkeypatch):
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)
    # the highest ranked candidate (vendor host, exact part) serves an error page
    responses.add(
        responses.POST,
        "https://html.duckduckgo.com/html/",
        body=(
            '<a class="result__a" href="https://www.ti.com/lit/ds/lm321.pdf">LM321</a>'
            '<a class="result__a" href="https://www.mouser.com/datasheet/lm321.pdf">LM321</a>'
        ),
    )
    responses.add(responses.GET, "https://www.ti.com/lit/ds/lm321.pdf", body=b"<html>nope</html>")
    responses.add(responses.GET, "https://www.mouser.com/datasheet/lm321.pdf", body=PDF_BYTES)

    meta = fetch.fetch_part("LM321")
    assert meta["url"] == "https://www.mouser.com/datasheet/lm321.pdf"
    assert len(meta["failed_attempts"]) == 1
    assert meta["part"] == "LM321"


@responses.activate
def test_fetch_part_without_any_downloadable_candidate(monkeypatch):
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)
    responses.add(responses.POST, "https://html.duckduckgo.com/html/",
                  body='<a class="result__a" href="https://bad.example/lm321.pdf">x</a>')
    responses.add(responses.GET, "https://bad.example/lm321.pdf", body=b"nope")
    with pytest.raises(EdaError, match="no downloadable datasheet"):
        fetch.fetch_part("LM321")
