# StockAnalyst Agent: Architecture (Draft v0.1)

## 1. Goals

An agent that ingests SEC EDGAR filings (8-K, 10-Q, 10-K) for a tracked set of tickers, stores them in structured and vector storage, and exposes two modes:

1. **Analysis mode**: given a ticker, produce a final analyst report.
2. **Q&A mode**: answer follow-up questions using the vector DB and math tools.

It has persistent memory, and every run is traced (MLflow) and evaluated (RAGAS).

## 2. Decisions So Far

| Topic | Decision |
|---|---|
| Universe | Top-50 watchlist (user-provided) + on-demand tickers |
| Filing detection | Daily batch (EDGAR daily index) |
| Stack | Local-first, with swappable interfaces so a cloud move is possible later |
| Vector DB | LanceDB (embedded, hybrid dense + FTS search). Pinecone/Milvus/OpenSearch are the upgrade path if scale demands it. |
| SQL DB | DuckDB (embedded, analytical). Ingestion is the single writer; the agent opens it read-only. |
| Prompts | MLflow Prompt Registry (versions + aliases) |
| Financial data access | Structured, SQL-backed tools (no free-form text-to-SQL for now) |
| Report contents | Financial metrics, Risk factors & MD&A, 8-K event timeline |
| Report format | **TBD**: user will provide agent prompt, tools and output template |
| Buy/hold/sell view | Not included |

## 3. High-Level Architecture

```
                 ┌───────────────────────────────────────────┐
                 │            INGESTION PIPELINE             │
 EDGAR daily  ──▶│ Scheduler ─▶ Fetcher ─▶ Parser ─▶ Chunker │
 index + API     │ (cron)       (SEC API)   (HTML/XBRL)  │    │
                 │                                       ▼    │
                 │                          Embedder ─▶ Stores │
                 └───────────────────────────────┬───────────┘
                                                 │
        ┌────────────────────────────────────────┼───────────────┐
        │                  STORAGE               │               │
        │  Raw filings (disk) │ DuckDB (facts,   │ Vector DB     │
        │  + manifest         │ metadata, XBRL)  │ (chunks)      │
        └────────────────────────────────────────┬───────────────┘
                                                 │
                 ┌───────────────────────────────▼───────────┐
                 │               AGENT RUNTIME               │
                 │  Router ─▶ Analysis graph │ Q&A graph     │
                 │  Tools: retrieval, SQL, math, EDGAR fetch │
                 │  Memory: short-term + long-term           │
                 └───────────────────────────────┬───────────┘
                                                 │
                 ┌───────────────────────────────▼───────────┐
                 │      OBSERVABILITY & EVALUATION           │
                 │  MLflow tracing │ RAGAS │ regression sets │
                 └───────────────────────────────────────────┘
```

## 4. Components

### 4.1 Ingestion Pipeline

- **Scheduler**: a daily cron job (or launchd on macOS) runs after EDGAR posts the daily index (evening ET). It is idempotent.
- **Fetcher**:
  - Reads the EDGAR daily index and filters to forms 8-K, 10-Q, 10-K (plus amendments 10-K/A, 10-Q/A) for watched CIKs.
  - Uses the `data.sec.gov` submissions API for on-demand ticker backfill.
  - Maps ticker to CIK through SEC's `company_tickers.json`.
  - Sends a compliant `User-Agent` header and stays under 10 requests/sec.
- **Manifest**: a DuckDB table keyed on accession number records status (`discovered`, `downloaded`, `parsed`, `embedded`, `failed`). This gives resume and dedup.
- **Parser**:
  - HTML to structured sections. 10-K/10-Q use the Item structure (Item 1A Risk Factors, Item 7 MD&A, Item 8 Financials). 8-Ks use item numbers (2.02 Results, 5.02 Officer changes, and so on).
  - XBRL financial facts come from the `companyfacts` / `Financial_Report` data into structured tables. These are not chunked.
