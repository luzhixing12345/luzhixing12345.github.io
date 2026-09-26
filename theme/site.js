(function () {
  var copyIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';
  var checkIcon = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

  function ready(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  function setupCopy() {
    document.querySelectorAll("pre > code").forEach(function (code) {
      var pre = code.parentElement;
      if (!pre || pre.querySelector(".copy-btn")) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "copy-btn";
      button.setAttribute("aria-label", "复制代码");
      button.innerHTML = copyIcon;
      button.addEventListener("click", function () {
        var text = code.innerText.replace(/\n$/, "");
        var done = function () {
          button.innerHTML = checkIcon;
          setTimeout(function () { button.innerHTML = copyIcon; }, 1200);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done).catch(function () {});
        }
      });
      pre.appendChild(button);
    });
  }

  function setupImages() {
    var viewer = document.createElement("div");
    viewer.className = "img-viewer";
    viewer.hidden = true;
    var picture = document.createElement("img");
    picture.alt = "";
    viewer.appendChild(picture);
    document.body.appendChild(viewer);

    function close() { viewer.hidden = true; picture.removeAttribute("src"); }
    viewer.addEventListener("click", close);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") close();
    });

    document.querySelectorAll(".prose img, .markdown-body img").forEach(function (img) {
      img.addEventListener("click", function (event) {
        event.preventDefault();
        picture.src = img.currentSrc || img.src;
        picture.alt = img.alt || "";
        viewer.hidden = false;
      });
    });
  }

  function setupMap() {
    var dataEl = document.getElementById("travel-data");
    var svg = document.getElementById("china-map");
    if (!dataEl || !svg) return;
    var places = JSON.parse(dataEl.textContent);
    var tip = document.getElementById("map-tip");
    var hideTimer = 0;
    var pinned = false;

    function fillTip(info) {
      tip.replaceChildren();
      var name = document.createElement("p");
      name.className = "tip-name";
      name.textContent = info.name;
      tip.appendChild(name);
      info.articles.forEach(function (article) {
        var link = document.createElement("a");
        link.href = article.href;
        link.textContent = article.title;
        tip.appendChild(link);
      });
    }

    function move(event) {
      if (tip.hidden) return;
      var pad = 14;
      tip.style.left = (event.clientX + pad) + "px";
      tip.style.top = (event.clientY + pad) + "px";
      var rect = tip.getBoundingClientRect();
      if (rect.right > window.innerWidth - 8) {
        tip.style.left = (event.clientX - rect.width - pad) + "px";
      }
      if (rect.bottom > window.innerHeight - 8) {
        tip.style.top = (event.clientY - rect.height - pad) + "px";
      }
    }

    function show(event, info) {
      fillTip(info);
      tip.hidden = false;
      move(event);
    }

    function scheduleHide() {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(function () {
        if (!pinned) tip.hidden = true;
      }, 160);
    }

    svg.querySelectorAll(".province").forEach(function (shape) {
      shape.addEventListener("mouseenter", function (event) {
        var info = places[shape.dataset.id];
        if (!info) return;
        pinned = false;
        clearTimeout(hideTimer);
        show(event, info);
      });
      shape.addEventListener("mousemove", function (event) {
        if (!pinned) move(event);
      });
      shape.addEventListener("mouseleave", scheduleHide);
      shape.addEventListener("click", function (event) {
        var info = places[shape.dataset.id];
        if (!info || !info.articles.length) return;
        if (info.articles.length === 1) {
          window.location.href = info.articles[0].href;
          return;
        }
        pinned = true;
        show(event, info);
      });
    });

    tip.addEventListener("mouseenter", function () { clearTimeout(hideTimer); });
    tip.addEventListener("mouseleave", function () {
      pinned = false;
      scheduleHide();
    });
    document.addEventListener("click", function (event) {
      if (!svg.contains(event.target) && !tip.contains(event.target)) {
        pinned = false;
        tip.hidden = true;
      }
    });
  }

  ready(function () {
    setupCopy();
    setupImages();
    setupMap();
  });
})();
