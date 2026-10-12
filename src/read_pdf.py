import pdfplumber  # bring in the pdfplumber toolbox for reading PDF files

with pdfplumber.open("data/raw/rider-gen.pdf") as pdf:  # with = open the PDF and auto-close it when the block ends (even if something crashes); as pdf = call the opened PDF 'pdf' inside the block
    print(f"Pages: {len(pdf.pages)}")  # f-string: the f lets { } run code inside text; len(pdf.pages) = how many pages the PDF has
    text = pdf.pages[0].extract_text()  # pages[0] = the first page (counting starts at 0); extract_text() pulls all its text into the variable text
    print(text[0:501])  # [0:501] = slice: characters at indexes 0-500; a peek at the start of the text
    tables = pdf.pages[0].extract_tables()  # extract_tables() finds tables on the page; returns a list of tables
    print(f"Tables :{len(tables)}")  # len(tables) = how many tables were found
    print(tables[0])  # tables[0] = the first table: a list of rows, where each row is a list of cell texts
