/* DoujinDigDatabase 公开端的**唯一**一份脚本(同源加载) —— 只做渐进增强。
 *
 * 三条铁律:
 *   1. 页面里**没有**内联 <script>、没有 onclick=, 只有本文件的 <script src> 引用;
 *      CSP 是 script-src 'self', 连 unsafe-inline 都没给。
 *   2. 不接管导航、不做 SPA; 只给已有表单加即时提示。
 *   3. **没有 JS 时全部功能照常可用** —— 所有校验服务端都会再查一遍,
 *      这里只是让体验好一点(少一次来回)。
 */
(function () {
  "use strict";

  /* 1. 注册页: 两次密码是否一致(只提示, 不拦提交 —— 服务端仍会校验) */
  function setupPasswordMatch() {
    var first = document.getElementById("password");
    var second = document.getElementById("password2");
    var hint = document.getElementById("password2-error");
    if (!first || !second || !hint) {
      return;
    }
    function sync() {
      var mismatch = first.value !== "" && second.value !== "" && first.value !== second.value;
      // **必须写文字**: 只切 hidden 会弹出一个空的红字块
      hint.textContent = mismatch ? "两次输入的密码不一致" : "";
      hint.hidden = !mismatch;
      // 双保险: 万一有样式把 [hidden] 覆盖掉(例如 display:block), 再显式设一次
      if (hint.style) { hint.style.display = mismatch ? "block" : "none"; }
    }
    // 多绑几个事件: 中文输入法 / 粘贴 / 自动填充 / 失焦都不漏
    ["input", "keyup", "change", "blur"].forEach(function (type) {
      first.addEventListener(type, sync);
      second.addEventListener(type, sync);
    });
    var form = first.form || (first.closest ? first.closest("form") : null);
    if (form) { form.addEventListener("submit", sync); }
    sync();
  }

  /* 1b. 用户名实时查重 + 违规词(防抖 400ms; 只提示, 服务端仍是最终判据) */
  function setupUsernameCheck() {
    var input = document.getElementById("username");
    var hint = document.getElementById("username-hint");
    var form = input && (input.form || (input.closest ? input.closest("form") : null));
    if (!input || !hint || !form || typeof window.fetch !== "function") {
      return;
    }
    // 只有注册表单才查(check 接口只用于注册)
    if (form.getAttribute("action") !== "/register") {
      return;
    }
    var timer = null;
    var seq = 0;
    function render(text, cls) {
      hint.textContent = text;
      hint.className = "field-hint" + (cls ? " " + cls : "");
    }
    function ask() {
      var value = input.value.trim();
      if (value === "") {
        render("", "");
        return;
      }
      var mine = ++seq;
      window.fetch("/username-check?u=" + encodeURIComponent(value), {
        headers: { "Accept": "application/json" },
      }).then(function (resp) {
        return resp.json();
      }).then(function (data) {
        if (mine !== seq) { return; }          // 过期响应丢弃
        render(data.available ? "可以使用" : (data.reason || "不可用"),
               data.available ? "is-ok" : "is-err");
      }).catch(function () {
        render("", "");                        // 查不了就不提示, 不打扰用户
      });
    }
    function schedule() {
      window.clearTimeout(timer);
      timer = window.setTimeout(ask, 400);
    }
    input.addEventListener("input", schedule);
    input.addEventListener("blur", ask);
  }

  /* 1c. 密码规则逐条点亮(与 users.password_problem 保持同一套规则) */
  function setupPasswordRules() {
    var input = document.getElementById("password");
    var list = document.getElementById("password-rules");
    if (!input || !list) {
      return;
    }
    function check() {
      var value = input.value;
      var state = {
        len: value.length >= 8,
        digit: /[0-9]/.test(value),
        letter: /[A-Za-z]/.test(value),
      };
      Array.prototype.forEach.call(list.querySelectorAll("li[data-rule]"), function (item) {
        var ok = state[item.getAttribute("data-rule")] === true;
        item.className = ok ? "is-ok" : "";
      });
    }
    input.addEventListener("input", check);
    input.addEventListener("keyup", check);
    check();
  }

  /* ==========================================================================
     制作人员: 全部 chip 按钮化
     * 艺人不再是"输入框 + 候选", 而是一排按钮
     * 每个按钮带 data-entity-id(ArtistId): 重名靠 id 区分, 马甲各自一个 id
     * 下方搜索框输入后从库内艺人候选里点选 -> 变按钮
     * 隐藏字段 name="artists" 保存**名称**(换行分隔), 与后端解析一致
     ========================================================================== */
  function setupPeopleChips() {
    var host = document.querySelector('[data-people="artists"]');
    if (!host) { return; }
    var card = host.closest(".edit-card") || document;
    var hidden = card.querySelector('input[type="hidden"][name="artists"]');
    var search = card.querySelector(".people-search");
    var list = card.querySelector(".people-suggest-list");

    function names() {
      var out = [];
      var chips = host.querySelectorAll(".person-chip");
      Array.prototype.forEach.call(chips, function (chip) {
        var name = chip.getAttribute("data-person");
        if (name) { out.push(name); }
      });
      return out;
    }

    function sync() {
      if (hidden) { hidden.value = names().join("\n"); }
      // 已加进按钮的候选不再显示
      if (!list) { return; }
      var current = names().map(function (one) { return one.toLowerCase(); });
      Array.prototype.forEach.call(
        list.querySelectorAll(".person-suggest"), function (item) {
          var name = (item.getAttribute("data-name") || "").toLowerCase();
          item.hidden = current.indexOf(name) !== -1;
        });
    }

    function makeChip(name, entityId) {
      var chip = document.createElement("span");
      chip.className = "person-chip";
      chip.setAttribute("data-person", name);
      chip.setAttribute("data-entity-id", String(entityId || 0));
      var kind = document.createElement("span");
      kind.className = "person-kind";
      kind.textContent = "艺人";
      var bold = document.createElement("b");
      bold.textContent = name;
      var del = document.createElement("button");
      del.type = "button";
      del.className = "person-x";
      del.setAttribute("data-person-del", "1");
      del.setAttribute("aria-label", "移除 " + name);
      del.textContent = "×";
      chip.appendChild(kind);
      chip.appendChild(bold);
      chip.appendChild(del);
      return chip;
    }

    function add(name, entityId) {
      var value = String(name || "").trim();
      if (!value) { return; }
      if (names().indexOf(value) !== -1) { return; }   // 同名不重复加
      host.appendChild(makeChip(value, entityId));
      sync();
      if (search) { search.value = ""; }
      filter("");
      // 加入后收起候选(用户点完就走)
      if (list) { list.style.display = "none"; }
    }

    function filter(text) {
      if (!list) { return; }
      var needle = String(text || "").trim().toLowerCase().replace(/^#/, "");
      var current = names().map(function (one) { return one.toLowerCase(); });
      var shown = 0;
      Array.prototype.forEach.call(
        list.querySelectorAll(".person-suggest"), function (item) {
          var name = (item.getAttribute("data-name") || "").toLowerCase();
          var eid = item.getAttribute("data-entity-id") || "";
          if (current.indexOf(name) !== -1) { item.hidden = true; return; }
          var hit = !needle || name.indexOf(needle) !== -1 || eid === needle;
          item.hidden = !hit;
          if (hit) { shown += 1; }
        });
      list.style.display = shown ? "block" : "none";
      list.setAttribute("data-count", String(shown));
    }

    // 点候选 -> 加入(用 mousedown: 比 click 更早, 不会被失焦/重排打断)
    if (list) {
      list.addEventListener("mousedown", function (e) {
        var btn = e.target && e.target.closest
          ? e.target.closest(".person-suggest") : null;
        if (!btn) { return; }
        e.preventDefault();          // 阻止输入框失焦
        e.stopPropagation();
        add(btn.getAttribute("data-name"),
            parseInt(btn.getAttribute("data-entity-id") || "0", 10) || 0);
      });
    }

    // 点 × -> 移除
    host.addEventListener("click", function (e) {
      var del = e.target && e.target.closest
        ? e.target.closest("[data-person-del]") : null;
      if (!del) { return; }
      e.preventDefault();
      var chip = del.closest(".person-chip");
      if (chip && chip.parentNode) { chip.parentNode.removeChild(chip); sync(); }
    });

    // 搜索框
    if (search) {
      search.addEventListener("input", function () { filter(search.value); });
      search.addEventListener("focus", function () { filter(search.value); });
      // **不要用 blur 关闭**: 点候选按钮时输入框先失焦, 候选会在点击前消失,
      // 表现为"点选框不出现/点不到"。关闭交给: 外部点击 + Esc + 选中后。
      search.addEventListener("keydown", function (e) {
        if (e.key !== "Enter" && e.keyCode !== 13) { return; }
        e.preventDefault();                 // 不提交表单
        // 有唯一匹配就加进去
        var hits = [];
        if (list) {
          Array.prototype.forEach.call(
            list.querySelectorAll(".person-suggest"), function (item) {
              if (!item.hidden) { hits.push(item); }
            });
        }
        if (hits.length === 1) {
          add(hits[0].getAttribute("data-name"),
              parseInt(hits[0].getAttribute("data-entity-id") || "0", 10) || 0);
        } else {
          var raw = search.value.trim();
          if (raw) { add(raw, 0); }         // 没有候选就直接用输入的名字
        }
      });
    }

    // 点页面其它地方也收起(候选框是浮层)
    if (!document.__peopleOutsideBound) {
      document.__peopleOutsideBound = true;
      document.addEventListener("mousedown", function (e) {
        var target = e.target;
        if (!target || !target.closest) { return; }
        // 点在搜索框/候选列表内部 -> 不关(否则点候选会先被关掉)
        if (target.closest(".people-picker")) { return; }
        if (target.closest(".people-suggest-list")) { return; }
        if (target.closest(".person-suggest")) { return; }
        Array.prototype.forEach.call(
          document.querySelectorAll(".people-suggest-list"),
          function (one) { one.style.display = "none"; });
      });
      // Esc 也收起
      document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") { return; }
        Array.prototype.forEach.call(
          document.querySelectorAll(".people-suggest-list"),
          function (one) { one.style.display = "none"; });
      });
    }

    sync();
  }
  /* ==========================================================================
     封面: 点击放大 + 多图槽位(左封面 / 右加号 / 全部可删)
     槽位规则:
       第 1 格 = **当前封面**(永远存在, 也可以删 -> 变成无封面)
       第 2..n 格 = 已上传的其它图
       最后一格 = 加号(点击选图/上传)
     ========================================================================== */
  function setupCoverGallery() {
    var form = document.getElementById("cover-form");
    var fileInput = document.getElementById("cover-input");
    var slots = document.querySelector("[data-cover-slots]");
    // 不能只找 [data-zoom-src]: "无封面"时左侧是 <div class="is-empty">,
    // 没有该属性 -> thumbImg 为 null -> 上传后左侧永远不更新。
    var thumbImg = document.querySelector(".edit-cover-img");
    if (!form || !fileInput) { return; }

    // ---- 灯箱放大 ----
    function openLightbox(src) {
      if (!src) { return; }
      var box = document.createElement("div");
      box.className = "cover-lightbox";
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-label", "封面大图");
      var img = document.createElement("img");
      img.src = src;
      img.alt = "封面大图";
      box.appendChild(img);
      box.addEventListener("click", function () { box.remove(); });
      document.addEventListener("keydown", function esc(e) {
        if (e.key === "Escape") {
          box.remove();
          document.removeEventListener("keydown", esc);
        }
      });
      document.body.appendChild(box);
    }
    var zoomBtn = document.querySelector("[data-cover-zoom]");
    if (zoomBtn) {
      zoomBtn.addEventListener("click", function (e) {
        e.preventDefault();
        if (thumbImg) { openLightbox(thumbImg.getAttribute("data-zoom-src")); }
      });
    }
    if (thumbImg) {
      thumbImg.addEventListener("click", function () {
        openLightbox(thumbImg.getAttribute("data-zoom-src"));
      });
    }

    if (!slots) { return; }
    var action = form.getAttribute("data-cover-action") || "";
    var token = form.getAttribute("data-cover-token") || "";
    var status = document.getElementById("cover-picked");

    function slotImages() {
      var out = [];
      Array.prototype.forEach.call(
        slots.querySelectorAll(".cover-slot[data-src]"), function (one) {
          out.push({ node: one, src: one.getAttribute("data-src") });
        });
      return out;
    }

    function setPrimary(src) {
      if (!thumbImg || !src) { return; }
      // "无封面"时 thumbImg 是 <div class="edit-cover-img is-empty">,
      // 直接设 .src 无效 —— 换成真的 <img>。
      if (thumbImg.tagName !== "IMG") {
        var img = document.createElement("img");
        img.className = "edit-cover-img";
        img.alt = "封面";
        img.loading = "lazy";
        thumbImg.parentNode.replaceChild(img, thumbImg);
        thumbImg = img;
      } else {
        thumbImg.classList.remove("is-empty");
        thumbImg.textContent = "";
      }
      thumbImg.setAttribute("data-zoom-src", src);
      thumbImg.src = src;
      // 补一个"放大"按钮(占位态时页面上没有)
      var thumbWrap = thumbImg.parentNode;
      if (thumbWrap && !thumbWrap.querySelector("[data-cover-zoom]")) {
        var zoom = document.createElement("button");
        zoom.type = "button";
        zoom.className = "edit-cover-zoom";
        zoom.setAttribute("data-cover-zoom", "1");
        zoom.textContent = "放大";
        zoom.addEventListener("click", function (e) {
          e.preventDefault();
          openLightbox(thumbImg.getAttribute("data-zoom-src"));
        });
        thumbWrap.appendChild(zoom);
      }
      Array.prototype.forEach.call(
        slots.querySelectorAll(".cover-slot[data-src]"), function (one) {
          one.classList.toggle("is-primary",
                               one.getAttribute("data-src") === src);
        });
    }

    function buildSlot(src, label) {
      var slot = document.createElement("div");
      slot.className = "cover-slot";
      slot.setAttribute("data-src", src);
      var img = document.createElement("img");
      img.src = src;
      img.alt = label || "封面";
      img.addEventListener("click", function (e) {
        e.preventDefault();
        setPrimary(src);
      });
      slot.appendChild(img);
      var del = document.createElement("button");
      del.type = "button";
      del.className = "cover-slot-x";
      del.setAttribute("data-cover-del", "1");
      del.setAttribute("aria-label", "删除这张图");
      del.textContent = "×";
      del.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        slot.remove();
        // 删掉的是当前封面 -> 把下一张提为封面
        var rest = slotImages();
        if (rest.length) {
          setPrimary(rest[0].src);
        } else if (thumbImg) {
          thumbImg.removeAttribute("data-zoom-src");
          thumbImg.removeAttribute("src");
          thumbImg.classList.add("is-empty");
          thumbImg.textContent = "无封面";
        }
      });
      return slot;
    }

    function addSlot(src, label, asPrimary) {
      if (!src) { return null; }
      var exists = slotImages().some(function (one) {
        return one.src === src;
      });
      if (exists) { return null; }
      var slot = buildSlot(src, label);
      // 插到加号之前(保持"加号永远在最后")
      var addBtn = slots.querySelector("[data-cover-add]");
      if (addBtn) {
        slots.insertBefore(slot, addBtn);
      } else {
        slots.appendChild(slot);
      }
      if (asPrimary) { setPrimary(src); }
      else if (!thumbImg || !thumbImg.getAttribute("data-zoom-src")) {
        setPrimary(src);
      }
      return slot;
    }

    // 初始: 当前封面进第 1 格
    if (thumbImg && thumbImg.getAttribute("data-zoom-src")) {
      addSlot(thumbImg.getAttribute("data-zoom-src"), "当前封面", true);
    }

    // 加号: 打开文件选择
    slots.addEventListener("click", function (e) {
      var add = e.target && e.target.closest
        ? e.target.closest("[data-cover-add]") : null;
      if (!add) { return; }
      e.preventDefault();
      fileInput.click();
    });

    function uploadOne(file, index, total) {
      return new Promise(function (resolve) {
        if (!action || typeof window.fetch !== "function") {
          resolve(null);
          return;
        }
        var body = new FormData();
        body.append("csrf_token", token);
        // 带上 next: 服务端上传成功后 303 回编辑页(否则跳详情页)
        var _next = form.getAttribute("data-cover-next") || "";
        if (_next) { body.append("next", _next); }
        body.append("cover", file);
        if (status) {
          status.textContent = "上传中 " + (index + 1) + "/" + total +
                               "：" + file.name;
        }
        window.fetch(action, {
          method: "POST",
          body: body,
          credentials: "same-origin"
        }).then(function (resp) {
          if (!resp.ok) { throw new Error("HTTP " + resp.status); }
          return resp.text();
        }).then(function () {
          // 服务端写到 covers/<key>.img, 加时间戳强制刷新
          resolve("/cover/" + encodeURIComponent(
            (form.getAttribute("data-cover-next") || "")
              .split("/album/")[1]
              ?.replace("/edit", "") || "") +
            "?v=" + Date.now());
        }).catch(function () { resolve(null); });
      });
    }

    fileInput.addEventListener("change", function () {
      var files = Array.prototype.slice.call(fileInput.files || []);
      if (!files.length) { return; }
      if (status) {
        status.textContent = "准备上传 " + files.length + " 张…";
      }
      var chain = Promise.resolve();
      files.forEach(function (file, i) {
        chain = chain.then(function () {
          return uploadOne(file, i, files.length).then(function (src) {
            if (src) {
              addSlot(src, file.name, i === 0 && !thumbImg.getAttribute("data-zoom-src"));
            }
          });
        });
      });
      chain.then(function () {
        if (status) {
          status.textContent = "已上传 " + files.length +
                               " 张（点缩略图设为封面，× 可删除）";
        }
        fileInput.value = "";
      });
    });

    // ---- 草稿: 清除 ----
    var clearBtn = document.querySelector("[data-draft-clear]");
    if (clearBtn) {
      clearBtn.addEventListener("click", function (e) {
        e.preventDefault();
        try {
          window.localStorage.removeItem("dddb:edit-draft");
          var bar = document.querySelector("[data-draft-bar] .draft-status");
          if (bar) { bar.textContent = "草稿已清除"; }
        } catch (err) { /* 隐私模式 */ }
      });
    }
  }
  /* ==========================================================================
     自动保存草稿: 编辑表单内容变化后(防抖 1.2s)存到 localStorage
     * 只存**表单值**, 不提交到服务器
     * 打开页面时如果有草稿, 提示可恢复(不自动覆盖, 避免冲掉别人的修改)
     ========================================================================== */
  function setupEditDraft() {
    var form = document.querySelector("form.edit-form");
    var bar = document.querySelector("[data-draft-bar] .draft-status");
    if (!form || typeof window.localStorage === "undefined") { return; }
    var KEY = "dddb:edit-draft";
    var action = form.getAttribute("action") || "";
    var timer = null;

    function collect() {
      var data = {};
      var els = form.querySelectorAll("input[name], textarea[name], select[name]");
      Array.prototype.forEach.call(els, function (el) {
        var name = el.getAttribute("name");
        if (!name || name === "csrf_token") { return; }
        if (el.type === "file") { return; }
        if (el.type === "checkbox" || el.type === "radio") {
          if (!el.checked) { return; }
        }
        if (Object.prototype.hasOwnProperty.call(data, name)) {
          if (!Array.isArray(data[name])) { data[name] = [data[name]]; }
          data[name].push(el.value);
        } else {
          data[name] = el.value;
        }
      });
      return data;
    }

    function save() {
      try {
        window.localStorage.setItem(KEY, JSON.stringify({
          action: action,
          at: Date.now(),
          values: collect()
        }));
        if (bar) {
          bar.textContent = "草稿已自动保存 " + new Date().toLocaleTimeString();
        }
      } catch (err) { /* 隐私模式 / 超限: 静默 */ }
    }

    form.addEventListener("input", function () {
      if (timer) { window.clearTimeout(timer); }
      timer = window.setTimeout(save, 1200);
    });
    form.addEventListener("change", function () {
      if (timer) { window.clearTimeout(timer); }
      timer = window.setTimeout(save, 1200);
    });

    // 有草稿就提示恢复(不自动套用)
    try {
      var raw = window.localStorage.getItem(KEY);
      if (!raw) { return; }
      var saved = JSON.parse(raw);
      if (!saved || saved.action !== action || !saved.values) { return; }
      if (!bar) { return; }
      bar.textContent = "发现 " + new Date(saved.at).toLocaleTimeString() +
                        " 的草稿";
      var restore = document.createElement("button");
      restore.type = "button";
      restore.className = "btn-ghost btn-xs";
      restore.textContent = "恢复草稿";
      restore.addEventListener("click", function (e) {
        e.preventDefault();
        var values = saved.values || {};
        Object.keys(values).forEach(function (name) {
          var list = Array.isArray(values[name]) ? values[name] : [values[name]];
          var els = form.querySelectorAll('[name="' + name + '"]');
          Array.prototype.forEach.call(els, function (el, i) {
            if (el.type === "file") { return; }
            if (i < list.length) {
              el.value = list[i];
              el.dispatchEvent(new Event("input", { bubbles: true }));
            }
          });
        });
        bar.textContent = "草稿已恢复";
      });
      bar.parentNode.appendChild(restore);
    } catch (err) { /* 解析失败忽略 */ }
  }

  /* ==========================================================================
     词条: 职责 + 人员都用**输入框**, 人员按回车加入
     * 人员在输入框敲名字回车 -> 加进该行(变成小 chip, 可 ×)
     * 同时在上方生成「职责 · 人名」按钮(详情页最终显示效果)
     * 输入时仍给库内艺人候选(点选也行, 但不是必须)
     ========================================================================== */
  function setupCreditPeople() {
    var card = document.querySelector('[data-card="credits"]');
    if (!card) { return; }
    var host = card.querySelector('[data-people="artists"]');
    if (!host) { return; }

    function chipNames() {
      var out = [];
      Array.prototype.forEach.call(
        host.querySelectorAll(".person-chip"), function (chip) {
          var name = chip.getAttribute("data-person");
          if (name) { out.push(name); }
        });
      return out;
    }

    function syncArtistsHidden() {
      var hidden = card.querySelector('input[type="hidden"][name="artists"]');
      if (!hidden) { return; }
      var names = [];
      Array.prototype.forEach.call(
        host.querySelectorAll(".person-chip"), function (one) {
          var n = one.getAttribute("data-person");
          if (n && names.indexOf(n) === -1) { names.push(n); }
        });
      // 词条里的人也要算进 artists
      Array.prototype.forEach.call(
        card.querySelectorAll(".credit-person-chip"), function (one) {
          var n = one.getAttribute("data-name");
          if (n && names.indexOf(n) === -1) { names.push(n); }
        });
      hidden.value = names.join("\n");
    }

    /* 在上方生成「职责 · 人名」按钮 */
    function addRoleChip(role, name) {
      var roleText = String(role || "").trim();
      var personText = String(name || "").trim();
      if (!personText) { return; }
      var existing = host.querySelectorAll(".person-chip");
      for (var i = 0; i < existing.length; i++) {
        var p = existing[i].getAttribute("data-person") || "";
        var r = existing[i].getAttribute("data-role") || "";
        if (p === personText && r === roleText) { return; }
      }
      var chip = document.createElement("span");
      chip.className = "person-chip";
      chip.setAttribute("data-person", personText);
      chip.setAttribute("data-role", roleText);
      chip.setAttribute("data-entity-id", "0");
      var kind = document.createElement("span");
      kind.className = "person-kind";
      kind.textContent = roleText || "艺人";
      var bold = document.createElement("b");
      bold.textContent = personText;
      var del = document.createElement("button");
      del.type = "button";
      del.className = "person-x";
      del.setAttribute("data-person-del", "1");
      del.setAttribute("aria-label", "移除 " + personText);
      del.textContent = "×";
      chip.appendChild(kind);
      chip.appendChild(bold);
      chip.appendChild(del);
      host.appendChild(chip);
      syncArtistsHidden();
    }

    /* 该行当前人员 */
    function valuesOf(row) {
      var out = [];
      Array.prototype.forEach.call(
        row.querySelectorAll(".credit-person-chip"), function (one) {
          var n = one.getAttribute("data-name");
          if (n) { out.push(n); }
        });
      return out;
    }

    /* 写回隐藏字段 credit_name(、分隔) */
    function writeValues(row) {
      var list = valuesOf(row);
      var hidden = row.querySelector('input[name="credit_name"]');
      if (hidden) { hidden.value = list.join("、"); }
      syncArtistsHidden();
    }

    /* 行内加一个人 */
    function addPersonToRow(row, name, roleText) {
      var value = String(name || "").trim();
      if (!value) { return false; }
      var have = valuesOf(row);
      if (have.indexOf(value) === -1) {
        var box = row.querySelector(".credit-people-chips");
        if (box) {
          var chip = document.createElement("span");
          chip.className = "credit-person-chip";
          chip.setAttribute("data-name", value);
          chip.textContent = value;
          var del = document.createElement("button");
          del.type = "button";
          del.className = "credit-person-x";
          del.setAttribute("data-credit-person-del", "1");
          del.setAttribute("aria-label", "移除 " + value);
          del.textContent = "×";
          chip.appendChild(del);
          box.appendChild(chip);
        }
      }
      writeValues(row);
      addRoleChip(roleText, value);
      return true;
    }

    /* ---- 回车加入人员 ---- */
    card.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.keyCode !== 13) { return; }
      var input = e.target;
      if (!input || !input.classList) { return; }
      e.preventDefault();

      // ① 人员输入框: 加入该行人员 + 生成上方按钮
      if (input.classList.contains("credit-person-input")) {
        var row = input.closest(".credit-row");
        if (!row) { return; }
        var raw = String(input.value || "").trim();
        if (!raw) { return; }
        var roleInput = row.querySelector('input[name="credit_role"]');
        var roleText = roleInput ? roleInput.value.trim() : "";
        // 支持一次敲多个(用、或,分隔)
        raw.split(/[、,，]/).forEach(function (one) {
          var name = one.trim();
          if (name) { addPersonToRow(row, name, roleText); }
        });
        input.value = "";
        return;
      }

      // ② 职责框: 把该行已有人员按这个职责再生成一遍按钮
      if (input.name === "credit_role") {
        var creditRow = input.closest(".credit-row");
        if (!creditRow) { return; }
        var people = valuesOf(creditRow);
        if (!people.length) {
          var hint = creditRow.querySelector(".credit-people-field .field-hint");
          if (hint) {
            hint.textContent = "先在上面输入人员";
            hint.classList.add("is-warn");
            window.setTimeout(function () {
              hint.textContent = "输入后回车加入";
              hint.classList.remove("is-warn");
            }, 1600);
          }
          return;
        }
        var r = input.value.trim();
        people.forEach(function (one) { addRoleChip(r, one); });
      }
    });

    /* ---- 删除行内人员 ---- */
    card.addEventListener("click", function (e) {
      var del = e.target && e.target.closest
        ? e.target.closest("[data-credit-person-del]") : null;
      if (!del) { return; }
      e.preventDefault();
      var chip = del.closest(".credit-person-chip");
      var row = chip ? chip.closest(".credit-row") : null;
      if (chip && chip.parentNode) { chip.parentNode.removeChild(chip); }
      if (row) { writeValues(row); }
    });

    /* ---- 上方按钮删除时, 同步清理词条里同名的人 ---- */
    host.addEventListener("click", function (e) {
      var del = e.target && e.target.closest
        ? e.target.closest("[data-person-del]") : null;
      if (!del) { return; }
      window.setTimeout(function () {
        var alive = chipNames();
        Array.prototype.forEach.call(
          card.querySelectorAll(".credit-row"), function (row) {
            var changed = false;
            Array.prototype.forEach.call(
              row.querySelectorAll(".credit-person-chip"), function (one) {
                if (alive.indexOf(one.getAttribute("data-name")) === -1) {
                  one.remove();
                  changed = true;
                }
              });
            if (changed) { writeValues(row); }
          });
      }, 0);
    });

    /* ---- 人员输入框: 输入时给艺人候选(点选或回车加入) ---- */
    function buildSuggestFor(input, row) {
      var wrap = input.closest(".credit-people-box");
      if (!wrap) { return null; }
      var box = wrap.querySelector(".credit-suggest");
      if (!box) {
        box = document.createElement("div");
        box.className = "credit-suggest";
        box.style.display = "none";
        wrap.appendChild(box);
      }
      return box;
    }

    function renderSuggest(input, row) {
      var box = buildSuggestFor(input, row);
      if (!box) { return; }
      var needle = String(input.value || "").trim().toLowerCase();
      if (!needle) {
        box.style.display = "none";
        box.textContent = "";
        return;
      }
      var have = valuesOf(row).map(function (one) { return one.toLowerCase(); });
      // 候选池缓存一次(页面上有 2000 个候选按钮, 每次输入都查 DOM 太慢)
      if (!window.__creditArtistPool) {
        var poolNodes = card.querySelectorAll(
          ".people-suggest-list .person-suggest");
        window.__creditArtistPool = Array.prototype.map.call(
          poolNodes, function (one) {
            return {
              name: one.getAttribute("data-name") || "",
              eid: one.getAttribute("data-entity-id") || ""
            };
          });
      }
      var pool = window.__creditArtistPool;
      var hits = [];
      pool.forEach(function (one) {
        var name = one.name || "";
        var eid = one.eid || "";
        var lower = name.toLowerCase();
        if (have.indexOf(lower) !== -1) { return; }
        if (lower.indexOf(needle) === -1 && eid !== needle) { return; }
        hits.push({ name: name, eid: eid });
      });
      box.textContent = "";
      if (!hits.length) {
        box.style.display = "none";
        return;
      }
      hits.slice(0, 20).forEach(function (item) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "credit-suggest-item";
        btn.setAttribute("data-name", item.name);
        btn.setAttribute("data-entity-id", item.eid);
        btn.textContent = item.name;
        var em = document.createElement("em");
        em.textContent = "#" + item.eid;
        btn.appendChild(em);
        box.appendChild(btn);
      });
      box.style.display = "block";
    }

    function hideAllSuggest() {
      Array.prototype.forEach.call(
        card.querySelectorAll(".credit-suggest"), function (box) {
          box.style.display = "none";
        });
    }

    // 输入时筛候选
    card.addEventListener("input", function (e) {
      var input = e.target;
      if (!input || !input.classList) { return; }
      if (!input.classList.contains("credit-person-input")) { return; }
      var row = input.closest(".credit-row");
      if (!row) { return; }
      renderSuggest(input, row);
    });

    // 候选点选(mousedown: 早于失焦)
    card.addEventListener("mousedown", function (e) {
      var btn = e.target && e.target.closest
        ? e.target.closest(".credit-suggest-item") : null;
      if (!btn) { return; }
      e.preventDefault();
      e.stopPropagation();
      var host = btn.closest(".credit-people-box");
      var input = host ? host.querySelector(".credit-person-input") : null;
      var row = input ? input.closest(".credit-row") : null;
      if (!row) { return; }
      var roleInput = row.querySelector('input[name="credit_role"]');
      var roleText = roleInput ? roleInput.value.trim() : "";
      addPersonToRow(row, btn.getAttribute("data-name"), roleText);
      if (input) { input.value = ""; }
      hideAllSuggest();
    });

    // 点别处 / Esc 关候选
    if (!document.__creditSuggestBound) {
      document.__creditSuggestBound = true;
      document.addEventListener("mousedown", function (e) {
        var t = e.target;
        if (!t || !t.closest) { return; }
        if (t.closest(".credit-people-box")) { return; }
        if (t.closest(".credit-suggest")) { return; }
        Array.prototype.forEach.call(
          document.querySelectorAll(".credit-suggest"), function (box) {
            box.style.display = "none";
          });
      });
      document.addEventListener("keydown", function (e) {
        if (e.key !== "Escape") { return; }
        Array.prototype.forEach.call(
          document.querySelectorAll(".credit-suggest"), function (box) {
            box.style.display = "none";
          });
      });
    }

    // 回车后收起候选
    card.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.keyCode === 13) {
        hideAllSuggest();
      }
    });

    // 初始化: 同步一次隐藏字段
    Array.prototype.forEach.call(
      card.querySelectorAll(".credit-row"), function (row) {
        writeValues(row);
      });
  }

  /* ==========================================================================
     编辑表单: 禁止回车直接提交
     用户在任意 input 里按 Enter 不该触发整页保存(很容易误操作);
     textarea(简介) 保留换行。要保存请点「保存」按钮或按 Ctrl/Cmd+Enter。
     ========================================================================== */
  function setupNoEnterSubmit() {
    var form = document.querySelector("form.edit-form");
    if (!form) { return; }

    form.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.keyCode !== 13) { return; }
      var el = e.target;
      if (!el || !el.tagName) { return; }
      var tag = el.tagName.toUpperCase();
      // textarea 里回车是换行, 放行
      if (tag === "TEXTAREA") { return; }
      // 提交按钮上的回车是"确认", 放行
      if (tag === "BUTTON" && el.type === "submit") { return; }
      // Ctrl/Cmd + Enter = 显式提交
      if (e.ctrlKey || e.metaKey) { return; }
      // 其余(包括 input)一律不提交。
      // **只 preventDefault, 绝不 stopPropagation** —— 捕获阶段就截断事件会让
      // 其它监听器(职责/人员回车、候选选择)全都收不到, 表现为"回车没反应"。
      if (tag === "INPUT" || tag === "SELECT") {
        e.preventDefault();
      }
    }, true);   // 捕获阶段: 先于浏览器默认提交行为

    // Ctrl/Cmd+Enter 快速保存
    form.addEventListener("keydown", function (e) {
      if (!(e.ctrlKey || e.metaKey)) { return; }
      if (e.key !== "Enter" && e.keyCode !== 13) { return; }
      e.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      if (btn) { btn.click(); }
    });
  }

  /* 1d. 编辑者申请弹窗: 只能点「关闭」按钮关(禁 Esc / 禁点遮罩), 自动存草稿 */
  function setupApplyDialog() {
    var dialog = document.getElementById("apply-dialog");
    var open = document.getElementById("apply-open");
    var close = document.getElementById("apply-close");
    var reason = document.getElementById("apply-reason");
    var hint = document.getElementById("apply-draft-hint");
    if (!dialog || !open || !close || !reason) {
      return;
    }
    var key = dialog.getAttribute("data-draft-key") || "dddb:apply-draft";
    function saveDraft() {
      try {
        if (reason.value.trim()) {
          window.localStorage.setItem(key, reason.value);
          if (hint) { hint.textContent = "草稿已自动保存(" + new Date().toLocaleTimeString() + ")"; }
        } else {
          window.localStorage.removeItem(key);
        }
      } catch (e) { /* 隐私模式无 localStorage: 静默降级 */ }
    }
    function restoreDraft() {
      try {
        var saved = window.localStorage.getItem(key);
        if (saved && !reason.value) {
          reason.value = saved;
          if (hint) { hint.textContent = "已恢复上次未提交的草稿"; }
        }
      } catch (e) { /* ignore */ }
    }
    open.addEventListener("click", function () {
      restoreDraft();
      if (typeof dialog.showModal === "function") { dialog.showModal(); }
      else { dialog.setAttribute("open", "open"); }
      reason.focus();
    });
    close.addEventListener("click", function () {
      saveDraft();
      if (typeof dialog.close === "function") { dialog.close(); }
      else { dialog.removeAttribute("open"); }
    });
    // 只允许「关闭」按钮: 禁 Esc, 禁点遮罩
    dialog.addEventListener("cancel", function (event) { event.preventDefault(); });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) { event.preventDefault(); }
    });
    reason.addEventListener("input", saveDraft);
    var form = reason.form;
    if (form) {
      form.addEventListener("submit", function () {
        try { window.localStorage.removeItem(key); } catch (e) { /* ignore */ }
      });
    }
  }

  /* 1e. 外站链接: 点击前确认(用户要求"点击后应提示正在跳转站外链接")
   *
   * 详情页的外链按钮 href 是本站中转地址(/link/<key>/<n>), 真实目标由服务端
   * 302 过去 —— 所以提示里不该直接打印 link.href(用户看到内部路径会困惑),
   * 只说明"即将离开本站"即可。 */
  function setupExternalLinks() {
    document.addEventListener("click", function (event) {
      var link = event.target && event.target.closest
        ? event.target.closest("a[data-external]") : null;
      if (!link) { return; }
      var target = link.getAttribute("href") || "";
      var isRelay = target.indexOf("/link/") === 0;
      var message = isRelay
        ? "即将跳转到站外链接。\n\n本站只做收录, 不对跳转目标的内容负责;\n请确认是安全链接后再继续访问。"
        : "你正在跳转到站外链接：\n" + link.href +
          "\n\n请确认是安全链接后再继续访问。";
      if (!window.confirm(message)) { event.preventDefault(); }
    });
  }

  /* 1f. 曲目表列宽拖拽
   *   硬约束: **所有列宽之和 ≤ 容器可用宽度**(拖动/恢复都按这条夹紧),
   *   加上外层 .table-scroll 的横向滚动, 保证永远不会把页面撑出边界。 */
  function clampColumnWidths(widths, available, minWidth) {
    // 可用宽度不可能小于「列数 × 最小宽」—— 更窄时交给 .table-scroll 横向滚动
    available = Math.max((minWidth || 56) * widths.length, available || 0);
    // 零/空值先归一到最小宽(旧版本存过 0 的话也能自愈)
    for (var z = 0; z < widths.length; z++) {
      if (!widths[z] || widths[z] < minWidth) { widths[z] = minWidth; }
    }
    var total = 0;
    for (var i = 0; i < widths.length; i++) { total += widths[i] || 0; }
    if (total > available && total > 0) {
      var ratio = available / total;
      for (var j = 0; j < widths.length; j++) {
        widths[j] = Math.max(minWidth, Math.round((widths[j] || 0) * ratio));
      }
      total = 0;
      for (var k = 0; k < widths.length; k++) { total += widths[k]; }
    }
    if (total > available) {                  // 缩到下限仍超宽: 从最宽列往下削
      var widest = 0;
      for (var m = 1; m < widths.length; m++) {
        if (widths[m] > widths[widest]) { widest = m; }
      }
      widths[widest] = Math.max(minWidth, widths[widest] - (total - available));
    }
    return widths;
  }

  window.clampColumnWidths = clampColumnWidths;


  /* 1g. 通用对话框: [data-dialog-open="id"] 打开, [data-dialog-close] 关闭 */

  /* 1h. 实体绑定输入: 回车分隔多值 / 有输入才出候选(带头像) / 选中即成项 */

  /* 1j. 外观: 主题 / 自定义背景 / 密度圆角字号, 存 localStorage 并即时生效 */
  var APPEARANCE_KEY = "dddb:appearance";

  function readAppearance() {
    try {
      return JSON.parse(window.localStorage.getItem(APPEARANCE_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  function saveAppearance(prefs) {
    try {
      window.localStorage.setItem(APPEARANCE_KEY, JSON.stringify(prefs || {}));
    } catch (e) { /* 隐私模式: 忽略 */ }
  }

  function applyAppearance(prefs) {
    var root = document.documentElement;
    var theme = prefs.theme || "dark";
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-density", prefs.density || "normal");
    root.setAttribute("data-radius", prefs.radius || "default");
    root.setAttribute("data-font", prefs.font || "normal");
    var body = document.body;
    if (!body) { return; }
    // 背景图来自**本站上传**的 /background/<uid>, 不再读外链
    var uploaded = body.getAttribute("data-bg-src") || "";
    var stored = prefs.bg_enabled === false ? "" : uploaded;
    if (stored) {
      body.style.setProperty("--bg-image", 'url("' + stored + '")');
    } else {
      body.style.removeProperty("--bg-image");
    }
    var dim = Number(prefs.bg_dim);
    body.style.setProperty("--bg-dim", isNaN(dim) ? "0.6" : String(dim / 100));
    var blur = Number(prefs.bg_blur);
    body.style.setProperty("--bg-blur", (isNaN(blur) ? 0 : blur) + "px");
    // 同步设置面板的选中态
    var buttons = document.querySelectorAll(".theme-swatch");
    Array.prototype.forEach.call(buttons, function (button) {
      button.setAttribute("aria-pressed",
        button.getAttribute("data-theme-value") === theme ? "true" : "false");
    });
  }


  /* 1k. 外观浮动按钮: 常驻右下角, 点开小面板, 与账号页共享同一套偏好 */
  function setupAppearanceFab() {
    var button = document.getElementById("appearance-fab");
    var pop = document.getElementById("appearance-pop");
    if (!button || !pop) { return; }
    var prefs = readAppearance();
    var dimInput = document.getElementById("fab-bg-dim");
    var blurInput = document.getElementById("fab-bg-blur");
    var density = document.getElementById("fab-density");
    var radius = document.getElementById("fab-radius");
    var font = document.getElementById("fab-font");

    function sync() {
      prefs = readAppearance();
        if (dimInput && prefs.bg_dim != null) { dimInput.value = prefs.bg_dim; }
      if (blurInput && prefs.bg_blur != null) { blurInput.value = prefs.bg_blur; }
      if (density) { density.value = prefs.density || "normal"; }
      if (radius) { radius.value = prefs.radius || "default"; }
      if (font) { font.value = prefs.font || "normal"; }
      var current = prefs.theme || "dark";
      Array.prototype.forEach.call(pop.querySelectorAll(".theme-swatch"), function (b) {
        b.setAttribute("aria-pressed",
          b.getAttribute("data-theme-value") === current ? "true" : "false");
      });
    }

    function update(patch) {
      prefs = readAppearance();
      Object.keys(patch).forEach(function (k) { prefs[k] = patch[k]; });
      saveAppearance(prefs);
      applyAppearance(prefs);
      sync();
    }

    button.addEventListener("click", function (event) {
      event.stopPropagation();
      pop.hidden = !pop.hidden;
      if (!pop.hidden) { sync(); }
    });
    document.addEventListener("click", function (event) {
      if (!pop.hidden && !pop.contains(event.target) && event.target !== button) {
        pop.hidden = true;
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { pop.hidden = true; }
    });

    Array.prototype.forEach.call(pop.querySelectorAll(".theme-swatch"), function (b) {
      b.addEventListener("click", function () {
        update({ theme: b.getAttribute("data-theme-value") || "dark" });
      });
    });
    if (dimInput) {
      dimInput.addEventListener("input", function () {
        update({ bg_dim: Number(dimInput.value) });
      });
    }
    if (blurInput) {
      blurInput.addEventListener("input", function () {
        update({ bg_blur: Number(blurInput.value) });
      });
    }
    [[density, "density"], [radius, "radius"], [font, "font"]].forEach(function (pair) {
      if (!pair[0]) { return; }
      pair[0].addEventListener("change", function () {
        var patch = {};
        patch[pair[1]] = pair[0].value;
        update(patch);
      });
    });
    var reset = document.getElementById("fab-reset");
    if (reset) {
      reset.addEventListener("click", function () {
        prefs = {};
        saveAppearance({});
        applyAppearance({});
        sync();
      });
    }
    sync();
  }

  function setupAppearance() {
    var prefs = readAppearance();
    applyAppearance(prefs);
    var panel = document.getElementById("appearance");
    if (!panel) { return; }

    function update(patch) {
      prefs = readAppearance();
      Object.keys(patch).forEach(function (k) { prefs[k] = patch[k]; });
      saveAppearance(prefs);
      applyAppearance(prefs);
    }

    Array.prototype.forEach.call(panel.querySelectorAll(".theme-swatch"), function (button) {
      button.addEventListener("click", function () {
        update({ theme: button.getAttribute("data-theme-value") || "dark" });
      });
    });

    // 背景图由服务端上传后经 data-bg-src 提供, 这里只做预览
    var preview = document.getElementById("pref-bg-preview");
    function refreshPreview() {
      if (!preview) { return; }
      var uploaded = (document.body && document.body.getAttribute("data-bg-src")) || "";
      preview.style.backgroundImage = uploaded ? 'url("' + uploaded + '")' : "none";
    }
    refreshPreview();
    [["pref-bg-dim", "bg_dim"], ["pref-bg-blur", "bg_blur"]].forEach(function (pair) {
      var input = document.getElementById(pair[0]);
      if (!input) { return; }
      input.addEventListener("input", function () {
        var patch = {};
        patch[pair[1]] = Number(input.value);
        update(patch);
      });
    });
    [["pref-density", "density"], ["pref-radius", "radius"], ["pref-font", "font"]]
      .forEach(function (pair) {
        var select = document.getElementById(pair[0]);
        if (!select) { return; }
        select.addEventListener("change", function () {
          var patch = {};
          patch[pair[1]] = select.value;
          update(patch);
        });
      });
    var reset = document.getElementById("pref-reset");
    if (reset) {
      reset.addEventListener("click", function () {
        prefs = {};
        saveAppearance({});
        applyAppearance({});
        refreshPreview();
        toast && toast("已恢复默认外观");
      });
    }
  }

  function setupBindFields() {
    var fields = document.querySelectorAll(".bind-field");
    Array.prototype.forEach.call(fields, function (wrapper) {
      var input = wrapper.querySelector(".bind-input");
      var hidden = wrapper.querySelector('input[type="hidden"]');
      var chips = wrapper.querySelector(".bind-chips");
      var list = wrapper.querySelector(".suggest-list");
      if (!input || !hidden || !chips) { return; }
      var items = [];
      // 展会字段: **不需要输入就显示全部候选**(用户要求)
      var showAllOnFocus = wrapper.getAttribute("data-suggest-all") === "1";

      function sync() {
        hidden.value = items.join("\n");
        chips.textContent = "";
        items.forEach(function (value, index) {
          var chip = document.createElement("span");
          chip.className = "bind-chip";
          chip.textContent = value;
          var remove = document.createElement("button");
          remove.type = "button";
          remove.className = "bind-chip-x";
          remove.setAttribute("aria-label", "移除 " + value);
          remove.textContent = "×";
          remove.addEventListener("click", function () {
            items.splice(index, 1);
            sync();
          });
          chip.appendChild(remove);
          chips.appendChild(chip);
        });
      }

      function add(raw) {
        var value = String(raw || "").trim();
        if (!value) { return; }
        if (items.indexOf(value) === -1) { items.push(value); }
        input.value = "";
        sync();
        filter("");
      }

      function filter(text) {
        if (!list) { return; }
        var needle = String(text || "").trim().toLowerCase();
        var buttons = list.querySelectorAll(".suggest-item");
        var shown = 0;
        Array.prototype.forEach.call(buttons, function (button) {
          var raw = button.getAttribute("data-value") || "";
          var value = raw.toLowerCase();
          var hit;
          if (!needle) {
            // 空输入: 只有"总是显示候选"的字段才列全部(展会)
            hit = showAllOnFocus && !items.some(function (one) {
              return one.toLowerCase() === value;
            });
          } else {
            hit = value.indexOf(needle) !== -1 && value !== needle;
          }
          button.hidden = !hit;
          if (hit) { shown += 1; }
          button.onclick = function () { add(raw); };
        });
        list.style.display = shown ? "block" : "none";
      }

      // 初始值: 换行或"、"都拆开(兼容旧数据)
      (hidden.value || "").split(/[\n、]+/).forEach(function (part) {
        var value = part.trim();
        if (value && items.indexOf(value) === -1) { items.push(value); }
      });
      sync();

      input.addEventListener("input", function () { filter(input.value); });
      input.addEventListener("focus", function () { filter(input.value); });
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === "Tab") {
          if (input.value.trim()) {
            event.preventDefault();
            add(input.value);
          }
        } else if (event.key === "Backspace" && !input.value && items.length) {
          items.pop();
          sync();
        } else if (event.key === "Escape" && list) {
          list.style.display = "none";
        }
      });
      input.addEventListener("blur", function () {
        setTimeout(function () { if (list) { list.style.display = "none"; } }, 150);
      });
    });
  }

  /* 1i. 展会联动: 选了展会就按库里的日期/场馆自动补全 */
  function setupEventSync() {
    var form = document.querySelector('[data-entity-form]');
    if (!form) { return; }
    var eventField = form.querySelector('.bind-field[data-bind="event"]');
    var dateInput = form.querySelector('input[name="release_date"]');
    if (!eventField || !dateInput) { return; }
    var hidden = eventField.querySelector('input[type="hidden"]');
    if (!hidden) { return; }
    var map = {};
    var buttons = eventField.querySelectorAll(".suggest-item");
    Array.prototype.forEach.call(buttons, function (button) {
      var name = button.getAttribute("data-value") || "";
      var text = button.textContent || "";
      var match = text.match(/(\d{4}-\d{2}-\d{2})/);
      if (name && match) { map[name] = match[1]; }
    });
    hidden.addEventListener("change", function () {
      var value = hidden.value.split("\n")[0];
      if (value && map[value] && !dateInput.value) { dateInput.value = map[value]; }
    });
  }

  function setupDialogs() {
    var openers = document.querySelectorAll("[data-dialog-open]");
    Array.prototype.forEach.call(openers, function (button) {
      button.addEventListener("click", function () {
        var dialog = document.getElementById(button.getAttribute("data-dialog-open"));
        if (dialog && typeof dialog.showModal === "function") { dialog.showModal(); }
      });
    });
    var closers = document.querySelectorAll("[data-dialog-close]");
    Array.prototype.forEach.call(closers, function (button) {
      button.addEventListener("click", function () {
        var dialog = button.closest("dialog");
        if (dialog && typeof dialog.close === "function") { dialog.close(); }
      });
    });
    var dialogs = document.querySelectorAll("dialog.app-dialog");
    Array.prototype.forEach.call(dialogs, function (dialog) {
      dialog.addEventListener("click", function (event) {
        if (event.target === dialog) { dialog.close(); }   // 点遮罩关闭
      });
    });
  }

  function setupTableResize() {
    var MIN_COL = 56;
    var tables = document.querySelectorAll("table.tracklist");
    Array.prototype.forEach.call(tables, function (table) {
      var key = "dddb:colw:" + (table.getAttribute("data-key") || "default");
      var headers = table.querySelectorAll("thead th");
      if (!headers.length) { return; }
      var wrap = table.parentElement;

      function available() {
        var box = wrap && wrap.clientWidth ? wrap.clientWidth : table.clientWidth;
        return Math.max(headers.length * MIN_COL, (box || 0) - 2);
      }

      function applyWidths(widths) {
        var avail = available();
        var clamped = clampColumnWidths(widths.slice(), avail, MIN_COL);
        var total = 0;
        Array.prototype.forEach.call(headers, function (th, index) {
          var width = clamped[index] || MIN_COL;
          total += width;
          th.style.width = width + "px";
          th.style.minWidth = MIN_COL + "px";
        });
        Array.prototype.forEach.call(table.rows, function (row) {
          Array.prototype.forEach.call(row.cells, function (cell, index) {
            if (index < clamped.length) {
              cell.style.width = clamped[index] + "px";
            }
          });
        });
        table.style.tableLayout = "fixed";
        table.style.width = Math.min(total, avail) + "px";
        table.style.maxWidth = avail + "px";
      }

      var saved = null;
      try {
        saved = JSON.parse(window.localStorage.getItem(key) || "null");
      } catch (e) { saved = null; }
      if (saved && saved.length) { applyWidths(saved); }

      Array.prototype.forEach.call(headers, function (th, index) {
        if (index === headers.length - 1) { return; }   // 最后一列不拖
        var grip = document.createElement("span");
        grip.className = "col-grip";
        grip.title = "拖动调整列宽";
        th.appendChild(grip);
        grip.addEventListener("pointerdown", function (event) {
          event.preventDefault();
          var startX = event.clientX;
          var startWidth = th.getBoundingClientRect().width;
          table.classList.add("is-resizing");
          function move(moveEvent) {
            var avail = available();
            var widths = [];
            var others = 0;
            Array.prototype.forEach.call(headers, function (h, i) {
              widths[i] = Math.round(h.getBoundingClientRect().width);
              if (i !== index) { others += widths[i]; }
            });
            var want = startWidth + (moveEvent.clientX - startX);
            // 关键: 新宽度不能侵占其余列 —— 上限 = 可用宽度 - 其余列之和
            var limit = Math.max(MIN_COL, avail - others - 2);
            widths[index] = Math.max(MIN_COL, Math.min(want, limit));
            applyWidths(widths);
          }
          function up() {
            document.removeEventListener("pointermove", move);
            document.removeEventListener("pointerup", up);
            table.classList.remove("is-resizing");
            var widths = [];
            Array.prototype.forEach.call(headers, function (h) {
              widths.push(Math.round(h.getBoundingClientRect().width));
            });
            try {
              window.localStorage.setItem(key, JSON.stringify(clampColumnWidths(
                widths, available(), MIN_COL)));
            } catch (e) { /* 隐私模式: 忽略 */ }
          }
          document.addEventListener("pointermove", move);
          document.addEventListener("pointerup", up);
        });
      });
      // 窗口尺寸变化时重新夹紧(窄屏也不会溢出)
      window.addEventListener("resize", function () {
        var widths = [];
        Array.prototype.forEach.call(headers, function (h) {
          widths.push(Math.round(h.getBoundingClientRect().width));
        });
        applyWidths(widths);
      });
    });
  }

  /* 2. 防重复提交: 标了 data-submit-once 的表单, 提交后禁用按钮 */
  function setupSubmitOnce() {
    var forms = document.querySelectorAll("form[data-submit-once]");
    Array.prototype.forEach.call(forms, function (form) {
      form.addEventListener("submit", function () {
        var button = form.querySelector('button[type="submit"]');
        if (button) {
          button.disabled = true;
          button.textContent = "提交中…";
        }
      });
    });
  }

  /* 3. 二次确认: 标了 data-confirm 的**表单**提交前问一句(用户拒绝则取消) */
  function setupConfirm() {
    var forms = document.querySelectorAll("form[data-confirm]");
    Array.prototype.forEach.call(forms, function (form) {
      form.addEventListener("submit", function (event) {
        var message = form.getAttribute("data-confirm") || "确定继续吗?";
        if (!window.confirm(message)) {
          event.preventDefault();
        }
      });
    });
  }


  /* ==========================================================================
     封面上传(独立于主表单)
     封面区不再是 form(嵌在主表单里会被浏览器丢弃), 所以用 fetch 单独上传;
     主表单的保存不会带上封面文件。
     ========================================================================== */
  function setupCoverUpload() {
    /* 「上传封面」按钮 = **打开文件选择**.

       真正的上传由 setupCoverGallery 负责(选完图立刻自动上传)。
       以前这里也读 input.files 再 POST 一次, 但两个函数绑同一个
       #cover-input, gallery 传完会把 input 清空 -> 这里读到空 ->
       报"请先选择一张图片"(用户截图)。

       所以这里只做一件事: 点按钮 -> 触发选图。若页面没有 gallery
       (创建专辑那种单图场景), 才退回"读 input 直接上传"的老逻辑。
    */
    var wrap = document.getElementById("cover-form");
    var input = document.getElementById("cover-input");
    var hint = document.getElementById("cover-picked");
    var button = wrap ? wrap.querySelector("[data-cover-upload]") : null;
    if (!wrap || !input || !button) { return; }

    var hasGallery = !!document.querySelector("[data-cover-slots]");

    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (hint) {
        hint.textContent = file
          ? "已选择: " + file.name + " (" + Math.round(file.size / 1024) +
            " KB)"
          : "";
      }
    });

    button.addEventListener("click", function (e) {
      e.preventDefault();
      // 有多图槽位: 上传交给 gallery, 这里只负责打开选择框
      if (hasGallery) {
        input.click();
        return;
      }
      // 单图场景: 退回直接上传
      var file = input.files && input.files[0];
      if (!file) {
        if (hint) { hint.textContent = "请先选择一张图片"; }
        input.click();
        return;
      }
      if (file.size > 2 * 1024 * 1024) {
        if (hint) { hint.textContent = "图片不能超过 2MB"; }
        return;
      }
      var action = wrap.getAttribute("data-cover-action") || "";
      var token = wrap.getAttribute("data-cover-token") || "";
      var next = wrap.getAttribute("data-cover-next") || "";
      if (!action) { return; }

      var body = new FormData();
      body.append("csrf_token", token);
      body.append("next", next);
      body.append("cover", file);

      button.disabled = true;
      var original = button.textContent;
      button.textContent = "上传中…";
      if (hint) { hint.textContent = "正在上传…"; }

      fetch(action, {
        method: "POST",
        body: body,
        credentials: "same-origin"
      }).then(function (resp) {
        if (resp.ok) {
          var target = next || window.location.pathname;
          var sep = target.indexOf("?") >= 0 ? "&" : "?";
          window.location.href = target + sep + "t=" + Date.now();
          return;
        }
        return resp.text().then(function (text) {
          var msg = "HTTP " + resp.status;
          var mm = text && text.match(
            /<p class="empty-title">[^<]*<\/p>\s*<p>([^<]*)<\/p>/);
          if (mm && mm[1]) { msg = mm[1]; }
          throw new Error(msg);
        });
      }).catch(function (err) {
        button.disabled = false;
        button.textContent = original;
        if (hint) { hint.textContent = "上传失败: " + err.message; }
      });
    });
  }


  function trackCell(name, label, value, placeholder, cls) {
    var td = document.createElement("td");
    if (cls) { td.className = cls; }
    // data-label 供窄屏 ::before 显示列名(手机上才分得清每格填什么)
    if (label) { td.setAttribute("data-label", label); }
    var input = document.createElement("input");
    input.type = "text";
    input.name = name;
    input.value = value == null ? "" : String(value);
    if (placeholder) { input.placeholder = placeholder; }
    td.appendChild(input);
    return td;
  }

  var TRACK_COLS = [
    { name: "track_index", label: "音轨", cls: "trk-col-idx", type: "text" },
    { name: "track_title", label: "歌曲名" },
    { name: "track_subtitle", label: "副标题", placeholder: "可选" },
    { name: "track_title_zh", label: "翻译歌名", placeholder: "可选" },
    { name: "track_artists", label: "艺人", placeholder: "、分隔" },
    { name: "track_lang", label: "语种", placeholder: "可选" },
    { name: "track_genre", label: "曲风", placeholder: "可选" },
    { name: "track_duration", label: "时长", placeholder: "3:42" }
  ];

  function buildTrackRow(disc, index) {
    var tr = document.createElement("tr");
    tr.className = "track-row";
    tr.setAttribute("data-disc", String(disc));

    // ① 排序列(拖动把手 + 隐藏字段) —— **必须在最前**, 与模板一致
    var sortTd = document.createElement("td");
    sortTd.className = "trk-col-sort";
    sortTd.setAttribute("data-label", "排序");
    var dragBtn = document.createElement("button");
    dragBtn.type = "button";
    dragBtn.className = "trk-drag";
    dragBtn.setAttribute("data-track-drag", "1");
    dragBtn.setAttribute("draggable", "true");
    dragBtn.title = "按住拖动排序";
    dragBtn.setAttribute("aria-label", "拖动排序");
    dragBtn.textContent = "⠿";
    sortTd.appendChild(dragBtn);
    // 隐藏字段: 碟号 —— **放进 td 里**(<tr> 直接放 input 是非法 HTML)
    var discHidden = document.createElement("input");
    discHidden.type = "hidden";
    discHidden.name = "track_disc";
    discHidden.value = String(disc);
    sortTd.appendChild(discHidden);
    tr.appendChild(sortTd);

    // ② 各字段列(含时长)
    TRACK_COLS.forEach(function (col) {
      var value = col.name === "track_index" ? index : "";
      tr.appendChild(trackCell(col.name, col.label, value,
                               col.placeholder, col.cls));
    });

    // ③ 操作列
    var ops = document.createElement("td");
    ops.className = "trk-col-ops";
    ops.setAttribute("data-label", "操作");
    var del = document.createElement("button");
    del.type = "button";
    del.className = "btn-ghost btn-xs";
    del.setAttribute("data-track-del", "1");
    del.title = "删除这一首";
    del.textContent = "删除";
    ops.appendChild(del);
    tr.appendChild(ops);
    return tr;
  }

  function discBody(discBlock) {
    return discBlock ? discBlock.querySelector("tbody") : null;
  }

  function discNumber(discBlock) {
    return parseInt(discBlock.getAttribute("data-disc") || "1", 10) || 1;
  }

  function renumberDisc(discBlock) {
    var body = discBody(discBlock);
    if (!body) { return; }
    var rows = body.querySelectorAll("tr.track-row");
    Array.prototype.forEach.call(rows, function (row, i) {
      var idx = row.querySelector('input[name="track_index"]');
      if (idx) { idx.value = String(i + 1); }
    });
    var empty = discBlock.querySelector(".disc-empty");
    if (empty) { empty.style.display = rows.length ? "none" : ""; }
  }

  function lastDiscNumber() {
    var blocks = document.querySelectorAll(".disc-block");
    var max = 0;
    Array.prototype.forEach.call(blocks, function (b) {
      var n = discNumber(b);
      if (n > max) { max = n; }
    });
    return max || 1;
  }

  function focusTitle(scope) {
    if (!scope) { return; }
    var rows = scope.querySelectorAll("tr.track-row");
    if (!rows.length) { return; }
    var last = rows[rows.length - 1];
    var input = last.querySelector('input[name="track_title"]');
    if (input) {
      input.focus();
      if (last.scrollIntoView) { last.scrollIntoView({ block: "nearest" }); }
    }
  }

  function addDiscBlock(discNo) {
    var host = document.getElementById("track-rows");
    if (!host) { return; }
    var block = document.createElement("section");
    block.className = "disc-block";
    block.setAttribute("data-disc", String(discNo));

    var head = document.createElement("div");
    head.className = "disc-head";
    var title = document.createElement("span");
    title.className = "disc-title";
    title.textContent = "Disc " + (discNo < 10 ? "0" + discNo : discNo);
    head.appendChild(title);

    var tools = document.createElement("span");
    tools.className = "disc-tools";
    // 「批量添加歌曲」已按用户要求移除, 只剩重排/删碟
    [["data-disc-renumber", "1", "调整曲轨符", "按当前顺序重排音轨号"],
     ["data-disc-remove", "1", "删除光盘", "删除整张碟"]].forEach(
      function (item) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn-ghost btn-xs";
        btn.setAttribute(item[0], item[1]);
        btn.title = item[3];
        btn.textContent = item[2];
        tools.appendChild(btn);
      });
    head.appendChild(tools);
    block.appendChild(head);

    var wrap = document.createElement("div");
    wrap.className = "track-table-wrap";
    var table = document.createElement("table");
    table.className = "track-table";
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    // 列顺序必须与模板 _track_row_html / 表头一致:
    // 排序 音轨 歌曲名 副标题 翻译歌名 艺人 语种 曲风 时长 操作
    ["排序", "音轨", "歌曲名", "副标题", "翻译歌名", "艺人", "语种",
     "曲风", "时长", "操作"].forEach(function (label) {
      var th = document.createElement("th");
      th.textContent = label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    tbody.setAttribute("data-disc-body", String(discNo));
    table.appendChild(tbody);
    wrap.appendChild(table);
    block.appendChild(wrap);

    var empty = document.createElement("div");
    empty.className = "disc-empty";
    empty.textContent = "暂无数据";
    block.appendChild(empty);
    // 新建碟**立即给一行**, 省得看到空表以为没加上
    if (typeof buildTrackRow === "function") {
      tbody.appendChild(buildTrackRow(discNo, 1));
    }

    var addWrap = document.createElement("div");
    addWrap.className = "disc-add";
    var addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "btn-ghost btn-xs";
    addBtn.setAttribute("data-track-add", "1");
    addBtn.textContent = "＋ 添加专辑歌曲";
    addWrap.appendChild(addBtn);
    block.appendChild(addWrap);

    // 插到最后一块碟之后(添加光盘按钮之前)
    host.appendChild(block);
    var actions = document.querySelector(".disc-add-row");
    if (actions && actions.parentNode === host) {
      host.insertBefore(block, actions);
    }

    // 新碟默认加一首
    tbody.appendChild(buildTrackRow(discNo, 1));
    renumberDisc(block);
    focusTitle(block);
    return block;
  }

  /* ==========================================================================
     词条 / 外链: 增删一行
     与曲目表同一套事件委托; 新增行必须带正确的 name 才能被解析。
     ========================================================================== */
  var PAIR_KINDS = {
    credit: {
      fields: [["credit_role", "词条", "如 编曲 / 演唱"],
               ["credit_name", "人员", "多人用、分隔"]]
    },
    url: {
      fields: [["url_key", "链接名", "自定义标签"],
               ["url_value", "网址", "https://..."]]
    }
  };

  /* 新增一条词条: 结构必须与服务端 _credit_row 完全一致
   * (职责输入框 + 人员输入框 + 行内 chips + 隐藏 credit_name),
   * 否则 setupCreditPeople 的回车/候选逻辑匹配不到新行。 */
  function buildCreditRow() {
    var row = document.createElement("div");
    row.className = "pair-row credit-row";
    row.setAttribute("data-pair-row", "credit");

    // 职责
    var roleLabel = document.createElement("label");
    roleLabel.className = "field auth-field";
    var roleSpan = document.createElement("span");
    roleSpan.textContent = "职责";
    var roleInput = document.createElement("input");
    roleInput.type = "text";
    roleInput.name = "credit_role";
    roleInput.placeholder = "如 编曲 / 演唱";
    roleLabel.appendChild(roleSpan);
    roleLabel.appendChild(roleInput);
    row.appendChild(roleLabel);

    // 人员: 输入框 + 回车加入
    var peopleField = document.createElement("div");
    peopleField.className = "field auth-field credit-people-field";
    var peopleSpan = document.createElement("span");
    peopleSpan.textContent = "人员";
    var hint = document.createElement("span");
    hint.className = "field-hint";
    hint.textContent = "输入后回车加入";
    peopleSpan.appendChild(hint);
    var box = document.createElement("div");
    box.className = "credit-people-box";
    var chips = document.createElement("div");
    chips.className = "credit-people-chips";
    var personInput = document.createElement("input");
    personInput.type = "text";
    personInput.className = "credit-person-input";
    personInput.placeholder = "输入艺人名后回车";
    personInput.setAttribute("autocomplete", "off");
    box.appendChild(chips);
    box.appendChild(personInput);
    var hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = "credit_name";
    hidden.value = "";
    peopleField.appendChild(peopleSpan);
    peopleField.appendChild(box);
    peopleField.appendChild(hidden);
    row.appendChild(peopleField);

    // 删除
    var del = document.createElement("button");
    del.type = "button";
    del.className = "pair-del";
    del.setAttribute("data-pair-del", "credit");
    del.title = "删除这一条";
    del.setAttribute("aria-label", "删除这一条");
    del.textContent = "✕";
    row.appendChild(del);
    return row;
  }

  function buildPairRow(kind) {
    // 词条用专门的结构(带人员输入框)
    if (kind === "credit") { return buildCreditRow(); }
    var conf = PAIR_KINDS[kind];
    if (!conf) { return null; }
    var row = document.createElement("div");
    row.className = "pair-row";
    row.setAttribute("data-pair-row", kind);
    conf.fields.forEach(function (field) {
      var label = document.createElement("label");
      label.className = "field auth-field";
      var span = document.createElement("span");
      span.textContent = field[1];
      var input = document.createElement("input");
      input.type = "text";
      input.name = field[0];
      input.placeholder = field[2] || "";
      label.appendChild(span);
      label.appendChild(input);
      row.appendChild(label);
    });
    var del = document.createElement("button");
    del.type = "button";
    del.className = "pair-del";
    del.setAttribute("data-pair-del", kind);
    del.title = "删除这一条";
    del.setAttribute("aria-label", "删除这一条");
    del.textContent = "✕";
    row.appendChild(del);
    return row;
  }

  function setupPairRows() {
    document.addEventListener("click", function (e) {
      var target = e.target;
      if (!target || !target.closest) { return; }

      // ＋ 添加
      var addBtn = target.closest("[data-pair-add]");
      if (addBtn) {
        e.preventDefault();
        var kind = addBtn.getAttribute("data-pair-add");
        // 容器查找顺序:
        //   ① 最近的 .rows(模板里词条/外链都包在 <div class="rows"> 里)
        //   ② 添加按钮所在 .pair-add 的前一个兄弟
        //   ③ 同 section 里第一个 .rows
        var host = addBtn.closest(".pair-add");
        var container = addBtn.closest(".rows");
        if (!container && host && host.previousElementSibling) {
          var prev = host.previousElementSibling;
          container = prev.classList && prev.classList.contains("rows")
            ? prev : prev;
        }
        if (!container) {
          var section = addBtn.closest("section");
          container = section ? section.querySelector(".rows") : null;
        }
        if (!container) { return; }
        var newRow = buildPairRow(kind);
        if (newRow) {
          container.appendChild(newRow);
          // 焦点给**人员输入框**(用户最常填的是人), 没有就给第一个输入框
          var personInput = newRow.querySelector(".credit-person-input");
          var first = personInput || newRow.querySelector("input");
          if (first) { first.focus(); }
        }
        return;
      }

      // ✕ 删除
      var delBtn = target.closest("[data-pair-del]");
      if (delBtn) {
        e.preventDefault();
        var row = delBtn.closest(".pair-row");
        if (row && row.parentNode) { row.parentNode.removeChild(row); }
        return;
      }
    });

    /* 提交前: 处理空行.
     *
     * 语义约定(与后端 parse_album_form 对齐):
     *   - 字段名**完全不存在** -> 视为"没改动", 保留库里原值
     *   - 字段名**存在但全空**   -> 视为"用户清空了", 写成空列表
     *
     * 所以每类(credit / url)必须**恰好保留一行**空字段带 name 作为"清空信号";
     * 其余空行去掉 name(否则会被后端当成有效行)。全都有值时则不需要信号行。
     */
    var form = document.querySelector(".edit-form");
    if (form) {
      form.addEventListener("submit", function () {
        ["credit", "url"].forEach(function (kind) {
          var rows = Array.prototype.slice.call(
            form.querySelectorAll('.pair-row[data-pair-row="' + kind + '"]'));
          var empties = [];
          var filled = 0;
          rows.forEach(function (row) {
            var inputs = row.querySelectorAll("input[type='text']");
            var hasValue = false;
            Array.prototype.forEach.call(inputs, function (input) {
              if (input.value.trim()) { hasValue = true; }
            });
            if (hasValue) {
              filled += 1;
            } else {
              empties.push({ row: row, inputs: inputs });
            }
          });
          // 有值才需要保留信号行: 全都有值 -> 空行全部去掉 name;
          // 否则保留**第一行**空行作为信号, 其余去掉 name。
          empties.forEach(function (item, index) {
            var keepAsSignal = (filled === 0 && index === 0);
            if (keepAsSignal) { return; }     // 保留 name, 表示"清空"
            Array.prototype.forEach.call(item.inputs, function (input) {
              input.removeAttribute("name");
            });
          });
        });
      });
    }
  }

  /* 选展会后自动填发售日: 只有发售日为空/与上次自动填的一致时才覆盖,
     避免把编辑者手填的日期冲掉。
     注意: `_bind()` 渲染的是**可见输入框** `.bind-input[data-name=event]`,
     真正提交的是隐藏的 `input[name=event]` —— 联动要读可见框的值。 */
  function setupEventAutoDate() {
    var eventInput = document.querySelector('.bind-input[data-name="event"]')
      || document.querySelector('input[name="event"]');
    var dateInput = document.querySelector('input[name="release_date"]');
    if (!eventInput || !dateInput) { return; }
    var lastAuto = "";

    function ask() {
      var name = (eventInput.value || "").trim();
      if (!name) { return; }
      if (typeof window.fetch !== "function") { return; }
      window.fetch("/event-date?name=" + encodeURIComponent(name),
                   { headers: { "Accept": "application/json" } })
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          if (!data || !data.ok || !data.date) { return; }
          var current = (dateInput.value || "").trim();
          // 空, 或仍是上次自动填的值 -> 更新; 否则不碰
          if (!current || current === lastAuto) {
            dateInput.value = data.date;
            lastAuto = data.date;
          }
        })
        .catch(function () { /* 静默: 自动填充失败不影响编辑 */ });
    }

    // 展会输入框变化(打字/选择)后问一次; 加防抖避免每敲一个字都请求
    var timer = null;
    function askSoon() {
      if (timer) { window.clearTimeout(timer); }
      timer = window.setTimeout(ask, 400);
    }
    eventInput.addEventListener("change", ask);
    eventInput.addEventListener("blur", ask);
    eventInput.addEventListener("input", askSoon);
    // 页面上如果有"选展会"的候选项(点击填充), 也触发一次
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) { return; }
      if (t.closest("[data-bind-option]") || t.closest(".bind-option")) {
        window.setTimeout(ask, 150);
      }
    });
  }

  /* ==========================================================================
     详情页曲目表: 点击曲目展开补充信息(语种/曲风/副标题/翻译)
     默认收起, 只在有补充信息时才有展开按钮; 纯 DOM 操作, 无需刷新。
     ========================================================================== */
  function setupTrackExpand() {
    document.addEventListener("click", function (e) {
      var target = e.target;
      if (!target || !target.closest) { return; }
      var row = target.closest('tr[data-track-expand="1"]');
      if (!row) { return; }
      var next = row.nextElementSibling;
      if (!next || !next.classList.contains("track-extra-row")) { return; }
      var opened = !next.hidden;
      next.hidden = opened;
      row.classList.toggle("is-open", !opened);
    });
  }

  function setupTrackRows() {
    document.addEventListener("click", function (e) {
      var target = e.target;
      if (!target || !target.closest) { return; }

      // ＋ 添加一首(加到该碟末尾)
      var addOne = target.closest('[data-track-add="1"]');
      if (addOne) {
        e.preventDefault();
        var block = addOne.closest(".disc-block");
        if (!block) { return; }
        var body = discBody(block);
        if (!body) { return; }
        var count = body.querySelectorAll("tr.track-row").length;
        body.appendChild(buildTrackRow(discNumber(block), count + 1));
        renumberDisc(block);
        focusTitle(block);
        return;
      }

      // 批量添加 10 首(同一碟)

      // ＋ 添加光盘: 新建一碟(空)
      var addDisc = target.closest('[data-disc-add="1"]');
      if (addDisc) {
        e.preventDefault();
        addDiscBlock(lastDiscNumber() + 1);
        return;
      }

      // 删除整张碟
      var removeDisc = target.closest('[data-disc-remove="1"]');
      if (removeDisc) {
        e.preventDefault();
        var rmBlock = removeDisc.closest(".disc-block");
        if (!rmBlock) { return; }
        if (document.querySelectorAll(".disc-block").length <= 1) {
          return;  // 至少留一碟
        }
        var rowsInDisc = discBody(rmBlock)
          ? discBody(rmBlock).querySelectorAll("tr.track-row").length : 0;
        if (rowsInDisc && !window.confirm(
            "这碟还有 " + rowsInDisc + " 首歌, 确定删除整张碟?")) {
          return;
        }
        if (rmBlock.parentNode) { rmBlock.parentNode.removeChild(rmBlock); }
        return;
      }

      // 调整曲轨符(按当前顺序重排)
      var renumber = target.closest('[data-disc-renumber="1"]');
      if (renumber) {
        e.preventDefault();
        var rnBlock = renumber.closest(".disc-block");
        if (rnBlock) { renumberDisc(rnBlock); }
        return;
      }

      // 删除单行
      var delOne = target.closest("[data-track-del]");
      if (delOne) {
        e.preventDefault();
        var row = delOne.closest("tr.track-row");
        var parentBlock = delOne.closest(".disc-block");
        if (row && row.parentNode) {
          row.parentNode.removeChild(row);
          if (parentBlock) { renumberDisc(parentBlock); }
        }
        return;
      }

    });

    /* 拖动排序: 按住「⠿」把手拖动整行; 只能在同一碟内移动(跨碟会破坏分碟语义)。
       用 HTML5 drag&drop, 不引入第三方库。 */
    var dragRow = null;
    var dragDisc = null;

    document.addEventListener("dragstart", function (e) {
      var handle = e.target && e.target.closest
        ? e.target.closest("[data-track-drag]") : null;
      if (!handle) { return; }
      var row = handle.closest("tr.track-row");
      if (!row) { return; }
      dragRow = row;
      dragDisc = handle.closest(".disc-block");
      row.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        // Firefox 要求 setData 才启动拖拽
        try { e.dataTransfer.setData("text/plain", "track"); } catch (err) {}
      }
    });

    document.addEventListener("dragover", function (e) {
      if (!dragRow) { return; }
      var over = e.target && e.target.closest
        ? e.target.closest("tr.track-row") : null;
      if (!over || over === dragRow) { return; }
      // 只允许同一张碟内拖动
      if (over.closest(".disc-block") !== dragDisc) { return; }
      e.preventDefault();
      if (e.dataTransfer) { e.dataTransfer.dropEffect = "move"; }
      over.classList.add("drag-over");
      var rect = over.getBoundingClientRect();
      var after = (e.clientY - rect.top) > rect.height / 2;
      over.parentNode.insertBefore(dragRow, after ? over.nextSibling : over);
    });

    document.addEventListener("dragleave", function (e) {
      var over = e.target && e.target.closest
        ? e.target.closest("tr.track-row") : null;
      if (over) { over.classList.remove("drag-over"); }
    });

    document.addEventListener("dragend", function () {
      if (dragRow) {
        dragRow.classList.remove("dragging");
        if (dragDisc) { renumberDisc(dragDisc); }
      }
      var marked = document.querySelectorAll("tr.track-row.drag-over");
      Array.prototype.forEach.call(marked, function (row) {
        row.classList.remove("drag-over");
      });
      dragRow = null;
      dragDisc = null;
    });

    document.addEventListener("drop", function (e) {
      if (!dragRow) { return; }
      e.preventDefault();
      var over = e.target && e.target.closest
        ? e.target.closest("tr.track-row") : null;
      if (over) { over.classList.remove("drag-over"); }
    });

    // 提交前: 去掉曲名为空的整行, 并按 DOM 顺序重排音轨号
    var form = document.querySelector(".edit-form") ||
               document.querySelector("form[enctype='multipart/form-data']");
    if (form) {
      form.addEventListener("submit", function () {
        var blocks = document.querySelectorAll(".disc-block");
        Array.prototype.forEach.call(blocks, function (block) {
          var body = discBody(block);
          if (!body) { return; }
          var discNo = discNumber(block);
          var rows = body.querySelectorAll("tr.track-row");
          var kept = 0;
          Array.prototype.forEach.call(rows, function (row) {
            var title = row.querySelector('input[name="track_title"]');
            if (!title || !title.value.trim()) {
              if (row.parentNode) { row.parentNode.removeChild(row); }
              return;
            }
            kept += 1;
            var idx = row.querySelector('input[name="track_index"]');
            if (idx) { idx.value = String(kept); }
            var discField = row.querySelector('input[name="track_disc"]');
            if (discField) { discField.value = String(discNo); }
          });
          // 整碟被删空的: 把隐藏字段也移除, 免得提交空碟
          if (!kept) {
            Array.prototype.forEach.call(
              block.querySelectorAll("input[name]"),
              function (input) { input.removeAttribute("name"); });
          }
        });
      });
    }
  }


  /* ==========================================================================
     展会系列卡片: 展开/收起会次列表
     默认只显示前 5 条(CSS max-height 限制), 点「展开」放开高度并允许滚动。
     ========================================================================== */
  function setupSeriesCards() {
    document.addEventListener("click", function (e) {
      var btn = e.target && e.target.closest
        ? e.target.closest("[data-series-more]") : null;
      if (!btn) { return; }
      e.preventDefault();
      var card = btn.closest("[data-series-card]");
      if (!card) { return; }
      var list = card.querySelector("[data-series-editions]");
      if (!list) { return; }
      var open = list.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        var hidden = Math.max(0, list.children.length - 5);
        btn.textContent = "收起";
        btn.setAttribute("data-open-count", String(hidden));
      } else {
        btn.textContent = "展开 " + (btn.getAttribute("data-open-count") || "")
          + " 条";
      }
    });
  }


  /* ==========================================================================
     报错/提示页的「回到上一页」
     没有上一页(直接打开链接、从外部跳进来)时退到首页 ——
     否则点了 history.back() 会停在原地，看起来像按钮坏了。
     ========================================================================== */
  function setupGoBack() {
    document.addEventListener("click", function (e) {
      var btn = e.target && e.target.closest
        ? e.target.closest("[data-go-back]") : null;
      if (!btn) { return; }
      e.preventDefault();
      if (window.history && window.history.length > 1) {
        window.history.back();
      } else {
        window.location.href = "/";
      }
    });
  }

  function boot() {
    setupPasswordMatch();
    setupUsernameCheck();
    setupPasswordRules();
    setupApplyDialog();
    setupTableResize();
    setupSeriesCards();
    setupGoBack();
    setupCoverUpload();
    setupTrackRows();
    setupPairRows();
    setupNoEnterSubmit();
    setupPeopleChips();
    setupCreditPeople();
    setupCoverGallery();
    setupEditDraft();
    setupDialogs();
    setupAppearance();
    setupAppearanceFab();
    setupBindFields();
    setupEventSync();
    setupExternalLinks();
    setupSubmitOnce();
    setupConfirm();
    setupTrackExpand();
    setupEventAutoDate();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();