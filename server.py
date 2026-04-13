"""server.py — Landman Pipeline Web UI (FastAPI + Jinja2)

Three pages:
  /setup       — Check & set API keys; configure pipeline settings
  /run         — Run Harvest, Score, Orchestrate with live logs
  /downloads   — Browse and download output files; archive old data

API keys are read from OS environment variables — never stored server-side.
Non-secret settings (model, batch size, etc.) are stored in config.json.

Run:
    pip install "fastapi[all]"
    python -m uvicorn server:app --port 5000 --reload

Then open http://127.0.0.1:5000
"""

import asyncio
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
CONFIG_FILE = BASE_DIR / "config.json"

DATA_DIR.mkdir(exist_ok=True)

SCRIPTS = {
    "harvest":      BASE_DIR / "harvest_contacts.py",
    "score":        BASE_DIR / "score_contacts.py",
    "orchestrator": BASE_DIR / "run_ohio_landman_assistant.py",
}

# ---------------------------------------------------------------------------
# LinkedIn seed  (upload + parse)
# ---------------------------------------------------------------------------
# Keywords searched in Position + Company fields (case-insensitive).
SEED_KEYWORDS = {
    "landman", "land man", "mineral", "minerals", "royalt", "acquisition",
    "utica", "marcellus", "appalachia", "appalachian", "oil & gas",
    "oil and gas", "o&g", "leasing", "upstream", "e&p", "exploration",
    "petroleum", "wellbore", "completions", "midstream",
}

# Columns written to linkedin_seed.csv — must match harvest_contacts.py CANDIDATE_FIELDS
LINKEDIN_CANDIDATE_FIELDS = [
    "candidate_id", "candidate_type", "display_name", "entity_name",
    "state", "county", "source_type", "parcel_context",
    "edgar_hit", "file_date", "form_type", "hit_count",
    "source_urls", "evidence_snippets", "query_families",
    "queries", "sources", "first_seen_at",
]


def _linkedin_matches(position: str, company: str) -> bool:
    text = f"{position} {company}".lower()
    return any(kw in text for kw in SEED_KEYWORDS)


def parse_linkedin_csv(content: str) -> tuple[list[dict], int]:
    """
    Parse a LinkedIn connections CSV export.

    LinkedIn export columns (as of 2024):
      First Name, Last Name, URL, Email Address, Company, Position, Connected On

    Returns (filtered_rows_as_candidate_dicts, total_connections_checked).
    """
    reader = csv.DictReader(io.StringIO(content))
    total = 0
    rows: list[dict] = []
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in reader:
        # LinkedIn sometimes uses different capitalizations
        first    = (row.get("First Name") or row.get("first name") or "").strip()
        last     = (row.get("Last Name")  or row.get("last name")  or "").strip()
        position = (row.get("Position")   or row.get("position")   or "").strip()
        company  = (row.get("Company")    or row.get("company")    or "").strip()
        url      = (row.get("URL")        or row.get("url")        or "").strip()

        if not (first or last or company):
            continue          # skip malformed rows

        total += 1

        if not _linkedin_matches(position, company):
            continue          # keyword filter

        name    = f"{first} {last}".strip()
        cid     = "li_" + hashlib.md5(f"{name}|{company}".encode()).hexdigest()[:8]
        snippet = f"{position} at {company} (LinkedIn connection)".strip(" at")

        cfg = load_config()
        rows.append({
            "candidate_id":       cid,
            "candidate_type":     "linkedin_seed",
            "display_name":       name,
            "entity_name":        company,
            "state":              cfg.get("geo_state",  ""),
            "county":             cfg.get("geo_county", ""),
            "source_type":        "linkedin_seed",
            "parcel_context":     "",
            "edgar_hit":          "False",
            "file_date":          "",
            "form_type":          "",
            "hit_count":          "1",
            "source_urls":        url,
            "evidence_snippets":  snippet,
            "query_families":     "linkedin",
            "queries":            "",
            "sources":            "linkedin",
            "first_seen_at":      ts,
        })

    return rows, total

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Landman Pipeline", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------------------------
# Config  (non-secret only — API keys stay in the OS environment)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "assistant_id":    "asst_EFzDaNzm7hT8bz1InWgAhwbT",
    "target_contacts": 50,
    "score_model":     "gpt-4o-mini",
    "batch_size":      20,
    "min_score":       4.0,
    # Geography
    "geo_state":       "Ohio",
    "geo_county":      "Belmont",
    "geo_region":      "",
    "geo_basin":       "Appalachian Basin",
    # Source class toggles (all on by default except county_tax)
    "source_google":      True,
    "source_marketplaces": True,
    "source_linkedin":    True,
    "source_sec":         True,
    "source_county_tax":  False,
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(data: dict) -> None:
    """Save only non-secret keys."""
    safe = {k: v for k, v in data.items() if "key" not in k.lower()}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(safe, f, indent=2)


