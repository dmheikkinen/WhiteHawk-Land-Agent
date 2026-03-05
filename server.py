"""server.py — Ohio Landman Pipeline Web UI (FastAPI + Jinja2)

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
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
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
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Ohio Landman Pipeline", docs_url=None, redoc_url=None)
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
    scored   = sorted(DATA_DIR.glob("ohio_landman_contacts_*.csv"), reverse=True)
    contacts = sorted(DATA_DIR.glob("ohio_contacts_*.csv"),          reverse=True)
    all_out  = scored + contacts
    return {
        "raw_hits_exists":  raw.exists(),
        "raw_hits_lines":   _count_lines(raw) if raw.exists() else 0,
        "candidates_exists": cand.exists(),
        "candidates_rows":  max(0, _count_lines(cand) - 1) if cand.exists() else 0,
        "scored_files":     [f.name for f in scored],
        "contact_files":    [f.name for f in contacts],
        "latest_output":    all_out[0].name if all_out else None,
        "all_scored_names": [f.name for f in all_out],
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
):
    cfg = load_config()
    cfg.update({
        "assistant_id":    assistant_id.strip() or cfg["assistant_id"],
        "target_contacts": target_contacts,
        "score_model":     score_model,
        "batch_size":      batch_size,
        "min_score":       min_score,
    })
    save_config(cfg)
    return RedirectResponse("/setup?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Page 2 — Run  /run
# ---------------------------------------------------------------------------

@app.get("/run", response_class=HTMLResponse)
async def run_page(request: Request):
    phase_jobs = {p: jobs.get(latest_jobs.get(p)) for p in ("harvest", "score", "orchestrator")}
    return _tpl("run.html", request, {
        "phase_jobs":       phase_jobs,
        "harvest_ok":       harvest_ok(),
        "score_ok":         score_ok(),
        "orchestrator_ok":  orchestrator_ok(),
    })


@app.post("/run/harvest")
async def run_harvest():
    DATA_DIR.mkdir(exist_ok=True)
    env = _base_env({
        "RAW_HITS_FILE":   str(DATA_DIR / "raw_hits.jsonl"),
        "CANDIDATES_FILE": str(DATA_DIR / "candidates.csv"),
    })
    jid = start_job("harvest", "Phase 1 — Harvest", [sys.executable, "-u", str(SCRIPTS["harvest"])], env)
    return RedirectResponse(f"/run?job={jid}", status_code=303)


@app.post("/run/score")
async def run_score():
    cfg = load_config()
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    DATA_DIR.mkdir(exist_ok=True)
    env = _base_env({
        "SCORE_MODEL":  cfg["score_model"],
        "BATCH_SIZE":   cfg["batch_size"],
        "MIN_SCORE":    cfg["min_score"],
        "OUTPUT_FILE":  str(DATA_DIR / f"ohio_landman_contacts_{ts}.csv"),
    })
    cmd = [sys.executable, "-u", str(SCRIPTS["score"]), str(DATA_DIR / "candidates.csv")]
    jid = start_job("score", "Phase 2 — Score", cmd, env)
    return RedirectResponse(f"/run?job={jid}", status_code=303)


@app.post("/run/orchestrator")
async def run_orchestrator(candidates_file: str = Form("")):
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
    jid = start_job("orchestrator", "Phase 3 — Orchestrate", cmd, env)
    return RedirectResponse(f"/run?job={jid}", status_code=303)


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
