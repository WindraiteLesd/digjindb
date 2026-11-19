"""降级方案: 生成一个"一键导入"书签 + 本地桥页.

背景:
    原设计用 CDP 驱动浏览器自动填表, 但在**受限环境**下(沙箱禁止
    启动浏览器进程 / crashpad 被拒) 无法工作。

所以提供两条**不依赖进程启动**的路:

    ① **书签小工具(bookmarklet)** —— 把填表 JS 存成书签, 在线上
       新建页点一下就用缓存填好。跨域限制天然绕开(同在目标页执行)。

    ② **本地桥页** —— 站长端生成一个 ``import.html``, 里面带全部
       字段值; 你在浏览器打开它, 点「填入线上」按钮, 它把值写到
       剪贴板, 或者打开线上页 + 用 postMessage/表单预填。

方案 ① 最稳: 生成一大段 JS, 复制到浏览器书签即可。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 书签里跑的填表脚本(与 CDP 版同一套逻辑, 只是从 localStorage 取值)
BOOKMARKLET_TEMPLATE = r"""javascript:(function(){
  var KEY = "dddb_import_{stem}";
  var raw = localStorage.getItem(KEY);
  if (!raw) { alert("没有缓存数据。请先在站长端生成并打开过桥页。"); return; }
  var p; try { p = JSON.parse(raw); } catch(e) { alert("缓存解析失败"); return; }
  var filled = [], missing = [];
  function setV(el, v) {
    if (!el || v === null || v === undefined || v === "") return false;
    el.value = String(v);
    el.dispatchEvent(new Event("input", {bubbles:true}));
    el.dispatchEvent(new Event("change", {bubbles:true}));
    return true;
  }
  function one(n) {
    var v = p[n]; if (!v) return;
    if (setV(document.querySelector('[name="'+n+'"]'), v)) filled.push(n);
    else missing.push(n);
  }
  ["title_original","title","title_romaji","circle","company","event",
   "release_date","catalog_no","price","hall","booth","tags","genres",
   "description","artists"].forEach(one);
  if (p.credits) {
    var rs = document.querySelectorAll('[name="credit_role"]');
    var ns = document.querySelectorAll('[name="credit_name"]');
    var n = Math.min(p.credits.length, rs.length, ns.length);
    for (var i=0;i<n;i++){ setV(rs[i],p.credits[i].role);
                           setV(ns[i],p.credits[i].name); }
    if (n) filled.push("credits("+n+")");
  }
  if (p.urls) {
    var ks = document.querySelectorAll('[name="url_key"]');
    var vs = document.querySelectorAll('[name="url_value"]');
    var m = Math.min(p.urls.length, ks.length, vs.length);
    for (var j=0;j<m;j++){ setV(ks[j],p.urls[j].key);
                           setV(vs[j],p.urls[j].value); }
    if (m) filled.push("urls("+m+")");
  }
  if (p.tracks) {
    var secs = {disc:"track_disc",index:"track_index",title:"track_title",
      subtitle:"track_subtitle",title_zh:"track_title_zh",
      artists:"track_artists",lang:"track_lang",genre:"track_genre",
      duration:"track_duration"};
    Object.keys(secs).forEach(function(f){
      var els = document.querySelectorAll('[name="'+secs[f]+'"]');
      var c = Math.min(p.tracks.length, els.length);
      for (var k=0;k<c;k++){ setV(els[k], p.tracks[k][f]); }
    });
    var tn = document.querySelectorAll('[name="track_title"]').length;
    if (tn) filled.push("tracks("+Math.min(p.tracks.length,tn)+")");
  }
  alert("已填入: " + (filled.join(", ") || "无") +
        (missing.length ? "\n页面没有这些字段: " + missing.join(", ") : "") +
        "\n\n请核对后**手动点保存**。封面需手动选择。");
})();"""

#: 桥页 HTML —— 打开后先把缓存写进 localStorage, 再引导去线上页
BRIDGE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>导入到线上 · {title}</title>
<style>
 body{{font:14px/1.7 system-ui,sans-serif;max-width:760px;margin:40px auto;
      padding:0 20px;background:#1c1a18;color:#e8e0d8}}
 h1{{font-size:20px}} h2{{font-size:15px;margin-top:28px;color:#c9a86a}}
 a.btn,button{{display:inline-block;padding:9px 18px;border-radius:8px;
   background:#c9a86a;color:#1c1a18;text-decoration:none;border:0;
   font-weight:600;cursor:pointer;margin:6px 8px 6px 0}}
 textarea{{width:100%;height:90px;background:#262220;color:#ddd;
   border:1px solid #3a3430;border-radius:6px;padding:10px;font:12px/1.5 monospace}}
 .ok{{color:#7fbf7f}} .warn{{color:#e0a060}}
 table{{width:100%;border-collapse:collapse;margin:10px 0}}
 td{{padding:4px 8px;border-bottom:1px solid #2e2a26;vertical-align:top}}
 td:first-child{{width:150px;color:#a99}}
 code{{background:#262220;padding:1px 5px;border-radius:3px}}
</style></head><body>
<h1>导入到线上 · {title}</h1>
<p>三步完成。全程在<strong>你自己的浏览器</strong>里操作, 不改线上任何代码。</p>

<h2>① 把数据存进浏览器缓存</h2>
<p>点下面这个按钮(它会把这页的字段写进 localStorage, 之后书签才读得到)。</p>
<button onclick="saveCache()">写入缓存</button>
<span id="saveState"></span>

<h2>② 装上填表书签</h2>
<p>把下面这段拖到书签栏(或新建一个书签, 把这段粘进 URL):</p>
<textarea readonly onclick="this.select()">{bookmarklet}</textarea>

<h2>③ 打开线上新建页, 点书签</h2>
<p><a class="btn" href="{new_url}" target="_blank">打开线上新建专辑页</a></p>
<p class="warn">表单填好后请<strong>自己核对</strong>, 再点页面上的保存。
封面请在新建页手动选择: <code>{cover_path}</code></p>

<h2>字段预览</h2>
<table>{rows}</table>

<script>
var PAYLOAD = {payload_json};
var KEY = "{storage_key}";
function saveCache() {{
  try {{
    localStorage.setItem(KEY, JSON.stringify(PAYLOAD));
    document.getElementById("saveState").innerHTML =
      '<span class="ok">✓ 已写入缓存(' + KEY + ')</span>';
  }} catch (e) {{
    document.getElementById("saveState").innerHTML =
      '<span class="warn">写入失败: ' + e.message + '</span>';
  }}
}}
window.addEventListener("load", saveCache);
</script>
</body></html>"""


