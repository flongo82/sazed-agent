"""Documenti costruiti dai test, senza file nel repository: PDF scritti a mano (testo o una
scansione), EPUB, DOCX e ODT come archivi zip."""

import html
import zipfile


def pdf(pages, title=None, author=None, scan=False):
    """Un PDF con una riga di testo per pagina (solo ASCII, senza parentesi), o con scan una
    pagina che è solo un'immagine, come una scansione."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in (["(immagine)"] if scan else pages):
        kids.append(len(objects) + 1)
        if scan:
            objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im1 %d 0 R >> >>"
                           b" /Contents %d 0 R >>" % (len(objects) + 3, len(objects) + 2))
            stream = b"q 300 0 0 300 150 400 cm /Im1 Do Q"
        else:
            objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >>"
                           b" /Contents %d 0 R >>" % (len(objects) + 2))
            stream = b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET"
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        if scan:
            objects.append(b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray"
                           b" /BitsPerComponent 8 /Length 4 >>\nstream\n\x00\xff\xff\x00\nendstream")
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    info = ""
    if title or author:
        fields = "".join(f" /{k} ({v})" for k, v in (("Title", title), ("Author", author)) if v)
        objects.append(f"<<{fields} >>".encode("ascii"))
        info = f" /Info {len(objects)} 0 R"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{info} >>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(out)


def epub(path, chapters, title="Il pozzo dell'ascensione", creators=("Brandon Sanderson",),
         description="<p>Il secondo romanzo di <b>Mistborn</b>.</p>"):
    """Un EPUB con i capitoli dati (titolo, testo); nella spina in ordine inverso al manifesto."""
    meta = f"<dc:title>{html.escape(title)}</dc:title>" + "".join(
        f"<dc:creator>{html.escape(c)}</dc:creator>" for c in creators)
    if description:
        meta += f"<dc:description>{html.escape(description)}</dc:description>"
    manifest = "".join(f'<item id="c{i}" href="text/c{i}.xhtml" media-type="application/xhtml+xml"/>'
                       for i in range(len(chapters)))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{meta}</metadata>'
                   f'<manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        for i, (heading, text) in enumerate(chapters):
            z.writestr(f"OEBPS/text/c{i}.xhtml",
                       '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><style>p {}</style>'
                       f'</head><body><h1>{html.escape(heading)}</h1><p>{html.escape(text)}</p></body></html>')


def docx(path, paragraphs, title=None, creator=None):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{html.escape(p)}</w:t></w:r></w:p>' for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
        if title or creator:
            z.writestr("docProps/core.xml",
                       '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
                       ' xmlns:dc="http://purl.org/dc/elements/1.1/">'
                       f'<dc:title>{html.escape(title or "")}</dc:title><dc:creator>{html.escape(creator or "")}</dc:creator>'
                       '</cp:coreProperties>')


def odt(path, heading, paragraphs, title=None, creator=None):
    office, text = "urn:oasis:names:tc:opendocument:xmlns:office:1.0", "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    body = f"<text:h>{html.escape(heading)}</text:h>" + "".join(
        f"<text:p>{html.escape(p)} <text:span>fine</text:span></text:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("meta.xml",
                   f'<office:document-meta xmlns:office="{office}" xmlns:dc="http://purl.org/dc/elements/1.1/"'
                   ' xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"><office:meta>'
                   f'<dc:title>{html.escape(title or "")}</dc:title>'
                   f'<meta:initial-creator>{html.escape(creator or "")}</meta:initial-creator>'
                   '</office:meta></office:document-meta>')
        z.writestr("content.xml",
                   f'<office:document-content xmlns:office="{office}" xmlns:text="{text}"><office:body><office:text>'
                   f'{body}</office:text></office:body></office:document-content>')
