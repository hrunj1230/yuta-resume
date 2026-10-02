"""Local resume version storage, HTML normalization, and DOCX generation."""

from __future__ import annotations

import datetime as _datetime
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit
from zipfile import ZipFile
import xml.etree.ElementTree as ET


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
BLOCK_TAGS = {"h1", "h2", "h3", "p", "li"}
IGNORED_TAGS = {"script", "style"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
TEMPLATE_INDICES = {"h1": 0, "sub": 1, "h2": 4, "li": 5, "li_end": 7, "body": 9, "h3": 13, "meta": 14}
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
AUTO_LINK = re.compile(
    r"(?<![\w@])(?:[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
    r"|(?:github\.com|(?:[A-Za-z0-9-]+\.)*github\.io)/[^\s<>\"']+)",
    re.IGNORECASE,
)


class Block(NamedTuple):
    kind: str
    parts: Tuple[Tuple[str, Optional[str]], ...]

    @property
    def text(self) -> str:
        return "".join(part for part, _ in self.parts)


class _Node:
    def __init__(self, tag: str = "", attrs=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children = []


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_semver(value: str) -> Tuple[int, int, int]:
    if not isinstance(value, str):
        raise ValueError("버전은 X.Y.Z 형식이어야 합니다.")
    match = SEMVER.fullmatch(value)
    if not match:
        raise ValueError("버전은 X.Y.Z 형식이어야 합니다.")
    return tuple(map(int, match.groups()))


def bump_version(value: str, bump: str) -> str:
    major, minor, patch = parse_semver(value)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError("bump는 major, minor, patch 중 하나여야 합니다.")


def load_versions(root) -> dict:
    path = Path(root) / "resumes" / "versions.json"
    with path.open(encoding="utf-8") as source:
        manifest = json.load(source)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("versions"), list):
        raise ValueError("versions.json 형식이 올바르지 않습니다.")
    seen = set()
    for item in manifest["versions"]:
        if not isinstance(item, dict):
            raise ValueError("versions.json 형식이 올바르지 않습니다.")
        number = parse_semver(item.get("version"))
        if number in seen:
            raise ValueError("versions.json에 중복 버전이 있습니다.")
        seen.add(number)
    return manifest


def write_versions(root, manifest: dict) -> None:
    """Replace the manifest atomically, leaving no temporary file on error."""
    directory = Path(root) / "resumes"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix=".versions-", suffix=".json", delete=False) as output:
            temp_path = Path(output.name)
            json.dump(manifest, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, directory / "versions.json")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _safe_href(value: Optional[str]) -> Optional[str]:
    if not value or re.search(r"[\s\x00-\x1f\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() == "https" and parsed.netloc and not parsed.username and not parsed.password:
        return value
    if parsed.scheme.lower() == "mailto" and parsed.path and "@" in parsed.path:
        return value
    return None


def _inline(nodes, href=None):
    for item in nodes:
        if isinstance(item, str):
            yield item, href
        elif item.tag in IGNORED_TAGS:
            continue
        elif item.tag == "br":
            yield " ", None
        else:
            next_href = _safe_href(item.attrs.get("href")) if item.tag == "a" else href
            yield from _inline(item.children, next_href)


def _clean_parts(nodes) -> Tuple[Tuple[str, Optional[str]], ...]:
    chars = []
    for text, href in _inline(nodes):
        for char in text:
            if char.isspace():
                if chars and chars[-1][0] != " ":
                    chars.append((" ", None))
            else:
                chars.append((char, href))
    if chars and chars[-1][0] == " ":
        chars.pop()
    merged = []
    for char, href in chars:
        if merged and merged[-1][1] == href:
            merged[-1] = (merged[-1][0] + char, href)
        else:
            merged.append((char, href))
    return tuple(merged)


def parse_content(content_html: str) -> List[Block]:
    if not isinstance(content_html, str):
        raise ValueError("본문은 HTML 문자열이어야 합니다.")
    parser = _TreeParser()
    parser.feed(content_html)
    parser.close()
    blocks = []

    def add(kind, nodes):
        parts = _clean_parts(nodes)
        if parts:
            blocks.append(Block(kind, parts))

    def walk(children):
        loose = []

        def flush():
            if loose:
                add("body", loose)
                loose.clear()

        for child in children:
            if isinstance(child, str):
                loose.append(child)
            elif child.tag in IGNORED_TAGS:
                continue
            elif child.tag in BLOCK_TAGS:
                flush()
                kind = child.tag
                if kind == "p":
                    classes = child.attrs.get("class", "").split()
                    kind = "sub" if "sub" in classes else "meta" if "meta" in classes else "body"
                nested = [node for node in child.children
                          if not isinstance(node, str) and node.tag in {"ul", "ol"}]
                add(kind, [node for node in child.children if node not in nested])
                for node in nested:
                    walk(node.children)
            elif child.tag in {"ul", "ol"}:
                flush()
                walk(child.children)
            elif child.tag in {"div", "section", "article", "main", "body", "html"}:
                flush()
                walk(child.children)
            else:
                loose.append(child)
        flush()

    walk(parser.root.children)
    return blocks


def _render_plain(text: str) -> str:
    output = []
    position = 0
    for match in AUTO_LINK.finditer(text):
        raw = match.group(0)
        visible = raw.rstrip(".,;:!?)]")
        if not visible:
            continue
        end = match.start() + len(visible)
        output.append(html.escape(text[position:match.start()]))
        href = "mailto:" + visible if "@" in visible else "https://" + visible
        output.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(visible)}</a>')
        position = end
    output.append(html.escape(text[position:]))
    return "".join(output)