def _storage_key(stem: str) -> str:
    return "dddb_import_{}".format(stem)


def build_bookmarklet(data: dict[str, Any], stem: str) -> str:
    return BOOKMARKLET_TEMPLATE.replace("{stem}", stem)


def build_bridge_html(data: dict[str, Any], *, base_url: str,
                      stem: str) -> str:
    """生成桥页 HTML(自带缓存 + 书签 + 字段预览)."""
    title = (data.get("title_original") or data.get("title")
             or data.get("album_key") or "专辑")
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    rows = []
    simple = [("标题", "title_original"), ("中文/别名", "title"),
              ("罗马音", "title_romaji"), ("社团", "circle"),
              ("公司", "company"), ("展会", "event"),
              ("发售日", "release_date"), ("编号", "catalog_no"),
              ("价格", "price"), ("展馆", "hall"), ("展位", "booth"),
              ("标签", "tags"), ("曲风", "genres"),
              ("艺人", "artists"), ("简介", "description")]
    for label, key in simple:
        v = str(payload.get(key) or "")
        if v:
            rows.append("<tr><td>{}</td><td>{}</td></tr>".format(
                label, _esc(v)))
    if payload.get("credits"):
        rows.append("<tr><td>词条</td><td>{}</td></tr>".format(
            _esc(", ".join("{}: {}".format(c.get("role", ""),
                                           c.get("name", ""))
                           for c in payload["credits"]))))
    if payload.get("urls"):
        rows.append("<tr><td>外链</td><td>{}</td></tr>".format(
            "<br>".join(_esc("{}: {}".format(u.get("key", ""),
                                             u.get("value", "")))
                        for u in payload["urls"])))
    if payload.get("tracks"):
        items = []
        for t in payload["tracks"]:
            items.append(_esc("{}. {} {}".format(
                t.get("index", ""), t.get("title", ""),
                "({})".format(t["duration"]) if t.get("duration") else "")))
        rows.append("<tr><td>曲目 {} 首</td><td>{}</td></tr>".format(
            len(payload["tracks"]), "<br>".join(items)))
    if data.get("cover_file"):
        rows.append("<tr><td>封面文件</td><td><code>{}</code></td></tr>"
                    .format(_esc(str(data["cover_file"]))))

    return BRIDGE_HTML.format(
        title=_esc(title),
        bookmarklet=_esc(build_bookmarklet(payload, stem)),
        new_url="{}/new/album".format(base_url.rstrip("/")),
        cover_path=_esc(str(data.get("cover_file") or "(无)")),
        rows="".join(rows) or "<tr><td colspan=2>(没有字段)</td></tr>",
        payload_json=json.dumps(payload, ensure_ascii=False),
        storage_key=_storage_key(stem),
    )


def _esc(text: Any) -> str:
    s = str(text if text is not None else "")
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def write_bridge(data: dict[str, Any], *, base_url: str,
                 out_dir: Path, stem: str) -> Path:
    """把桥页写到 out_dir, 返回路径."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "import-{}.html".format(stem)
    path.write_text(
        build_bridge_html(data, base_url=base_url, stem=stem),
        encoding="utf-8")
    return path


def write_bookmarklet(data: dict[str, Any], out_dir: Path,
                      stem: str) -> Path:
    """把书签脚本单独存一份(方便复制)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "bookmarklet-{}.txt".format(stem)
    path.write_text(build_bookmarklet(data, stem), encoding="utf-8")
    return path


__all__ = [
    "build_bookmarklet",
    "build_bridge_html",
    "write_bookmarklet",
    "write_bridge",
]
