"""Source-derived, rerunnable backfill for tariff_rules citations.

Rules are backfilled with source_page and citation_status from their producing paths:
- Appalachian AI rules: from extraction cache (data/cache/ai-extract/claude-sonnet-5-5/)
  which carries the quote and page that passed verification at extraction time.
- Dominion rules: from the page citations in their producing parsers.

A rule whose citation cannot be re-derived is stored citation_status='uncited', source_page=NULL,
and counted in the report. Never guess or infer a page from the rule text alone.
"""

import json
import re
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

ROOT = Path(__file__).resolve().parent.parent
APCO_UTILITY = "Appalachian Power Company"
VEPGA_UTILITY = "Virginia Electric and Power Company"
CACHE_DIR = ROOT / "data" / "cache" / "ai-extract" / "claude-sonnet-5-5"


def _apco_cache_rules(cache_dir=None):
    """Load AI-extracted Appalachian rules from cache with page citations."""
    cache_dir = Path(cache_dir or CACHE_DIR)
    cache_files = list(cache_dir.glob("*.json"))
    
    rules_by_schedule = {}  # (schedule_code) -> [(rule_type, rule_text, source_page, source_quote), ...]
    
    for cache_file in cache_files:
        with open(cache_file) as f:
            data = json.load(f)
        
        sections = data.get("sections", {})
        for section_key, section in sections.items():
            raw_str = section.get("raw", "")
            if isinstance(raw_str, str):
                try:
                    raw = json.loads(raw_str)
                except (json.JSONDecodeError, ValueError):
                    continue
            else:
                raw = raw_str
            
            designation = raw.get("designation")
            if not designation:
                continue
            
            # Extract schedule code: designation becomes schedule_code (e.g., "R.S." -> "R.S.")
            schedule_code = designation.strip()
            
            rules = raw.get("rules", [])
            for rule in rules:
                rule_type = rule.get("rule_type", "unknown")
                rule_text = rule.get("rule_text", "")
                source_page = rule.get("source_page")
                source_quote = rule.get("source_quote", "")
                
                if not rule_text:
                    continue
                
                if schedule_code not in rules_by_schedule:
                    rules_by_schedule[schedule_code] = []
                
                rules_by_schedule[schedule_code].append({
                    "rule_type": rule_type,
                    "rule_text": rule_text,
                    "source_page": source_page,
                    "source_quote": source_quote,
                })
    
    return rules_by_schedule


def _vepga_rule_pages(conn):
    """Extract page citations for Dominion rules from printed tariff structure.
    
    Dominion rules are derived from schedule analysis during parsing. The rule text
    for "schedule_designation" rules includes the printed page range. Other rule types
    cannot be reliably cited to pages without re-parsing the PDF, so they remain uncited.
    
    Returns: {schedule_code -> {rule_type -> source_page}, ...}
    """
    pages_by_schedule_and_type = {}
    
    cur = conn.cursor()
    cur.execute("""
        SELECT v.schedule_code, r.rule_type, r.rule_text
        FROM tariff_rules r
        JOIN tariff_versions v ON r.tariff_version_id = v.id
        WHERE v.utility = ?
        AND r.rule_type = 'schedule_designation'
    """, (VEPGA_UTILITY,))
    
    for schedule_code, rule_type, rule_text in cur.fetchall():
        # Extract the page from: "PDF pages 28-30" or "PDF pages 28-28"
        match = re.search(r"PDF pages (\d+)-\d+", rule_text)
        if match:
            page = int(match.group(1))
            if schedule_code not in pages_by_schedule_and_type:
                pages_by_schedule_and_type[schedule_code] = {}
            pages_by_schedule_and_type[schedule_code]["schedule_designation"] = page
    
    return pages_by_schedule_and_type


