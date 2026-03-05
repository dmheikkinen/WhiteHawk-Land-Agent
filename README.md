# Ohio Landman Pipeline

An AI-powered contact harvesting and scoring pipeline for Ohio minerals and royalties deal sourcing. Covers **Belmont, Monroe, Jefferson, Carroll, Harrison, Guernsey, Noble, and Washington Counties** — and the broader Appalachian Basin (Utica / Marcellus shale plays).

---

## What It Does

| Phase | Script | AI cost | Output |
|---|---|---|---|
| **1 — Harvest** | `harvest_contacts.py` | SerpAPI credits only | `raw_hits.jsonl`, `candidates.csv` |
| **LinkedIn Seed** *(optional)* | Web UI upload | Free | Merges your network into Phase 2 |
| **2 — Score** | `score_contacts.py` | ~$0.01–0.05 / run | `ohio_landman_contacts_<ts>.csv` |
| **3 — Orchestrate** *(optional)* | `run_ohio_landman_assistant.py` | GPT-4.1 costs | `ohio_contacts_<ts>.csv` |

Phase 1 runs 80+ targeted SerpAPI queries and 10 SEC EDGAR searches with no OpenAI calls. Phase 2 uses the cheap `gpt-4o-mini` model to classify and tier-rank candidates. Phase 3 is an optional deep-enrichment pass using a GPT-4.1 Assistants API agent.

---

## Prerequisites

