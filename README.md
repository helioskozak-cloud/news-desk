# news-desk

Live financial headline aggregator. Pulls from a curated set of free RSS feeds (MarketWatch, Yahoo Finance, CNBC, Investing.com, Seeking Alpha, SEC EDGAR 8-Ks, PR Newswire, BusinessWire, Reuters, BizToc), tags each story with relevant tickers, and renders a chronological stream.

A GitHub Action refreshes the feed every 10 minutes during US market hours, every 30 minutes off-hours, and hourly on weekends. The result is committed to `docs/data/headlines.json` and served by GitHub Pages.

## Architecture

```
news-desk/
├── docs/
│   ├── index.html              # static dashboard (dark, single page)
│   └── data/headlines.json     # the feed (refreshed by CI)
├── scan/
│   ├── fetch_news.py           # RSS scraper + ticker tagger
│   └── universe_tickers.csv    # ticker set for tagging
├── .github/workflows/refresh.yml
└── requirements.txt
```

## Local development

```bat
pip install -r requirements.txt
python scan/fetch_news.py
```

Open `docs/index.html` in a browser (the page reads `data/headlines.json` via a relative path; opening directly with `file://` should work in most browsers).

## Configuration

- **Add or remove feeds**: edit `FEEDS` in `scan/fetch_news.py`. Each entry is `(display_name, category, url)`.
- **Adjust ticker tagging**: `scan/universe_tickers.csv` is one ticker per line. `TICKER_BLOCKLIST` in the scraper filters out common English words that look like tickers (THE, FOR, ON, etc).
- **Cron frequency**: `.github/workflows/refresh.yml` defines three schedules — market hours, off-hours, weekends.

## Adding new sources

Most financial RSS feeds work out of the box with `feedparser`. To add one:

1. Find the feed URL (usually linked from a publication's footer or `/rss/` page).
2. Append a tuple to `FEEDS`.
3. Pick a category — used for color-coded source chips in the UI.

## Not investment advice

This is a news viewer. Headlines come from third parties; tickers are extracted heuristically and may be wrong. Do your own research.
