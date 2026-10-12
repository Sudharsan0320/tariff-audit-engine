import pdfplumber, re, sqlite3  # three toolboxes: pdfplumber reads PDFs, re does pattern-matching (regex), sqlite3 talks to the database

with pdfplumber.open("data/raw/rider-gen.pdf") as pdf:  # open the PDF as 'pdf'; with auto-closes it when the block ends
    print(f"Pages: {len(pdf.pages)}")  # f-string: { } runs code inside text; len() counts the PDF's pages
    text = pdf.pages[0].extract_text()  # pages[0] = first page; extract_text() pulls all its text
    print(text[0:501])  # slice: characters 0-500, a peek at the text
    tables = pdf.pages[0].extract_tables()  # find tables on the page; returns a list of tables
    print(f"Tables :{len(tables)}")  # how many tables were found
    print(tables[0])  # first table: a list of rows, each row a list of cell texts

conn = sqlite3.connect("data/tariffs.db")  # open the database file
cur = conn.cursor()  # cursor: the messenger that carries SQL to the database
version_values = (  # a tuple (an ordered list you can't change after making) holding the six values that identify this version...
    "VA",  # ...the state
    "Virginia Electric and Power Company",  # ...the utility name
    "RIDER GEN",  # ...the schedule code
    1,  # ...the version number
    "2025-04-01",  # ...the date this version takes effect
    "draft",  # ...the status: draft = written by the pipeline, not yet validated/active
)  # defined once so the SELECT and the INSERT below can never disagree with each other
# --- The SQL below searches for this version. Line by line: ---
# SELECT id FROM tariff_versions = "give me just the id column from the versions table"
# WHERE ... = six conditions joined by AND (every one must be true); ? = placeholder, filled from version_values in order
# ORDER BY id DESC = newest first (highest id on top); LIMIT 1 = take only the top row
# The SQL text and its values travel separately: this is called a parameterized query
cur.execute(
    """
    SELECT id FROM tariff_versions
    WHERE state = ? AND utility = ? AND schedule_code = ?
      AND version = ? AND effective_from = ? AND status = ?
    ORDER BY id DESC
    LIMIT 1
    """,
    version_values,
)
existing_version = cur.fetchone()  # take the first result row, or None if the SELECT found nothing (fetchone = one row; fetchall = all rows)
if existing_version is None:  # the version is NOT in the database yet...
    # --- The SQL below inserts it. Line by line: ---
    # INSERT INTO tariff_versions(...) = add a row to tariff_versions, naming the six columns (names only: no types, no quotes)
    # VALUES (?, ?, ?, ?, ?, ?) = the six values, one ? placeholder per column, filled from version_values
    cur.execute(
        """
        INSERT INTO tariff_versions(
            state, utility, schedule_code, version, effective_from, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        version_values,
    )
    tariff_version_id = cur.lastrowid  # lastrowid = the id the database just assigned to that row; remember it (bare name, no parentheses: it's a value the cursor holds, not an action)
else:  # the version WAS already found...
    tariff_version_id = existing_version[0]  # ...so reuse its id: [0] = the first column of the found row

print(tariff_version_id)  # show the version id everything below will be stamped with

for row in tables[0][1:]:  # loop over the table's data rows; tables[0] = first table, [1:] = rows from index 1 onward, skipping row 0 (the header)
    schedule = row[0]  # the row's first cell: the schedule name, e.g. 'Schedule 5'
    for component_type, rate_cell in (  # inner loop over two (label, cell) pairs; each round unpacks one pair into the two names
        ("supply_cents_per_kwh", row[1]),  # pair 1: label for the energy charge; cell = column 1 (the kWh charge column)
        ("demand_dollars_per_kw", row[4]),  # pair 2: label for the demand charge; cell = column 4 (the kW demand column)
    ):
        if rate_cell is None or not rate_cell.strip():  # if the cell is missing, or blank even after strip() removes surrounding whitespace...
            continue  # ...skip to the next round of the loop (no component gets written for this cell)

        # --- The pattern below tests the whole cell. Piece by piece: ---
        # r = raw string: backslashes stay literal, which regex needs
        # \$? = optional dollar sign; (\d+(?:\.\d+)?) = CAPTURED group 1 = the number (digits, optionally with decimals)
        # (?:¢/kWh|/kW) = the unit suffix, grouped but NOT captured; (?:\d+(?:,\d+)*)? = optional footnote digits (like 1 or 3,4), ignored
        # re.fullmatch(pattern, cell) = does the ENTIRE cell match? returns a match object, or None
        match = re.fullmatch(
            r"\$?(\d+(?:\.\d+)?)(?:¢/kWh|/kW)(?:\d+(?:,\d+)*)?",
            rate_cell,
        )
        if match is None:  # the cell matched nothing recognizable...
            raise ValueError(f"Unrecognized rate for {schedule}: {rate_cell}")  # ...crash loudly with a message (f-string fills in the names); never write a guessed rate into the database

        # --- The SQL below writes one component row: ---
        # INSERT INTO tariff_components(...) = add a row, naming four columns
        # VALUES (?, ?, ?, ?) = four placeholders, filled in order from the tuple below it
        # the values: the version id from above; the loop's label; the schedule name (stored in billing_category);
        #   float(match.group(1)) = group(1) is the captured number text, float() converts it to a real number
        cur.execute(
            """
            INSERT INTO tariff_components(
                tariff_version_id, component_type, billing_category, rate
            ) VALUES (?, ?, ?, ?)
            """,
            (tariff_version_id, component_type, schedule, float(match.group(1))),
        )

conn.commit()  # SAVE everything: one single commit for all rows = all-or-nothing, like activation
conn.close()  # close the database connection
