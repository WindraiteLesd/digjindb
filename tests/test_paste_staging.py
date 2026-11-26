"""整段粘贴的**前端暂存**逻辑(用户 m16094)。

两边各有一套实现:
  - 服务端 ``dddb/trackparse.py`` / ``dddb/creditparse.py``(表单照常提交时用)
  - 前端 ``dddb/public/static/public.js`` 的 ``parsePasteTracks`` /
    ``parsePasteCredits``(点按钮时先填进下面的表格)

这里既比对两边结果一致, 也用 node 跑前端真源码验证"点了按钮不提交表单、
只把行填进表格"的行为。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from dddb.creditparse import parse_credits
from dddb.trackparse import parse_tracklist

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "dddb/public/static/public.js"

PASTE_TEXT = (
    "01. 曲名 - 艺人 4:12\n"
    "02. 全角曲名 / 歌手\n"
    "Tr.3 第三首 4分12秒\n"
    "曲名\t表格艺人\t5:03"
)
CREDIT_TEXT = (
    "制作: Nika Lenz\n"
    "作曲: Nika Lenz / kaza\n"
    "插画: かにくみーとい (https://x.com/a)\n"
    "演唱: kaza\n"
    "instrument:Piano: 某丙\n"
    "编曲: 甲、乙\n"
    "职责: 名字\n"
    "staff\n"
    "没有冒号行"
)


def _extract_block() -> str:
    """取出 public.js 里那段粘贴暂存实现(从注释头到详情页曲目表那段之前)。"""
    text = JS.read_text(encoding="utf-8")
    marker = text.find("整段粘贴: 只在前端解析")
    assert marker > 0, "找不到粘贴暂存实现的注释"
    start = text.rfind("  /* =====", 0, marker)
    assert start > 0, "找不到粘贴暂存实现的注释头"
    detail = text.find("详情页曲目表", marker)
    assert detail > 0, "找不到粘贴暂存实现的结尾"
    end = text.rfind("  /* =====", 0, detail)
    assert end > start, "找不到粘贴暂存实现的结尾"
    return text[start:end]


def _run_node(body: str) -> str:
    if not shutil.which("node"):
        raise unittest.SkipTest("需要 node 才能跑前端逻辑的行为验证")
    script = (
        "var document = { addEventListener: function () {} };\n"
        "var window = { fetch: null };\n"
        + _extract_block()
        + "\n"
        + body
    )
    tmp = ROOT / ".tmp" / "paste_node"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        path = tmp / "probe.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
    except (PermissionError, OSError, FileNotFoundError) as exc:
        raise unittest.SkipTest(f"本环境无法启动 node 子进程: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise AssertionError("node 执行失败:\n" + (proc.stderr or proc.stdout))
    return proc.stdout.strip()


class TestPasteParserParity(unittest.TestCase):
    """前端解析器与 Python 解析器必须给出**完全相同**的结果."""

    def test_tracks_match_python(self) -> None:
        out = json.loads(_run_node(
            "console.log(JSON.stringify(parsePasteTracks(%s)));"
            % json.dumps(PASTE_TEXT)))
        expected = [
            {"title": t.title, "artists": t.artists, "duration": t.duration}
            for t in parse_tracklist(PASTE_TEXT)
        ]
        self.assertEqual(
            [{"title": t["title"], "artists": t["artists"], "duration": t["duration"]}
             for t in out],
            expected)

    def test_credits_match_python(self) -> None:
        out = json.loads(_run_node(
            "console.log(JSON.stringify(parsePasteCredits(%s)));"
            % json.dumps(CREDIT_TEXT)))
        expected = [[c.role, c.name] for c in parse_credits(CREDIT_TEXT)]
        self.assertEqual([[c["role"], c["name"]] for c in out], expected)

    def test_empty_input_yields_nothing(self) -> None:
        out = json.loads(_run_node(
            "console.log(JSON.stringify([parsePasteTracks(''), "
            "parsePasteCredits(''), parsePasteTracks('   \\n  \\n')]));"))
        self.assertEqual(out, [[], [], []])

    def test_header_and_noise_lines_are_skipped(self) -> None:
        text = "曲目表\n曲名 / 艺人 / 时长\nDisc 1\n---\n01. 只这一首 - 甲 3:00"
        out = json.loads(_run_node(
            "console.log(JSON.stringify(parsePasteTracks(%s)));" % json.dumps(text)))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "只这一首")
        self.assertEqual(out[0]["duration"], 180)

    def test_durations_and_formatting(self) -> None:
        out = json.loads(_run_node(
            "console.log(JSON.stringify({"
            "parse: ['4:12', '4分12秒', '252', '[3:42]'].map(parsePasteDuration),"
            "fmt: [formatPasteDuration(252), formatPasteDuration(3725), "
            "formatPasteDuration(42)]}));"))
        self.assertEqual(out["parse"], [252, 252, 252, 222])
        self.assertEqual(out["fmt"], ["4:12", "1:02:05", "0:42"])

    def test_credit_groups_keep_first_order(self) -> None:
        out = json.loads(_run_node(
            "console.log(JSON.stringify(groupPasteCredits(parsePasteCredits(%s))));"
            % json.dumps(CREDIT_TEXT)))
        self.assertEqual([g["role"] for g in out],
                         ["制作", "作曲", "插画", "演唱", "instrument:Piano", "编曲"])
        self.assertEqual(out[1]["names"], ["Nika Lenz", "kaza"])


class TestStagingWiring(unittest.TestCase):
    """按钮是 type=button, 且前端有解析/填表/提示的钩子."""

    def setUp(self) -> None:
        self.js = JS.read_text(encoding="utf-8")

    def test_both_buttons_are_plain_buttons(self) -> None:
        self.assertIn('data-track-apply="1"', self.js)
        self.assertIn('data-credit-apply="1"', self.js)
        self.assertIn("setupPasteFill", self.js)
        self.assertIn("setupPasteFill();", self.js)

    def test_parsers_and_fillers_exist(self) -> None:
        for name in ("parsePasteTracks", "parsePasteCredits", "fillPastedTracks",
                     "fillPastedCredits", "groupPasteCredits", "formatPasteDuration"):
            self.assertIn("function " + name + "(", self.js, name)

    def test_click_handlers_prevent_default(self) -> None:
        block = _extract_block()
        self.assertIn("e.preventDefault()", block)
        self.assertIn("绝对不提交表单", block)
        # 不能有 form.submit() / requestSubmit() 之类的提交动作
        self.assertNotIn("requestSubmit", block)
        self.assertNotIn(".submit()", block)

    def test_no_network_calls(self) -> None:
        """暂存纯前端: 不许 fetch / XHR."""
        block = _extract_block()
        self.assertNotIn("fetch(", block)
        self.assertNotIn("XMLHttpRequest", block)

    def test_hint_reports_not_yet_saved(self) -> None:
        block = _extract_block()
        self.assertIn("还没保存", block)
        self.assertIn("点最下面的「保存」才会写进库里", block)

    def test_append_and_replace_modes_supported(self) -> None:
        block = _extract_block()
        self.assertIn('track_mode', block)
        self.assertIn('credit_mode', block)
        self.assertIn('"append"', block)

    def test_filler_uses_the_page_own_builders(self) -> None:
        """填表必须走页面自己的建行逻辑, 不能另造一套结构."""
        block = _extract_block()
        self.assertIn("buildTrackRow(", block)
        self.assertIn("renumberDisc(", block)
        self.assertIn('[data-pair-add="credit"]', block)
        self.assertIn("credit-person-input", block)
        self.assertIn("KeyboardEvent", block)


if __name__ == "__main__":
    unittest.main()
