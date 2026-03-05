"""harvest_contacts.py

Phase 1 of the Ohio Landman contact pipeline.

Collects candidate entities from:
  1. Web search (SerpAPI) across 6 query families (~80+ queries)
  2. SEC EDGAR full-text search (10 queries)

Outputs:
  raw_hits.jsonl    - every raw hit from every source/query (append-safe)
  candidates.csv    - deduplicated URLs/entities ready for scoring

Usage:
  python harvest_contacts.py

Env vars required:
  SERPAPI_API_KEY   - your SerpAPI key

Env vars optional:
  RAW_HITS_FILE     - override raw_hits.jsonl path
  CANDIDATES_FILE   - override candidates.csv path
  SERPAPI_DELAY     - seconds between SerpAPI calls (default 1.5)
  EDGAR_DELAY       - seconds between EDGAR calls (default 0.6)
  WEB_RESULTS_PER_QUERY - results per SerpAPI call (default 10)
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERPAPI_KEY = os.environ.get("SERPAPI_API_KEY", "")
RAW_HITS_FILE = os.environ.get("RAW_HITS_FILE", "raw_hits.jsonl")
CANDIDATES_FILE = os.environ.get("CANDIDATES_FILE", "candidates.csv")
SERPAPI_DELAY = float(os.environ.get("SERPAPI_DELAY", "1.5"))
EDGAR_DELAY = float(os.environ.get("EDGAR_DELAY", "0.6"))
WEB_RESULTS_PER_QUERY = int(os.environ.get("WEB_RESULTS_PER_QUERY", "10"))

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_FILING_BASE = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={entity}&CIK=&type=&dateb=&owner=include&count=10&search_text=&action=getcompany"
EDGAR_HIT_URL_TEMPLATE = "https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&startdt=2018-01-01&enddt=2026-12-31"


# ---------------------------------------------------------------------------
# Query families
# ---------------------------------------------------------------------------
WEB_QUERIES: dict[str, list[str]] = {
    "brokers_buyers": [
        "sell mineral rights Belmont County Ohio broker",
        "mineral buyer Belmont County Ohio",
        "royalty acquisition company Ohio Utica",
        "mineral rights buyer Monroe County Ohio",
        "mineral rights broker Ohio Appalachian Basin",
        "Ohio mineral royalty buyer acquisitions",
        "Utica Shale mineral rights purchase company",
        "Appalachian mineral acquisition firm",
        '"mineral rights" "Belmont County" OR "Monroe County" buy',
        '"royalty interests" Ohio acquisition company',
        "non-operated mineral rights buyer Ohio",
        "mineral deed purchase Ohio landman",
    ],
    "marketplaces": [
        "site:energynet.com Belmont County mineral",
        "site:energynet.com Monroe County Ohio royalty",
        "site:mineralexchange.com Utica royalty",
        "site:mineralexchange.com Ohio mineral rights",
        "site:mineralrightsforum.com Belmont County Ohio",
        "site:landman.org Ohio Utica mineral royalty",
        "mineral rights marketplace Ohio Utica listing",
        "EnergyNet Ohio mineral auction",
        "MineralExchange Utica Marcellus royalty listing",
        "mineral rights auction eastern Ohio",
    ],
    "funds_aggregators": [
        "mineral fund Appalachia acquisitions",
        "royalty fund Utica Marcellus",
        "minerals aggregator acquired royalties Appalachia",
        "mineral interests acquisition press release Ohio",
        '"mineral rights" fund Ohio acquisitions 2022 OR 2023 OR 2024',
        '"royalty interests" fund Appalachian Basin',
        "Utica Shale mineral fund investment",
        "non-operated mineral acquisition Ohio fund",
        "mineral royalty private equity Ohio",
        "overriding royalty interest acquisition company Ohio",
        "NPRI acquisition Ohio Appalachia fund",
        "royalty aggregator Utica Marcellus Appalachian",
        "mineral rights portfolio company Ohio acquired",
    ],
    "linkedin_public": [
        'site:linkedin.com/in "mineral rights" Ohio landman',
        'site:linkedin.com/in "royalty acquisition" Ohio',
        'site:linkedin.com/in "Utica Shale" minerals Ohio',
        'site:linkedin.com/company "mineral" "royalty" Ohio acquisition',
        'site:linkedin.com/in "oil gas" "landman" "Belmont" OR "Monroe" Ohio',
        'site:linkedin.com/in "mineral acquisitions" Appalachia',
        'site:linkedin.com/in "royalty buyer" OR "mineral buyer" Ohio',
        'site:linkedin.com/in "ORRI" OR "NPRI" Ohio acquisition',
    ],
    "attorneys_cpas": [
        "mineral rights attorney Ohio oil gas Belmont Monroe",
        "oil gas CPA Ohio mineral royalty tax",
        '"mineral rights" attorney "Utica" OR "Marcellus" Ohio',
        "oil gas attorney Guernsey Noble Washington County Ohio",
        "mineral deed attorney eastern Ohio",
        "oil gas law firm Columbus Ohio Utica Marcellus",
        "royalty income CPA Ohio mineral rights taxes",
        "mineral rights estate attorney Ohio Appalachian",
        "landman attorney Ohio oil gas title",
    ],
    "influencers_associations": [
        "Ohio mineral rights YouTube channel landman",
        "Appalachian landman podcast Ohio royalties",
        "mineral rights blog Ohio Utica Marcellus",
        "Ohio AAPL landman association Utica",
        "Ohio Oil Gas Association mineral royalty",
        "Utica Shale landman conference speaker Ohio",
        "mineral rights education Ohio royalty owner",
        "Appalachian Basin oil gas newsletter mineral",
    ],
    "news_press": [
        "mineral acquisition Ohio press release 2023 OR 2024 OR 2025",
        "royalty acquisition Utica Marcellus announcement",
        "mineral rights portfolio Ohio acquired",
        "Appalachian mineral acquisition deal closed",
        "non-operated royalty purchase Ohio announcement",
        '"mineral rights" acquisition Ohio "million" 2023 OR 2024',
        "Utica Shale royalty deal announcement Ohio 2024",
        "mineral fund acquires Ohio Appalachian royalties",
        "oil gas minerals deal eastern Ohio news",
    ],
}

EDGAR_QUERIES: list[str] = [
    '"mineral interests" "Appalachia"',
    '"royalty interests" "Ohio"',
    '"overriding royalty" "Utica"',
    '"mineral and royalty acquisitions"',
    '"mineral interests" "Belmont"',
    '"mineral interests" "Monroe County"',
    '"NPRI" "Ohio"',
    '"ORRI" "Ohio" "Utica"',
    '"mineral rights" "Utica" "acquisition"',
    '"royalty fund" "Appalachian"',
]


# ---------------------------------------------------------------------------
# Resume support — track which queries are already done
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
    """Append a list of hit dicts to raw_hits.jsonl."""
    with open(raw_hits_file, "a", encoding="utf-8") as fh:
        for hit in hits:
            fh.write(json.dumps(hit, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# SerpAPI web search
# ---------------------------------------------------------------------------

def serpapi_search(query: str, num: int = 10) -> list[dict]:
    """Return organic results from SerpAPI."""
    if not SERPAPI_KEY:
        print("[WARN] SERPAPI_API_KEY not set; skipping web search.")
        return []
    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_KEY,
        "num": num,
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
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "position": item.get("position"),
            }
        )
    return results


def harvest_web(raw_hits_file: str, done_queries: set[str]) -> int:
    """Run all web query families through SerpAPI. Returns count of new hits."""
    total_new = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for family, queries in WEB_QUERIES.items():
        print(f"\n=== Query family: {family} ({len(queries)} queries) ===")
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
                hit = {
                    "source": "web_search",
                    "query_family": family,
                    "query": query,
                    "title": r.get("title", ""),
                    "url": url,
                    "snippet": r.get("snippet", ""),
                    "position": r.get("position"),
                    "entity_name": None,
                    "file_date": None,
                    "form_type": None,
                    "harvested_at": now_iso,
                }
                hits.append(hit)

            append_hits(raw_hits_file, hits)
            done_queries.add(key)
            total_new += len(hits)
            time.sleep(SERPAPI_DELAY)

    return total_new


# ---------------------------------------------------------------------------
# SEC EDGAR full-text search
# ---------------------------------------------------------------------------

def edgar_search(query: str, max_hits: int = 20) -> list[dict]:
    """Query EDGAR full-text search and return structured hits."""
    params = {
        "q": query,
        "dateRange": "custom",
        "startdt": "2018-01-01",
        "enddt": "2026-12-31",
        "hits.hits.total.value": "true",
        "hits.hits._source": "entity_name,file_date,form_type,period_of_report,biz_location,inc_states",
    }
    try:
        resp = requests.get(
            EDGAR_SEARCH_URL,
            params=params,
            timeout=30,
            headers={"User-Agent": "OhioLandmanAgent contact@example.com"},
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
        entity_name = src.get("entity_name", "")
        file_date = src.get("file_date", "")
        form_type = src.get("form_type", "")
        biz_location = src.get("biz_location", "")
        inc_states = src.get("inc_states", "")

        # Build a usable URL pointing to the EDGAR company page
        safe_name = requests.utils.quote(entity_name)
        edgar_url = f"https://www.sec.gov/cgi-bin/browse-edgar?company={safe_name}&CIK=&type=&dateb=&owner=include&count=10&action=getcompany"

        results.append(
            {
                "entity_name": entity_name,
                "file_date": file_date,
                "form_type": form_type,
                "biz_location": biz_location,
                "inc_states": inc_states,
                "edgar_url": edgar_url,
            }
        )
    return results


def harvest_edgar(raw_hits_file: str, done_queries: set[str]) -> int:
    """Run all EDGAR queries. Returns count of new hits."""
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
            hit = {
                "source": "edgar",
                "query_family": "edgar",
                "query": query,
                "title": f"{r['entity_name']} ({r['form_type']} {r['file_date']})",
                "url": r["edgar_url"],
                "snippet": (
                    f"Filing: {r['form_type']} dated {r['file_date']}. "
                    f"Location: {r.get('biz_location', 'N/A')}. "
                    f"Inc. state: {r.get('inc_states', 'N/A')}."
                ),
                "position": None,
                "entity_name": r["entity_name"],
                "file_date": r["file_date"],
                "form_type": r["form_type"],
                "harvested_at": now_iso,
            }
            hits.append(hit)

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
    """Lowercase, strip punctuation, strip common org suffixes for fuzzy matching."""
    if not name:
        return ""
    n = name.lower()
    n = _NON_ALPHA.sub(" ", n)
    n = _STOP_SUFFIXES.sub("", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def url_to_domain(url: str) -> str:
    """Extract bare domain from URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        # Strip www.
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return url


