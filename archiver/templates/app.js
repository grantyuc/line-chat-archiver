// 照片燈箱、搜尋關鍵字標示、首頁全文搜尋。全部離線可用（file:// 開啟也行）。
(function () {
  "use strict";

  // ---- 燈箱：點照片放大，左右鍵切換，Esc 關閉 ----
  const photos = Array.from(document.querySelectorAll("a.photo"));
  if (photos.length) {
    const box = document.createElement("div");
    box.className = "lightbox";
    box.innerHTML = '<button type="button" aria-label="關閉">×</button><img alt="">';
    document.body.appendChild(box);
    const img = box.querySelector("img");
    let current = -1;
    const show = (i) => {
      current = (i + photos.length) % photos.length;
      img.src = photos[current].getAttribute("href");
      box.classList.add("open");
    };
    const hide = () => { box.classList.remove("open"); img.removeAttribute("src"); current = -1; };
    photos.forEach((a, i) => a.addEventListener("click", (e) => { e.preventDefault(); show(i); }));
    box.addEventListener("click", (e) => { if (e.target !== img) hide(); });
    document.addEventListener("keydown", (e) => {
      if (current < 0) return;
      if (e.key === "Escape") hide();
      if (e.key === "ArrowRight") show(current + 1);
      if (e.key === "ArrowLeft") show(current - 1);
    });
  }

  // ---- 從搜尋結果點進來時，標示關鍵字 ----
  const params = new URLSearchParams(location.search);
  const q = (params.get("q") || "").trim();
  if (q && document.querySelector("main.chat")) {
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
    input.addEventListener("input", render);
    if (q) { input.value = q; render(); }
  }
})();
