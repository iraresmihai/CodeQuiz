// Shared helpers for the CodeQuiz pages.

// If a page breaks, say so visibly instead of silently doing nothing.
window.addEventListener("error", e => showPageError(e.message));
window.addEventListener("unhandledrejection", e => showPageError(e.reason && e.reason.message || String(e.reason)));
function showPageError(msg) {
  if (!document.body) return;
  let bar = document.getElementById("pageError");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "pageError";
    bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:99;background:#e21b3c;color:#fff;padding:8px 14px;font:600 14px system-ui;";
    document.body.append(bar);
  }
  bar.textContent = "Something went wrong on this page: " + msg + " (try reloading)";
}

function el(tag, props = {}, ...kids) {
  const e = Object.assign(document.createElement(tag), props);
  for (const k of kids) if (k !== null && k !== undefined && k !== false) e.append(k);
  return e;
}

function fmtTime(sec) { sec = Math.max(0, Math.ceil(sec)); return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0"); }

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/**
 * A small, safe Markdown subset for question statements:
 *   # / ## / ### headings, - or * bullet lists, 1. numbered lists, > callout (e.g. the question to think about),
 *   ```code blocks```, `inline code`, **bold**, *italic*, [link](https://...),
 *   ![caption](img:IMAGE_ID) to place an uploaded image.
 * Everything is HTML-escaped first, so statements can't inject markup.
 * Returns { html, used } where used = the image ids placed in the text.
 */
function renderMarkdown(src, imageUrl) {
  const used = new Set();
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;

  function inline(t) {
    t = esc(t);
    const codes = [];
    t = t.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    t = t.replace(/!\[([^\]]*)\]\(img:([\w-]+)\)/g, (_, alt, id) => {
      const url = imageUrl(id);
      if (!url) return "";
      used.add(id);
      return `<img class="md-img" src="${esc(url)}" alt="${alt}">`;
    });
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    t = t.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    t = t.replace(/(^|[^*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1<i>$2</i>");
    t = t.replace(/\u0000(\d+)\u0000/g, (_, k) => `<code>${codes[+k]}</code>`);
    return t;
  }

  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre class="md-code">${esc(buf.join("\n"))}</pre>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    let m = line.match(/^(#{1,3})\s+(.*)$/);
    if (m) { const h = 2 + m[1].length; out.push(`<h${h}>${inline(m[2])}</h${h}>`); i++; continue; }
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        let item = lines[i++].replace(/^\s*[-*]\s+/, "");
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*[-*]\s+/.test(lines[i])) item += " " + lines[i++].trim();
        items.push(`<li>${inline(item)}</li>`);
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(/^\s*\d+[.)]\s+/, ""))}</li>`);
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(inline(lines[i++].replace(/^>\s?/, "")));
      out.push(`<div class="md-ask">${buf.join("<br>")}</div>`);
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,3}\s|\s*[-*]\s+|\s*\d+[.)]\s+|>)/.test(lines[i])) buf.push(inline(lines[i++]));
    out.push(`<p>${buf.join("<br>")}</p>`);
  }
  return { html: out.join("\n"), used };
}

/** Statement + images that the text doesn't place itself (shown below). */
function renderStatement(container, statement, images, urlOf) {
  const byId = Object.fromEntries((images || []).map(im => [im.id, im]));
  const { html, used } = renderMarkdown(statement, id => byId[id] ? urlOf(byId[id]) : null);
  container.innerHTML = html;
  for (const im of images || []) {
    if (!used.has(im.id)) container.append(el("img", { className: "md-img", src: urlOf(im), alt: im.name }));
  }
}

const MD_CSS = `
  .md h3 { font-size: 1.25em; margin: .8em 0 .3em; } .md h4 { font-size: 1.1em; margin: .8em 0 .3em; } .md h5 { font-size: 1em; margin: .8em 0 .3em; }
  .md p { margin: .5em 0; line-height: 1.45; } .md ul, .md ol { margin: .4em 0; padding-left: 1.4em; } .md li { margin: .25em 0; line-height: 1.4; }
  .md code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; background: rgba(70,23,143,.08); padding: .05em .35em; border-radius: 4px; font-size: .92em; }
  .md pre.md-code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .9em; background: #f6f4fb; border: 1px solid #e3def0;
                     padding: .6em .8em; border-radius: 8px; overflow-x: auto; white-space: pre; line-height: 1.35; }
  .md .md-ask { background: #fff6dc; border-left: 4px solid #ffb703; padding: .5em .8em; border-radius: 6px; margin: .6em 0; font-weight: 600; }
  .md img.md-img { display: block; max-width: 100%; height: auto; border-radius: 8px; margin: .6em 0; border: 1px solid #e3def0; }
  .md a { color: #46178f; }
`;
document.head.append(Object.assign(document.createElement("style"), { textContent: MD_CSS }));
