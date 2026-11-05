#!/usr/bin/env python3
"""
md2html_offline.py – convert an Obsidian-style Markdown **folder tree** into a
fully offline, self-contained HTML knowledge base.

    pip install markdown-it-py mdit-py-plugins pyyaml linkify-it-py

    python md2html_offline.py notes_root/          # → notes_root/html_out/
    python md2html_offline.py notes_root/ -o out/  # custom output folder

The script:
  • walks the whole tree (folders, .md files, assets)
  • builds a single index.html with a searchable collapsible folder tree
  • downloads MathJax and Mermaid **once** into _libs/ and references
    them locally → no CDN needed at runtime
  • converts each .md to .html preserving the folder structure
  • keeps all Obsidian extensions: [[wikilinks]], ![[embeds]], callouts,
    highlights, tags, block-ids, YAML properties, $math$, ```mermaid,
    RTL/LTR detection, TOC, font embedding.
  • note pages get a sticky sidebar TOC (desktop) with active-section
    tracking + a back-to-top button
  • images can be opened in a fullscreen in-page lightbox (no OS fullscreen,
    no CDN — pure inline HTML/CSS/JS)
"""

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import unicodedata
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote

# ---------------------------------------------------------------------------
# markdown-it setup
# ---------------------------------------------------------------------------
try:
    from markdown_it import MarkdownIt
    from mdit_py_plugins.dollarmath import dollarmath_plugin
    from mdit_py_plugins.footnote import footnote_plugin
    from mdit_py_plugins.tasklists import tasklists_plugin
except ImportError:
    sys.exit("Missing packages. Run: pip install markdown-it-py mdit-py-plugins pyyaml linkify-it-py")

esc = html.escape

# ---------------------------------------------------------------------------
# page style
# ---------------------------------------------------------------------------
FONT = '"BNazanin", "B Nazanin", Tahoma, sans-serif'

