"""测试: 本地化覆盖 + 外观设置(主题/背景/参数)."""

from __future__ import annotations

import unittest

from dddb.public import i18n
from dddb.public import templates as T
from dddb.public import site as S


class TestLocalisation(unittest.TestCase):
    def test_all_three_languages_cover_keys(self) -> None:
        missing = []
        for key, entry in i18n.STRINGS.items():
            for lang in ("zh", "en", "ja"):
                if not entry.get(lang):
                    missing.append((key, lang))
        self.assertEqual(missing, [], f"缺翻译: {missing[:8]}")

    def test_key_groups_present(self) -> None:
        groups = {key.split(".")[0] for key in i18n.STRINGS}
        for group in ("nav", "page", "section", "field", "btn", "msg"):
            self.assertIn(group, groups)

    def test_english_page_is_translated(self) -> None:
        ctx = T.SiteContext(csrf_token="t", lang="en")
        html = T.artist_detail_page(ctx, artist={
            "name": "A", "album_count": 0, "albums": [], "circles": [],
            "occupations": [], "relations": ["B"]})
        self.assertIn("Relations", html)
        self.assertIn("Description", html)
        self.assertNotIn("人际关系", html)

    def test_japanese_page_is_translated(self) -> None:
        ctx = T.SiteContext(csrf_token="t", lang="ja")
        html = T.artist_detail_page(ctx, artist={
            "name": "A", "album_count": 0, "albums": [], "circles": [],
            "occupations": [], "relations": ["B"]})
        self.assertIn("関係", html)

    def test_chinese_stays_chinese(self) -> None:
        ctx = T.SiteContext(csrf_token="t", lang="zh")
        html = T.artist_detail_page(ctx, artist={
            "name": "A", "album_count": 0, "albums": [], "circles": [],
            "occupations": [], "relations": ["B"]})
        self.assertIn("人际关系", html)

    def test_no_translate_leak_in_output(self) -> None:
        ctx = T.SiteContext(csrf_token="t", lang="en")
        html = T.artist_detail_page(ctx, artist={
            "name": "A", "album_count": 0, "albums": [], "circles": [],
            "occupations": [], "relations": []})
        self.assertNotIn("i18n.translate(", html, "模板字面量不应泄漏到页面")


class TestAppearance(unittest.TestCase):
    def _page(self, tab: str = "appearance") -> str:
        return T.account_page(
            T.SiteContext(csrf_token="t"),
            user={"id": 1, "username": "u", "role": "viewer", "role_label": "用户"},
            profile={}, visibility={}, tab=tab,
        )

    def test_appearance_tab_exists_in_sidebar(self) -> None:
        self.assertIn('href="/account?tab=appearance"', self._page())

    def test_theme_swatches_present(self) -> None:
        html = self._page()
        self.assertIn("theme-swatch", html)
        for theme in ("dark", "light", "warm", "contrast", "forest", "sakura"):
            self.assertIn(f'data-theme-value="{theme}"', html, f"缺配色 {theme}")

    def test_background_and_parameters(self) -> None:
        html = self._page()
        # 背景图改为**本地上传**(不用外链), 所以检查的是上传表单
        self.assertIn('action="/account/background"', html, "背景图上传表单")
        self.assertIn('name="background"', html, "文件字段")
        self.assertIn('id="pref-bg-dim"', html, "背景遮罩滑杆")
        self.assertIn('id="pref-bg-blur"', html, "背景模糊滑杆")
        self.assertIn('id="pref-density"', html, "密度选择")
        self.assertIn('id="pref-radius"', html, "圆角选择")
        self.assertIn('id="pref-font"', html, "字号选择")
        self.assertIn('id="pref-reset"', html, "恢复默认")

    def test_css_defines_theme_variants(self) -> None:
        from pathlib import Path

        css = (Path(__file__).resolve().parents[1]
               / "dddb/public/static/public.css").read_text(encoding="utf-8")
        for theme in ("light", "warm", "contrast", "forest", "sakura"):
            self.assertIn(f'[data-theme="{theme}"]', css, f"CSS 缺 {theme} 主题")
        self.assertIn("--bg-image", css)
        self.assertIn("--bg-dim", css)
        self.assertIn("--bg-blur", css)

    def test_privacy_tab_still_intact(self) -> None:
        html = self._page("privacy")
        self.assertIn('action="/account/privacy"', html)
        self.assertIn('name="accept_messages"', html)


class TestLangRoutes(unittest.TestCase):
    def test_lang_route_supports_three(self) -> None:
        for lang in ("zh", "en", "ja"):
            route = S.parse_route(f"/lang/{lang}")
            self.assertEqual(route["name"], "lang")
            self.assertEqual(route["arg"], lang)


if __name__ == "__main__":
    unittest.main()