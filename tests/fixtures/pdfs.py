"""Build small, real PDFs in-memory for tests (no third-party writer needed).

The document-custody and extraction paths only mean anything against actual PDF bytes:
a fake byte string would prove the plumbing and nothing about page boundaries. This
writes a genuine, minimal PDF with a Helvetica text layer, one content stream per page,
so ``pypdf`` recovers the same page split a real document would produce.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _page_stream(lines: list[str]) -> bytes:
    body = ["BT", "/F1 11 Tf", "72 760 Td", "14 TL"]
    body += [f"({_escape(line)}) Tj T*" for line in lines]
    body.append("ET")
    return "\n".join(body).encode("latin-1", errors="replace")


def build_pdf(pages: list[list[str]]) -> bytes:
    """Return a valid PDF whose page N contains ``pages[N - 1]`` as text lines."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)  # 1-based object number

    # Object numbering is fixed up front so /Kids and /Parent can reference each other.
    catalog_num, pages_num = 1, 2
    objects.extend([b"", b""])  # placeholders for catalog + page tree
    font_num = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_nums: list[int] = []
    for lines in pages:
        stream = _page_stream(lines)
        content_num = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_nums.append(
            add(
                b"<< /Type /Page /Parent "
                + str(pages_num).encode()
                + b" 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 "
                + str(font_num).encode()
                + b" 0 R >> >> /Contents "
                + str(content_num).encode()
                + b" 0 R >>"
            )
        )

    kids = b" ".join(f"{n} 0 R".encode() for n in page_nums)
    objects[pages_num - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_nums)).encode() + b" >>"
    )
    objects[catalog_num - 1] = b"<< /Type /Catalog /Pages " + str(pages_num).encode() + b" 0 R >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root "
        + str(catalog_num).encode()
        + b" 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


#: A two-page bank statement whose figures the source-of-wealth narrative can cite.
BANK_STATEMENT_PAGES = [
    [
        "GLOBAL TRUST BANK - STATEMENT OF ACCOUNT",
        "Account holder: Meridian Logistics Holdings Pte Ltd",
        "Account number: 501-88231-004    Period: 01 Jan 2026 to 31 Mar 2026",
        "Opening balance: SGD 1,204,880.15",
        "Closing balance: SGD 3,918,442.60",
    ],
    [
        "TRANSACTION DETAIL (page 2 of 2)",
        "14 Feb 2026  Proceeds, sale of freehold property at 21 Orchard Rise",
        "             Credit SGD 2,450,000.00",
        "28 Mar 2026  Dividend, Meridian Logistics operating subsidiary",
        "             Credit SGD 318,500.00",
    ],
]
