"""HTML 解析与 CSS 选择器测试."""

from __future__ import annotations

import unittest

from dddb import htmlutil as H

PAGE = """<!doctype html>
<html lang="ja"><head>
  <meta charset="utf-8">
  <title>M3-2026秋 同人音楽</title>
  <meta property="og:image" content="https://example.com/cover.jpg">
  <meta name="description" content="テスト">
  <script type="application/ld+json">
  {"@type":"MusicAlbum","name":"幻想郷の記憶","byArtist":{"name":"ShibayanRecords"}}
  </script>
</head><body>
  <div id="main">
    <h1 class="title">幻想郷の記憶</h1>
    <a class="artist" href="/artist/123">ShibayanRecords</a>
    <a class="artist" href="/artist/456">坂上なち</a>
  </div>
  <table id="info">
    <tr><td>Catalog Number</td><td>SBCD-042</td></tr>
    <tr><td>Release Date</td><td>2026-10-25</td></tr>
  </table>
  <div id="tracklist">
    <span class="track"><span class="n">1</span>Opening<span class="time">1:23</span></span>
    <span class="track"><span class="n">2</span>幻想郷の記憶<span class="time">4:05</span></span>
  </div>
  <ul class="tags"><li>東方Project</li><li>Electronic</li><li>Vocal</li></ul>
  <p class="empty"></p>
  <div class="wrap"><span class="a">1</span><span class="b">2</span><span class="a">3</span></div>
  <!-- comment -->
</body></html>
"""


class TestParse(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = H.parse_html(PAGE, url="https://example.com/album/1")

    def test_title_and_meta(self) -> None:
        self.assertIn("M3-2026秋", self.doc.title)
        self.assertEqual(H.og_image(self.doc), "https://example.com/cover.jpg")
        self.assertEqual(self.doc.meta.get("description"), "テスト")

    def test_json_ld(self) -> None:
        blocks = H.json_ld_blocks(self.doc)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["name"], "幻想郷の記憶")

    def test_table_rows(self) -> None:
        table = self.doc.select_one("table#info")
        self.assertIsNotNone(table)
        rows = H.table_rows(table)
        self.assertEqual(rows[0], ["Catalog Number", "SBCD-042"])
        self.assertEqual(rows[1][1], "2026-10-25")

    def test_text_content(self) -> None:
        h1 = self.doc.select_one("h1.title")
        self.assertEqual(h1.text_content(), "幻想郷の記憶")


class TestSelectors(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = H.parse_html(PAGE)

    def test_tag_and_class(self) -> None:
        self.assertEqual(len(self.doc.select("a.artist")), 2)
        self.assertEqual(self.doc.select_one("a.artist").text_content(), "ShibayanRecords")

    def test_descendant_and_child(self) -> None:
        self.assertEqual(len(self.doc.select("#main a")), 2)
        self.assertEqual(len(self.doc.select("#main > a")), 2)
        self.assertEqual(len(self.doc.select("body > div#main a")), 2)
        self.assertEqual(len(self.doc.select("table > tbody > tr")), 0)  # 没有显式 tbody

    def test_attribute_selectors(self) -> None:
        self.assertEqual(len(self.doc.select('a[href="/artist/123"]')), 1)
        self.assertEqual(len(self.doc.select('a[href^="/artist/"]')), 2)
        self.assertEqual(len(self.doc.select('a[href$="/456"]')), 1)
        self.assertEqual(len(self.doc.select('a[href*="artist"]')), 2)
        self.assertEqual(len(self.doc.select("a[href]")), 2)

    def test_group_and_nth(self) -> None:
        self.assertEqual(len(self.doc.select("h1, table, ul")), 3)
        self.assertEqual(len(self.doc.select(".wrap span:nth-child(2)")), 1)
        self.assertEqual(self.doc.select_one(".wrap span:nth-child(2)").text_content(), "2")

    def test_first_last_child(self) -> None:
        first = self.doc.select_one(".wrap span:first-child")
        last = self.doc.select_one(".wrap span:last-child")
        self.assertEqual(first.text_content(), "1")
        self.assertEqual(last.text_content(), "3")

    def test_not_and_contains(self) -> None:
        self.assertEqual(len(self.doc.select("a.artist:not([href$='/456'])")) , 1)
        found = self.doc.select('div:contains("ShibayanRecords")')
        self.assertTrue(found)

    def test_empty_pseudo(self) -> None:
        self.assertIsNotNone(self.doc.select_one("p.empty"))

    def test_void_and_implicit_close(self) -> None:
        doc = H.parse_html("<ul><li>a<li>b<li>c</ul><p>one<p>two")
        self.assertEqual([n.text_content() for n in doc.select("li")], ["a", "b", "c"])
        self.assertEqual(len(doc.select("p")), 2)

    def test_malformed_html_survives(self) -> None:
        doc = H.parse_html("<div><span>未闭合<div><a href='x'>link</a>")
        self.assertTrue(doc.select("a[href='x']"))


class TestHelpers(unittest.TestCase):
    def test_text_of(self) -> None:
        self.assertIn("Hello", H.text_of("<div><b>Hello</b> world</div>"))

    def test_absolutize(self) -> None:
        self.assertEqual(H.absolutize("/a", "https://x.com/b"), "https://x.com/a")
        self.assertEqual(H.absolutize("//cdn.x.com/a.png", "https://x.com"), "https://cdn.x.com/a.png")
        self.assertEqual(H.absolutize("https://y.com/z", "https://x.com"), "https://y.com/z")

    def test_outer_html(self) -> None:
        doc = H.parse_html('<a href="/x" class="c">Hi</a>')
        node = doc.select_one("a")
        self.assertIn('href="/x"', node.outer_html())
        self.assertIn("Hi", node.outer_html())


if __name__ == "__main__":
    unittest.main()