# ─── Optimised CSS ─────────────────────────────────────────────────────────
# Design tokens · quiet borders · minimal headings · horizontal-chip TOC on
# mobile, sticky vertical TOC on desktop · circular back-to-top · rounded
# code/search/tree chrome. Preserves every class the generator emits.
CSS_T = """
:root{
  --bg:#fff;--fg:#18181b;--muted:#71717a;--border:#e4e4e7;--code-bg:#fafafa;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#0b0b0e;--fg:#e4e4e7;--muted:#a1a1aa;--border:#27272a;--code-bg:#141417;
    color-scheme:dark;
  }
}
:root[data-theme=dark]{
  --bg:#0b0b0e;--fg:#e4e4e7;--muted:#a1a1aa;--border:#27272a;--code-bg:#141417;
  color-scheme:dark;
}
*{box-sizing:border-box;border-radius:0}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}

body{
  margin:0;background:var(--bg);color:var(--fg);
  font:17px/1.75 FONT;overflow-wrap:break-word;
  -webkit-font-smoothing:antialiased;
}
@media (min-width:60rem){body{font-size:18px}}

main{max-width:52rem;margin:0 auto;padding:0 1rem 4rem}
main:has(.layout){max-width:72rem}

/* ─── Top nav (scoped so it cannot bleed into .toc) ──────────────── */
main>nav{
  display:flex;justify-content:space-between;align-items:center;
  padding:.75rem 0;margin-bottom:1.5rem;font-size:.9em;
  border-bottom:1px solid var(--border);gap:.5rem;flex-wrap:wrap;
}
main>nav a{color:var(--muted);text-decoration:none}
main>nav a:hover{color:var(--fg)}

/* ─── Buttons / links ────────────────────────────────────────────── */
button{
  font:inherit;color:var(--fg);background:transparent;
  border:1px solid var(--border);padding:.35rem .7rem;cursor:pointer;
  transition:background .15s,border-color .15s;
}
button:hover{background:var(--code-bg);border-color:var(--fg)}

a{
  color:var(--fg);text-decoration:underline;text-underline-offset:.2em;
  text-decoration-color:var(--border);text-decoration-thickness:1px;
}
a:hover{text-decoration-color:var(--fg)}
a.unresolved{text-decoration-style:dotted}

/* ─── Headings ───────────────────────────────────────────────────── */
h1,h2,h3,h4,h5,h6{
  line-height:1.35;margin:2rem 0 .75rem;font-weight:700;
  scroll-margin-top:1rem;
}
h1{font-size:1.6em;padding-bottom:.3em;border-bottom:2px solid var(--fg)}
h2{font-size:1.3em;padding-bottom:.2em;border-bottom:1px solid var(--border)}
h3{font-size:1.15em;border-inline-start:3px solid var(--fg);padding-inline-start:.75rem}
h4,h5,h6{font-size:1em}

/* ─── Text blocks ────────────────────────────────────────────────── */
p,ul,ol{margin:.85rem 0}
li{margin:.2rem 0}
ul,ol{padding-inline-start:1.5rem}
hr{border:0;border-top:1px solid var(--border);margin:2rem 0}
img,video{max-width:100%;height:auto;display:block}
mark{background:var(--fg);color:var(--bg);padding:0 .15em;border-radius:2px}
.tag{border:1px solid var(--border);padding:0 .35em;font-size:.85em;border-radius:2px;color:var(--muted)}
blockquote{
  margin:1rem 0;padding:.1rem 1rem;
  border-inline-start:3px solid var(--border);color:var(--muted);
}

/* ─── Callouts ───────────────────────────────────────────────────── */
.callout,details.callout{border:1px solid var(--border);border-radius:4px;margin:1rem 0}
.callout-title,details.callout>summary{font-weight:700;padding:.4rem .8rem}
.callout-body{padding:.1rem .8rem;border-top:1px solid var(--border)}
details.callout[open]>summary{border-bottom:1px solid var(--border)}
details.callout>summary{cursor:pointer}
.callout-body>:first-child{margin-top:.6rem}
.callout-body>:last-child{margin-bottom:.6rem}

/* ─── Embeds ─────────────────────────────────────────────────────── */
.embed{border-inline-start:3px solid var(--border);padding:0 1rem;margin:1rem 0}

/* ─── Tables ─────────────────────────────────────────────────────── */
.tw{overflow-x:auto;margin:1rem 0;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.92em}
td,th{border:1px solid var(--border);padding:.55rem .7rem;vertical-align:top;min-width:6rem}
th{background:var(--code-bg);font-weight:700;text-align:start}
@media (max-width:40rem){
  td,th{padding:.4rem .5rem;min-width:5rem;font-size:.9em}
}

/* ─── Code ───────────────────────────────────────────────────────── */
code{
  font:.88em Consolas,Menlo,monospace;background:var(--code-bg);
  border:1px solid var(--border);padding:.05em .3em;border-radius:3px;
}
pre{
  background:var(--code-bg);border:1px solid var(--border);border-radius:4px;
  padding:.75rem 1rem;overflow-x:auto;direction:ltr;text-align:left;line-height:1.55;
}
pre code{border:0;padding:0;background:none;direction:inherit;unicode-bidi:inherit;font-size:.9em}
pre.mermaid{
  border:0;background:transparent;text-align:center;direction:ltr;
  padding:0;overflow:visible;margin:1rem 0;
}
pre.mermaid svg{max-width:100%;height:auto}
pre.mermaid text,pre.mermaid foreignObject{unicode-bidi:plaintext}

/* ─── Math / Props / Foot / misc ─────────────────────────────────── */
.math{direction:ltr;unicode-bidi:isolate;display:inline-block}
dl.props{
  border:1px solid var(--border);border-radius:4px;
  padding:.6rem .85rem;margin:0 0 1.5rem;font-size:.9em;
  background:var(--code-bg);
}
dl.props dt{font-weight:700;color:var(--muted);font-size:.9em}
dl.props dd{margin:0 0 .4rem}
input[type=checkbox]{accent-color:var(--fg)}
.footnotes{font-size:.9em;color:var(--muted)}
.en{font-family:"Times New Roman",Times,"Liberation Serif",serif}
[dir="rtl"]{text-align:right}
[dir="ltr"]{text-align:left}

/* ─── Layout with TOC ────────────────────────────────────────────── */
.layout{display:grid;grid-template-columns:1fr;gap:1.25rem;margin-bottom:2rem}
@media (min-width:60rem){
  .layout{grid-template-columns:16rem minmax(0,1fr);gap:2rem}
  .toc-panel{
    position:sticky;top:1rem;align-self:start;
    max-height:calc(100vh - 2rem);overflow-y:auto;padding-inline-end:.25rem;
  }
}

/* ─── TOC (desktop) ──────────────────────────────────────────────── */
.toc{
  border:1px solid var(--border);border-radius:4px;
  padding:.75rem .85rem;margin:0;background:var(--bg);
  font-size:.92em;line-height:1.5;
}
.toc-title{
  font-weight:700;font-size:.9em;color:var(--muted);
  margin:0 0 .6rem;padding:0 0 .4rem .2rem;
  border-bottom:1px solid var(--border);
}
.toc ul{list-style:none;padding:0;margin:0}
.toc li{margin:0}
.toc a{
  display:block;text-decoration:none;padding:.25rem .4rem;
  border-inline-start:2px solid transparent;border-radius:2px;
  color:var(--fg);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  transition:border-color .15s,background .15s;
}
.toc a:hover{background:var(--code-bg);border-inline-start-color:var(--border)}
.toc a.active{font-weight:600;border-inline-start-color:var(--fg);background:var(--code-bg)}
.toc ul ul{padding-inline-start:.65rem}
.toc ul ul a{font-size:.95em;color:var(--muted)}
.toc ul ul a.active{color:var(--fg)}
.toc ul ul ul a{font-size:.9em}

/* ─── TOC (mobile – full hierarchical vertical list) ────────────── */
@media (max-width:59.99rem){
  .toc{max-height:40vh;overflow-y:auto;-webkit-overflow-scrolling:touch}
}

/* ─── Back-to-top (rectangular) ─────────────────────────────────── */
.back-top{
  position:fixed;inset-block-end:1.2rem;inset-inline-end:1.2rem;
  width:2.6rem;height:2.6rem;display:flex;align-items:center;justify-content:center;
  background:var(--fg);color:var(--bg);border:0;
  cursor:pointer;opacity:0;pointer-events:none;font-size:1.3em;z-index:10;
  transition:opacity .2s;
}
.back-top.visible{opacity:.85;pointer-events:auto}
.back-top:hover{opacity:1}

/* ─── Index: search + folder tree ───────────────────────────────── */
.kb-search{display:flex;gap:.5rem;margin:0 0 1rem;flex-wrap:wrap}
.kb-search input[type=search]{
  flex:1;min-width:10rem;font:inherit;color:var(--fg);background:var(--bg);
  border:1px solid var(--border);border-radius:4px;padding:.5rem .7rem;
}
.kb-search input[type=search]:focus{
  outline:2px solid var(--fg);outline-offset:-1px;border-color:var(--fg);
}
.kb-search button{
  font:inherit;color:var(--fg);background:transparent;
  border:1px solid var(--border);border-radius:4px;
  padding:.5rem .8rem;cursor:pointer;white-space:nowrap;
}
.kb-search button:hover{background:var(--code-bg);border-color:var(--fg)}
.kb-subtitle{margin:-.5rem 0 1rem;color:var(--muted);font-size:.9em}

.folder-tree{list-style:none;padding:0;margin:0;line-height:1.6}
.folder-tree ul{list-style:none;padding:0;margin:0;padding-inline-start:1.3rem}
.folder-tree li{margin:0}
.folder-tree details>summary{
  list-style:none;cursor:pointer;padding:.25rem .4rem;
  display:flex;align-items:center;gap:.35rem;user-select:none;
  border-inline-start:2px solid transparent;border-radius:2px;
}
.folder-tree details>summary::-webkit-details-marker{display:none}
.folder-tree details>summary::before{
  content:"\\25B8";display:inline-block;font-size:.72em;width:.9em;flex-shrink:0;
  transition:transform .15s;opacity:.6;
}
.folder-tree details[open]>summary::before{transform:rotate(90deg)}
.folder-tree details>summary:hover{background:var(--code-bg);border-inline-start-color:var(--border)}
.folder-tree details>summary:hover::before{opacity:1}
.folder-tree summary>.name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.folder-tree summary>.count{
  font-size:.75em;color:var(--muted);border:1px solid var(--border);
  padding:0 .45em;margin-inline-start:auto;border-radius:99px;
  line-height:1.5;flex-shrink:0;
}
.folder-tree li.leaf>a{
  display:flex;align-items:center;gap:.35rem;padding:.2rem .4rem;
  text-decoration:none;border-inline-start:2px solid transparent;
  border-radius:2px;overflow:hidden;
}
.folder-tree li.leaf>a::before{
  content:"\\00B7";width:.9em;flex-shrink:0;opacity:.4;text-align:center;
}
.folder-tree li.leaf>a>span{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;
}
.folder-tree li.leaf>a:hover{background:var(--code-bg);border-inline-start-color:var(--border)}
.folder-tree li.leaf>a:hover::before{content:"\\203A";opacity:1}
.kb-empty{color:var(--muted);font-style:italic}

/* ─── Lightbox ───────────────────────────────────────────────────── */
main img{cursor:zoom-in}
a img{cursor:pointer}
.lightbox{
  position:fixed;inset:0;z-index:100;
  background:rgba(0,0,0,.9);
  display:flex;align-items:center;justify-content:center;
  padding:1rem;cursor:zoom-out;
  opacity:0;pointer-events:none;transition:opacity .2s;
}
.lightbox.open{opacity:1;pointer-events:auto}
.lightbox img{
  max-width:95vw;max-height:95vh;width:auto;height:auto;
  object-fit:contain;cursor:default;
  box-shadow:0 10px 40px rgba(0,0,0,.6);
}
.lightbox .lb-close{
  position:absolute;top:.75rem;inset-inline-end:.75rem;
  background:rgba(255,255,255,.15);color:#fff;border:0;
  width:2.6rem;height:2.6rem;font-size:1.4em;line-height:1;
  cursor:pointer;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
}
.lightbox .lb-close:hover{background:rgba(255,255,255,.3)}
body.lb-open{overflow:hidden}
"""

