#!/usr/bin/env python3
"""Turn one rendered guide page into markdown.

Written against a deck-style static site: a page with an <h1>, a .subtitle, and
numbered <section> blocks holding prose, .file-table tables, .code-block code,
.callout asides and .param lists. The deck's index.json only carries a
flattened search blob - headings run into prose and every code block is gone -
so the notes are built from the rendered page instead.

Stdlib only: html.parser, because the target machine installs nothing.
"""
import html as _html
import re
from html.parser import HTMLParser

VOID = {"br", "hr", "img", "input", "meta", "link", "source", "col"}
SKIP = {"script", "style", "svg", "nav", "noscript"}


class Node:
    __slots__ = ("tag", "attrs", "kids", "text")

    def __init__(self, tag="", attrs=None, text=None):
        self.tag, self.attrs, self.kids, self.text = tag, attrs or {}, [], text

    def cls(self):
        return (self.attrs.get("class") or "").split()

    def find(self, tag=None, klass=None):
        for n in self.walk():
            if tag and n.tag != tag:
                continue
            if klass and klass not in n.cls():
                continue
            return n
        return None

    def walk(self):
        for k in self.kids:
            yield k
            for g in k.walk():
                yield g


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, dict(attrs))
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(Node("#text", text=data))


def parse(src):
    t = Tree()
    t.feed(src)
    return t.root


# ------------------------------------------------------------------ inline
def inline(n):
    """Text of a node, keeping only the emphasis markdown can carry."""
    if n.tag == "#text":
        return n.text or ""
    if n.tag in SKIP:
        return ""
    if n.tag == "br":
        return " "
    inner = "".join(inline(k) for k in n.kids)
    if n.tag in ("em", "i"):
        return "*%s*" % inner.strip() if inner.strip() else ""
    if n.tag in ("strong", "b"):
        return "**%s**" % inner.strip() if inner.strip() else ""
    if n.tag == "code":
        return "`%s`" % inner.strip() if inner.strip() else ""
    if n.tag == "a":
        href = n.attrs.get("href", "")
        if href.startswith(("http://", "https://")) and inner.strip():
            return "[%s](%s)" % (inner.strip(), href)
    return inner


def tidy(s):
    return re.sub(r"[ \t]+", " ", s.replace("\xa0", " ")).strip()


def raw_text(n):
    """Everything under n, entities resolved, markup dropped - for code."""
    if n.tag == "#text":
        return n.text or ""
    return "".join(raw_text(k) for k in n.kids)


# ------------------------------------------------------------------ blocks
def table(n):
    rows = []
    for tr in n.walk():
        if tr.tag != "tr":
            continue
        cells = [tidy(inline(c)).replace("|", "\\|")
                 for c in tr.kids if c.tag in ("td", "th")]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join([" --- "] * w) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def code(n):
    head = n.find("div", "code-header")
    caption = tidy(inline(head.kids[0])) if head and head.kids else ""
    pre = n.find("pre")
    body = raw_text(pre) if pre else ""
    body = body.strip("\n").replace("\xa0", " ")
    lang = ""
    m = re.match(r"^([\w.\-/]+)\.(c|h|py|sh|rs|js|json|ya?ml|tcl|mk|S|cfg|toml)\b",
                 caption)
    if m:
        lang = {"h": "c", "S": "asm", "yml": "yaml", "mk": "make"}.get(m.group(2), m.group(2))
    elif re.match(r"^\$|^#", body):
        lang = "bash"
    out = []
    if caption:
        out.append("*%s*" % caption)
    out.append("```%s\n%s\n```" % (lang, body))
    return "\n".join(out)


def callout(n):
    label = n.find("div", "label")
    lines = []
    if label:
        lines.append("**%s**" % tidy(inline(label)))
    for k in n.kids:
        if k is label or k.tag == "#text":
            continue
        t = block(k).strip()
        if t:
            lines.append(t)
    return "\n".join("> " + l for l in "\n\n".join(lines).split("\n"))


def params(n):
    out = []
    for p in n.kids:
        if "param" not in p.cls() and "field" not in p.cls():
            continue
        name = p.find("span", "name") or p.find("span", "label")
        desc = p.find("span", "desc")
        if name and desc:
            out.append("- **%s** — %s" % (tidy(inline(name)), tidy(inline(desc))))
        else:
            t = tidy(inline(p))
            if t:
                out.append("- " + t)
    return "\n".join(out)


def listing(n, ordered):
    out, i = [], 1
    for li in n.kids:
        if li.tag != "li":
            continue
        t = tidy(inline(li))
        if not t:
            continue
        out.append(("%d. " % i if ordered else "- ") + t)
        i += 1
    return "\n".join(out)


def block(n):
    """One node to markdown. Returns '' for anything not worth keeping."""
    if n.tag in SKIP or "toc" in n.cls():
        return ""
    if n.tag == "#text":
        return tidy(n.text or "")
    c = n.cls()
    if "code-block" in c:
        return code(n)
    if "callout" in c:
        return callout(n)
    if "params" in c:
        return params(n)
    if "file-table" in c or n.tag == "table":
        return table(n)
    if n.tag in ("ul", "ol"):
        return listing(n, n.tag == "ol")
    if n.tag == "h2":
        num = n.find("span", "num")
        head = tidy(inline(n))
        if num:
            nt = tidy(inline(num))
            head = tidy(head[len(nt):]) if head.startswith(nt) else head
        return "## " + head
    if n.tag in ("h3", "h4"):
        return "### " + tidy(inline(n))
    if n.tag == "p":
        return tidy(inline(n))
    if n.tag == "pre":
        return "```\n%s\n```" % raw_text(n).strip("\n")
    if n.tag == "hr":
        return ""
    # a wrapper: keep walking
    return "\n\n".join(x for x in (block(k) for k in n.kids) if x)


def convert(src):
    """-> (title, summary, markdown). Raises ValueError if it is not a guide."""
    root = parse(src)
    body = None
    for n in root.walk():
        if "data-pagefind-body" in n.attrs:
            body = n
            break
    if body is None:
        body = root.find("main") or root
    h1 = body.find("h1")
    if h1 is None:
        raise ValueError("no <h1>: not a guide page")
    # the page splits its title over a <br> and puts the second half in <em>;
    # the site itself shows the two joined by an em dash
    head = []
    for k in h1.kids:
        if k.tag == "br":
            head.append(" — ")
        else:
            head.append(inline(k))
    title = re.sub(r"\s+", " ", "".join(head).replace("*", "")).strip(" —").strip()
    sub = body.find("p", "subtitle")
    summary = tidy(inline(sub)) if sub else ""

    parts = []
    main = body.find("main") or body
    for sec in main.kids:
        if sec.tag == "section" or sec.tag in ("h2", "p", "div", "ul", "ol", "table"):
            t = block(sec).strip()
            if t:
                parts.append(t)
    md = "\n\n".join(parts)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return title, summary, md


if __name__ == "__main__":
    import sys, urllib.request
    u = sys.argv[1]
    src = (open(u).read() if not u.startswith("http")
           else urllib.request.urlopen(u, timeout=60).read().decode("utf-8", "replace"))
    t, s, m = convert(src)
    print("# %s\n\n%s\n\n%s" % (t, s, m))
