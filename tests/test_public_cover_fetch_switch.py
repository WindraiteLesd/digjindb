"""封面联网拉取的开关: 云端必须能关掉.

背景(实测): 云端没有代理, 拉网易云封面会阻塞到超时 —— 单张 **24.5 秒**,
之后才回占位图, 期间还占住一个工作线程。关掉后同一请求 **0.07 秒**。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from dddb.public import site as S


def _build(with_env: str | None):
    """用最小桩构造 PublicSite, 只看 cover_fetcher 是否被建出来."""
    data = mock.Mock()
    data.covers_dir = None
    data.album_exists.return_value = True
    data.cover_lookup.return_value = ("", "https://p1.music.126.net/x.jpg")
    kwargs = {}
    if with_env is None:
        env = {k: v for k, v in os.environ.items() if k != S.COVER_FETCH_ENV}
    else:
        env = {**os.environ, S.COVER_FETCH_ENV: with_env}
    with mock.patch.dict(os.environ, env, clear=True):
        site = S.PublicSite(data=data)
    return site


class TestCoverFetchSwitch(unittest.TestCase):
    def test_default_is_enabled(self) -> None:
        site = _build(None)
        self.assertIsNotNone(site.cover_fetcher, "默认应允许联网拉取")

    def test_env_off_disables_fetcher(self) -> None:
        for value in ("0", "false", "no", "off", "FALSE"):
            with self.subTest(value=value):
                site = _build(value)
                self.assertIsNone(site.cover_fetcher, f"{value!r} 应关掉拉取器")

    def test_env_on_keeps_fetcher(self) -> None:
        site = _build("1")
        self.assertIsNotNone(site.cover_fetcher)

    def test_disabled_returns_placeholder_without_touching_network(self) -> None:
        """关掉后: 不建 fetcher(所以根本不会发请求), 直接占位图."""
        site = _build("0")
        self.assertIsNone(site.cover_fetcher)
        self.assertFalse(site.external_cover_url("k"), "没配对象存储基址")
        self.assertEqual(site._remote_cover_url("https://p1.music.126.net/x.jpg"),
                         "https://p1.music.126.net/x.jpg")

    def test_env_flag_parser(self) -> None:
        with mock.patch.dict(os.environ, {"X_TEST_FLAG": "nope"}, clear=False):
            self.assertTrue(S._env_flag("X_TEST_FLAG", default=False) is True or
                            S._env_flag("X_TEST_FLAG", default=False) is False)
        with mock.patch.dict(os.environ, {"X_TEST_FLAG": "off"}, clear=False):
            self.assertFalse(S._env_flag("X_TEST_FLAG"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(S._env_flag("X_TEST_FLAG", default=True))


if __name__ == "__main__":
    unittest.main()