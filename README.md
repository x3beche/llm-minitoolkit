# llm-minitoolkit

A small toolkit for a machine with **one GPU and no internet**: it reads
documents, answers questions from your own notes, and hands the whole thing to
whatever model you already use.

Two models take turns on the card — an OCR model for pages, a small instruct
model for questions — because 2.5 GB of VRAM does not hold both. Swapping costs
a second or two and the manager will not pull a model out from under a reply
that is still streaming.

Python standard library plus llama.cpp. No framework, no service, no account,
nothing fetched at runtime. One `./run.sh` and a browser.

## What it does

- **ocr** — drop pages or a 300-page PDF; tables come out as markdown, a page
  that is mostly a diagram also gets a short note describing it, and a read page
  can be saved into the library as a searchable note.
- **knowledge** — your notes, at three levels: keyword **search** (no model, under
  a second), **fetch** (the model picks from the titles and you get those articles
  in full, nothing paraphrased), and **ask** (a written answer citing `[#id]`).
  Every reply shows what it cost — tokens, rate, how much of the window it used —
  counted by the model's own tokenizer rather than guessed.
- **skills** — packaged procedures other models install and follow. A skill
  carries what it needs to run offline: python wheels, vendored source, prebuilt
  binaries. One line installs it into Claude Code and opencode at once.
- **a client** — `minitoolkit.py`, one file, served by the app itself. Give a model
  the prompt at `/api/prompt` and it can search, read your PDFs, write notes back
  and install skills on its own.

There is no user system and no authentication. It is meant for a network where
everyone on it is already trusted.