- **Chunker** (structure-aware):
  - Split on section and sub-heading boundaries first, then by token size (about 500-800 tokens with small overlap).
  - Keep tables intact as their own chunks (serialised as markdown) with a caption.
  - Attach metadata to every chunk: `ticker, cik, form_type, accession, filing_date, period_of_report, item, section_path, chunk_id`.
  - Optionally add a short contextual header (company, form, period, section) to the embedded text.
- **Embedder**: a pluggable interface (local sentence-transformers model by default, API model optional).

### 4.2 Storage

| Store | Tech (local) | Contents |
|---|---|---|
| Raw filings | Filesystem `data/raw/{cik}/{accession}/` | Original HTML/XML, for reproducibility |
| Structured | DuckDB | Manifest, filings metadata, XBRL facts, derived metrics, 8-K event table |
| Vector | LanceDB | Chunk embeddings plus metadata filters, hybrid (dense + BM25/FTS) search |
| Memory | DuckDB tables (plus a LanceDB table for semantic memory) | See 4.5 |

All stores sit behind repository interfaces (`FilingStore`, `FactStore`, `VectorStore`, `MemoryStore`). That keeps a later move to Postgres+pgvector or Qdrant and S3 mostly a config change.

### 4.3 Agent Runtime

Framework: LangGraph (or an equivalent explicit state-machine framework). Explicit graphs give predictable behavior and clean tracing.

- **Router**: classifies the request as `analyze(ticker)`, `followup(question)`, or `ingest(ticker)` (on-demand), and resolves the ticker.
- **Analysis graph** (a pipeline of specialist steps, then a synthesizer):
  1. Ensure data is fresh. If the ticker is not ingested, trigger on-demand backfill and wait.
  2. **Financials analyst**: SQL and math tools over XBRL facts (growth, margins, ratios, trends, YoY/QoQ).
  3. **Risk & MD&A analyst**: retrieval over Item 1A and Item 7, with diff against the prior period's filing.
  4. **Events analyst**: builds a timeline from 8-Ks.
  5. **Synthesizer**: assembles the final report from the user-provided template, with citations to accession and section.
- **Q&A graph**: a ReAct-style loop with tools. It reads conversation and long-term memory, retrieves, computes, and answers with citations. It refuses or flags when the evidence is insufficient.
- **Tools**:
  - `search_filings(query, ticker, form_type, date_range, item)`: hybrid (dense + BM25) retrieval with metadata filters and optional reranking.
  - `get_metric(ticker, metric, periods, basis)`: structured arguments in, code-built parameterised SQL over DuckDB XBRL facts out. The LLM never writes SQL.
  - `compare_metrics(tickers, metric, periods)`: the same, across tickers or periods.
  - `calc(expression | named metric)`: deterministic math (growth rates, CAGR, margins, ratios). The LLM never does arithmetic itself.
  - `get_filing_section(accession, item)`: fetch a full section.
  - `list_filings(ticker, form_type)`: manifest lookup.
  - `ingest_ticker(ticker)`: on-demand backfill.
- **Prompts**: stored in the **MLflow Prompt Registry**. Each agent prompt (router, financials, risk/MD&A, events, synthesizer, Q&A) and the report template is a registered prompt with numbered versions. The runtime loads by alias (`production`, `staging`) and every trace and eval run records the prompt name and version. Promotion is a move of the alias after a RAGAS run passes. A local `prompts/` folder holds the source files that are registered through a sync script, so they stay reviewable in git.
- **Tool schemas**: live next to the tool code, since the description and argument schema are part of the prompt. The prompt text itself is user-provided (open item).
- **DuckDB access**: ingestion is the only writer. The agent opens the file with `read_only=True`, so run ingestion on its schedule and not concurrently with on-demand writes (on-demand ingest goes through the same writer process). Postgres + pgvector is the fallback if concurrent writes become necessary.

### 4.4 Report Output

