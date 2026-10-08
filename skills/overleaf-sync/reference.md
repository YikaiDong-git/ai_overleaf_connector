# olsync reference

## Commands

| Command | Purpose |
|---|---|
| `olsync init <url> [--dir D] [--main F] [--build CMD]` | Connect folder `D` to an Overleaf project and show the first comparison. |
| `olsync status [--json] [--no-fetch]` | Fetch Overleaf and list every file that differs, with the direction it would move. |
| `olsync sync [options]` | Pull, merge, check the build, push, then fetch again and re-check. |
| `olsync review [-o FILE] [--no-fetch]` | Write an HTML page with every pending change as a word-level diff. |
| `olsync build [--json]` | Run the configured build command and report on it. |

`sync` options:

| Option | Effect |
|---|---|
| `--expect SHA` | Stop (exit 4) unless Overleaf is still at this commit. |
| `--prefer local\|overleaf\|newer` | Resolve overlapping edits toward one side; other edits from both sides are kept. |
| `--allow-delete` | Also delete on Overleaf the files that were deleted locally. |
| `--pull-only` | Bring Overleaf changes in and push nothing. |
| `--no-build` | Skip the configured build check. |
| `-m MSG` | Message stored with the pushed commit. |
| `--dry-run` | Show the plan and change nothing. |
| `--json` | Machine-readable result. |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done. |
| 1 | Usage or setup problem, or a pre-write check stopped the run (message on stderr). |
| 2 | git, network or authentication problem. |
| 3 | Both sides edited the same lines, or `--prefer newer` met an edit against a deletion; nothing was changed. |
| 4 | Overleaf changed after `status` (with `--expect`) or during the push; run `status` again. |
| 5 | The configured build failed; Overleaf changes were pulled, nothing was pushed. |

## Which files are synced

1. Every file in the Overleaf project, always.
2. Every local file the manuscript references, found by reading the main file and all tracked
   `.tex` files: `\input`, `\include`, `\subfile`, `\import`/`\subimport`, `\includegraphics`
   (with `\graphicspath` and the usual image extensions; an ambiguous name sends every match),
   `\includesvg`, `\includepdf`, `\lstinputlisting`, `\inputminted`, `\verbatiminput`,
   `\bibliography`, `\addbibresource`, and local `.cls`, `.sty` and `.bst` files.
3. Local files matching the `include` globs in the configuration.

`exclude` patterns keep local-only files from being sent; the defaults cover LaTeX build output
(`*.aux`, `*.log`, `*.bbl`, `*.synctex.gz`, ...) and the compiled main PDF. Arguments built from
macros (containing `\` or `#`) are skipped, so a figure named through a macro needs an `include`
glob.

Text formats (`.tex`, `.bib`, `.bst`, `.cls`, `.sty`, `.txt`, `.md`, `.csv`, `.json`, `.yaml`,
`.py`, `.r` and similar, plus `latexmkrc`) are merged line by line and compared with LF line
endings, so a CRLF copy of an identical file counts as identical. Every other file, including
PDF, EPS and SVG figures, is compared and copied byte for byte.

## Configuration: `<folder>/.olsync/config.json`

| Key | Meaning |
|---|---|
| `remote` | Overleaf git URL (`https://git.overleaf.com/<id>`, or a Server Pro URL). |
| `branch` | Branch served by Overleaf (detected at `init`). |
| `main` | Main `.tex` file, relative to the folder. |
| `build` | Shell command run in the folder after each sync that changes something (except with `--pull-only`); `null` to skip. |
| `build_timeout` | Seconds before the build is stopped (default 900). |
| `log` | The final LaTeX log, relative to the folder, when the build writes it elsewhere (for example `build/main.log` with `latexmk -outdir=build`); `null` means `<main>.log`. |
| `include` | Extra glob patterns of local files to sync, e.g. `"figures/**/*"`. |
| `exclude` | Glob patterns of local-only files never sent (matched against the path and the file name). |

Other contents of `.olsync/`: `repo.git` (bare clone of the project; the ref `refs/olsync/base`
marks the last synced commit), `state.json` (last build metrics, pending chosen deletions),
`review.html`, `trash/`, and a `.gitignore` containing `*` so the folder stays out of any
surrounding repository.

## How a sync runs

1. Fetch Overleaf. With `--expect`, stop if its head moved.
2. Compare each tracked file in three versions: last synced snapshot, Overleaf now, folder now.
3. Stop on overlapping edits unless `--prefer` was given.
4. Check before writing anything: two project paths that differ only in letter case or Unicode
   form on a case-insensitive disk, a pull that would write through a symbolic link (anywhere on
   its path), or a folder and a file swapping names stop the run. Files reached through links are
   still read and sent.
5. Write pulled and merged files. For each one, the new content is prepared in a fresh temporary
   file; the current file is moved aside and checked against the version that was compared; the
   new file is then put in place only if nothing new appeared at that path meanwhile. An editor
   save at any point stops the run with that save kept (in place, or in `.olsync/trash/` with the
   path printed). The result keeps the old file's permissions and is read back to confirm. Files deleted on
   Overleaf move to `.olsync/trash/<time>/local/`. Where `--prefer` keeps one whole file, the other
   version is saved under `.olsync/trash/<time>/local/` or `.olsync/trash/<time>/overleaf/`; saved
   copies never replace each other. Text files that used CRLF keep CRLF.
6. Run the build command, if configured; a failure stops here.
7. Commit the local changes on top of Overleaf's head and push. The push is a fast-forward, so a
   co-author edit that lands meanwhile makes it fail (exit 4) and leaves Overleaf untouched.
8. Fetch again, report whether Overleaf moved on during the run, and confirm every tracked file
   matches the synced commit.

## Authentication

olsync never stores the token. With `OVERLEAF_TOKEN` set, it hands the token to git through a
credential helper scoped to the project's own `https://` host, so git offers it to no other
server; plain `http://` remotes never receive it. Without the variable, git uses whatever
credential manager it already has. All prompts are disabled, so a missing credential fails fast.

## Build report

After a build, olsync reads `<main>.log` (or the `log` setting; or the command's output when no
fresh log exists) and reports the page count, undefined references and citations, and "Missing
character" warnings (characters the font cannot draw, which print as nothing). Each value is shown
next to the previous successful build's value, so a sync that introduces a problem stands out.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Authentication failed`, `could not read Username` | Provide a token: `OVERLEAF_TOKEN`, or git's credential manager with username `git`. |
| `Repository not found` / HTTP 403 | Check the project URL, and that the project owner's plan includes git integration. |
| Network errors under Codex | Allow network access for the session (Codex sandboxes block it by default). |
| `Another olsync run is using this folder` | A previous run was interrupted; delete `.olsync/lock`. |
| `symbolic link` in the stop message | Update the link target by hand, or replace the link with a regular file. |
| `folder where Overleaf has a file` (or the reverse) | Move the local folder or file aside, then sync again. |
| `changed on disk during the sync` | An editor saved meanwhile; run sync again. |
| `differ only in letter case` | Rename one of the two files on Overleaf. |
| A figure is not pushed | Check `status`; a figure named through a macro needs an `include` glob. |
| Start over | Move `.olsync/` away (keep its `trash/` if needed) and run `init` again. |
