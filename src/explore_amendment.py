import pdfplumber  # bring in the pdfplumber toolbox for reading PDF files

with pdfplumber.open("data/raw/VEPGA-Amendment-12-Effective-July-1-2025.pdf") as pdf:  # with = open the PDF and auto-close it when the block ends (even if something crashes); as pdf = call the opened PDF 'pdf' inside the block
    print(f"Pages: {len(pdf.pages)}")  # f-string: the f lets { } run code inside text; len(pdf.pages) = how many pages the PDF has
    text = pdf.pages[16].extract_text()  # pages[16] = printed page 17; extract_text() pulls all its text into the variable text
    print(text)  # print the complete extracted text
