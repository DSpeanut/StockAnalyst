"""Download filings for a ticker to data/raw/{cik}/{accession}/ using settings (.env / SEC_USER_AGENT)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from stockanalyst.config import Settings, load_settings
from stockanalyst.ingestion.edgar_client import DEFAULT_FORMS, EdgarClient, FilingFetcher, FilingRef


def download_filings(
    ticker: str,
    forms: tuple[str, ...] = DEFAULT_FORMS,
    since: date | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
) -> list[tuple[FilingRef, Path]]:
    """Fetch a ticker's filings with the configured User-Agent and rate limit. Skips files already on disk."""
    settings = settings or load_settings()
    with EdgarClient(settings.sec.user_agent, settings.sec.max_requests_per_second) as client:
        return FilingFetcher(client, settings.raw_dir).fetch(ticker, forms, since, limit)
