"""score_contacts.py

Phase 2 of the Ohio Landman contact pipeline.

Reads candidates.csv produced by harvest_contacts.py, sends batches to
OpenAI (chat completions), and writes a scored contact list.

Outputs:
  ohio_landman_contacts_<timestamp>.csv  - final scored contacts

Usage:
  python score_contacts.py [candidates_file]

  Default candidates file: candidates.csv

Env vars required:
  OPENAI_API_KEY

Env vars optional:
  SCORE_MODEL          - OpenAI model (default: gpt-4o-mini)
  BATCH_SIZE           - candidates per OpenAI call (default: 15, hard-capped at 20)
  OUTPUT_FILE          - override output filename
  MIN_SCORE            - drop contacts below this relevance score (default: 4.0)
  MAX_SNIPPET_CHARS    - truncate evidence snippets before sending (default: 300)
  DRY_RUN=1            - validate config, print batch plan, exit without API calls
"""

import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCORE_MODEL = os.environ.get("SCORE_MODEL", "gpt-4o-mini")

# Hard cap: >20 candidates in one prompt reliably causes token-overflow or
# truncated JSON on most models.  User-supplied BATCH_SIZE is honoured up to
# that ceiling so callers never have to think about it.
_HARD_CAP_BATCH = 20
BATCH_SIZE = min(int(os.environ.get("BATCH_SIZE", "15")), _HARD_CAP_BATCH)

MIN_SCORE = float(os.environ.get("MIN_SCORE", "4.0"))

# Optional LinkedIn seed file — when set, those candidates are merged into the
# scoring batch alongside web-harvested ones and tagged _candidate_type=linkedin_seed.
LINKEDIN_SEED_FILE = os.environ.get("LINKEDIN_SEED_FILE", "")

# Snippet length cap — long snippets balloon the prompt without adding signal.
MAX_SNIPPET_CHARS = int(os.environ.get("MAX_SNIPPET_CHARS", "300"))

_flag = lambda k: os.environ.get(k, "").strip().lower() in ("1", "true", "yes")
DRY_RUN = _flag("DRY_RUN")
OUTPUT_FILE = os.environ.get(
    "OUTPUT_FILE",
    f"ohio_landman_contacts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
)
CANDIDATES_FILE = sys.argv[1] if len(sys.argv) > 1 else "candidates.csv"

client = OpenAI()

# ---------------------------------------------------------------------------
# Contact schema (matches the existing assistant schema)
# ---------------------------------------------------------------------------
CONTACT_FIELDS = [
    "name",
    "role",
    "organization",
    "segment",
    "geo_focus",
    "relevance_score",
    "tier",
    "email",
    "phone",
    "website",
    "linkedin_url",
    "source_urls",
    "evidence_notes",
    "last_verified_year",
    # Pipeline metadata
    "_candidate_id",
    "_candidate_type",
    "_hit_count",
    "_query_families",
]

SEGMENT_ENUM = ["Influencer", "Broker", "Fund", "Attorney", "CPA", "Owner", "Other"]
TIER_ENUM = ["A", "B", "C"]

