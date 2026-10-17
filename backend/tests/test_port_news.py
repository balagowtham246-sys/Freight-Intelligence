"""Tests for the GDELT port-news adapter.

Everything here runs offline against fixture payloads. The fixtures are not
invented: the junk article and the two error bodies are real responses seen
while building the adapter, kept so the regressions they caused stay caught.
"""
from __future__ import annotations

import json

import pytest

from app.domain.reference import load_ports_list
from app.ingest import ADAPTERS
from app.ingest.port_news import (
    PORT_QUERIES,
    GdeltQueryError,
    PortNewsAdapter,
    parse_gdelt,
    url_hash,
)

# A real GDELT artlist response, trimmed. The first two articles are genuinely
# about the port; the third is the obituary that a loose Mississippi query
# actually returned, and the fourth a summit piece that merely mentioned the
# region.
SAMPLE = json.dumps(
    {
        "articles": [
            {
                "url": "https://orissapost.com/dhamra-port-row-sparks-rail-blockade/",
                "title": "Dhamra Port row sparks rail blockade",
                "seendate": "20260910T053000Z",
                "domain": "orissapost.com",
                "language": "English",
                "sourcecountry": "India",
            },
            {
                "url": "https://example.com/paradip-berth-dredging",
                "title": "Paradip deepens coal berth to 18.5 m",
                "seendate": "20260912T093000Z",
                "domain": "example.com",
                "language": "English",
                "sourcecountry": "India",
            },
            {
                "url": "https://times-journal.com/obituaries/billy-colburn",
                "title": "Obit Coach Billy Colburn Fort Payne",
                "seendate": "20260912T103000Z",
                "domain": "times-journal.com",
                "language": "English",
                "sourcecountry": "United States",
            },
            {
                "url": "https://moneycontrol.com/rising-bharat-summit",
                "title": "Rising Bharat Summit: Meet the Voices Shaping the East",
                "seendate": "20260912T093000Z",
                "domain": "moneycontrol.com",
                "language": "English",
                "sourcecountry": "India",
            },
        ]
    }
).encode()

THROTTLED = b"Please limit requests to one every 5 seconds or contact ... for larger queries."
BAD_QUERY = b"Queries containing OR'd terms must be surrounded by ().\n"


# ------------------------------------------------------------------ parsing


def test_parses_relevant_articles():
    records = parse_gdelt(SAMPLE, ("dhamra", "paradip"))
    titles = [r["title"] for r in records]

    assert "Dhamra Port row sparks rail blockade" in titles
    assert "Paradip deepens coal berth to 18.5 m" in titles
    assert len(records) == 2

    first = next(r for r in records if r["matched_term"] == "dhamra")
    assert first["outlet"] == "orissapost.com"
    assert first["language"] == "English"
    assert first["source_country"] == "India"
    assert first["seen_at"].isoformat() == "2026-09-10T05:30:00+00:00"
    assert first["url_hash"] == url_hash(first["url"])


def test_relevance_guard_drops_real_but_unrelated_articles():
    """The obituary and the summit piece are the actual failures this exists for."""
    records = parse_gdelt(SAMPLE, ("dhamra", "paradip"))
    titles = " ".join(r["title"] for r in records).lower()

    assert "obit" not in titles, "an obituary survived the relevance guard"
    assert "summit" not in titles, "an off-topic summit piece survived the relevance guard"


def test_matched_term_is_recorded_so_the_filter_is_auditable():
    records = parse_gdelt(SAMPLE, ("paradip",))
    assert len(records) == 1
    assert records[0]["matched_term"] == "paradip"


def test_term_may_match_the_url_not_only_the_title():
    payload = json.dumps(
        {
            "articles": [
                {
                    "url": "https://example.com/haldia/congestion-update",
                    "title": "Congestion update for eastern terminals",
                    "seendate": "20260910T053000Z",
                    "domain": "example.com",
                }
            ]
        }
    ).encode()
    assert len(parse_gdelt(payload, ("haldia",))) == 1


def test_articles_missing_a_usable_timestamp_are_skipped():
    payload = json.dumps(
        {"articles": [{"url": "https://e.com/paradip", "title": "Paradip", "seendate": "nonsense"}]}
    ).encode()
    assert parse_gdelt(payload, ("paradip",)) == []


def test_no_coverage_is_an_empty_list_not_an_error():
    assert parse_gdelt(b'{"articles": []}', ("sandheads",)) == []


def test_bare_empty_object_is_a_failure_not_no_coverage():
    """GDELT really does answer `{}` with a 200 under load.

    It is valid JSON, so it slips past the non-JSON guard, and reading it as
    "no coverage" would blank a port that actually has articles. A genuine
    empty result carries an `articles` key; this one does not.
    """
    with pytest.raises(ValueError):
        parse_gdelt(b"{}", ("gangavaram",))


# ----------------------------------------------------------- failure modes


def test_throttle_body_raises_rather_than_parsing_to_nothing():
    """A 200 is not a promise of JSON.

    Returning [] here would overwrite a port's stored articles with nothing the
    next time GDELT rate-limits us, silently emptying the screen.
    """
    with pytest.raises(ValueError):
        parse_gdelt(THROTTLED, ("paradip",))


def test_malformed_query_raises_a_distinct_error():
    """Our bug, not a transient one, so the adapter fails loudly instead of caching."""
    with pytest.raises(GdeltQueryError):
        parse_gdelt(BAD_QUERY, ("paradip",))


# ------------------------------------------------------------------ queries


