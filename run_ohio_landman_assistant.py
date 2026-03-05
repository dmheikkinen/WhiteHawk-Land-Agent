"""run_ohio_landman_assistant.py

Production orchestrator for the Ohio Landman OpenAI Assistant.

Can be used two ways:

  1. Standalone (assistant searches from scratch):
       python run_ohio_landman_assistant.py

  2. With pre-harvested candidates (recommended for large lists):
       python run_ohio_landman_assistant.py --candidates ohio_landman_contacts_20260305_120000.csv

     In this mode the candidates file is summarized and injected into the
     assistant prompt so it can score/rank/enrich from a known set of hits
     rather than inventing contacts from scratch.

Env vars required:
  OPENAI_API_KEY

Env vars optional:
  SERPAPI_API_KEY    - enables real web_search tool calls
  ASSISTANT_ID       - override the assistant ID
  TARGET_CONTACTS    - how many contacts to request (default: 50)
  OUTPUT_FILE        - override output CSV filename
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ASSISTANT_ID = os.environ.get(
    "ASSISTANT_ID", "asst_EFzDaNzm7hT8bz1InWgAhwbT"
)
SERPAPI_KEY = os.environ.get("SERPAPI_API_KEY", "")
TARGET_CONTACTS = int(os.environ.get("TARGET_CONTACTS", "50"))
OUTPUT_FILE = os.environ.get(
    "OUTPUT_FILE",
    f"ohio_contacts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
)

# How many SerpAPI results to fetch per query issued by the assistant
SERPAPI_RESULTS_PER_QUERY = 30

# Polling backoff: starts at 2s, doubles each iteration, capped at 16s
POLL_INITIAL_SLEEP = 2
POLL_MAX_SLEEP = 16

client = OpenAI()


# ---------------------------------------------------------------------------
# SerpAPI helper
# ---------------------------------------------------------------------------

def perform_web_search(query: str, num: int = SERPAPI_RESULTS_PER_QUERY) -> list[dict]:
    """Call SerpAPI Google search. Returns list of {title, url, snippet}."""
    if not SERPAPI_KEY:
        print("  [WARN] SERPAPI_API_KEY not set; returning empty results.")
        return []

    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": min(num, 100),
    }
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [ERROR] SerpAPI: {exc}")
        return []

    results = []
    for item in data.get("organic_results", []):
        url = item.get("link", "")
        if not url:
            continue
        results.append(
            {
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("snippet", ""),
            }
        )
    print(f"  [web_search] '{query}' -> {len(results)} results")
    return results


# ---------------------------------------------------------------------------
# JSON extraction from assistant reply (handles markdown code fences)
# ---------------------------------------------------------------------------

def extract_json(text: str) -> str:
    """
    Strip optional ```json ... ``` fences and // comment lines, then return
    the inner JSON string.  LLMs occasionally inject JS-style // comments
    (e.g. '// list truncated…') which make json.loads() fail.
    """
    # 1) Remove ```json or ``` fences
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped.strip())

    # 2) Strip // comment lines.
    #    Safe to key on line-start whitespace: real // in URLs always appears
    #    inside a quoted string, never as the first non-space token on a line.
    lines = stripped.split("\n")
    lines = [ln for ln in lines if not ln.lstrip().startswith("//")]
    stripped = "\n".join(lines)

    # 3) Trim any trailing comma before ] or } which can also break parsing
    stripped = re.sub(r",\s*([\]\}])", r"\1", stripped)

    return stripped.strip()


# ---------------------------------------------------------------------------
# Candidates context builder
# ---------------------------------------------------------------------------

def load_candidates_context(candidates_file: str, max_rows: int = 200) -> str:
    """
    Load a candidates CSV (from score_contacts.py or harvest_contacts.py)
    and return a compact JSON summary for injection into the assistant prompt.

    Caps at max_rows to avoid hitting token limits.
    """
    if not os.path.exists(candidates_file):
        print(f"[WARN] Candidates file not found: {candidates_file}")
        return ""

    rows = []
    with open(candidates_file, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)

    total = len(rows)
    rows = rows[:max_rows]

    # Build compact summary — just the fields the assistant needs
    compact = []
    for row in rows:
        entry: dict = {}
        # Try scored contacts first, then raw candidates
        for name_field in ("name", "display_name", "entity_name"):
            if row.get(name_field):
                entry["name"] = row[name_field]
                break
        for org_field in ("organization", "entity_name"):
            if row.get(org_field) and row.get(org_field) != entry.get("name"):
                entry["org"] = row[org_field]
                break
        for url_field in ("source_urls", "website"):
            if row.get(url_field):
                # Take first URL only
                entry["url"] = row[url_field].split("|")[0].strip()
                break
        if row.get("evidence_snippets"):
            # Take first snippet, truncated
            snip = row["evidence_snippets"].split("|")[0].strip()
            entry["evidence"] = snip[:200]
        if row.get("evidence_notes"):
            entry["evidence"] = row["evidence_notes"][:200]
        if row.get("query_families"):
            entry["families"] = row["query_families"]
        compact.append(entry)

    summary = json.dumps(compact, ensure_ascii=False)
    note = f"({total} total, showing first {len(rows)})" if total > max_rows else f"({total} total)"
    print(f"[candidates] Loaded {len(rows)} rows {note} from {candidates_file}")
    return summary


# ---------------------------------------------------------------------------
# Build user prompt
# ---------------------------------------------------------------------------

def build_user_prompt(candidates_context: str = "") -> str:
    base = (
        f"Build an A-, B-, and C-tier list of brokers, funds, attorneys, CPAs, "
        f"influencers, and owners actively involved in minerals and royalties in "
        f"Belmont, Monroe, Jefferson, Carroll, Harrison, Guernsey, Noble, and Washington "
        f"Counties, Ohio, and the broader Appalachian Basin (Utica/Marcellus shale plays). "
        f"Return as many distinct, non-duplicative contacts as you can confidently identify — "
        f"aim for at least {TARGET_CONTACTS} contacts. "
        f"Cover all segments: Broker, Fund, Attorney, CPA, Influencer, Owner, Other. "
        f"If high-confidence A-tier contacts are limited, include B- and C-tier contacts "
        f"with weaker evidence but clearly note limitations in evidence_notes. "
    )

    if candidates_context:
        base += (
            "\n\nYou have been provided a list of pre-researched candidate entities below. "
            "Use these as your primary starting point. Score, classify, and enrich each "
            "relevant entry. You may also search for additional contacts beyond this list.\n\n"
            f"PRE-HARVESTED CANDIDATES:\n{candidates_context}\n\n"
        )

    base += (
        "Respond ONLY with a single valid JSON object with these top-level fields: "
        "region (string), query_description (string), segments_included (array of strings), "
        "contacts (array of contact objects), generated_at_iso (string). "
        "Each contact object MUST include: name, role, organization, segment, geo_focus, "
        "relevance_score, tier, email, phone, website, linkedin_url, "
        "source_urls (array of strings), evidence_notes, last_verified_year. "
        "Do not include any text before or after the JSON. Do not ask questions. "
        "Do not truncate, abbreviate, or add any comments (// or /* */) inside the JSON — "
        "output the complete, valid JSON only."
    )
    return base


# ---------------------------------------------------------------------------
# Poll run with exponential backoff
# ---------------------------------------------------------------------------

def poll_run(thread_id: str, run_id: str):
    """Poll a run until terminal or requires_action. Returns the run object."""
    sleep_time = POLL_INITIAL_SLEEP
    while True:
        run = client.beta.threads.runs.retrieve(
            thread_id=thread_id, run_id=run_id
        )
        status = run.status
        print(f"  Run status: {status}")

        if status in ("completed", "failed", "cancelled", "expired", "requires_action"):
            return run

        time.sleep(sleep_time)
        sleep_time = min(sleep_time * 2, POLL_MAX_SLEEP)


# ---------------------------------------------------------------------------
# Tool call dispatcher
# ---------------------------------------------------------------------------

def handle_tool_calls(run, thread_id: str):
    """Dispatch tool calls and submit outputs. Returns updated run."""
    tool_outputs = []

    for tool_call in run.required_action.submit_tool_outputs.tool_calls:
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments or "{}")

        print(f"\n  Tool: {name}")
        print(f"  Args: {json.dumps(args)}")

        if name == "web_search":
            query = args.get("query", "")
            results = perform_web_search(query, num=SERPAPI_RESULTS_PER_QUERY)
            payload = {
                "results": results,
                "total_results": len(results),
            }

        elif name == "people_enrich":
            # Not yet implemented — return informative stub
            payload = {
                "enriched": False,
                "note": "people_enrich not yet implemented; use web_search for manual enrichment.",
                "input": args,
            }

        elif name == "save_contacts":
            count = len(args.get("contacts", []))
            payload = {
                "saved": True,
                "count": count,
                "note": "Contacts will be written to CSV by the orchestrator.",
            }

        else:
            payload = {"error": f"Unknown tool: {name}"}

        tool_outputs.append(
            {
                "tool_call_id": tool_call.id,
                "output": json.dumps(payload),
            }
        )

    # Retry submit_tool_outputs with full-jitter exponential backoff
    MAX_SUBMIT_ATTEMPTS = 4
    for attempt in range(MAX_SUBMIT_ATTEMPTS):
        try:
            updated_run = client.beta.threads.runs.submit_tool_outputs(
                thread_id=thread_id,
                run_id=run.id,
                tool_outputs=tool_outputs,
            )
            return updated_run
        except Exception as exc:
            if attempt < MAX_SUBMIT_ATTEMPTS - 1:
                cap = min(2 ** attempt * 3, 30)          # 3 s, 6 s, 12 s …
                sleep_for = random.uniform(0, cap)
                print(f"  [RETRY submit {attempt+1}/{MAX_SUBMIT_ATTEMPTS-1}] {exc} — sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for)
            else:
                print(f"  [ERROR] submit_tool_outputs failed after {MAX_SUBMIT_ATTEMPTS} attempts: {exc}")
                raise


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def json_to_csv(json_text: str, output_file: str) -> int:
    """
    Parse the assistant's JSON reply and write contacts to a CSV file.
    Returns number of contacts written.
    """
    data = json.loads(json_text)
    contacts = data.get("contacts", [])
    if not contacts:
        print("[json_to_csv] No contacts found in JSON.")
        return 0

    fieldnames = list(contacts[0].keys())

    with open(output_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in contacts:
            # Flatten any list fields to pipe-separated strings
            flat = {}
            for k, v in row.items():
                flat[k] = " | ".join(str(x) for x in v) if isinstance(v, list) else v
            writer.writerow(flat)

    print(f"[csv] Written {len(contacts)} contacts -> {output_file}")
    return len(contacts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ohio Landman Assistant orchestrator"
    )
    parser.add_argument(
        "--candidates",
        metavar="FILE",
        default="",
        help="Path to a candidates CSV from harvest/score pipeline to use as context",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=TARGET_CONTACTS,
        help=f"Number of contacts to request (default: {TARGET_CONTACTS})",
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_FILE,
        help="Output CSV filename",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and print plan; exit without making any API calls",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target = args.target
    output_file = args.output

    print("=" * 60)
    print("Ohio Landman — Assistant Orchestrator")
    print(f"Assistant: {ASSISTANT_ID}")
    print(f"Target contacts: {target}")
    print(f"Output: {output_file}")
    if SERPAPI_KEY:
        print("SerpAPI: enabled")
    else:
        print("SerpAPI: DISABLED (set SERPAPI_API_KEY to enable real searches)")
    print("=" * 60)

    # Dry run: validate config and print plan, then exit without API calls
    if args.dry_run:
        print("\n[DRY RUN] Configuration validated. No API calls will be made.")
        print(f"  Assistant ID   : {ASSISTANT_ID}")
        print(f"  Target contacts: {target}")
        print(f"  Candidates file: {args.candidates or '(none — assistant searches from scratch)'}")
        print(f"  Output file    : {output_file}")
        print(f"  SerpAPI        : {'enabled' if SERPAPI_KEY else 'DISABLED'}")
        print("\n[DRY RUN] Remove --dry-run to run for real.")
        return

    # Optionally load pre-harvested candidates for context injection
    candidates_context = ""
    if args.candidates:
        candidates_context = load_candidates_context(args.candidates)

    # 1) Create thread
    print("\nCreating thread...")
    thread = client.beta.threads.create()
    print(f"Thread: {thread.id}")

    # 2) Add user message
    user_prompt = build_user_prompt(candidates_context)
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_prompt,
    )
    print("User message added.")

    # 3) Start run
    print("Starting run...")
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=ASSISTANT_ID,
    )
    print(f"Run: {run.id}")

    # 4) Event loop
    while True:
        run = poll_run(thread.id, run.id)

        if run.status == "completed":
            print("\nRun completed.")
            break

        if run.status in ("failed", "cancelled", "expired"):
            print(f"\nRun ended with status: {run.status}")
            if run.last_error:
                print(f"  Error code: {getattr(run.last_error, 'code', 'N/A')}")
                print(f"  Error message: {getattr(run.last_error, 'message', 'N/A')}")
            sys.exit(1)

        if run.status == "requires_action":
            print("\nHandling tool calls...")
            run = handle_tool_calls(run, thread.id)
            # After submitting tool outputs, poll again immediately
            continue

    # 5) Fetch assistant reply
    print("\nFetching assistant reply...")
    messages = client.beta.threads.messages.list(thread_id=thread.id)

    assistant_reply = None
    for msg in reversed(messages.data):
        if msg.role == "assistant":
            for part in msg.content:
                if part.type == "text":
                    assistant_reply = part.text.value.strip()
                    break
            if assistant_reply:
                break

    if not assistant_reply:
        print("[ERROR] No assistant reply found.")
        sys.exit(1)

    # Strip markdown fences if present
    clean_reply = extract_json(assistant_reply)

    print("\n--- Raw reply (first 500 chars) ---")
    print(clean_reply[:500])
    print("...")

    # 6) Write CSV
    try:
        count = json_to_csv(clean_reply, output_file)
        print(f"\nSuccess: {count} contacts written to {output_file}")
    except json.JSONDecodeError as exc:
        print(f"\n[ERROR] Could not parse assistant reply as JSON: {exc}")
        fallback = f"raw_reply_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(fallback, "w", encoding="utf-8") as fh:
            fh.write(assistant_reply)
        print(f"Raw reply saved to {fallback} for inspection.")
        sys.exit(1)
    except Exception as exc:
        print(f"\n[ERROR] Writing CSV: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
