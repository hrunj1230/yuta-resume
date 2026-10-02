import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from scripts import resumelib


SOURCE_ROOT = Path(__file__).resolve().parents[1]
NS = {"w": resumelib.W_NS}


def temporary_root(destination):
    root = Path(destination)
    shutil.copytree(SOURCE_ROOT / "resumes", root / "resumes")
    (root / "scripts").mkdir()
    shutil.copyfile(SOURCE_ROOT / "scripts" / "resume_template.docx",
                    root / "scripts" / "resume_template.docx")
    return root


class ResumeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = temporary_root(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_semver_sort_and_bump(self):
        self.assertGreater(resumelib.parse_semver("1.10.0"), resumelib.parse_semver("1.9.0"))
        self.assertEqual(resumelib.bump_version("1.9.3", "major"), "2.0.0")
        self.assertEqual(resumelib.bump_version("1.9.3", "minor"), "1.10.0")
        self.assertEqual(resumelib.bump_version("1.9.3", "patch"), "1.9.4")
        with self.assertRaises(ValueError):
            resumelib.parse_semver("01.2.3")

    def test_duplicate_and_older_versions_leave_files_unchanged(self):
        manifest = (self.root / "resumes" / "versions.json").read_bytes()
        folders = sorted(path.name for path in (self.root / "resumes").iterdir())
        for version in ("1.0.0", "0.9.9"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                resumelib.create_version(self.root, version=version, note="test",
                                         content_html="<h1>Name</h1>")
        self.assertEqual((self.root / "resumes" / "versions.json").read_bytes(), manifest)
        self.assertEqual(sorted(path.name for path in (self.root / "resumes").iterdir()), folders)

    def test_html_normalization_and_links(self):
        original = ('<div><h1><span style="color:red">A</span> B</h1>'
                    '<p>one<br>two <b>bold</b> <a href="javascript:alert(1)">bad</a> '
                    '<a href="https://example.com/x">good</a> github.com/name/repo '
                    'person@example.com</p><script>alert(1)</script><style>p{}</style>'
                    '<p>   </p><ul><li>entry</li><li> </li></ul></div>')
        normalized = resumelib.normalize_content(original)
        self.assertNotRegex(normalized, r"(?i)<(?:span|div|br|b|script|style)\b|javascript:|style=")
        self.assertIn('<p class="body">', normalized)
        self.assertIn('<a href="https://github.com/name/repo">github.com/name/repo</a>', normalized)
        self.assertIn('<a href="mailto:person@example.com">person@example.com</a>', normalized)
        self.assertIn('bold bad <a href="https://example.com/x">good</a>', normalized)
        self.assertEqual([block.text for block in resumelib.parse_content(normalized)],
                         ["A B", "one two bold bad good github.com/name/repo person@example.com", "entry"])
        self.assertEqual(resumelib.normalize_content(normalized), normalized)

    def test_original_content_text_is_preserved(self):
        source = self.root / "resumes" / "v1.0.0" / "content.html"
        if source.exists():
            original = source.read_text(encoding="utf-8")
        else:
            page = (SOURCE_ROOT / "resume.html").read_text(encoding="utf-8")
            original = re.search(r'<div class="paper">(.*?)</div>', page, re.DOTALL).group(1)
        before = [block.text for block in resumelib.parse_content(original)]
        after = [block.text for block in resumelib.parse_content(resumelib.normalize_content(original))]
        self.assertTrue(before)
        self.assertEqual(after, before)

    def test_docx_contains_every_block_as_one_paragraph(self):
        content = ('<h1>Heading</h1><p class="sub">contact@example.com</p>'
                   '<h2>Section</h2><ul><li>One</li><li>Two</li></ul>'
                   '<h3>Project</h3><p class="meta">2026</p><p class="body">Body</p>')
        result = resumelib.create_version(self.root, bump="patch", note="test", content_html=content,
                                          date="2026-10-02")
        self.assertEqual(result["version"], "1.0.1")
        docx = self.root / result["path"] / "resume.docx"
        with ZipFile(docx) as archive:
            document = ET.fromstring(archive.read("word/document.xml"))
        paragraphs = document.findall(".//w:body/w:p", NS)
        blocks = resumelib.parse_content(content)
        self.assertEqual(len(paragraphs), len(blocks))
        for paragraph, block in zip(paragraphs, blocks):
            text = "".join(node.text or "" for node in paragraph.findall(".//w:t", NS))
            self.assertEqual(text, block.text)
        manifest = json.loads((self.root / "resumes" / "versions.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["versions"][0]["version"], "1.0.1")

    def test_docx_failure_rolls_back_folder_and_manifest(self):
        manifest = (self.root / "resumes" / "versions.json").read_bytes()
        (self.root / "scripts" / "resume_template.docx").unlink()
        with self.assertRaises(FileNotFoundError):
            resumelib.create_version(self.root, bump="minor", note="failure",
                                     content_html="<h1>Heading</h1>")
        self.assertEqual((self.root / "resumes" / "versions.json").read_bytes(), manifest)
        self.assertFalse((self.root / "resumes" / "v1.1.0").exists())
        self.assertFalse(list((self.root / "resumes").glob(".v1.1.0-*")))

    def test_manifest_failure_rolls_back_created_folder(self):
        manifest = (self.root / "resumes" / "versions.json").read_bytes()
        with patch.object(resumelib.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                resumelib.create_version(self.root, bump="minor", note="failure",
                                         content_html="<h1>Heading</h1>")
        self.assertEqual((self.root / "resumes" / "versions.json").read_bytes(), manifest)
        self.assertFalse((self.root / "resumes" / "v1.1.0").exists())
        self.assertFalse(list((self.root / "resumes").glob(".versions-*")))

    def test_cli_copies_latest_and_rejects_duplicate(self):
        command = [sys.executable, str(SOURCE_ROOT / "scripts" / "new_version.py"),
                   "--root", str(self.root), "--bump", "patch", "--note", "test"]
        created = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(Path(created.stdout.strip()), (self.root / "resumes" / "v1.0.1").resolve())
        latest = (self.root / "resumes" / "v1.0.1" / "content.html").read_text(encoding="utf-8")
        with ZipFile(self.root / "resumes" / "v1.0.1" / "resume.docx") as archive:
            document = ET.fromstring(archive.read("word/document.xml"))
        docx_text = ["".join(node.text or "" for node in paragraph.findall(".//w:t", NS))
                     for paragraph in document.findall(".//w:body/w:p", NS)]
        self.assertEqual(docx_text, [block.text for block in resumelib.parse_content(latest)])
        manifest = (self.root / "resumes" / "versions.json").read_bytes()
        duplicate = subprocess.run([sys.executable, str(SOURCE_ROOT / "scripts" / "new_version.py"),
                                    "--root", str(self.root), "--version", "1.0.1", "--note", "duplicate"],
                                   capture_output=True, text=True, check=False)
        self.assertEqual(duplicate.returncode, 1)
        self.assertEqual((self.root / "resumes" / "versions.json").read_bytes(), manifest)



class NestedListTests(unittest.TestCase):
    def test_nested_list_items_become_separate_items(self):
        from scripts.resumelib import normalize_content
        html_text = "<ul><li>바깥<ul><li>안쪽</li></ul></li><li>다음</li></ul>"
        self.assertEqual(normalize_content(html_text), "<ul>\n<li>바깥</li>\n<li>안쪽</li>\n<li>다음</li>\n</ul>\n")


if __name__ == "__main__":
    unittest.main()
