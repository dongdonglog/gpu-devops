#!/usr/bin/env python3
"""Build a reflowable EPUB 3 from the MkDocs site and source Mermaid diagrams."""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"


@dataclass
class Element:
    tag: str
    attrs: list[tuple[str, str | None]] = field(default_factory=list)
    children: list[Element | str] = field(default_factory=list)


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: Element | None = None
        self.stack: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.stack:
            if tag == "article" and "md-content__inner" in dict(attrs).get("class", "").split():
                self.root = Element("article", attrs)
                self.stack.append(self.root)
            return
        element = Element(tag, attrs)
        self.stack[-1].children.append(element)
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.stack:
            self.stack[-1].children.append(Element(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.stack and data:
            self.stack[-1].children.append(data)


def text_content(node: Element | str) -> str:
    if isinstance(node, str):
        return node
    return "".join(text_content(child) for child in node.children)


def find_first(node: Element, tag: str) -> Element | None:
    if node.tag == tag:
        return node
    for child in node.children:
        if isinstance(child, Element):
            found = find_first(child, tag)
            if found is not None:
                return found
    return None


def serialize(node: Element | str) -> str:
    if isinstance(node, str):
        return html.escape(node, quote=False)
    attrs = "".join(
        f' {name}="{html.escape(value or "", quote=True)}"'
        for name, value in node.attrs
        if not name.lower().startswith("on")
    )
    if node.tag in VOID_TAGS:
        return f"<{node.tag}{attrs} />"
    contents = "".join(serialize(child) for child in node.children)
    return f"<{node.tag}{attrs}>{contents}</{node.tag}>"


def page_xhtml(title: str, body: str) -> bytes:
    document = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="{XHTML_NS}" xml:lang="zh-CN" lang="zh-CN">
<head><title>{html.escape(title)}</title><meta charset="utf-8" /><link rel="stylesheet" type="text/css" href="../Styles/book.css" /></head>
<body><main>{body}</main></body>
</html>'''
    return document.encode("utf-8")


def parse_article(page: Path) -> Element:
    parser = ArticleParser()
    parser.feed(page.read_text(encoding="utf-8"))
    if parser.root is None:
        raise ValueError(f"Could not find the rendered article in {page}")
    return parser.root


def make_book_tree(
    node: Element,
    *,
    chapter_number: int | None,
    page_route: str,
    site_dir: Path,
    temp_dir: Path,
    images: dict[str, bytes],
    diagram_counts: dict[int, int],
) -> Element:
    result = Element(node.tag, list(node.attrs), [])
    for child in node.children:
        if isinstance(child, str):
            result.children.append(child)
            continue

        classes = dict(child.attrs).get("class", "").split()
        if child.tag == "a" and "headerlink" in classes:
            continue

        if child.tag == "div" and "mermaid" in classes:
            if chapter_number is None:
                raise ValueError("Mermaid diagrams are expected only in chapter articles")
            diagram_counts[chapter_number] = diagram_counts.get(chapter_number, 0) + 1
            index = diagram_counts[chapter_number]
            generated = temp_dir / f"ch{chapter_number:02d}-{index}.svg"
            if not generated.is_file():
                raise ValueError(f"Missing generated diagram: {generated.name}")
            asset = f"diagram-ch{chapter_number:02d}-{index:02d}.svg"
            images[asset] = generated.read_bytes()
            result.children.append(
                Element(
                    "figure",
                    [("class", "diagram")],
                    [Element("img", [("src", f"../Images/{asset}"), ("alt", f"第 {chapter_number} 章技术图 {index}")])],
                )
            )
            continue

        if child.tag == "img":
            attrs = dict(child.attrs)
            source = attrs.get("src", "")
            parsed = urlsplit(source)
            if parsed.scheme or source.startswith("//"):
                raise ValueError(f"External image cannot be bundled offline: {source}")
            asset_path = Path(os.path.normpath(os.path.join(page_route, parsed.path)))
            if asset_path.is_absolute() or ".." in asset_path.parts:
                raise ValueError(f"Image path escapes the site directory: {source}")
            source_file = site_dir / asset_path
            if not source_file.is_file():
                raise ValueError(f"Image not found in built site: {source_file}")
            suffix = source_file.suffix.lower()
            digest = hashlib.sha256(source_file.read_bytes()).hexdigest()[:10]
            asset = f"asset-{digest}{suffix}"
            images.setdefault(asset, source_file.read_bytes())
            result.children.append(
                Element(
                    "img",
                    [("alt", attrs.get("alt", "")), ("src", f"../Images/{asset}")],
                )
            )
            continue

        result.children.append(
            make_book_tree(
                child,
                chapter_number=chapter_number,
                page_route=page_route,
                site_dir=site_dir,
                temp_dir=temp_dir,
                images=images,
                diagram_counts=diagram_counts,
            )
        )
    return result


def render_nav(entries: list[tuple[str, str]]) -> bytes:
    links = "\n".join(
        f'<li><a href="Text/{html.escape(href, quote=True)}">{html.escape(title)}</a></li>'
        for title, href in entries
    )
    return f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="{XHTML_NS}" xmlns:epub="{EPUB_NS}" xml:lang="zh-CN" lang="zh-CN">
<head><title>目录</title><meta charset="utf-8" /><link rel="stylesheet" type="text/css" href="Styles/book.css" /></head>
<body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>{links}</ol></nav></body>
</html>'''.encode("utf-8")


def render_opf(title: str, identifier: str, entries: list[tuple[str, str]], images: dict[str, bytes]) -> bytes:
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />',
        '<item id="style" href="Styles/book.css" media-type="text/css" />',
    ]
    spine = []
    for index, (_, href) in enumerate(entries):
        item_id = f"text-{index:02d}"
        manifest.append(f'<item id="{item_id}" href="Text/{html.escape(href, quote=True)}" media-type="application/xhtml+xml" />')
        spine.append(f'<itemref idref="{item_id}" />')
    for index, asset in enumerate(sorted(images)):
        media_type = "image/svg+xml" if asset.lower().endswith(".svg") else "image/png"
        manifest.append(f'<item id="image-{index:02d}" href="Images/{html.escape(asset, quote=True)}" media-type="{media_type}" />')

    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="{OPF_NS}" version="3.0" unique-identifier="book-id" xml:lang="zh-CN" prefix="rendition: http://www.idpf.org/vocab/rendition/#">
  <metadata xmlns:dc="{DC_NS}">
    <dc:identifier id="book-id">{identifier}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>AI DevOps Book</dc:creator>
    <dc:language>zh-CN</dc:language>
    <meta property="dcterms:modified">{modified}</meta>
    <meta property="rendition:layout">reflowable</meta>
  </metadata>
  <manifest>{''.join(manifest)}</manifest>
  <spine>{''.join(spine)}</spine>
</package>'''
    return document.encode("utf-8")


BOOK_CSS = """html { -webkit-text-size-adjust: 100%; }
body { color: #202426; font-family: serif; font-size: 1em; line-height: 1.65; margin: 5%; }
h1, h2, h3, h4 { color: #235c57; font-family: sans-serif; line-height: 1.3; page-break-after: avoid; }
h1 { font-size: 1.65em; margin: 0 0 1em; }
h2 { font-size: 1.3em; margin: 1.8em 0 .6em; }
h3 { font-size: 1.12em; margin: 1.4em 0 .5em; }
p, li { orphans: 2; widows: 2; }
a { color: #166c68; text-decoration: underline; }
img { height: auto; max-width: 100%; }
figure { margin: 1.3em 0; text-align: center; }
figure.diagram { page-break-inside: avoid; }
figure.diagram img { width: 100%; }
table { border-collapse: collapse; font-size: .88em; margin: 1em 0; width: 100%; }
th, td { border: 1px solid #aeb8b7; padding: .4em; text-align: left; vertical-align: top; overflow-wrap: anywhere; }
th { background: #eef3f2; }
pre { background: #f3f5f5; border: 1px solid #d7dddd; padding: .7em; white-space: pre-wrap; word-wrap: break-word; }
code, pre { font-family: monospace; font-size: .86em; }
blockquote { border-left: .2em solid #7a9996; margin-left: 0; padding-left: 1em; }
.admonition { border: 1px solid #bac8c6; margin: 1em 0; padding: .7em; }
"""


def build_epub(site_dir: Path, source_dir: Path, output: Path, mmdc: str) -> None:
    if not (site_dir / "index.html").is_file():
        raise SystemExit(f"Built MkDocs site not found at {site_dir}; run `mkdocs build` first")

    chapters = sorted((source_dir / "chapters").glob("ch[0-9][0-9].md"))
    if len(chapters) != 14:
        raise SystemExit(f"Expected 14 chapter source files, found {len(chapters)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {
        "EPUB/Styles/book.css": BOOK_CSS.encode("utf-8"),
        "META-INF/container.xml": f'''<?xml version="1.0" encoding="utf-8"?>
<container xmlns="{CONTAINER_NS}" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml" /></rootfiles></container>'''.encode("utf-8"),
    }
    images: dict[str, bytes] = {}
    entries: list[tuple[str, str]] = []
    diagram_counts: dict[int, int] = {}
    identifier = f"urn:uuid:{uuid.uuid4()}"

    with tempfile.TemporaryDirectory(prefix="ai-devops-book-epub-") as temporary:
        temp_dir = Path(temporary)
        for chapter in chapters:
            match = re.fullmatch(r"ch(\d{2})\.md", chapter.name)
            assert match is not None
            chapter_number = int(match.group(1))
            output_base = temp_dir / f"ch{chapter_number:02d}.svg"
            subprocess.run(
                [mmdc, "-i", str(chapter), "-o", str(output_base), "--theme", "neutral", "--quiet"],
                check=True,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        intro = parse_article(site_dir / "index.html")
        intro_title_node = find_first(intro, "h1")
        title = text_content(intro_title_node).strip() if intro_title_node else "大规模 GPU Kubernetes 训练集群运维"
        intro_tree = make_book_tree(
            intro,
            chapter_number=None,
            page_route=".",
            site_dir=site_dir,
            temp_dir=temp_dir,
            images=images,
            diagram_counts=diagram_counts,
        )
        files["EPUB/Text/intro.xhtml"] = page_xhtml("前言与学习路线", "".join(serialize(child) for child in intro_tree.children))
        entries.append(("前言与学习路线", "intro.xhtml"))

        for chapter in chapters:
            chapter_number = int(chapter.stem[-2:])
            page = site_dir / "chapters" / chapter.stem / "index.html"
            article = parse_article(page)
            h1 = find_first(article, "h1")
            chapter_title = text_content(h1).strip() if h1 else f"第 {chapter_number} 章"
            before = diagram_counts.get(chapter_number, 0)
            tree = make_book_tree(
                article,
                chapter_number=chapter_number,
                page_route=f"chapters/{chapter.stem}",
                site_dir=site_dir,
                temp_dir=temp_dir,
                images=images,
                diagram_counts=diagram_counts,
            )
            relative = f"ch{chapter_number:02d}.xhtml"
            files[f"EPUB/Text/{relative}"] = page_xhtml(chapter_title, "".join(serialize(child) for child in tree.children))
            entries.append((chapter_title, relative))
            if diagram_counts.get(chapter_number, 0) - before != 3:
                raise ValueError(f"Chapter {chapter_number} should export exactly 3 diagrams")

        files["EPUB/nav.xhtml"] = render_nav(entries)
        files["EPUB/package.opf"] = render_opf(title, identifier, entries, images)
        files.update({f"EPUB/Images/{name}": content for name, content in images.items()})

    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, content in sorted(files.items()):
            archive.writestr(name, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    validate_epub(output)
    print(f"Created {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"Included {len(chapters)} chapters and {sum(diagram_counts.values())} Mermaid diagrams as static SVG")


def validate_epub(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if archive.infolist()[0].filename != "mimetype" or archive.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            raise ValueError("EPUB mimetype must be the first uncompressed ZIP entry")
        if archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("Invalid EPUB mimetype")

        for name in names:
            if name.endswith((".xhtml", ".opf", ".xml", ".svg")):
                root = ET.fromstring(archive.read(name))
                if name.endswith(".xhtml"):
                    for element in root.iter():
                        if element.tag.rsplit("}", 1)[-1] == "script":
                            raise ValueError(f"Scripts are not allowed in offline EPUB content: {name}")
                        for attribute in ("href", "src"):
                            value = element.attrib.get(attribute, "")
                            if not value or value.startswith("#"):
                                continue
                            parsed = urlsplit(value)
                            if parsed.scheme or parsed.netloc:
                                continue
                            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), parsed.path))
                            if target not in names:
                                raise ValueError(f"EPUB resource is missing: {name} -> {value}")
        opf = ET.fromstring(archive.read("EPUB/package.opf"))
        manifest = opf.find(f"{{{OPF_NS}}}manifest")
        spine = opf.find(f"{{{OPF_NS}}}spine")
        if manifest is None or spine is None:
            raise ValueError("EPUB package is missing its manifest or spine")
        for item in manifest:
            if f"EPUB/{item.attrib['href']}" not in names:
                raise ValueError(f"EPUB manifest entry is missing: {item.attrib['href']}")
        if len(spine) != 15:
            raise ValueError(f"Expected intro plus 14 chapter documents, found {len(spine)} spine entries")
        diagrams = [name for name in names if re.fullmatch(r"EPUB/Images/diagram-ch\d{2}-\d{2}\.svg", name)]
        if len(diagrams) != 42:
            raise ValueError(f"Expected 42 static diagram images, found {len(diagrams)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the built GPU Kubernetes book to a reflowable EPUB 3")
    parser.add_argument("--site", type=Path, default=ROOT / "site", help="MkDocs build output directory")
    parser.add_argument("--source", type=Path, default=ROOT / "docs", help="Markdown source directory")
    parser.add_argument("--output", type=Path, default=ROOT / "exports" / "GPU-Kubernetes-训练集群运维.epub")
    parser.add_argument("--mmdc", default=shutil.which("mmdc"), help="Mermaid CLI executable")
    args = parser.parse_args()
    if not args.mmdc:
        raise SystemExit("Mermaid CLI `mmdc` is required; install @mermaid-js/mermaid-cli or pass --mmdc")
    build_epub(args.site.resolve(), args.source.resolve(), args.output.resolve(), args.mmdc)


if __name__ == "__main__":
    main()
