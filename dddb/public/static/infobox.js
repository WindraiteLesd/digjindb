/* ==========================================================================
   dddb infobox: 粘贴 -> 一键填入表单
   ==========================================================================
   用户要求:
     "线上新建专辑页面放置一个编辑infobox按钮，会直接弹出一个输入框，
      复制现在的 dddb infobox 进去就可以一键填入所有词条，
      并在输入框下方额外放置帮助按钮弹出二级窗口的格式说明"

   解析在前端做 —— 粘贴后立刻看到效果, 不用请求服务端。
   ========================================================================== */
(function () {
  "use strict";

  var MARK = "[dddb]";
  var SINGLE = {
    key: 1, title_original: 1, title: 1, title_romaji: 1, circle: 1,
    company: 1, artists: 1, event: 1, release_date: 1, catalog_no: 1,
    price: 1, hall: 1, booth: 1, tags: 1, genres: 1, cover: 1
  };

  function $(s, root) { return (root || document).querySelector(s); }
  function $$(s, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(s));
  }

  /* ---- 解析 infobox ---- */
  function parseInfobox(text) {
    var out = {
      single: {}, links: [], credits: [], tracks: [], description: ""
    };
    var current = "";
    var lines = String(text || "").split(/\r?\n/);
    var descParts = [];

    lines.forEach(function (line) {
      if (!line.trim() || line.trim() === MARK) { return; }
      var m = line.match(/^\s*([a-zA-Z_]+)\s*\|\|\s?(.*)$/);
      var value;
      if (m) {
        current = m[1].toLowerCase();
        value = m[2];
      } else {
        var c = line.match(/^\s*\|\|\s?(.*)$/);
        if (!c || !current) { return; }
        value = c[1];
      }

      if (SINGLE[current]) {
        out.single[current] = value.trim();
      } else if (current === "links") {
        var lm = value.match(/^\s*([^:：|]+?)\s*(?:[:：]|\|\|)\s*(\S+)\s*$/);
        if (lm) { out.links.push([lm[1].trim(), lm[2].trim()]); }
      } else if (current === "credits") {
        var parts = value.split(/[:：]/);
        if (parts.length < 2) { return; }
        var role = parts.slice(0, -1).join(":").trim();
        var rest = parts[parts.length - 1].trim();
        var alias = "", orig = "";
        var am = rest.match(/\s*\[([^\]]+)\]\s*$/);
        if (am) { alias = am[1].trim(); rest = rest.slice(0, am.index).trim(); }
        var om = rest.match(/\s*[（(]([^（()）]+)[)）]\s*$/);
        if (om) { orig = om[1].trim(); rest = rest.slice(0, om.index).trim(); }
        if (rest) {
          out.credits.push({role: role, name: rest,
                            original: orig, alias: alias});
        }
      } else if (current === "tracks") {
        var tm = value.match(
          /^\s*(\d+)\s*[.、)]\s*(.+?)\s*(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\])?\s*(?:\/\s*(.+))?$/);
        if (tm) {
          out.tracks.push({
            index: parseInt(tm[1], 10) || out.tracks.length + 1,
            title: (tm[2] || "").trim(),
            duration: tm[3] || "",
            artists: tm[4] ? tm[4].trim() : ""
          });
        }
      } else if (current === "description") {
        descParts.push(value.replace(/\s+$/, ""));
      }
    });

    out.description = descParts.join("\n");
    return out;
  }

  /* ---- 填进表单 ---- */
  function fire(el, type) {
    el.dispatchEvent(new Event(type, {bubbles: true}));
  }

  /* 多值字段(circle / event / tags)在页面上是 bind 控件:
     可见输入框只有 data-name, 真正提交的是同名隐藏域, chip 由
     public.js 的 setupBindFields 维护。**只改隐藏域不够** —— 那边
     内部有 items 数组, 之后任何一次增删都会把它覆盖回旧值。
     所以这里改隐藏域之后派发 change, 让它自己重建 chips。 */
  function setBindField(name, parts) {
    var hidden = document.querySelector('input[type="hidden"][name="' + name + '"]');
    if (!hidden) { return false; }
    hidden.value = parts.join("\n");
    fire(hidden, "input");
    fire(hidden, "change");
    return true;
  }

  /* 词条 / 外链是**成对行**: 页面上只有用户手动加过几行就有几行,
     infobox 里的条数常常比它多。缺行就点一下对应的「＋ 添加」按钮,
     走的是页面自己的建行逻辑(结构才不会和已有的行不一致)。 */
  function ensureRows(kind, count) {
    var selector = '[data-pair-add="' + kind + '"]';
    var rowSel = '[data-pair-row="' + kind + '"]';
    var add = document.querySelector(selector);
    if (add) {
      for (var guard = 0; guard < 60; guard++) {
        if (document.querySelectorAll(rowSel).length >= count) { break; }
        add.click();
      }
    }
    // 多出来的旧行要删掉: 「填入」的语义是**用 infobox 整份替换**,
    // 留着旧行会变成"原有 + 新增"的混合数据, 和粘贴的内容对不上。
    var rows = $$(rowSel);
    for (var i = rows.length - 1; i >= count; i--) { rows[i].remove(); }
  }

  /* 词条的人员是 chip + 隐藏域, 隐藏域由 public.js 从 chip 重建 ——
     直接写隐藏域会被下一次同步覆盖。这里往行内的「人员」输入框里
     写值并派发回车, 走的就是用户手动添加时的那条路径。 */
  function addCreditPeople(row, names) {
    var input = row.querySelector(".credit-person-input");
    if (!input) { return false; }
    names.forEach(function (one) {
      var value = String(one || "").trim();
      if (!value) { return; }
      input.value = value;
      input.dispatchEvent(new KeyboardEvent("keydown",
        {key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true}));
    });
    input.value = "";
    return true;
  }

  function setField(name, value) {
    if (value == null) { return false; }
    var el = document.querySelector('[name="' + name + '"]');
    if (!el) { return false; }
    el.value = String(value);
    fire(el, "input");
    fire(el, "change");
    return true;
  }

  function applyInfobox(text) {
    var d = parseInfobox(text);
    var s = d.single;
    var filled = [];

    ["title_original", "title", "title_romaji", "circle", "company",
     "event", "release_date", "catalog_no", "price", "hall", "booth"]
      .forEach(function (k) {
        if (!s[k]) { return; }
        // circle / event 是 bind 控件(可能多值), 其余是普通文本框
        var ok = (k === "circle" || k === "event")
          ? setBindField(k, String(s[k]).split(/\s*[\/、]\s*/).filter(Boolean))
          : setField(k, s[k]);
        if (ok) { filled.push(k); }
      });

    // 多值字段. 注意三种字段在页面上的控件不同:
    //   artists -> 一排按钮(.person-chip) + 隐藏域(换行分隔)
    //   tags / circle / event -> bind 控件(隐藏域 + chips)
    //   genres -> 普通文本框(「、」分隔)
    // 直接写隐藏域而不重建按钮/chips, 之后任何一次交互都会把它覆盖回去。
    ["artists", "tags", "genres"].forEach(function (k) {
      if (!s[k]) { return; }
      var parts = String(s[k]).split(/\s*[\/、]\s*/).filter(Boolean);
      if (k === "genres") {
        if (setField(k, parts.join("、"))) { filled.push(k); }
      } else if (setBindField(k, parts)) {
        filled.push(k);
      }
    });

    // 外链: 行数不够就先补行(否则多出来的链接全丢)
    if (d.links.length) {
      ensureRows("url", d.links.length);
      var keys = $$('[name="url_key"]');
      var vals = $$('[name="url_value"]');
      d.links.forEach(function (pair, i) {
        if (i < keys.length && i < vals.length) {
          keys[i].value = pair[0];
          vals[i].value = pair[1];
          fire(keys[i], "input");
          fire(vals[i], "input");
        }
      });
      filled.push("外链 " + d.links.length);
    }

    // 词条: 同样先补行; 人员必须走输入框(隐藏域由 chip 重建)
    if (d.credits.length) {
      ensureRows("credit", d.credits.length);
      var rows = $$('[data-pair-row="credit"]');
      d.credits.forEach(function (c, i) {
        var row = rows[i];
        if (!row) { return; }
        var roleInput = row.querySelector('[name="credit_role"]');
        if (roleInput) {
          roleInput.value = c.role;
          fire(roleInput, "input");
        }
        // 表单里已经没有「原名 / 别名」两列了, 只填职位与人员
        addCreditPeople(row, String(c.name || "").split(/[、,，\/]/));
      });
      filled.push("词条 " + d.credits.length);
    }

    // 曲目: 用「整段粘贴」模式最省事
    var paste = document.querySelector('[name="track_paste"]');
    if (paste && d.tracks.length) {
      paste.value = d.tracks.map(function (t) {
        var line = t.index + ". " + t.title;
        if (t.duration) { line += " [" + t.duration + "]"; }
        if (t.artists) { line += " / " + t.artists; }
        return line;
      }).join("\n");
      paste.dispatchEvent(new Event("input", {bubbles: true}));
      filled.push("曲目 " + d.tracks.length);
    }

    if (d.description && setField("description", d.description)) {
      filled.push("简介");
    }
    // 封面: 编辑页/新建页都不再有「封面 URL」输入(一律走上传),
    // 所以这一项填不进去 —— 明确说出来, 别让用户以为已经填了。
    if (s.cover) { filled.push("封面需手动上传"); }
    return filled;
  }

  /* ---- 弹窗交互 ---- */
  function bind() {
    var modal = $("[data-infobox-modal]");
    var helpModal = $("[data-infobox-help-modal]");
    if (!modal) { return; }
    var text = $("[data-infobox-text]", modal);
    var msg = $("[data-infobox-msg]", modal);

    function open() { modal.hidden = false; if (text) { text.focus(); } }
    function close() { modal.hidden = true; if (msg) { msg.textContent = ""; } }

    $$("[data-infobox-open]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.preventDefault();
        open();
      });
    });
    $$("[data-infobox-close]", modal).forEach(function (b) {
      b.addEventListener("click", close);
    });
    modal.addEventListener("click", function (e) {
      if (e.target === modal) { close(); }
    });

    var apply = $("[data-infobox-apply]", modal);
    if (apply) {
      apply.addEventListener("click", function () {
        var filled = applyInfobox(text ? text.value : "");
        if (!filled.length) {
          if (msg) { msg.textContent = "没解析到字段，检查格式"; }
          return;
        }
        if (msg) {
          msg.textContent = "已填入：" + filled.join(" · ");
        }
        window.setTimeout(close, 900);
      });
    }

    var helpBtn = $("[data-infobox-help]", modal);
    if (helpBtn && helpModal) {
      helpBtn.addEventListener("click", function () {
        helpModal.hidden = false;
      });
      $$("[data-infobox-help-close]", helpModal).forEach(function (b) {
        b.addEventListener("click", function () { helpModal.hidden = true; });
      });
      helpModal.addEventListener("click", function (e) {
        if (e.target === helpModal) { helpModal.hidden = true; }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  // 给站长端自动填表用: 暴露一个全局钩子
  window.dddbApplyInfobox = applyInfobox;
  window.dddbParseInfobox = parseInfobox;
})();
