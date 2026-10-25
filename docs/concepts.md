# Projects, sessions and files

## Projects

You work in **projects**. A project is an ordinary folder you can open in Finder or Explorer,
back up, or copy elsewhere, with a SQLite index inside it (`src/lectern/projects/`).

- **Where.** `<data>/users/<name>/projects/<uuid>/`: `<data>` is `~/.lectern/data` when
  installed (or `LECTERN_HOME`), and `<name>` is the owner's sign-in name, `local` until
  accounts are on.
- **The folder's name** is the project's UUID, set when it's made and never changed:
  renaming a project moves nothing. Projects made before that change keep their old folder.
- **A new project** starts with one empty chat, which is what it opens on.
- **Removing** a project asks for its name to be typed. Its chats and OCR jobs stop, and the
  folder moves to `<data>/users/<name>/trash/`, under the project's name and the time, for 30
  days; then it's deleted the next time Lectern starts.

```
<uuid>/                      the project "Lease archive 1998"
├── .lectern/
│   ├── project.sqlite       the index: sessions, transcripts, files and where they came
│   │                        from, script runs, OCR state, open tabs, full-text search
│   └── trash/               deleted files, for 30 days
├── lease-1998-scan.pdf      imported
├── lease-1998-scan/         an OCR session's Document (see OCR)
├── scripts/
│   ├── penalty.py           a script the coding model wrote
│   ├── penalty.json         its manifest and test run
│   └── out/                 files the scripts wrote
└── lease-1998-scan.odt      an export
```

## Sessions and tabs

A **session** is a place you work in, and opens in a tab of its own. There are two kinds: a
**chat** ([The chat](agent.md)) and an **OCR session** for one PDF or image
([OCR](ocr.md)).

**Overview** is always the first tab and can't be closed. It lists the project's sessions and
files like a folder, newest first, with each file's From. Every session or file opened becomes
another tab; the open tabs (up to 30) are remembered for each project, and a tab whose target
is gone is dropped. Closing a tab shows the one to its right, else its left, else Overview. A
tab a chat opens, such as a new OCR session, opens without taking focus.

## Files

Every file in the project is a project file (in the code, an artifact): something you
imported, or something Lectern made: a file the chat wrote, a script and its output, an OCR
session's Document, an export, a web page saved by the chat.

- **From.** Each file records where it came from: imported, made by a session, or made from
  another file (an export from its Document, a script's output from the script). Overview's
  From column and the file preview's *Where it came from* show it, with a link to the source;
  a saved web page shows its address.
- **Never overwritten.** Lectern always writes a new file; a taken name gets ` (2)`, ` (3)`.
- **Deleting** moves a file to `.lectern/trash/` for 30 days. In the chat it always asks
  first.
- **Imports** go in by upload, up to 2 GB a file. Which files open in the browser and which
  download is in [Security](security.md#files-and-paths).
- **Search.** Every text file and Document is indexed for full-text search (SQLite FTS5),
  in pieces the chat can cite: `notes.md #c3` for a file, `#b3.2` for a Document's block.

## The app's own data

Next to the projects, `<data>` holds the app database `lectern.sqlite` (accounts, logins,
settings, the list of projects, downloads), the downloaded models in `models/` and the
llama.cpp builds in `runners/`. Settings that come from the environment are in
`~/.lectern/lectern.env` when installed ([Architecture](architecture.md#settings)).

## First run

The first time Lectern opens, an admin goes through two steps (`api/firstrun.py`):

1. **Hardware.** The detected GPU and RAM, and the [tier](models.md#hardware-tiers) they give,
   with what it means. It also checks for Podman or Docker: without one, scripts can't run and
   exact answers are marked *not computed*, with a link to the setup steps and a button to
   check again.
2. **Download.** The chat model for the tier (or another from the list), showing its size,
   licence and the free disk space, and the llama.cpp build for the machine. On a Mac with MLX
   switched on, the chat model's MLX build is downloaded instead. Nothing else is downloaded
   up front: the OCR and coding models and the sandbox image come the first time they're
   needed, each after asking.

Then it opens the project list.
