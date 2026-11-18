"""领域模型 / 配置 / 持久层测试."""

from __future__ import annotations

import unittest
from pathlib import Path

from dddb.config import Config, write_example_config
from dddb.db import Store
from dddb.models import Album, Credit, MatchResult, Track
from tests._tmp import temp_dir


def make_album(**kw) -> Album:
    base = dict(
        title="幻想郷の記憶",
        title_original="幻想郷の記憶",
        circle="ShibayanRecords",
        release_date="2026-10-25",
        event="M3-2026秋",
        cover_url="https://example.com/c.jpg",
        tracks=[Track(title="Opening", index=1, duration=83), Track(title="幻想郷の記憶", index=2, duration=245)],
        credits=[Credit(name="ShibayanRecords", role="circle")],
        urls={"vgmdb": "https://vgmdb.net/album/1"},
        source_ids={"vgmdb": "1"},
    )
    base.update(kw)
    return Album(**base)


class TestAlbum(unittest.TestCase):
    def test_key_stable(self) -> None:
        a = make_album()
        b = make_album()
        self.assertEqual(a.key, b.key)
        self.assertIn("shibayanrecords", a.key)

    def test_merge_fills_gaps(self) -> None:
        a = make_album(cover_url="", tracks=[])
        b = make_album(cover_url="https://example.com/x.jpg", tracks=[Track(title="T1")],
                       urls={"bangumi": "https://bgm.tv/subject/1"}, source_ids={"bangumi": "1"})
        a.merge(b)
        self.assertEqual(a.cover_url, "https://example.com/x.jpg")
        self.assertEqual(len(a.tracks), 1)
        self.assertIn("bangumi", a.urls)
        self.assertIn("vgmdb", a.urls)

    def test_merge_prefers_longer_tracklist(self) -> None:
        a = make_album(tracks=[Track(title="only")])
        b = make_album(tracks=[Track(title=f"t{i}") for i in range(5)])
        a.merge(b)
        self.assertEqual(len(a.tracks), 5)

    def test_merge_credits_dedupe(self) -> None:
        a = make_album(credits=[Credit(name="A", role="circle")])
        b = make_album(credits=[Credit(name="A", role="circle"), Credit(name="B", role="vocal")])
        a.merge(b)
        self.assertEqual(len(a.credits), 2)

    def test_roundtrip_json(self) -> None:
        a = make_album()
        back = Album.from_json(a.to_json())
        self.assertEqual(back.key, a.key)
        self.assertEqual(back.to_dict(), a.to_dict())

    def test_event_kind_autodetect(self) -> None:
        self.assertEqual(make_album(event="M3-2026秋").event_kind, "m3")
        self.assertEqual(make_album(event="C107").event_kind, "comiket")