SYSTEM_PROMPT = """You are "The Ohio Landman", an expert AI landman specializing in oil and gas \
minerals and royalties in Ohio (Belmont, Monroe, Jefferson, Carroll, Harrison, Guernsey, Noble, \
Washington, and adjacent counties) and the broader Appalachian Basin (Utica/Marcellus shale plays).

You classify, score, and structure contact candidates extracted from web search results and \
SEC EDGAR filings into a clean contact universe for mineral and royalty deal sourcing.

SEGMENTS:
- Influencer: YouTube/podcast/blog/association education on minerals or royalties
- Broker: mineral/royalty broker, land shop, acquisition company
- Fund: mineral fund, royalty fund, family office, PE-backed aggregator
- Attorney: oil & gas or mineral rights focused law firm or partner
- CPA: accountant or firm with mineral/royalty tax focus
- Owner: large visible mineral/royalty owner, family office, aggregator
- Other: relevant but outside the above

SCORING RULES:
- relevance_score 0-10: how useful is this contact for Ohio minerals/royalties deal sourcing?
  - 8-10: Active Ohio/Appalachia buyer or broker; clear deal evidence
  - 6-7: Relevant firm or professional; regional connection confirmed
  - 4-5: Indirect relevance; national player or adjacent practice
  - 0-3: Irrelevant; skip entirely (do not include in output)
- tier:
  - A: relevance_score >= 8
  - B: relevance_score >= 6
  - C: relevance_score >= 4

QUALITY RULES:
- Do NOT fabricate email addresses, phone numbers, or LinkedIn URLs. Set to null if unknown.
- A website URL may be inferred from the source_urls if one is a homepage.
- Prefer fewer, high-confidence contacts over noisy lists.
- If the candidate is clearly irrelevant (SEO spam, wrong geography, wrong industry), skip it.
- EDGAR entity names are corporate names — classify them as Broker, Fund, or Owner as appropriate.
"""

CLASSIFICATION_PROMPT_TEMPLATE = """Below are {count} candidate entities extracted from web search \
results, SEC EDGAR filings, and LinkedIn connections. Each candidate includes: display_name, \
entity_name (if EDGAR/LinkedIn), candidate_type (web_page | edgar_entity | linkedin_seed), \
source_urls, and evidence_snippets.

Your task: for each relevant candidate, output ONE structured contact object.

Skip candidates that are:
- Clearly not related to minerals, royalties, or oil & gas
- Generic real estate or banking companies with no minerals connection
- Duplicate of another contact in this batch (pick the best one)

For each contact you keep, output a JSON object with EXACTLY these fields:
{{
  "name": "Person or Organization name",
  "role": "Job title or null if org-level only",
  "organization": "Company/firm name",
  "segment": "{segments}",
  "geo_focus": "Primary geography (e.g. 'Belmont/Monroe, Ohio' or 'Appalachian Basin')",
  "relevance_score": 0-10,
  "tier": "A|B|C",
  "email": null,
  "phone": null,
  "website": "homepage URL or null",
  "linkedin_url": null,
  "source_urls": ["url1", "url2"],
  "evidence_notes": "1-2 sentences explaining relevance and confidence",
  "last_verified_year": integer year or null,
  "_candidate_id": "the candidate_id field from the input",
  "_candidate_type": "web_page | edgar_entity | linkedin_seed",
  "_hit_count": integer
}}

Output ONLY a JSON array of contact objects. No markdown, no explanation, no text outside the JSON.
If no candidates in this batch are relevant, output an empty array: []

Candidates:
{candidates_json}
"""


# ---------------------------------------------------------------------------
# Load candidates
# ---------------------------------------------------------------------------