def render_content(blocks: List[Block]) -> str:
    lines = []
    in_list = False
    for block in blocks:
        if block.kind == "li" and not in_list:
            lines.append("<ul>")
            in_list = True
        elif block.kind != "li" and in_list:
            lines.append("</ul>")
            in_list = False
        content = "".join(
            f'<a href="{html.escape(href, quote=True)}">{html.escape(text)}</a>'
            if href else _render_plain(text) for text, href in block.parts
        )
        if block.kind in {"sub", "meta", "body"}:
            lines.append(f'<p class="{block.kind}">{content}</p>')
        elif block.kind in {"h1", "h2", "h3", "li"}:
            lines.append(f"<{block.kind}>{content}</{block.kind}>")
        else:
            raise ValueError("알 수 없는 본문 블록입니다.")
    if in_list:
        lines.append("</ul>")
    return "\n".join(lines) + ("\n" if lines else "")


def normalize_content(content_html: str) -> str:
    return render_content(parse_content(content_html))


_PARAGRAPH = re.compile(r"<w:p(?:\s[^>]*)?>.*?</w:p>", re.DOTALL)
_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>.*?</w:t>", re.DOTALL)


def _template_paragraph(paragraph: str, text: str, identifier: int) -> str:
    first = _TEXT.search(paragraph)
    if first is None:
        raise ValueError("DOCX 템플릿 문단에 텍스트가 없습니다.")
    tag = first.group(0).split(">", 1)[0]
    if "xml:space=" not in tag:
        tag += ' xml:space="preserve"'
    replacement = f"{tag}>{html.escape(text, quote=False)}</w:t>"
    paragraph = (_TEXT.sub("", paragraph[:first.start()]) + replacement
                 + _TEXT.sub("", paragraph[first.end():]))
    paragraph = re.sub(r'w14:paraId="[0-9A-Fa-f]+"', f'w14:paraId="{identifier:08X}"', paragraph)
    paragraph = re.sub(r'w14:textId="[0-9A-Fa-f]+"', f'w14:textId="{identifier:08X}"', paragraph)
    paragraph = re.sub(r'(<w:bookmarkStart\b[^>]*\bw:id=")[^"]+', rf'\g<1>{identifier}', paragraph)
    paragraph = re.sub(r'(<w:bookmarkEnd\b[^>]*\bw:id=")[^"]+', rf'\g<1>{identifier}', paragraph)
    paragraph = re.sub(r'(<w:bookmarkStart\b[^>]*\bw:name=")[^"]+', rf'\g<1>_r{identifier}', paragraph)
    return paragraph


