A toolkit at {BASE_URL} reads documents and answers from the team's own notes.

Get the tool once:

    [ -f minitoolkit.py ] || curl -sO {BASE_URL}/minitoolkit.py

## The team's notes — three levels

**1. Keyword search.** No model, sub-second. Titles and scores only.

    python3 minitoolkit.py search "swd debug"
    python3 minitoolkit.py get 261                    # one article in full, as markdown

Articles come back as markdown - headings, tables, fenced code - so read them as
written rather than guessing at structure.

**2. Get the articles.** The model reads the shortlisted titles and picks which
are worth opening, then you get those articles' full text with their source
links — nothing is paraphrased. A few seconds. **Prefer this**: you read the
source better than a small model summarises it.

    python3 minitoolkit.py ask "stm32 swd debug" --direct
    python3 minitoolkit.py ask "stm32 swd debug" --direct --no-pick   # skip the model

`--limit N` how many articles, `--chars N` how much of each.

It tells you what the reply costs — `16605 tokens, 48242 chars, 4 truncated` —
counted by the model's own tokenizer, so you can size it against your own
context before reading it in. Raise `--limit`/`--chars` if you have room, lower
them if you do not.

**3. Have it answered.** The model picks, reads, and writes an answer citing
`[#id]`. 20-90 seconds.

    python3 minitoolkit.py ask "how do we wire SWD for STM32?"

If it says the notes do not cover the question, they genuinely do not. Say so
rather than filling the gap, or answer from your own knowledge and make that
clear.

Over HTTP:

    curl -s -X POST {BASE_URL}/api/v1/ask \
      -H 'Content-Type: application/json' \
      -d '{"question": "...", "mode": "direct"}'   # or "summary"

## Read a document

    python3 minitoolkit.py read scan.pdf
    python3 minitoolkit.py read photo.jpg --pages 1,5
    python3 minitoolkit.py read book.pdf --split pages/

PDF, PNG, JPG, WEBP, BMP, TIFF, GIF. Every page of a PDF is read; tables come
out as markdown. A page with a chart or drawing also gets a `--- figure ---`
note explaining it; the numbers always come from the text, not that note.

**About 30 seconds per page.** A 20-page PDF takes ten minutes, longer than
most command timeouts. Raise the timeout or run it in the background:

    nohup python3 minitoolkit.py read book.pdf --out text.txt > run.log 2>&1 &
    tail -3 run.log

Never kill it and start over — you lose the work and the new run queues behind
the old one.

## Write something down

    python3 minitoolkit.py add notes.txt --title "Board bring-up" --tags "stm32 hw"
    echo "..." | python3 minitoolkit.py add --title "..." --tags "..."

Tags and title carry most of the search weight, so name it the way someone
would ask for it.

## Shared skills

Procedures the team has packaged up, each a folder with a `SKILL.md` and
whatever it needs to run offline - wheels, vendored source, prebuilt binaries.

    python3 minitoolkit.py skill list
    python3 minitoolkit.py skill show stm32-debug
    python3 minitoolkit.py skill install stm32-debug --with-wheels

Install writes files and runs nothing on its own; `--with-wheels` additionally
pip-installs the shipped wheels with `--no-index`, so nothing is fetched from
the network. Read SKILL.md before following it. It installs into
`~/.claude/skills/` and registers the skill with opencode in the same step;
`--no-opencode` skips that half.

To publish one you have built: `python3 minitoolkit.py skill publish ./my-skill`.

`skill installed` says what is on this machine and whether it is on; `skill
disable NAME` keeps the files but hides it, `skill enable NAME` puts it back,
`skill uninstall NAME` deletes it. Those four do not need the server.

## Notes

- `status` shows what is loaded and how big the library is.
- The library is grouped into decks. `GET /api/decks` lists them and
  `GET /api/deck/<name>` gives that deck's guides in their published order,
  if you would rather walk the subject than search it.
- Reading and answering use different models and the GPU holds one at a time,
  so a request may pause a second or two while they swap. Searching does not.
- Add `--json` for the raw reply, `--out FILE` to write to a file, `-q` for no
  progress messages. Flags work before or after the subcommand.
