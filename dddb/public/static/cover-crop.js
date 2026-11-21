/* ==========================================================================
   封面与附图: 选择 -> 预览裁切 -> 上传
   ==========================================================================
   用户要求:
     "选择文件后弹出预览和框选范围界面再按上传，
      最后成功显示在封面槽位，而右侧加号是添加其他照片，
      其他照片不含封面，可以配备注"

   三个按钮(选择文件 / 上传封面 / 左侧大图加号)统一到同一个入口:
     click -> 选图 -> **裁切弹窗** -> 确认上传 -> 落到封面

   右侧加号 -> 选图 -> 裁切弹窗 -> 确认 -> 落到「其他照片」(可加备注)
   其他照片**永远不会**变成封面。
   ========================================================================== */
(function () {
  "use strict";

  var modal = null;          // 裁切弹窗
  var state = {
    kind: "cover",           // cover | photo
    form: null,
    file: null,
    img: null,
    scale: 1,
    minScale: 1,
    offset: {x: 0, y: 0},    // 图片相对视口的偏移
    box: {x: 0, y: 0, w: 0, h: 0},   // 裁切框(视口坐标)
    dragging: null,
    note: ""
  };

  /* ---- 工具 ---- */
  function $(s, root) { return (root || document).querySelector(s); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  /* ---- 裁切弹窗 ---- */
  function buildModal() {
    if (modal) { return modal; }
    modal = el("div", "crop-modal");
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-label", "裁切图片");

    var panel = el("div", "crop-panel");
    var head = el("div", "crop-head");
    head.appendChild(el("strong", null, "调整范围"));
    var closeBtn = el("button", "crop-x", "✕");
    closeBtn.type = "button";
    closeBtn.addEventListener("click", closeModal);
    head.appendChild(closeBtn);

    var stage = el("div", "crop-stage");
    var img = el("img", "crop-img");
    img.alt = "预览";
    img.draggable = false;
    var mask = el("div", "crop-mask");
    var frame = el("div", "crop-frame");
    ["nw", "ne", "sw", "se"].forEach(function (dir) {
      var h = el("span", "crop-handle crop-" + dir);
      h.setAttribute("data-dir", dir);
      frame.appendChild(h);
    });
    stage.appendChild(img);
    stage.appendChild(mask);
    stage.appendChild(frame);

    var zoomRow = el("div", "crop-zoom");
    var zoomOut = el("button", "ghost", "−");
    zoomOut.type = "button";
    var zoomIn = el("button", "ghost", "＋");
    zoomIn.type = "button";
    var zoomVal = el("span", "muted", "100%");
    zoomRow.appendChild(zoomOut);
    zoomRow.appendChild(zoomVal);
    zoomRow.appendChild(zoomIn);

    var noteRow = el("label", "field");
    noteRow.appendChild(el("span", null, "备注（可选）"));
    var noteInput = el("input");
    noteInput.type = "text";
    noteInput.placeholder = "如：会场照 / 特典图";
    noteInput.maxLength = 200;
    noteRow.appendChild(noteInput);

    var acts = el("div", "crop-acts");
    var cancel = el("button", "ghost", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", closeModal);
    var ok = el("button", "btn", "上传");
    ok.type = "button";
    ok.addEventListener("click", confirmUpload);
    acts.appendChild(cancel);
    acts.appendChild(ok);

    panel.appendChild(head);
    panel.appendChild(stage);
    panel.appendChild(zoomRow);
    panel.appendChild(noteRow);
    panel.appendChild(acts);
    modal.appendChild(panel);
    document.body.appendChild(modal);

    // 整框拖动(点框内非角区域)
    frame.addEventListener("mousedown", startDrag);
    frame.addEventListener("touchstart", startDrag, {passive: false});
    // **四角拖动缩放**: 之前只设了 data-dir, 没绑事件, 所以框是死的
    frame.querySelectorAll(".crop-handle").forEach(function (h) {
      h.addEventListener("mousedown", startResize);
      h.addEventListener("touchstart", startResize, {passive: false});
    });
    img.addEventListener("mousedown", startDrag);
    img.addEventListener("touchstart", startDrag, {passive: false});
    zoomIn.addEventListener("click", function () { setScale(state.scale * 1.15); });
    zoomOut.addEventListener("click", function () { setScale(state.scale / 1.15); });
    window.addEventListener("resize", function () {
      if (modal.classList.contains("on")) { layout(); }
    });

    modal._refs = {
      stage: stage, img: img, frame: frame, mask: mask,
      zoomVal: zoomVal, noteInput: noteInput, noteRow: noteRow, ok: ok
    };
    return modal;
  }

  function openModal(file, kind, form) {
    buildModal();
    state.kind = kind;
    state.form = form;
    state.file = file;
    state.scale = 1;
    state.offset = {x: 0, y: 0};
    state.note = "";

    var refs = modal._refs;
    refs.noteRow.style.display = kind === "photo" ? "block" : "none";
    refs.noteInput.value = "";
    modal.classList.add("on");
    document.body.classList.add("crop-open");

    // 先清掉上一次的尺寸, 避免残留 0 宽高
    refs.img.style.width = "";
    refs.img.style.height = "";
    refs.img.style.left = "";
    refs.img.style.top = "";
    state.img = null;

    // **不用 createObjectURL**: CSP 的 img-src 未必允许 blob:,
    // 被拦时 img.onerror 立刻触发 -> "图片读取失败"。改用 FileReader
    // 产出 data: URL(img-src 白名单本来就有 data:)。
    refs.img.onload = function () {
      state.img = refs.img;
      // **必须等 naturalWidth 有值**再算布局, 否则会算出 0 宽高 -> 图看不见
      var tries = 0;
      (function waitSize() {
        if (refs.img.naturalWidth > 0 && refs.img.naturalHeight > 0) {
          layout();
          return;
        }
        if (tries++ < 30) { setTimeout(waitSize, 30); }
      })();
    };
    refs.img.onerror = function () {
      alert("图片读取失败，请换一张试试");
      closeModal();
    };

    var reader = new FileReader();
    reader.onload = function () {
      refs.img.src = String(reader.result || "");
    };
    reader.onerror = function () {
      alert("图片读取失败，请换一张试试");
      closeModal();
    };
    reader.readAsDataURL(file);
  }

  function closeModal() {
    if (!modal) { return; }
    modal.classList.remove("on");
    document.body.classList.remove("crop-open");
    state.file = null;
    state.img = null;
  }

  /* ---- 布局: 计算最小缩放与裁切框 ---- */
  function layout() {
    if (!modal || !state.img) { return; }
    var refs = modal._refs;
    var stage = refs.stage;
    var sw = stage.clientWidth;
    var sh = stage.clientHeight;
    if (!sw || !sh) {
      setTimeout(layout, 40);
      return;
    }
    var iw = state.img.naturalWidth || 0;
    var ih = state.img.naturalHeight || 0;
    if (!iw || !ih) {
      // 图还没解码完 -> 稍后再来, 别用 1 当尺寸算出 0 宽的图
      setTimeout(layout, 40);
      return;
    }

    // 最小缩放 = 让图完全盖住裁切框(这里用 stage 的 80%)
    var boxW = Math.round(sw * 0.8);
    var boxH = Math.round(sh * 0.8);
    state.box = {
      x: Math.round((sw - boxW) / 2),
      y: Math.round((sh - boxH) / 2),
      w: boxW, h: boxH
    };
    state.minScale = Math.max(boxW / iw, boxH / ih);
    if (state.scale < state.minScale) { state.scale = state.minScale; }
    applyTransform();
    paintFrame();
  }

  function applyTransform() {
    var refs = modal._refs;
    var img = refs.img;
    var sw = refs.stage.clientWidth;
    var sh = refs.stage.clientHeight;
    var iw = img.naturalWidth || 0;
    var ih = img.naturalHeight || 0;
    if (!iw || !ih) { return; }     // 图还没解码完, 等 layout 重试
    var w = iw * state.scale;
    var h = ih * state.scale;

    // 不让图离开裁切框: 限制偏移
    var maxX = Math.max(0, (w - state.box.w) / 2);
    var maxY = Math.max(0, (h - state.box.h) / 2);
    state.offset.x = Math.min(maxX, Math.max(-maxX, state.offset.x));
    state.offset.y = Math.min(maxY, Math.max(-maxY, state.offset.y));

    var left = (sw - w) / 2 + state.offset.x;
    var top = (sh - h) / 2 + state.offset.y;
    img.style.width = w + "px";
    img.style.height = h + "px";
    img.style.left = left + "px";
    img.style.top = top + "px";
    refs.zoomVal.textContent = Math.round(state.scale / state.minScale * 100) + "%";
  }

  function paintFrame() {
    var refs = modal._refs;
    var b = state.box;
    var f = refs.frame;
    f.style.left = b.x + "px";
    f.style.top = b.y + "px";
    f.style.width = b.w + "px";
    f.style.height = b.h + "px";
  }

  function setScale(v) {
    if (!state.img) { return; }
    state.scale = Math.min(state.minScale * 4, Math.max(state.minScale, v));
    applyTransform();
  }

  /* ---- 四角拖动: 自由调整裁切范围 ---- */
  var MIN_BOX = 40;          // 最小边长, 免得拖成一条线

  function startResize(e) {
    if (!state.img || !modal) { return; }
    var dir = e.target && e.target.getAttribute
      ? e.target.getAttribute("data-dir") : "";
    if (!dir) { return; }
    e.preventDefault();
    e.stopPropagation();
    var pt = e.touches ? e.touches[0] : e;
    state.resizing = {
      dir: dir, x: pt.clientX, y: pt.clientY,
      box: {x: state.box.x, y: state.box.y,
            w: state.box.w, h: state.box.h}
    };
    window.addEventListener("mousemove", doResize);
    window.addEventListener("mouseup", endResize);
    window.addEventListener("touchmove", doResize, {passive: false});
    window.addEventListener("touchend", endResize);
  }

  function doResize(e) {
    if (!state.resizing) { return; }
    e.preventDefault();
    var pt = e.touches ? e.touches[0] : e;
    var dx = pt.clientX - state.resizing.x;
    var dy = pt.clientY - state.resizing.y;
    var b = state.resizing.box;
    var dir = state.resizing.dir;

    var x = b.x, y = b.y, w = b.w, h = b.h;
    if (dir.indexOf("w") >= 0) {         // 左边
      x = b.x + dx; w = b.w - dx;
    }
    if (dir.indexOf("e") >= 0) {         // 右边
      w = b.w + dx;
    }
    if (dir.indexOf("n") >= 0) {         // 上边
      y = b.y + dy; h = b.h - dy;
    }
    if (dir.indexOf("s") >= 0) {         // 下边
      h = b.h + dy;
    }

    // 最小尺寸: 拖过头就卡住, 而不是翻转
    if (w < MIN_BOX) {
      if (dir.indexOf("w") >= 0) { x = b.x + (b.w - MIN_BOX); }
      w = MIN_BOX;
    }
    if (h < MIN_BOX) {
      if (dir.indexOf("n") >= 0) { y = b.y + (b.h - MIN_BOX); }
      h = MIN_BOX;
    }

    // 不超出舞台
    var refs = modal._refs;
    var sw = refs.stage.clientWidth;
    var sh = refs.stage.clientHeight;
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > sw) { w = sw - x; }
    if (y + h > sh) { h = sh - y; }
    if (w < MIN_BOX) { w = MIN_BOX; }
    if (h < MIN_BOX) { h = MIN_BOX; }

    state.box = {x: Math.round(x), y: Math.round(y),
                 w: Math.round(w), h: Math.round(h)};
    paintFrame();
    // 框变了, 图片的可拖范围也要重算
    applyTransform();
  }

  function endResize() {
    state.resizing = null;
    window.removeEventListener("mousemove", doResize);
    window.removeEventListener("mouseup", endResize);
    window.removeEventListener("touchmove", doResize);
    window.removeEventListener("touchend", endResize);
  }

  /* ---- 拖动 ---- */
  function startDrag(e) {
    if (!state.img) { return; }
    // 点在角上 = 缩放, 不要同时触发整框拖动
    if (e.target && e.target.closest &&
        e.target.closest(".crop-handle")) {
      return;
    }
    var pt = e.touches ? e.touches[0] : e;
    state.dragging = {
      x: pt.clientX, y: pt.clientY,
      ox: state.offset.x, oy: state.offset.y
    };
    e.preventDefault();
    window.addEventListener("mousemove", doDrag);
    window.addEventListener("mouseup", endDrag);
    window.addEventListener("touchmove", doDrag, {passive: false});
    window.addEventListener("touchend", endDrag);
  }

  function doDrag(e) {
    if (!state.dragging) { return; }
    var pt = e.touches ? e.touches[0] : e;
    state.offset.x = state.dragging.ox + (pt.clientX - state.dragging.x);
    state.offset.y = state.dragging.oy + (pt.clientY - state.dragging.y);
    applyTransform();
    e.preventDefault();
  }

  function endDrag() {
    state.dragging = null;
    window.removeEventListener("mousemove", doDrag);
    window.removeEventListener("mouseup", endDrag);
    window.removeEventListener("touchmove", doDrag);
    window.removeEventListener("touchend", endDrag);
  }

  /* ---- 确认: canvas 裁切 -> 上传 ---- */
  function confirmUpload() {
    if (!state.img || !state.form) { return; }
    var refs = modal._refs;
    var box = state.box;
    var img = state.img;
    var sw = refs.stage.clientWidth;
    var sh = refs.stage.clientHeight;
    var w = img.naturalWidth * state.scale;
    var h = img.naturalHeight * state.scale;
    var left = (sw - w) / 2 + state.offset.x;
    var top = (sh - h) / 2 + state.offset.y;

    // 视口坐标 -> 原图坐标
    var sx = (box.x - left) / state.scale;
    var sy = (box.y - top) / state.scale;
    var sWidth = box.w / state.scale;
    var sHeight = box.h / state.scale;

    var outW = Math.min(1200, Math.round(sWidth));
    var outH = Math.round(outW * box.h / box.w);
    var canvas = document.createElement("canvas");
    canvas.width = outW;
    canvas.height = outH;
    var ctx = canvas.getContext("2d");
    ctx.drawImage(img, sx, sy, sWidth, sHeight, 0, 0, outW, outH);

    refs.ok.disabled = true;
    refs.ok.textContent = "上传中…";
    canvas.toBlob(function (blob) {
      if (!blob) {
        refs.ok.disabled = false;
        refs.ok.textContent = "上传";
        return;
      }
      var note = state.kind === "photo" ? refs.noteInput.value : "";
      uploadBlob(blob, state.kind, note).then(function (ok) {
        refs.ok.disabled = false;
        refs.ok.textContent = "上传";
        if (ok) { closeModal(); }
      });
    }, "image/jpeg", 0.9);
  }

  /* ---- 实际上传 ---- */
  function uploadBlob(blob, kind, note) {
    var form = state.form;
    var action = form.getAttribute(
      kind === "photo" ? "data-photo-action" : "data-cover-action") || "";
    var token = form.getAttribute("data-cover-token") || "";
    if (!action) {
      alert("缺少上传地址");
      return Promise.resolve(false);
    }
    var fd = new FormData();
    fd.append("csrf_token", token);
    fd.append("kind", kind);
    fd.append("note", note);
    fd.append(kind === "photo" ? "photo" : "cover", blob,
              (kind === "photo" ? "photo" : "cover") + ".jpg");

    return fetch(action, {
      method: "POST", body: fd, credentials: "same-origin"
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          throw new Error("HTTP " + r.status + " " + t.slice(0, 120));
        });
      }
      // 上传成功后**整页刷新** —— 服务端会把新图/附图渲染出来。
      // 这比在 DOM 里拼状态可靠得多(之前就是这里出问题)。
      var next = form.getAttribute("data-cover-next") ||
                 location.pathname;
      var sep = next.indexOf("?") >= 0 ? "&" : "?";
      location.href = next + sep + "t=" + Date.now();
      return true;
    }).catch(function (e) {
      alert("上传失败：" + e.message);
      return false;
    });
  }

  /* ---- 绑定三个入口 ---- */
  function bind() {
    var form = document.getElementById("cover-form");
    if (!form) { return; }
    var fileInput = document.getElementById("cover-input");
    if (!fileInput) { return; }
    var kind = "cover";

    // 统一的"选图"入口
    function pick(which) {
      kind = which;
      fileInput.value = "";
      fileInput.click();
    }

    fileInput.addEventListener("change", function () {
      var f = fileInput.files && fileInput.files[0];
      if (!f) { return; }
      if (f.size > 4 * 1024 * 1024) {
        alert("图片不能超过 4MB");
        fileInput.value = "";
        return;
      }
      openModal(f, kind, form);
    });

    // ① 「选择文件」原生 input —— 直接走 change(由浏览器自己触发)
    // ② 「上传封面」按钮
    var upBtn = form.querySelector("[data-cover-upload]");
    if (upBtn) {
      upBtn.addEventListener("click", function (e) {
        e.preventDefault();
        pick("cover");
      });
    }
    // ③ **大加号 = 添加照片**(用户要求: 其他照片的添加途径就是大加号)
    var addBtn = document.querySelector("[data-cover-add]");
    if (addBtn) {
      addBtn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        pick("photo");
      });
    }
    // ③b 左侧大图 = 换封面(点当前封面图)
    var thumbWrap = document.querySelector(".edit-cover-thumb");
    if (thumbWrap) {
      thumbWrap.style.cursor = "pointer";
      thumbWrap.title = "点击更换封面";
      thumbWrap.addEventListener("click", function (e) {
        if (e.target.closest && e.target.closest("[data-cover-add]")) {
          return;
        }
        e.preventDefault();
        pick("cover");
      });
    }

    // 附图上的删除 / 改备注
    document.querySelectorAll("[data-photo-del]").forEach(function (b) {
      b.addEventListener("click", function () {
        var url = b.getAttribute("data-photo-del");
        if (!url || !confirm("删除这张照片？")) { return; }
        var fd = new FormData();
        fd.append("csrf_token", form.getAttribute("data-cover-token") || "");
        fd.append("url", url);
        fetch(form.getAttribute("data-photo-del-action") || "", {
          method: "POST", body: fd, credentials: "same-origin"
        }).then(function () { location.reload(); });
      });
    });
    document.querySelectorAll("[data-photo-note]").forEach(function (inp) {
      inp.addEventListener("change", function () {
        var fd = new FormData();
        fd.append("csrf_token", form.getAttribute("data-cover-token") || "");
        fd.append("url", inp.getAttribute("data-photo-note"));
        fd.append("note", inp.value);
        fetch(form.getAttribute("data-photo-note-action") || "", {
          method: "POST", body: fd, credentials: "same-origin"
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
