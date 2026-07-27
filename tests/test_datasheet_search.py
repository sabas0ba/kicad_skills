import json

import pytest
import responses

from eda_toolkit.datasheet import providers
from eda_toolkit.util import EdaError


def test_scoring_prefers_exact_part_and_vendor_pdf():
    vendor = providers.Candidate(url="https://www.ti.com/lit/ds/symlink/lm321.pdf",
                                 source="x", title="LM321 datasheet")
    aggregator = providers.Candidate(url="https://www.mouser.com/pdfdocs/other.pdf",
                                     source="x", title="something else")
    noise = providers.Candidate(url="https://pdf1.alldatasheet.com/view/1/LM321.html",
                                source="x")
    assert providers.score_candidate(vendor, "LM321") > providers.score_candidate(aggregator, "LM321")
    assert providers.score_candidate(noise, "LM321") < providers.score_candidate(vendor, "LM321")


def test_duckduckgo_redirect_unwrapping():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.ti.com%2Flit%2Fds%2Flm321.pdf&rut=abc"
    assert providers._unwrap_ddg(wrapped) == "https://www.ti.com/lit/ds/lm321.pdf"
    assert providers._unwrap_ddg("https://example.com/x.pdf") == "https://example.com/x.pdf"


def test_providers_without_credentials_are_skipped(monkeypatch):
    for var in ("MOUSER_API_KEY", "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET",
                "NEXAR_TOKEN", "SEARXNG_URL"):
        monkeypatch.delenv(var, raising=False)
    names = [p.name for p in providers.build_providers()]
    assert names == ["duckduckgo"]

    monkeypatch.setenv("MOUSER_API_KEY", "key")
    assert "mouser" in [p.name for p in providers.build_providers()]


@responses.activate
def test_mouser_provider(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "key")
    responses.add(
        responses.POST,
        providers.MouserProvider.endpoint,
        json={"SearchResults": {"Parts": [
            {"ManufacturerPartNumber": "LM321MF", "Manufacturer": "TI",
             "Description": "Op amp", "DataSheetUrl": "https://ti.com/lm321.pdf"},
            {"ManufacturerPartNumber": "LM321X", "DataSheetUrl": ""},
        ]}},
    )
    found = providers.MouserProvider().search("LM321", 5)
    assert [c.url for c in found] == ["https://ti.com/lm321.pdf"]
    assert found[0].manufacturer == "TI"


@responses.activate
def test_digikey_provider(monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    responses.add(responses.POST, providers.DigikeyProvider.token_url,
                  json={"access_token": "tok"})
    responses.add(responses.POST, providers.DigikeyProvider.search_url,
                  json={"Products": [{"ManufacturerProductNumber": "LM321",
                                      "Manufacturer": {"Name": "TI"},
                                      "Description": {"ProductDescription": "op amp"},
                                      "DatasheetUrl": "//ti.com/lm321.pdf"}]})
    found = providers.DigikeyProvider().search("LM321", 5)
    assert found[0].url == "https://ti.com/lm321.pdf"


@responses.activate
def test_search_merges_providers_and_records_errors(monkeypatch):
    monkeypatch.setenv("MOUSER_API_KEY", "key")
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    responses.add(responses.POST, providers.MouserProvider.endpoint,
                  json={"SearchResults": {"Parts": [
                      {"ManufacturerPartNumber": "LM321", "Manufacturer": "TI",
                       "DataSheetUrl": "https://www.ti.com/lit/ds/lm321.pdf"}]}})
    responses.add(responses.GET, "https://searx.example/search", status=502)
    responses.add(responses.POST, providers.DuckDuckGoProvider.endpoint,
                  body='<a class="result__a" href="https://www.ti.com/lit/ds/lm321.pdf">LM321</a>')

    result = providers.search("LM321", limit=3)
    assert [c["url"] for c in result["candidates"]] == ["https://www.ti.com/lit/ds/lm321.pdf"]
    assert [e["provider"] for e in result["errors"]] == ["searxng"]
    assert "hint" not in result


@responses.activate
def test_search_without_results_returns_a_hint(monkeypatch):
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    responses.add(responses.POST, providers.DuckDuckGoProvider.endpoint, body="<html></html>")
    result = providers.search("NOSUCHPART", limit=3)
    assert result["candidates"] == []
    assert "EDA_NETWORK" in result["hint"]


@responses.activate
def test_duckduckgo_network_failure_is_surfaced(monkeypatch):
    monkeypatch.delenv("MOUSER_API_KEY", raising=False)
    responses.add(responses.POST, providers.DuckDuckGoProvider.endpoint, status=403)
    result = providers.search("LM321", limit=3)
    assert [e["provider"] for e in result["errors"]] == ["duckduckgo"]
    assert result["candidates"] == []


def test_search_without_any_provider(monkeypatch):
    monkeypatch.setattr(providers, "ALL_PROVIDERS", ())
    with pytest.raises(EdaError):
        providers.search("LM321")
