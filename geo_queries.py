"""geo_queries.py

Dynamic query builders for the Landman Pipeline.

Replaces the hardcoded Ohio-specific query lists in harvest_contacts.py with
geography-aware functions that generate queries from state / county / region /
basin inputs.

Public API
----------
build_web_queries(state, county, region, basin, source_classes) -> dict[str, list[str]]
build_sec_queries(state, county, region, basin)                  -> list[str]
build_county_portal_queries(state, county, tax_site_url)         -> list[str]
load_county_sources(state, county)                               -> dict
discover_sources(state, county)                                  -> dict
"""

import json
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# County source registry
# ---------------------------------------------------------------------------
_REGISTRY_PATH = Path(__file__).parent / "county_sources.json"
_registry: dict | None = None


def _load_registry() -> dict:
    global _registry
    if _registry is None:
        if _REGISTRY_PATH.exists():
            with open(_REGISTRY_PATH, encoding="utf-8") as f:
                data = json.load(f)
                # Strip the _comment meta-key
                _registry = {k: v for k, v in data.items() if not k.startswith("_")}
        else:
            _registry = {}
    return _registry


def load_county_sources(state: str, county: str) -> dict:
    """Return source metadata dict for given state/county, or {} if not found."""
    reg = _load_registry()
    sk = state.strip().lower()
    ck = county.strip().lower().replace(" county", "").strip()
    return reg.get(sk, {}).get(ck, {})


