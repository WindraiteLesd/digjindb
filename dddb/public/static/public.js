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
    /* 上方是**只读汇总**(用户要求 m11474: 只显示下方词条的效果, 不是输入区)。
       所以这里没有搜索框 / 候选点选 / × 删除 —— 唯一的编辑入口是下方词条行。
       保留的职责只有两件:
         1. 把 hidden[name=artists] 的值渲染成一排 chip (服务端已渲染, 这里兜底)
         2. 监听 hidden 的 change 重建 chip (infobox 填入 / 词条变更后刷新汇总)
    */
    var host = document.querySelector('[data-people="artists"]');
    if (!host) { return; }
    var card = host.closest(".edit-card") || document;
    var hidden = card.querySelector('input[type="hidden"][name="artists"]');

    function names() {
      var out = [];
      var chips = host.querySelectorAll(".person-chip");
      Array.prototype.forEach.call(chips, function (chip) {
        var name = chip.getAttribute("data-person");
        if (name) { out.push(name); }
      });
      return out;
    }

    function makeChip(name, role) {
      var chip = document.createElement("span");
      chip.className = "person-chip is-static";
      chip.setAttribute("data-person", name);
      chip.setAttribute("data-role", role || "");
      chip.setAttribute("data-entity-id", "0");
      var kind = document.createElement("span");
      kind.className = "person-kind";
      kind.textContent = role || "艺人";
      var bold = document.createElement("b");
      bold.textContent = name;
      chip.appendChild(kind);
      chip.appendChild(bold);
      return chip;
    }

    /* 用 hidden 里的名字重建 chip; 已有 chip 保留其 data-role(职责由服务端合并) */
    function readHidden() {
      if (!hidden) { return; }
      var wanted = [];
      (hidden.value || "").split(/[\n、]+/).forEach(function (part) {
        var one = part.trim();
        if (one && wanted.indexOf(one) === -1) { wanted.push(one); }
      });
      var existing = {};
      Array.prototype.forEach.call(
        host.querySelectorAll(".person-chip"), function (chip) {
          existing[chip.getAttribute("data-person") || ""] = chip;
        });
      // 删掉不再需要的
      Object.keys(existing).forEach(function (name) {
        if (wanted.indexOf(name) === -1) { existing[name].remove(); }
      });
      // 补上缺的(已有职责的 chip 保持原样)
      wanted.forEach(function (one) {
        if (!existing[one]) { host.appendChild(makeChip(one, "")); }
      });
    }

    if (hidden) { hidden.addEventListener("change", readHidden); }
    readHidden();
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
      if (!src) { return; }
      // thumbImg 可能在初始化时还不存在, 或被替换后引用失效 ——
      // 这里重新查一次, 不然上传成功后主图永远不更新。
      if (!thumbImg || !thumbImg.parentNode) {
        thumbImg = document.querySelector(".edit-cover-img");
      }
      if (!thumbImg) { return; }
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
          // 封面地址由服务端给(data-cover-src), 这里**不再反推** ——
          // 以前从 data-cover-next 拼, 格式一变就得到 /cover/?v=... 图裂,
          // 看起来像"上传了但没显示"。
          var base = form.getAttribute("data-cover-src") || "";
          if (!base) {
            // 兜底: 从 next 里取 /album/<key>/edit 的 key
            var m = (form.getAttribute("data-cover-next") || "")
              .match(/\/album\/(.+?)\/edit/);
            base = m ? "/cover/" + m[1] : "";
          }
          if (!base) { resolve(null); return; }
          resolve(base + (base.indexOf("?") >= 0 ? "&" : "?") +
                  "v=" + Date.now());
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
              // thumbImg 可能是 null(初始化时页面上没有 .edit-cover-img),
              // 直接 .getAttribute 会抛异常 —— 而异常发生在 addSlot
              // **已经加完槽位之后**, 于是"槽位有了、主图没变"。
              var hasPrimary = !!(thumbImg &&
                                  thumbImg.getAttribute("data-zoom-src"));
              addSlot(src, file.name, i === 0 && !hasPrimary);
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
        // **确定性兜底**: 数据已经写进库了, 只要主图没更新就直接刷新。
        // 不依赖 setPrimary 的任何前端状态 —— 刷新后服务端一定渲染新图。
        window.setTimeout(function () {
          var main = document.querySelector(".edit-cover-img");
          var ok = main && main.tagName === "IMG" &&
                   main.getAttribute("src") &&
                   !main.classList.contains("is-empty");
          if (!ok) {
            var next = form.getAttribute("data-cover-next") ||
                       window.location.pathname;
            var sep = next.indexOf("?") >= 0 ? "&" : "?";
            // 用户 m16568: 跳走之前先把草稿落盘, 否则刷新后丢进度
            if (typeof window.dddbFlushDraft === "function") {
              window.dddbFlushDraft();
            }
            window.location.href = next + sep + "t=" + Date.now();
          }
        }, 400);
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
  /* 提交之后, 草稿什么时候才算"用掉了"?
     只有**真的落地到了别的页面**(详情页 / 新建成功的 ?created=1)才算 —— 那时
     页面上没有 form.edit-form 了。若还停在编辑表单上, 说明保存失败或校验没过,
     草稿必须原样留着(用户 m13494: 500 退回后草稿丢了, 也没法恢复)。 */
  function settlePendingDraft() {
    if (typeof window.localStorage === "undefined") { return; }
    var KEY = "dddb:edit-draft";
    var PENDING = KEY + ":pending";
    var pending = null;
    try { pending = window.localStorage.getItem(PENDING); } catch (err) { return; }
    if (!pending) { return; }
    /* 还在编辑表单上 -> 没走成, 清掉标记(草稿留着, 下次照样能恢复) */
    if (document.querySelector("form.edit-form")) {
      try { window.localStorage.removeItem(PENDING); } catch (err) { /* 忽略 */ }
      return;
    }
    var path = window.location.pathname || "";
    var query = window.location.search || "";
    var landed = query.indexOf("created=1") >= 0 || /^\/album\/[^/]+$/.test(path);
    try {
      window.localStorage.removeItem(PENDING);
      if (landed) { window.localStorage.removeItem(KEY); }
    } catch (err) { /* 忽略 */ }
  }

  function setupEditDraft() {
    var form = document.querySelector("form.edit-form");
    var bar = document.querySelector("[data-draft-bar] .draft-status");
    if (!form || typeof window.localStorage === "undefined") { return; }
    var KEY = "dddb:edit-draft";
    /* 提交后"待确认"标记: 见 form 的 submit 监听与 settlePendingDraft() */
    var PENDING_KEY = KEY + ":pending";
    var action = form.getAttribute("action") || "";
    var timer = null;

    function collect() {
      var data = {};
      var els = form.querySelectorAll("input[name], textarea[name], select[name]");
      Array.prototype.forEach.call(els, function (el) {
        var name = el.getAttribute("name");
        if (!name || name === "csrf_token") { return; }
        /* cover_data 由 extra 槽单独收 —— 若也从这里收, 同一份 base64
           会在草稿里存两遍。1MB 的图 data URL 约 1.37M 字符, 两份按
           UTF-16 算就超过 localStorage 的 5MB 配额, setItem 直接抛异常,
           结果整份草稿都存不下来(用户 m12486: 还是不行)。 */
        if (name === "cover_data") { return; }
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

    /* 有些状态挂在没有 name 的元素上(典型是封面预览的 data URL ——
       给它 name 会让表单白传一份最大 2MB 的 base64)。草稿的 extra 槽
       专门收这类值, 避免「草稿不存储封面」(用户 m12280)。 */
    function collectExtra() {
      var extra = {};
      var cover = document.getElementById("cover-data");
      if (cover && cover.value) { extra.cover_data = cover.value; }
      /* infobox 缓存封面的 token 也进草稿(用户 m13899): 刷新/报错回来之后
         它还在, 提交时服务端才能把那张缓存图转正。 */
      var cached = document.getElementById("cover-cache");
      if (cached && cached.value) { extra.cover_cache = cached.value; }
      return extra;
    }

    function applyExtra(extra) {
      if (!extra) { return; }
      var cached = document.getElementById("cover-cache");
      if (cached && extra.cover_cache) {
        cached.value = String(extra.cover_cache);
        var img = document.querySelector(".edit-cover-img img, .edit-cover-thumb img");
        if (img) {
          img.src = "/cover-cache/" + String(extra.cover_cache);
          img.hidden = false;
        }
        var plus = document.querySelector(".cover-add-thumb .cover-plus");
        if (plus) { plus.style.display = "none"; }
      }
      var cover = document.getElementById("cover-data");
      if (!cover || !extra.cover_data) { return; }
      cover.value = String(extra.cover_data);
      /* 派发 change: setupCoverPick 监听它把图贴回 .cover-preview-img */
      cover.dispatchEvent(new Event("change", { bubbles: true }));
    }

    /* 把一份草稿(或历史版本)灌回表单。抽成函数是因为「恢复草稿」按钮和
       「草稿历史」面板都要用 —— 两处各写一遍的话, 行的重建逻辑迟早走偏。
       顺序很重要: 先把重复行(词条/外链/曲目/碟)补齐或裁掉, 再逐字段写值,
       否则草稿里第 3 条词条在页面上只有 2 行时无处可放(用户 m11689)。 */
    function applyDraft(saved, note) {
        var values = (saved && saved.values) || {};
        var listOf = function (name) {
          var raw = values[name];
          if (raw === undefined || raw === null) { return []; }
          return Array.isArray(raw) ? raw : [raw];
        };
        /* 恢复草稿必须先把**重复行**补齐/裁掉, 否则草稿里第 3 条词条
           在页面上只有 2 行时无处可放(用户 m11689: 字段不全)。 */
        var pairRows = function (kind) {
          return Array.prototype.slice.call(
            form.querySelectorAll('[data-pair-row="' + kind + '"]'));
        };
        var fitPairRows = function (kind, need) {
          if (!need) { return; }
          var rows = pairRows(kind);
          var guard = 0;
          while (rows.length < need && guard < 80) {
            var add = form.querySelector('[data-pair-add="' + kind + '"]');
            if (!add) { break; }
            add.click();
            var after = pairRows(kind);
            if (after.length <= rows.length) { break; }
            rows = after;
            guard += 1;
          }
          while (rows.length > need) {
            var last = rows[rows.length - 1];
            if (!last || !last.parentNode) { break; }
            last.parentNode.removeChild(last);
            rows = pairRows(kind);
          }
        };
        var discBlocks = function () {
          return Array.prototype.slice.call(
            form.querySelectorAll(".disc-block"));
        };
        var fitDiscs = function (need) {
          var blocks = discBlocks();
          var guard = 0;
          while (blocks.length < need && guard < 20) {
            var addDisc = form.querySelector('[data-disc-add="1"]');
            if (!addDisc) { break; }
            addDisc.click();
            var after = discBlocks();
            if (after.length <= blocks.length) { break; }
            blocks = after;
            guard += 1;
          }
          while (blocks.length > need) {
            var last = blocks[blocks.length - 1];
            if (!last || !last.parentNode) { break; }
            last.parentNode.removeChild(last);
            blocks = discBlocks();
          }
          return blocks;
        };
        var fitTrackRows = function (block, need) {
          if (!block) { return; }
          var body = block.querySelector("tbody") || block;
          var rows = Array.prototype.slice.call(
            body.querySelectorAll("tr.track-row"));
          var addOne = block.querySelector('[data-track-add="1"]');
          var guard = 0;
          while (rows.length < need && addOne && guard < 200) {
            addOne.click();
            var after = Array.prototype.slice.call(
              body.querySelectorAll("tr.track-row"));
            if (after.length <= rows.length) { break; }
            rows = after;
            guard += 1;
          }
          while (rows.length > need) {
            var last = rows[rows.length - 1];
            if (!last || !last.parentNode) { break; }
            last.parentNode.removeChild(last);
            rows = Array.prototype.slice.call(
              body.querySelectorAll("tr.track-row"));
          }
        };

        fitPairRows("credit", Math.max(listOf("credit_role").length,
                                      listOf("credit_name").length));
        fitPairRows("url", Math.max(listOf("url_key").length,
                                    listOf("url_value").length));
        // 曲目: track_disc 决定分碟, 每碟的行数按草稿里该碟的条数来
        var discs = [];
        var perDisc = {};
        listOf("track_disc").forEach(function (one) {
          var d = String(one || "1");
          if (!Object.prototype.hasOwnProperty.call(perDisc, d)) {
            discs.push(d);
            perDisc[d] = 0;
          }
          perDisc[d] += 1;
        });
        if (discs.length) {
          var blocks = fitDiscs(discs.length);
          blocks.forEach(function (block, i) {
            fitTrackRows(block, perDisc[discs[i]] || 0);
          });
        }

        Object.keys(values).forEach(function (name) {
          var list = listOf(name);
          var els = form.querySelectorAll('[name="' + name + '"]');
          Array.prototype.forEach.call(els, function (el, i) {
            if (el.type === "file") { return; }
            if (el.type === "radio" || el.type === "checkbox") {
              // 草稿只记选中的项 -> 按值比对还原勾选状态
              el.checked = list.indexOf(el.value) !== -1;
            } else if (i < list.length) {
              el.value = list[i];
            } else {
              return;
            }
            // input 让字数统计/职责汇总跟着变; change 让 chip 类控件
            // (setupBindFields / setupPeopleChips) 重建隐藏域对应的 chip
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
          });
        });
        bar.textContent = note || "草稿已恢复";
        applyExtra(saved.extra);
    }

    var lastSaved = "";
    /* 页面刚加载时 setupBindFields / setupPeopleChips 等会重建 chip、
       改写隐藏域 —— 这些 DOM 变更会被下面的 MutationObserver 看到。
       如果照着「变更就存」, 载入 1.2 秒后就会用**页面原值**把上一份
       草稿整个覆盖掉, 表现就是「刷新后草稿没了」(用户 m12045)。
       所以只有用户真的动过表单(dirty)才允许写盘。 */
    var dirty = false;

    function snapshotOf(values, extra) {
      try {
        return JSON.stringify({ action: action, values: values, extra: extra });
      } catch (err) {
        return "";
      }
    }

    /* ---- 草稿版本历史(用户 m13494) ------------------------------------
       每次草稿内容真的变了就留一份快照, 最多 HISTORY_MAX 份。用途很具体:
       报错回到编辑页时草稿可能被后续操作覆盖掉, 有个历史就能"回到某个时段"。
       与 KEY 分开存, 所以历史本身不会被"覆盖草稿"的动作抹掉。 */
    var HISTORY_KEY = KEY + ":history";
    var HISTORY_MAX = 20;

    function draftFingerprint(values, extra) {
      try { return JSON.stringify([values, extra]); } catch (err) { return ""; }
    }

    function readHistory() {
      try {
        var rows = window.localStorage.getItem(HISTORY_KEY);
        var list = rows ? JSON.parse(rows) : [];
        if (!Array.isArray(list)) { return []; }
        return list.filter(function (one) {
          return one && one.values && one.action === action;
        });
      } catch (err) { return []; }
    }

    function writeHistory(list) {
      try {
        window.localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
        return true;
      } catch (err) { return false; }
    }

    function pushHistory(values, extra) {
      var stamp = draftFingerprint(values, extra);
      if (!stamp) { return; }
      var list = readHistory();
      if (list.length && list[0].stamp === stamp) { return; }
      list.unshift({
        at: Date.now(), action: action, stamp: stamp,
        values: values, extra: extra
      });
      /* 配额有限: 攒到 MAX 就丢最老的; 仍写不进去就再砍一半重试一次。 */
      while (list.length > HISTORY_MAX) { list.pop(); }
      if (!writeHistory(list) && list.length > 4) {
        writeHistory(list.slice(0, Math.ceil(list.length / 2)));
      }
    }

    function formatWhen(at) {
      try {
        return new Date(at).toLocaleString();
      } catch (err) { return ""; }
    }

    function save(force) {
      /* force: 用户点了「保存草稿」按钮 —— 那是明确意图, 不受 dirty 闸门限制
         (比如刚打开页面什么都没改, 也想把当前内容留一份)。 */
      if (!force && !dirty) { return; }
      var values, extra;
      try {
        values = collect();
        extra = collectExtra();
      } catch (err) { return; }
      /* 指纹只取「值」: 之前误用 payload.slice(0, indexOf(',"at":')),
         而 JSON 键序是 action / at / values, 切出来只剩 action ——
         第一次保存之后指纹永远相同, 后续修改全被跳过(m12045)。 */
      var stamp = snapshotOf(values, extra);
      if (!force && (!stamp || stamp === lastSaved)) { return; }
      lastSaved = stamp;
      dirty = false;
      pushHistory(values, extra);
      try {
        window.localStorage.setItem(KEY, JSON.stringify({
          action: action, at: Date.now(), values: values, extra: extra
        }));
        if (bar) {
          bar.textContent = "草稿已保存 " + new Date().toLocaleTimeString();
        }
        return true;
      } catch (err) {
        /* 隐私模式 / 超限: 静默 */
        return false;
      }
    }

    function scheduleSave() {
      if (timer) { window.clearTimeout(timer); }
      timer = window.setTimeout(save, 1200);
    }

    /* 只有**用户真的改过**才允许写盘: 载入时 setupBindFields /
       setupPeopleChips 会重建 chip、改写隐藏域, 那些 DOM 变更同样会命中
       MutationObserver。若照着"变更就存", 1.2 秒后就会用页面原值把上一份
       草稿覆盖掉 —— 表现正是「刷新后草稿丢失」(用户 m12045)。 */
    function markDirty() {
      dirty = true;
      scheduleSave();
    }

    function onMutation() {
      if (dirty) { scheduleSave(); }
    }

    form.addEventListener("input", markDirty);
    form.addEventListener("change", markDirty);
    /* 行增删 / chip 增删都不派发 input|change(它们直接改 DOM 与隐藏域),
       只监听这两个事件的话「点了＋添加词条 / ＋添加歌曲」之后的内容根本没进草稿
       (用户 m11689: 草稿保存的字段并不全)。所以再加 click/keydown/drop 兜住。 */
    form.addEventListener("click", markDirty);
    form.addEventListener("keydown", markDirty);
    form.addEventListener("drop", markDirty);
    if (typeof window.MutationObserver === "function") {
      new window.MutationObserver(onMutation).observe(form, {
        childList: true, subtree: true
      });
    }
    /* 防抖 1.2 秒 + 打完字立刻刷新 = 草稿还没落地。离开页面前补一次同步写盘。
       注意要包一层调用: 直接把 save 交给 addEventListener 会把 Event 当成
       force 传进去, 于是每次离开页面都无条件写盘(没改过也写)。 */
    window.addEventListener("beforeunload", function () { save(false); });
    /* 用户 m16568: 改封面之后页面会整页刷新(服务端要重新渲染新图),
       刷新前必须把草稿**同步写盘**, 否则 1.2 秒防抖还没落地就跳走了,
       回来看到的是旧草稿(或者根本没有)。上传封面的两条路径都会先调它。
       同时留一个"回来自动恢复"的标记 —— 用户明确要求
       「要么不刷新, 要么刷新后自动恢复最新进度」, 不能只弹个"恢复草稿"按钮。 */
    var RESUME_KEY = KEY + ":resume";
    window.dddbFlushDraft = function () {
      try { save(false); } catch (err) { /* 忽略 */ }
      try { window.localStorage.setItem(RESUME_KEY, "1"); } catch (err) { /* 忽略 */ }
    };
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") { save(false); }
    });
    /* 真正提交了就不该再留着草稿: 保存成功会跳转, 失败也会带着提交值
       重新渲染这一页(那次加载会因为"草稿与页面一致"自动清掉)。 */
    /* 提交时**不能**直接删草稿(用户 m13494: 报错退回编辑页后草稿没了,
       而且没有恢复入口)。这里只留一个"待确认"标记, 等真的落地到详情页
       (页面上没有编辑表单了) 才清 —— 见 settlePendingDraft()。 */
    form.addEventListener("submit", function () {
      try {
        window.localStorage.setItem(PENDING_KEY, JSON.stringify({
          action: action, at: Date.now()
        }));
      } catch (err) { /* 隐私模式 */ }
    });

    /* 手动「保存草稿」: 不必等 1.2 秒防抖, 也不用先改过东西。 */
    var saveBtn = document.querySelector("[data-draft-save]");
    if (saveBtn) {
      saveBtn.addEventListener("click", function (e) {
        e.preventDefault();
        if (timer) { window.clearTimeout(timer); timer = null; }
        if (!save(true) && bar) { bar.textContent = "保存草稿失败(浏览器不允许本地存储)"; }
      });
    }

    /* ---- 草稿历史面板 ---------------------------------------------------
       用户 m13494: "建立历史草稿版本记录功能, 可恢复到某个时段草稿版本"。
       历史由 pushHistory() 在每次存草稿时留底, 这里只负责把它列出来。 */
    function buildHistoryPanel() {
      var btn = document.querySelector("[data-draft-history]");
      if (!btn) { return; }
      var panel = document.createElement("div");
      panel.className = "draft-history-panel";
      panel.hidden = true;

      function render() {
        var list = readHistory();
        panel.textContent = "";
        var head = document.createElement("div");
        head.className = "draft-history-title";
        head.textContent = list.length
          ? "草稿历史（选一份恢复，共 " + list.length + " 份）"
          : "还没有历史版本";
        panel.appendChild(head);
        list.forEach(function (entry, index) {
          var row = document.createElement("div");
          row.className = "draft-history-row";
          var when = document.createElement("span");
          when.className = "draft-history-when";
          when.textContent = formatWhen(entry.at);
          row.appendChild(when);

          var back = document.createElement("button");
          back.type = "button";
          back.className = "btn-ghost btn-xs";
          back.textContent = "恢复";
          back.addEventListener("click", function (e) {
            e.preventDefault();
            applyDraft(entry, "已恢复到 " + formatWhen(entry.at));
            panel.hidden = true;
          });
          row.appendChild(back);

          var drop = document.createElement("button");
          drop.type = "button";
          drop.className = "btn-ghost btn-xs";
          drop.textContent = "删除";
          drop.addEventListener("click", function (e) {
            e.preventDefault();
            var rest = readHistory().filter(function (_, i) { return i !== index; });
            if (writeHistory(rest)) { render(); }
          });
          row.appendChild(drop);
          panel.appendChild(row);
        });
        var hint = document.createElement("p");
        hint.className = "field-hint";
        hint.textContent = "历史版本存在本机浏览器里，最多保留最近 " +
                           HISTORY_MAX + " 份。";
        panel.appendChild(hint);
      }

      btn.addEventListener("click", function (e) {
        e.preventDefault();
        render();
        panel.hidden = !panel.hidden;
      });
      if (bar && bar.parentNode) { bar.parentNode.appendChild(panel); }
      else { btn.parentNode.appendChild(panel); }
    }
    buildHistoryPanel();

    // 有草稿就提示恢复(不自动套用)
    try {
      var raw = window.localStorage.getItem(KEY);
      if (!raw) { return; }
      var saved = JSON.parse(raw);
      if (!saved || saved.action !== action || !saved.values) { return; }
      if (!bar) { return; }
      /* 草稿和页面当前值一样时不必提示 —— 通常是刚保存完又回到编辑页,
         提示了反而让人以为有没提交的改动。 */
      var same = false;
      try {
        same = snapshotOf(collect(), collectExtra()) ===
               snapshotOf(saved.values, saved.extra);
      } catch (err) { same = false; }
      if (same) {
        try { window.localStorage.removeItem(KEY); } catch (err2) { /* 忽略 */ }
        bar.textContent = "编辑内容会自动存草稿";
        return;
      }
      /* 用户 m16568: 页面是因为「刚传完封面刷新」而重载的(resume 标记在),
         这时候不要等用户点按钮 —— 直接恢复, 否则改封面前填的东西看着像丢了。 */
      var resume = false;
      try {
        resume = window.localStorage.getItem(RESUME_KEY) === "1";
        if (resume) { window.localStorage.removeItem(RESUME_KEY); }
      } catch (err3) { resume = false; }
      if (resume) {
        applyDraft(saved, "已自动恢复刷新前的编辑内容");
        bar.textContent = "已自动恢复刷新前的编辑内容";
        return;
      }
      bar.textContent = "发现 " + new Date(saved.at).toLocaleTimeString() +
                        " 的草稿";
      var restore = document.createElement("button");
      restore.type = "button";
      restore.className = "btn-ghost btn-xs";
      restore.textContent = "恢复草稿";
      restore.addEventListener("click", function (e) {
        e.preventDefault();
        applyDraft(saved, "草稿已恢复");
        if (restore.parentNode) { restore.parentNode.removeChild(restore); }
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
    /* 词条行是**唯一**的人员编辑入口(用户要求 m11474)。
       上方那排 chip 只是"下方词条合并后的效果" —— 每次词条变动都重建它,
       所以上下永远同步, 而上方不能添加/删除任何人。
    */
    var card = document.querySelector('[data-card="credits"]');
    if (!card) { return; }
    var host = card.querySelector('[data-people="artists"]');
    if (!host) { return; }
    var artistsHidden = card.querySelector(
      'input[type="hidden"][name="artists"]');

    /* 词条里出现的人(按行序去重) */
    function creditPeople() {
      var out = [];
      Array.prototype.forEach.call(
        card.querySelectorAll(".credit-row"), function (row) {
          Array.prototype.forEach.call(
            row.querySelectorAll(".credit-person-chip"), function (one) {
              var n = one.getAttribute("data-name");
              if (n && out.indexOf(n) === -1) { out.push(n); }
            });
        });
      return out;
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
    }

    /* 从基础名单里摘掉一个人(只改属性, 不动已渲染的 chip) */
    function dropFromBase(name) {
      var keep = (host.getAttribute("data-artists") || "")
        .split(/[\n、]+/)
        .map(function (one) { return one.trim(); })
        .filter(function (one) { return !!one && one !== name; });
      host.setAttribute("data-artists", keep.join("\n"));
    }

    /* 重建上方只读汇总: 与详情页同源算法
         - 先放原 artists(职责空 -> 显示「艺人」)
         - 再遍历词条补职责, 同一人只有一条
         (服务端已按同规则渲染, 这里只做前端即时同步)
    */
    function rebuildSummary() {
      var base = (host.getAttribute("data-artists") || "")
        .split(/[\n、]+/)
        .map(function (one) { return one.trim(); })
        .filter(function (one) { return !!one; });
      var order = base.slice();
      var roles = {};
      Array.prototype.forEach.call(
        card.querySelectorAll(".credit-row"), function (row) {
          var roleInput = row.querySelector('input[name="credit_role"]');
          var role = roleInput ? roleInput.value.trim() : "";
          valuesOf(row).forEach(function (name) {
            if (order.indexOf(name) === -1) { order.push(name); }
            /* 一个人挂多个职责时**收齐**(用 / 连接), 与服务端渲染一致
               —— 只留第一条会漏掉其他职责(用户 m12697)。 */
            if (!role) { return; }
            var list = roles[name] || (roles[name] = []);
            if (list.indexOf(role) === -1) { list.push(role); }
          });
        });
      host.textContent = "";
      /* 职责代码 -> 显示名: 服务端把 ROLE_LABELS 以 JSON 塞在
         data-role-labels 上。词条框里是 `arrange` 这种原始代码, 服务端
         渲染成「编曲」; 前端不查表就会显示成 `arrange`, 一次交互后变样。 */
      var labelMap = {};
      try {
        labelMap = JSON.parse(host.getAttribute("data-role-labels") || "{}")
          || {};
      } catch (err) { labelMap = {}; }
      function roleLabelOf(role) {
        var code = String(role || "").trim();
        if (!code) { return ""; }
        return String(labelMap[code] || labelMap[code.toLowerCase()] || code);
      }

      order.forEach(function (name) {
        var chips = roles[name] || [];
        var roleText = chips.join("/");
        var shown = chips.map(roleLabelOf).filter(Boolean).join("/");
        var chip = document.createElement("span");
        chip.className = "person-chip is-static";
        chip.setAttribute("data-person", name);
        chip.setAttribute("data-role", roleText);
        chip.setAttribute("data-entity-id", "0");
        var kind = document.createElement("span");
        kind.className = "person-kind";
        kind.textContent = shown || "艺人";
        var bold = document.createElement("b");
        bold.textContent = name;
        chip.appendChild(kind);
        chip.appendChild(bold);
        host.appendChild(chip);
      });
      // artists 隐藏域 = 汇总里出现的**全部**人(词条 + 原有 artists)
      if (artistsHidden) { artistsHidden.value = order.join("\n"); }
    }

    /* 只造一个行内 chip, 不碰隐藏域(隐藏域已经是真相时用) */
    function addChipOnly(row, name) {
      var value = String(name || "").trim();
      if (!row || !value) { return false; }
      if (valuesOf(row).indexOf(value) !== -1) { return true; }
      var box = row.querySelector(".credit-people-chips");
      if (!box) { return false; }
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
      return true;
    }

    /* 行内加一个人 */
    function addPersonToRow(row, name) {
      var value = String(name || "").trim();
      if (!value) { return false; }
      if (valuesOf(row).indexOf(value) !== -1) { writeValues(row); return true; }
      addChipOnly(row, value);
      writeValues(row);
      rebuildSummary();
      return true;
    }

    /* ---- 回车加入人员 ---- */
    card.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.keyCode !== 13) { return; }
      var input = e.target;
      if (!input || !input.classList) { return; }
      if (!input.classList.contains("credit-person-input")) { return; }
      e.preventDefault();                 // 不提交表单
      var row = input.closest(".credit-row");
      if (!row) { return; }
      var raw = String(input.value || "").trim();
      if (!raw) { return; }
      // 支持一次敲多个(用、或,分隔)
      raw.split(/[、,，]/).forEach(function (one) {
        var name = one.trim();
        if (name) { addPersonToRow(row, name); }
      });
      input.value = "";
      closeSuggest(row);
    });

    /* ---- 删除行内人员 ---- */
    card.addEventListener("click", function (e) {
      var del = e.target && e.target.closest
        ? e.target.closest("[data-credit-person-del]") : null;
      if (!del) { return; }
      e.preventDefault();
      var chip = del.closest(".credit-person-chip");
      var row = chip ? chip.closest(".credit-row") : null;
      var gone = chip ? String(chip.getAttribute("data-name") || "") : "";
      if (chip && chip.parentNode) { chip.parentNode.removeChild(chip); }
      if (row) { writeValues(row); }
      /* 基础名单(data-artists)也要把这个人踢掉 —— 它是 infobox 填入时
         写进去的, rebuildSummary 每次都从它起头, 不删就等于删了个寂寞
         (用户 m12610: 删除后仍然出现在预览中)。
         同一个人若还在别的词条行, 下面 rebuildSummary 的补人循环会把它
         加回来, 所以这里无条件删是安全的。 */
      if (gone) { dropFromBase(gone); }
      rebuildSummary();
    });

    /* ---- 职责框改动: 上方 chip 的职责跟着变 ---- */
    card.addEventListener("input", function (e) {
      var t = e.target;
      if (!t || t.name !== "credit_role") { return; }
      rebuildSummary();
    });

    /* ---- 隐藏域 credit_name 被外部改写时重建该行 chip ----
       恢复草稿只改表单值并派发 change; 若这里不重建 chip, 页面看似空的,
       下一次任何交互又会用 chip 里的旧值把 credit_name 覆盖回去
       (用户 m11689: 草稿恢复后字段不全)。 */
    card.addEventListener("change", function (e) {
      var t = e.target;
      if (!t || t.name !== "credit_name") { return; }
      var row = t.closest(".credit-row");
      var box = row ? row.querySelector(".credit-people-chips") : null;
      if (!box) { return; }
      box.textContent = "";
      String(t.value || "").split(/[\n、]+/).forEach(function (part) {
        var name = part.trim();
        if (name) { addChipOnly(row, name); }
      });
      rebuildSummary();
    });

    /* ---- 删除/新增词条行: 上方跟着变 ---- */
    card.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) { return; }
      if (t.closest("[data-pair-del]") || t.closest("[data-pair-add]")) {
        // 交给页面的行增删逻辑处理完再刷新
        window.setTimeout(rebuildSummary, 0);
        window.setTimeout(rebuildSummary, 60);
      }
    });

    /* ---- 人员输入框: 输入时给艺人候选(点选或回车加入) ---- */
    function suggestBoxOf(row) {
      var wrap = row.querySelector(".credit-people-box");
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

    function closeSuggest(row) {
      var box = row ? row.querySelector(".credit-suggest") : null;
      if (box) { box.style.display = "none"; box.textContent = ""; }
    }

    function renderSuggest(input, row) {
      var box = suggestBoxOf(row);
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
      var hits = [];
      var pool = window.__creditArtistPool;
      pool.forEach(function (one) {
        var name = one.name || "";
        var lower = name.toLowerCase();
        if (!name || have.indexOf(lower) !== -1) { return; }
        if (lower.indexOf(needle) === -1 && (one.eid || "") !== needle) { return; }
        hits.push(one);
      });
      box.textContent = "";
      if (!hits.length) { box.style.display = "none"; return; }
      hits.slice(0, 20).forEach(function (item) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "credit-suggest-item";
        btn.setAttribute("data-name", item.name);
        btn.setAttribute("data-entity-id", item.eid);
        btn.textContent = item.name;
        box.appendChild(btn);
      });
      box.style.display = "block";
    }

    card.addEventListener("input", function (e) {
      var t = e.target;
      if (!t || !t.classList || !t.classList.contains("credit-person-input")) {
        return;
      }
      var row = t.closest(".credit-row");
      if (row) { renderSuggest(t, row); }
    });

    card.addEventListener("mousedown", function (e) {
      var btn = e.target && e.target.closest
        ? e.target.closest(".credit-suggest-item") : null;
      if (!btn) { return; }
      e.preventDefault();
      var row = btn.closest(".credit-row");
      if (!row) { return; }
      addPersonToRow(row, btn.getAttribute("data-name"));
      var input = row.querySelector(".credit-person-input");
      if (input) { input.value = ""; }
      closeSuggest(row);
    });

    card.addEventListener("focusout", function (e) {
      var t = e.target;
      if (!t || !t.classList || !t.classList.contains("credit-person-input")) {
        return;
      }
      var row = t.closest(".credit-row");
      window.setTimeout(function () { closeSuggest(row); }, 120);
    });

    rebuildSummary();
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
      function readHidden() {
        items = [];
        (hidden.value || "").split(/[\n、]+/).forEach(function (part) {
          var value = part.trim();
          if (value && items.indexOf(value) === -1) { items.push(value); }
        });
      }
      readHidden();
      sync();

      // **外部改写隐藏域时重建 chips**
      //   infobox「一键填入」直接写 hidden.value 并派发 change;
      //   这里如果不重建 items, 之后任何一次 chip 增删都会用旧的 items
      //   把 hidden 覆盖回去 —— 表现为"填进去又没了"。
      hidden.addEventListener("change", function () {
        var before = items.join("\n");
        readHidden();
        if (items.join("\n") !== before) { sync(); }
      });

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
  /* ==========================================================================
     新建专辑: 封面「上传封面」按钮 = 只选图 + 本地预览

     编辑模式有封面 key, 可以异步上传 + 裁切(cover-crop.js);
     新建模式还没有 key, 封面只能随主表单一起提交, 所以不能走裁切弹窗。
     这里只做两件事: 点按钮 -> 打开文件选择; 选完 -> 左侧缩略图预览。
     ========================================================================== */
  /* 把 data URL 缩到 maxSide 以内再转回 data URL。
     草稿里存的是**预览**, 不需要原始画质 —— 一张 1MB 的封面 base64 后
     1.37M 字符, 按 UTF-16 算就吃不掉 localStorage 的 5MB 配额;
     缩到 900px 通常只有 100-250KB, 存几十张也不成问题。 */
  function shrinkDataUrl(dataUrl, maxSide, quality) {
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () {
        try {
          var w = img.naturalWidth || img.width;
          var h = img.naturalHeight || img.height;
          if (!w || !h) { resolve(""); return; }
          var scale = Math.min(1, (maxSide || 900) / Math.max(w, h));
          var cw = Math.max(1, Math.round(w * scale));
          var ch = Math.max(1, Math.round(h * scale));
          var canvas = document.createElement("canvas");
          canvas.width = cw;
          canvas.height = ch;
          var g = canvas.getContext("2d");
          if (!g) { resolve(""); return; }
          g.fillStyle = "#fff";
          g.fillRect(0, 0, cw, ch);
          g.drawImage(img, 0, 0, cw, ch);
          var out = canvas.toDataURL("image/jpeg", quality || 0.82);
          resolve(String(out || ""));
        } catch (err) {
          resolve("");
        }
      };
      img.onerror = function () { resolve(""); };
      img.src = dataUrl;
    });
  }

  /* 新建专辑页的「其他照片」(用户 m13494: 编辑页有, 新建页也该有)。
     新建时还没有 key, 走不了 /album/<key>/photo 那套异步上传 —— 所以这里只做
     三件事: 选图 -> 缩到 900px 存成 data URL -> 连同备注塞进隐藏域
     photos_data, 随主表单一起提交(服务端 _save_new_photos 再落盘)。 */
  function setupLocalPhotos() {
    var host = document.querySelector("[data-photo-local]");
    var input = document.getElementById("photo-input");
    var store = document.getElementById("photos-data");
    var list = document.querySelector("[data-photo-list]");
    if (!host || !input || !store || !list) { return; }
    var addBtn = host.querySelector("[data-photo-add]");
    var MAX = 12;
    var items = [];

    function sync() {
      try {
        store.value = items.length ? JSON.stringify(items) : "";
      } catch (err) { store.value = ""; }
      /* 草稿与脏标记都靠事件, 这里手动派发一次 */
      store.dispatchEvent(new Event("input", { bubbles: true }));
      store.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function render() {
      list.textContent = "";
      items.forEach(function (item, index) {
        var card = document.createElement("div");
        card.className = "photo-card photo-card-local";
        var img = document.createElement("img");
        img.className = "photo-thumb";
        img.alt = "";
        img.src = item.data;
        card.appendChild(img);

        var note = document.createElement("input");
        note.className = "input photo-note";
        note.type = "text";
        note.placeholder = "备注(可留空)";
        note.value = item.note || "";
        note.addEventListener("input", function () {
          if (items[index]) { items[index].note = note.value; }
          sync();
        });
        card.appendChild(note);

        var del = document.createElement("button");
        del.type = "button";
        del.className = "btn-ghost btn-xs photo-del";
        del.textContent = "×";
        del.title = "移除这张";
        del.addEventListener("click", function (e) {
          e.preventDefault();
          items.splice(index, 1);
          render();
          sync();
        });
        card.appendChild(del);
        list.appendChild(card);
      });
      if (addBtn) { addBtn.hidden = items.length >= MAX; }
    }

    function addFiles(files) {
      Array.prototype.forEach.call(files, function (file) {
        if (!file || items.length >= MAX) { return; }
        if (file.size > 2 * 1024 * 1024) { return; }
        if (String(file.type || "").indexOf("image/") !== 0) { return; }
        var reader = new FileReader();
        reader.onload = function () {
          var raw = String(reader.result || "");
          if (!raw) { return; }
          shrinkDataUrl(raw, 900).then(function (small) {
            items.push({ data: small || raw, note: "" });
            render();
            sync();
          });
        };
        reader.readAsDataURL(file);
      });
    }

    if (addBtn) {
      addBtn.addEventListener("click", function (e) {
        e.preventDefault();
        input.click();
      });
    }
    input.addEventListener("change", function () {
      /* 先拷贝一份再清空 input: 直接传 live 的 FileList, 清空后就读不到了 */
      addFiles(Array.prototype.slice.call(input.files || []));
      input.value = "";
    });
  }

  /* 「重复专辑合并」弹窗(用户 m13899): 按钮在操作行里, 点开才显示表单。 */
  function setupMergeDialog() {
    var form = document.getElementById("merge-album-form");
    if (!form) { return; }
    function open() {
      form.hidden = false;
      document.body.classList.add("merge-open");
      var input = form.querySelector('input[name="target"]');
      if (input) { input.focus(); }
    }
    function close() {
      form.hidden = true;
      document.body.classList.remove("merge-open");
    }
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.closest) { return; }
      if (t.closest("[data-merge-open]")) { e.preventDefault(); open(); return; }
      if (t.closest("[data-merge-close]")) { e.preventDefault(); close(); return; }
      /* 点遮罩(弹窗之外)也关掉, 但别把「点弹窗内部」也算进去 */
      if (!form.hidden && t === document.body) { close(); }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !form.hidden) { close(); }
    });
    /* 提交前至少要勾一项 —— 服务端也会拒, 这里先给即时反馈 */
    form.addEventListener("submit", function (e) {
      if (!form.querySelector('input[name="part"]:checked')) {
        e.preventDefault();
        window.alert("请至少勾选一项要合并的内容");
      }
    });
  }

  function setupCoverPick() {
    /* 入口可能有多个: 「上传封面」小按钮 + 新建页那个大方块(没图时就是加号)。
       以前只取第一个匹配, 新建页的大方块点不动(用户 m13494)。 */
    var buttons = Array.prototype.slice.call(
      document.querySelectorAll("[data-cover-pick]"));
    if (!buttons.length) { return; }
    var input = document.getElementById("cover-input");
    var hint = document.getElementById("cover-picked");
    var thumb = document.querySelector(".cover-fallback");
    var img = document.querySelector(".cover-preview-img");
    var word = thumb ? thumb.querySelector("span") : null;
    var store = document.getElementById("cover-data");
    if (!input) { return; }

    buttons.forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        input.click();
      });
      /* 那个大方块是 div[role=button], 键盘也要能开(无障碍) */
      btn.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          input.click();
        }
      });
    });

    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (!file) { return; }
      if (file.size > 2 * 1024 * 1024) {
        if (hint) { hint.textContent = "图片不能超过 2MB"; }
        input.value = "";
        return;
      }
      /* 用户自己选了图 -> infobox 缓存的那张作废(用户 m13899:
         "若不够清楚，由用户进行替换后删除云端缓存，改为保存由用户提交的")。
         服务端看到缓存 token 为空就不会把它转正; 文件也在下次缓存清理时删掉。 */
      var cached = document.getElementById("cover-cache");
      if (cached && cached.value) {
        cached.value = "";
        cached.dispatchEvent(new Event("input", { bubbles: true }));
        cached.dispatchEvent(new Event("change", { bubbles: true }));
      }
      if (hint) {
        hint.textContent = "已选择: " + file.name + " (" +
          Math.round(file.size / 1024) + " KB)";
      }
      if (!img) { return; }
      // 用 FileReader 出 data: URL —— CSP 的 img-src 允许 data:,
      // 但 createObjectURL 的 blob: 未必放行(踩过这个坑)。
      var reader = new FileReader();
      reader.onload = function () {
        var data = String(reader.result || "");
        img.src = data;
        img.hidden = false;
        if (word) { word.hidden = true; }
        /* 草稿要能记住选过哪张图(用户 m12280)。file input 的值存不进
           localStorage, 所以把 data URL 也写进隐藏域, 由 extra 槽单独收。
           注意这**不是**提交封面的通路: 服务端只认 multipart 的 cover。
           这里先缩到 900px 再存 —— 原图 base64 动辄 1.3M 字符, 按
           UTF-16 算直接吃掉 localStorage 大半配额, 缩完通常 100-250KB。 */
        if (!store) { return; }
        shrinkDataUrl(data, 900).then(function (small) {
          if (small && small.length <= 1500000) {
            store.value = small;
          } else {
            store.value = "";
            if (hint) {
              hint.textContent += "（图太大，草稿里无法保留这张预览）";
            }
          }
          store.dispatchEvent(new Event("input", { bubbles: true }));
        });
      };
      reader.readAsDataURL(file);
    });

    /* 草稿恢复时只有隐藏域被写值, 预览不会自己出现 —— 监听 change
       把 data URL 贴回 <img>。恢复逻辑同时派发 input 与 change。 */
    if (store) {
      store.addEventListener("change", function () {
        var data = String(store.value || "");
        if (!data || !img) { return; }
        img.src = data;
        img.hidden = false;
        if (word) { word.hidden = true; }
        if (hint) { hint.textContent = "已恢复草稿里的封面预览"; }
      });
    }
  }

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
          // 用户 m16568: 这一跳会丢掉没落盘的草稿, 先同步写一次
          if (typeof window.dddbFlushDraft === "function") {
            window.dddbFlushDraft();
          }
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

  /* 用户 m16568: 碟能被上移/下移之后, 碟号必须按**当前 DOM 顺序**重新编,
     否则 track_disc 会和屏幕上看到的顺序对不上(保存后碟序又变回去)。
     名字输入框的 name 也要跟着改(它按碟号命名: disc_name_<碟号>)。 */
  function syncDiscNumbers() {
    var blocks = document.querySelectorAll(".disc-block");
    Array.prototype.forEach.call(blocks, function (block, i) {
      var no = i + 1;
      block.setAttribute("data-disc", String(no));
      var title = block.querySelector(".disc-title");
      if (title) {
        title.textContent = "Disc " + (no < 10 ? "0" + no : no);
      }
      var nameInput = block.querySelector(".disc-name-input");
      if (nameInput) {
        nameInput.setAttribute("name", "disc_name_" + no);
        nameInput.setAttribute("aria-label", "Disc " + no + " 的名字");
      }
      var body = discBody(block);
      if (body) { body.setAttribute("data-disc-body", String(no)); }
      var rows = block.querySelectorAll("tr.track-row");
      Array.prototype.forEach.call(rows, function (row) {
        row.setAttribute("data-disc", String(no));
        var field = row.querySelector('input[name="track_disc"]');
        if (field) { field.value = String(no); }
      });
      renumberDisc(block);
    });
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

    // 用户 m16568: 新建的碟也要有命名框, 否则 JS 加出来的碟没法命名
    var nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "disc-name-input";
    nameInput.name = "disc_name_" + discNo;
    nameInput.placeholder = "这一碟的名字(可留空)";
    nameInput.setAttribute("aria-label", "Disc " + discNo + " 的名字");
    head.appendChild(nameInput);

    var tools = document.createElement("span");
    tools.className = "disc-tools";
    // 用户 m16568: 「调整曲轨符」没用(音轨号本来就会自动重排), 换成调整光碟顺序
    [["data-disc-move", "-1", "↑ 上移", "与上一张碟交换位置"],
     ["data-disc-move", "1", "↓ 下移", "与下一张碟交换位置"],
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
      fields: [["url_key", "链接名", "填网址自动识别"],
               ["url_value", "网址", "https://..."]]
    }
  };

  /* 链接名输入框要挂上服务端渲染的候选表(<datalist id="dl-url_key">)。
     模板里已有的行是服务端写的, 但「＋ 添加外链」动态建出来的行不带这个
     属性 —— 结果新行既没有候选下拉, 也跟着丢了自动识名的观感(用户 m14663)。 */
  var DATALIST_FIELDS = { url_key: "dl-url_key" };

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
      if (DATALIST_FIELDS[field[0]]) {
        input.setAttribute("list", DATALIST_FIELDS[field[0]]);
        input.setAttribute("autocomplete", "off");
      }
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
        if (!container && host) {
          // 从 .pair-add 往前找**第一个** .rows, 不能只取前一个兄弟:
          // 外链卡片里 <datalist id="dl-url_key"> 就夹在中间, 取前一个兄弟
          // 会把新行 appendChild 进 <datalist>(浏览器不渲染, 按钮看起来完全
          // 没反应 —— 用户 m14663)。模板已把 datalist 挪到 .rows 之前,
          // 这里再兜一层, 以后加别的兄弟节点也不会踩。
          var prev = host.previousElementSibling;
          while (prev) {
            if (prev.classList && prev.classList.contains("rows")) {
              container = prev;
              break;
            }
            prev = prev.previousElementSibling;
          }
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

  /* 外链名自动识别: 填完网址(或粘贴整段 embed code)后, 按域名认一个标准名
     (Apple Music / SoundCloud / Spotify / X (Twitter) / Embed Link (Youtube) ...)。
     规则见 dddb/public/linknames.py —— 服务端把名字表塞进 #dl-url_key 的
     option 里, 这里只按**域名后缀**做匹配, 保证两边一致。
     只在链接名为空时填, 绝不覆盖用户自己写的名字(用户 m13077)。 */
  function setupLinkAutoName() {
    function hostOf(value) {
      var text = String(value || "").trim();
      if (!text) { return ""; }
      if (text.indexOf("//") < 0) { text = "https://" + text.replace(/^\/+/, ""); }
      try { return (new URL(text)).hostname.toLowerCase().replace(/\.$/, ""); }
      catch (e) { return ""; }
    }
    // 与 linknames.LINK_NAME_RULES 同序(长后缀在前)
    var RULES = [
      ["music.apple.com", "Apple Music"],
      ["open.spotify.com", "Spotify"],
      ["play.spotify.com", "Spotify"],
      ["on.soundcloud.com", "SoundCloud"],
      ["soundcloud.com", "SoundCloud"],
      ["music.youtube.com", "YouTube"],
      ["youtube-nocookie.com", "YouTube"],
      ["youtu.be", "YouTube"],
      ["youtube.com", "YouTube"],
      ["x.com", "X (Twitter)"],
      ["twitter.com", "X (Twitter)"],
      ["bandcamp.com", "Bandcamp"],
      ["nicovideo.jp", "niconico"],
      ["nico.ms", "niconico"],
      ["bilibili.com", "bilibili"],
      ["instagram.com", "Instagram"],
      ["booth.pm", "BOOTH"],
      ["melonbooks.co.jp", "Melonbooks"],
      ["amazon.co.jp", "Amazon"],
      ["amazon.com", "Amazon"],
      ["linktr.ee", "linktr.ee"],
      ["lit.link", "lit.link"],
      ["note.com", "note"],
      ["tunecore.co.jp", "TuneCore"],
      ["diverse.direct", "DIVERSE DIRECT"]
    ];
    function guess(value) {
      var text = String(value || "").trim();
      if (!text) { return ""; }
      // 整段 embed code: iframe 里出现的平台字样
      if (/<iframe\b/i.test(text)) {
        var low = text.toLowerCase();
        if (low.indexOf("soundcloud") >= 0) { return "Embed Link (SoundCloud)"; }
        if (low.indexOf("youtu") >= 0) { return "Embed Link (Youtube)"; }
        return "";
      }
      var host = hostOf(text);
      if (!host) { return ""; }
      for (var i = 0; i < RULES.length; i++) {
        var suffix = RULES[i][0];
        if (host === suffix || host.slice(-(suffix.length + 1)) === "." + suffix) {
          return RULES[i][1];
        }
      }
      return "";
    }
    function fill(row) {
      if (!row) { return; }
      var urlInput = row.querySelector('input[name="url_value"]');
      var keyInput = row.querySelector('input[name="url_key"]');
      if (!urlInput || !keyInput) { return; }
      if (String(keyInput.value || "").trim()) { return; }   // 用户填过就别动
      var name = guess(urlInput.value);
      if (!name) { return; }
      keyInput.value = name;
      keyInput.dispatchEvent(new Event("input", { bubbles: true }));
      keyInput.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function rowOf(el) {
      return el && el.closest ? el.closest('[data-pair-row="url"]') : null;
    }
    document.addEventListener("focusout", function (e) {
      var t = e.target;
      if (!t || !t.getAttribute || t.getAttribute("name") !== "url_value") { return; }
      fill(rowOf(t));
    });
    document.addEventListener("change", function (e) {
      var t = e.target;
      if (!t || !t.getAttribute || t.getAttribute("name") !== "url_value") { return; }
      fill(rowOf(t));
    });
    /* 粘贴 embed code 时 input 事件先到: 防抖后也判一次, 不用等失焦 */
    var timer = null;
    document.addEventListener("input", function (e) {
      var t = e.target;
      if (!t || !t.getAttribute || t.getAttribute("name") !== "url_value") { return; }
      if (timer) { window.clearTimeout(timer); }
      timer = window.setTimeout(function () { fill(rowOf(t)); }, 500);
    });
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
     整段粘贴: 只在前端解析并填进下面的表格(暂存), 不提交表单(用户 m16094)
     ---------------------------------------------------------------------------
     曲目粘贴 / 制作人员粘贴两个折叠框都走这一套:
       点按钮 -> 解析 textarea -> 填进下方曲目表 / 词条行 -> 提示"还没保存"
     真正的写库仍然只发生在用户点最下面那个「保存」时。

     解析规则与 dddb/trackparse.py、dddb/creditparse.py 一一对应(含全角归一、
     轨号前缀、括号时长、制表符表格、跳过表头/碟号/注释; 词条先剥掉括号里的
     链接再按**最后一个**冒号切职责)。服务端那两条老链路仍然在 —— 表单照常
     提交时服务端会再解析一遍, 所以 JS 没跑起来也不会丢数据。
     ========================================================================== */
  function fireEvent(el, type) {
    if (!el || !el.dispatchEvent) { return; }
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }

  /* 全角 -> 半角 + 各种空白归一(与 trackparse._norm 同表) */
  function normPasteText(text) {
    return String(text == null ? "" : text)
      .replace(/[\uFF01-\uFF5E]/g, function (ch) {
        return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0);
      })
      .replace(/[\u3000\u00A0\u2002-\u200A\u202F\u205F]/g, " ")
      .replace(/[\u200B-\u200D\u2060\uFEFF]/g, "");
  }

  function tidyPasteText(text) {
    return normPasteText(text).replace(/\s+/g, " ").trim();
  }

  var DUR_BODY = "(?:\\d{1,3}:\\d{2}(?::\\d{2})?|\\d{1,3}分\\d{1,2}秒|\\d{1,3}秒)";
  var RE_DUR_BRACKET = new RegExp("[([【]\\s*(" + DUR_BODY + ")\\s*[)\\]】]", "g");
  var RE_TRAIL_DUR = new RegExp("^(.*\\S)[ \\t]+(" + DUR_BODY + ")[ \\t]*$");
  var RE_TRAIL_SEC = /^(.*\S)[ \t]+(\d{2,5})[ \t]*$/;
  var RE_SEP_ANY = /\s+\/\s+|\s+[-–—]\s+/;
  var RE_DUR_FULL = new RegExp("^(?:" + DUR_BODY + "|\\d+)$");
  var RE_COLON_DUR = /^(\d{1,3}):(\d{2})(?::(\d{2}))?$/;
  var RE_MIN_SEC = /^(\d{1,3})分(\d{1,2})秒$/;
  var RE_SEC = /^(\d{1,3})秒$/;
  var BRACKET_PAIRS = [["[", "]"], ["(", ")"], ["【", "】"], ["{", "}"], ["〈", "〉"]];

  function parsePasteDuration(text) {
    var value = normPasteText(text).trim();
    if (!value) { return null; }
    for (var i = 0; i < BRACKET_PAIRS.length; i++) {
      var open = BRACKET_PAIRS[i][0], close = BRACKET_PAIRS[i][1];
      if (value.length >= 2 && value.charAt(0) === open
          && value.charAt(value.length - 1) === close) {
        value = value.slice(1, -1).trim();
        break;
      }
    }
    if (!RE_DUR_FULL.test(value)) { return null; }
    var m = RE_COLON_DUR.exec(value);
    if (m) {
      var a = parseInt(m[1], 10), b = parseInt(m[2], 10);
      return m[3] ? a * 3600 + b * 60 + parseInt(m[3], 10) : a * 60 + b;
    }
    m = RE_MIN_SEC.exec(value);
    if (m) { return parseInt(m[1], 10) * 60 + parseInt(m[2], 10); }
    m = RE_SEC.exec(value);
    if (m) { return parseInt(m[1], 10); }
    var bare = parseInt(value, 10);
    return (bare > 0 && bare <= 86399) ? bare : null;
  }

  function formatPasteDuration(seconds) {
    var total = Number(seconds);
    if (!isFinite(total) || total < 0) { return ""; }
    total = Math.floor(total);
    var hours = Math.floor(total / 3600);
    var mins = Math.floor((total % 3600) / 60);
    var secs = total % 60;
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return hours ? hours + ":" + pad(mins) + ":" + pad(secs) : mins + ":" + pad(secs);
  }

  var HEADER_EXACT = {};
  ("曲目 曲目表 曲目列表 曲目清单 曲名 曲名リスト 标题 標題 歌曲 歌曲列表 収録曲 " +
   "收録曲 收录曲 收录曲目 トラックリスト トラック 番号 タイトル 時間 时长 " +
   "アーティスト アーティスト名 歌手 艺人 tracklist tracks songs tracklisting songlist"
  ).split(" ").forEach(function (word) { HEADER_EXACT[word.toLowerCase()] = true; });

  var COLUMN_WORDS = {};
  Object.keys(HEADER_EXACT).forEach(function (key) { COLUMN_WORDS[key] = true; });
  ("# no no. tr tr. track number title name time length duration artist artists song " +
   "disc disk cd 曲 作曲 作詞 作词 編曲 编曲 演唱 演奏 初出 原曲 时长 碟"
  ).split(" ").forEach(function (word) { COLUMN_WORDS[word.toLowerCase()] = true; });

  var RE_HEADER_SEP = /[\s#.,:;|/\-–—()\[\]{}「」【】『』、]+/g;
  var RE_PIECE_SPLIT = /[\s|/\-–—]+/;
  var RE_SPLIT_TA = /\s+\/\s+|\s+[-–—]\s+/;
  var RE_ARTIST_SPLIT = /\s*\/\s*|\s*[、,，;；]\s*/;
  var RE_CELL_ARTIST_SPLIT = /\s*\/\s*|[,，、;；]\s*/;
  var RE_BARE_INDEX = /^(?:(?:tr|trk|track|no|number|#)\s*[.:、)]?\s*)?(\d{1,3})[.,、)]?$/i;
  var RE_BULLET = /^(?:[・•·‣■□●○◆◇★☆]|[*+]\s*|[-–—]\s+)/;
  var RE_COMMENT = /^(?:#(?!\s*\d)|\/\/|;|※|>\s)/;
  // 碟号行(用户 m16568: 要能识别分碟, 所以这里**捕获**碟号而不是丢掉)
  var RE_DISC_LINE = /^(?:disc|disk|cd|vol\.?|volume|ディスク|碟)[ \t]*[-–—]?[ \t]*(\d+)$/i;
  // 只有碟号、没写 Disc 前缀的写法(第2碟 / 2枚目)
  var RE_DISC_CN = /^第?\s*(\d{1,2})\s*(?:枚目|碟|盘|張目|张目)$/;
  var RE_RULE_LINE = /^[-=_*~·—–\s]{3,}$/;
  var RE_INDEX_HEAD = new RegExp(
    "^((?:(?:tr|trk|track|no|number)\\s*[.:、)\\]]?\\s*|#\\s*)(\\d{1,3})" +
    "|(\\d{1,3})\\s*[.,、)]\\s*(?!\\d)" +
    "|(\\d{1,3})[ \\t]+" +
    "|(0\\d{1,2})(?=[^\\d\\s:.,)\\]|\\-])" +
    "|(\\d{1,3})(?=[^\\d\\s:.,)\\]|\\-]))", "i");

  function splitPasteTitleArtists(text) {
    var body = tidyPasteText(text);
    var match = RE_SPLIT_TA.exec(body);
    if (!match) { return [body, []]; }
    var title = body.slice(0, match.index).trim();
    var rest = body.slice(match.index + match[0].length).trim();
    if (!title || !rest) { return [body, []]; }
    var artists = rest.split(RE_ARTIST_SPLIT).map(function (one) {
      return one.trim();
    }).filter(function (one) { return !!one; });
    return [title, artists];
  }

  function isPasteHeaderLine(line) {
    var token = line.replace(RE_HEADER_SEP, "").toLowerCase();
    if (HEADER_EXACT[token]) { return true; }
    var pieces = line.split(RE_PIECE_SPLIT);
    if (pieces.length < 2) { return false; }
    return pieces.every(function (piece) {
      return !!COLUMN_WORDS[piece.replace(RE_HEADER_SEP, "").toLowerCase()];
    });
  }

  /* 剥出**第一个**括号时长; 返回 [剩余文本, 秒数或 null] */
  function extractPasteDurations(text) {
    var found = null;
    RE_DUR_BRACKET.lastIndex = 0;
    var match = RE_DUR_BRACKET.exec(text);
    if (match) {
      found = parsePasteDuration(match[1]);
      if (found === null) { found = null; }
    }
    return [text.replace(RE_DUR_BRACKET, " "), found];
  }

  function parsePasteTable(cells, duration) {
    var rest = cells.slice();
    var number = null;
    if (rest.length >= 2) {
      var head = RE_BARE_INDEX.exec(rest[0]);
      if (head) {
        number = parseInt(head[1], 10);
        rest = rest.slice(1);
      }
    }
    if (duration == null) {
      for (var i = rest.length - 1; i >= 0; i--) {
        var found = parsePasteDuration(rest[i]);
        if (found !== null) {
          duration = found;
          rest.splice(i, 1);
          break;
        }
      }
    }
    var title = "", artists = [];
    if (rest.length) {
      var headCell = rest[0];
      rest = rest.slice(1);
      var inner = extractPasteDurations(headCell);
      headCell = inner[0];
      if (duration == null) { duration = inner[1]; }
      var pair = splitPasteTitleArtists(headCell);
      title = pair[0];
      artists = pair[1].slice();
    }
    rest.forEach(function (cell) {
      cell.split(RE_CELL_ARTIST_SPLIT).forEach(function (one) {
        if (one.trim()) { artists.push(one.trim()); }
      });
    });
    return {
      raw: "", duration: duration, number: number,
      weak: false, title: title, artists: artists,
      altTitle: "", altArtists: []
    };
  }

  /* 解析一行(已归一化); 返回 null 表示这行不是曲目 */
  function scanPasteLine(normalized, raw) {
    if (!normalized || RE_RULE_LINE.test(normalized)) { return null; }
    var body = normalized.replace(RE_BULLET, "").trim();
    if (!body) { return null; }
    if (RE_COMMENT.test(body) || RE_DISC_LINE.test(body)) { return null; }
    if (isPasteHeaderLine(body)) { return null; }

    var extracted = extractPasteDurations(body);
    body = extracted[0].trim();
    var duration = extracted[1];
    if (!body) { return null; }
    if (parsePasteDuration(body) !== null) { return null; }
    if (duration == null) {
      var trail = RE_TRAIL_DUR.exec(body);
      if (trail) {
        body = trail[1].trim();
        duration = parsePasteDuration(trail[2]);
      }
    }
    if (!duration) { duration = null; }
    if (!body) { return null; }

    if (body.indexOf("\t") !== -1) {
      var cells = body.split("\t").map(function (cell) { return cell.trim(); })
        .filter(function (cell) { return !!cell; });
      if (cells.length >= 2) {
        var tabled = parsePasteTable(cells, duration);
        tabled.raw = raw;
        return tabled.title ? tabled : null;
      }
    }

    var number = null, weak = false, whole = body;
    var head = RE_INDEX_HEAD.exec(body);
    if (head) {
      number = parseInt(head[2] || head[3] || head[4] || head[5] || head[6], 10);
      weak = !head[5] && !!head[6];
      body = body.slice(head[0].length).trim();
      body = body.replace(/^[-–—][ \t]*/, "").trim();
      if (!body || parsePasteDuration(body) !== null) { return null; }
    }

    var secMatch = RE_TRAIL_SEC.exec(body);
    if (secMatch && duration == null && RE_SEP_ANY.test(secMatch[1])) {
      var secs = parseInt(secMatch[2], 10);
      if (secs >= 20 && secs <= 7200) {
        body = secMatch[1].trim();
        duration = secs;
      }
    }

    var split = splitPasteTitleArtists(body);
    var pending = {
      raw: raw, duration: duration, number: number, weak: weak,
      title: split[0], artists: split[1], altTitle: "", altArtists: []
    };
    if (weak) {
      var alt = splitPasteTitleArtists(whole);
      pending.altTitle = alt[0];
      pending.altArtists = alt[1];
    }
    if (!pending.title && !pending.altTitle) { return null; }
    return pending;
  }

  /* 这一行是不是碟号分隔行? 是则返回碟号, 否则 null(用户 m16568) */
  function parsePasteDisc(line) {
    var match = RE_DISC_LINE.exec(line) || RE_DISC_CN.exec(line);
    if (!match) { return null; }
    var number = parseInt(match[1], 10);
    return number > 0 ? number : null;
  }

  function parsePasteTracks(text) {
    var source = String(text == null ? "" : text);
    var pending = [];
    var disc = 1;
    source.split(/\r\n|\r|\n/).forEach(function (line) {
      var normalized = normPasteText(line).trim();
      var found = parsePasteDisc(normalized);
      if (found !== null) { disc = found; return; }
      var item = scanPasteLine(normalized, line.trim());
      if (item) {
        item.disc = disc;
        pending.push(item);
      }
    });
    var tracks = [];
    var counters = {};
    pending.forEach(function (item, position) {
      var number = item.number, title, artists;
      if (item.weak && number !== position + 1) {
        title = item.altTitle;
        artists = item.altArtists.slice();
      } else {
        title = item.title;
        artists = item.artists.slice();
      }
      if (!title) { return; }
      counters[item.disc] = (counters[item.disc] || 0) + 1;
      tracks.push({
        disc: item.disc,
        index: counters[item.disc],
        title: title, artists: artists,
        duration: item.duration
      });
    });
    return tracks;
  }

  /* --- 制作人员: 规则同 dddb/creditparse.py --- */
  var RE_CREDIT_PAREN = /[(（][^()（）]*[)）]/g;
  var RE_CREDIT_URL = /https?:\/\/\S+/g;
  var RE_CREDIT_COLON = /[:：]/g;
  var RE_CREDIT_NAME_SPLIT = /\s*[/、,，;；]\s*/;
  var RE_CREDIT_BULLET = /^\s*(?:[-*·•]|\d+\s*[.、)）])\s*/;
  // 推特/X 文本里的 @handle(用户 m16568: 制作人员识别要排除 @)。
  // 只认前面不是字母数字的 @ —— 邮箱 a@b.com 不该被腰斩。
  var RE_CREDIT_AT = /(?<![\w.])@[A-Za-z0-9_]{1,30}/g;
  var CREDIT_HEADER_WORDS = {};
  ("职责 职位 角色 担当 人员 名字 姓名 staff credit credits role roles name names"
  ).split(" ").forEach(function (word) { CREDIT_HEADER_WORDS[word.toLowerCase()] = true; });

  function stripCreditLinks(text) {
    var value = String(text == null ? "" : text);
    for (var round = 0; round < 4; round++) {
      var next = value.replace(RE_CREDIT_PAREN, " ");
      if (next === value) { break; }
      value = next;
    }
    return value.replace(RE_CREDIT_URL, " ").replace(RE_CREDIT_AT, " ")
      .replace(/\s+/g, " ").trim();
  }

  function isCreditHeaderLine(line) {
    var bare = line.replace(RE_CREDIT_BULLET, "");
    var pieces = bare.split(/\s*[/、,，;；:：]\s*/);
    if (!pieces.length) { return false; }
    return pieces.every(function (piece) {
      return !!CREDIT_HEADER_WORDS[piece.toLowerCase()];
    });
  }

  function parsePasteCredits(text) {
    var source = String(text == null ? "" : text);
    var out = [];
    var seen = {};
    source.split(/\r\n|\r|\n/).forEach(function (rawLine) {
      var line = String(rawLine).replace(RE_CREDIT_BULLET, "").trim();
      if (!line) { return; }
      if (isCreditHeaderLine(line)) { return; }
      RE_CREDIT_COLON.lastIndex = 0;
      if (!RE_CREDIT_COLON.test(line)) { return; }
      //: 先剥掉括号里的链接再找冒号, 否则最后一个冒号会是 "https:" 里的
      line = stripCreditLinks(line);
      if (!line) { return; }
      var cut = -1;
      RE_CREDIT_COLON.lastIndex = 0;
      var scan;
      while ((scan = RE_CREDIT_COLON.exec(line)) !== null) { cut = scan.index; }
      if (cut <= 0) { return; }
      var role = line.slice(0, cut).trim();
      var rest = line.slice(cut + 1).trim();
      if (!role || !rest) { return; }
      rest.split(RE_CREDIT_NAME_SPLIT).forEach(function (name) {
        var person = name.trim();
        if (!person) { return; }
        var token = role + "\u0000" + person;
        if (seen[token]) { return; }
        seen[token] = true;
        out.push({ role: role, name: person });
      });
    });
    return out;
  }

  /* 把 [(name)] 按职责并成 [ {role, names: [...]} ], 保持首次出现顺序 */
  function groupPasteCredits(list) {
    var order = [], map = {};
    list.forEach(function (item) {
      var key = item.role;
      if (!map[key]) { map[key] = { role: key, names: [] }; order.push(key); }
      map[key].names.push(item.name);
    });
    return order.map(function (key) { return map[key]; });
  }

  /* --- 填表: 曲目 --- */
  function putTrackCell(row, name, value) {
    var input = row.querySelector('[name="' + name + '"]');
    if (!input) { return; }
    input.value = value == null ? "" : String(value);
    fireEvent(input, "input");
    fireEvent(input, "change");
  }

  function fillPastedTracks(tracks, mode) {
    var host = document.getElementById("track-rows");
    if (!host || !tracks.length) { return 0; }
    var blocks = Array.prototype.slice.call(host.querySelectorAll(".disc-block"));
    if (!blocks.length) { return 0; }

    // 用户 m16568: 粘贴文本可能带分碟(Disc 2 ...), 按碟号分组后再填
    var groups = [];
    var byDisc = {};
    tracks.forEach(function (track) {
      var disc = parseInt(track.disc, 10) || 1;
      if (disc < 1) { disc = 1; }
      if (!byDisc[disc]) { byDisc[disc] = []; groups.push(disc); }
      byDisc[disc].push(track);
    });
    groups.sort(function (a, b) { return a - b; });

    var writeRow = function (body, discNo, track) {
      var count = body.querySelectorAll("tr.track-row").length;
      var row = buildTrackRow(discNo, count + 1);
      body.appendChild(row);
      putTrackCell(row, "track_title", track.title);
      putTrackCell(row, "track_artists", (track.artists || []).join("、"));
      putTrackCell(row, "track_duration", formatPasteDuration(track.duration));
    };
    // addDiscBlock 会顺手塞一行空行, 粘贴时不要它
    var freshDisc = function (discNo) {
      var block = addDiscBlock(discNo);
      if (!block) { return null; }
      var body = discBody(block);
      if (body) { body.innerHTML = ""; }
      return block;
    };

    if (mode === "append") {
      // 追加: 每一组都落在**最后一碟之后**的新碟里, 绝不塞进已有的碟
      var base = discNumber(blocks[blocks.length - 1]);
      groups.forEach(function (disc, i) {
        var block;
        if (i === 0) {
          block = blocks[blocks.length - 1];
          var reuseBody = discBody(block);
          if (!reuseBody) { return; }
          byDisc[disc].forEach(function (track) {
            writeRow(reuseBody, base, track);
          });
        } else {
          block = freshDisc(base + i);
          if (!block) { return; }
          var body = discBody(block);
          if (!body) { return; }
          byDisc[disc].forEach(function (track) {
            writeRow(body, base + i, track);
          });
        }
        renumberDisc(block);
      });
      return tracks.length;
    }

    // replace: 清空所有碟, 再按粘贴里的碟号重建(不够就补碟, 多了就删多余的空碟)
    blocks.forEach(function (block) {
      var body = discBody(block);
      if (body) { body.innerHTML = ""; }
    });
    groups.forEach(function (disc, i) {
      var block = blocks[i];
      if (!block) {
        block = freshDisc(discNumber(blocks[blocks.length - 1]) + 1);
        if (!block) { return; }
        blocks.push(block);
      }
      var body = discBody(block);
      if (!body) { return; }
      var discNo = discNumber(block);
      byDisc[disc].forEach(function (track) {
        writeRow(body, discNo, track);
      });
      renumberDisc(block);
    });
    // 比粘贴内容多出来的碟: 只留一碟, 其余删掉(空的才删, 有内容的原样保留)
    for (var i = groups.length; i < blocks.length; i++) {
      var extra = blocks[i];
      var extraBody = discBody(extra);
      if (extraBody && !extraBody.querySelectorAll("tr.track-row").length && i > 0) {
        extra.parentNode.removeChild(extra);
      }
    }
    blocks.forEach(function (block) {
      if (block.parentNode) { renumberDisc(block); }
    });
    return tracks.length;
  }

  /* --- 填表: 制作人员 --- */
  function fillPastedCredits(groups, mode) {
    var card = document.querySelector('[data-card="credits"]');
    if (!card || !groups.length) { return 0; }
    var add = card.querySelector('[data-pair-add="credit"]');
    var rowsHost = card.querySelector(".rows");
    if (!add || !rowsHost) { return 0; }

    var rows = Array.prototype.slice.call(
      rowsHost.querySelectorAll('[data-pair-row="credit"]'));
    if (mode !== "append") {
      rows.forEach(function (row) {
        if (row.parentNode) { row.parentNode.removeChild(row); }
      });
      rows = [];
    }

    groups.forEach(function (group) {
      // 追加模式先复用"职责空 + 没有人员"的旧空行, 免得每次粘贴都堆一行空的
      var row = null;
      for (var i = 0; i < rows.length; i++) {
        var roleInput = rows[i].querySelector('input[name="credit_role"]');
        var chips = rows[i].querySelectorAll(".credit-person-chip");
        if (!chips.length && (!roleInput || !String(roleInput.value).trim())) {
          row = rows[i];
          break;
        }
      }
      if (!row) {
        // 走页面自己的「＋ 添加词条」, 新行结构才不会和已有行不一致
        var before = rowsHost.querySelectorAll('[data-pair-row="credit"]').length;
        add.click();
        var all = rowsHost.querySelectorAll('[data-pair-row="credit"]');
        if (all.length <= before) { return; }
        row = all[all.length - 1];
        rows.push(row);
      }
      var role = row.querySelector('input[name="credit_role"]');
      if (role) {
        role.value = group.role;
        fireEvent(role, "input");
        fireEvent(role, "change");
      }
      var input = row.querySelector(".credit-person-input");
      if (!input) { return; }
      group.names.forEach(function (name) {
        input.value = name;
        // 走用户手动回车加入的那条路径(chip 与隐藏域都由它维护)
        input.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true
        }));
      });
      input.value = "";
    });
    return groups.length;
  }

  function pasteHint(box, text) {
    if (!box) { return; }
    var hint = box.querySelector(".trk-paste-hint");
    if (!hint) { return; }
    hint.textContent = text;
  }

  function readPasteMode(box, name) {
    var picked = box.querySelector('input[name="' + name + '"]:checked');
    var value = picked ? picked.value : "replace";
    return value === "append" ? "append" : "replace";
  }

  function setupPasteFill() {
    document.addEventListener("click", function (e) {
      var target = e.target;
      if (!target || !target.closest) { return; }

      var trackBtn = target.closest('[data-track-apply="1"]');
      if (trackBtn) {
        e.preventDefault();                 // 绝对不提交表单
        var trackBox = trackBtn.closest(".trk-paste");
        var trackArea = trackBox
          ? trackBox.querySelector('textarea[name="track_paste"]') : null;
        if (!trackBox || !trackArea) { return; }
        var tracks = parsePasteTracks(trackArea.value);
        var trackMode = readPasteMode(trackBox, "track_mode");
        if (!tracks.length) {
          pasteHint(trackBox, "这段内容里没识别出曲目; 下面的曲目表保持原样。");
          return;
        }
        var filled = fillPastedTracks(tracks, trackMode);
        if (!filled) {
          pasteHint(trackBox, "识别出 " + tracks.length + " 首, 但页面上找不到曲目表。");
          return;
        }
        pasteHint(trackBox, "已把 " + filled + " 首"
          + (trackMode === "append" ? "追加" : "填")
          + "进下面的曲目表; 还没保存, 点最下面的「保存」才会写进库里。");
        return;
      }

      var creditBtn = target.closest('[data-credit-apply="1"]');
      if (!creditBtn) { return; }
      e.preventDefault();
      var creditBox = creditBtn.closest(".credit-paste");
      var creditArea = creditBox
        ? creditBox.querySelector('textarea[name="credit_paste"]') : null;
      if (!creditBox || !creditArea) { return; }
      var parsed = parsePasteCredits(creditArea.value);
      var creditMode = readPasteMode(creditBox, "credit_mode");
      if (!parsed.length) {
        pasteHint(creditBox, "这段内容里没识别出词条; 下面的词条保持原样。");
        return;
      }
      var groups = groupPasteCredits(parsed);
      var added = fillPastedCredits(groups, creditMode);
      if (!added) {
        pasteHint(creditBox, "识别出 " + parsed.length + " 条, 但页面上找不到词条卡片。");
        return;
      }
      pasteHint(creditBox, "已把 " + groups.length + " 行 / " + parsed.length + " 条"
        + (creditMode === "append" ? "追加" : "填")
        + "进下面的词条; 还没保存, 点最下面的「保存」才会写进库里。");
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

  /* ==========================================================================
     详情页封面画廊(用户 m16568)
     一张专辑可能有两张封面(双碟各一张, 第二张其实是附图)。
     模板把封面 + 附图都渲染成 data-cover-frame, 这里只负责切换:
     右侧点击区域 = 下一张, 底部圆点 = 直接跳。
     ========================================================================== */
  /* ==========================================================================
     出品信息: 同人展侧 / 独立同人侧(用户 m16568)
     两组字段都在表单里(没脚本时两组都显示, 照样能保存),
     有脚本时按选中的侧只显示对应的那组。
     ========================================================================== */
  function setupReleaseKind() {
    var picker = document.querySelector("[data-release-kind-pick]");
    if (!picker) { return; }
    var groups = document.querySelectorAll(".info-group[data-release-kind]");

    function apply(kind) {
      Array.prototype.forEach.call(groups, function (group) {
        var match = group.getAttribute("data-release-kind") === kind;
        if (match) { group.removeAttribute("hidden"); }
        else { group.setAttribute("hidden", ""); }
      });
    }

    var checked = picker.querySelector('input[name="release_kind"]:checked');
    if (checked) { apply(checked.value); }
    picker.addEventListener("change", function (e) {
      var input = e.target;
      if (input && input.name === "release_kind" && input.checked) {
        apply(input.value);
      }
    });
  }

  /* 详情页封面画廊(用户 m16568). 名字不能叫 setupCoverGallery ——
     编辑页那套多图槽位也叫这个名, 函数声明提升会让后者顶掉前者。 */
  function setupDetailCover() {
    var gallery = document.querySelector("[data-cover-gallery]");
    if (!gallery) { return; }
    var frames = gallery.querySelectorAll("[data-cover-frame]");
    if (frames.length < 2) { return; }
    var dots = gallery.querySelectorAll("[data-cover-goto]");
    var counter = gallery.querySelector("[data-cover-count]");
    var current = 0;

    function show(index) {
      var total = frames.length;
      current = ((index % total) + total) % total;
      Array.prototype.forEach.call(frames, function (frame, i) {
        var hidden = i !== current;
        frame.classList.toggle("is-hidden", hidden);
        if (hidden) { frame.setAttribute("hidden", ""); }
        else { frame.removeAttribute("hidden"); }
      });
      Array.prototype.forEach.call(dots, function (dot, i) {
        dot.classList.toggle("is-active", i === current);
      });
      if (counter) { counter.textContent = (current + 1) + "/" + total; }
    }

    gallery.addEventListener("click", function (e) {
      var target = e.target;
      if (!target || !target.closest) { return; }
      if (target.closest("[data-cover-next]")) {
        e.preventDefault();
        show(current + 1);
        return;
      }
      var dot = target.closest("[data-cover-goto]");
      if (dot) {
        e.preventDefault();
        show(parseInt(dot.getAttribute("data-cover-goto"), 10) || 0);
      }
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
        syncDiscNumbers();
        return;
      }

      // 调整光碟顺序(用户 m16568: 原来的「调整曲轨符」没实际作用, 换成上移/下移)
      var moveDisc = target.closest("[data-disc-move]");
      if (moveDisc) {
        e.preventDefault();
        var mvBlock = moveDisc.closest(".disc-block");
        if (!mvBlock) { return; }
        var step = parseInt(moveDisc.getAttribute("data-disc-move"), 10) || 0;
        var all = document.querySelectorAll(".disc-block");
        var pos = Array.prototype.indexOf.call(all, mvBlock);
        var to = pos + step;
        if (pos < 0 || to < 0 || to >= all.length) { return; }
        var other = all[to];
        if (step < 0) {
          other.parentNode.insertBefore(mvBlock, other);
        } else {
          other.parentNode.insertBefore(mvBlock, other.nextSibling);
        }
        syncDiscNumbers();
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

    /* 拖动排序: 按住「⠿」把手拖动整行。
       用户 m16568: 曲子可以跨碟拖 —— 拖到别的碟的行上就整行搬过去,
       拖到空碟的表格区域/「暂无数据」上也行。松手后两边的碟都要重排音轨号。 */
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
      if (over && over !== dragRow) {
        // 用户 m16568: 跨碟也能拖 —— 松开后 syncDiscNumbers() 会把两边的
        // track_disc / 音轨号重排, 所以这里不再限制同一张碟。
        e.preventDefault();
        if (e.dataTransfer) { e.dataTransfer.dropEffect = "move"; }
        over.classList.add("drag-over");
        var rect = over.getBoundingClientRect();
        var after = (e.clientY - rect.top) > rect.height / 2;
        over.parentNode.insertBefore(dragRow, after ? over.nextSibling : over);
        return;
      }
      // 拖到空碟(没有曲目行)上: 直接落到该碟的 tbody 里
      var body = e.target && e.target.closest
        ? e.target.closest("tbody") : null;
      if (body && !body.querySelector("tr.track-row")) {
        var emptyBlock = body.closest(".disc-block");
        if (emptyBlock) {
          e.preventDefault();
          if (e.dataTransfer) { e.dataTransfer.dropEffect = "move"; }
          body.appendChild(dragRow);
        }
      }
    });

    document.addEventListener("dragleave", function (e) {
      var over = e.target && e.target.closest
        ? e.target.closest("tr.track-row") : null;
      if (over) { over.classList.remove("drag-over"); }
    });

    document.addEventListener("dragend", function () {
      if (dragRow) {
        dragRow.classList.remove("dragging");
        // 跨碟拖动后, 每张碟的音轨号与碟号都要按当前 DOM 重排
        syncDiscNumbers();
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
        // 删过/移动过碟之后, 编号可能留下空洞(1,3), 提交前先按 DOM 顺序归一
        syncDiscNumbers();
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
    // 封面上传改由 cover-crop.js 接管(带裁切预览)
    // setupCoverUpload();
    // 新建专辑: 没有 key, 不能异步上传, 只选图 + 预览
    setupCoverPick();
    setupLocalPhotos();
    setupMergeDialog();
    setupTrackRows();
    setupPairRows();
    setupNoEnterSubmit();
    setupPeopleChips();
    setupCreditPeople();
    // setupCoverGallery();   // 同上: 由 cover-crop.js 接管
    settlePendingDraft();
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
    setupLinkAutoName();
    // 整段粘贴: 只在前端解析并填进下面的表格(暂存), 写库仍归最下面的「保存」
    setupPasteFill();
    // 详情页封面画廊(用户 m16568): 右侧点一下换下一张, 底部圆点直接跳
    setupDetailCover();
    // 出品信息: 同人展侧 / 独立同人侧 切换显示
    setupReleaseKind();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();