# ---------------------------------------------------------------------------
# font embedding
# ---------------------------------------------------------------------------
FONT_NAMES = ("iransharp", "nazanin")
FONT_FMT = {".ttf": "truetype", ".otf": "opentype", ".woff": "woff", ".woff2": "woff2"}
WEIGHT_WORDS = ("bold", "light", "black", "medium", "thin", "heavy", "ultra", "italic", "fa")
FONT_CSS = ""
FONT_STACK = FONT

def font_dirs():
    home = Path.home()
    dirs = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
        home / "Library/Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"),
        home / ".fonts", home / ".local/share/fonts",
        Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
    ]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Windows/Fonts")
    return [d for d in dirs if d.is_dir()]

def find_fonts():
    for name in FONT_NAMES:
        found = []
        for d in font_dirs():
            for f in d.rglob("*"):
                key = re.sub(r"[\s_-]", "", f.stem.lower())
                if f.suffix.lower() in FONT_FMT and name in key:
                    found.append(f)
        if found:
            bold = [f for f in found if "bold" in f.stem.lower()]
            plain = [f for f in found if not any(w in f.stem.lower() for w in WEIGHT_WORDS)]
            regular = min(plain or found, key=lambda f: len(f.stem))
            return regular, (min(bold, key=lambda f: len(f.stem)) if bold else None)
    return None, None

def font_face_css(regular, bold):
    rules = ""
    for path, weight in ((regular, "normal"), (bold, "bold")):
        if path:
            ext = path.suffix.lower()
            data = base64.b64encode(Path(path).read_bytes()).decode()
            rules += (
                f'@font-face{{font-family:"Embedded";font-weight:{weight};'
                f'src:url(data:font/{ext[1:]};base64,{data}) format("{FONT_FMT[ext]}")}}\n'
            )
    return rules

def page_css():
    return FONT_CSS + CSS_T.replace("FONT", FONT_STACK)

# ---------------------------------------------------------------------------
# PAGE template
# ---------------------------------------------------------------------------
PAGE = """<!doctype html>
<html dir="ltr" lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<main>
<nav><span>{back}</span><button id="theme" type="button">&#9680;</button></nav>
{content}
</main>
{backtop}
{lightbox}
<script>
(function(){{var r=document.documentElement,k="theme";
try{{var s=localStorage.getItem(k);if(s)r.dataset.theme=s}}catch(e){{}}
document.getElementById("theme").onclick=function(){{
var dark=r.dataset.theme?r.dataset.theme=="dark":matchMedia("(prefers-color-scheme:dark)").matches;
r.dataset.theme=dark?"light":"dark";try{{localStorage.setItem(k,r.dataset.theme)}}catch(e){{}}}}}})();
</script>
{scripts}
</body>
</html>
"""

BACKTOP_BUTTON = (
    '<button class="back-top" id="back-top" type="button" aria-label="Back to top">&uarr;</button>'
    '<script>'
    '(function(){var b=document.getElementById("back-top");if(!b)return;'
    'function c(){b.classList.toggle("visible",window.scrollY>400)}'
    'window.addEventListener("scroll",c,{passive:true});'
    'b.onclick=function(){window.scrollTo({top:0,behavior:"smooth"})};c()})();'
    '</script>'
)

LIGHTBOX = (
    '<div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="Image preview">'
    '<button class="lb-close" id="lb-close" type="button" aria-label="Close">&times;</button>'
    '<img id="lb-img" src="" alt="">'
    '</div>'
    '<script>'
    '(function(){'
    'var lb=document.getElementById("lightbox");'
    'var lbImg=document.getElementById("lb-img");'
    'if(!lb||!lbImg)return;'
    'var lbOpen=false;'
    'function open(src,alt){lbImg.src=src;lbImg.alt=alt||"";lb.classList.add("open");document.body.classList.add("lb-open");lbOpen=true}'
    'function close(){lb.classList.remove("open");document.body.classList.remove("lb-open");lbImg.src="";lbOpen=false}'
    'document.querySelectorAll("main img").forEach(function(img){'
    'if(img.closest("a"))return;'
    'img.addEventListener("click",function(e){e.preventDefault();open(img.src,img.alt)})'
    '});'
    'lb.addEventListener("click",function(e){if(e.target!==lbImg)close()});'
    'document.addEventListener("keydown",function(e){if(e.key==="Escape"&&lbOpen)close()});'
    '})();'
    '</script>'
)

SCROLLSPY_SCRIPT = (
    '<script>'
    '(function(){'
    'var links=document.querySelectorAll(".toc-panel a[href^=\\"#\\"]");'
    'if(!links.length)return;'
    'var map={};links.forEach(function(a){map[a.getAttribute("href").slice(1)]=a});'
    'var hs=Array.from(document.querySelectorAll("main h1[id],main h2[id],main h3[id],main h4[id],main h5[id],main h6[id]"));'
    'function onScroll(){'
    'var y=window.scrollY+90,cur=null;'
    'for(var i=0;i<hs.length;i++){if(hs[i].offsetTop<=y)cur=hs[i];else break}'
    'links.forEach(function(a){a.classList.remove("active")});'
    'if(cur&&map[cur.id])map[cur.id].classList.add("active")'
    '}'
    'window.addEventListener("scroll",onScroll,{passive:true});'
    'window.addEventListener("load",onScroll);onScroll()'
    '})();'
    '</script>'
)

