"""harvest_contacts.py

Phase 1 of the Landman Pipeline contact harvester.

Collects candidate entities from:
  1. Web search (SerpAPI) — queries generated dynamically from state/county/basin
  2. SEC EDGAR full-text search
  3. County portal discovery (optional — when SOURCE_COUNTY_TAX=1)

Outputs:
  raw_hits.jsonl    - every raw hit from every source/query (append-safe)
  candidates.csv    - deduplicated URLs/entities ready for scoring

Usage:
  python harvest_contacts.py

Geography env vars (override defaults):
  GEO_STATE              - target state (default: Ohio)
  GEO_COUNTY             - target county (default: Belmont)
  GEO_REGION             - optional region label (default: empty)
  GEO_BASIN              - optional basin label (default: Appalachian Basin)

Source-class toggles (all default ON):
  SOURCE_GOOGLE=0        - disable web/Google queries
  SOURCE_MARKETPLACES=0  - disable marketplace site: queries
  SOURCE_LINKEDIN=0      - disable LinkedIn public queries
  SOURCE_SEC=0           - disable SEC EDGAR queries
  SOURCE_COUNTY_TAX=1    - enable county portal/tax-site queries

Other env vars:
  SERPAPI_API_KEY        - required for web search
  RAW_HITS_FILE          - override raw_hits.jsonl path
  CANDIDATES_FILE        - override candidates.csv path
  SERPAPI_DELAY          - seconds between SerpAPI calls (default 1.5)
  EDGAR_DELAY            - seconds between EDGAR calls (default 0.6)
  WEB_RESULTS_PER_QUERY  - results per SerpAPI call (default 10)
  CONSERVATIVE_MODE=1    - halve results/query, truncate snippets to 150 chars
  DRY_RUN=1              - print query plan and exit without API calls
"""

import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import geo_queries as gq

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERPAPI_KEY    = os.environ.get("SERPAPI_API_KEY", "")
RAW_HITS_FILE  = os.environ.get("RAW_HITS_FILE",  "raw_hits.jsonl")
CANDIDATES_FILE = os.environ.get("CANDIDATES_FILE", "candidates.csv")
SERPAPI_DELAY  = float(os.environ.get("SERPAPI_DELAY", "1.5"))
EDGAR_DELAY    = float(os.environ.get("EDGAR_DELAY",  "0.6"))
WEB_RESULTS_PER_QUERY = int(os.environ.get("WEB_RESULTS_PER_QUERY", "10"))

# ── Geography ─────────────────────────────────────────────────────────────────
GEO_STATE  = os.environ.get("GEO_STATE",  "Ohio")
GEO_COUNTY = os.environ.get("GEO_COUNTY", "Belmont")
GEO_REGION = os.environ.get("GEO_REGION", "")
GEO_BASIN  = os.environ.get("GEO_BASIN",  "Appalachian Basin")

# ── Operational safeguards ──────────────────────────────────────────────────
_flag = lambda k: os.environ.get(k, "").strip().lower() in ("1", "true", "yes")

CONSERVATIVE_MODE = _flag("CONSERVATIVE_MODE")
DRY_RUN           = _flag("DRY_RUN")

if CONSERVATIVE_MODE:
    WEB_RESULTS_PER_QUERY = min(WEB_RESULTS_PER_QUERY, 5)
    SERPAPI_DELAY = max(SERPAPI_DELAY, 2.5)
    print("[CONSERVATIVE] Mode ON: 5 results/query, delay >= 2.5 s, snippets capped at 150 chars")

MAX_SNIPPET_CHARS = 150 if CONSERVATIVE_MODE else 500

# ── Source class toggles ──────────────────────────────────────────────────────
# All ON by default; set env var to "0" / "false" / "no" to disable.
_flag_on = lambda k: os.environ.get(k, "").strip().lower() not in ("0", "false", "no")

SOURCE_CLASSES = {
    "google":      _flag_on("SOURCE_GOOGLE"),
    "marketplaces": _flag_on("SOURCE_MARKETPLACES"),
    "linkedin":    _flag_on("SOURCE_LINKEDIN"),
    "county_tax":  _flag("SOURCE_COUNTY_TAX"),   # OFF by default — opt-in
}
SOURCE_SEC = _flag_on("SOURCE_SEC")

# ── Build query sets from geography ──────────────────────────────────────────
WEB_QUERIES: dict[str, list[str]] = gq.build_web_queries(
    GEO_STATE, GEO_COUNTY, GEO_REGION, GEO_BASIN, SOURCE_CLASSES
)

EDGAR_QUERIES: list[str] = gq.build_sec_queries(
    GEO_STATE, GEO_COUNTY, GEO_REGION, GEO_BASIN
) if SOURCE_SEC else []