def test_every_or_group_is_parenthesised():
    """GDELT rejects unparenthesised OR with a 200 and a prose body.

    That failure looks exactly like "this port has no news", which is how it
    went unnoticed the first time.
    """
    def has_bare_or(query: str) -> bool:
        """True if any ` OR ` sits at parenthesis depth zero."""
        depth = 0
        for i, char in enumerate(query):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and query[i : i + 4] == " OR ":
                return True
        return False

    offenders = [code for code, (query, _) in PORT_QUERIES.items() if has_bare_or(query)]
    assert not offenders, f"unparenthesised OR in queries for: {offenders}"

    # The check must be capable of failing, or it guards nothing.
    assert has_bare_or('"Sagar Island" OR "Sandheads"')
    assert not has_bare_or('("Sagar Island" OR "Sandheads")')
    assert not has_bare_or('("a" OR "b") ("c" OR "d")')


def test_queries_and_reference_ports_agree():
    codes = {port["code"] for port in load_ports_list()}
    assert set(PORT_QUERIES) == codes, (
        "every port in port_constraints.yaml needs a news query and vice versa; "
        f"missing queries: {sorted(codes - set(PORT_QUERIES))}, "
        f"unknown ports: {sorted(set(PORT_QUERIES) - codes)}"
    )


def test_every_port_declares_at_least_one_relevance_term():
    for code, (_, terms) in PORT_QUERIES.items():
        assert terms, f"{code} has no relevance terms, so nothing would be filtered"
        assert all(t == t.lower() for t in terms), f"{code} terms must be lowercase to match"


# ------------------------------------------------------------------ adapter


def test_adapter_is_registered_and_not_synthetic():
    assert ADAPTERS["port_news"] is PortNewsAdapter
    adapter = PortNewsAdapter()
    try:
        record = adapter.provenance()
    finally:
        adapter.close()

    assert record["source_name"] == "port_news"
    assert record["is_synthetic"] is False


def test_query_url_encodes_the_query():
    adapter = PortNewsAdapter()
    try:
        url = adapter.query_url('("Paradip Port")')
    finally:
        adapter.close()

    assert url.startswith("https://api.gdeltproject.org/api/v2/doc/doc?")
    assert "format=json" in url
    assert "sort=datedesc" in url
    assert "Paradip" in url
    assert " " not in url


# ----------------------------------------------------------------- endpoint


@pytest.fixture()
def news_client():
    """The API on a throwaway in-memory database.

    There is no shared DB fixture in this suite yet, and the news endpoint does
    enough on the way out - ordering, de-duplication, the limit - to be worth
    testing directly rather than trusting by inspection.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import get_session
    from app.main import app
    from app.models import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    def override():
        session = Local()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = override
    try:
        yield TestClient(app), Local
    finally:
        app.dependency_overrides.pop(get_session, None)
        engine.dispose()


def _article(**kwargs):
    from datetime import datetime, timezone

    from app.models import NewsArticle

    defaults = dict(
        port_code="INPRT",
        url="https://example.com/a",
        title="A story",
        outlet="example.com",
        language="English",
        source_country="India",
        seen_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        matched_term="paradip",
        source_name="port_news",
        source_url="https://api.gdeltproject.org/...",
        is_synthetic=False,
        retrieved_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    defaults["url_hash"] = url_hash(defaults["url"])
    return NewsArticle(**defaults)


def test_endpoint_returns_newest_first_and_respects_the_limit(news_client):
    from datetime import datetime, timezone

    client, Local = news_client
    with Local() as s:
        for day in range(1, 8):
            s.add(
                _article(
                    url=f"https://example.com/{day}",
                    title=f"Story {day}",
                    seen_at=datetime(2026, 9, day, tzinfo=timezone.utc),
                )
            )
        s.commit()

    body = client.get("/api/news/INPRT?limit=5").json()
    assert [a["title"] for a in body["articles"]] == [
        "Story 7", "Story 6", "Story 5", "Story 4", "Story 3",
    ]
    assert body["port_name"] == "Paradip"
    assert body["provenance"]["source_name"] == "port_news"
    assert body["provenance"]["is_synthetic"] is False


def test_endpoint_collapses_one_story_syndicated_across_outlets(news_client):
    """GDELT returns the same headline from many outlets; five slots, five stories."""
    from datetime import datetime, timezone

    client, Local = news_client
    with Local() as s:
        for i, outlet in enumerate(["a.com", "b.com", "c.com"]):
            s.add(
                _article(
                    url=f"https://{outlet}/same",
                    title="Dhamra Port row sparks rail blockade",
                    outlet=outlet,
                    seen_at=datetime(2026, 9, 10 - i, tzinfo=timezone.utc),
                )
            )
        s.add(_article(url="https://d.com/other", title="A different story"))
        s.commit()

    body = client.get("/api/news/INPRT?limit=5").json()
    titles = [a["title"] for a in body["articles"]]
    assert len(titles) == 2
    assert titles.count("Dhamra Port row sparks rail blockade") == 1
    # The earliest sighting of the story is the one kept.
    kept = next(a for a in body["articles"] if a["title"].startswith("Dhamra"))
    assert kept["outlet"] == "a.com"


def test_endpoint_explains_itself_when_there_is_nothing(news_client):
    client, _ = news_client
    body = client.get("/api/news/INSND").json()

    assert body["articles"] == []
    assert body["window_days"] > 0
    assert "Sandheads" in body["query"] or "Hooghly" in body["query"]


def test_endpoint_404s_on_an_unknown_port(news_client):
    client, _ = news_client
    assert client.get("/api/news/NOPE").status_code == 404
