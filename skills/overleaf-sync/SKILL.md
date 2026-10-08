---
name: overleaf-sync
description: Edit an Overleaf paper in a local folder and keep it in two-way sync with Overleaf through Overleaf's official git integration. Use when the user wants to revise, proofread, restructure or batch-edit a paper that lives on Overleaf; asks to connect a folder to Overleaf, sync, pull from Overleaf or push to Overleaf; asks what co-authors changed on Overleaf; or before and after you edit files in a folder that contains a `.olsync/` directory.
license: MIT
compatibility: Needs git and Python 3.9+ with network access to the Overleaf git server. The Overleaf project needs git integration (a paid or institution-provided Overleaf plan, or Overleaf Server Pro).
---

# Overleaf sync

The paper lives in an ordinary local folder (for example `paper/` inside a research repository)
connected to one Overleaf project. You edit the files there; `olsync` merges your edits with what
co-authors changed on Overleaf and sends the result back.

Run the bundled CLI with Python 3. `<skill>` is the folder that contains this SKILL.md:

```
python3 <skill>/scripts/olsync.py <command> [options]      # "python" on Windows
```

If `olsync` is on PATH (installed with pipx), `olsync <command>` is the same program. Every command
takes `--dir <paper folder>`; without it, olsync finds the connected folder from the current
directory (the folder itself, a parent, or one or two levels below).

## Connect a folder (once per machine)

1. Ask for the Overleaf project URL (`https://www.overleaf.com/project/<id>`) and the local folder.
2. The user creates a git token in Overleaf (Account Settings > Git integration) and either sets
   `OVERLEAF_TOKEN` in the environment you run in or stores it in git's credential manager
   (username `git`). Ask the user to do this step themselves: the token belongs in their
   environment, outside the chat, command lines and files.
3. `olsync init <project-url> --dir <folder>`. Add `--main <file.tex>` when the main file cannot be
   detected, and `--build "<command>"` when the user compiles locally, for example
   `--build "latexmk -pdf -interaction=nonstopmode main.tex"`.
4. `olsync sync --dir <folder>` downloads the project. If the folder already held files that differ
   from Overleaf, they appear as conflicts; ask the user which copy is current.

## Editing workflow

1. **Start from the latest text:** `olsync sync`. Use `olsync sync --pull-only` when the folder
   holds local work the user is not ready to send.
2. **Edit** the `.tex`, `.bib` and figure files. Files reached through `\input`, `\include`,
   `\includegraphics`, `\bibliography`, `\addbibresource` and local `.cls`/`.sty`/`.bst` files are
   synced, including new ones; notes, scripts and data in the same folder stay local. A new file the
   paper does not reference yet is sent once a `.tex` file references it.
3. **Let the user check large batches:** `olsync review` writes an HTML page with every pending
   change as a before/after diff (`.olsync/review.html`); give the user its path.
4. **Send:** `olsync status --json`, then
   `olsync sync --expect <overleaf_head> -m "<one line on what changed>"`.
5. **Report** in a short table: Overleaf commit before and after, files pulled, merged and pushed,
   conflicts, build result.

## Reading `olsync status --json`

- `overleaf_head`: pass it to `sync --expect`, so a co-author edit made after you looked stops the
  run (exit 4) and you re-check instead of pushing a plan nobody saw.
- `changes[].state`:

  | state | meaning | what `sync` does |
  |---|---|---|
  | `push` / `new-local` | changed or added locally | sends it to Overleaf |
  | `pull` / `new-overleaf` | changed or added on Overleaf | writes it locally |
  | `merge` | both sides edited, in different places | merges, writes both sides |
  | `conflict` | both sides edited the same lines | stops; see below |
  | `deleted-local` | missing locally | keeps it on Overleaf unless `--allow-delete` |
  | `deleted-overleaf` | removed on Overleaf | moves the local copy to `.olsync/trash/` |

- `missing_references`: files the LaTeX references that exist on neither side. Mention them.

## When both sides edited the same lines

`sync` stops with exit code 3 and changes nothing. Show the user the files with both edit times
(`local_edit_time`, `overleaf_edit_time`); `olsync review` shows the two versions side by side.
Then, as the user decides:

- edit the file so both intentions are kept, and run sync again;
- `--prefer overleaf` or `--prefer local`: the overlapping lines take that side, and every
  non-overlapping edit from both sides is kept;
- `--prefer newer`: the overlapping lines take whichever side was edited last. An edit against a
  deletion (on either side) needs an explicit `--prefer local` or `--prefer overleaf`.

A file that cannot be merged line by line (first sync of a folder that already had files, binary
files, edit against deletion) is kept whole from the chosen side, and the other version is saved
under `.olsync/trash/<time>/local/` or `.olsync/trash/<time>/overleaf/`. Tell the user where.

When the user has given a standing rule (for example "the most recent edit wins"), apply it
without asking again.

## Rules

- Sync only through olsync. `.olsync/` holds the git copy of the project and the last synced
  snapshot; leave it untouched, and leave the Overleaf git URL to olsync.
- Describe co-author changes by file; summarize their wording only when the user asks.
- Exit 5 means the configured build failed and nothing was pushed: fix the LaTeX error, or ask the
  user before passing `--no-build`.
- Sync also stops before writing when a pull would go through a symbolic link, when a folder and
  a file swap names, or when two project files differ only in letter case on a case-insensitive
  disk; and it stops mid-run if a file changes on disk while it works (an editor save). Relay the
  message; for the last case just run sync again.
- After a sync, a note that Overleaf "has moved on" means a co-author edited during the run; sync
  again to bring that in.
- A network error inside a sandbox (Codex blocks network by default) needs the user to allow
  network access for the session; an authentication error needs a token, as in step 2 above.

Exit codes: 0 done, 1 usage or setup, 2 git, network or authentication, 3 conflicts, 4 Overleaf
changed meanwhile, 5 build failed. Details and configuration: [reference.md](reference.md).