# ---------------------------------------------------------------------------
# Environment-variable status  (values are never exposed)
# ---------------------------------------------------------------------------
ENV_DESCRIPTIONS = {
    "OPENAI_API_KEY":  "Needed for Phase 2 (Score) and Phase 3 (Orchestrate)",
    "SERPAPI_API_KEY": "Needed for Phase 1 (Harvest) — web search",
}


def env_status() -> dict[str, bool]:
    return {k: bool(os.environ.get(k, "").strip()) for k in ENV_DESCRIPTIONS}


def harvest_ok()      -> bool: return bool(os.environ.get("SERPAPI_API_KEY"))
def score_ok()        -> bool: return bool(os.environ.get("OPENAI_API_KEY")) and (DATA_DIR / "candidates.csv").exists()
def orchestrator_ok() -> bool: return bool(os.environ.get("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# Job management  (in-memory; output files persist across restarts)
# ---------------------------------------------------------------------------
jobs: dict[str, dict] = {}
latest_jobs: dict[str, str] = {}   # phase -> job_id


def _new_job(phase: str, label: str) -> str:
    jid = str(uuid.uuid4())[:8]
    jobs[jid] = {
        "id": jid, "phase": phase, "label": label,
        "status": "running", "logs": [],
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None, "return_code": None,
    }
    latest_jobs[phase] = jid
    return jid


def _run_subprocess(jid: str, cmd: list, env: dict) -> None:
    job = jobs[jid]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, cwd=str(BASE_DIR),
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        for line in proc.stdout:
            job["logs"].append(line.rstrip())
        proc.wait()
        job["return_code"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        job["logs"].append(f"[ERROR] {exc}")
        job["status"] = "error"
    finally:
        job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def start_job(phase: str, label: str, cmd: list, env: dict) -> str:
    jid = _new_job(phase, label)
    threading.Thread(target=_run_subprocess, args=(jid, cmd, env), daemon=True).start()
    return jid


def _base_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
OUTPUT_EXTS = {".csv", ".jsonl", ".txt"}


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n //= 1024
    return f"{n} GB"


def _count_lines(p: Path) -> int:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def list_data_files() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    files = []
    for p in sorted(DATA_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file() or p.suffix not in OUTPUT_EXTS:
            continue
        stat = p.stat()
        files.append({
            "name": p.name,
            "ext":  p.suffix.lstrip(".").upper(),
            "size_human": _human_size(stat.st_size),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return files


def pipeline_state() -> dict:
    raw  = DATA_DIR / "raw_hits.jsonl"
    cand = DATA_DIR / "candidates.csv"
    seed = DATA_DIR / "linkedin_seed.csv"
    # Glob both new and legacy output patterns for backwards compatibility
    scored   = sorted(
        list(DATA_DIR.glob("landman_contacts_*.csv")) +
        list(DATA_DIR.glob("ohio_landman_contacts_*.csv")),
        reverse=True,
    )
    contacts = sorted(DATA_DIR.glob("ohio_contacts_*.csv"), reverse=True)
    all_out  = scored + contacts
    return {
        "raw_hits_exists":       raw.exists(),
        "raw_hits_lines":        _count_lines(raw) if raw.exists() else 0,
        "candidates_exists":     cand.exists(),
        "candidates_rows":       max(0, _count_lines(cand) - 1) if cand.exists() else 0,
        "linkedin_seed_exists":  seed.exists(),
        "linkedin_seed_count":   max(0, _count_lines(seed) - 1) if seed.exists() else 0,
        "scored_files":          [f.name for f in scored],
        "contact_files":         [f.name for f in contacts],
        "latest_output":         all_out[0].name if all_out else None,
        "all_scored_names":      [f.name for f in all_out],
    }


def archive_data() -> tuple[str, int]:
    """Move current data files into data/archive/<timestamp>/."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for p in DATA_DIR.iterdir():
        if p.is_file() and p.suffix in OUTPUT_EXTS:
            shutil.move(str(p), str(dest / p.name))
            moved += 1
    return ts, moved


# ---------------------------------------------------------------------------
# Jinja2 global helpers
# ---------------------------------------------------------------------------
@app.middleware("http")
async def inject_globals(request: Request, call_next):
    response = await call_next(request)
    return response


def _tpl(name: str, request: Request, ctx: dict) -> HTMLResponse:
    ctx.setdefault("env", env_status())
    ctx.setdefault("ps", pipeline_state())
    ctx.setdefault("cfg", load_config())
    ctx["request"] = request
    return templates.TemplateResponse(name, ctx)


# ---------------------------------------------------------------------------
# Page 1 — Setup  /setup
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/setup")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, saved: str = ""):
    return _tpl("setup.html", request, {
        "saved": saved == "1",
        "env_descriptions": ENV_DESCRIPTIONS,
    })


@app.post("/setup/save")
async def setup_save(
    assistant_id:    str   = Form(""),
    target_contacts: int   = Form(50),
    score_model:     str   = Form("gpt-4o-mini"),
    batch_size:      int   = Form(20),
    min_score:       float = Form(4.0),
    # Geography
    geo_state:       str   = Form("Ohio"),
    geo_county:      str   = Form("Belmont"),
    geo_region:      str   = Form(""),
    geo_basin:       str   = Form(""),
    # Source class toggles (checkbox — present = on, absent = off)
    source_google:      str = Form(""),
    source_marketplaces: str = Form(""),
    source_linkedin:    str = Form(""),
    source_sec:         str = Form(""),
    source_county_tax:  str = Form(""),
):
    cfg = load_config()
    cfg.update({
        "assistant_id":    assistant_id.strip() or cfg["assistant_id"],
        "target_contacts": target_contacts,
        "score_model":     score_model,
        "batch_size":      batch_size,
        "min_score":       min_score,
        "geo_state":       geo_state.strip()  or "Ohio",
        "geo_county":      geo_county.strip() or "Belmont",
        "geo_region":      geo_region.strip(),
        "geo_basin":       geo_basin.strip(),
        "source_google":      bool(source_google),
        "source_marketplaces": bool(source_marketplaces),
        "source_linkedin":    bool(source_linkedin),
        "source_sec":         bool(source_sec),
        "source_county_tax":  bool(source_county_tax),
    })
    save_config(cfg)
    return RedirectResponse("/setup?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Page 2 — Run  /run
# ---------------------------------------------------------------------------

@app.get("/run", response_class=HTMLResponse)
async def run_page(
    request: Request,
    linkedin_ok:    str = "",
    linkedin_total: str = "",
    linkedin_warn:  str = "",
):
    phase_jobs = {p: jobs.get(latest_jobs.get(p)) for p in ("harvest", "score", "orchestrator")}
    return _tpl("run.html", request, {
        "phase_jobs":       phase_jobs,
        "harvest_ok":       harvest_ok(),
        "score_ok":         score_ok(),
        "orchestrator_ok":  orchestrator_ok(),
        "linkedin_ok":      int(linkedin_ok)    if linkedin_ok.isdigit()    else None,
        "linkedin_total":   int(linkedin_total) if linkedin_total.isdigit() else None,
        "linkedin_warn":    linkedin_warn,
    })


@app.post("/run/harvest")
async def run_harvest(conservative: str = Form(""), dry_run: str = Form("")):
    cfg = load_config()
    DATA_DIR.mkdir(exist_ok=True)
    extra: dict = {
        "RAW_HITS_FILE":        str(DATA_DIR / "raw_hits.jsonl"),
        "CANDIDATES_FILE":      str(DATA_DIR / "candidates.csv"),
        # Geography
        "GEO_STATE":            cfg.get("geo_state",  "Ohio"),
        "GEO_COUNTY":           cfg.get("geo_county", "Belmont"),
        "GEO_REGION":           cfg.get("geo_region", ""),
        "GEO_BASIN":            cfg.get("geo_basin",  "Appalachian Basin"),
        # Source class toggles
        "SOURCE_GOOGLE":        "1" if cfg.get("source_google",      True)  else "0",
        "SOURCE_MARKETPLACES":  "1" if cfg.get("source_marketplaces", True) else "0",
        "SOURCE_LINKEDIN":      "1" if cfg.get("source_linkedin",    True)  else "0",
        "SOURCE_SEC":           "1" if cfg.get("source_sec",         True)  else "0",
        "SOURCE_COUNTY_TAX":    "1" if cfg.get("source_county_tax",  False) else "0",
    }
    if conservative:
        extra["CONSERVATIVE_MODE"] = "1"
    if dry_run:
        extra["DRY_RUN"] = "1"
    env = _base_env(extra)
    jid = start_job("harvest", "Phase 1 — Harvest", [sys.executable, "-u", str(SCRIPTS["harvest"])], env)
    return RedirectResponse(f"/run?job={jid}", status_code=303)


@app.post("/run/score")
async def run_score(conservative: str = Form(""), dry_run: str = Form("")):
    cfg = load_config()
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    DATA_DIR.mkdir(exist_ok=True)
    extra: dict = {
        "SCORE_MODEL":  cfg["score_model"],
        "BATCH_SIZE":   cfg["batch_size"],
        "MIN_SCORE":    cfg["min_score"],
        "OUTPUT_FILE":  str(DATA_DIR / f"landman_contacts_{ts}.csv"),
        # Geography
        "GEO_STATE":    cfg.get("geo_state",  "Ohio"),
        "GEO_COUNTY":   cfg.get("geo_county", "Belmont"),
        "GEO_REGION":   cfg.get("geo_region", ""),
        "GEO_BASIN":    cfg.get("geo_basin",  "Appalachian Basin"),
    }
    if conservative:
        extra["CONSERVATIVE_MODE"] = "1"
    if dry_run:
        extra["DRY_RUN"] = "1"
    # Auto-inject LinkedIn seed when present
    seed_file = DATA_DIR / "linkedin_seed.csv"
    if seed_file.exists():
        extra["LINKEDIN_SEED_FILE"] = str(seed_file)
    env = _base_env(extra)
    cmd = [sys.executable, "-u", str(SCRIPTS["score"]), str(DATA_DIR / "candidates.csv")]
    jid = start_job("score", "Phase 2 — Score", cmd, env)
    return RedirectResponse(f"/run?job={jid}", status_code=303)


@app.post("/run/orchestrator")
async def run_orchestrator(candidates_file: str = Form(""), dry_run: str = Form("")):
    cfg = load_config()
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    DATA_DIR.mkdir(exist_ok=True)
    env = _base_env({
        "ASSISTANT_ID":    cfg.get("assistant_id", ""),
        "TARGET_CONTACTS": cfg["target_contacts"],
        "OUTPUT_FILE":     str(DATA_DIR / f"ohio_contacts_{ts}.csv"),
    })
    cmd = [sys.executable, "-u", str(SCRIPTS["orchestrator"])]
    if candidates_file:
        cmd += ["--candidates", str(DATA_DIR / candidates_file)]
    cmd += ["--target", str(cfg["target_contacts"])]
    if dry_run:
        cmd += ["--dry-run"]
    jid = start_job("orchestrator", "Phase 3 — Orchestrate", cmd, env)
    return RedirectResponse(f"/run?job={jid}", status_code=303)


# ---------------------------------------------------------------------------
# Source discovery  /discover-sources
# ---------------------------------------------------------------------------

@app.get("/discover-sources")
async def discover_sources_endpoint(state: str = "", county: str = ""):
    """Return county source bundle (tax site, assessor, etc.) from the registry."""
    import geo_queries as gq
    cfg = load_config()
    state  = state.strip()  or cfg.get("geo_state",  "Ohio")
    county = county.strip() or cfg.get("geo_county", "Belmont")
    return gq.discover_sources(state, county)


# ---------------------------------------------------------------------------
# SSE log stream  /jobs/{id}/stream
# ---------------------------------------------------------------------------

@app.get("/jobs/{jid}/stream")
async def stream_logs(jid: str):
    job = jobs.get(jid)

    if not job:
        async def _nf():
            yield "data: Job not found\n\n"
        return StreamingResponse(_nf(), media_type="text/event-stream")

    async def generate():
        sent = 0
        while True:
            log = job["logs"]
            while sent < len(log):
                yield f"data: {log[sent].replace(chr(10), ' ')}\n\n"
                sent += 1
            if job["status"] != "running":
                yield f"event: done\ndata: {job['status']}\n\n"
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/jobs/{jid}/status")
async def job_status(jid: str):
    job = jobs.get(jid)
    if not job:
        return {"error": "not found"}
    return {"status": job["status"], "log_count": len(job["logs"]),
            "return_code": job["return_code"], "finished_at": job["finished_at"]}


# ---------------------------------------------------------------------------
# LinkedIn seed  /data/linkedin-upload  /data/linkedin-clear
# ---------------------------------------------------------------------------

@app.post("/data/linkedin-upload")
async def linkedin_upload(file: UploadFile = File(...)):
    """Parse a LinkedIn connections CSV export and write linkedin_seed.csv."""
    raw_bytes = await file.read()
    # Handle UTF-8 BOM (common in LinkedIn exports on Windows)
    try:
        content = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw_bytes.decode("latin-1", errors="replace")

    rows, total = parse_linkedin_csv(content)

    if not rows:
        return RedirectResponse(
            f"/run?linkedin_warn=no_matches&linkedin_total={total}", status_code=303
        )

    DATA_DIR.mkdir(exist_ok=True)
    seed_file = DATA_DIR / "linkedin_seed.csv"
    with open(seed_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LINKEDIN_CANDIDATE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    return RedirectResponse(
        f"/run?linkedin_ok={len(rows)}&linkedin_total={total}", status_code=303
    )


@app.post("/data/linkedin-clear")
async def linkedin_clear():
    """Delete linkedin_seed.csv."""
    seed_file = DATA_DIR / "linkedin_seed.csv"
    if seed_file.exists():
        seed_file.unlink()
    return RedirectResponse("/run", status_code=303)


# ---------------------------------------------------------------------------
# Page 3 — Downloads  /downloads
# ---------------------------------------------------------------------------

@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request, archived: str = ""):
    return _tpl("downloads.html", request, {
        "files": list_data_files(),
        "archived_count": int(archived) if archived.isdigit() else None,
    })


@app.get("/files/download/{filename:path}")
async def download_file(filename: str):
    from fastapi import HTTPException
    target = (DATA_DIR / filename).resolve()
    if not target.is_relative_to(DATA_DIR.resolve()):
        raise HTTPException(status_code=403)
    if not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(str(target), filename=target.name)


@app.post("/data/reset")
async def reset_data():
    ts, count = archive_data()
    return RedirectResponse(f"/downloads?archived={count}", status_code=303)


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 60)
    print("  Ohio Landman — Web UI  (FastAPI)")
    print("  Open http://127.0.0.1:5000")
    print("=" * 60 + "\n")
    uvicorn.run("server:app", host="127.0.0.1", port=5000, reload=True)
