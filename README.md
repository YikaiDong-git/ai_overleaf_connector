# AI Overleaf Connector

[![tests](https://github.com/YikaiDong-git/ai_overleaf_connector/actions/workflows/tests.yml/badge.svg)](https://github.com/YikaiDong-git/ai_overleaf_connector/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Let Claude Code, Codex, or any coding agent revise your Overleaf paper.**
The agent edits the LaTeX files in a folder on your machine, in batches and with your whole
repository at hand. `olsync` merges those edits with what your co-authors changed on Overleaf and
sends the result back, through Overleaf's official git integration. Co-authors keep working in the
browser and see the agent's revisions there.

```text
   you + Claude Code / Codex                              co-authors
   edit paper/ on your machine                            edit on overleaf.com
              \                                                /
               '------->  olsync sync  (three-way merge)  <---'
```

## Why

- **Agents are at their best on files.** Applying a round of reviewer comments, tightening every
  section, renaming a term across forty files, or regenerating figures from the analysis code are
  quick jobs in a local folder next to your code.
- **Co-authors stay on Overleaf.** Their edits arrive in your folder at every sync, and the agent's
  edits appear in their editor.
- **Everyone's edits survive.** Edits in different parts of a file merge automatically. Where both
  sides changed the same lines, olsync stops and shows you both versions, or applies the rule you
  choose: keep local, keep Overleaf, or keep the newer edit.
- **You see every change before co-authors do.** `olsync review` writes an HTML page with each
  pending edit as a word-level before/after diff, with Accept/Revise buttons.

## Features

- Three-way merge against the last synced snapshot, file by file and hunk by hunk. Text files are
  merged line by line; figures and other binary files travel byte for byte.
- Concurrency guard: `--expect <commit>` stops a push when Overleaf changed after you looked, and
  every push is a fast-forward on top of Overleaf's latest commit.
- Sends what the manuscript uses: files reached through `\input`, `\include`, `\includegraphics`,
  `\bibliography`, `\addbibresource` and local `.cls`/`.sty`/`.bst` files, plus everything already
  on Overleaf. Notes, scripts and data in the same folder stay local, so the paper folder can sit
  inside a larger code repository.
- Optional build check: compile locally after each sync and report page count, undefined
  references and missing characters next to the previous build's values; a failing build holds
  the push.
- Built for agents: `--json` output, documented exit codes, no interactive prompts, and the token
  passed through an environment variable.
- One Python file, standard library only; CI runs the tests on Linux, macOS and Windows.

## Requirements

- An Overleaf project with **git integration**. On overleaf.com it is a premium feature,
  [available](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration)
  when the project owner has a paid subscription or has been granted access to the feature (many
  institutions provide it). Overleaf Server Pro supports it as well.
- `git` and Python 3.9 or newer.
- An Overleaf git token: Overleaf > Account Settings > Git integration > generate token
  ([Overleaf help](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration/git-integration-authentication-tokens)).

## Install

**Claude Code, as a plugin**

```text
/plugin marketplace add YikaiDong-git/ai_overleaf_connector
/plugin install overleaf-connector@ai-overleaf-connector
```

**Claude Code, Codex, Cursor and other agents, with the skills CLI**

```bash
npx skills add YikaiDong-git/ai_overleaf_connector
```

**By hand**: copy `skills/overleaf-sync/` into `~/.claude/skills/` (Claude Code) or
`~/.agents/skills/` (Codex).

**Command line only**: `pipx install git+https://github.com/YikaiDong-git/ai_overleaf_connector`
installs an `olsync` command. The skill works without this step; it runs the bundled script.

## Quick start

Put the token in the environment your agent runs in, before you start the agent:

```bash
export OVERLEAF_TOKEN="<your token>"          # PowerShell: $env:OVERLEAF_TOKEN = "<your token>"
```

Or store it once in git's credential manager (username `git`, password the token); olsync uses
whatever git already knows. Then ask your agent, for example:

> Connect `paper/` to https://www.overleaf.com/project/0123456789abcdef01234567 and download it.

> Apply the reviewer comments in `reviews/round1.md` to the Methods and Discussion, show me the
> changes, then sync to Overleaf.

> What did my co-authors change on Overleaf since my last sync?

The same steps by hand:

```bash
olsync init https://www.overleaf.com/project/<id> --dir paper \
       --build "latexmk -pdf -interaction=nonstopmode main.tex"     # --build is optional
olsync sync --dir paper            # the first sync downloads the project
# ...edit...
olsync status --dir paper          # what differs, and which way it would move
olsync review --dir paper          # HTML page of every pending change
olsync sync --dir paper -m "Revise the Discussion"
```

## How it works

`paper/.olsync/` holds a bare git copy of the Overleaf project and a marker for the last synced
commit. Each sync compares three versions of every tracked file: the last synced snapshot,
Overleaf now, and your folder now.

| Overleaf since last sync | Your folder since last sync | Result |
|---|---|---|
| unchanged | changed | pushed |
| changed | unchanged | pulled |
| changed | changed, in different places | merged, then written to both sides |
| changed | changed, on the same lines | stops; edit by hand or pick a side with `--prefer` |

Before writing anything, olsync checks that every write stays inside the folder and that no two
project files would collide on a case-insensitive disk. Pulled files are then written one by one,
in a way that never overwrites an edit saved during the sync: the current file is moved aside and
checked, the new one is put in place only if nothing new appeared there, and an editor save at any
point stops the run with that save kept. Local changes are committed on top of Overleaf's latest commit and
pushed as a fast-forward. Finally olsync fetches Overleaf again, says so if a co-author edited
during the run, and confirms that every tracked file matches.

## Commands

| Command | What it does |
|---|---|
| `olsync init <url> --dir <folder>` | Connect a folder to an Overleaf project. |
| `olsync status [--json]` | List what differs and which way each file would move. |
| `olsync sync [--expect SHA] [-m MSG]` | Pull, merge, build check, push, verify. |
| `olsync sync --prefer local\|overleaf\|newer` | Resolve overlapping edits toward one side. |
| `olsync sync --pull-only` | Bring Overleaf changes in, push nothing. |
| `olsync review` | Write the HTML review page. |
| `olsync build` | Run the configured build and report on it. |

Exit codes, configuration keys and troubleshooting are in
[skills/overleaf-sync/reference.md](skills/overleaf-sync/reference.md).

## Good to know

- Files deleted on Overleaf are moved to `paper/.olsync/trash/` on your side, and so is the losing
  version whenever `--prefer` keeps one whole file. Deleting a file locally reaches Overleaf only
  with `--allow-delete`.
- Every push is an ordinary commit in the project's git history, so earlier versions stay
  retrievable.
- The token goes only to the project's own `https://` host, through git's credential mechanism;
  olsync keeps no copy of it.
- Files reached through symbolic links are read and sent; when Overleaf changes such a file,
  olsync stops and leaves the link target for you to update.
- `.olsync/` carries its own ignore rule, so your repository's `git status` stays clean.
- Overleaf notes that "pushes from Git to Overleaf can result in the loss or displacement of track
  changes and comments"; settle open comments on a passage before an agent rewrites it.
- Codex runs commands in a sandbox with network access off by default. Allow it for the session,
  for example `codex -c 'sandbox_workspace_write.network_access=true'`, or set
  `network_access = true` under `[sandbox_workspace_write]` in `~/.codex/config.toml`
  ([Codex docs](https://developers.openai.com/codex/agent-approvals-security)).
- On a free Overleaf plan the git integration is unavailable, so olsync cannot connect.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests run offline: a local git repository stands in for Overleaf, and a second clone plays a
co-author editing in the browser.

## License

[MIT](LICENSE)
