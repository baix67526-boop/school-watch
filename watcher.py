name: school watch

on:
  workflow_dispatch:
  schedule:
    - cron: "*/30 * * * *"
  push:
    paths:
      - sources.txt
      - watcher.py
      - subscriptions.xlsx
      - state.json
      - .github/workflows/watch.yml

jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Run watcher
        env:
          SMTP_HOST: ${{ secrets.SMTP_HOST }}
          SMTP_PORT: ${{ secrets.SMTP_PORT }}
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
          MAIL_TO: ${{ secrets.MAIL_TO }}

          # ✅ 关键：把这两个也传进去
          TEST_MAIL_TO: ${{ secrets.TEST_MAIL_TO }}
          ALWAYS_SEND_SUMMARY: ${{ secrets.ALWAYS_SEND_SUMMARY }}
        run: python watcher.py

      - name: Commit state
        run: |
          git config user.name "github-actions"
          git config user.email "github-actions@github.com"
          git add state.json
          git commit -m "Update state" || echo "No changes"
          git push