def generate_docx(blocks: List[Block], template_path, destination) -> None:
    template_path = Path(template_path)
    destination = Path(destination)
    with ZipFile(template_path) as template:
        document = template.read("word/document.xml").decode("utf-8")
        paragraph_matches = list(_PARAGRAPH.finditer(document))
        if len(paragraph_matches) <= max(TEMPLATE_INDICES.values()):
            raise ValueError("DOCX 템플릿의 문단이 부족합니다.")
        samples = [match.group(0) for match in paragraph_matches]
        paragraphs = []
        for index, block in enumerate(blocks):
            kind = block.kind
            if kind == "li" and (index + 1 == len(blocks) or blocks[index + 1].kind != "li"):
                kind = "li_end"
            sample = samples[TEMPLATE_INDICES[kind]]
            paragraphs.append(_template_paragraph(sample, block.text, 0x100 + index))
        document = document[:paragraph_matches[0].start()] + "".join(paragraphs) + document[paragraph_matches[-1].end():]
        ET.fromstring(document)
        with ZipFile(destination, "w") as output:
            for item in template.infolist():
                payload = document.encode("utf-8") if item.filename == "word/document.xml" else template.read(item.filename)
                output.writestr(item, payload)


def create_version(root, *, bump=None, version=None, note, content_html=None,
                   docx_path=None, date=None) -> dict:
    """Create a version and replace the manifest after both artifacts are ready."""
    root = Path(root).resolve()
    if (bump is None) == (version is None):
        raise ValueError("bump 또는 version 중 하나만 지정해야 합니다.")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 200:
        raise ValueError("메모는 1~200자여야 합니다.")
    if date is None:
        date = _datetime.date.today().isoformat()
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.")
    try:
        _datetime.date.fromisoformat(date)
    except (TypeError, ValueError) as error:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from error
    manifest = load_versions(root)
    entries = manifest["versions"]
    latest = max(entries, key=lambda item: parse_semver(item["version"])) if entries else None
    newest = parse_semver(latest["version"]) if latest else None
    if bump is not None:
        if bump not in {"major", "minor", "patch"}:
            raise ValueError("bump는 major, minor, patch 중 하나여야 합니다.")
        version = bump_version(latest["version"] if latest else "0.0.0", bump)
    desired = parse_semver(version)
    if newest is not None and desired <= newest:
        raise ValueError("새 버전은 현재 최대 버전보다 커야 합니다.")
    directory = root / "resumes"
    final = directory / f"v{version}"
    if final.exists():
        raise ValueError("버전 폴더가 이미 있습니다.")
    if content_html is None:
        if latest is None:
            raise ValueError("복사할 최신 본문이 없습니다.")
        content_html = (directory / f"v{latest['version']}" / "content.html").read_text(encoding="utf-8")
    blocks = parse_content(content_html)
    if not blocks:
        raise ValueError("본문이 비어 있습니다.")
    normalized = render_content(blocks)
    staging = Path(tempfile.mkdtemp(prefix=f".v{version}-", dir=directory))
    renamed = False
    try:
        (staging / "content.html").write_text(normalized, encoding="utf-8")
        if docx_path is None:
            generate_docx(blocks, root / "scripts" / "resume_template.docx", staging / "resume.docx")
        else:
            shutil.copyfile(docx_path, staging / "resume.docx")
        if final.exists():
            raise ValueError("버전 폴더가 이미 있습니다.")
        staging.rename(final)
        renamed = True
        new_manifest = dict(manifest)
        new_manifest["versions"] = [{"version": version, "date": date, "note": note.strip()}] + entries
        write_versions(root, new_manifest)
    except Exception:
        if renamed:
            shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"version": version, "date": date, "path": f"resumes/v{version}/"}
