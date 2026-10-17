/* Port News - recent coverage for one port, from the GDELT article index.
 *
 * Read from the database, not fetched live: GDELT throttles hard enough that a
 * per-request fetch would 429 in front of the user. The adapter refreshes on
 * the normal pipeline run. See backend/app/ingest/port_news.py. */
import { useEffect, useState } from "react";
import { api, type NewsArticle, type Port, type PortNews as PortNewsData } from "../api";
import { ProvenanceLine } from "../components/Provenance";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function since(iso: string): string {
  const delta = Date.now() - new Date(iso).getTime();
  if (delta < HOUR) return `${Math.max(1, Math.round(delta / MINUTE))}m ago`;
  if (delta < DAY) return `${Math.round(delta / HOUR)}h ago`;
  const days = Math.round(delta / DAY);
  return days < 30 ? `${days}d ago` : `${Math.round(days / 30)}mo ago`;
}

function stamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function PortNews() {
  const [ports, setPorts] = useState<Port[]>([]);
  const [code, setCode] = useState("INPRT");
  const [news, setNews] = useState<PortNewsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.ports().then(setPorts).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .news(code, 5)
      .then((n) => setNews(n))
      .catch((e) => {
        setError(String(e));
        setNews(null);
      })
      .finally(() => setLoading(false));
  }, [code]);

  const byRole = (role: string) => ports.filter((p) => p.role === role);

  return (
    <div>
      <div className="page-head">
        <h2>Port News</h2>
        <p>
          The five most recent articles naming each port, from the GDELT global article
          index. Berth closures, strikes, blockades and dredging programmes show up here
          before they show up in a queue length &mdash; this is the screen that explains
          why a number moved. Headlines and links only; follow a link to read the piece at
          its publisher.
        </p>
      </div>

      <div className="panel">
        <div className="form-row">
          <div className="field">
            <label>Port</label>
            <select value={code} onChange={(e) => setCode(e.target.value)}>
              <optgroup label="Discharge">
                {byRole("discharge").map((p) => (
                  <option key={p.code} value={p.code}>{p.name}</option>
                ))}
              </optgroup>
              <optgroup label="Transhipment">
                {byRole("transhipment").map((p) => (
                  <option key={p.code} value={p.code}>{p.name}</option>
                ))}
              </optgroup>
              <optgroup label="Load">
                {byRole("load").map((p) => (
                  <option key={p.code} value={p.code}>{p.name}</option>
                ))}
              </optgroup>
            </select>
          </div>
          {news?.checked_at && (
            <div className="field">
              <label>Last checked</label>
              <div style={{ fontSize: 13, paddingTop: 6 }}>{stamp(news.checked_at)}</div>
            </div>
          )}
        </div>
      </div>

      {loading && <div className="loading">Loading&hellip;</div>}
      {error && (
        <div className="panel">
          <div className="error">{error}</div>
        </div>
      )}

      {news && !loading && (
        <div className="panel">
          <h3>
            {news.port_name} &mdash;{" "}
            {news.articles.length > 0
              ? `${news.articles.length} recent article${news.articles.length === 1 ? "" : "s"}`
              : "no recent coverage"}
          </h3>

          {news.articles.length > 0 ? (
            <div>
              {news.articles.map((a: NewsArticle) => (
                <article key={a.url} className="news-item">
                  <a
                    className="news-title"
                    href={a.url}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {a.title}
                  </a>
                  <div className="news-meta">
                    <span className="news-outlet">{a.outlet ?? "unknown outlet"}</span>
                    <span className="news-dot">&middot;</span>
                    <span title={stamp(a.seen_at)}>{since(a.seen_at)}</span>
                    {a.source_country && (
                      <>
                        <span className="news-dot">&middot;</span>
                        <span>{a.source_country}</span>
                      </>
                    )}
                    {a.matched_term && (
                      <span className="news-term" title="Relevance term that matched">
                        {a.matched_term}
                      </span>
                    )}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            /* An empty result is only trustworthy if it says what was looked
             * for. Anchorages like Sandheads genuinely go weeks without press. */
            <div className="news-empty">
              <div className="news-empty-head">
                No articles naming this port in the last {news.window_days} days.
              </div>
              {news.query && (
                <div className="news-empty-line">
                  query <code>{news.query}</code>
                </div>
              )}
              <div className="news-empty-line">
                {news.checked_at
                  ? `last checked ${since(news.checked_at)}`
                  : "not yet checked — run the port_news adapter to populate"}
              </div>
            </div>
          )}

          {news.provenance && <ProvenanceLine p={news.provenance} />}
        </div>
      )}
    </div>
  );
}
