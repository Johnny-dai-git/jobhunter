#!/usr/bin/env bash
# 每周一次的趋势报告
#
# crontab 加一行 (周日早 9 点):
#   0 9 * * 0  /home/johnny/Documents/Claude/Projects/job-agent/scripts/weekly.sh >> /tmp/job-agent.log 2>&1

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

CONDA_BASE="${CONDA_BASE:-/home/johnny/miniconda3}"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate job-agent

echo "=== $(date) Weekly trends report ==="
python -m src.main trends --days 30 --min-score 50 --format md --format html --email
echo "=== Done $(date) ==="
