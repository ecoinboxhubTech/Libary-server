"""E-book helpers: split uploaded text into pages and build the dummy sample book.

Only extracted page text is stored (in the database). The uploaded file itself is never kept or served,
so there is nothing for a reader to download."""
import re

MAX_PAGES = 500
PAGE_CHARS = 1800  # target size when a file has no explicit page breaks


def paginate(text: str) -> list[str]:
    """Pages are split on form-feed characters or on a line holding only `---`. With neither, the text is
    packed into pages of roughly PAGE_CHARS characters, breaking at paragraph boundaries."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if "\f" in text:
        parts = text.split("\f")
    elif re.search(r"^\s*---\s*$", text, flags=re.M):
        parts = re.split(r"^\s*---\s*$", text, flags=re.M)
    else:
        parts, current = [], ""
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            while len(para) > PAGE_CHARS * 1.5:  # one enormous paragraph: cut at a sentence end
                cut = para.rfind(". ", 0, PAGE_CHARS) + 1 or PAGE_CHARS
                if current:
                    parts.append(current); current = ""
                parts.append(para[:cut].strip()); para = para[cut:].strip()
            if current and len(current) + len(para) > PAGE_CHARS:
                parts.append(current); current = ""
            current = f"{current}\n\n{para}" if current else para
        if current:
            parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def sample_pages(title: str, authors: str | None, subject: str | None) -> list[str]:
    by = authors or "the author"
    area = (subject or "transport").replace("_", " ")
    return [
        f"{title}\n\nby {by}\n\nSample e-copy for the NITT Library reader.\n\nThis is placeholder text so the reading experience can be tested. "
        f"The real e-book for this title can be uploaded by a library administrator and will replace these pages.",
        f"Chapter 1: Introduction\n\n{title} introduces the main ideas of {area} for students at the Nigerian Institute of Transport Technology. "
        "Transport systems move people and goods, and every link in the chain depends on planning, people and infrastructure.\n\n"
        "In this sample chapter you can practise turning pages, changing the text size and switching between reading themes.",
        "Chapter 2: Key Concepts\n\nA good reading room is quiet and consistent. In the same way, a good transport network is predictable: "
        "vehicles arrive when expected, cargo is where the paperwork says it is, and delays are explained.\n\n"
        "Three habits help in both settings: measure what matters, share the numbers, and fix the slowest step first.",
        "Chapter 3: Summary\n\nYou have reached the end of the sample e-copy. E-copies are for reading on screen while you have the title on loan. "
        "They cannot be downloaded or printed, and the copy disappears from your reader when you return the book.\n\nThank you for reading.",
    ]