| Requirement | Version | Where to get it |
|---|---|---|
| Python | 3.11 or 3.12 | [python.org](https://www.python.org/downloads/) |
| OpenAI API key | — | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| SerpAPI key | — | [serpapi.com/manage-api-key](https://serpapi.com/manage-api-key) |

Docker is optional (for VM deployment).

---

## Quick Start — Local

### Windows (PowerShell) — one command

```powershell
.\start.ps1
```

`start.ps1` will:
1. Create a `.venv` virtual environment if one doesn't exist
2. Install all dependencies from `requirements.txt`
3. Copy `.env.example` → `.env` on first run and **exit**, prompting you to fill in your API keys
4. On subsequent runs: load `.env`, check for required keys, then launch the web UI

**After the first run**, open `.env` in any text editor, fill in your keys, then run `.\start.ps1` again.

### macOS / Linux — one command

```bash
make setup    # creates .venv, installs deps, scaffolds .env
```

Then open `.env`, fill in your API keys, and:

```bash
make run      # starts the web UI at http://127.0.0.1:5000
```

### Manual steps (any OS)

```bash
# 1. Create virtual environment
python3 -m venv .venv

# 2. Activate it
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows PowerShell

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure secrets
cp .env.example .env
# Open .env and fill in OPENAI_API_KEY and SERPAPI_API_KEY

# 5. Start the web UI
uvicorn server:app --host 127.0.0.1 --port 5000 --reload
```

Open **http://127.0.0.1:5000** in your browser.

---

## Configuration

### API keys

Copy `.env.example` to `.env` and set the two required values:

```
OPENAI_API_KEY=sk-proj-...
SERPAPI_API_KEY=...
```

Keys are read from the OS environment at startup. They are **never** written to `config.json`, never logged, and never sent to the browser — the Setup page only shows ✓ / ✗.

### Pipeline settings

All non-secret settings (scoring model, batch size, score threshold, assistant ID) are configured through the **Setup** page in the web UI and saved to `config.json`.

### Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | Phase 2 & 3 | — | OpenAI API key |
| `SERPAPI_API_KEY` | Phase 1 | — | SerpAPI key for Google search |
| `ASSISTANT_ID` | No | bundled | Override the Phase 3 assistant |
| `TARGET_CONTACTS` | No | `50` | Contacts requested from Phase 3 |
| `SCORE_MODEL` | No | `gpt-4o-mini` | Model used for Phase 2 scoring |
| `BATCH_SIZE` | No | `15` | Candidates per AI call (hard cap: 20) |
| `MIN_SCORE` | No | `4.0` | Drop contacts below this relevance score |
| `MAX_SNIPPET_CHARS` | No | `300` | Truncate snippets before sending to AI |
| `CONSERVATIVE_MODE` | No | `0` | Set `1` to halve searches and slow delays |
| `DRY_RUN` | No | `0` | Set `1` to print plan without API calls |

---

## Using the Web UI

### Setup page (`/setup`)
- Shows which API keys are set (✓ / ✗)
- Configure scoring model, batch size, score threshold
- Copy/paste commands for setting keys in PowerShell or bash

### Run page (`/run`)
- **Run Mode panel** — toggle **Conservative** (fewer results, cheaper) or **Dry Run** (no API calls)
- **Phase 1 — Harvest** — runs 80+ web queries and 10 EDGAR searches
- **LinkedIn Seed** *(optional)* — upload `linkedin_connections.csv`; only oil & gas / minerals matches are kept and merged into Phase 2 automatically
- **Phase 2 — Score** — batches candidates through `gpt-4o-mini`; LinkedIn seed is included automatically if present
- **Phase 3 — Orchestrate** *(optional)* — GPT-4.1 deep enrichment pass

All phases stream live logs to the browser via Server-Sent Events.

### Downloads page (`/downloads`)
- Browse and download all output files
- **Reset Data** — archives current files to `data/archive/<timestamp>/` for a fresh start

---

## CLI Usage

You can run each phase directly without the web UI:

```bash
# Phase 1
python harvest_contacts.py

# Phase 2 (pass the candidates file)
python score_contacts.py data/candidates.csv

# Phase 3 (optional — use a scored CSV as starting context)
python run_ohio_landman_assistant.py \
  --candidates data/ohio_landman_contacts_<timestamp>.csv \
  --target 75

# Dry run — validate config without any API calls
DRY_RUN=1 python harvest_contacts.py
DRY_RUN=1 python score_contacts.py data/candidates.csv
python run_ohio_landman_assistant.py --dry-run

# Conservative mode (halves SerpAPI pages, longer delays)
CONSERVATIVE_MODE=1 python harvest_contacts.py
```

---

## Make Targets (macOS / Linux)

```
make setup        First-time setup: venv + deps + .env scaffold
make run          Start the web UI (reads .env automatically)
make harvest      Run Phase 1 from CLI
make score        Run Phase 2 from CLI
make orchestrate  Run Phase 3 from CLI
make dry-run      Dry-run all three phases (no API calls)
make docker-build Build the Docker image
make docker-run   Run in Docker (reads .env, mounts ./data)
make docker-up    Start with Docker Compose (detached)
make docker-down  Stop Docker Compose stack
make clean        Remove __pycache__ and .pyc files
make help         Show this list
```

---

## Deploying to a Single VM

### Option A — systemd (no Docker)

Tested on Ubuntu 22.04 LTS. 1 vCPU / 1 GB RAM is sufficient.

**1. Install Python 3.11**
```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv
```

**2. Deploy the code** (without `.env` — never commit your keys)
```bash
git clone https://github.com/your-org/ohio-landman-agent /opt/ohio-landman
cd /opt/ohio-landman
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p data
```

**3. Create the systemd unit — secrets live here, not in the repo**
```bash
sudo tee /etc/systemd/system/ohio-landman.service > /dev/null << 'EOF'
[Unit]
Description=Ohio Landman Pipeline
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/ohio-landman
Environment="OPENAI_API_KEY=sk-proj-YOUR_KEY_HERE"
Environment="SERPAPI_API_KEY=YOUR_KEY_HERE"
ExecStart=/opt/ohio-landman/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 5000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo chmod 600 /etc/systemd/system/ohio-landman.service
sudo systemctl daemon-reload
sudo systemctl enable --now ohio-landman
sudo systemctl status ohio-landman
```

**4. Put nginx in front** (TLS + public access; uvicorn stays on localhost)
```bash
sudo apt install -y nginx certbot python3-certbot-nginx

sudo tee /etc/nginx/sites-available/ohio-landman > /dev/null << 'EOF'
server {
    listen 80;
    server_name your-domain.example.com;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;

        # Required for SSE (live log streaming)
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/ohio-landman /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your-domain.example.com
```

**5. Updates**
```bash
cd /opt/ohio-landman
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart ohio-landman
```

---

### Option B — Docker Compose

**1. Install Docker**
```bash
curl -fsSL https://get.docker.com | sh
```

**2. Deploy the code**
```bash
git clone https://github.com/your-org/ohio-landman-agent /opt/ohio-landman
cd /opt/ohio-landman
```

**3. Create `.env` on the server** (never in the repo)
```bash
cp .env.example .env
nano .env    # fill in OPENAI_API_KEY and SERPAPI_API_KEY
```

**4. Launch**
```bash
docker compose up -d
```

Output files appear in `./data/` (mounted volume — persists container restarts).

**5. Put nginx in front** — same config as Option A, step 4.

**6. Updates**
```bash
git pull
docker compose up -d --build
```

---

## Security Notes

| Concern | Mitigation |
|---|---|
| API keys in source control | `.env` is in `.gitignore`; keys are never written to `config.json` |
| Keys visible in the browser | Setup page shows ✓ / ✗ status only — values are never sent to the frontend |
| Keys in Docker image layers | `--env-file .env` / `env_file:` in Compose passes keys at runtime, not build time |
| Keys in systemd unit file | Use `chmod 600` on the unit file; only root can read it |
| Uvicorn exposed to the internet | Always bind to `127.0.0.1` and use nginx (with TLS) for public access |
| Output CSVs publicly accessible | nginx config proxies only to uvicorn — `data/` is never served as static files |

---

## Project Structure

```
ohio_landman_agent/
├── server.py                      # FastAPI web UI (Setup / Run / Downloads)
├── harvest_contacts.py            # Phase 1 — SerpAPI + EDGAR harvester
├── score_contacts.py              # Phase 2 — gpt-4o-mini batch scorer
├── run_ohio_landman_assistant.py  # Phase 3 — GPT-4.1 assistant orchestrator
├── templates/
│   ├── base.html
│   ├── setup.html
│   ├── run.html
│   └── downloads.html
├── data/                          # Runtime outputs (gitignored)
│   ├── raw_hits.jsonl
│   ├── candidates.csv
│   ├── linkedin_seed.csv
│   └── ohio_landman_contacts_<ts>.csv
├── requirements.txt
├── .env.example                   # Safe to commit — no real values
├── .env                           # NOT committed — your real secrets
├── Makefile                       # macOS / Linux shortcuts
├── start.ps1                      # Windows PowerShell quick-start
├── Dockerfile
└── docker-compose.yml
```
