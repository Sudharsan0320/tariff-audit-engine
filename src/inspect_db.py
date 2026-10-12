import sqlite3  # the database toolbox
conn = sqlite3.connect("data/tariffs.db")  # open the database file
cur = conn.cursor()  # make the cursor: the messenger that carries SQL to the database
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")  # sqlite_master = SQLite's own catalog of what's inside the file; this asks it: "give me the names of all tables"
print(cur.fetchall())  # fetchall() = grab every result row as a list; print() shows it on screen
conn.close()  # close the connection (no commit() needed — SELECT only reads, it changes nothing)
