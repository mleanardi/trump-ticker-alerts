name: Trump Ticker Alerter

on:
  schedule:
    # Every 5 minutes. GitHub Actions cron can be delayed under load —
    # typical lag is 1-5 min, occasionally longer. Good enough for this.
    - cron: "*/5 * * * *"
  workflow_dispatch: {}  # allow manual runs from the Actions tab

# Allow this workflow to push state.json back to the repo
permissions:
  contents: write

# Cancel a stuck previous run if a new one is starting
concurrency:
  group: alerter
  cancel-in-progress: false

jobs:
  poll:
    runs-on: ubuntu-latest
    timeout-minutes: 4
    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          # Need write access to commit state.json
          token: ${{ secrets.GITHUB_TOKEN }}

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      # No third-party deps — alerter.py uses only Python stdlib.

      - name: Run alerter
        env:
          PUSHOVER_TOKEN: ${{ secrets.PUSHOVER_TOKEN }}
          PUSHOVER_USER: ${{ secrets.PUSHOVER_USER }}
          FACTBASE_API_KEY: ${{ secrets.FACTBASE_API_KEY }}
          # Daily heartbeat push. HEARTBEAT_HOUR is in local time defined by
          # HEARTBEAT_TZ_OFFSET (hours from UTC). Default below = 9am US Eastern.
          # Set HEARTBEAT=0 to disable the daily ping entirely.
          HEARTBEAT: "1"
          HEARTBEAT_HOUR: "9"
          HEARTBEAT_TZ_OFFSET: "-5"
          DEBUG: "1"
        run: python alerter.py

      - name: Commit updated state
        run: |
          git config user.name  "trump-ticker-bot"
          git config user.email "bot@users.noreply.github.com"
          if [[ -n "$(git status --porcelain state.json)" ]]; then
            git add state.json
            git commit -m "state: update seen_ids [skip ci]"
            git push
          else
            echo "No state changes to commit."
          fi