INDEX_SCRIPT = (
    '<script>'
    '(function(){'
    'var root=document.querySelector(".folder-tree");if(!root)return;'
    'var input=document.getElementById("kb-search");'
    'var expandBtn=document.getElementById("kb-expand");'
    'var collapseBtn=document.getElementById("kb-collapse");'
    'if(expandBtn)expandBtn.onclick=function(){root.querySelectorAll("details").forEach(function(d){d.open=true})};'
    'if(collapseBtn)collapseBtn.onclick=function(){root.querySelectorAll("details").forEach(function(d){d.open=false})};'
    'if(!input)return;'
    'try{var saved=localStorage.getItem("kb-search");if(saved)input.value=saved}catch(e){}'
    'function applyFilter(){'
    'var q=input.value.trim().toLowerCase();'
    'try{localStorage.setItem("kb-search",input.value)}catch(e){}'
    'if(!q){root.querySelectorAll("li").forEach(function(li){li.style.display=""});return}'
    'function check(li,force){'
    'var v;'
    'if(li.classList.contains("leaf")){v=force||li.textContent.toLowerCase().indexOf(q)>=0}'
    'else{'
    'var s=li.querySelector(":scope > details > summary");'
    'var selfMatch=s&&s.textContent.toLowerCase().indexOf(q)>=0;'
    'var f=force||selfMatch;'
    'var kids=Array.from(li.querySelectorAll(":scope > details > ul > li"));'
    'var any=kids.map(function(c){return check(c,f)}).some(Boolean);'
    'v=selfMatch||any;'
    'if(v){var d=li.querySelector(":scope > details");if(d)d.open=true}'
    '}'
    'li.style.display=v?"":"none";'
    'return v'
    '}'
    'Array.from(root.querySelectorAll(":scope > li")).forEach(function(li){check(li,false)})'
    '}'
    'input.addEventListener("input",applyFilter);'
    'if(input.value)applyFilter()'
    '})();'
    '</script>'
)

# ---------------------------------------------------------------------------
# download libs (offline)
# ---------------------------------------------------------------------------
def download_libs(out_dir: Path, force: bool = False) -> Path:
    """Download MathJax & Mermaid into out_dir/_libs/ (cached)."""
    libs_dir = out_dir / "_libs"
    libs_dir.mkdir(parents=True, exist_ok=True)

    downloads = [
        ("mathjax", "tex-svg.js",
         "https://cdn.jsdelivr.net/npm/mathjax@4/tex-svg.js"),
        ("mermaid", "mermaid.min.js",
         "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"),
    ]

    for pkg, filename, url in downloads:
        dest = libs_dir / pkg / filename
        if dest.exists() and not force:
            print(f"\u2714 {pkg} (cached)")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"\u2193 Downloading {pkg} \u2026")
        try:
            urllib.request.urlretrieve(url, dest)
        except Exception as e:
            print(f"Error downloading {url}: {e}")
            sys.exit(1)
    return libs_dir

# ---------------------------------------------------------------------------
# direction helpers
# ---------------------------------------------------------------------------
def first_strong_direction(text):
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b in ("R", "AL"):
            return "rtl"
        if b == "L":
            return "ltr"
    return None

def text_direction_majority(text):
    rtl = ltr = 0
    for ch in text:
        b = unicodedata.bidirectional(ch)
        if b in ("R", "AL"):
            rtl += 1
        elif b == "L":
            ltr += 1
    if not rtl and not ltr:
        return None
    return "rtl" if rtl >= ltr else "ltr"

def strip_for_direction(text):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"\$\$.*?\$\$", " ", text, flags=re.S)
    text = re.sub(r"\$[^$\n]+?\$", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[\[[^\]]*\]\]", " ", text)
    text = re.sub(r"!?\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"@@\d+@@", " ", text)
    return text

def expand_placeholders(text, stash_texts):
    return re.sub(r"@@(\d+)@@", lambda m: stash_texts.get(int(m.group(1)), ""), text)

def document_direction(md_text):
    return text_direction_majority(strip_for_direction(md_text)) or "ltr"

# ---------------------------------------------------------------------------
# English wrapper
# ---------------------------------------------------------------------------
WRAP_DIGITS = True
_start = "[A-Za-z0-9]" if WRAP_DIGITS else "[A-Za-z]"
EN_RUN = re.compile(rf"(&#?\w+;)|({_start}[A-Za-z0-9]*(?:[ \t\-'\u2019./&+:,()%_]+[A-Za-z0-9]+)*)")
SKIP_OPEN = re.compile(r'<(?:(pre|code|script|style|textarea|svg)\b|(span|div) class="math)', re.I)

def wrap_en(markup):
    out, skip = [], None
    for part in re.split(r"(<[^>]*>)", markup):
        if part[:1] == "<" and part[-1:] == ">":
            if skip:
                if part.lower().startswith(skip):
                    skip = None
            else:
                m = SKIP_OPEN.match(part)
                if m:
                    skip = "</" + (m.group(1) or m.group(2)).lower()
            out.append(part)
        elif skip or not part:
            out.append(part)
        else:
            out.append(EN_RUN.sub(
                lambda m: m.group(1) or f'<span class="en" lang="en">{m.group(2)}</span>', part))
    return "".join(out)

def slug(s):
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", s.strip().lower()))

def outside_code(text, fn):
    out, pos = [], 0
    for m in CODE.finditer(text):
        out += [fn(text[pos:m.start()]), m.group(0)]
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)

def front_matter(text):
    m = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", text, re.S)
    if not m:
        return {}, text
    data = {}
    try:
        import yaml
        data = yaml.safe_load(m.group(1))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v.strip():
                data[k.strip()] = v.strip()
    return data, text[m.end():]