def candidate_id(key: str) -> str:
    """SHA-256 of the dedup key, truncated to 16 hex chars."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def build_candidates(raw_hits_file: str) -> list[dict]:
    """
    Read raw_hits.jsonl and collapse into deduplicated candidates.

    Dedup logic:
      - EDGAR hits: group by normalized entity_name
      - Web hits:   group by (domain, normalized title)
                    where the domain is extracted from the URL
    """
    if not os.path.exists(raw_hits_file):
        print(f"[ERROR] {raw_hits_file} not found.")
        return []

    # bucket: key -> {metadata, source_urls, snippets, query_families, queries}
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

            source = hit.get("source", "")
            url = hit.get("url", "")
            entity_name = hit.get("entity_name") or ""
            title = hit.get("title", "")
            snippet = hit.get("snippet", "")
            query_family = hit.get("query_family", "")
            query = hit.get("query", "")
            file_date = hit.get("file_date")
            form_type = hit.get("form_type")
            harvested_at = hit.get("harvested_at", "")

            # Determine dedup key
            if source == "edgar" and entity_name:
                norm = normalize_name(entity_name)
                dedup_key = f"edgar::{norm}"
                display_name = entity_name
                candidate_type = "edgar_entity"
            else:
                domain = url_to_domain(url)
                norm_title = normalize_name(title)
                dedup_key = f"web::{domain}::{norm_title[:40]}"
                display_name = title
                candidate_type = "web_page"

            if dedup_key not in buckets:
                buckets[dedup_key] = {
                    "candidate_id": candidate_id(dedup_key),
                    "candidate_type": candidate_type,
                    "display_name": display_name,
                    "entity_name": entity_name,
                    "source_urls": [],
                    "evidence_snippets": [],
                    "query_families": set(),
                    "queries": set(),
                    "sources": set(),
                    "edgar_hit": source == "edgar",
                    "file_date": file_date,
                    "form_type": form_type,
                    "first_seen_at": harvested_at,
                    "hit_count": 0,
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

    # Convert sets to sorted lists
    candidates = []
    for b in buckets.values():
        b["query_families"] = sorted(b["query_families"])
        b["queries"] = sorted(b["queries"])
        b["sources"] = sorted(b["sources"])
        candidates.append(b)

    # Sort by hit_count descending (most-referenced first)
    candidates.sort(key=lambda c: c["hit_count"], reverse=True)
    return candidates


CANDIDATE_FIELDS = [
    "candidate_id",
    "candidate_type",
    "display_name",
    "entity_name",
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
    """Write candidates list to CSV. Multi-value fields are pipe-separated."""
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
                row[field] = val
            writer.writerow(row)
    print(f"\n[candidates] Written {len(candidates)} candidates -> {candidates_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 60)
    print("Ohio Landman — Harvest Contacts  (Phase 1)")
    print("=" * 60)

    if not SERPAPI_KEY:
        print("[WARN] SERPAPI_API_KEY not set — web search will be skipped.")

    # Load which queries are already done (resume support)
    done_queries = load_done_queries(RAW_HITS_FILE)
    print(f"\nResume: {len(done_queries)} queries already completed in {RAW_HITS_FILE}")

    # --- Phase 1a: Web search ---
    web_hits = harvest_web(RAW_HITS_FILE, done_queries)
    print(f"\n[web] New hits collected: {web_hits}")

    # --- Phase 1b: EDGAR ---
    edgar_hits = harvest_edgar(RAW_HITS_FILE, done_queries)
    print(f"\n[edgar] New hits collected: {edgar_hits}")

    # --- Phase 2: Dedup -> candidates.csv ---
    print("\nBuilding candidates from raw_hits.jsonl ...")
    candidates = build_candidates(RAW_HITS_FILE)
    print(f"Total unique candidates: {len(candidates)}")

    write_candidates_csv(candidates, CANDIDATES_FILE)

    print("\nDone. Next step: run score_contacts.py to classify and score candidates.")


if __name__ == "__main__":
    main()
