"""新建专辑时粘贴的曲目必须落库.

真实事故(m10181): 站长端「导出这张」给出的地址是 ``/new/album``(新建页),
用户把 infobox 贴进编辑页点「填入」后曲目全丢 —— 因为
``_do_new_entity`` 的 album 分支只读 ``track_*`` 行, **完全没读
``track_paste``**; 而编辑页 ``_do_album_edit`` 早就有这一步。

这个测试走完整 HTTP(和浏览器一样发 multipart), 不是直接调内部方法 ——
绕过 HTTP 层的测试会全绿而线上仍然丢数据(参见 tests/test_multipart_dispatch.py)。
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]

B = "----newalbumtracks"

SAMPLE = """1. What I thought (feat. Letsu) / N_dog、Letsu
2. WOW'N'FLUTTER (feat. icesawder & Yutori) / N_dog、icesawder、Yutori
3. OUTRO (DAC) / N_dog"""


def _multipart(fields: dict[str, str]) -> tuple[dict, bytes]:
    parts = []
    for k, v in fields.items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}'
             .format(B, k, v)).encode())
    body = b"\r\n".join(parts) + b"\r\n--" + B.encode() + b"--\r\n"
    return ({"content-type": "multipart/form-data; boundary={}".format(B)},
            body)


class TestNewAlbumPasteTracks(unittest.TestCase):
    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("new-album-tracks")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "dddb.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.save_series("M3", abbr="M3", full_name="M3")
        store.save_event("M3-2026秋", series="M3", vol=58, vol_label="M3-58")
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3", covers_dir=self.covers)
        self.site.can_edit = lambda s: True            # type: ignore

    def tearDown(self) -> None:
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        from dddb.public import auth

        st = auth.AuthState()
        st.session = auth.Session(sid="t", user_id=1, username="e",
                                  role="editor")
        return st

    def _post(self, fields: dict[str, str]):
        headers, body = _multipart(fields)
        st = self._state()
        st.csrf_token = "testtoken"
        self.site.sessions.state = lambda cookies: st              # type: ignore
        self.site.same_origin = lambda *a, **k: (True, "")         # type: ignore
        signer = self.site.sessions.signer
        orig = signer.check
        signer.check = lambda binding, given: True                 # type: ignore
        try:
            return self.site.handle(
                "POST", "/new/album",
                headers={**headers, "host": "127.0.0.1:9",
                         "origin": "http://127.0.0.1:9"},
                body=body)
        finally:
            signer.check = orig

    def test_pasted_tracks_are_saved(self) -> None:
        resp = self._post({
            "title_original": "AD/DA CONVERTER",
            "circle": "N_dog",
            "release_date": "2026-10-30",
            "artists": "N_dog\nLetsu",
            "track_paste": SAMPLE,
            "csrf_token": "testtoken",
        })
        self.assertLess(resp.status, 400,
                        "提交被拒: {}".format((resp.body or b"")[:300]))

        from dddb.db import Store

        store = Store(self.db)
        try:
            albums = store.list_albums()
            self.assertEqual(len(albums), 1, "专辑没建出来")
            album = albums[0]
            payload = album.to_dict()
        finally:
            store.close()

        tracks = payload.get("tracks") or []
        self.assertEqual(len(tracks), 3, "粘贴的 3 首曲目丢了一部分: {}".format(tracks))
        self.assertEqual([t.get("title") for t in tracks],
                         ["What I thought (feat. Letsu)",
                          "WOW'N'FLUTTER (feat. icesawder & Yutori)",
                          "OUTRO (DAC)"])
        # 「、」必须拆开, 否则消歧义/按艺人检索全失败
        self.assertEqual(tracks[0].get("artists"), ["N_dog", "Letsu"])

    def test_without_paste_still_works(self) -> None:
        resp = self._post({
            "title_original": "No Tracks",
            "circle": "X",
            "track_paste": "",
            "csrf_token": "testtoken",
        })
        self.assertLess(resp.status, 400)

        from dddb.db import Store

        store = Store(self.db)
        try:
            albums = store.list_albums()
            self.assertEqual(len(albums), 1)
            self.assertEqual(albums[0].to_dict().get("tracks") or [], [])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