def extract_section(text, heading):
    lines, target, start, level = text.splitlines(), slug(heading), None, 0
    for i, line in enumerate(lines):
        m = re.match(r"(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        if start is None:
            if slug(m.group(2)) == target:
                start, level = i, len(m.group(1))
        elif len(m.group(1)) <= level:
            return "\n".join(lines[start:i])
    return "\n".join(lines[start:]) if start is not None else text

def note_title(src: Path) -> str:
    props, body = front_matter(src.read_text(encoding="utf-8-sig"))
    if props.get("title"):
        return str(props["title"])
    m = re.search(r"^#\s+(.+?)\s*#*\s*$", body, re.M)
    return m.group(1) if m else src.stem

# ---------------------------------------------------------------------------
# table of contents
# ---------------------------------------------------------------------------
def build_toc(items, label):
    if len(items) < 2:
        return ""
    min_lvl = min(lvl for lvl, _, _ in items)
    root = {"level": min_lvl - 1, "children": []}
    stack = [root]
    for level, hid, text in items:
        while stack and stack[-1]["level"] >= level:
            stack.pop()
        node = {"level": level, "id": hid, "text": text, "children": []}
        (stack[-1] if stack else root)["children"].append(node)
        stack.append(node)

    def render(nodes):
        parts = ["<ul>"]
        for n in nodes:
            d = first_strong_direction(n["text"]) or "ltr"
            parts.append(f'<li dir="{d}"><a href="#{esc(n["id"])}">{esc(n["text"])}</a>')
            if n["children"]:
                parts.append(render(n["children"]))
            parts.append("</li>")
        parts.append("</ul>")
        return "".join(parts)

    label_dir = first_strong_direction(label) or "ltr"
    return (f'<nav class="toc">'
            f'<div class="toc-title" dir="{label_dir}">{esc(label)}</div>'
            f'{render(root["children"])}</nav>\n')

# ---------------------------------------------------------------------------
# tree scanning & rendering
# ---------------------------------------------------------------------------
def scan_tree(root: Path, exclude: set):
    tree = {"type": "folder", "name": root.name, "path": "", "children": []}
    notes, files = [], []

    def _walk(dir_path: Path, node: dict):
        for entry in sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name.startswith("."):
                continue
            if entry.resolve() in exclude:
                continue
            if entry.is_dir():
                child = {"type": "folder", "name": entry.name,
                         "path": entry.relative_to(root).as_posix(), "children": []}
                node["children"].append(child)
                _walk(entry, child)
            elif entry.suffix.lower() == ".md":
                rel = entry.relative_to(root).with_suffix("").as_posix()
                title = note_title(entry)
                child = {"type": "file", "name": entry.name, "path": rel,
                         "html_path": rel + ".html", "title": title}
                node["children"].append(child)
                notes.append((entry, title))
            else:
                files.append(entry)

    _walk(root, tree)
    return tree, notes, files

def count_leaves(node) -> int:
    if node["type"] == "file":
        return 1
    return sum(count_leaves(c) for c in node["children"])

def build_folder_tree_html(node) -> str:
    if node["type"] == "file":
        return (
            f'<li class="leaf">'
            f'<a href="{node["html_path"]}" title="{esc(node["path"])}" dir="auto">'
            f'<span>{esc(node["title"])}</span></a></li>'
        )
    children = "".join(build_folder_tree_html(c) for c in node["children"])
    if not children:
        return ""
    count = count_leaves(node)
    return (
        f'<li class="folder"><details>'
        f'<summary><span class="name" dir="auto">{esc(node["name"])}</span>'
        f'<span class="count">{count}</span></summary>'
        f'<ul>{children}</ul></details></li>'
    )

# ---------------------------------------------------------------------------
# regexes & token tables
# ---------------------------------------------------------------------------
IMG = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".avif"}
AUDIO = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}
VIDEO = {".mp4", ".webm", ".mov", ".mkv"}
LIST = r"(?:(?:[-*+]|\d+[.)])[ \t>]+)?"
CODE = re.compile(
    rf"^[ \t>]*{LIST}[ \t>]*(`{{3,}}|~{{3,}}).*?^[ \t>]*{LIST}[ \t>]*\1[ \t]*$"
    r"|`+[^`\n]+?`+"
    r"|\$\$.*?\$\$|(?<![\\$])\$[^$\n]+?\$",
    re.M | re.S,
)
PH_BLOCK = re.compile(r'<p[^>]*>@@(\d+)@@</p>')
PH = re.compile(r"@@(\d+)@@")
CALLOUT = re.compile(r"\[!([\w-]+)\]([+-]?)[ \t]*([^\n]*)(?:\n|$)")

LEAF_BLOCKS = ("paragraph_open", "heading_open", "td_open", "th_open", "list_item_open")
CONTAINER_CLOSE = {
    "bullet_list_open": "bullet_list_close",
    "ordered_list_open": "ordered_list_close",
    "table_open": "table_close",
    "blockquote_open": "blockquote_close",
}

