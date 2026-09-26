(function () {
  function ready(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  function norm(url) {
    var path = new URL(url, window.location.href).pathname;
    if (path.endsWith("/index.html")) path = path.slice(0, -"/index.html".length);
    if (!path.endsWith("/")) path += "/";
    return path;
  }

  function setupTree() {
    var tree = document.querySelector(".dir-tree");
    if (!tree) return;
    var here = norm(window.location.href);
    tree.querySelectorAll(":scope > ul").forEach(function (ul) {
      var item = ul.querySelector(":scope > li");
      if (!item) return;
      var children = item.querySelectorAll(":scope > ul");
      var link = item.querySelector(":scope > a");
      if (link && children.length) {
        link.addEventListener("click", function (event) {
          event.preventDefault();
          var collapsed = children[0].classList.contains("collapsed");
          children.forEach(function (child) {
            child.classList.toggle("collapsed", !collapsed);
            child.style.height = collapsed ? "auto" : "0";
          });
        });
      }
    });
    tree.querySelectorAll("a").forEach(function (link) {
      if (norm(link.href) !== here) return;
      if (link.parentElement.querySelector(":scope > ul")) return;
      link.classList.add("link-active");
      link.scrollIntoView({ block: "center", inline: "nearest" });
    });
  }

  function setupToc() {
    var nav = document.querySelector(".header-navigator");
    if (!nav || !nav.querySelector("a")) return;

    function place() {
      nav.style.display = window.innerWidth > 768 ? "block" : "none";
    }
    place();
    window.addEventListener("resize", place);

    nav.querySelectorAll('a[href^="#"]').forEach(function (link) {
      link.addEventListener("click", function (event) {
        var target = document.querySelector(link.getAttribute("href"));
        if (!target) return;
        event.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        history.pushState(null, "", link.getAttribute("href"));
      });
    });

    var links = Array.prototype.slice.call(nav.querySelectorAll('a[href^="#"]'));
    var heads = links.map(function (link) {
      return document.querySelector(link.getAttribute("href"));
    }).filter(Boolean);

    function mark() {
      var current = heads[0];
      var line = window.scrollY + 96;
      heads.forEach(function (head) {
        if (head.offsetTop <= line) current = head;
      });
      links.forEach(function (link) {
        link.classList.toggle("link-active", current && link.getAttribute("href") === "#" + current.id);
      });
    }
    mark();
    window.addEventListener("scroll", mark, { passive: true });
  }

  ready(function () {
    setupTree();
    setupToc();
  });
})();