# County portal queries (opt-in)
if SOURCE_CLASSES["county_tax"]:
    _county_src = gq.load_county_sources(GEO_STATE, GEO_COUNTY)
    _tax_url    = _county_src.get("tax_site", "")
    WEB_QUERIES["county_portal"] = gq.build_county_portal_queries(
        GEO_STATE, GEO_COUNTY, _tax_url
    )

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"


# ---------------------------------------------------------------------------
# Resume support
# ---------------------------------------------------------------------------

def load_done_queries(raw_hits_file: str) -> set[str]:
    """Read raw_hits.jsonl and return set of 'source::query' already collected."""
    done: set[str] = set()
    if not os.path.exists(raw_hits_file):
        return done
    with open(raw_hits_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                key = f"{rec.get('source', '')}::{rec.get('query', '')}"
                done.add(key)
            except json.JSONDecodeError:
                pass
    return done


def append_hits(raw_hits_file: str, hits: list[dict]) -> None:
    with open(raw_hits_file, "a", encoding="utf-8") as fh:
        for hit in hits:
            fh.write(json.dumps(hit, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# SerpAPI web search
# ---------------------------------------------------------------------------

def serpapi_search(query: str, num: int = 10) -> list[dict]:
    if not SERPAPI_KEY:
        print("[WARN] SERPAPI_API_KEY not set; skipping web search.")
        return []
    params = {
        "engine":  "google",
        "q":       query,
        "api_key": SERPAPI_KEY,
        "num":     num,
    }
    try:
        resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [ERROR] SerpAPI: {exc}")
        return []

    return [
        {
            "title":    item.get("title", ""),
            "url":      item.get("link", ""),
            "snippet":  item.get("snippet", ""),
            "position": item.get("position"),
        }
        for item in data.get("organic_results", [])
    ]


def _source_type_from_family(family: str) -> str:
    """Map a query family name to a source_type tag."""
    if family == "county_portal":
        return "county_tax_site"
    if family == "marketplaces":
        return "marketplace"
    return "google"


def harvest_web(raw_hits_file: str, done_queries: set[str]) -> int:
    """Run all web query families through SerpAPI. Returns count of new hits."""
    total_new = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for family, queries in WEB_QUERIES.items():
        print(f"\n=== Query family: {family} ({len(queries)} queries) ===")
        src_type = _source_type_from_family(family)

        for query in queries:
            key = f"web_search::{query}"
            if key in done_queries:
                print(f"  [SKIP] already done: {query!r}")
                continue

            print(f"  Searching: {query!r}")
            results = serpapi_search(query, num=WEB_RESULTS_PER_QUERY)
            print(f"  -> {len(results)} results")

            hits = []
            for r in results:
                url = r.get("url", "")
                if not url:
                    continue
                snippet = (r.get("snippet", "") or "")[:MAX_SNIPPET_CHARS]
                hits.append({
                    "source":       "web_search",
                    "source_type":  src_type,
                    "query_family": family,
                    "query":        query,
                    "title":        r.get("title", ""),
                    "url":          url,
                    "snippet":      snippet,
                    "position":     r.get("position"),
                    "entity_name":  None,
                    "file_date":    None,
                    "form_type":    None,
                    "harvested_at": now_iso,
                    "geo_state":    GEO_STATE,
                    "geo_county":   GEO_COUNTY,
                })

            append_hits(raw_hits_file, hits)
            done_queries.add(key)
            total_new += len(hits)
            time.sleep(SERPAPI_DELAY)

    return total_new


# ---------------------------------------------------------------------------
# SEC EDGAR full-text search
# ---------------------------------------------------------------------------

def edgar_search(query: str, max_hits: int = 20) -> list[dict]:
    params = {
        "q":         query,
        "dateRange": "custom",
        "startdt":   "2018-01-01",
        "enddt":     "2026-12-31",
        "hits.hits.total.value": "true",
        "hits.hits._source": (
            "entity_name,file_date,form_type,period_of_report,"
            "biz_location,inc_states"
        ),
    }
    try:
        resp = requests.get(
            EDGAR_SEARCH_URL, params=params, timeout=30,
            headers={"User-Agent": "LandmanPipelineAgent contact@example.com"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [ERROR] EDGAR: {exc}")
        return []

    raw_hits = data.get("hits", {}).get("hits", [])
    results = []
    for hit in raw_hits[:max_hits]:
        src = hit.get("_source", {})
        entity_name  = src.get("entity_name", "")
        file_date    = src.get("file_date", "")
        form_type    = src.get("form_type", "")
        biz_location = src.get("biz_location", "")
        inc_states   = src.get("inc_states", "")
        safe_name    = requests.utils.quote(entity_name)
        edgar_url    = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?company={safe_name}"
            f"&CIK=&type=&dateb=&owner=include&count=10&action=getcompany"
        )
        results.append({
            "entity_name":  entity_name,
            "file_date":    file_date,
            "form_type":    form_type,
            "biz_location": biz_location,
            "inc_states":   inc_states,
            "edgar_url":    edgar_url,
        })
    return results


def harvest_edgar(raw_hits_file: str, done_queries: set[str]) -> int:
    """Run all EDGAR queries. Returns count of new hits."""
    if not EDGAR_QUERIES:
        print("\n[SEC EDGAR] Skipped (SOURCE_SEC=0)")
        return 0

    total_new = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    print(f"\n=== SEC EDGAR queries ({len(EDGAR_QUERIES)} queries) ===")
    for query in EDGAR_QUERIES:
        key = f"edgar::{query}"
        if key in done_queries:
            print(f"  [SKIP] already done: {query!r}")
            continue

        print(f"  Querying EDGAR: {query!r}")
        results = edgar_search(query)
        print(f"  -> {len(results)} filings")

        hits = []
        for r in results:
            hits.append({
                "source":       "edgar",
                "source_type":  "sec",
                "query_family": "edgar",
                "query":        query,
                "title":        f"{r['entity_name']} ({r['form_type']} {r['file_date']})",
                "url":          r["edgar_url"],
                "snippet": (
                    f"Filing: {r['form_type']} dated {r['file_date']}. "
                    f"Location: {r.get('biz_location', 'N/A')}. "
                    f"Inc. state: {r.get('inc_states', 'N/A')}."
                ),
                "position":     None,
                "entity_name":  r["entity_name"],
                "file_date":    r["file_date"],
                "form_type":    r["form_type"],
                "harvested_at": now_iso,
                "geo_state":    GEO_STATE,
                "geo_county":   GEO_COUNTY,
            })

        append_hits(raw_hits_file, hits)
        done_queries.add(key)
        total_new += len(hits)
        time.sleep(EDGAR_DELAY)

    return total_new


# ---------------------------------------------------------------------------
# Deduplication and candidate extraction
# ---------------------------------------------------------------------------

_STOP_SUFFIXES = re.compile(
    r"\b(llc|inc|corp|ltd|lp|llp|co|company|group|fund|capital|partners|resources|energy|"
    r"royalties|royalty|minerals|mineral|acquisitions|acquisition|management|services|solutions)\b",
    re.IGNORECASE,
)
_NON_ALPHA = re.compile(r"[^a-z0-9 ]")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower()
    n = _NON_ALPHA.sub(" ", n)
    n = _STOP_SUFFIXES.sub("", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def url_to_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return url


def candidate_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _derive_source_type(query_families: set, sources: set) -> str:
    """Pick the most informative source_type for a deduplicated candidate."""
    if "edgar" in sources:
        return "sec"
    if "county_portal" in query_families:
        return "county_tax_site"
    if query_families and query_families <= {"marketplaces"}:
        return "marketplace"
    return "google"


def build_candidates(raw_hits_file: str) -> list[dict]:
    """
    Read raw_hits.jsonl and collapse into deduplicated candidates.

    Dedup logic:
      - EDGAR hits: group by normalized entity_name
      - Web hits:   group by (domain, normalized title[:40])
    """
    if not os.path.exists(raw_hits_file):
        print(f"[ERROR] {raw_hits_file} not found.")
        return []

    buckets: dict[str, dict] = {}

    with open(raw_hits_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                hit = json.loads(line)
            except json.JSONDecodeError:
                continue

            source       = hit.get("source", "")
            url          = hit.get("url", "")
            entity_name  = hit.get("entity_name") or ""
            title        = hit.get("title", "")
            snippet      = hit.get("snippet", "")
            query_family = hit.get("query_family", "")
            query        = hit.get("query", "")
            file_date    = hit.get("file_date")
            form_type    = hit.get("form_type")
            harvested_at = hit.get("harvested_at", "")
            geo_state    = hit.get("geo_state", GEO_STATE)
            geo_county   = hit.get("geo_county", GEO_COUNTY)

            if source == "edgar" and entity_name:
                norm      = normalize_name(entity_name)
                dedup_key = f"edgar::{norm}"
                display_name    = entity_name
                candidate_type  = "edgar_entity"
            else:
                domain    = url_to_domain(url)
                norm_title = normalize_name(title)
                dedup_key = f"web::{domain}::{norm_title[:40]}"
                display_name    = title
                candidate_type  = "web_page"

            if dedup_key not in buckets:
                buckets[dedup_key] = {
                    "candidate_id":   candidate_id(dedup_key),
                    "candidate_type": candidate_type,
                    "display_name":   display_name,
                    "entity_name":    entity_name,
                    "source_urls":    [],
                    "evidence_snippets": [],
                    "query_families": set(),
                    "queries":        set(),
                    "sources":        set(),
                    "edgar_hit":      source == "edgar",
                    "file_date":      file_date,
                    "form_type":      form_type,
                    "first_seen_at":  harvested_at,
                    "hit_count":      0,
                    # Geography stamped from harvest-time env
                    "state":          geo_state,
                    "county":         geo_county,
                    "parcel_context": None,
                }

            b = buckets[dedup_key]
            b["hit_count"] += 1
            if url and url not in b["source_urls"]:
                b["source_urls"].append(url)
            if snippet and snippet not in b["evidence_snippets"]:
                b["evidence_snippets"].append(snippet)
            b["query_families"].add(query_family)
            b["queries"].add(query)
            b["sources"].add(source)
            if source == "edgar":
                b["edgar_hit"] = True

    candidates = []
    for b in buckets.values():
        b["query_families"] = sorted(b["query_families"])
        b["queries"]        = sorted(b["queries"])
        b["sources"]        = sorted(b["sources"])
        # Derive source_type after all hits are aggregated
        b["source_type"] = _derive_source_type(
            set(b["query_families"]), set(b["sources"])
        )
        candidates.append(b)

    candidates.sort(key=lambda c: c["hit_count"], reverse=True)
    return candidates


CANDIDATE_FIELDS = [
    "candidate_id",
    "candidate_type",
    "display_name",
    "entity_name",
    "state",
    "county",
    "source_type",
    "parcel_context",
    "edgar_hit",
    "file_date",
    "form_type",
    "hit_count",
    "source_urls",
    "evidence_snippets",
    "query_families",
    "queries",
    "sources",
    "first_seen_at",
]


def write_candidates_csv(candidates: list[dict], candidates_file: str) -> None:
    with open(candidates_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CANDIDATE_FIELDS)
        writer.writeheader()
        for c in candidates:
            row = {}
            for field in CANDIDATE_FIELDS:
                val = c.get(field, "")
                if isinstance(val, list):
                    val = " | ".join(str(v) for v in val)
                elif isinstance(val, bool):
                    val = str(val)
                row[field] = val if val is not None else ""
            writer.writerow(row)
    print(f"\n[candidates] Written {len(candidates)} candidates -> {candidates_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("Landman Pipeline — Harvest Contacts  (Phase 1)")
    print(f"Geography: {GEO_COUNTY} County, {GEO_STATE}" +
          (f" | {GEO_BASIN}" if GEO_BASIN else "") +
          (f" | {GEO_REGION}" if GEO_REGION else ""))
    if CONSERVATIVE_MODE: print("Mode: CONSERVATIVE")
    if DRY_RUN:           print("Mode: DRY RUN  (no API calls will be made)")
    print("=" * 60)

    if not SERPAPI_KEY:
        print("[WARN] SERPAPI_API_KEY not set — web search will be skipped.")

    total_web = sum(len(q) for q in WEB_QUERIES.values())

    if DRY_RUN:
        print(f"\nDRY RUN — would execute {total_web} web queries + {len(EDGAR_QUERIES)} EDGAR queries\n")
        for family, queries in WEB_QUERIES.items():
            print(f"  [{family}]  ({len(queries)} queries, {WEB_RESULTS_PER_QUERY} results each)")
            for q in queries:
                print(f"    * {q}")
        if EDGAR_QUERIES:
            print(f"\n  [edgar]  ({len(EDGAR_QUERIES)} queries)")
            for q in EDGAR_QUERIES:
                print(f"    * {q}")
        print(f"\nOutputs would be written to:\n  {RAW_HITS_FILE}\n  {CANDIDATES_FILE}")
        print("\nDRY RUN complete — no API calls made.")
        return

    done_queries = load_done_queries(RAW_HITS_FILE)
    print(f"\nResume: {len(done_queries)} queries already completed in {RAW_HITS_FILE}")

    web_hits   = harvest_web(RAW_HITS_FILE, done_queries)
    print(f"\n[web] New hits collected: {web_hits}")

    edgar_hits = harvest_edgar(RAW_HITS_FILE, done_queries)
    print(f"\n[edgar] New hits collected: {edgar_hits}")

    print("\nBuilding candidates from raw_hits.jsonl ...")
    candidates = build_candidates(RAW_HITS_FILE)
    print(f"Total unique candidates: {len(candidates)}")

    write_candidates_csv(candidates, CANDIDATES_FILE)

    print("\nDone. Next step: run score_contacts.py to classify and score candidates.")


if __name__ == "__main__":
    main()
