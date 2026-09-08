"""Deterministic M6.3 temporal-intent and freshness-gate contracts."""

from __future__ import annotations

import json

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from tieru.tools import search


def _rows(monkeypatch, rows):
    monkeypatch.setattr(search, "_current_year", lambda: 2026)
    monkeypatch.setattr(search, "_search_once", lambda _query, _limit: (rows, "test"))


def test_current_ubuntu_rejects_2204_when_newer_evidence_exists(monkeypatch):
    _rows(
        monkeypatch,
        [
            (
                "Ubuntu 22.04.5 LTS",
                "Ubuntu LTS release from 2022.",
                "https://releases.example/ubuntu/22.04/",
            ),
            (
                "Ubuntu 26.04 LTS",
                "Current Ubuntu LTS release for 2026.",
                "https://releases.example/ubuntu/26.04/",
            ),
        ],
    )

    payload = json.loads(search.web_search("current Ubuntu LTS"))

    assert [item["url"] for item in payload["results"]] == [
        "https://releases.example/ubuntu/26.04/"
    ]
    assert "22.04" not in json.dumps(payload["results"])


def test_stale_year_is_downgraded_for_current_query(monkeypatch):
    monkeypatch.setattr(search, "_current_year", lambda: 2026)

    freshness = search._freshness(
        "latest Acme release",
        "Acme 2022 release",
        "Version published in 2022.",
        "https://acme.example/releases/2022/",
        {"published_date": "2026-08-17"},
    )

    assert freshness["status"] == "stale"
    assert freshness["score"] < 0


def test_explicit_year_and_trusted_provider_date_are_detected(monkeypatch):
    monkeypatch.setattr(search, "_current_year", lambda: 2026)

    explicit = search._freshness(
        "Acme release 2026",
        "Acme release for 2026",
        "Published release notes.",
        "https://acme.example/release",
    )
    provider_date = search._freshness(
        "latest Acme release",
        "Acme release",
        "Release notes.",
        "https://acme.example/release",
        {"published_date": "2026-04-01", "search_date": "2025-01-01"},
    )

    assert explicit["status"] == "verified"
    assert explicit["evidence"]["matched_years"] == [2026]
    assert provider_date["status"] == "verified"
    assert provider_date["evidence"]["metadata_years"] == [2026]


def test_current_result_wins_when_relevance_is_equivalent(monkeypatch):
    _rows(
        monkeypatch,
        [
            ("Acme release 2025", "Acme version", "https://acme.example/2025"),
            ("Acme release 2026", "Acme version", "https://acme.example/2026"),
        ],
    )

    payload = json.loads(search.web_search("latest Acme release"))

    assert payload["results"][0]["url"] == "https://acme.example/2026"
    detail = payload["freshness"]["results"]["https://acme.example/2026"]
    assert detail["status"] == "verified"


def test_non_temporal_search_behavior_is_unchanged(monkeypatch):
    _rows(
        monkeypatch,
        [("Acme homepage", "Official Acme product", "https://acme.example/")],
    )

    payload = json.loads(search.web_search("official Acme homepage"))

    assert payload["results"][0]["url"] == "https://acme.example/"
    assert "freshness" not in payload


def test_missing_date_is_candidate_not_verified(monkeypatch):
    _rows(
        monkeypatch,
        [("Latest Acme release", "Current Acme version", "https://acme.example/release")],
    )

    payload = json.loads(search.web_search("latest Acme release"))
    detail = payload["freshness"]["results"]["https://acme.example/release"]

    assert detail["status"] == "candidate"
    assert detail["evidence"]["title_years"] == []


def test_fetch_content_year_is_scoped_freshness_evidence(monkeypatch):
    monkeypatch.setattr(search, "_current_year", lambda: 2026)

    verified = search._freshness(
        "latest Acme release",
        "Latest Acme release",
        "Current version",
        "https://acme.example/release",
        content="Acme release updated in 2026 with supported packages.",
    )
    copyright_only = search._freshness(
        "latest Acme release",
        "Latest Acme release",
        "Current version",
        "https://acme.example/release",
        content="Acme copyright 2026.",
    )

    assert verified["status"] == "verified"
    assert verified["evidence"]["content_years"] == [2026]
    assert copyright_only["status"] == "candidate"


def test_no_freshness_evidence_stops_search_safely(monkeypatch):
    _rows(
        monkeypatch,
        [("Acme", "Official product information", "https://acme.example/")],
    )

    payload = json.loads(search.web_search("current Acme information"))

    assert payload["results"] == []


def test_fetch_content_must_verify_candidate_freshness(monkeypatch, tmp_path):
    monkeypatch.setattr(search, "_current_year", lambda: 2026)
    url = "https://acme.example/release"
    monkeypatch.setattr(
        search,
        "_search_once",
        lambda _query, _limit: (
            [("Latest Acme release", "Current version", url)],
            "test",
        ),
    )
    monkeypatch.setattr(search, "_validate_public_url", lambda value: value)
    monkeypatch.setattr(
        search,
        "_read",
        lambda _request, _limit: (
            b"<html><body>Acme release notes without a publication date.</body></html>",
            {"Content-Type": "text/html"},
            url,
            False,
        ),
    )
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    turn = [
        response([tool_block("web_search", {"query": "latest Acme release"})], "tool_use"),
        response([tool_block("web_fetch", {"url": url})], "tool_use"),
        response([text_block("I could not verify which release is latest.")]),
        response([text_block("I could not verify which release is latest.")]),
    ]
    app = make_waku(tmp_path / "home", client=ScriptedClient([gate] + turn))

    result = app.respond("What is the latest Acme release?")

    fetch_output = json.loads(result.tool_calls[1]["output"])
    assert fetch_output["error"]["code"] == "freshness_unverified"
    assert url not in result.reply
