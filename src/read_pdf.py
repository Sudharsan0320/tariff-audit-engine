import pdfplumber

with pdfplumber.open("data/raw/rider-gen.pdf") as pdf:
    print(f"Pages: {len(pdf.pages)}")
    text = pdf.pages[0].extract_text()
    print(text[0:501])
    tables = pdf.pages[0].extract_tables()
    print(f"Tables :{len(tables)}")
    print(tables[0])
