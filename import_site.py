#!/usr/bin/env python3
"""Import a deck-style static site into the knowledge base as markdown.

The site is expected to publish `<deck>/index.json` listing its articles, and
a rendered page per article; the page is what gets converted, because the
index's own `content` field is a flattened search blob with the code stripped.

    export IMPORT_SITE=https://example.github.io
    export IMPORT_REPO_API=https://api.github.com/repos/you/you.github.io/contents/
    python3 import_site.py            # import everything
    python3 import_site.py --dry      # show what would happen, change nothing

Personal details are removed on the way in by the patterns in a local
redactions.py; see redactions.example.py.
"""
import sys, os, re, json, html, argparse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
import site_md

SITE = os.environ.get("IMPORT_SITE", "").rstrip("/")
REPO_API = os.environ.get("IMPORT_REPO_API", "")

# Patterns that strip personal details on the way in - names, addresses, home
# directory paths. They are nobody else's business, so they live in a local
# redactions.py that is not committed. See redactions.example.py for the shape.
try:
    from redactions import REDACTIONS
except ImportError:
    REDACTIONS = []


AUTHOR = ""


def clean(text):
    if not text:
        return ""
    # the site serves its titles and summaries already escaped, and they used to
    # land in the database that way: "OpenOCD &amp; JTAG" rendered as written
    text = html.unescape(text).replace("\xa0", " ")   # nbsp would not tokenise
    for pat, rep in REDACTIONS:
        text = pat.sub(rep, text)
    return text.strip()


def redacts(text):
    """True only if a personal detail was actually removed - unescaping and
    whitespace tidying are not redactions and must not be counted as such."""
    return any(pat.search(text or "") for pat, _ in REDACTIONS)


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def decks():
    d = json.loads(fetch(REPO_API))
    return sorted(f["name"] for f in d if f["type"] == "dir" and f["name"].endswith("-deck"))


def article_md(url):
    """Fetch one guide page and return (title, summary, markdown)."""
    return site_md.convert(fetch(url))


def main():
    global SITE, REPO_API, AUTHOR
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=SITE, help="base url of the site")
    ap.add_argument("--repo-api", default=REPO_API,
                    help="github contents api url, used to list the decks")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--only", help="one deck name")
    ap.add_argument("--refresh", action="store_true",
                    help="re-render guides already in the library, keeping their ids")
    a = ap.parse_args()
    SITE, REPO_API = a.site.rstrip("/"), a.repo_api
    if not SITE or not REPO_API:
        print("set --site and --repo-api (or IMPORT_SITE / IMPORT_REPO_API)")
        return 2
    AUTHOR = SITE.split("//", 1)[-1].split("/")[0]
    if not REDACTIONS:
        print("  note: no redactions.py, nothing will be stripped from the text")

    names = [a.only] if a.only else decks()
    print(f"{len(names)} deck")
    # match on the url, not the title: the markdown title comes from the page's
    # <h1> and can differ from index.json's, which silently created duplicates
    rows = store.all_notes(limit=100000)
    by_url = {n["url"]: n["id"] for n in rows if n.get("url")}
    # the title fallback is only for rows imported before urls were recorded, and
    # only when the title is unique: two decks both publish "ARM Semihosting" and
    # matching on that overwrote one guide with the other, losing it
    seen = {}
    for n in rows:
        if not n.get("url"):
            for form in {n["title"], html.unescape(n["title"])}:
                seen[form] = None if form in seen else n["id"]
    by_title = {t: i for t, i in seen.items() if i is not None}
    total = added = updated = skipped = failed = redacted = 0

    for deck in names:
        try:
            idx = json.loads(fetch(f"{SITE}/{deck}/index.json"))
        except Exception as ex:
            print(f"  {deck:<24} okunamadi: {ex}")
            continue
        arts = idx.get("articles", [])
        if not a.dry:
            store.set_deck(deck.replace("-deck", ""), clean(idx.get("title")),
                           clean(idx.get("summary")), f"{SITE}/{deck}/")
        n_add = n_upd = n_fail = 0
        for pos, art in enumerate(arts, 1):
            total += 1
            url = art.get("url") or ""
            fallback_title = clean(art.get("title", ""))
            if not url:
                skipped += 1
                continue
            # an old row's title may still carry entities (&mdash;), and the
            # page's <h1> can differ from index.json's, so try both forms
            existing = (by_url.get(url) or by_title.get(fallback_title)
                        or by_title.get(html.unescape(fallback_title)))
            if existing and not a.refresh:
                skipped += 1
                continue
            try:
                raw_title, raw_summary, raw_md = article_md(url)
            except Exception as ex:
                n_fail += 1
                failed += 1
                print(f"    {url} -> {ex}")
                continue
            title = clean(raw_title) or fallback_title
            summary = clean(raw_summary)
            body_md = clean(raw_md)
            if redacts(raw_title) or redacts(raw_summary) or redacts(raw_md):
                redacted += 1
            if not title or not body_md:
                skipped += 1
                continue
            tags = " ".join(filter(None, [
                deck.replace("-deck", ""),
                (art.get("category") or "").lower(),
                (art.get("topic") or "").lower(),
                (art.get("slug") or "").replace("/", " "),
            ]))
            full = (summary + "\n\n" if summary else "") + body_md
            src = dict(deck=deck.replace("-deck", ""), slug=art.get("slug", ""),
                       url=url, category=clean(art.get("category")),
                       topic=clean(art.get("topic")), ord=pos)
            if not a.dry:
                if existing:
                    store.update(existing, title, full, tags, AUTHOR, **src)
                else:
                    existing_id = store.add(title, full, tags, AUTHOR, **src)
                    by_url[url] = existing_id
                    by_title[title] = existing_id
            if existing:
                n_upd += 1
                updated += 1
            else:
                n_add += 1
                added += 1
        print(f"  {deck:<24} {len(arts):>3} makale, {n_add:>3} yeni, {n_upd:>3} yenilendi"
              + (f", {n_fail} basarisiz" if n_fail else ""))

    print(f"\n  toplam {total} makale | yeni {added} | yenilenen {updated}"
          f" | atlanan {skipped} | basarisiz {failed}"
          f" | icinde kisisel bilgi temizlenen {redacted}")
    if a.dry:
        print("  (--dry: hicbir sey yazilmadi)")
    else:
        c = store.count()
        print(f"  kayitli: {c['n']} not, {c['b']} karakter")


if __name__ == "__main__":
    main()
