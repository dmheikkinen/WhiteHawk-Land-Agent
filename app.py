"""app.py — Ohio Landman Pipeline Web UI

A lightweight Flask app that wraps the three pipeline scripts in a
beginner-friendly browser interface.  No database, no auth, no build step.

Run:
    pip install flask
    python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

SCRIPTS = {
    "harvest": BASE_DIR / "harvest_contacts.py",
    "score": BASE_DIR / "score_contacts.py",
    "orchestrator": BASE_DIR / "run_ohio_landman_assistant.py",
}

DOWNLOADABLE_EXTENSIONS = {".csv", ".jsonl", ".txt"}
SKIP_FILES = {"config.json"}

# ---------------------------------------------------------------------------
# Config — stored in config.json, API keys never sent to browser
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "openai_api_key": "",
    "serpapi_api_key": "",
    "assistant_id": "asst_EFzDaNzm7hT8bz1InWgAhwbT",
    "target_contacts": 50,
    "score_model": "gpt-4o-mini",
    "batch_size": 20,
    "min_score": 4.0,
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
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def config_is_complete() -> bool:
    cfg = load_config()
    return bool(cfg.get("openai_api_key")) and bool(cfg.get("serpapi_api_key"))


# ---------------------------------------------------------------------------
# Job management — in-memory only; files persist on disk across restarts
# ---------------------------------------------------------------------------
jobs: dict[str, dict] = {}
latest_jobs: dict[str, str] = {}  # phase -> most recent job_id


def new_job(phase: str, label: str) -> str:
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "id": job_id,
        "phase": phase,
        "label": label,
        "status": "running",
        "logs": [],
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "return_code": None,
    }
    latest_jobs[phase] = job_id
    return job_id


def _run_subprocess(job_id: str, cmd: list, env: dict) -> None:
    """Run cmd in a background thread, capturing output line-by-line."""
    job = jobs[job_id]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(BASE_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in proc.stdout:
            job["logs"].append(line.rstrip())
        proc.wait()
        job["return_code"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        job["logs"].append(f"[ERROR] Failed to start process: {exc}")
        job["status"] = "error"
    finally:
        job["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def start_job(phase: str, label: str, cmd: list, env: dict) -> str:
    job_id = new_job(phase, label)
    t = threading.Thread(
        target=_run_subprocess, args=(job_id, cmd, env), daemon=True
    )
    t.start()
    return job_id


def build_env(extra: dict | None = None) -> dict:
    """Copy os.environ and overlay config API keys + any extras."""
    cfg = load_config()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"  # critical for real-time log streaming
    if cfg.get("openai_api_key"):
        env["OPENAI_API_KEY"] = cfg["openai_api_key"]
    if cfg.get("serpapi_api_key"):
        env["SERPAPI_API_KEY"] = cfg["serpapi_api_key"]
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def list_output_files() -> list[dict]:
    files = []
    for p in sorted(BASE_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        if p.suffix not in DOWNLOADABLE_EXTENSIONS:
            continue
        if p.name in SKIP_FILES or p.name.startswith("."):
            continue
        stat = p.stat()
        files.append(
            {
                "name": p.name,
                "ext": p.suffix.lstrip(".").upper(),
                "size_human": _human_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                ),
            }
        )
    return files


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n //= 1024
    return f"{n} GB"


def pipeline_status() -> dict:
    """Return exists/count info for key pipeline files."""
    raw_hits = BASE_DIR / "raw_hits.jsonl"
    candidates = BASE_DIR / "candidates.csv"
    scored = sorted(BASE_DIR.glob("ohio_landman_contacts_*.csv"), reverse=True)
    contacts = sorted(BASE_DIR.glob("ohio_contacts_*.csv"), reverse=True)
    return {
        "raw_hits_exists": raw_hits.exists(),
        "raw_hits_lines": _count_lines(raw_hits) if raw_hits.exists() else 0,
        "candidates_exists": candidates.exists(),
        "candidates_rows": _count_lines(candidates) - 1 if candidates.exists() else 0,
        "scored_files": [f.name for f in scored],
        "contact_files": [f.name for f in contacts],
    }


def _count_lines(path: Path) -> int:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def get_scored_csvs() -> list[str]:
    """Return filenames of scored contact CSVs, newest first."""
    files = sorted(BASE_DIR.glob("ohio_landman_contacts_*.csv"), reverse=True)
    files += sorted(BASE_DIR.glob("ohio_contacts_*.csv"), reverse=True)
    return [f.name for f in files]


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    phase_jobs = {
        phase: jobs.get(latest_jobs.get(phase))
        for phase in ("harvest", "score", "orchestrator")
    }
    return render_template(
        "index.html",
        config_ok=config_is_complete(),
        phase_jobs=phase_jobs,
        pipeline=pipeline_status(),
        all_jobs=sorted(jobs.values(), key=lambda j: j["started_at"], reverse=True)[:10],
    )


# ---------------------------------------------------------------------------
# Routes — Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET"])
def settings():
    cfg = load_config()
    return render_template(
        "settings.html",
        openai_set=bool(cfg.get("openai_api_key")),
        serpapi_set=bool(cfg.get("serpapi_api_key")),
        cfg=cfg,
    )


@app.route("/settings", methods=["POST"])
def settings_save():
    cfg = load_config()

    # Only overwrite a key field if the user typed something (not blank/placeholder)
    def maybe_update_key(field: str):
        val = request.form.get(field, "").strip()
        if val:  # any non-empty value replaces the stored key
            cfg[field] = val

    maybe_update_key("openai_api_key")
    maybe_update_key("serpapi_api_key")

    cfg["assistant_id"] = request.form.get("assistant_id", cfg["assistant_id"]).strip() or cfg["assistant_id"]
    cfg["target_contacts"] = int(request.form.get("target_contacts", cfg["target_contacts"]))
    cfg["score_model"] = request.form.get("score_model", cfg["score_model"])
    cfg["batch_size"] = int(request.form.get("batch_size", cfg["batch_size"]))
    cfg["min_score"] = float(request.form.get("min_score", cfg["min_score"]))

    save_config(cfg)
    flash("Settings saved successfully.", "success")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Routes — Run pipeline phases
# ---------------------------------------------------------------------------

@app.route("/run/harvest", methods=["POST"])
def run_harvest():
    cmd = [sys.executable, "-u", str(SCRIPTS["harvest"])]
    job_id = start_job("harvest", "Phase 1 — Harvest", cmd, build_env())
    return redirect(url_for("logs_view", job_id=job_id))


@app.route("/run/score", methods=["POST"])
def run_score():
    cfg = load_config()
    env = build_env({
        "SCORE_MODEL": cfg.get("score_model", "gpt-4o-mini"),
        "BATCH_SIZE": cfg.get("batch_size", 20),
        "MIN_SCORE": cfg.get("min_score", 4.0),
    })
    cmd = [sys.executable, "-u", str(SCRIPTS["score"])]
    job_id = start_job("score", "Phase 2 — Score", cmd, env)
    return redirect(url_for("logs_view", job_id=job_id))


@app.route("/run/orchestrator", methods=["POST"])
def run_orchestrator():
    cfg = load_config()
    env = build_env({
        "ASSISTANT_ID": cfg.get("assistant_id", ""),
        "TARGET_CONTACTS": cfg.get("target_contacts", 50),
    })
    candidates_file = request.form.get("candidates_file", "").strip()
    cmd = [sys.executable, "-u", str(SCRIPTS["orchestrator"])]
    if candidates_file:
        cmd += ["--candidates", candidates_file]
    cmd += ["--target", str(cfg.get("target_contacts", 50))]
    job_id = start_job("orchestrator", "Phase 3 — Orchestrate", cmd, env)
    return redirect(url_for("logs_view", job_id=job_id))


# ---------------------------------------------------------------------------
# Routes — Logs & SSE
# ---------------------------------------------------------------------------

@app.route("/logs")
def logs_list():
    all_jobs = sorted(jobs.values(), key=lambda j: j["started_at"], reverse=True)
    return render_template("logs.html", jobs=all_jobs, selected_job=None)


@app.route("/logs/<job_id>")
def logs_view(job_id: str):
    job = jobs.get(job_id)
    all_jobs = sorted(jobs.values(), key=lambda j: j["started_at"], reverse=True)
    return render_template("logs.html", jobs=all_jobs, selected_job=job)


@app.route("/jobs/<job_id>/stream")
def stream_logs(job_id: str):
    """Server-Sent Events endpoint — streams log lines to the browser."""
    job = jobs.get(job_id)
    if not job:
        return Response("data: Job not found\n\n", mimetype="text/event-stream")

    def generate():
        sent = 0
        while True:
            current = job["logs"]
            while sent < len(current):
                # Escape any bare newlines inside the log line
                line = current[sent].replace("\n", " ")
                yield f"data: {line}\n\n"
                sent += 1

            if job["status"] != "running":
                yield f"event: done\ndata: {job['status']}\n\n"
                break

            time.sleep(0.25)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/jobs/<job_id>/status")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status": job["status"],
        "log_count": len(job["logs"]),
        "return_code": job["return_code"],
        "finished_at": job["finished_at"],
    })


# ---------------------------------------------------------------------------
# Routes — Files
# ---------------------------------------------------------------------------

@app.route("/files")
def files_view():
    return render_template(
        "files.html",
        files=list_output_files(),
        scored_csvs=get_scored_csvs(),
    )


@app.route("/files/download/<path:filename>")
def download_file(filename: str):
    target = BASE_DIR / filename
    # Security: confirm the resolved path is inside BASE_DIR
    try:
        target.resolve().relative_to(BASE_DIR.resolve())
    except ValueError:
        return "Forbidden", 403
    if not target.exists() or not target.is_file():
        return "File not found", 404
    return send_file(target, as_attachment=True)


# ---------------------------------------------------------------------------
# Context processor — makes pipeline_status available to all templates
# ---------------------------------------------------------------------------

@app.context_processor
def inject_pipeline():
    return {"pipeline": pipeline_status()}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Ohio Landman — Web UI")
    print("  Open http://127.0.0.1:5000 in your browser")
    print("=" * 60 + "\n")
    app.run(debug=False, threaded=True, host="127.0.0.1", port=5000)