def derive_citation_records(conn, cache_dir=None):
    """Derive citation records from producing-path evidence only."""
    apco_cache_rules = _apco_cache_rules(cache_dir or CACHE_DIR)
    vepga_pages = _vepga_rule_pages(conn)
    
    citations = {
        "cited": [],
        "uncited": [],
    }
    
    cur = conn.cursor()
    
    # Process Appalachian rules from cache
    cur.execute("""
        SELECT r.id, v.schedule_code, r.rule_type, r.rule_text
        FROM tariff_rules r
        JOIN tariff_versions v ON r.tariff_version_id = v.id
        WHERE v.utility = ?
        ORDER BY v.schedule_code, r.rule_type, r.rule_text
    """, (APCO_UTILITY,))
    
    for rule_id, schedule_code, rule_type, rule_text in cur.fetchall():
        citation_found = False
        
        if schedule_code in apco_cache_rules:
            for cached_rule in apco_cache_rules[schedule_code]:
                if (cached_rule["rule_type"] == rule_type and 
                    cached_rule["rule_text"] == rule_text):
                    source_page = cached_rule.get("source_page")
                    if source_page is not None:
                        citations["cited"].append({
                            "rule_id": rule_id,
                            "utility": APCO_UTILITY,
                            "schedule_code": schedule_code,
                            "rule_type": rule_type,
                            "source_page": source_page,
                            "source_quote": cached_rule.get("source_quote", ""),
                        })
                        citation_found = True
                        break
        
        if not citation_found:
            citations["uncited"].append({
                "rule_id": rule_id,
                "utility": APCO_UTILITY,
                "schedule_code": schedule_code,
                "rule_type": rule_type,
            })
    
    # Process Dominion rules
    cur.execute("""
        SELECT r.id, v.schedule_code, r.rule_type, r.rule_text
        FROM tariff_rules r
        JOIN tariff_versions v ON r.tariff_version_id = v.id
        WHERE v.utility = ?
        ORDER BY v.schedule_code, r.rule_type, r.rule_text
    """, (VEPGA_UTILITY,))
    
    for rule_id, schedule_code, rule_type, rule_text in cur.fetchall():
        citation_found = False
        
        if schedule_code in vepga_pages:
            schedule_pages = vepga_pages[schedule_code]
            
            if rule_type == "schedule_designation":
                # Extract page from the rule text itself
                match = re.search(r"PDF pages (\d+)-\d+", rule_text)
                if match:
                    source_page = int(match.group(1))
                    citations["cited"].append({
                        "rule_id": rule_id,
                        "utility": VEPGA_UTILITY,
                        "schedule_code": schedule_code,
                        "rule_type": rule_type,
                        "source_page": source_page,
                        "source_quote": rule_text[:100],
                    })
                    citation_found = True
        
        if not citation_found:
            citations["uncited"].append({
                "rule_id": rule_id,
                "utility": VEPGA_UTILITY,
                "schedule_code": schedule_code,
                "rule_type": rule_type,
            })
    
    return citations


def backfill_citations(conn, cache_dir=None, dry_run=False):
    """Backfill citation columns on tariff_rules from producing-path evidence."""
    citations = derive_citation_records(conn, cache_dir)
    
    cur = conn.cursor()
    
    # Update cited rules
    for citation in citations["cited"]:
        rule_id = citation["rule_id"]
        source_page = citation["source_page"]
        
        if not dry_run:
            cur.execute(
                "UPDATE tariff_rules SET source_page = ?, citation_status = 'cited' WHERE id = ?",
                (source_page, rule_id)
            )
    
    # Update uncited rules
    for citation in citations["uncited"]:
        rule_id = citation["rule_id"]
        
        if not dry_run:
            cur.execute(
                "UPDATE tariff_rules SET source_page = NULL, citation_status = 'uncited' WHERE id = ?",
                (rule_id,)
            )
    
    if not dry_run:
        conn.commit()
    
    return {
        "cited_count": len(citations["cited"]),
        "uncited_count": len(citations["uncited"]),
        "cited": citations["cited"],
        "uncited": citations["uncited"],
    }
