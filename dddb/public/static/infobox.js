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
    artists: 1, event: 1, release_date: 1, catalog_no: 1, price: 1,
    hall: 1, booth: 1, tags: 1, genres: 1, cover: 1
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
  function setField(name, value) {
    var el = document.querySelector('[name="' + name + '"]');
    if (!el || value == null) { return false; }
    el.value = String(value);
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
    return true;
  }

  function applyInfobox(text) {
    var d = parseInfobox(text);
    var s = d.single;
    var filled = [];

    ["title_original", "title", "title_romaji", "circle", "company",
     "event", "release_date", "catalog_no", "price", "hall", "booth"]
      .forEach(function (k) {
        if (s[k] && setField(k, s[k])) { filled.push(k); }
      });

    // 多值字段: 数组 -> "、" 分隔(与表单约定一致)
    ["artists", "tags", "genres"].forEach(function (k) {
      if (!s[k]) { return; }
      var parts = String(s[k]).split(/\s*\/\s*/).filter(Boolean);
      if (setField(k, parts.join("、"))) { filled.push(k); }
    });

    // 外链: 成对填 url_key / url_value
    var keys = $$('[name="url_key"]');
    var vals = $$('[name="url_value"]');
    d.links.forEach(function (pair, i) {
      if (i < keys.length && i < vals.length) {
        keys[i].value = pair[0];
        vals[i].value = pair[1];
        keys[i].dispatchEvent(new Event("input", {bubbles: true}));
        vals[i].dispatchEvent(new Event("input", {bubbles: true}));
      }
    });
    if (d.links.length) { filled.push("外链 " + d.links.length); }

    // 词条: 成对填 credit_role / credit_name
    var roles = $$('[name="credit_role"]');
    var names = $$('[name="credit_name"]');
    d.credits.forEach(function (c, i) {
      if (i < roles.length && i < names.length) {
        roles[i].value = c.role;
        names[i].value = c.name;
        roles[i].dispatchEvent(new Event("input", {bubbles: true}));
        names[i].dispatchEvent(new Event("input", {bubbles: true}));
      }
    });
    if (d.credits.length) { filled.push("词条 " + d.credits.length); }

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
