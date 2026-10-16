// 照片燈箱、搜尋關鍵字標示、首頁全文搜尋。全部離線可用（file:// 開啟也行）。
(function () {
  "use strict";

  // ---- 燈箱：點照片放大，左右鍵切換，Esc 關閉；燈箱裡再點照片可以放大／縮回 ----
  const photos = Array.from(document.querySelectorAll("a.photo"));
  if (photos.length) {
    const box = document.createElement("div");
    box.className = "lightbox";
    box.innerHTML = '<button type="button" aria-label="關閉">×</button><img alt="">';
    document.body.appendChild(box);
    const img = box.querySelector("img");
    let current = -1;
    const unzoom = () => { box.classList.remove("zoomed"); img.style.width = img.style.height = ""; };
    const show = (i) => {
      current = (i + photos.length) % photos.length;
      unzoom();
      img.src = photos[current].getAttribute("href");
      box.classList.add("open");
    };
    const hide = () => { unzoom(); box.classList.remove("open"); img.removeAttribute("src"); current = -1; };
    // 放大：窄長的照片放大到畫面寬度、上下捲動；扁長的放大到畫面高度、左右捲動；
    // 和畫面比例差不多的放大兩倍。點的位置放大後仍在手指下。
    const zoom = (e) => {
      const r = img.getBoundingClientRect();
      const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
      const vw = box.clientWidth, vh = box.clientHeight;
      let w = vw, h = vw * img.naturalHeight / img.naturalWidth;      // 放大到畫面寬度
      if (h < vh * 1.2) {                                          // 不夠高：改成放大到畫面高度
        h = vh; w = vh * img.naturalWidth / img.naturalHeight;
      }
      if (w < r.width * 1.5 && h < r.height * 1.5) { w = r.width * 2; h = r.height * 2; }
      box.classList.add("zoomed");
      img.style.width = w + "px";
      img.style.height = h + "px";
      box.scrollLeft = Math.max(0, fx * w - e.clientX);
      box.scrollTop = Math.max(0, fy * h - e.clientY);
    };
    photos.forEach((a, i) => a.addEventListener("click", (e) => { e.preventDefault(); show(i); }));
    img.addEventListener("click", (e) => {
      e.stopPropagation();
      if (box.classList.contains("zoomed")) unzoom(); else zoom(e);
    });
    box.addEventListener("click", (e) => { if (e.target !== img) hide(); });
    document.addEventListener("keydown", (e) => {
      if (current < 0) return;
      if (e.key === "Escape") hide();
      if (e.key === "ArrowRight") show(current + 1);
      if (e.key === "ArrowLeft") show(current - 1);
    });
  }

  // ---- 動圖（被 LINE 轉成 MP4 的 GIF）：捲到畫面上才播放，離開就暫停 ----
  const anims = document.querySelectorAll("video.anim");
  if (anims.length && "IntersectionObserver" in window) {
    const watcher = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) e.target.play().catch(() => {});
        else e.target.pause();
      });
    });
    anims.forEach((v) => watcher.observe(v));
  }

  // ---- 從搜尋結果點進來時：顯示「返回搜尋結果」並標示關鍵字 ----
  const params = new URLSearchParams(location.search);
  const q = (params.get("q") || "").trim();
  const chat = document.querySelector("main.chat");
  if (q && chat) {
    const back = "index.html?q=" + encodeURIComponent(q);
    const bar = document.createElement("a");
    bar.className = "back-to-results";
    bar.href = back;
    bar.textContent = "‹ 返回「" + q + "」的搜尋結果";
    chat.prepend(bar);
    document.querySelectorAll('header.bar a[href="index.html"]').forEach((a) => { a.href = back; });

    const lower = q.toLowerCase();
    document.querySelectorAll(".bubble").forEach((el) => {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      nodes.forEach((node) => {
        const text = node.nodeValue;
        const at = text.toLowerCase().indexOf(lower);
        if (at < 0) return;
        const mark = document.createElement("mark");
        const after = node.splitText(at);
        after.splitText(q.length);
        mark.textContent = after.nodeValue;
        after.parentNode.replaceChild(mark, after);
      });
    });
  }

  // ---- 首頁全文搜尋（資料在 search-index.js 的 window.SEARCH_INDEX） ----
  const input = document.querySelector("#q");
  const results = document.querySelector("#results");
  const index = window.SEARCH_INDEX || [];
  if (input && results) {
    const escape = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const render = () => {
      const term = input.value.trim().toLowerCase();
      results.innerHTML = "";
      if (!term) return;
      let count = 0;
      const frag = document.createDocumentFragment();
      for (const [page, id, date, time, sender, text] of index) {
        const at = text.toLowerCase().indexOf(term);
        if (at < 0) continue;
        count += 1;
        if (count > 300) continue;
        const start = Math.max(0, at - 30);
        const snippet = escape(text.slice(start, at)) + "<mark>" + escape(text.slice(at, at + term.length)) +
          "</mark>" + escape(text.slice(at + term.length, at + term.length + 60));
        const a = document.createElement("a");
        a.className = "hit";
        a.href = page + "?q=" + encodeURIComponent(input.value.trim()) + "#m" + id;
        a.innerHTML = "<small>" + escape(date + " " + time + "　" + sender) + "</small>" + (start ? "…" : "") + snippet;
        frag.appendChild(a);
      }
      const head = document.createElement("p");
      head.className = "summary";
      head.textContent = count ? `找到 ${count} 則${count > 300 ? "（只列出前 300 則）" : ""}` : "沒有找到";
      results.appendChild(head);
      results.appendChild(frag);
    };
    // 把關鍵字寫進網址：從訊息頁按「上一頁」或「返回搜尋結果」回來時，結果還在
    let timer = null;
    input.addEventListener("input", () => {
      render();
      clearTimeout(timer);
      timer = setTimeout(() => {
        const term = input.value.trim();
        history.replaceState(null, "", term ? "?q=" + encodeURIComponent(term) : location.pathname);
      }, 300);
    });
    if (q) { input.value = q; render(); }
  }
})();
