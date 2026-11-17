"""测试: 曲目补充字段(语种/曲风/副标题/翻译)的完整存储链路."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


class TestTrackExtraFieldsModel(unittest.TestCase):
    """Track 模型支持 subtitle/title_zh/lang/genre."""

    def test_fields_exist(self) -> None:
        from dddb.models import Track

        track = Track(title="曲", subtitle="副", title_zh="译",
                      lang="日语", genre="Trance")
        self.assertEqual(track.subtitle, "副")
        self.assertEqual(track.title_zh, "译")
        self.assertEqual(track.lang, "日语")
        self.assertEqual(track.genre, "Trance")

    def test_defaults_empty(self) -> None:
        from dddb.models import Track

        track = Track(title="曲")
        self.assertEqual(track.subtitle, "")
        self.assertEqual(track.title_zh, "")
        self.assertEqual(track.lang, "")
        self.assertEqual(track.genre, "")

    def test_to_dict(self) -> None:
        from dddb.models import Track

        data = Track(title="曲", lang="英语").to_dict()
        self.assertEqual(data["lang"], "英语")
        for key in ("subtitle", "title_zh", "lang", "genre"):
            self.assertIn(key, data)

    def test_from_dict(self) -> None:
        from dddb.models import Track

        track = Track.from_dict({
            "title": "曲", "subtitle": "副", "title_zh": "译",
            "lang": "日语", "genre": "Rock",
        })
        self.assertEqual(track.subtitle, "副")
        self.assertEqual(track.title_zh, "译")
        self.assertEqual(track.lang, "日语")
        self.assertEqual(track.genre, "Rock")

    def test_stripped(self) -> None:
        from dddb.models import Track

        track = Track(title="曲", lang="  日语  ")
        self.assertEqual(track.lang, "日语")


class TestTrackExtraFieldsParse(unittest.TestCase):
    """表单解析支持补充字段."""

    def test_parse_all(self) -> None:
        from dddb.public.site import parse_album_form

        patch, errors = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "track_disc": [""], "track_index": ["1"],
            "track_title": ["曲一"],
            "track_subtitle": ["副标题X"], "track_title_zh": ["译名Y"],
            "track_lang": ["日语"], "track_genre": ["Trance"],
        })
        self.assertEqual(errors, {})
        track = patch["track_list"][0]
        self.assertEqual(track["subtitle"], "副标题X")
        self.assertEqual(track["title_zh"], "译名Y")
        self.assertEqual(track["lang"], "日语")
        self.assertEqual(track["genre"], "Trance")

    def test_parse_empty(self) -> None:
        from dddb.public.site import parse_album_form

        patch, _errors = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "track_disc": [""], "track_index": ["1"], "track_title": ["曲"],
        })
        track = patch["track_list"][0]
        for key in ("subtitle", "title_zh", "lang", "genre"):
            self.assertEqual(track[key], "", f"{key} 应为空")

    def test_multiple_tracks_aligned(self) -> None:
        """多首曲目的补充字段不能错位."""
        from dddb.public.site import parse_album_form

        patch, _ = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "track_disc": ["1", "1"], "track_index": ["1", "2"],
            "track_title": ["曲一", "曲二"],
            "track_lang": ["日语", "英语"],
            "track_genre": ["Trance", "Rock"],
        })
        tracks = patch["track_list"]
        self.assertEqual(tracks[0]["lang"], "日语")
        self.assertEqual(tracks[1]["lang"], "英语")
        self.assertEqual(tracks[0]["genre"], "Trance")
        self.assertEqual(tracks[1]["genre"], "Rock")


class TestTrackExtraFieldsPersist(unittest.TestCase):
    """补充字段要真的落库并能读回."""

    def test_roundtrip(self) -> None:
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        ctx = temp_dir("trkextra-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            store = Store(config.db_path)
            store.upsert_album(Album.from_dict({
                "key": "t", "title": "T", "tracks": [], "credits": [],
                "source_ids": {}}))
            ok, message = store.update_album_fields("t", {
                "track_list": [{
                    "disc": 1, "index": 1, "title": "曲一",
                    "artists": [], "duration": 100,
                    "subtitle": "副标题X", "title_zh": "译名Y",
                    "lang": "日语", "genre": "Trance",
                }],
            })
            self.assertTrue(ok, message)
            saved = store.get_album("t")
            store.close()
            track = saved.tracks[0]
            self.assertEqual(track.subtitle, "副标题X")
            self.assertEqual(track.title_zh, "译名Y")
            self.assertEqual(track.lang, "日语")
            self.assertEqual(track.genre, "Trance")
        finally:
            ctx.__exit__(None, None, None)

    def test_edit_values_outputs_extras(self) -> None:
        """编辑页回填时要带出补充字段."""
        from dddb.public import templates as T

        values = T.edit_values_from_album({
            "key": "t", "title": "T", "title_original": "T", "urls": {},
            "credits": [],
            "tracks": [{
                "disc": 1, "index": 1, "title": "曲",
                "artists": [], "duration": 100,
                "subtitle": "副", "title_zh": "译",
                "lang": "日语", "genre": "Rock",
            }],
        })
        row = values["tracks"][0]
        self.assertEqual(row["subtitle"], "副")
        self.assertEqual(row["title_zh"], "译")
        self.assertEqual(row["lang"], "日语")
        self.assertEqual(row["genre"], "Rock")


class TestDetailShowsExtras(unittest.TestCase):
    """详情页: 有值才显示, 默认隐藏."""

    def _detail(self, tracks):
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
        return T.album_detail_page(ctx, album={
            "key": "t", "title": "T", "title_original": "T", "urls": {},
            "credits": [], "tags": [], "genres": [], "missing": [],
            "tracks": tracks, "track_count": len(tracks),
        })

    def test_shows_when_present(self) -> None:
        html = self._detail([{
            "disc": 1, "index": 1, "title": "曲", "artists": [],
            "duration": 100, "duration_text": "1:40",
            "lang": "日语", "genre": "Trance",
        }])
        self.assertIn("日语", html)
        self.assertIn("Trance", html)
        self.assertIn("track-toggle", html)
        self.assertIn('track-extra-row" hidden', html, "默认隐藏")

    def test_hidden_when_absent(self) -> None:
        html = self._detail([{
            "disc": 1, "index": 1, "title": "曲", "artists": [],
            "duration": 100, "duration_text": "1:40",
        }])
        self.assertNotIn("track-toggle", html)
        self.assertNotIn("track-extra-row", html)


if __name__ == "__main__":
    unittest.main()