# ---------------------------------------------------------------------------
# Site class
# ---------------------------------------------------------------------------
class Site:
    def __init__(self, root: Path, out: Path, tree: dict, notes: list, files: list, libs_dir: Path):
        self.root, self.out, self.tree = root, out, tree
        self.libs_dir = libs_dir
        self.flags, self.stash, self.uris = set(), [], {}
        self.stash_texts = {}
        self.notes, self.files = {}, {}

        for p, _title in notes:
            rel = p.relative_to(root).with_suffix("").as_posix().lower()
            self.notes.setdefault(rel, p)
            self.notes.setdefault(p.stem.lower(), p)

        for p in files:
            self.files.setdefault(p.relative_to(root).as_posix().lower(), p)
            self.files.setdefault(p.name.lower(), p)

        self.md = self.make_md()

    # ---- markdown-it setup
    def make_md(self):
        site = self
        md = MarkdownIt("commonmark", {"html": True, "breaks": True})
        md.enable(["table", "strikethrough"])
        try:
            md.enable("linkify")
            md.options["linkify"] = True
        except Exception:
            pass
        md.use(footnote_plugin)
        md.use(tasklists_plugin)

        def math(content, opts):
            site.flags.add("math")
            c = esc(content)
            if opts.get("display_mode"):
                return f'<span class="math">\\[{c}\\]</span>'
            return f'<span class="math">\\({c}\\)</span>'
        md.use(dollarmath_plugin, allow_space=True, allow_digits=True, renderer=math)

        # direction rule
        def direction_rule(state):
            toks = state.tokens
            for t in toks:
                if t.type != "inline":
                    continue
                content = expand_placeholders(t.content, site.stash_texts)
                d = first_strong_direction(strip_for_direction(content))
                if d:
                    t.meta = t.meta or {}
                    t.meta["dir"] = d

            def first_inline_dir(i):
                opentype = toks[i].type
                close_type = opentype.replace("_open", "_close")
                depth = 0
                for j in range(i + 1, len(toks)):
                    tj = toks[j]
                    if tj.type == opentype:
                        depth += 1
                    elif tj.type == close_type:
                        if depth == 0:
                            return None
                        depth -= 1
                    elif tj.type == "inline":
                        return (tj.meta or {}).get("dir")
                return None

            for i, t in enumerate(toks):
                if t.type in LEAF_BLOCKS:
                    d = first_inline_dir(i)
                    if d:
                        t.attrSet("dir", d)

            for i, t in enumerate(toks):
                if t.type not in CONTAINER_CLOSE:
                    continue
                close_type = CONTAINER_CLOSE[t.type]
                depth = 0
                first_dir = None
                for j in range(i + 1, len(toks)):
                    tj = toks[j]
                    if tj.type == t.type:
                        depth += 1
                    elif tj.type == close_type:
                        if depth == 0:
                            break
                        depth -= 1
                    elif tj.type in LEAF_BLOCKS:
                        d = tj.attrGet("dir")
                        if d:
                            first_dir = d
                            break
                if first_dir:
                    t.attrSet("dir", first_dir)
        md.core.ruler.after("inline", "directions", direction_rule)

        # callouts
        def callouts(state):
            toks, stack = state.tokens, []
            for i, t in enumerate(toks):
                if t.type == "blockquote_open":
                    stack.append(t)
                    if (i + 2 < len(toks) and toks[i + 1].type == "paragraph_open"
                            and toks[i + 2].type == "inline"):
                        m = CALLOUT.match(toks[i + 2].content)
                        if m:
                            t.meta = {"callout": (m.group(1).lower(), m.group(2), m.group(3).strip())}
                            rest = toks[i + 2].content[m.end():]
                            toks[i + 2].content = rest
                            if not rest.strip():
                                toks[i + 1].hidden = toks[i + 3].hidden = True
                elif t.type == "blockquote_close" and stack:
                    t.meta = stack.pop().meta
        md.core.ruler.before("inline", "callouts", callouts)

        # heading ids & TOC
        def heading_ids(state):
            seen = {}
            toc = state.env.get("toc")
            for i, t in enumerate(state.tokens):
                if t.type == "heading_open":
                    text = "".join(
                        c.content for c in state.tokens[i + 1].children
                        if c.type in ("text", "code_inline")
                    )
                    s = slug(text) or "section"
                    n = seen.get(s, 0)
                    seen[s] = n + 1
                    hid = s if n == 0 else f"{s}-{n}"
                    t.attrSet("id", hid)
                    if toc is not None:
                        toc.append((int(t.tag[1]), hid, text))
        md.core.ruler.push("heading_ids", heading_ids)

        # renderers
        def render_list_open(tag):
            def r(self, tokens, idx, options, env):
                t = tokens[idx]
                parts = []
                start = t.attrGet("start")
                if start is not None:
                    parts.append(f'start="{esc(str(start))}"')
                d = t.attrGet("dir")
                if d:
                    parts.append(f'dir="{d}"')
                attr = (" " + " ".join(parts)) if parts else ""
                return f"<{tag}{attr}>\n"
            return r

        def list_item_open(self, tokens, idx, options, env):
            d = tokens[idx].attrGet("dir")
            dd = f' dir="{d}"' if d else ""
            return f"<li{dd}>\n"

        def bq_open(self, tokens, idx, options, env):
            c = (tokens[idx].meta or {}).get("callout")
            d = tokens[idx].attrGet("dir")
            dd = f' dir="{d}"' if d else ""
            if not c:
                return f"<blockquote{dd}>"
            kind, fold, title = c
            title_html = md.renderInline(title) if title else esc(kind.replace("-", " ").title())
            td = first_strong_direction(title) if title else None
            tattr = f' dir="{td}"' if td else ""
            if fold:
                op = " open" if fold == "+" else ""
                return (
                    f'<details class="callout" data-type="{esc(kind)}"{op}{dd}>'
                    f'<summary{tattr}>{title_html}</summary><div class="callout-body">'
                )
            return (
                f'<div class="callout" data-type="{esc(kind)}"{dd}>'
                f'<div class="callout-title"{tattr}>{title_html}</div>'
                f'<div class="callout-body">'
            )

        def bq_close(self, tokens, idx, options, env):
            c = (tokens[idx].meta or {}).get("callout")
            if not c:
                return "</blockquote>\n"
            return "</div></details>\n" if c[1] else "</div></div>\n"

        def table_open(self, tokens, idx, options, env):
            d = tokens[idx].attrGet("dir")
            dd = f' dir="{d}"' if d else ""
            return f'<div class="tw"><table{dd}>'

        orig_fence = md.renderer.rules["fence"]

        def fence(self, tokens, idx, options, env):
            if tokens[idx].info.strip().lower() == "mermaid":
                site.flags.add("mermaid")
                return f'<pre class="mermaid">{esc(tokens[idx].content)}</pre>\n'
            return orig_fence(tokens, idx, options, env)

        def code_inline(self, tokens, idx, options, env):
            return f'<code dir="auto">{esc(tokens[idx].content)}</code>'

        orig_image = md.renderer.rules["image"]

        def image(self, tokens, idx, options, env):
            t = tokens[idx]
            f = site.local_image(t.attrGet("src") or "", env.get("cur", Path(".")))
            if f:
                t.attrSet("src", site.data_uri(f))
            return orig_image(tokens, idx, options, env)

        def td_th(tag):
            def r(self, tokens, idx, options, env):
                t = tokens[idx]
                style = t.attrGet("style")
                d = t.attrGet("dir")
                s = f' style="{esc(style)}"' if style else ""
                dd = f' dir="{d}"' if d else ""
                return f"<{tag}{s}{dd}>"
            return r

        md.add_render_rule("image", image)
        md.add_render_rule("code_inline", code_inline)
        md.add_render_rule("blockquote_open", bq_open)
        md.add_render_rule("blockquote_close", bq_close)
        md.add_render_rule("fence", fence)
        md.add_render_rule("bullet_list_open", render_list_open("ul"))
        md.add_render_rule("ordered_list_open", render_list_open("ol"))
        md.add_render_rule("list_item_open", list_item_open)
        md.add_render_rule("table_open", table_open)
        md.add_render_rule("td_open", td_th("td"))
        md.add_render_rule("th_open", td_th("th"))
        md.add_render_rule("table_close", lambda s, t, i, o, e: "</table></div>\n")
        return md

    # ---- link / file helpers
    def url(self, dest: Path, cur: Path) -> str:
        start = (self.out / cur).parent
        return quote(os.path.relpath(dest, start).replace(os.sep, "/"))

    def note_url(self, src: Path, cur: Path) -> str:
        return self.url(self.out / src.relative_to(self.root).with_suffix(".html"), cur)

    def asset_url(self, src: Path, cur: Path) -> str:
        dest = self.out / src.relative_to(self.root)
        if dest.resolve() != src.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        return self.url(dest, cur)

    def data_uri(self, path: Path) -> str:
        path = Path(path)
        if path not in self.uris:
            mime = (
                "image/svg+xml" if path.suffix.lower() == ".svg"
                else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            )
            self.uris[path] = (
                f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
            )
        return self.uris[path]

    def local_image(self, src: str, cur: Path):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", src, re.I):
            return None
        src = unquote(src.split("#")[0].split("?")[0])
        for cand in ((self.root / cur).parent / src, self.root / src):
            if cand.is_file():
                return cand
        return self.find_file(Path(src).name)

    def find_note(self, name: str):
        return self.notes.get(re.sub(r"\.md$", "", name.strip(), flags=re.I).lower())

    def find_file(self, name: str):
        return self.files.get(name.strip().lower())

    def hold(self, markup: str, block: bool = False, text: str = "") -> str:
        self.stash.append(markup)
        idx = len(self.stash) - 1
        if text:
            self.stash_texts[idx] = text
        ph = f"@@{idx}@@"
        return f"\n\n{ph}\n\n" if block else ph

    def wikilink(self, m, cur: Path) -> str:
        inner = m.group(1).replace("\\|", "|")
        target, _, alias = inner.partition("|")
        name, _, head = target.partition("#")
        head = "" if head.startswith("^") else head
        frag = f"#{slug(head)}" if head else ""
        label = alias or (f"{name} &gt; {esc(head)}" if name and head else esc(name or head))
        if alias:
            label = esc(alias)
        label_text = alias or name or head
        if not name:
            return self.hold(f'<a href="{frag or "#"}">{label}</a>', text=label_text)
        note = self.find_note(name)
        if note:
            return self.hold(
                f'<a href="{self.note_url(note, cur)}{frag}">{label}</a>',
                text=label_text,
            )
        f = self.find_file(name)
        if f:
            return self.hold(
                f'<a href="{self.asset_url(f, cur)}">{label}</a>',
                text=label_text,
            )
        return self.hold(f'<a class="unresolved">{label}</a>', text=label_text)

    def embed(self, m, cur: Path, depth: int) -> str:
        inner = m.group(1).replace("\\|", "|")
        target, _, opt = inner.partition("|")
        name, _, head = target.partition("#")
        ext = Path(name).suffix.lower()
        if ext in IMG | AUDIO | VIDEO | {".pdf"}:
            f = self.find_file(name)
            if not f:
                return self.hold(f'<span class="unresolved">{esc(name)}</span>', text=name)
            if ext in IMG:
                u = self.data_uri(f)
                size = re.fullmatch(r"(\d+)(?:x(\d+))?", opt.strip())
                dims = ""
                if size:
                    dims = f' width="{size.group(1)}"' + (
                        f' height="{size.group(2)}"' if size.group(2) else ""
                    )
                alt = esc(name if (size or not opt) else opt)
                return self.hold(f'<img src="{u}" alt="{alt}"{dims}>', text=name)
            u = self.asset_url(f, cur)
            if ext in AUDIO:
                return self.hold(f'<audio controls src="{u}"></audio>', True, text=name)
            if ext in VIDEO:
                return self.hold(f'<video controls src="{u}"></video>', True, text=name)
            return self.hold(f'<a href="{u}">{esc(name)}</a>', text=name)
        note = self.find_note(name)
        if not note or depth >= 2:
            return self.hold(
                f'<span class="unresolved">{esc(name or head)}</span>',
                text=name or head,
            )
        _, body = front_matter(note.read_text(encoding="utf-8-sig"))
        if head and not head.startswith("^"):
            body = extract_section(body, head)
        return self.hold(
            f'<div class="embed">{self.render(body, cur, depth + 1)}</div>',
            True,
            text=name or head,
        )

    def transform(self, s: str, cur: Path, depth: int) -> str:
        s = re.sub(r"%%.*?%%", "", s, flags=re.S)
        s = re.sub(r"!\[\[([^\]\n]+?)\]\]", lambda m: self.embed(m, cur, depth), s)
        s = re.sub(r"\[\[([^\]\n]+?)\]\]", lambda m: self.wikilink(m, cur), s)
        s = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r"<mark>\1</mark>", s)
        s = re.sub(
            r"(?<![\w&/#(\[])#([\w/-]*[^\W\d][\w/-]*)",
            lambda m: f'<span class="tag">#{m.group(1)}</span>',
            s,
        )
        s = re.sub(r"[ \t]+\^[\w-]+[ \t]*$", "", s, flags=re.M)
        return s

    def render(self, text: str, cur: Path, depth: int = 0, toc=None) -> str:
        text = outside_code(text, lambda s: self.transform(s, cur, depth))
        env = {"cur": cur}
        if toc is not None:
            env["toc"] = toc
        out = self.md.render(text, env)
        out = PH_BLOCK.sub(lambda m: self.stash[int(m.group(1))], out)
        out = PH.sub(lambda m: self.stash[int(m.group(1))], out)
        return wrap_en(out) if depth == 0 else out

    # ---- page rendering
    def _libs_rel(self, page_path: Path) -> str:
        page_dir = page_path.parent
        return os.path.relpath(self.libs_dir, self.out / page_dir).replace(os.sep, "/")

    def page(self, src: Path, has_index: bool, lang=None,
             show_toc: bool = True, toc_label=None) -> Path:
        cur = src.relative_to(self.root)
        props, body = front_matter(src.read_text(encoding="utf-8-sig"))
        self.flags.clear()
        doc_dir = document_direction(body)

        toc_items = []
        content_body = self.render(body, cur, toc=toc_items if show_toc else None)
        title = note_title(src)

        # properties
        pr = ""
        if props:
            rows = []
            for k, v in props.items():
                v = ", ".join(map(str, v)) if isinstance(v, list) else v
                rows.append(f"<dt>{esc(str(k))}</dt><dd dir=\"auto\">{esc(str(v))}</dd>")
            pr = wrap_en(f'<dl class="props">{"".join(rows)}</dl>\n')

        # TOC
        if show_toc:
            label = toc_label or ("فهرست مطالب" if doc_dir == "rtl" else "Contents")
            toc_html = wrap_en(build_toc(toc_items, label))
        else:
            toc_html = ""

        # back link
        back = ""
        if has_index:
            back = f'<a href="{self.url(self.out / "index.html", cur)}">Index</a>'

        # content with layout wrapper
        if toc_html:
            content = (
                f'<div class="layout">'
                f'<aside class="toc-panel">{toc_html}</aside>'
                f'<div class="content">{pr}{content_body}</div>'
                f'</div>'
            )
            extra_scripts = SCROLLSPY_SCRIPT
        else:
            content = f'<div class="content">{pr}{content_body}</div>'
            extra_scripts = ""

        libs_rel = self._libs_rel(cur.with_suffix(".html"))
        scripts = self._local_scripts(libs_rel) + extra_scripts

        dest = self.out / cur.with_suffix(".html")
        dest.parent.mkdir(parents=True, exist_ok=True)
        lang = lang or ("fa" if doc_dir == "rtl" else "en")
        dest.write_text(
            PAGE.format(
                lang=lang, title=esc(title), css=page_css(),
                back=back or "&nbsp;", content=content,
                backtop=BACKTOP_BUTTON, lightbox=LIGHTBOX,
                scripts=scripts,
            ),
            encoding="utf-8",
        )
        return dest

    def _local_scripts(self, libs_rel: str) -> str:
        tags = []
        if "math" in self.flags:
            tags.append('<script>window.MathJax={options:{skipHtmlTags:["script","style","pre","code"]}};</script>')
            tags.append(f'<script src="{libs_rel}/mathjax/tex-svg.js"></script>')
        if "mermaid" in self.flags:
            tags.append(f'<script src="{libs_rel}/mermaid/mermaid.min.js"></script>')
            tags.append(mermaid_script(FONT_STACK))
        return "\n".join(tags)

