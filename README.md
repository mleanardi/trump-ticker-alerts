# Trump Ticker Alerter

Polls Trump's Truth Social posts (and optionally Factba.se for speech remarks) every 5 minutes. Whenever a new post mentions a publicly traded company — by name, alias, or ticker — you get a push notification on your phone via Pushover.

## What you get

- **`alerter.py`** — the polling/matching/alerting script. Pure Python stdlib, no `pip install` needed.
- **`tickers.csv`** — 200+ high-relevance companies (Big Tech, banks, defense, autos, retail, media, etc.). Edit freely.
- **`.github/workflows/alerter.yml`** — runs the script every 5 minutes on GitHub Actions, free.
- **`state.json`** — auto-created on first run; tracks which posts we've already alerted on so you don't get duplicates.

## How alerts look

> **Trump mentioned $TSLA, $F, $GM**
>
> [truthsocial] 2026-05-29 14:32 UTC
> Matched: TSLA (Tesla), F (Ford), GM (General Motors)
>
> "Tesla is doing GREAT things in America. Ford and General Motors better step up!"

Tapping the alert opens the original post.

## Setup (about 10 minutes)

### 1. Get Pushover credentials

1. Install **Pushover** on your phone (iOS / Android, $5 one-time after a 30-day trial).
2. Sign up at <https://pushover.net>.
3. On the dashboard, copy your **User Key**.
4. Click **Create an Application/API Token**, name it "Trump Alerts", and copy the **API Token**.

### 2. Create a private GitHub repo

1. Create a new **private** repository on GitHub (e.g. `trump-ticker-alerts`).
2. Upload all files in this folder to it — including `.github/workflows/alerter.yml`.

### 3. Add secrets

In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**, and add:

| Name | Value |
| --- | --- |
| `PUSHOVER_TOKEN` | Your Pushover application API token |
| `PUSHOVER_USER`  | Your Pushover user key |
| `FACTBASE_API_KEY` | (Optional) Your Factba.se API key, if you have one |

### 4. Enable Actions and run it

1. Go to the **Actions** tab. If prompted, enable workflows.
2. Click **Trump Ticker Alerter → Run workflow** to trigger a manual first run.
3. From then on it runs automatically every 5 minutes.

## Notes & caveats

- **GitHub Actions cron is not exact.** It targets every 5 minutes but can lag by 1–10 minutes during peak hours. For most of his posts, alerts will arrive within 10 minutes of posting.
- **Truth Social source.** We poll `trumpstruth.org`, a community-maintained mirror of Trump's Truth Social posts. If it ever goes down, alerts pause. Replace `TRUTH_RSS` in `alerter.py` with another source if needed.
- **Factba.se for speeches.** Their realtime-remarks endpoints are paywalled and the exact URL/format varies by tier. The current `fetch_factbase()` function is a stub — if you have a subscription, swap `FACTBASE_REMARKS_URL` and the response parsing to match your tier's docs.
- **Watchlist.** `tickers.csv` includes the 200 most politically/economically salient names. Edit the file (add rows, remove rows) to taste. The `aliases` column is `|`-separated and lets you catch nicknames ("J&J", "Coke", "Marlboro", etc.).
- **False positives.** "Apple" only alerts if disambiguating context (iPhone, Tim Cook, App Store, etc.) appears in the same post — OR another unambiguous company is mentioned alongside. Similar guards exist for Target, Ford, Block, Square, Gap, Discover, Visa, and Shell.
- **First run dedup.** On the very first run, posts older than 60 minutes are silently marked as seen so you don't get a flood of historical alerts. Adjust with `LOOKBACK_MINUTES`.

## Test it locally

```bash
# Sanity-check the matcher against a handful of sample posts:
python3 alerter.py selftest

# Dry-run the full pipeline (set DEBUG=1 to see logs; without Pushover keys
# it will skip sending but otherwise exercise the full code path):
DEBUG=1 python3 alerter.py
```

## Why not run this inside Cowork?

Cowork's sandbox blocks egress to trumpstruth.org, Factba.se, Pushover, and most non-Anthropic domains. A scheduled task inside Cowork would fail on every run. GitHub Actions is free, always-on, and unblocked — strictly better for this workload.
