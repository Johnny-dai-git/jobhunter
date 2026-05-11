#!/usr/bin/env bash
# 每周一次的趋势报告 (开销稍大,不建议每天跑)
#
# crontab 加一行 (周日早 9 点):
#   0 9 * * 0  /home/johnny/Documents/Claude/Projects/job-agent/scripts/weekly.sh >> /tmp/job-agent.log 2>&1

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

echo "=== $(date) Weekly trends report ==="
python3 -m src.main trends --days 30 --min-score 50 --format md --format html --email
echo "=== Done $(date) ==="
