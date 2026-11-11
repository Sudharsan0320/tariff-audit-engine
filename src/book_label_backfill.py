"""Source-verified, rerunnable backfill of each tariff version's book label (after schedule_kind_backfill).

Every version row belongs to one printed book. The mapping below is keyed by (utility, version
number) and names the book's label, its source document, the page the label is printed on, and that
page's verbatim printed line. Before writing, the backfill re-reads each cited page of the actual
PDF and refuses to run if the printed line is not on it, so a label is never stored on wording that
the document does not carry.

`book_label` holds the clean name; `book_evidence` holds the source document, the page, and the
verbatim printed line in quotes, so nothing is silently normalized. Dominion's April 2025 Rider GEN
sheet prints no book label: it is grouped under "Rider GEN sheet", and its evidence says plainly
that no printed label exists and that the grouping is by its source document.

The backfill is idempotent (a second run changes nothing) and refuses to overwrite a stored
`book_label` that disagrees with the mapping. It touches only the two book columns.
"""

import argparse
import re
import sys
from pathlib import Path

try:
    from src import db
except ImportError:
    import db

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

APCO = "Appalachian Power Company"
DOMINION = "Virginia Electric and Power Company"
PEPCO = "Pepco"

NO_PRINTED_LABEL = None

# (utility, version) -> book. `printed_line` is None only where the document prints no book label.
BOOKS = {
    (APCO, 28): {
        "label": "Virginia S.C.C. Tariff No. 28",
        "source": "APCo-Tariff-28.pdf",
        "page": 1,
        "printed_line": "VIRGINIA S.C.C. TARIFF NO. 28",
    },
    (DOMINION, 12): {
        "label": "Amendment No. 12",
        "source": "VEPGA-Amendment-12-Effective-July-1-2025.pdf",
        "page": 1,
        "printed_line": "AMENDMENT NO. 12",
    },
    (PEPCO, 1): {
        "label": "P.S.C. of D.C. No. 1",
        "source": "Pepco-DC-Tariff-2026-10-01.pdf",
        "page": 1,
        "printed_line": "Electricity--P.S.C. of D.C. No. 1",
    },
    (DOMINION, 1): {
        "label": "Rider GEN sheet",
        "source": "rider-gen.pdf",
        "page": None,
        "printed_line": NO_PRINTED_LABEL,
    },
}

COLUMNS = ("book_label", "book_evidence")


class BookLabelError(RuntimeError):
    """The backfill refused to run; nothing was written."""


def evidence_text(book):
    if book["printed_line"] is None:
        return (f"{book['source']} prints no book label; this version is grouped by its source "
                f"document, {book['source']}.")
    return f"{book['source']}, page {book['page']}, printed as \"{book['printed_line']}\""


def _page_lines(path, page):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        if not 1 <= page <= len(pdf.pages):
            raise BookLabelError(f"{path.name} has no page {page}.")
        text = pdf.pages[page - 1].extract_text() or ""
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]


def verify_sources(raw_dir=RAW, page_reader=_page_lines):
    """Confirm every mapped label is printed as recorded on its cited page. Raises before any write."""
    for (utility, version), book in BOOKS.items():
        path = Path(raw_dir) / book["source"]
        if not path.exists():
            raise BookLabelError(f"Source document {book['source']} is not present; nothing was written.")
        if book["printed_line"] is None:
            continue
        lines = page_reader(path, book["page"])
        if book["printed_line"] not in lines:
            raise BookLabelError(
                f"{book['source']} page {book['page']} does not print the line "
                f"{book['printed_line']!r}; nothing was written.")
    return True


def ensure_columns(conn):
    """Add the two nullable book columns when missing. Nothing else about the table changes."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(tariff_versions)").fetchall()}
    added = []
    for column in COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE tariff_versions ADD COLUMN {column} TEXT")
            added.append(column)
    return added


def plan(conn):
    """Rows to fill, rows already correct, and refusals. Reads only."""
    rows = conn.execute(
        "SELECT id, utility, version, book_label, book_evidence FROM tariff_versions ORDER BY id").fetchall()
    to_fill, unchanged, conflicts, unmapped = [], 0, [], []
    for row_id, utility, version, label, evidence in rows:
        book = BOOKS.get((utility, version))
        if book is None:
            unmapped.append((row_id, utility, version))
            continue
        wanted = (book["label"], evidence_text(book))
        if label is not None and label != book["label"]:
            conflicts.append((row_id, utility, version, label, book["label"]))
        elif (label, evidence) == wanted:
            unchanged += 1
        else:
            to_fill.append((row_id, wanted))
    return to_fill, unchanged, conflicts, unmapped


def backfill(conn, commit=True, raw_dir=RAW, page_reader=_page_lines):
    """Verify sources, add the columns if needed, and fill every row. One transaction; returns a summary."""
    verify_sources(raw_dir, page_reader)
    try:
        added = ensure_columns(conn)
        to_fill, unchanged, conflicts, unmapped = plan(conn)
        if conflicts:
            raise BookLabelError(
                "Stored book labels disagree with the mapping; nothing was written: " + repr(conflicts[:5]))
        if unmapped:
            raise BookLabelError(
                "Versions with no mapped book; nothing was written: " + repr(unmapped[:5]))
        for row_id, (label, evidence) in to_fill:
            conn.execute("UPDATE tariff_versions SET book_label = ?, book_evidence = ? WHERE id = ?",
                         (label, evidence, row_id))
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"columns_added": added, "filled": len(to_fill), "unchanged": unchanged}


def counts(conn):
    return conn.execute(
        "SELECT utility, version, book_label, COUNT(*) FROM tariff_versions "
        "GROUP BY utility, version, book_label ORDER BY utility, version").fetchall()


def main(argv=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    conn = db.connect(db.default_target(ROOT / "data" / "tariffs.db"))
    try:
        summary = backfill(conn)
        print(summary)
        for row in counts(conn):
            print(row)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