def discover_sources(state: str, county: str) -> dict:
    """Return a complete source bundle for the given state/county."""
    src = load_county_sources(state, county)
    return {
        "state":         state,
        "county":        county,
        "tax_site":      src.get("tax_site", ""),
        "portal_type":   src.get("portal_type", ""),
        "assessor_site": src.get("assessor_site", ""),
        "recorder_site": src.get("recorder_site", ""),
        "gis_site":      src.get("gis_site", ""),
        "notes":         src.get("notes", ""),
        "in_registry":   bool(src),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    """Extract bare domain from a URL."""
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.lower().lstrip("www.")


# ---------------------------------------------------------------------------
# Web / Google query builders
# ---------------------------------------------------------------------------

def build_web_queries(
    state: str,
    county: str,
    region: str = "",
    basin: str = "",
    source_classes: dict | None = None,
) -> dict[str, list[str]]:
    """
    Build a WEB_QUERIES-compatible dict of {family: [queries]}.

    source_classes keys (all default True if omitted):
        google, marketplaces, linkedin, county_tax
    """
    sc = source_classes or {}
    inc_google     = sc.get("google",      True)
    inc_market     = sc.get("marketplaces", True)
    inc_li         = sc.get("linkedin",    True)

    cs  = f"{county} County {state}"       # "Belmont County Ohio"
    s   = state                             # "Ohio"
    geo = basin or region or cs            # broadest useful anchor

    out: dict[str, list[str]] = {}

    # ── brokers_buyers ────────────────────────────────────────────────────────
    if inc_google:
        brokers = [
            f'sell mineral rights {cs} broker',
            f'mineral buyer {cs}',
            f'royalty acquisition company {s}',
            f'mineral rights buyer {cs}',
            f'mineral rights broker {s}',
            f'"{cs}" mineral rights purchase company',
            f'"{cs}" royalty interests acquisition',
            f'{s} mineral royalty buyer acquisitions',
            f'non-operated mineral rights buyer {s}',
            f'mineral deed purchase {s} landman',
        ]
        if basin:
            brokers += [
                f'{basin} mineral acquisition firm',
                f'{basin} royalty buyer acquisitions',
            ]
        out["brokers_buyers"] = brokers

    # ── marketplaces ──────────────────────────────────────────────────────────
    if inc_market:
        mkts = [
            f'site:energynet.com {county} County mineral',
            f'site:energynet.com {cs} royalty',
            f'site:mineralexchange.com {s} royalty',
            f'site:mineralexchange.com {cs}',
            f'site:mineralrightsforum.com {cs}',
            f'site:landman.org {s} mineral royalty',
            f'mineral rights marketplace {cs}',
            f'mineral rights auction {cs}',
        ]
        if basin:
            mkts += [
                f'site:mineralexchange.com {basin} royalty listing',
                f'EnergyNet {basin} mineral auction',
            ]
        out["marketplaces"] = mkts

    # ── funds_aggregators ─────────────────────────────────────────────────────
    if inc_google:
        funds = [
            f'mineral fund {s} acquisitions',
            f'royalty fund {s}',
            f'"mineral rights" fund {s} acquisitions',
            f'"royalty interests" fund {s}',
            f'mineral royalty private equity {s}',
            f'NPRI acquisition {s}',
            f'overriding royalty interest acquisition company {s}',
            f'royalty aggregator {s}',
            f'mineral rights portfolio company {s} acquired',
            f'non-operated mineral acquisition {s} fund',
        ]
        if basin:
            funds += [
                f'minerals aggregator {basin}',
                f'mineral fund {basin} investment',
            ]
        out["funds_aggregators"] = funds

    # ── linkedin_public ───────────────────────────────────────────────────────
    if inc_li:
        li = [
            f'site:linkedin.com/in "mineral rights" {s} landman',
            f'site:linkedin.com/in "royalty acquisition" {s}',
            f'site:linkedin.com/in "{county} County" minerals',
            f'site:linkedin.com/company "mineral" "royalty" {s} acquisition',
            f'site:linkedin.com/in "mineral acquisitions" {geo}',
            f'site:linkedin.com/in "royalty buyer" OR "mineral buyer" {s}',
        ]
        if basin:
            li += [
                f'site:linkedin.com/in "{basin}" minerals',
                f'site:linkedin.com/in "landman" "{basin}"',
            ]
        out["linkedin_public"] = li

    # ── attorneys_cpas ────────────────────────────────────────────────────────
    if inc_google:
        out["attorneys_cpas"] = [
            f'mineral rights attorney {s} oil gas {county}',
            f'oil gas CPA {s} mineral royalty tax',
            f'"mineral rights" attorney {s}',
            f'oil gas law firm {s} mineral royalty',
            f'mineral deed attorney {s}',
            f'royalty income CPA {s} mineral rights taxes',
            f'mineral rights estate attorney {s}',
            f'landman attorney {s} oil gas title',
            f'"oil and gas" attorney {cs}',
        ]

    # ── influencers_associations ───────────────────────────────────────────────
    if inc_google:
        inf = [
            f'{s} mineral rights YouTube channel landman',
            f'mineral rights blog {s} royalty owner',
            f'AAPL landman association {s}',
            f'{s} Oil Gas Association mineral royalty',
            f'mineral rights education {s} royalty owner',
            f'landman conference speaker {s} minerals',
        ]
        if basin:
            inf += [
                f'{basin} landman podcast royalties',
                f'{basin} oil gas newsletter mineral',
            ]
        out["influencers_associations"] = inf

    # ── news_press ────────────────────────────────────────────────────────────
    if inc_google:
        news = [
            f'mineral acquisition {s} press release 2023 OR 2024 OR 2025',
            f'mineral rights portfolio {s} acquired',
            f'"mineral rights" acquisition {s} "million" 2024',
            f'mineral fund acquires {s} royalties',
            f'royalty acquisition {cs} announcement',
        ]
        if basin:
            news += [
                f'royalty acquisition {basin} announcement',
                f'mineral acquisition deal closed {basin}',
            ]
        out["news_press"] = news

    return out


# ---------------------------------------------------------------------------
# SEC EDGAR query builder
# ---------------------------------------------------------------------------

def build_sec_queries(
    state: str,
    county: str,
    region: str = "",
    basin: str = "",
) -> list[str]:
    """Build EDGAR full-text search queries for the given geography."""
    queries = [
        f'"mineral interests" "{state}"',
        f'"royalty interests" "{state}"',
        '"mineral and royalty acquisitions"',
        f'"mineral interests" "{county}"',
        f'"NPRI" "{state}"',
        f'"mineral rights" "{state}" "acquisition"',
        f'"royalty fund" "{state}"',
        f'"non-operated" "mineral" "{state}"',
    ]
    if basin:
        queries += [
            f'"mineral interests" "{basin}"',
            f'"overriding royalty" "{basin}"',
        ]
    elif region:
        queries.append(f'"mineral interests" "{region}"')
    return queries


# ---------------------------------------------------------------------------
# County portal / property tax query builder
# ---------------------------------------------------------------------------

def build_county_portal_queries(
    state: str,
    county: str,
    tax_site_url: str = "",
) -> list[str]:
    """Build queries for county tax / property portal discovery."""
    cs = f"{county} County {state}"
    queries = [
        f'"{cs}" mineral owner parcel property records',
        f'"{cs}" grantee grantor mineral deed records',
        f'site:kofiletech.us "{county} County" "{state}"',
        f'"{cs}" county recorder mineral deed',
    ]
    if tax_site_url:
        domain = _domain(tax_site_url)
        if domain:
            queries.append(f'site:{domain} "{county} County"')
    return queries
