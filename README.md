# Job Agent

[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/Orchestration-LangGraph-blue.svg)](https://python.langchain.com/docs/langgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An AI-powered personal job hunting agent that automates job discovery, intelligent matching, resume tailoring, and application tracking. **Not an auto-apply tool** — designed for thoughtful, human-controlled job search with AI assistance.

---

## Features

- **Multi-platform Job Collection** — Automated discovery from LinkedIn, Indeed, Dice, YC Jobs, HackerNews with per-title search strategy
- **6-Dimension Match Scoring** — Background fit, skills overlap, experience relevance, seniority, work authorization, company type preference
- **Profile Analysis Pipeline** — 3-round DeepSeek analysis to extract your top 10 target roles with aliases and broader search terms
- **Smart Resume Tailoring** — 2-round pipeline: DeepSeek gap analysis + plan → Claude Opus execution with materials library integration
- **Interactive Resume Refinement Chat** — Persistent version history with HR/HM dual-perspective system prompt
- **Cross-Platform Deduplication** — URL + content hash based on normalized company/title/location
- **Manual Job Addition** — Paste JD text or upload file, auto-extract fields and scoring
- **Daily Digest Email** — Top-N matching jobs as HTML report (optional)
- **Market Trend Analysis** — Key players, tech stack trends, salary ranges, actionable insights
- **Multi-Agent LangGraph Orchestration** — Daily workflow with context, collection, matching, digest, and validation agents
- **Per-Agent Health Monitoring** — Liveness and readiness endpoints for Kubernetes/systemd integration
- **Web UI + CLI** — FastAPI web interface and comprehensive CLI commands

---

## Architecture

### Multi-Agent Orchestration

`job-agent` uses **LangGraph** as the orchestration layer for daily full-pipeline execution. The system is modular — each agent is a graph node that orchestrates CLI components.

```
LangGraph Orchestrator
  │
  ├─ Context Agent
  │   └─ Load profile + cached resume
  │
  ├─ Collection Agent
  │   └─ collect_all() → LinkedIn/Indeed/Dice/YC/HackerNews
  │
  ├─ Matching Agent
  │   └─ score_pending() → 6-dimension scoring with DeepSeek
  │
  ├─ Digest Agent (conditional)
  │   └─ run_digest() → HTML email
  │
  ├─ Trend Agent (conditional)
  │   └─ generate_trends_report() → Market analysis
  │
  └─ Validation Agent
      └─ Verify artifacts + health checks
```

### LLM Model Routing

```yaml
Profile Analysis:  DeepSeek Chat    (cost-effective, fast)
Matching (6D):     DeepSeek Chat    (high volume, 6x daily per job)
Tailoring:         Claude Opus      (quality-critical, 1-2 per apply)
Market Trends:     DeepSeek Chat    (analytical, monthly/weekly)
```

Both DeepSeek and Claude use the Anthropic SDK with custom base URLs — seamless provider switching.

---

## Quick Start

### 1. Installation

```bash
git clone <repo-url>
cd job-agent

# Create Python environment
conda create -n job-agent python=3.12 -y
conda activate job-agent

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

### 2. Configuration

```bash
# Set up environment variables
cp .env.example .env
# Edit .env and add:
#   ANTHROPIC_API_KEY (for Claude fallback, optional)
#   DEEPSEEK_API_KEY (required)
#   SMTP_PASSWORD (for email digest, optional)
```

Edit `config.yaml`:
- **Preferences**: `job_titles`, `locations`, `must_have_skills`, `nice_to_have_skills`, `exclude_keywords`
- **Platforms**: Enable/disable LinkedIn, Indeed, Dice, YC, HackerNews with cost and timeout settings
- **Scoring Thresholds**: `min_recommend_score` (70), `auto_archive_below` (40)
- **Email Digest**: Recipient, SMTP settings, top-N count

Place your resume file(s) in `data/resume/` (PDF, DOCX, MD, or TXT).

### 3. Initial Setup

```bash
# Initialize database and directories
python -m src.main init

# Parse and cache your resume
python -m src.main parse-resume

# Analyze your profile (generates Top-10 positions from resume)
python -m src.main analyze-profile

# Log in to job platforms and save session cookies
python -m src.main login --platform linkedin
python -m src.main login --platform indeed
```

### 4. Run the Pipeline

**Single-step commands**:
```bash
# Collect from all enabled platforms
python -m src.main collect

# Score all unmatched jobs (6 dimensions)
python -m src.main match

# View high-scoring jobs
python -m src.main list --min-score 70

# See detailed scoring breakdown for a job
python -m src.main show --job-id 5

# Generate tailored resume + cover letter
python -m src.main tailor --job-id 5

# Record that you applied manually
python -m src.main mark-applied --job-id 5 --note "Applied via company website"

# Generate and email digest
python -m src.main digest

# Analyze market trends and email report
python -m src.main trends --email
```

**One-command full workflow** (no auto-apply):
```bash
python -m src.main run-all
# Runs: context → collect → match → digest (if enabled) → trends (if enabled) → validate
```

**Automated daily execution** (cron):
```bash
# Edit crontab
crontab -e

# Add (runs daily at 7:30 AM):
30 7 * * *  /path/to/job-agent/scripts/daily.sh >> /tmp/job-agent.log 2>&1

# Add (runs every Sunday at 9 AM):
0 9 * * 0   /path/to/job-agent/scripts/weekly.sh >> /tmp/job-agent.log 2>&1
```

---

## Command Reference

| Command | Description |
|---------|-------------|
| `init` | Initialize database and directory structure |
| `parse-resume` | Extract and cache resume text (for faster subsequent loads) |
| `analyze-profile` | 3-round DeepSeek analysis to identify Top-10 target roles with aliases |
| `login --platform PLATFORM` | Open browser, log in manually, save session cookies |
| `collect [--platform PLATFORM]` | Scrape all enabled platforms, deduplicate, filter exclude_keywords, store in DB |
| `add-job [--url URL] [--jd JD_TEXT \| --jd-file PATH]` | Manually add a job: LLM extracts fields, auto-scores if `--no-match` not set |
| `match [--limit N]` | 6-dimension scoring for all new unscored jobs |
| `list [--status active\|new\|scored\|applied\|archived] [--min-score N]` | View job tracking table with filter and sort |
| `show --job-id ID` | Display detailed scoring breakdown (6 dimensions), keywords, fit bullets, gaps |
| `tailor --job-id ID [--name NAME] [--no-cover]` | Generate customized resume + cover letter (2-round pipeline) |
| `mark-applied --job-id ID [--note NOTE]` | Mark a job as applied (status tracking only, manual application) |
| `digest` | Generate today's top-N digest as HTML, optionally email |
| `trends [--days 30] [--min-score 50] [--format md\|html] [--email]` | Analyze market trends over time period and email report |
| `enrich [--limit N]` | Backfill `work_mode` and `min_education` for old jobs |
| `backfill-hash [--dry-run]` | Add `content_hash` to historical jobs for cross-platform deduplication |
| `web [--host 127.0.0.1] [--port 8765]` | Start FastAPI web UI for browsing jobs and interactive resume refinement |
| `run-all [--platform all\|PLATFORM] [--no-collect] [--no-digest] [--no-trends]` | Full LangGraph multi-agent workflow (cron-friendly) |

---

## Data Structures

### Job Record (SQLite)

Each job entry stores:
- **Metadata**: source, external_id, url, title, company, location, salary
- **Timestamps**: posted_at, created_at, updated_at, applied_at
- **6-Dimension Scores**: background, skills_overlap, experience, seniority, authorization, company (each 0-30, stored separately)
- **Overall Score**: aggregated match score (0-100)
- **Extracted Insights**: keywords, fit_bullets, connector (from matcher), gaps, summary
- **Deduplication**: content_hash (normalized company+title+location SHA256 prefix)
- **Status**: new, scored, applied, archived
- **Outputs**: tailored_resume_path, tailored_resume_pdf_path, cover_letter_path

### Profile Record

AI-extracted analysis stored in `data/resume/_profile.json`:
- **Summary**: One-sentence positioning
- **Top 10 Positions**: Each with:
  - `title`: Primary job title
  - `aliases`: Alternative titles (e.g., "ML Engineer" → "Machine Learning Engineer")
  - `broader_terms`: Related titles to cast wider net
  - `direction`: Career trajectory label (IC Track, Management Track, etc.)
  - `scores`: Market demand, competition, user advantage, composite score
  - `why_this_position`: List of reasons this role fits
  - `linkedin_search_url`: Direct link to filtered LinkedIn job search
- **Recommended Companies**: By region with hiring signals
- **Target Locations**: Inferred from experience

### Resume Revisions

When using the `/job/{id}/refine` endpoint for interactive improvements:
- **ResumeRevision** records: job_id, version number, markdown content, modification summary, timestamp
- **Chat History**: Persistent JSON file per job tracking user requests and model responses
- **Version Control**: Full history preserved; users can revert to any prior version

---

## Directory Structure

```
job-agent/
├── README.md                    # This file
├── requirements.txt             # Python dependencies
├── pyproject.toml              # Project metadata
├── .env.example                # Template for environment variables
├── .env                        # (git-ignored) Actual credentials
├── config.yaml                 # Job search preferences and platform settings
│
├── src/
│   ├── main.py                 # CLI entry point and command definitions
│   ├── config.py               # Configuration loader
│   ├── db.py                   # SQLAlchemy models (Job, Profile, ResumeRevision, etc.)
│   ├── auth.py                 # Platform login and cookie persistence
│   ├── agent.py                # LLM client factory (Claude/DeepSeek unified)
│   ├── resume_reader.py        # Parse resume files + materials library
│   │
│   ├── profile_analyzer.py     # 3-round profile extraction → Top-10 roles
│   │
│   ├── collect.py              # Job collection orchestration
│   ├── collectors/
│   │   ├── _base.py            # Abstract collector interface
│   │   ├── apify_base.py       # Shared Apify Actor logic
│   │   ├── linkedin_apify.py   # LinkedIn via harvestapi
│   │   ├── indeed_apify.py     # Indeed via Apify
│   │   ├── yc_apify.py         # YC Jobs via Apify
│   │   ├── dice_apify.py       # Dice via Apify
│   │   ├── hackernews.py       # HackerNews (direct API, free)
│   │   └── __init__.py         # Collector registry
│   │
│   ├── matcher.py              # 6-dimension job scoring (tool_use structured output)
│   ├── dedup.py                # Content hash + deduplication logic
│   ├── manual_add.py           # Manual job ingestion with auto-parsing
│   ├── tailor.py               # 2-round resume tailoring pipeline
│   ├── refine.py               # Interactive resume refinement chat
│   ├── cover_letter.py         # Cover letter generation
│   ├── tracker.py              # Application status tracking
│   ├── digest.py               # Daily HTML email generation
│   ├── trends.py               # Market trend analysis and reporting
│   ├── enricher.py             # Backfill missing job fields
│   │
│   ├── multiagent/
│   │   ├── graph.py            # LangGraph graph definition and entry point
│   │   ├── nodes.py            # Individual agent node implementations
│   │   ├── state.py            # Shared state + run options
│   │   └── types.py            # Type definitions for graph
│   │
│   ├── pdf_generator.py        # Markdown to PDF conversion
│   └── web.py                  # FastAPI web server
│
├── prompts/
│   ├── profile_extract.md      # Round 1: skill/experience extraction
│   ├── profile_perspectives.md # Round 2: HR/HM/strategist analysis
│   ├── profile_analyzer.md     # Round 3: output Top-10 positions
│   ├── matcher.md              # 6-dimension scoring instructions
│   ├── tailor_plan.md          # Round 1 (DeepSeek): gap analysis + plan
│   ├── tailor.md               # Round 2 (Claude Opus): execute plan
│   ├── cover_letter.md         # Cover letter generation
│   ├── trends.md               # Market trend analysis
│   └── refine_prompt.txt       # HR/HM perspective rules for interactive refinement
│
├── scripts/
│   ├── daily.sh                # Cron entry: run collect + match + digest daily
│   └── weekly.sh               # Cron entry: run trends report weekly
│
└── data/
    ├── resume/                 # Your resume file(s) — PDF/DOCX/MD/TXT
    │   └── _profile.json       # (auto-generated) Profile analysis result
    │
    ├── materials/              # Personal background materials
    │   ├── articles/           # Technical articles you've published
    │   ├── papers/             # Research papers, patents
    │   ├── projects/           # Project writeups, case studies
    │   └── portfolio/          # Portfolio links, code samples
    │
    ├── jobs/                   # Raw job JSON files (if needed for debugging)
    │
    ├── outputs/                # Generated artifacts
    │   ├── *_resume.md         # Tailored resume (markdown source, editable)
    │   ├── *_resume.pdf        # Tailored resume (PDF for submission)
    │   ├── *_cover_letter.txt  # Generated cover letter
    │   ├── digest_*.html       # Daily digest emails
    │   └── trends_*.md/.html   # Market trend reports
    │
    ├── cookies/                # (git-ignored) Platform session cookies
    │   ├── linkedin.json
    │   ├── indeed.json
    │   └── ...
    │
    └── jobs.db                 # SQLite database (all job records + scoring)
```

---

## 6-Dimension Match Scoring

Inspired by [DailyJobMatch](https://github.com/chunxubioinfor/DailyJobMatch), each job is scored across six dimensions:

| Dimension | Max Points | Evaluation Criteria |
|-----------|------------|-------------------|
| **Background Match** | 10 | Industry/domain fit (startup vs. enterprise, ML vs. systems, etc.) |
| **Skills Overlap** | 30 | Intersection of required tech stack with resume skills (highest weight) |
| **Experience Relevance** | 30 | How well your past projects/roles map to job responsibilities (highest weight) |
| **Seniority** | 10 | Level alignment (junior, mid, senior, staff) and growth trajectory |
| **Work Authorization** | 10 | Visa sponsorship needs, US work eligibility, or remote policy |
| **Company Type** | 10 | Preference match (startup, mid-size, large, public, funded stage) |
| **OVERALL** | **100** | Weighted sum of above six |

**Matcher also returns**:
- **Keywords**: High-frequency JD terms for ATS optimization
- **Fit Bullets**: 3-5 specific reasons why you're a strong match (reusable in cover letter)
- **Connector**: One-sentence opening hook for cover letter
- **Gaps**: Honest assessment of skill/experience gaps

Run `show --job-id N` to see the breakdown of all six scores.

---

## Resume Tailoring Pipeline

### Two-Round Approach

**Round 1 (DeepSeek, fast & cheap)**
- Input: Original resume + JD + materials library
- Output: Structured gap analysis + modification plan
  - JD keywords (ATS critical)
  - Strong matches in your background
  - Honest gaps
  - Hidden strengths from materials
  - Specific rewrite instructions for each section
- Cost: ~$0.02 per job

**Round 2 (Claude Opus, high quality)**
- Input: Plan from Round 1 + original resume + materials library
- Output: Final customized resume (markdown)
  - Reordered sections based on job relevance
  - Rewritten bullets with JD keywords naturally injected
  - Skills section prioritized for ATS
  - Preserved all hyperlinks (GitHub, portfolio, papers)
- Cost: ~$0.05 per job

Outputs:
- `{job_id:03d}_{company}_{title}_resume.md` — Editable markdown source (includes plan comments)
- `{job_id:03d}_{company}_{title}_resume.pdf` — PDF ready for submission

### Materials Library

Place supporting documents in `data/materials/`:
- Technical articles you've written
- Research papers, patents
- Project case studies
- Blog posts, publications

The tailoring pipeline automatically reads and incorporates relevant materials when crafting resume points.

---

## Profile Analysis (3-Round Pipeline)

The `analyze-profile` command runs a sophisticated 3-round dialogue to extract your career positioning:

**Round 1 — Extraction**
- Parses resume + materials library
- Extracts: technical skills, projects, leadership, industry experience, unique specializations
- Builds: ATS keyword pool, experience summary

**Round 2 — Multi-Perspective Analysis**
- HR perspective: What jumps out in 6 seconds? ATS keywords, growth signals?
- Hiring Manager perspective: Technical credibility, system design depth, unique strengths?
- Strategist perspective: Career trajectory, positioning, underutilized strengths?

**Round 3 — Top-10 Positions**
- Synthesizes all perspectives
- Outputs: Top 10 roles you should target
  - **Primary title**: Main role (e.g., "Machine Learning Engineer")
  - **Aliases**: Alternative names (e.g., "ML Engineer", "Software Engineer, ML")
  - **Broader terms**: Related searches (e.g., "AI Engineer", "Data Scientist")
  - **Direction**: Career path (IC Track, Management Track, Research)
  - **Scores**: Market demand, competition level, your advantage fit
  - **Why this position**: Specific reasons from your background
  - **LinkedIn direct link**: Filtered job search for that title

The output (stored in `data/resume/_profile.json`) guides all subsequent job collection. The system uses the primary titles + aliases + broader_terms as search keywords across all platforms.

---

## Job Collection Strategy

### Platform-Specific Configuration

Each platform has its own cost model and scraping strategy:

```yaml
linkedin:
  Strategy: Per-title Apify search (harvestapi/linkedin-job-search)
  Cost: $1 per 1000 jobs (~$0.015 per title)
  Coverage: 15 results/title × 40 titles = 600 raw → ~400 deduped
  Est. cost/run: $0.60

indeed:
  Strategy: Apify (misceres/indeed-scraper)
  Cost: $5 per 1000 jobs (~$0.005 per job)
  Coverage: 12 jobs/run
  Est. cost/run: $0.06

yc:
  Strategy: Apify (artemlazarevm/yc-jobs-scraper)
  Coverage: 15 jobs/run
  Cost: $0 or subscription

dice:
  Strategy: Apify (worldunboxer/dice-jobs-scraper)
  Coverage: 15 jobs/run

hackernews:
  Strategy: Direct Firebase + Algolia API (no auth needed)
  Cost: $0 (free tier)
  Coverage: 30 jobs/run
```

### Deduplication Strategy

Three-layer deduplication ensures no duplicates in your tracking database:

1. **Exact platform match**: Same source + external_id
2. **URL match**: Same job posting URL (cross-platform)
3. **Content hash**: Normalized company + title + location
   - Handles: "ML Engineer" vs "Machine Learning Engineer"
   - Handles: "John Doe Inc" vs "John Doe, Inc"
   - Handles: "San Francisco, CA" vs "SF, California"

---

## Web UI

Start the FastAPI web server:
```bash
python -m src.main web --host 0.0.0.0 --port 8765
```

**Features**:
- Browse all collected jobs with filtering and sorting
- Click to view detailed 6-dimension scoring
- Interactive "Refine Resume" chat: make targeted improvements without re-tailoring
- Version history: each refinement creates a new `ResumeRevision` entry
- One-click PDF export
- Health check endpoints for monitoring

**Endpoints**:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Basic server health |
| `/health/liveness` | GET | Pod liveness probe (Kubernetes) |
| `/health/readiness` | GET | Pod readiness probe (Kubernetes) |
| `/health/agents` | GET | Per-agent heartbeat status (collection, matching, etc.) |
| `/job/add` | POST | Ingest a new job manually (JSON or form) |
| `/job/{id}/refine` | POST | Send chat message for interactive resume refinement |
| `/job/{id}/refine/history` | GET | Retrieve chat history and revisions |

---

## Configuration Reference

### `config.yaml` — Preferences Section

```yaml
preferences:
  # Job titles to search (used if no profile.json exists)
  job_titles:
    - Machine Learning Engineer
    - ML Infrastructure Engineer
    - AI Engineer
    - Software Engineer Machine Learning

  # Geographic scope
  locations:
    - United States

  # Employment type filter
  job_types:
    - Full-time

  # Company size preferences (for scoring weights)
  company_size:
    - Startup
    - Mid-size
    - Large

  # Technical skills that boost scoring
  must_have_skills:
    - Python

  nice_to_have_skills:
    - PyTorch
    - Kubernetes
    - LLM
    - CUDA
    - vLLM

  # Keywords that auto-filter jobs during collection (saves LLM cost)
  exclude_keywords:
    - Senior Director
    - intern
    - internship
    - student
    - postdoc
    - contractor only

  # Experience filtering
  years_of_experience:
    min: 0
    max: 8

  # Minimum salary (optional, for filtering)
  min_salary_usd: 80000

# Scoring thresholds
scoring:
  min_recommend_score: 70      # Jobs below this won't get tailored resume
  auto_archive_below: 40       # Auto-archive very low matches

# Job freshness (only collect recent postings)
freshness:
  max_age_hours: 24            # Only jobs from last 24 hours

# Daily digest email
digest:
  enabled: true
  top_n: 15                    # Show top 15 matches
  to: your-email@example.com
  smtp:
    host: smtp.gmail.com
    port: 587
    username: your-gmail@gmail.com
    password_env: SMTP_PASSWORD
```

---

## Cost Estimation

**Per daily run** (assuming all platforms enabled):
- LinkedIn: 40 titles × $0.015 = $0.60
- Indeed: $0.06
- Dice: Negligible
- YC: Free (or subscription)
- HackerNews: Free
- **DeepSeek matching**: ~¥0.5–1 (1-200 jobs × cheap model)
- **Total**: ~$0.70–1.00 per day

**Per tailored resume** (when you apply):
- Round 1 (DeepSeek plan): ~$0.02
- Round 2 (Claude Opus): ~$0.05
- **Total**: ~$0.07 per application

**Monthly**: ~$25–40 for collection + matching + tailoring

---

## Privacy & Security

- **Resume content**: Sent to DeepSeek/Claude APIs only during tailoring and profile analysis. Not logged or stored by the service providers beyond normal API logs.
- **Cookies**: Stored locally in `data/cookies/` (git-ignored). Never transmitted to third parties.
- **Database**: SQLite file, local only. No cloud sync.
- **Credentials**: Loaded from `.env` (git-ignored). Never logged.
- **Platform ToS**: This tool respects rate limits and login-based scraping. Use responsibly.

---

## Important Notes

### What This Tool Does NOT Do

- ❌ Auto-fill application forms
- ❌ Auto-submit applications
- ❌ Delete or modify your applications
- ❌ Generate dishonest resume claims
- ❌ Spam applications

By design, you review and manually submit each application. The tool helps you find and customize, but the final decision is yours.

### Maintaining Collector Health

Job site selectors change frequently. If a collector fails:
1. Check the console error message (usually selector mismatch)
2. Open the platform's job page in a browser
3. Inspect the DOM to find the updated selectors
4. Update the collector file (`src/collectors/*.py`)
5. Test with `python -m src.main collect --platform <name>`

### When to Re-analyze Profile

Run `analyze-profile --force` when:
- Your resume changes significantly
- You want to pivot to a different career direction
- You've completed major projects you want to highlight

---

## Example Workflow

```bash
# Day 1: Initial setup
python -m src.main init
python -m src.main parse-resume
python -m src.main analyze-profile
python -m src.main login --platform linkedin
python -m src.main login --platform indeed

# Day 2: Collect and score jobs
python -m src.main collect
python -m src.main match
python -m src.main list --min-score 70

# Day 3: Deep dive into promising jobs
python -m src.main show --job-id 5
python -m src.main show --job-id 8
python -m src.main show --job-id 12

# Day 4: Tailor and apply
python -m src.main tailor --job-id 5
# Download resume PDF, visit company website, apply manually
python -m src.main mark-applied --job-id 5 --note "Applied via careers page"
python -m src.main mark-applied --job-id 8 --note "Applied via careers page"

# Day 7: Weekly trends
python -m src.main trends --email

# Automated: Add to crontab
# 30 7 * * *  /path/to/job-agent/scripts/daily.sh >> /tmp/job-agent.log 2>&1
```

---

## Development

### Running Tests (if available)
```bash
pytest tests/
```

### Adding a New Collector

1. Create `src/collectors/myplatform_apify.py` (inherit from `ApifyCollector`)
2. Register in `src/collectors/__init__.py`
3. Add to `config.yaml` under `collectors:`
4. Test with `python -m src.main collect --platform myplatform`

### Customizing Prompts

Edit prompt files in `prompts/` and reload:
```bash
python -m src.main collect  # Uses cached prompts, but edits will reload on next run
```

---

## Troubleshooting

**Q: Collector returns no results**
- A: Check if platform is enabled in `config.yaml`. Verify API tokens (Apify). If using browser-based collector, check if selectors still match the site's DOM.

**Q: Scores seem off**
- A: Run `show --job-id N` to see 6D breakdown. Review `match_summary` and `match_gaps` for matcher's reasoning. The scoring is deterministic — same JD gets same score.

**Q: Tailored resume doesn't include my keywords**
- A: Check `data/materials/` has your relevant documents. The 2-round pipeline tries to pull supporting evidence from materials. Also verify the JD was fully passed to the prompt.

**Q: Email digest not sending**
- A: Check `.env` has `SMTP_PASSWORD`. Verify Gmail account has "Less secure apps" enabled or uses an app password. Test with `python -m src.main digest --email`.

**Q: Database locked errors**
- A: Close any open connections and try again. SQLite doesn't handle concurrent writers well. Consider running `collect`, `match`, etc. serially, not in parallel.

**Q: API key errors**
- A: Verify `.env` has correct `DEEPSEEK_API_KEY` (and `ANTHROPIC_API_KEY` if using Claude). Test connectivity with a simple call.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes and test
4. Submit a pull request

---

## Acknowledgments

- Inspired by [DailyJobMatch](https://github.com/chunxubioinfor/DailyJobMatch) — open-source job matching approach
- Built with [LangGraph](https://python.langchain.com/docs/langgraph/) for multi-agent orchestration
- Uses [DeepSeek](https://www.deepseek.com/) for cost-effective LLM operations
- Uses [Claude](https://www.anthropic.com/claude) for quality-critical tailoring
- Web scraping powered by [Apify](https://apify.com/) and [Playwright](https://playwright.dev/)

---

**Last Updated**: May 2026

For questions or issues, open a GitHub issue or reach out directly.
