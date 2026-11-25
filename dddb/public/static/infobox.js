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

  /* 曲目表: 用户看到的是**下边的表格**, 不是「整段粘贴」那个折叠框。
     只往粘贴框里写, 表格还是空的 —— 用户会以为曲目没填进去(实测反馈)。
     所以跟词条/外链一样走「按需补行」: 缺行就点该碟的「＋ 添加歌曲」,
     多出来的旧行删掉, 再逐格填值。全部塞进第 1 张碟(infobox 没有碟信息)。
     返回填进去的行数; 页面上没有曲目表(老页面)时返回 0, 由调用方退回粘贴框。 */
  function fillTrackRows(tracks) {
    var blocks = $$(".disc-block");
    if (!blocks.length) { return 0; }
    var bodyOf = function (block) {
      return block.querySelector("tbody") || block.querySelector("[data-disc-body]");
    };
    var rowsOf = function (block) {
      var body = bodyOf(block);
      return body ? $$("tr.track-row", body) : [];
    };

    // infobox 的曲目行是**一维**的, 没有碟号字段。但导出时音轨号是**按碟重新
    // 从 1 开始**的(见 IB.build), 所以「音轨号不再递增」就是碟边界 —— 用这个
    // 把曲目切成若干组, 一组对应一张碟。只有一张碟的页面则全部归到一组并改成
    // 连续编号, 否则会出现 1..11,1..11 这种重复音轨号。
    var singleDisc = blocks.length <= 1;
    var groups = [];
    if (singleDisc) {
      groups.push(tracks.map(function (t, i) {
        return { track: t, index: i + 1 };
      }));
    } else {
      var cur = [];
      var lastIndex = 0;
      tracks.forEach(function (t) {
        var n = Number(t.index) || 0;
        if (cur.length && n && n <= lastIndex) {
          groups.push(cur);
          cur = [];
        }
        cur.push({ track: t, index: n || cur.length + 1 });
        lastIndex = n || lastIndex;
      });
      if (cur.length) { groups.push(cur); }
    }

    var filledCount = 0;
    groups.forEach(function (group, gi) {
      // 组比碟多时, 溢出的都并进最后一张碟, 不丢曲目
      var block = blocks[Math.min(gi, blocks.length - 1)];
      if (!block) { return; }
      var add = block.querySelector('[data-track-add="1"]');
      var rows = rowsOf(block);

      for (var guard = 0; guard < 200 && rows.length < group.length && add; guard++) {
        add.click();
        rows = rowsOf(block);
      }
      for (var i = rows.length - 1; i >= group.length; i--) {
        var del = rows[i].querySelector('[data-track-del="1"]');
        if (rows[i].parentNode) {
          if (del) { del.click(); } else { rows[i].remove(); }
        }
      }
      rows = rowsOf(block);

      var discNo = block.getAttribute("data-disc") || String(gi + 1);
      group.forEach(function (item, i) {
        var row = rows[i];
        if (!row) { return; }
        var t = item.track;
        var put = function (name, value) {
          var input = row.querySelector('[name="' + name + '"]');
          if (!input) { return; }
          input.value = value == null ? "" : String(value);
          fire(input, "input");
          fire(input, "change");
        };
        put("track_index", item.index);
        put("track_title", t.title || "");
        // 艺人列用「、」分隔(_track_row_html 与后端 split_list 都认这个)
        var artists = t.artists;
        if (Array.isArray(artists)) { artists = artists.join("、"); }
        put("track_artists", artists || "");
        put("track_duration", t.duration || "");
        // infobox 没有这几项的来源, 清空旧值 —— 否则"整份替换"会残留上一张碟的内容
        put("track_subtitle", "");
        put("track_title_zh", "");
        put("track_lang", "");
        put("track_genre", "");
        put("track_disc", discNo);
        filledCount += 1;
      });
    });
    return filledCount;
  }

  function ibMsg() { return $("[data-infobox-msg]"); }

  // infobox 里的 cover 地址 -> **先缓存到服务端, 提交时再转正**(用户 m13899).
  //
  // 为什么不再"填表就写库": 编辑者可能只是粘一段 infobox 试试, 或者发现这张
  // 图不够清楚要换 —— 直接写库就没有退路了。现在分两步:
  //   ① 这里 POST /cover-cache/new, 服务端抓图存进 covers/pending/<token>,
  //      页面立刻用 /cover-cache/<token> 预览, token 写进隐藏域 cover_cache;
  //   ② 提交表单时服务端 _apply_pending_cover() 把它转成正式封面;
  //      若用户自己传了图, 缓存直接作废(以用户提交的为准)。
  // 返回 true 表示这一项确实被处理了(用于"已填入"提示)。
  function applyCover(url) {
    var box = ibMsg();
    var hint = document.querySelector('[name="cover_url_hint"]');
    // local: 开头是"库里已经有这张本地封面文件"的记号(见 dddb/infobox.py build)。
    // 它不是能下载的地址, POST 出去必然被 is_public_image_url() 拒掉,
    // 以前就是它把"封面没自动缓存"变成常态的 —— 直接跳过, 保留现有封面。
    var raw = String(url || "").trim();
    if (!raw || /^local:/i.test(raw) || !/^https?:\/\//i.test(raw)) {
      if (hint) { hint.value = ""; }
      if (box) {
        box.textContent = raw
          ? "封面已是本地文件，不需要重新抓取"
          : "没有封面地址";
      }
      return false;
    }
    if (hint) { hint.value = raw; }
    var cacheField = document.getElementById("cover-cache");
    var form = document.querySelector("form.edit-form");
    var token = form && form.querySelector('input[name="csrf_token"]');
    token = token && token.value;
    if (!cacheField || !token) {
      // 极端情况(没有编辑表单/没有 CSRF): 退回旧的兜底路径, 保存时由服务端抓
      if (box) { box.textContent = "封面会在保存时自动抓取"; }
      return true;
    }
    var body = new URLSearchParams();
    body.set("csrf_token", token);
    body.set("url", raw);
    if (box) { box.textContent = "正在缓存封面…"; }
    fetch("/cover-cache/new", {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" }
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: "HTTP " + r.status };
      });
    }).then(function (data) {
      if (data && data.ok && data.token) {
        cacheField.value = data.token;
        // 让草稿与"脏"标记都能看到这个变化
        cacheField.dispatchEvent(new Event("input", { bubbles: true }));
        cacheField.dispatchEvent(new Event("change", { bubbles: true }));
        var img = document.querySelector(".edit-cover-img img, .edit-cover-thumb img");
        if (img) {
          img.setAttribute("src", data.url);
          img.hidden = false;
        }
        var plus = document.querySelector(".cover-add-thumb .cover-plus");
        if (plus) { plus.style.display = "none"; }
        var fallback = document.querySelector(".cover-fallback span");
        if (fallback) { fallback.style.display = "none"; }
        if (box) {
          var size = (data.width && data.height)
            ? "（" + data.width + "×" + data.height + "）" : "";
          box.textContent = data.small
            ? "封面已缓存" + size + "，但原图偏小；不满意可以直接换一张，"
              + "换成你上传的那张后缓存的这张会被丢掉"
            : "封面已缓存" + size + "，点保存才会真正存下来";
        }
      } else if (box) {
        box.textContent = "封面抓取失败: " + ((data && data.error) || "未知原因");
      }
    }).catch(function (err) {
      if (box) {
        box.textContent = "封面抓取失败: " + (err && err.message ? err.message : err);
      }
    });
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
    //   tags / circle / event -> bind 控件(隐藏域 + chips)
    //   genres -> 普通文本框(「、」分隔)
    // 直接写隐藏域而不重建按钮/chips, 之后任何一次交互都会把它覆盖回去。
    // artists 不在这里处理 —— 见下面的「艺人落进词条行」。
    ["tags", "genres"].forEach(function (k) {
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

    /* 艺人(``artists``)落进**词条行的「人员」框**(用户要求 m11892)。
       以前只写 hidden[name=artists], 于是上方只读预览显示有人、下方词条行
       全空 —— 用户看不出这些人从哪来, 也没法给它们填职责。
       现在: 词条里没出现过的人, 补一行职责留空(后端按 role="artist" 存,
       显示成「艺人」), 名字进人员框; 已经出现在词条里的人不重复加。
       整行职责留空是有意的 —— 一旦写了「艺人」两个字, 它会作为未知职责
       排到最后, 而留空走的是后端的 ``role or "artist"`` 兜底。 */
    if (s.artists) {
      var artistNames = String(s.artists).split(/\s*[\/、]\s*/)
        .filter(Boolean).map(function (one) { return one.trim(); });
      var covered = {};
      $$('[data-pair-row="credit"] .credit-person-chip').forEach(
        function (chip) { covered[chip.getAttribute("data-name") || ""] = 1; });
      /* 汇总区的基序换成 infobox 的 artists —— 否则贴着旧冠会残留
         上一份数据里、这次 infobox 里没有的人。 */
      var summaryHost = document.querySelector('[data-people="artists"]');
      if (summaryHost) {
        summaryHost.setAttribute("data-artists", artistNames.join("\n"));
      }
      var missing = artistNames.filter(function (one) {
        return one && !covered[one];
      });
      if (missing.length) {
        // 先找「职责空 + 没有人员」的行来放, 没有才新加一行
        var target = null;
        $$('[data-pair-row="credit"]').forEach(function (row) {
          if (target) { return; }
          var roleInput = row.querySelector('[name="credit_role"]');
          var role = roleInput ? String(roleInput.value || "").trim() : "";
          if (!role && !row.querySelectorAll(".credit-person-chip").length) {
            target = row;
          }
        });
        if (!target) {
          ensureRows("credit", $$('[data-pair-row="credit"]').length + 1);
          target = $$('[data-pair-row="credit"]').pop();
        }
        if (target) {
          addCreditPeople(target, missing);
          filled.push("艺人 " + missing.length);
        }
      }
      /* 让上方只读汇总按新基序重排: rebuildSummary 挂在职责框的 input 上,
         它同时会用「基序 + 词条人员」重写 artists 隐藏域。 */
      var anyRole = document.querySelector(
        '[data-pair-row="credit"] [name="credit_role"]');
      if (anyRole) { fire(anyRole, "input"); }
    }

    // 曲目: 优先**直接填进下方曲目表的行**(用户看到的就是这张表);
    // 老页面没有曲目表时退回「整段粘贴」文本框。
    if (d.tracks.length) {
      var rowsFilled = fillTrackRows(d.tracks);
      if (rowsFilled) {
        filled.push("曲目 " + rowsFilled);
      } else {
        var paste = document.querySelector('[name="track_paste"]');
        if (paste) {
          paste.value = d.tracks.map(function (t) {
            var line = t.index + ". " + t.title;
            if (t.duration) { line += " [" + t.duration + "]"; }
            if (t.artists) { line += " / " + t.artists; }
            return line;
          }).join("\n");
          fire(paste, "input");
          filled.push("曲目 " + d.tracks.length);
        }
      }
    }

    if (d.description && setField("description", d.description)) {
      filled.push("简介");
    }
    // 封面: 编辑页/新建页都没有「封面 URL」输入(一律走上传),
    // 但服务端能按地址抓 —— 见 applyCover。
    if (s.cover) {
      if (applyCover(s.cover)) { filled.push("封面"); }
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
