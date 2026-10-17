"""Recent news coverage for each port, via the GDELT DOC 2.0 API.

Why this source. GDELT indexes global news and exposes article *metadata* -
headline, outlet domain, language, link, and the time it first saw the piece -
with no API key and terms that permit unrestricted use. The alternative with
better small-port coverage, Google News RSS, states in its own feed that it is
for "personal, non-commercial use" in "a personal feed reader", which is not
what this is. See DATA_SOURCES.md.

Three things about GDELT that the code below exists to handle, all found by
probing it rather than by reading the documentation:

**It throttles hard.** The published limit is one request every five seconds.
In practice requests spaced 5.5 s apart draw 429s, and after sustained use it
imposes a cooldown that outlasts 12 s of backoff. Ports are therefore walked
serially with generous spacing, a full refresh takes three to four minutes, and
a run that only manages some of the ports is treated as a normal outcome rather
than a failure - the rows already stored stay put, and the next run fills in.

**``OR`` must be parenthesised.** ``"a" OR "b"`` is rejected with *HTTP 200* and
a plain-text body, not an error status. So a 200 is not a promise of JSON, and
zero articles is not a promise that the port has no coverage.

**A query alone is not enough.** Searching for a port name returns real but
unrelated material - a political summit for Paradip, an obituary for the
Mississippi query. Each port therefore carries relevance terms, and an article
is kept only if one of them appears in its title or URL.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from ..config import RAW_DIR
from ..models import NewsArticle
from .base import IngestResult, SourceAdapter, upsert

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
LANDING_PAGE = "https://www.gdeltproject.org/"

#: How far back to ask for. GDELT DOC holds a rolling window of a few months.
WINDOW_DAYS = 90
TIMESPAN = "3months"
#: Articles requested per port before the relevance guard thins them.
MAX_RECORDS = 40
#: Seconds between ports. Above the documented 5 s because the documented
#: figure is not sufficient in practice.
REQUEST_SPACING_S = 15.0
RETRY_BACKOFF_S = (20.0, 45.0)

#: port code -> (GDELT query, relevance terms).
#:
#: Held here rather than in port_constraints.yaml: this is search tuning in one
#: provider's query syntax, not port physics. `sse_cdfi.py` holds its SERIES_MAP
#: the same way and for the same reason.
#:
#: Every OR group is parenthesised because GDELT rejects the query otherwise,
#: and does so with a 200. `test_port_news.py` asserts this.
PORT_QUERIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "INPRT": ('("Paradip Port" OR "Paradip Port Authority" OR "Paradipport")', ("paradip",)),
    "INVTZ": (
        '("Visakhapatnam Port" OR "Vizag Port" OR "Visakhapatnam Port Authority")',
        ("visakhapatnam", "vizag"),
    ),
    "INGGV": ('("Gangavaram Port" OR "Adani Gangavaram")', ("gangavaram",)),
    "INDHR": ('("Dhamra Port" OR "Adani Dhamra")', ("dhamra",)),
    "INGPR": ('("Gopalpur Port" OR "Gopalpur ports")', ("gopalpur",)),
    "INHAL": (
        '("Haldia Dock" OR "Haldia Port" OR "Syama Prasad Mookerjee Port")',
        ("haldia", "mookerjee"),
    ),
    "INSAG": ('("Sagar Island" OR "Sagar anchorage" OR "Hooghly transhipment")', ("sagar", "hooghly")),
    "INSND": ('("Sandheads" OR "Hooghly estuary" OR "Sandheads anchorage")', ("sandheads", "hooghly")),
    "AUNTL": (
        '("Port of Newcastle" OR "Newcastle coal" OR "Port Waratah")',
        ("newcastle", "waratah"),
    ),
    "AUHPT": (
        '("Hay Point" OR "Dalrymple Bay" OR "Dalrymple Bay Coal Terminal")',
        ("hay point", "dalrymple"),
    ),
    "IDTAB": (
        '("Taboneo" OR "Banjarmasin" OR "South Kalimantan coal")',
        ("taboneo", "banjarmasin", "kalimantan"),
    ),
    "MZBEW": ('("Port of Beira" OR "Beira port" OR "Beira corridor")', ("beira",)),
    "USORF": (
        '("Lamberts Point" OR "Norfolk Southern coal" OR "Port of Virginia")',
        ("lamberts point", "norfolk", "virginia"),
    ),
    "USMSR": (
        '("Mississippi River" OR "Port of South Louisiana" OR "New Orleans port")',
        ("mississippi", "louisiana", "new orleans"),
    ),
}


class GdeltQueryError(ValueError):
    """The query we sent was malformed. Ours to fix, not a transient failure."""


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]


def _parse_seendate(value: str) -> datetime | None:
    """GDELT stamps are ``20260912T093000Z``."""
    try:
        return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return None


def parse_gdelt(content: bytes, terms: tuple[str, ...]) -> list[dict]:
    """Parse one GDELT artlist response into relevant article records.

    Raises rather than returning an empty list when the body is not JSON. A
    throttle or a malformed query would otherwise look identical to "this port
    has no coverage", and writing that over good rows would silently empty the
    screen.
    """
    text = content.decode("utf-8", "replace").strip()

    if "must be surrounded by ()" in text:
        raise GdeltQueryError(f"GDELT rejected the query: {text[:200]}")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # Throttling and outages both arrive as prose with a 200.
        raise ValueError(f"GDELT returned a non-JSON body: {text[:200]!r}") from exc

    # A bare {} is the subtlest of GDELT's failure modes: valid JSON, HTTP 200,
    # and indistinguishable from "no coverage" unless you look for the key. A
    # port that genuinely has nothing still comes back with an articles list.
    # Treating {} as "no news" would quietly blank a port that in fact has some.
    if "articles" not in payload:
        raise ValueError(f"GDELT payload carried no 'articles' key: {text[:200]!r}")

    records: list[dict] = []
    for article in payload.get("articles") or []:
        url = (article.get("url") or "").strip()
        title = (article.get("title") or "").strip()
        if not url or not title:
            continue

        haystack = f"{title} {url}".lower()
        matched = next((t for t in terms if t in haystack), None)
        if matched is None:
            continue  # real article, wrong subject

        seen_at = _parse_seendate(article.get("seendate", ""))
        if seen_at is None:
            continue

        records.append(
            {
                "url_hash": url_hash(url),
                "url": url[:1000],
                "title": title[:500],
                "outlet": (article.get("domain") or None),
                "language": (article.get("language") or None),
                "source_country": (article.get("sourcecountry") or None),
                "seen_at": seen_at,
                "matched_term": matched,
            }
        )

    return records


class PortNewsAdapter(SourceAdapter):
    name = "port_news"
    description = (
        "Recent news coverage per port from the GDELT DOC 2.0 article index: "
        "headline, outlet and link, filtered to articles naming the port"
    )
    homepage = LANDING_PAGE

    def query_url(self, query: str) -> str:
        return f"{ENDPOINT}?" + urlencode(
            {
                "query": query,
                "mode": "artlist",
                "maxrecords": MAX_RECORDS,
                "format": "json",
                "sort": "datedesc",
                "timespan": TIMESPAN,
            }
        )

    def probe(self) -> tuple[bool, str]:
        query, terms = PORT_QUERIES["INPRT"]
        response = self.client.get(self.query_url(query))
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        try:
            records = parse_gdelt(response.content, terms)
        except GdeltQueryError as exc:
            return False, str(exc)
        except ValueError:
            return False, "throttled or unavailable (non-JSON body)"
        return True, f"{len(records)} relevant articles for Paradip"

    def run(self, session: Session) -> IngestResult:
        """Walk every port, slowly, writing whatever comes back.

        Partial success is the expected case under throttling, so this keeps
        going after a port fails and reports how many made it.
        """
        total_written = 0
        ok_ports: list[str] = []
        cached_ports: list[str] = []
        failed: list[str] = []

        for index, (port_code, (query, terms)) in enumerate(PORT_QUERIES.items()):
            if index:
                time.sleep(REQUEST_SPACING_S)

            url = self.query_url(query)
            try:
                # Note: base.fetch() caches any non-empty body, so a throttled
                # {} can land in the cache. That is survivable because
                # parse_gdelt rejects it, so the port reports failed rather
                # than empty, and the next good fetch overwrites it.
                fetched = self._fetch_with_backoff(url)
                records = parse_gdelt(fetched.content, terms)
            except GdeltQueryError:
                raise  # our bug; fail the run loudly rather than hiding it
            except Exception as exc:
                failed.append(f"{port_code}({type(exc).__name__})")
                continue

            if not fetched.from_cache:
                stamp = datetime.now(timezone.utc).date().isoformat()
                archive = RAW_DIR / "port_news" / f"{port_code}_{stamp}.json"
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(fetched.content)

            provenance = self.provenance(fetched, url=url)
            rows = [{**record, "port_code": port_code, **provenance} for record in records]
            total_written += upsert(session, NewsArticle, rows, ["port_code", "url_hash"])
            # Commit per port rather than once at the end. This loop runs for
            # minutes; if it is interrupted half way, the ports already fetched
            # should stay fetched instead of being thrown away.
            session.commit()

            (cached_ports if fetched.from_cache else ok_ports).append(port_code)

        if not ok_ports and not cached_ports:
            raise RuntimeError(f"every port failed: {', '.join(failed)}")

        status = "ok" if not cached_ports and not failed else "stale"
        message = f"{len(ok_ports)}/{len(PORT_QUERIES)} ports fetched live"
        if cached_ports:
            message += f", {len(cached_ports)} from cache"
        if failed:
            message += f", failed: {', '.join(failed)}"

        return IngestResult(
            self.name,
            status,
            rows_written=total_written,
            message=message,
            used_cache=bool(cached_ports),
        )

    def _fetch_with_backoff(self, url: str):
        """Retry a throttled request, settling for cache only once retries run out.

        Exactly one HTTP request per attempt. ``fetch()`` already swallows a
        failure and serves the cached copy, so a cached result is the signal
        that the live attempt failed and is worth retrying - probing an extra
        time first would double our request count against an API that is
        throttling us for making too many.
        """
        cached = None
        last_error: Exception | None = None

        for pause in (0.0, *RETRY_BACKOFF_S):
            if pause:
                time.sleep(pause)
            try:
                result = self.fetch(url)
            except Exception as exc:  # no cache to fall back on either
                last_error = exc
                continue
            if not result.from_cache:
                return result
            cached = result

        if cached is not None:
            return cached
        raise last_error or RuntimeError(f"no response for {url}")
