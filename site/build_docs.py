"""Render docs/*.md into site/docs/*.html with the site's visual system.
Run:  uv run python site/build_docs.py   (then site/deploy.py)"""

from __future__ import annotations

import html
import re
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = ROOT / "site" / "docs"

PAGES = [
    ("tutorial", "TUTORIAL.md", "Tutorial", "Your first run in ten minutes: install, sign in, run the example, read the report, then your own repo and your first experiment."),
    ("guide", "GUIDE.md", "User guide", "How each stage works, what \"solved\" means, reading the report, resume, experiments, trading projects, troubleshooting."),
    ("config", "CONFIG.md", "Configuration", "Every key in project.yaml: project, autonomy, budget, providers, agents, permissions."),
    ("providers", "PROVIDERS.md", "Providers", "Each backend's exact invocation, envelope, and gotchas; how to add one."),
    ("design", "DESIGN.md", "Design notes", "Architecture, schemas, state machine, failure modes, and the milestone log."),
]

CSS = """
:root{color-scheme:light;--chalk:#f3f4f0;--paper:#fff;--ink:#171c26;--ink-2:#4a5160;--ink-3:#7b8290;--line:#d9dcd6;--oak:#b8763b;--oak-2:#8f5a2a;--felt:#2f6b4f;--brass:#c9a24d;--signal:#d24b3f;--code-bg:#1d2330;--code-ink:#e9ecf1;--inline:#e9eae4}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--chalk:#12161f;--paper:#1a202c;--ink:#eef0f3;--ink-2:#b4bac6;--ink-3:#7f8796;--line:#2c3442;--oak:#c98a4d;--oak-2:#e0aa72;--felt:#5fb08a;--brass:#d8b45e;--signal:#ee6a5e;--code-bg:#0d1017;--code-ink:#e9ecf1;--inline:#242b38}}
:root[data-theme="dark"]{color-scheme:dark;--chalk:#12161f;--paper:#1a202c;--ink:#eef0f3;--ink-2:#b4bac6;--ink-3:#7f8796;--line:#2c3442;--oak:#c98a4d;--oak-2:#e0aa72;--felt:#5fb08a;--brass:#d8b45e;--signal:#ee6a5e;--code-bg:#0d1017;--code-ink:#e9ecf1;--inline:#242b38}
*{box-sizing:border-box}body{margin:0;background:var(--chalk);color:var(--ink);font:400 16.5px/1.6 "IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--oak-2)}a:hover{color:var(--oak)}
h1,h2,h3,h4{font-family:"Bricolage Grotesque","IBM Plex Sans",sans-serif;font-variation-settings:"opsz" 96;letter-spacing:-.02em;line-height:1.08;text-wrap:balance;margin:0}
h1{font-size:clamp(2rem,5vw,3rem);font-weight:800}h2{font-size:1.6rem;font-weight:800;margin-top:2.4em;padding-top:1em;border-top:1px solid var(--line)}h3{font-size:1.15rem;font-weight:600;margin-top:1.8em}h4{font-size:1rem;font-weight:600;margin-top:1.4em}
h2+*,h3+*,h4+*{margin-top:.6em}
p,ul,ol{margin:0 0 1em}li{margin:.25em 0}
code{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace;font-size:.88em;background:var(--inline);padding:.1em .35em;border-radius:5px}
pre{background:var(--code-bg);color:var(--code-ink);border-radius:12px;padding:16px 18px;overflow-x:auto;font-size:.86rem;line-height:1.6;margin:0 0 1.2em}pre code{background:none;padding:0;font-size:inherit;color:inherit}
table{border-collapse:collapse;width:100%;margin:0 0 1.4em;font-size:.93rem;display:block;overflow-x:auto}th,td{text-align:left;vertical-align:top;padding:.55em .7em;border-bottom:1px solid var(--line)}th{font-weight:600;color:var(--ink-2);font-size:.8rem;letter-spacing:.06em;text-transform:uppercase}
blockquote{margin:0 0 1em;padding:.2em 1em;border-left:4px solid var(--brass);color:var(--ink-2)}
hr{border:0;border-top:1px solid var(--line);margin:2em 0}
header{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:color-mix(in srgb,var(--chalk) 86%,transparent);backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
header .wrap{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-block:12px}
.brand{display:flex;align-items:center;gap:10px;font-family:"Bricolage Grotesque",sans-serif;font-weight:800;font-size:1.15rem;text-decoration:none;color:var(--ink)}
nav{display:flex;gap:6px;flex-wrap:wrap}nav a{text-decoration:none;font-size:.92rem;font-weight:500;color:var(--ink-2);padding:.45em .7em;border-radius:8px}nav a:hover,nav a.on{background:var(--paper);color:var(--ink)}
.wrap{max-width:1120px;margin-inline:auto;padding-inline:clamp(16px,4vw,40px)}
.layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:clamp(24px,4vw,56px);padding-block:36px 72px}
aside{position:sticky;top:76px;align-self:start;font-size:.9rem}aside .eyebrow{font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-3);font-weight:600;margin-bottom:8px}
aside a{display:block;color:var(--ink-2);text-decoration:none;padding:.3em 0}aside a.on{color:var(--ink);font-weight:600}aside .toc a{padding-left:0;font-size:.85rem;color:var(--ink-3)}aside .toc{margin-top:18px;border-top:1px solid var(--line);padding-top:12px;max-height:60vh;overflow:auto}
.layout>*{min-width:0}main{max-width:76ch;min-width:0}main .lede{font-size:1.1rem;color:var(--ink-2);margin:.6em 0 1.6em}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:16px;margin-top:24px}.card{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:18px;text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:6px}.card h3{margin:0}.card p{margin:0;color:var(--ink-2);font-size:.95rem}
footer{border-top:1px solid var(--line);padding-block:28px;font-size:.88rem;color:var(--ink-3)}footer .wrap{display:flex;flex-wrap:wrap;gap:12px 28px;justify-content:space-between}
@media (max-width:820px){.layout{grid-template-columns:1fr}aside{position:static}aside .toc{display:none}}
@media (max-width:640px){nav a:not(.gh){display:none}}
"""

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Ccircle cx='32' cy='32' r='22' fill='%23b8763b'/%3E%3Ccircle cx='32' cy='32' r='14' fill='%23a1622d'/%3E%3Ccircle cx='32' cy='6' r='5' fill='%23171c26'/%3E%3Ccircle cx='32' cy='58' r='5' fill='%23171c26'/%3E%3Ccircle cx='6' cy='32' r='5' fill='%23171c26'/%3E%3Ccircle cx='58' cy='32' r='5' fill='%23d24b3f'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{css}</style>
</head>
<body>
<header><div class="wrap">
  <a class="brand" href="/"><svg width="26" height="26" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="22" fill="#b8763b"/><circle cx="32" cy="32" r="14" fill="#a1622d"/><circle cx="32" cy="6" r="5" fill="currentColor"/><circle cx="32" cy="58" r="5" fill="currentColor"/><circle cx="6" cy="32" r="5" fill="currentColor"/><circle cx="58" cy="32" r="5" fill="#d24b3f"/></svg>Roundtable</a>
  <nav aria-label="Site">{nav}<a class="gh" href="https://github.com/bronette/roundtable">GitHub</a></nav>