# ---------------------------------------------------------------------------
# Mermaid init
# ---------------------------------------------------------------------------
def mermaid_script(font_stack: str) -> str:
    light = {
        "primaryColor": "#fff", "primaryTextColor": "#000", "primaryBorderColor": "#000",
        "secondaryColor": "#f5f5f5", "secondaryTextColor": "#000", "secondaryBorderColor": "#000",
        "tertiaryColor": "#fff", "tertiaryTextColor": "#000", "tertiaryBorderColor": "#000",
        "mainBkg": "#fff", "nodeBorder": "#000", "clusterBkg": "#fff", "clusterBorder": "#000",
        "lineColor": "#000", "textColor": "#000", "titleColor": "#000",
        "edgeLabelBackground": "#fff", "nodeTextColor": "#000",
        "actorBkg": "#fff", "actorBorder": "#000", "actorTextColor": "#000",
        "actorLineColor": "#000", "signalColor": "#000", "signalTextColor": "#000",
        "labelBkg": "#fff", "labelTextColor": "#000", "loopTextColor": "#000",
        "noteBkg": "#fff", "noteBorder": "#000", "noteTextColor": "#000",
        "activationBkg": "#eee", "activationBorder": "#000", "sequenceNumberColor": "#fff",
    }
    dark = {
        "primaryColor": "#000", "primaryTextColor": "#fff", "primaryBorderColor": "#fff",
        "secondaryColor": "#1a1a1a", "secondaryTextColor": "#fff", "secondaryBorderColor": "#fff",
        "tertiaryColor": "#000", "tertiaryTextColor": "#fff", "tertiaryBorderColor": "#fff",
        "mainBkg": "#000", "nodeBorder": "#fff", "clusterBkg": "#000", "clusterBorder": "#fff",
        "lineColor": "#fff", "textColor": "#fff", "titleColor": "#fff",
        "edgeLabelBackground": "#000", "nodeTextColor": "#fff",
        "actorBkg": "#000", "actorBorder": "#fff", "actorTextColor": "#fff",
        "actorLineColor": "#fff", "signalColor": "#fff", "signalTextColor": "#fff",
        "labelBkg": "#000", "labelTextColor": "#fff", "loopTextColor": "#fff",
        "noteBkg": "#000", "noteBorder": "#fff", "noteTextColor": "#fff",
        "activationBkg": "#333", "activationBorder": "#fff", "sequenceNumberColor": "#000",
    }
    return f'''<script>
const LIGHT = {json.dumps(light)};
const DARK = {json.dumps(dark)};
const FONT = {json.dumps(font_stack)};
const els = [...document.querySelectorAll("pre.mermaid")];
els.forEach(el => {{ el.dataset.src = el.textContent; }});
function isDark() {{
  const t = document.documentElement.dataset.theme;
  if (t === "dark") return true;
  if (t === "light") return false;
  return matchMedia("(prefers-color-scheme:dark)").matches;
}}
async function renderAll() {{
  const vars = Object.assign({{fontFamily: FONT, fontSize: "16px"}}, isDark() ? DARK : LIGHT);
  mermaid.initialize({{startOnLoad: false, theme: "base", themeVariables: vars,
    flowchart: {{htmlLabels: true, curve: "basis", useMaxWidth: true}},
    sequence: {{useMaxWidth: true}}, gantt: {{useMaxWidth: true}},
    securityLevel: "loose"}});
  for (const el of els) {{ el.removeAttribute("data-processed"); el.textContent = el.dataset.src; }}
  try {{ await mermaid.run({{nodes: els}}); }} catch (e) {{ console.error(e); }}
  els.forEach(el => el.querySelectorAll("foreignObject div, foreignObject span").forEach(d => {{
    if (!d.hasAttribute("dir")) d.setAttribute("dir", "auto");
  }}));
}}
renderAll();
new MutationObserver(renderAll).observe(document.documentElement,
  {{attributes: true, attributeFilter: ["data-theme"]}});
</script>'''

