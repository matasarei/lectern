# M1a acceptance: projects and chat

M1a turns the chat into an agent that works on a project: it finds, searches and reads the project's files, writes new ones, and asks before deleting. This file records whether that works with a real small model and the real UI. The tool-calling prompt set (about 30 prompts) and the system prompt rules are M1b.

## How to run it

```sh
uv sync && npm --prefix web ci && npm --prefix web run build
uv run lectern serve        # finish first run if needed; it downloads the chat model
```

In the browser:

1. **Projects → New project.** It opens on Overview and one empty chat tab.
2. **Import files…** a Ukrainian DOCX (a lease with numbered clauses) and a small Markdown file. Both show "text indexed" in Overview.
3. In the chat, send the five prompts below, one after another, and note for each: the tool lines, the answer, and whether it's right.
4. On the delete prompt, a card asks "Move notes.md to the trash?". Press **Allow**.
5. Reload the page while a reply is streaming: the text so far comes back, and it keeps streaming.
6. Switch between Overview, the chat and a file tab, then reload: the same tabs are open.

| # | Prompt | Expected |
|---|---|---|
| 1 | Is there a lease contract file in this project? | `project.find` finds the DOCX |
| 2 | Яка пеня за прострочення оплати? Процитуй пункт договору. | reads the DOCX; quotes the penalty clause word for word, in Ukrainian |
| 3 | Who is the tenant, and until when is the contract valid? | reads the right blocks; both facts correct |
| 4 | Write a short reply letter to the tenant saying the payment day stays the 10th, and save it as reply-letter.md. | `artifact.write` creates `reply-letter.md`; Overview shows it as Generated, From = the chat |
| 5 | Delete notes.md from the project. | a card asks first; after Allow, the file is in `.lectern/trash/` and gone from Overview |

## Results

### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 (Metal, llama.cpp b11236)

Run on 2026-09-29 with `LECTERN_TIER=minimum`, so the chat model is Qwen3.5-4B Q4_K_M with an 8,192-token context: the smallest model Lectern ships, and the hardest case for tool calling. The five prompts went through the real API (the same calls the page makes); the UI checks were done in Chrome afterwards.

| # | Tools the model called | Answer | Time | |
|---|---|---|---|---|
| 1 | `project.find("lease", kind="files")` | "Yes, there is a file named `lease-1998.docx`" | 7.8 s (first token 7.2 s, model load included) | pass |
| 2 | `document.read("lease-1998.docx")` | quotes §3.3 exactly, in Ukrainian | 1.5 s | pass |
| 3 | `document.read(…, refs=["#c2", "#c4"])` | tenant Ковальчук Олена Петрівна; valid until 31 December 1999 | 1.6 s | pass |
| 4 | `artifact.write("reply-letter.md", …)` | saved as `reply-letter.md` (619 bytes), Generated, From = the chat | 3.7 s | pass, see notes |
| 5 | `artifact.trash("notes.md")`, card → Allow | moved to the trash | 0.9 s | pass |

Chat speed during the run: about 100 tokens/s.

UI checks, same machine:

| Check | Result |
|---|---|
| A sixth prompt from the page ("Find where the lease mentions the rent amount, then open the lease file in a tab") | `project.search` ×2 (English words, no match in the Ukrainian text) → `project.find` → `document.outline` → `document.read(heading="Орендна плата")` → `tab.open`; quoted §3.2; the file opened in a tab |
| The chat takes its title from the first message; Overview updates | pass |
| Overview: kinds, sizes, "text indexed", From, times | pass, after the fixes below |
| File tab: Markdown shown as text, "Where it came from" with a button to open the chat | pass |
| DOCX in a file tab | details and Download only ("can't be shown here yet"), as planned |
| Tabs remembered after a reload | pass |

### Found and fixed during the run

- The message box scrolled out of view in a long chat. The project page now fills the window, and the transcript scrolls inside it.
- Overview showed small files as "0 MB". Sizes now go down to kB and bytes.
- Times were read as local time ("3 hours ago" for something just made), because SQLite hands times back without their zone. The API now sends them in UTC with the zone.
- Overview counted the model's tool-call steps as messages ("15 messages" for 5 questions and 5 answers). It now counts 10.
- Switching back to a chat reopened the file its turn had opened: the tab replays its turn's events, including `tab.open`'s action. The events stream now marks where the replay ends, and the page only acts on live events.

### Notes

- **The model makes up details.** In prompt 4, it dated the letter "12 травня 2026" and signed it for the landlord's company, and nobody asked for either. That's the model's writing, not a tool problem. M1b's system prompt rules are the place to tell it not to invent facts.
- **Search language.** `project.search` matches words, so English words don't find Ukrainian text. The model recovered on its own (outline, then read by heading). A search that works across languages would need embeddings; that isn't planned yet.
- **Markdown shows as plain text in replies** (`**bold**` stays as asterisks). That was a decision in the plan: nothing the model writes becomes HTML. A Markdown renderer can come later.
- **MLX** wasn't tried: tool calls on `mlx_lm.server` are unverified, and MLX is off by default.
- **Not run on the 4 GB and 8 GB PCs yet.** Tool calling doesn't depend on the GPU, but speed and the first token after a load do. That belongs with the M1b prompt set.