class TestConfig(unittest.TestCase):
    def test_defaults_and_proxy_routing(self) -> None:
        cfg = Config.default()
        # 默认 mode="bypass": 除了直连名单, 其余全走代理。
        # 这个默认值是被真实故障逼出来的 —— 之前 mode="hosts" 时漏配了
        # lain.bgm.tv(bgm 的封面 CDN), 于是所有封面直连超时 43 秒、图片显示不出来。
        self.assertEqual(cfg.proxy.mode, "bypass")
        self.assertTrue(cfg.proxy.should_proxy("https://vgmdb.net/album/1"))
        self.assertTrue(cfg.proxy.should_proxy("https://www.dojin-music.info/eventV1"))
        self.assertTrue(cfg.proxy.should_proxy("https://api.bgm.tv/v0/subjects/1"))
        self.assertTrue(cfg.proxy.should_proxy("https://lain.bgm.tv/pic/cover/l/12/dd/x.jpg"),
                        "回归: bgm 的封面 CDN 必须走代理")
        self.assertTrue(cfg.proxy.should_proxy("https://bgm.tv/group/doujinmusic"))
        self.assertTrue(cfg.proxy.should_proxy("https://doujinstyle.com/?p=albums"))
        self.assertTrue(cfg.proxy.should_proxy("https://anything-new.example/x"),
                        "bypass 模式下新增站点自动走代理, 不用再维护名单")
        self.assertFalse(cfg.proxy.should_proxy("https://music.163.com/weapi/x"))
        self.assertFalse(cfg.proxy.should_proxy("http://127.0.0.1:3000/login/status"))

    def test_proxy_mode_hosts_still_available(self) -> None:
        """mode="hosts" 的旧行为要保留(有些人只想过白名单)。"""
        cfg = Config.default()
        cfg.proxy.mode = "hosts"
        self.assertTrue(cfg.proxy.should_proxy("https://vgmdb.net/album/1"))
        self.assertFalse(cfg.proxy.should_proxy("https://anything-new.example/x"))
        self.assertFalse(cfg.proxy.should_proxy("https://music.163.com/weapi/x"))

    def test_load_example_config(self) -> None:
        with temp_dir("cfg-") as root:
            path = root / "config" / "config.toml"
            write_example_config(path)
            cfg = Config.load(root=root)
            self.assertIsNotNone(cfg.config_file)
            self.assertEqual(cfg.netease.playlist_mode, "per_event")
            self.assertIn("doujinstyle", cfg.sources.enabled)
            self.assertNotIn("thwiki", cfg.sources.enabled)
            self.assertNotIn("touhoudb", cfg.sources.enabled)
            self.assertNotIn("x", cfg.sources.enabled)
            self.assertAlmostEqual(cfg.match.min_score, 0.72)

    def test_from_dict_partial(self) -> None:
        cfg = Config.from_dict({"netease": {"playlist_prefix": "测试"}, "match": {"min_score": 0.5}})
        self.assertEqual(cfg.netease.playlist_prefix, "测试")
        self.assertAlmostEqual(cfg.match.min_score, 0.5)
        self.assertTrue(cfg.netease.like_album)  # 未指定的保持默认


class TestStore(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("store-")
        self.tmp = self._ctx.__enter__()
        self.store = Store(Path(self.tmp) / "t.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_upsert_and_get(self) -> None:
        album = make_album()
        self.assertEqual(self.store.upsert_album(album), "created")
        self.assertEqual(self.store.upsert_album(make_album()), "unchanged")
        self.assertIsNotNone(self.store.get_album(album.key))
        self.assertIsNotNone(self.store.get_album_by_source("vgmdb", "1"))

    def test_cross_source_merge_in_store(self) -> None:
        self.store.upsert_album(make_album())
        other = make_album(
            source_ids={"bangumi": "999"},
            urls={"bangumi": "https://bgm.tv/subject/999"},
            title_romaji="Gensokyo no Kioku",
            cover_url="",
            tracks=[],
        )
        # 不同 key(没有 romaji 参与 key)但标题/社团相同 -> 应命中相似合并
        status = self.store.upsert_album(other)
        self.assertIn(status, ("updated", "unchanged"))
        merged = self.store.get_album_by_source("bangumi", "999")
        self.assertIsNotNone(merged)
        self.assertIn("vgmdb", merged.source_ids)

    def test_filters(self) -> None:
        self.store.upsert_album(make_album())
        self.store.upsert_album(make_album(title="別のアルバム", circle="Other Circle",
                                           source_ids={"vgmdb": "2"}, urls={}))
        self.assertEqual(len(self.store.list_albums(event="M3-2026秋")), 2)
        self.assertEqual(len(self.store.list_albums(query="Other")), 1)
        self.assertEqual(len(self.store.list_albums(tag="M3-2026秋")), 2)
        self.assertEqual(len(self.store.list_albums(source="vgmdb")), 2)

    def test_matches(self) -> None:
        m = MatchResult(album_key="k", target_id=1, target_name="N", score=0.9)
        self.store.save_match(m)
        self.assertIsNotNone(self.store.get_match("k"))
        self.assertEqual(len(self.store.list_matches(min_score=0.8)), 1)

    def test_counts(self) -> None:
        self.store.upsert_album(make_album())
        counts = self.store.counts()
        self.assertEqual(counts["albums"], 1)
        self.assertEqual(counts["album_sources"], 1)


if __name__ == "__main__":
    unittest.main()
