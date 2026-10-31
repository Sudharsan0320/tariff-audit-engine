"""Evidence citations for parsed tariff components (B1).

A citation is (source_page, source_cell): the 1-based PDF page the value was
read from, and a locator written in the printed page's own terms. Locators are
never guessed: a snippet that cannot be found in the page text raises.
"""

MAX_LOCATOR_TEXT = 200


def squash(text):
    return " ".join(str(text).split())


def clip(text):
    text = squash(text)
    return text if len(text) <= MAX_LOCATOR_TEXT else text[: MAX_LOCATOR_TEXT - 1] + "…"


def table_cell(row_label, column_header, cell_text):
    """Locator for a table cell: row label + column header + the printed cell."""
    parts = [f'row "{squash(row_label)}"']
    if column_header:
        parts.append(f'column "{squash(column_header)}"')
    parts.append(f'cell "{squash(cell_text)}"')
    return ", ".join(parts)


def line_cell(line_text):
    return f'line "{clip(line_text)}"'


class PageIndex:
    """Maps a printed snippet back to the PDF page it starts on."""

    def __init__(self, pages):
        self._starts = []
        pieces = []
        position = 0
        for page_number, text in pages:
            normalized = squash(text or "")
            self._starts.append((position, page_number))
            pieces.append(normalized)
            position += len(normalized) + 1
        self._combined = " ".join(pieces)

    def page_of(self, snippet):
        needle = squash(snippet)
        if not needle:
            raise ValueError("Cannot cite an empty snippet.")
        first = self._combined.find(needle)
        if first < 0:
            raise ValueError(f"Cited text not found on any source page: {needle!r}")
        second = self._combined.find(needle, first + 1)
        page = self._page_at(first)
        if second >= 0 and self._page_at(second) != page:
            raise ValueError(f"Cited text is ambiguous across pages: {needle!r}")
        return page

    def _page_at(self, position):
        page = self._starts[0][1]
        for start, page_number in self._starts:
            if start <= position:
                page = page_number
        return page

    def text_citation(self, snippet):
        return self.page_of(snippet), f'text "{clip(snippet)}"'