# ---------------------------------------------------------------------------
# index page
# ---------------------------------------------------------------------------
def index_page(site: Site, tree: dict, lang=None) -> Path:
    total = sum(count_leaves(c) for c in tree["children"])
    lang_final = lang or (
        "fa" if document_direction(" ".join(
            n["title"] for n in tree["children"] if n["type"] == "file")) == "rtl"
        else "en"
    )
    note_word = "یادداشت" if lang_final == "fa" else ("note" if total == 1 else "notes")

    if tree["children"]:
        controls = (
            '<div class="kb-search">'
            '<input id="kb-search" type="search" placeholder="Search notes\u2026" aria-label="Search notes">'
            '<button id="kb-expand" type="button">Expand all</button>'
            '<button id="kb-collapse" type="button">Collapse all</button>'
            '</div>'
        )
        items = "".join(build_folder_tree_html(c) for c in tree["children"])
        tree_html = f'{controls}<ul class="folder-tree">{items}</ul>'
    else:
        tree_html = '<p class="kb-empty">No notes found.</p>'

    body = wrap_en(
        f'<h1 dir="auto">Knowledge Base</h1>'
        f'<p class="kb-subtitle">{total} {note_word}</p>'
        f'{tree_html}'
    )
    content = f'<div class="content">{body}</div>'

    dest = site.out / "index.html"
    dest.write_text(
        PAGE.format(
            lang=lang_final, title="Index", css=page_css(),
            back="&nbsp;", content=content,
            backtop=BACKTOP_BUTTON, lightbox=LIGHTBOX,
            scripts=INDEX_SCRIPT,
        ),
        encoding="utf-8",
    )
    return dest

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert an Obsidian-style Markdown folder tree into a fully offline HTML knowledge base."
    )
    ap.add_argument("input_dir", help="Root folder containing .md files and subfolders")
    ap.add_argument("-o", "--out", help="Output folder (default: <input_dir>/html_out)")
    ap.add_argument("--font", nargs="+", metavar="FILE",
                    help="Font file(s) to embed: REGULAR [BOLD] (default: auto-find IRANSharp / BNazanin)")
    ap.add_argument("--no-font", action="store_true", help="Do not embed any font")
    ap.add_argument("--lang", help="Language code for <html lang=...>")
    ap.add_argument("--no-toc", action="store_true", help="Do not prepend a table of contents")
    ap.add_argument("--toc-label", metavar="TEXT",
                    help="Label for the table of contents (default: 'Contents' / 'فهرست مطالب')")
    ap.add_argument("--force-download", action="store_true",
                    help="Re-download MathJax / Mermaid even if cached")
    a = ap.parse_args()

    global FONT_CSS, FONT_STACK
    if not a.no_font:
        if a.font:
            regular, bold = Path(a.font[0]), (Path(a.font[1]) if len(a.font) > 1 else None)
        else:
            regular, bold = find_fonts()
        if regular and regular.is_file():
            FONT_CSS = font_face_css(regular, bold)
            FONT_STACK = '"Embedded", ' + FONT
            print("Font embedded:", regular.name, bold.name if bold else "")
        else:
            print("Warning: IRANSharp/BNazanin not found – using system fallback (use --font FILE)")

    root = Path(a.input_dir).resolve()
    if not root.is_dir():
        sys.exit(f"Not a directory: {a.input_dir}")

    out = Path(a.out).resolve() if a.out else (root / "html_out")
    out.mkdir(parents=True, exist_ok=True)

    libs_dir = download_libs(out, force=a.force_download)

    exclude = {out}
    tree, notes, files = scan_tree(root, exclude)
    if not notes:
        sys.exit("No .md files found in the tree.")

    site = Site(root, out, tree, notes, files, libs_dir)
    multi = len(notes) > 1
    for src, _title in notes:
        print("wrote", site.page(src, True, lang=a.lang,
                                 show_toc=not a.no_toc,
                                 toc_label=a.toc_label))
    print("wrote", index_page(site, tree, lang=a.lang))

if __name__ == "__main__":
    main()