</div></header>
"""

FOOT = """
<footer><div class="wrap"><span>Roundtable · MIT licensed · built by <a href="https://kevinbrunette.com">Kevin Brunette</a></span><span><a href="https://github.com/bronette/roundtable">Source on GitHub</a> · <a href="/docs/">Docs</a></span></div></footer>
</body>
</html>
"""


def nav_html(current: str) -> str:
    items = [("/", "Home", "home"), ("/docs/", "Docs", "docs")] + [(f"/docs/{slug}.html", title, slug) for slug, _, title, _ in PAGES]
    return "".join(f'<a href="{href}"{" class=\"on\"" if key == current else ""}>{label}</a>' for href, label, key in items[:2])


def side_html(current: str, toc: list[tuple[int, str, str]]) -> str:
    links = "".join(f'<a href="/docs/{slug}.html"{" class=\"on\"" if slug == current else ""}>{title}</a>' for slug, _, title, _ in PAGES)
    toc_html = "".join(f'<a href="#{anchor}" style="padding-left:{(lvl - 2) * 12}px">{html.escape(text)}</a>' for lvl, text, anchor in toc if lvl <= 3)
    return f'<aside><div class="eyebrow">Documentation</div>{links}<div class="toc">{toc_html}</div></aside>'


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def render(md_text: str) -> tuple[str, list[tuple[int, str, str]]]:
    mdi = MarkdownIt("commonmark").enable("table")
    tokens = mdi.parse(md_text)
    toc: list[tuple[int, str, str]] = []
    seen: dict[str, int] = {}
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            lvl = int(t.tag[1])
            text = tokens[i + 1].content
            anchor = slugify(text)
            seen[anchor] = seen.get(anchor, 0) + 1
            if seen[anchor] > 1:
                anchor = f"{anchor}-{seen[anchor]}"
            t.attrSet("id", anchor)
            if lvl >= 2:
                toc.append((lvl, text, anchor))
    body = mdi.renderer.render(tokens, mdi.options, {})
    # links between docs: GUIDE.md → guide.html etc.
    for slug, fname, _, _ in PAGES:
        body = body.replace(f'href="{fname}"', f'href="/docs/{slug}.html"').replace(f'href="docs/{fname}"', f'href="/docs/{slug}.html"')
    body = body.replace('href="LICENSE"', 'href="https://github.com/bronette/roundtable/blob/main/LICENSE"')
    return body, toc


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for slug, fname, title, desc in PAGES:
        body, toc = render((DOCS / fname).read_text())
        page = (HEAD.format(title=f"{title} · Roundtable", desc=html.escape(desc), css=CSS, nav=nav_html("docs"))
                + f'<div class="wrap layout">{side_html(slug, toc)}<main>{body}</main></div>' + FOOT)
        (OUT / f"{slug}.html").write_text(page)
    cards = "".join(f'<a class="card" href="/docs/{slug}.html"><h3>{title}</h3><p>{html.escape(desc)}</p></a>' for slug, _, title, desc in PAGES)
    index = (HEAD.format(title="Roundtable Docs", desc="Documentation for Roundtable, the multi-model AI collaboration orchestrator.", css=CSS, nav=nav_html("docs"))
             + '<div class="wrap" style="padding-block:44px 72px"><h1>Documentation</h1>'
             '<p class="lede" style="font-size:1.1rem;color:var(--ink-2);max-width:60ch;margin-top:.8em">Everything is generated from the Markdown in the repository\'s <code>docs/</code> folder, so the site and GitHub never disagree.</p>'
             f'<div class="cards">{cards}</div>'
             '<h2 style="margin-top:2.4em">Quick start</h2><pre><code>git clone https://github.com/bronette/roundtable\ncd roundtable &amp;&amp; uv sync\nuv run roundtable providers -c examples/project.yaml\nuv run roundtable run examples/project.yaml</code></pre>'
             '</div>' + FOOT)
    (OUT / "index.html").write_text(index)
    print(f"built {len(PAGES) + 1} pages into {OUT}")


if __name__ == "__main__":
    build()
