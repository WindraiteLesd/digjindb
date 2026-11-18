"""i18n 测试: 语言规整 / 翻译回退 / 导航渲染 / 路由."""

from __future__ import annotations

import unittest

from dddb.public import i18n
from dddb.public import templates as T
from dddb.public import site as S


class TestI18n(unittest.TestCase):
    def test_language_normalisation(self) -> None:
        self.assertEqual(i18n.language_of("zh-CN"), "zh")
        self.assertEqual(i18n.language_of("en_US"), "en")
        self.assertEqual(i18n.language_of("JA"), "ja")
        self.assertEqual(i18n.language_of("de"), "zh")   # 不支持的回退中文
        self.assertEqual(i18n.language_of(""), "zh")

    def test_translate_and_fallback(self) -> None:
        self.assertEqual(i18n.translate("nav.albums", "en"), "Albums")
        self.assertEqual(i18n.translate("nav.albums", "ja"), "アルバム")
        self.assertEqual(i18n.translate("nav.albums", "zh"), "浏览")
        self.assertEqual(i18n.translate("no.such.key", "en", "X"), "X")

    def test_nav_renders_english(self) -> None:
        ctx = T.SiteContext(csrf_token="t", lang="en")
        html = T.layout(ctx, title="T", body="<p>x</p>")
        self.assertIn('href="/albums">Albums<', html)
        self.assertIn('href="/artists">Artists<', html)
        self.assertIn('lang="en"', html)

    def test_nav_renders_japanese(self) -> None:
        ctx = T.SiteContext(csrf_token="t", lang="ja")
        html = T.layout(ctx, title="T", body="<p>x</p>")
        self.assertIn("アルバム", html)
        self.assertIn('lang="ja"', html)

    def test_lang_route(self) -> None:
        self.assertEqual(S.parse_route("/lang/en")["name"], "lang")
        self.assertEqual(S.parse_route("/lang/ja")["arg"], "ja")


if __name__ == "__main__":
    unittest.main()