def load_linkedin_seed(seed_file: str) -> list[dict]:
    """Load linkedin_seed.csv as additional candidates to score."""
    if not seed_file or not os.path.exists(seed_file):
        return []
    rows = []
    with open(seed_file, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(row)
    print(f"[linkedin_seed] Loaded {len(rows)} seeded contacts from {seed_file}")
    return rows


def load_candidates(candidates_file: str) -> list[dict]:
    """Load candidates.csv into a list of dicts."""
    if not os.path.exists(candidates_file):
        print(f"[ERROR] Candidates file not found: {candidates_file}")
        sys.exit(1)

    candidates = []
    with open(candidates_file, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            candidates.append(row)

    print(f"Loaded {len(candidates)} candidates from {candidates_file}")
    return candidates


def candidate_to_context(c: dict) -> dict:
    """Convert a CSV row into a compact dict for the scoring prompt."""
    snippets_raw = c.get("evidence_snippets", "")
    snippets = [
        s.strip()[:MAX_SNIPPET_CHARS]          # enforce per-snippet cap
        for s in snippets_raw.split("|") if s.strip()
    ][:3]                                       # max 3 snippets per candidate

    urls_raw = c.get("source_urls", "")
    urls = [u.strip() for u in urls_raw.split("|") if u.strip()][:3]  # 3 URLs enough

    return {
        "candidate_id": c.get("candidate_id", ""),
        "candidate_type": c.get("candidate_type", ""),
        "display_name": c.get("display_name", ""),
        "entity_name": c.get("entity_name", "") or None,
        "hit_count": int(c.get("hit_count", 1)),
        "edgar_hit": c.get("edgar_hit", "False") == "True",
        "query_families": c.get("query_families", ""),
        "source_urls": urls,
        "evidence_snippets": snippets,
    }


# ---------------------------------------------------------------------------
# Scoring via OpenAI chat completions
# ---------------------------------------------------------------------------

def score_batch(batch: list[dict], batch_num: int, total_batches: int) -> list[dict]:
    """
    Send a batch of candidates to OpenAI for classification.
    Returns a list of scored contact dicts.
    """
    print(f"\n[batch {batch_num}/{total_batches}] Scoring {len(batch)} candidates...")

    candidates_json = json.dumps(batch, indent=2, ensure_ascii=False)
    user_prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(
        count=len(batch),
        segments="|".join(SEGMENT_ENUM),
        candidates_json=candidates_json,
    )

    # Exponential backoff with full jitter — handles TPM / RPM rate limits.
    MAX_ATTEMPTS = 5
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.chat.completions.create(
                model=SCORE_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
            raw = response.choices[0].message.content.strip()
            break
        except Exception as exc:
            if attempt < MAX_ATTEMPTS - 1:
                # Full jitter: sleep = random(0, cap) where cap doubles each retry
                cap = min(2 ** attempt * 4, 60)          # 4 s, 8 s, 16 s, 32 s …
                sleep_for = random.uniform(0, cap)
                print(f"  [RETRY {attempt+1}/{MAX_ATTEMPTS-1}] {exc} — sleeping {sleep_for:.1f}s")
                time.sleep(sleep_for)
            else:
                print(f"  [ERROR] All {MAX_ATTEMPTS} attempts exhausted: {exc}")
                return []

    # Strip optional markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        contacts = json.loads(raw)
        if not isinstance(contacts, list):
            print("  [WARN] Response is not a JSON array; skipping batch.")
            return []
    except json.JSONDecodeError as exc:
        print(f"  [WARN] JSON parse failed: {exc}")
        print(f"  Raw response (first 500 chars): {raw[:500]}")
        return []

    print(f"  -> {len(contacts)} contacts extracted")
    return contacts


# ---------------------------------------------------------------------------
# Post-processing and output
# ---------------------------------------------------------------------------

def clean_contact(c: dict) -> dict:
    """Validate and fill defaults on a scored contact dict."""
    # Coerce relevance_score to float
    try:
        c["relevance_score"] = float(c.get("relevance_score", 0))
    except (TypeError, ValueError):
        c["relevance_score"] = 0.0

    # Enforce tier based on score
    score = c["relevance_score"]
    if score >= 8:
        c["tier"] = "A"
    elif score >= 6:
        c["tier"] = "B"
    elif score >= 4:
        c["tier"] = "C"
    else:
        c["tier"] = "C"

    # Validate segment
    if c.get("segment") not in SEGMENT_ENUM:
        c["segment"] = "Other"

    # Flatten source_urls list to pipe-separated string for CSV
    if isinstance(c.get("source_urls"), list):
        c["source_urls"] = " | ".join(c["source_urls"])

    # Ensure all fields exist
    for field in CONTACT_FIELDS:
        c.setdefault(field, None)

    return c


def write_contacts_csv(contacts: list[dict], output_file: str) -> None:
    """Write the final scored contacts to CSV."""
    with open(output_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=CONTACT_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        for c in contacts:
            writer.writerow({f: c.get(f, "") for f in CONTACT_FIELDS})
    print(f"\n[output] Written {len(contacts)} contacts -> {output_file}")


def print_summary(contacts: list[dict]) -> None:
    """Print tier breakdown to stdout."""
    from collections import Counter
    tiers = Counter(c.get("tier", "?") for c in contacts)
    segs = Counter(c.get("segment", "?") for c in contacts)
    print("\n--- Summary ---")
    print(f"Total contacts: {len(contacts)}")
    for t in ["A", "B", "C"]:
        print(f"  Tier {t}: {tiers.get(t, 0)}")
    print("Segments:")
    for seg, cnt in sorted(segs.items()):
        print(f"  {seg}: {cnt}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

import re  # noqa: E402 (placed here to keep top-of-file clean)


def main() -> None:
    print("=" * 60)
    print("Ohio Landman — Score Contacts  (Phase 2)")
    print(f"Model: {SCORE_MODEL}  |  Batch size: {BATCH_SIZE}  |  Min score: {MIN_SCORE}")
    print("=" * 60)

    # Load candidates
    candidates = load_candidates(CANDIDATES_FILE)

    # Merge optional LinkedIn seed (auto-detected from LINKEDIN_SEED_FILE env var)
    if LINKEDIN_SEED_FILE:
        seed = load_linkedin_seed(LINKEDIN_SEED_FILE)
        if seed:
            candidates = candidates + seed
            print(f"Total after LinkedIn merge: {len(candidates)} candidates")

    # Convert to scoring context format
    context_list = [candidate_to_context(c) for c in candidates]

    # Batch and score
    all_contacts: list[dict] = []
    total_batches = (len(context_list) + BATCH_SIZE - 1) // BATCH_SIZE

    # Dry run: validate config and print batch plan, then exit without API calls
    if DRY_RUN:
        print("\n[DRY RUN] Configuration validated. No API calls will be made.")
        print(f"  Candidates file : {CANDIDATES_FILE}")
        print(f"  LinkedIn seed   : {LINKEDIN_SEED_FILE or '(none)'}")
        print(f"  Candidates loaded: {len(context_list)} rows")
        print(f"  Batch size      : {BATCH_SIZE}  (hard cap: {_HARD_CAP_BATCH})")
        print(f"  Total batches   : {total_batches}")
        print(f"  Model           : {SCORE_MODEL}  |  Min score: {MIN_SCORE}")
        print(f"  Output file     : {OUTPUT_FILE}")
        print("\n[DRY RUN] Set DRY_RUN=0 (or unset) to run for real.")
        return

    for i in range(0, len(context_list), BATCH_SIZE):
        batch = context_list[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        scored = score_batch(batch, batch_num, total_batches)
        all_contacts.extend(scored)

        # Be polite to the API between batches
        if i + BATCH_SIZE < len(context_list):
            time.sleep(1.0)

    print(f"\nTotal raw contacts from scoring: {len(all_contacts)}")

    # Clean and filter
    cleaned = [clean_contact(c) for c in all_contacts]
    filtered = [c for c in cleaned if c["relevance_score"] >= MIN_SCORE]

    # Sort by relevance_score descending, then tier
    tier_order = {"A": 0, "B": 1, "C": 2}
    filtered.sort(
        key=lambda c: (
            tier_order.get(c.get("tier", "C"), 3),
            -c["relevance_score"],
        )
    )

    print(f"Contacts after min_score={MIN_SCORE} filter: {len(filtered)}")

    # Write output
    write_contacts_csv(filtered, OUTPUT_FILE)
    print_summary(filtered)

    print(
        f"\nDone. To use these contacts with the assistant, run:\n"
        f"  python run_ohio_landman_assistant.py --candidates {OUTPUT_FILE}"
    )


if __name__ == "__main__":
    main()