Contents: financial metrics, risk factors and MD&A (with period-over-period changes), 8-K event timeline. The exact layout, sections and prompt come from the user's spec. The synthesizer takes the template as input so it can change without code changes. Each claim carries a citation. No buy/hold/sell recommendation.

### 4.5 Memory

- **Short-term**: conversation state for the current session (the graph state/checkpointer).
- **Long-term, semantic**: user preferences (preferred metrics, risk focus) and facts about tracked companies.
- **Long-term, episodic**: past reports and past Q&A summaries per ticker, so the agent can say what changed since the last report.
- **Policy**: write memory explicitly at the end of a run (a "memory writer" step), with a timestamp and a source. Memory never overrides filing data. On conflict, filings win.

### 4.6 Tracing and Evaluation

- **MLflow**:
  - Tracing on all LLM, tool and retrieval calls (`mlflow.<flavor>.autolog()` or manual spans).
  - Runs log prompt version, model, embedding model, chunking config and index version, so experiments are comparable.
  - Ingestion jobs are also logged (counts, failures, latency).
- **RAGAS** (offline evaluation):
  - Metrics: faithfulness, answer relevancy, context precision, context recall, plus answer correctness where ground truth exists.
  - Golden set: 50-100 hand-checked Q&A pairs across tickers and forms, including numeric questions, multi-filing comparisons and unanswerable questions.
  - Run on every change to chunking, retrieval, prompts or model. Results are logged to MLflow, with thresholds as a regression gate.
- **Extra checks**:
  - Numeric accuracy: report figures are checked against XBRL ground truth.
  - Citation validity: every cited accession and section exists and supports the claim.
  - Sampled online evaluation of production traces.

## 5. Repo Layout (proposed)

```
StockAnalyst/
├── architecture.md
├── config/              # tickers.yaml (top-50), settings
├── prompts/             # source files synced to the MLflow Prompt Registry
├── src/
│   ├── ingestion/       # scheduler, fetcher, parser, chunker, embedder
│   ├── storage/         # repository interfaces + local impls
│   ├── agent/           # router, graphs, tools, memory
│   ├── eval/            # RAGAS runners, golden sets, numeric checks
│   └── cli.py           # ingest | analyze TICKER | ask
├── data/                # raw filings, duckdb, vector index (gitignored)
├── tests/
└── mlruns/              # local MLflow (gitignored)
```

## 6. Key Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Messy filing HTML, inconsistent Item headings | Fallback parsers, per-form tests, manual review on a sample |
| Wrong numbers from the LLM | Numbers come from XBRL and the calc tool, not from model arithmetic |
| Tables chunked badly | Tables kept intact, with captions and metadata |
| Stale data (daily batch lag) | The report states "data as of" the last ingested filing, and checks for a newer one on request |
| SEC rate limits and policy | Compliant User-Agent, throttling, retry with backoff, local caching |
| Hallucinated citations | Citation validation step and RAGAS faithfulness gate |
| Cost and latency of re-embedding | Skip by accession (idempotent), version the embedding model in the index |

## 7. Open Items

- [ ] Top-50 ticker list (`config/tickers.yaml`)
- [ ] Agent prompt text and report output template (to register in the MLflow Prompt Registry)
- [ ] LLM and embedding model choices (default proposal: Claude for reasoning, a local embedding model)
- [ ] Orchestration framework: LangGraph vs a custom loop
- [ ] How much history to backfill per ticker (proposal: last 3 years of 10-K/10-Q, last 12 months of 8-K)
- [ ] Interface for follow-up Q&A (CLI, notebook or small web UI)

## 8. Suggested Build Order

1. Fetcher, manifest and raw storage for the watchlist
2. Parser and XBRL facts into DuckDB
3. Chunker, embedder and vector store, with retrieval smoke tests
4. Tools (search, SQL, calc) with unit tests
5. Q&A graph, then the analysis graph, using your prompts and template
6. MLflow tracing, then the RAGAS golden set and regression gate
7. Memory
8. On-demand ingestion and the CLI or UI