The interface is one page, styled after [opencode](https://opencode.ai).

## Requirements

| | Check |
|---|---|
| NVIDIA driver + CUDA (or CPU only) | `nvidia-smi` |
| llama.cpp with `llama-server` | `llama-server --version` |
| Python 3.9+ | `python3 -V` |
| Models in `models/` | see below |

Everything else is the standard library. `install_deps.py` puts the two optional
wheels (`pypdfium2` for PDFs, `Pillow` for image handling) into `lib/` if they
are missing and there is a network; on an offline machine, copy them in.

## Run

```bash
./run.sh                          # every interface, port 3333
./run.sh --preload kb             # load a model at startup instead of on first use
./run.sh --prompt-ip 192.0.2.10  # address the LLM prompt should advertise
./stop.sh
```

## Models

Drop the GGUFs into `models/`; the profiles are built from whatever is there.

| Profile | Files | VRAM when loaded | Used for |
|---|---|---|---|
| `ocr` | `GLM-OCR-*.gguf` + its mmproj | 1804 MiB | page text, tables |
| `kb` | `Qwen3-*.gguf` | 2440 MiB | answering questions |
| `vision` | `Qwen2.5-VL-*.gguf` + mmproj | 0 (CPU) | explaining figures |

The knowledge model runs **fully on the GPU** with a q4_0 KV cache at 8192
context. That combination was picked by measurement, not by size:

| Setup | 3 questions | Correct values |
|---|---|---|
| IQ4_XS, partial offload, ctx 16384 | 67.4 s | 5/8 |
| IQ3_XXS, full GPU, ctx 8192 | far worse — rambled, invented a register name | 3/4 on one question |
| **Q3_K_M, full GPU, ctx 8192, KV q4_0** | **57.9 s** | **8/8** |

The smaller quant winning was not expected. Raw generation speed is not the
thing that matters: IQ3_XXS generated 18.9 words/s against IQ4_XS's 11.4 and
still took three times longer end to end, because it produced 2219 characters
where the others produced 116 and stopped citing its sources. Full f16 KV would
not fit at all — the weights alone left no room.

Full GPU only fits when the card is quiet. Opening a few browser tabs takes it
below what the weights need, and the same command that worked a minute ago runs
out of memory. So the profile declares fallbacks and steps down on its own:
full GPU → full GPU with a 4096 context → `-ngl 20` → `-ngl 12` → CPU. The
status line names the variant it settled on.

`WB_KB_CTX` changes the context size. `WB_KB_NGL` sets where the chain starts —
leave it unset on a machine with ~2.5 GB free so the first attempt wins, set it
low on a busy card to skip an attempt that is going to fail anyway.

`ocr` and `kb` together would need 3046 MiB, more than a 2.4 GB budget, so the
one a request needs is loaded and the other stopped. **Swapping costs 1-2
seconds** — the weights are already in the page cache and llama.cpp maps them,
which is nothing against the 25 s a page takes or the minute a question takes.
Keeping both resident was not worth the memory. Search costs nothing at all:
no model is involved.

## The web interface

Three tabs, `ocr`, `knowledge` and `docs`. The top right shows which model is
loaded and lights up while it swaps.

- **ocr** — drop pages or PDFs, watch the text stream in, copy or download it, or
  **save to library** to turn a read page into a searchable note.
- **knowledge / ask** — a question, a streamed answer, and the notes it came from.
- **knowledge / add** — write a note, or drop `.md` files in. Title and tags carry most of
  the search weight. **copy writing prompt** gives a model the format to write in
  (`WRITE-PROMPT.md`); its reply, saved as a file and dropped here, is parsed into title,
  tags and body for review before saving. Several at once is fine.
- **knowledge / notes** — the library, laid out the way the site publishes it. **select**
  turns on multi-select for deleting several at once; a single note can also be deleted from
  its own page.
  a catalog of decks, then that deck's guides, then the guide itself rendered
  from markdown with its tables, code blocks and callouts intact. Notes written
  here rather than imported sit under their own deck so nothing is unreachable.
- **skills** — packaged procedures: browse them, read their files, publish a folder.
- **docs** — what the thing is and how to hand it to another model, with the
  prompt one click away. Written for whoever opens the address, not for us.

The mode toggle and the ask button lock while an answer is streaming, so a run
cannot land under the other mode's rules.

## The command line

```bash
curl -sO http://<host>:3333/minitoolkit.py

python3 minitoolkit.py search "swd debug"       # instant, no model
python3 minitoolkit.py get 261                  # one note in full
python3 minitoolkit.py ask "how do we wire SWD?"
python3 minitoolkit.py read scan.pdf --split pages/
python3 minitoolkit.py add notes.txt --title "..." --tags "..."
python3 minitoolkit.py status
```

The copy you download has this server's address baked in. Text goes to stdout
and progress to stderr, so it pipes. `--json`, `--out FILE` and `-q` work
before or after the subcommand.

A ready-made prompt for an assistant is at `GET /api/prompt`, or the
**copy LLM prompt** button in the sidebar.

## What the figures under a result mean

Every number shown is one llama-server reported for that request, or a count from the model's
own tokenizer (`/tokenize`) — nothing is estimated from character counts.

- **search** — hits, time, and that no model was loaded.
- **fetch** — how many tokens the returned articles come to, so you can size the payload against
  your own context before pasting it anywhere; plus what the picking step itself cost.
- **ask** — tokens written and the rate, time to first token, how much of the model's window the
  prompt filled, and what picking cost on top.

The context figure turns amber past 85%: that is the point where notes start being cut off
rather than read.

## Two ways to use the notes

**direct** — keyword search draws up a shortlist, the model is shown the titles
and picks which are worth opening, and you get those articles in full with their
source links. Nothing is paraphrased, so nothing is invented; a few seconds. An
assistant with a large context is better served by the source text than by a
small model's precis of it. `--no-pick` (API: `"pick": false`) skips the model
and returns the top keyword hits in well under a second.

**summary** — the model reads the shortlist and answers from the few notes that
fit, citing `[#id]`. 20-90 seconds.

The web interface has a toggle on the ask tab; the CLI takes `--direct`; the API
takes `"mode": "direct"`.

### How summary mode works

The library can grow far past what the model could read at once, so:

1. **Keyword search** ranks every note; titles and tags count three times over.
2. **The model reads the shortlist** — twelve titles with a preview — and says
   which are worth opening, at most four.
3. **It answers from those only**, citing `[#id]`.

Retrieval is BM25 with three corrections, each added because the plain version
got a real question wrong:

- A wider stop list. With a thin one, *"how do I flash firmware over serial"*
  ranked a note about descaling the coffee machine first — it contains "Do it
  monthly" and is short.
- `b = 0.45` instead of 0.75, so short notes stop winning on length alone.
- A crude stemmer. Without it *"stm32 debugging"* ranked a bootloader note above
  one titled *"SWD debug wiring"*, because the query said debugging and the note
  said debug.

## Skills

A skill is a folder with a `SKILL.md` at its root — YAML front matter giving `name` and
`description`, then instructions another model follows. Everything else in the folder travels
with it.

**It has to work with no internet.** Python dependencies ship as wheels in `wheels/` with a
`requirements.txt` and are installed with `pip --no-index --find-links`, which never reaches the
network. Anything compiled ships as source under `src/` with a Makefile, or as a binary under
`bin/`. A skill that would download something at install time is not finished.

```bash
python3 minitoolkit.py skill list
python3 minitoolkit.py skill show stm32-debug
python3 minitoolkit.py skill install stm32-debug --with-wheels     # ~/.claude/skills/
python3 minitoolkit.py skill install stm32-debug --no-opencode     # skip the AGENTS.md block
python3 minitoolkit.py skill publish ./my-skill                    # from a folder
```

Install writes into `~/.claude/skills/<name>/` **and** registers the skill with opencode by
writing a block into `~/.config/opencode/AGENTS.md`, because a skill installed into one tool and
missing from the other is what people forget. The block is replaced rather than duplicated on a
reinstall; `--no-opencode` skips it.

Install writes files and **runs nothing** on its own — installing a skill means taking somebody
else's instructions and scripts onto your machine, so it shows what it is about to write and
stops there. `--with-wheels` is the explicit opt-in to run pip.

`SKILL-PROMPT.md` (`GET /api/skill-prompt`, or the button on the publish tab) tells a model how
to author one, including the offline rule.

### Using a skill once it is installed

**opencode** loads its global `AGENTS.md` on every run, and the install put a block there, so it
already knows the skill exists. Open it and ask in plain words — the block points at the
SKILL.md and it reads it itself:

```
opencode
> flash the firmware over serial
```

**Claude Code** picks up anything under `~/.claude/skills/`, so it needs nothing further. Ask,
or name it: `/uart-tool`.

Neither tool is required. The folder is just files; any model that can run a command can read
`minitoolkit.py skill show <name>` and follow it.

### Turning one off, or taking it away

```bash
python3 minitoolkit.py skill installed            # what is here, and whether it is on
python3 minitoolkit.py skill disable uart-tool    # keep the files, hide it from both tools
python3 minitoolkit.py skill enable uart-tool
python3 minitoolkit.py skill uninstall uart-tool  # delete the folder and the block
```

`disable` moves the folder to `~/.claude/skills/.disabled/` and takes the block out of
`AGENTS.md`; `enable` puts both back. `uninstall` deletes them. Either way the skill is still
published on the server, so installing it again is one line. `--tool claude|opencode` does only
one half, and `--to DIR` works on a different location.

These four run entirely locally — the server does not have to be up.

## Running and removing the app itself

```bash
./run.sh                   # foreground, ctrl-c to stop
./stop.sh                  # stops whatever is listening on the port
```

To start it with the machine, a user unit is enough — no root, and it stops when you log out
unless you enable lingering:

```ini
# ~/.config/systemd/user/llm-minitoolkit.service
[Unit]
Description=llm-minitoolkit
[Service]
WorkingDirectory=%h/llm-minitoolkit
ExecStart=%h/llm-minitoolkit/run.sh
Restart=on-failure
[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now llm-minitoolkit     # on
systemctl --user disable --now llm-minitoolkit    # off, stays installed
loginctl enable-linger $USER                      # keep it running after logout
```

Removing it is deleting the folder. **Take a backup first** — `data/notes.db` is the only copy
of everything anyone wrote:

```bash
curl -OJ http://<host>:3333/api/backup.db
./stop.sh && rm -rf ~/llm-minitoolkit
```

Skills installed on other machines are not affected by any of this; they are plain folders
under `~/.claude/skills/` and are removed with `skill uninstall`.

Uploads go up one raw body per file (`POST /api/skill-stage/<name>?path=…`, then
`/api/skill-publish/<name>`), so a 40 MB wheel costs 40 MB on the wire rather than 53 MB of
base64, and a half-finished upload leaves the previous version standing. Limits: 128 MB a file,
512 MB a skill, 2000 files.

## Backing it up

```bash
curl -OJ http://<host>:3333/api/backup.db      # the database, taken through sqlite's own backup
curl -OJ http://<host>:3333/api/export.zip     # one .md per note, plus library.json
```

Both are also links in the sidebar. The `.db` is the exact backup — restore it by stopping the
server and copying it over `data/notes.db`. The zip is for reading and for moving notes to
another instance: each `.md` is in the shape the add tab accepts, and `library.json` carries the
fields the markdown does not (deck, slug, source url).

## Checking the interface

`tests-ui.js` drives the page in headless Chrome and asserts what it actually does — every check
in it covers a behaviour that was once broken:

```bash
python3 ~/.claude/skills/web-ui-check/uicheck.py test http://127.0.0.1:3333/ tests-ui.js
```

`tests-upload.js` drops real files on the page and checks the parsing, the refusals and that
what comes back out renders. `tests-metrics.js` drives all three levels for real and checks that the figures under each
result are present and shaped right. `tests-own.js` covers writing a note and reading it back.

`tests-md.js` is the heavier one: it renders every note in the library through the page's own
markdown engine and checks the counts. It is what caught a `|a|b|` row with no separator under it
spinning the renderer forever, and a table separator regex that silently matched nothing.

Both exit non-zero on any failure and need nothing but python3 and a chrome binary, so they run
on the offline machine too.

## Having a model write a guide

`WRITE-PROMPT.md`, served at `GET /api/write-prompt` and behind the **copy writing prompt**
button, tells a model exactly what a guide here looks like: `# Title` first, a `tags:` line,
a summary paragraph, numbered `## NN` sections, fenced code with the indentation preserved,
markdown tables, blockquote callouts, a gotchas section and a sources list. It also says to
name where every number came from and to admit an unverified claim rather than invent one.

Dropping the reply on **knowledge → add** parses it: `# Title` or YAML front matter becomes
the title, the `tags:` line becomes the tags, and everything after is the body. A file with
no title falls back to its filename; a file with no text under the title is refused.

## Where the guides come from

`import_site.py` reads each deck's `index.json` for the running order, then
fetches every guide's **rendered page** and converts it to markdown with
`site_md.py`. The index's own `content` field is a flattened search blob —
headings run into the prose and every code block is gone — so it is used only
for ordering and tags.

Guides are matched on their **url**, never their title: two decks both publish
"ARM Semihosting", and matching on the title overwrote one with the other and
lost it. `--refresh` re-renders guides already in the library, keeping their ids
so `[#id]` citations stay valid.

Personal details are stripped on the way in (`REDACTIONS` in `import_site.py`).
The count printed at the end is of guides where something was actually removed —
unescaping and whitespace tidying are not redactions and are not counted.

## Filling the library

`import_site.py` pulls the decks from a static site that publishes
`<deck>/index.json`, one note per article, and strips personal details on the
way in. Re-running it only adds articles that are new; it never touches notes
people wrote by hand.

## Storage

`data/notes.db`, one SQLite file. Back it up by copying it.
`work/` holds temporary page images and can be deleted at any time.

## Security

Default `--host 0.0.0.0` means anyone on the network can read, add and delete
notes and run OCR, with no authentication. Use `--host 127.0.0.1` to keep it
local.
