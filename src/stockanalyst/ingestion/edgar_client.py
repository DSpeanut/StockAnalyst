"""EDGAR HTTP client: rate limiting, User-Agent, retries, and ticker -> filings -> download.

SEC fair access: max 10 requests/second per IP and a User-Agent identifying you.
We default to 8 req/s for headroom. No API key is needed.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"

DEFAULT_FORMS = ("8-K", "10-Q", "10-K")


class RateLimiter:
    """Thread-safe limiter: spaces calls at least 1/rate seconds apart."""

    def __init__(self, rate_per_second: float = 8.0):
        if not 0 < rate_per_second <= 10:
            raise ValueError("SEC allows at most 10 requests/second")
        self._interval = 1.0 / rate_per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next - now
            self._next = max(now, self._next) + self._interval
        if sleep_for > 0:
            time.sleep(sleep_for)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


class EdgarClient:
    """Rate-limited, retrying GET client for SEC endpoints."""

    def __init__(self, user_agent: str, rate_per_second: float = 8.0, timeout: float = 30.0):
        self._limiter = RateLimiter(rate_per_second)
        self._http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
        )

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    def get(self, url: str) -> httpx.Response:
        self._limiter.wait()
        resp = self._http.get(url)
        resp.raise_for_status()
        return resp

    def get_json(self, url: str) -> dict:
        return self.get(url).json()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EdgarClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass(frozen=True)
class FilingRef:
    ticker: str
    cik: int
    accession: str  # with dashes, e.g. 0000320193-24-000123
    form: str
    filing_date: str
    report_date: str
    primary_document: str
    items: str  # 8-K item codes, e.g. "2.02,9.01"; empty otherwise

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(
            cik=self.cik, acc_nodash=self.accession.replace("-", ""), doc=self.primary_document
        )


class FilingFetcher:
    """Given a ticker, list and download its 8-K / 10-Q / 10-K filings from EDGAR."""

    def __init__(self, client: EdgarClient, raw_dir: Path):
        self.client = client
        self.raw_dir = Path(raw_dir)
        self._ticker_map: dict[str, int] | None = None

    # -- ticker -> CIK ---------------------------------------------------
    def cik_for(self, ticker: str) -> int:
        if self._ticker_map is None:
            data = self.client.get_json(TICKERS_URL)
            self._ticker_map = {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}
        try:
            return self._ticker_map[ticker.upper()]
        except KeyError:
            raise ValueError(f"Unknown ticker: {ticker}") from None

    # -- list filings ----------------------------------------------------
    def list_filings(
        self,
        ticker: str,
        forms: tuple[str, ...] = DEFAULT_FORMS,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[FilingRef]:
        """Newest-first filings of the given forms. Walks older submission pages only if `since` needs them."""
        cik = self.cik_for(ticker)
        sub = self.client.get_json(SUBMISSIONS_URL.format(cik=cik))
        blocks = [sub["filings"]["recent"]]
        if since:
            for extra in sub["filings"].get("files", []):
                if extra.get("filingTo", "9999") < since.isoformat():
                    continue
                blocks.append(self.client.get_json(SUBMISSIONS_PAGE_URL.format(name=extra["name"])))

        wanted = set(forms)
        out: list[FilingRef] = []
        for b in blocks:
            for i, form in enumerate(b["form"]):
                if form not in wanted:
                    continue
                fdate = b["filingDate"][i]
                if since and fdate < since.isoformat():
                    continue
                out.append(
                    FilingRef(
                        ticker=ticker.upper(),
                        cik=cik,
                        accession=b["accessionNumber"][i],
                        form=form,
                        filing_date=fdate,
                        report_date=b["reportDate"][i],
                        primary_document=b["primaryDocument"][i],
                        items=b["items"][i] if "items" in b else "",
                    )
                )
        out.sort(key=lambda f: f.filing_date, reverse=True)
        return out[:limit] if limit else out

    # -- download --------------------------------------------------------
    def download(self, ref: FilingRef, overwrite: bool = False) -> Path:
        """Save primary document + metadata.json to raw_dir/{cik}/{accession}/. Returns the document path."""
        dest = self.raw_dir / str(ref.cik) / ref.accession
        doc_path = dest / ref.primary_document
        if doc_path.exists() and not overwrite:
            return doc_path
        dest.mkdir(parents=True, exist_ok=True)
        resp = self.client.get(ref.url)
        doc_path.write_bytes(resp.content)
        (dest / "metadata.json").write_text(json.dumps({**asdict(ref), "url": ref.url}, indent=2))
        return doc_path

    def fetch(
        self,
        ticker: str,
        forms: tuple[str, ...] = DEFAULT_FORMS,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[tuple[FilingRef, Path]]:
        """List then download. Already-downloaded filings are skipped (idempotent)."""
        return [(r, self.download(r)) for r in self.list_filings(ticker, forms, since, limit)]
