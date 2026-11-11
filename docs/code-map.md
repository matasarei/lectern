# Code map

Lookup index of Python modules, large-file landmarks, HTTP/WebSocket routes, agent tools,
database tables, and frontend components. Narrative documentation: [`docs/README.md`](README.md).

## 1. Python modules (`src/lectern/`)

| File | Lines | Purpose | Key symbols |
|---|---:|---|---|
| `src/lectern/app.py` | 201 | FastAPI app factory, lifespan, middleware, `/health` | `create_app` |
| `src/lectern/cli.py` | 753 | `lectern` CLI (`serve`, `url`, `user *`, `doctor`, `chat`, `bench`, `dev *`) | `serve`, `url`, `user_init`, `user_add`, `LecternServer`, `doctor`, `bench` |
| `src/lectern/config.py` | 64 | `LECTERN_HOME` and data/model/runner paths | `Paths`, `lectern_home`, `get_paths` |
| `src/lectern/web.py` | 31 | Serves built SPA (`src/lectern/web_dist/`) | `mount_web` |
| `src/lectern/core/security.py` | 392 | Host, Origin, `X-Lectern-Token`/WS subprotocol guard, CSP headers | `LocalGuard`, `load_or_create_token`, `parse_public_urls`, `login_cookie` |
| `src/lectern/core/secret_files.py` | 30 | Secrets in files of their own (the Brave Search key), owner-readable | `read_secret`, `write_secret` |
| `src/lectern/core/auth.py` | 49 | Route dependencies for signed-in and admin users | `User`, `LOCAL_USER`, `current_user`, `require_admin` |
| `src/lectern/core/accounts.py` | 345 | Users, sign-in rate-limit/backoff, login cookies (`logins` table) | `init_admin`, `add_user`, `sign_in`, `open_login`, `end_login`, `Logins` |
| `src/lectern/core/passwords.py` | 65 | Argon2id password hashing and 15-char policy | `hash_password`, `verify`, `verify_nobody`, `check_new`, `WeakPassword` |
| `src/lectern/core/db.py` | 79 | SQLite engine setup (WAL, foreign keys) and in-process Alembic migration | `make_engine`, `migrate`, `make_sessionmaker`, `get_session` |
| `src/lectern/core/settings.py` | 73 | Key-value settings in the app database | `get`, `update`, `put` |
| `src/lectern/core/tables.py` | 131 | App DB ORM models (`lectern.sqlite`) | `Base`, `UserRow`, `LoginRow`, `SettingRow`, `ProjectRow`, `DownloadRow`, `ImageTimingRow` |
| `src/lectern/core/winevent.py` | 69 | Windows named-event stop signal for tray app (`serve --stop-event`) | `wait_for`, `set_event` |
| `src/lectern/core/winloop.py` | 91 | Windows ProactorEventLoop wrapper surviving dropped accepts | `gone_before_accepted`, `new_loop` |
| `src/lectern/api/__init__.py` | 58 | Mounts `open_api_router` (`/api/login`) and `api_router` (`current_user`) | `open_api_router`, `api_router` |
| `src/lectern/api/accounts.py` | 222 | `/api/login`, `/api/logout`, `/api/password`, `/api/users` | `SignInGate`, `whoami`, `sign_in`, `sign_out`, `change_password`, `list_users`, `add_user` |
| `src/lectern/api/projects.py` | 365 | Project CRUD, file import/download/markdown preview, items, search, tabs | `list_projects`, `create_project`, `import_file`, `artifact_content`, `project_items`, `search_project` |
| `src/lectern/api/sessions.py` | 346 | Chat session routes, message send, confirmations, stop, follow-ups, pins | `new_chat`, `get_chat`, `send`, `events`, `decide`, `stop`, `stop_tool`, `run_follow_up` |
| `src/lectern/api/models.py` | 265 | Model catalog, role assignments, downloads, updates, removal | `list_models`, `set_role_model`, `start_download`, `update_model`, `remove_model` |
| `src/lectern/api/runtime.py` | 112 | Runtime state, event stream/WS, unload, MLX setting | `get_runtime`, `runtime_events`, `unload`, `get_settings`, `put_settings` |
| `src/lectern/api/firstrun.py` | 209 | First-run wizard state and llama-server download | `get_first_run`, `advance_first_run`, `download_engine` |
| `src/lectern/api/export.py` | 87 | Document and file export endpoints | `exporters`, `export` |
| `src/lectern/api/chat.py` | 117 | Direct single-turn `/api/chat` streaming endpoint (used by `lectern chat`) | `chat`, `ChatRequest` |
| `src/lectern/api/system.py` | 56 | Hardware/tier report and sandbox engine probe | `system_report`, `get_system`, `check_sandbox` |
| `src/lectern/api/view.py` | 184 | One-time ticketed `/view/{ticket}` sandboxed HTML viewer | `ViewTickets`, `view`, `view_page`, `view_nothing` |
| `src/lectern/api/sse.py` | 120 | Server-Sent Events stream helpers with periodic login re-check | `hub_stream`, `event_response`, `follow`, `pump` |
| `src/lectern/api/ws.py` | 126 | WebSocket event helpers with periodic login re-check | `hub_socket`, `socket_events`, `refuse`, `refuse_as` |
| `src/lectern/api/dev.py` | 52 | Admin `/api/dev/swap-test` routes (enabled by `LECTERN_DEV=1`) | `start_swap_test`, `get_swap_test` |
| `src/lectern/projects/store.py` | 279 | Project folder creation, rename, trash, and per-project DB sessionmaker | `Project`, `ProjectStore`, `ProjectNotFound`, `NameMismatch`, `touch` |
| `src/lectern/projects/files.py` | 291 | Path confinement (`inside`), file import/write, text indexing, trash | `inside`, `safe_name`, `free_name`, `import_stream`, `write_bytes`, `trash` |
| `src/lectern/projects/tables.py` | 277 | Project DB ORM models (`<project>/.lectern/project.sqlite`) | `ProjectBase`, `SessionRow`, `MessageRow`, `ConfirmationRow`, `ArtifactRow`, `OcrSessionRow` |
| `src/lectern/projects/text.py` | 322 | MIME detection and text/chunk extraction (PDF, DOCX, ODT, text) | `mime_for`, `extract`, `pdf_pages`, `scanned_pages`, `docx_markdown` |
| `src/lectern/projects/search.py` | 109 | SQLite FTS5 indexing and prefix search over `artifact_text` | `index`, `remove`, `chunks`, `match_expression`, `search` |
| `src/lectern/projects/preview.py` | 110 | File tab preview classification and DOCX/ODT-to-Markdown conversion | `preview_kind`, `document_markdown`, `DocumentTooLarge` |
| `src/lectern/projects/html_page.py` | 292 | Bundles a project HTML file's local CSS/images/scripts as `data:` URLs for `/view` | `inline_page` |
| `src/lectern/agent/loop.py` | 1005 | Agent turn loop, tool execution, confirmation cards, exact-number checks | `TurnRun`, `run_turn`, `run_tool`, `call_model`, `load_history`, `maybe_compact` |
| `src/lectern/agent/turns.py` | 616 | Concurrent turn manager, SSE/WS replay channels, follow-up scheduling | `TurnManager`, `Channel`, `Busy` |
| `src/lectern/agent/core_tools.py` | 665 | Core chat tools (`project.*`, `document.*`, `artifact.*`, `session.*`, `tab.*`, `chat.*`) | `core_registry`, `asked_for_file`, `asked_to_delete`, `said_by_person` |
| `src/lectern/agent/export_tools.py` | 217 | `export.formats` and `export.run` tools | `export_tools`, `write_export` |
| `src/lectern/agent/followups.py` | 315 | Queued post-OCR tool calls (`task.after`) | `FollowUp`, `add`, `mark_held`, `pending`, `task_after_tool` |
| `src/lectern/agent/context.py` | 267 | Context window assembly (pins, files, summary, turns) and token budgeting | `build`, `split_turns`, `answer_reserve`, `ContextTooSmall` |
| `src/lectern/agent/compaction.py` | 123 | Summarises older turns when context exceeds 75% | `fold_point`, `write_summary`, `summary_request` |
| `src/lectern/agent/prompts.py` | 104 | System prompt builder for chat turns | `system_prompt` |
| `src/lectern/agent/numbers.py` | 41 | Extracts numbers from text/tool arguments for the exact-calculation guard | `numbers_in`, `written_numbers` |
| `src/lectern/agent/state.py` | 118 | Chat session pins and compaction summary persistence | `ChatState`, `load`, `pin`, `unpin`, `save_summary` |
| `src/lectern/agent/tools.py` | 170 | `AgentTool` definition, JSON schema validation, result pagination | `AgentTool`, `ToolContext`, `ToolResult`, `Registry`, `page` |
| `src/lectern/agent/model_download.py` | 51 | Downloads a missing role model inside a tool after card confirmation | `on_disk`, `why_not`, `download` |
| `src/lectern/documents/document.py` | 200 | `lectern.document/1` schema (`document.json`) and `document.md` rendering | `Document`, `Block`, `Doubt`, `PageInfo`, `render_markdown` |
| `src/lectern/documents/export.py` | 204 | Exporter protocol, loss calculation, and doubt/figure formatting | `Exporter`, `ExportOptions`, `lost`, `resolved_options`, `segments` |
| `src/lectern/documents/from_text.py` | 123 | Converts Markdown, plain text, PDF text layer, or DOCX into a `Document` | `parse_markdown`, `parse_text`, `parse_pages` |
| `src/lectern/modules/__init__.py` | 146 | Entry-point loader (`lectern.modules`) and per-turn tool registry assembly | `Module`, `ToolEnv`, `load_modules`, `tool_registry`, `EnvProbe` |
| `src/lectern/modules/ocr/jobs.py` | 685 | Background OCR worker queue (rasterise → recognise → clean → assemble) | `OcrJobs`, `page_dict` |
| `src/lectern/modules/ocr/clean.py` | 800 | LLM page cleanup, JSON schema validation, hallucination/drop guards | `clean_page`, `page_message`, `keep_this_page`, `restore_missing_lines`, `upright_tables` |
| `src/lectern/modules/ocr/assemble.py` | 376 | Multi-page assembly (drops running headers/footers, merges split blocks/tables) | `assemble`, `PageInput` |
| `src/lectern/modules/ocr/store.py` | 520 | OCR session DB/disk persistence and `document.json`/`document.md` publishing | `create_session`, `save_reading`, `save_cleaned`, `publish_document` |
| `src/lectern/modules/ocr/api.py` | 454 | `/api/projects/{id}/ocr/*` routes | `new_ocr`, `get_ocr`, `answer_doubt`, `answer_others`, `rerun_page`, `clean_page_again` |
| `src/lectern/modules/ocr/tools.py` | 276 | `ocr.start` and `ocr.note` chat tools | `StartParams`, `NoteParams` |
| `src/lectern/modules/ocr/pages.py` | 151 | PDF 300-dpi rasterisation (pypdfium2) and image normalisation (Pillow) | `rasterise`, `page_count`, `page_file` |
| `src/lectern/modules/ocr/recognise.py` | 119 | Calls vision runner on a page PNG and collects token logprobs | `recognise`, `model_input`, `PageText` |
| `src/lectern/modules/ocr/doubts.py` | 69 | Flags low-logprob OCR words (`< -3.0`) as unsure | `unsure_words`, `mark_unsure`, `ocr_doubts` |
| `src/lectern/modules/ocr/review.py` | 104 | Matches and applies stored user decisions (`ocr_decisions`) to assembled blocks | `Decision`, `match_decisions`, `apply_decisions` |
| `src/lectern/modules/ocr/text.py` | 75 | Detects and trims trailing repetition loops in OCR readings | `normalise`, `looped`, `cut_trailing_loop` |
| `src/lectern/modules/sandbox/container.py` | 261 | Locked-down Podman/Docker execution (`lectern-sandbox:2`) | `Sandbox`, `RunResult`, `SandboxError`, `LOCKDOWN` |
| `src/lectern/modules/sandbox/tools.py` | 713 | `code.write` (coding model + test loop) and `code.run` tools | `run_script`, `stored_run`, `coder_request`, `ensure_coder` |
| `src/lectern/modules/pdf_edit/engine.py` | 709 | In-place PDF text replacement preserving embedded/system fonts (pypdfium2) | `apply_edits`, `Edit`, `Change`, `Result`, `EditFailed` |
| `src/lectern/modules/pdf_edit/forms.py` | 288 | Reads and fills PDF AcroForm text fields and checkboxes | `form_fields`, `fill_form`, `Field`, `Filled` |
| `src/lectern/modules/pdf_edit/fonts.py` | 180 | System font discovery and TrueType subsetting for PDF edits | `InstalledFonts`, `installed`, `subset_for` |
| `src/lectern/modules/pdf_edit/tools.py` | 181 | `pdf.edit` and `pdf.fill` chat tools | `pdf_tools`, `EditParams`, `FillParams` |
| `src/lectern/modules/image/generate.py` | 425 | Runs `sd-cli` per image, tracks step progress, strips PNG metadata | `generate`, `size_settings`, `memory_flags`, `need_bytes` |
| `src/lectern/modules/image/tools.py` | 233 | `image.generate` chat tool with editable prompt/shape confirmation card | `GenerateParams`, `shape_size`, `wait_text` |
| `src/lectern/modules/web/fetch.py` | 205 | SSRF-guarded public HTTP/HTTPS fetcher with DNS-pinned IP connection | `fetch`, `check_url`, `is_public`, `FetchRefused` |
| `src/lectern/modules/web/sources.py` | 190 | DuckDuckGo and Yahoo search engines with 30s rate-limit cooldown | `DuckDuckGo`, `Yahoo`, `ScrapedEngine` |
| `src/lectern/modules/web/brave.py` | 68 | Brave Search API client | `BraveApi` |
| `src/lectern/modules/web/wiki.py` | 90 | Wikipedia API lookup | `lookup`, `Article` |
| `src/lectern/modules/web/page.py` | 163 | HTML-to-Markdown conversion for fetched web pages | `decode`, `to_text`, `Page` |
| `src/lectern/modules/web/tools.py` | 299 | `web.search`, `wiki.lookup`, and `web.fetch` chat tools | `web_tools`, `search_tool` |
| `src/lectern/modules/web/api.py` | 67 | `/api/web/settings` and `/api/web/key` admin routes | `get_settings`, `put_settings`, `put_key`, `delete_key` |
| `src/lectern/modules/export_md/__init__.py` | 81 | Markdown exporter module (`export-md`) | `MarkdownExporter`, `MarkdownModule` |
| `src/lectern/modules/export_docx/__init__.py` | 103 | DOCX exporter module (`export-docx`, python-docx) | `DocxExporter`, `DocxModule` |
| `src/lectern/modules/export_odt/__init__.py` | 122 | ODT exporter module (`export-odt`, odfdo) | `OdtExporter`, `OdtModule` |
| `src/lectern/runtime/supervisor.py` | 709 | Model lease supervisor, LRU eviction, memory budget, image reservations | `ModelRuntime`, `Lease`, `Reservation`, `Priority` |
| `src/lectern/runtime/planner.py` | 161 | Estimates model + KV-cache memory and selects context/quantisation plan | `plan`, `read_shape`, `kv_bytes`, `FitPlan`, `DoesNotFit` |
| `src/lectern/runtime/wiring.py` | 316 | Connects catalog, hardware budget, and runners into `ModelRuntime` | `budget_for`, `role_model`, `choices`, `cleanup_model`, `build_runtime` |
| `src/lectern/runtime/gguf_header.py` | 91 | Fast GGUF header metadata reader (skips tokenizer arrays) | `read_metadata` |
| `src/lectern/runners/base.py` | 62 | Runner protocol and spec types | `Runner`, `RunnerHandle`, `RunnerSpec`, `MeasuredMemory` |
| `src/lectern/runners/llamacpp.py` | 264 | Spawns and monitors `llama-server` processes on `127.0.0.1` | `LlamaCppRunner`, `LlamaCppHandle`, `InstallingLlamaCppRunner` |
| `src/lectern/runners/binaries.py` | 306 | Pinned `llama.cpp` release assets, variant selection, SHA-256 installer | `LlamaCppInstaller`, `select_variant`, `LLAMA_CPP_BUILD` |
| `src/lectern/runners/mlx.py` | 167 | Spawns `mlx_text_server` for Apple Silicon chat models | `MlxRunner`, `mlx_supported`, `mlx_available` |
| `src/lectern/runners/mlx_text_server.py` | 95 | Auth-guarded wrapper around `mlx_lm.server` | `guard`, `main` |
| `src/lectern/runners/sdcpp.py` | 225 | Pinned `stable-diffusion.cpp` (`sd-cli`) release installer | `SdCppInstaller`, `select_variant`, `SD_CPP_BUILD` |
| `src/lectern/hardware/probe.py` | 184 | Unified GPU/RAM hardware probe (NVIDIA, Vulkan, Apple Silicon, CPU) | `probe`, `HardwareInfo`, `Gpu`, `best_gpu`, `gpu_budget` |
| `src/lectern/hardware/tiers.py` | 70 | Classifies hardware into `comfortable`, `recommended`, `minimum`, `below_minimum` | `Tier`, `tier_for`, `effective_tier` |
| `src/lectern/hardware/nvidia.py` | 61 | NVML GPU memory and CUDA version probe | `probe`, `cuda_version` |
| `src/lectern/hardware/vulkan.py` | 101 | Parses `llama-server --list-devices` for AMD/Intel/UMA GPUs | `list_devices`, `parse_devices`, `gpus_from_devices` |
| `src/lectern/hardware/apple.py` | 56 | macOS `sysctl` Apple Silicon unified memory probe | `probe`, `MacInfo` |
| `src/lectern/hardware/sandbox.py` | 50 | Detects Podman or Docker CLI availability | `detect`, `SandboxRuntime` |
| `src/lectern/catalog/catalog.py` | 282 | Loads and validates `src/lectern/catalog/models.yaml` | `Catalog`, `CatalogEntry`, `load_catalog`, `parse` |
| `src/lectern/downloads/manager.py` | 680 | Resumable Hugging Face model downloads, staging updates, disk-space checks | `DownloadManager`, `DownloadState`, `UpdateState`, `EventHub` |
| `src/lectern/downloads/fetch.py` | 86 | Resumable `.part` file downloader with SHA-256 verification | `fetch_verified`, `DownloadError` |
| `src/lectern/dev/agent_check.py` | 445 | `lectern dev agent-check` harness (`calc`, `tools`, `long`, `web`, `ocr`, `pdf`) | `run_agent_check`, `load_set`, `answer_matches` |
| `src/lectern/dev/ocr_check.py` | 413 | `lectern dev ocr-check` CER/WER benchmark harness | `run_ocr_check`, `cer`, `wer` |
| `src/lectern/dev/pdf_files.py` | 160 | Generates synthetic PDF fixtures for `agent-check --set pdf` | `write_agreement`, `write_form`, `pdf_text`, `form_values` |
| `src/lectern/dev/swap_test.py` | 97 | `lectern dev swap-test` workload | `run_swap_test`, `SwapTestResult` |

## 2. Landmarks in large files (600+ lines)

| File | Line range | Section / symbols |
|---|---|---|
| `src/lectern/agent/loop.py` (1005L) | `L73-173` | `TurnRun`, `History`, `load_history`, `as_openai`, `store_message`, `Reply` |
| | `L176-355` | `call_model`, `build_context`, `maybe_compact`, `_tokenizer`, `_stream` |
| | `L361-553` | `run_tool`, `_with_card_values`, `_run_stoppable`, `_ask` (confirmation card wait) |
| | `L559-768` | `run_turn` (8-step loop), `_forced_call` (Image toggle), `_write_arguments` |
| | `L854-1005` | Exact-number guards: `_may_check`, `_with_check`, `_Held`, `_unchecked`, `_with_call_check` |
| `src/lectern/modules/ocr/clean.py` (800L) | `L81-187` | `CleanBlock`, `CleanDoubt`, `CleanFix`, `CleanedPage`, `SCHEMA`, `page_message` |
| | `L190-269` | `clean_page`, `_what_was_wrong`, `_say_loop_left_out` |
| | `L332-504` | `keep_this_page` (neighbour-copy drop), `_narrow`, `_widen`, `_place_doubts` |
| | `L568-790` | `restore_lookalikes`, `restore_missing_lines`, `upright_tables`, `ask_about_unmade_fixes` |
| `src/lectern/cli.py` (753L) | `L38-165` | `serve`, `where_it_runs`, `url` |
| | `L177-318` | `user_init`, `user_add`, `user_password`, `user_list`, `user_disable`, `user_enable` |
| | `L328-447` | `LecternServer` (graceful shutdown & Windows stop event), `server_config`, `open_when_listening` |
| | `L451-753` | `doctor`, `chat`, `bench`, `agent_check`, `swap_test`, `ocr_check` |
| `src/lectern/modules/sandbox/tools.py` (713L) | `L45-250` | `find_script`, `find_inputs`, `ensure_image`, `run_script`, `stored_run`, `describe_run` |
| | `L256-348` | `RunParams`, `_code_run`, `code.run` tool definition |
| | `L355-713` | `WriteParams`, `_code_write`, `_write_and_test` (3-try coder loop), `_sheet_previews`, `code.write` tool |
| `src/lectern/modules/pdf_edit/engine.py` (709L) | `L47-106` | `EditFailed`, `Edit`, `Change`, `Result`, `apply_edits` |
| | `L122-207` | `_Char`, `_Line`, `_PageText`, `_read`, `_Spot`, `_occurrences` |
| | `L210-616` | `_Editor`: `apply`, `_locate`, `_close_match`, `_replace`, `_draw`, `_shift_rest` |
| | `L623-709` | `_Style`, `_colour`, `_font_name`, `_zero_width`, `_object_text` |
| `src/lectern/runtime/supervisor.py` (709L) | `L64-115` | `Priority`, `ModelChoice`, `Resident`, `Lease`, `Reservation` |
| | `L118-355` | `ModelRuntime.__init__`, `state`, `needs_room`, `turn_started`, `waiting_for_person` |
| | `L360-534` | `lease`, `_acquire`, `job_hold`, `reserve` (image VRAM reservation), `_try_reserve` |
| | `L536-709` | `_try_admit`, `_spare_ram`, `_commit_short`, `_load`, `_unload`, `unload_idle`, `stop_all` |
| `src/lectern/modules/ocr/jobs.py` (685L) | `L91-289` | `OcrJobs`: `settle`, `summary`, `start`, `resume`, `pause`, `rerun`, `reclean`, `stop_all` |
| | `L293-396` | `_work` queue loop, `_asked_for_more`, `notice`, `_run` |
| | `L398-576` | Phases: `_rasterise`, `_recognise`, `_clean`, `_assemble`, `_leased_unless_paused` |
| `src/lectern/downloads/manager.py` (680L) | `L45-126` | `DownloadState`, `Installed`, `UpdateState`, `EventHub` |
| | `L129-330` | `DownloadManager`: `installed`, `start`, `resume_interrupted`, `stop`, `removable`, `remove` |
| | `L337-559` | Model updates: `update_available`, `start_update`, `_update`, `_carry_over`, `_swap`, `_recover` |
| `src/lectern/agent/core_tools.py` (665L) | `L111-251` | `project.list` (`L140`), `project.find` (`L205`), `project.search` (`L240`) |
| | `L254-347` | `document.outline` (`L277`), `document.read` (`L335`) |
| | `L350-497` | `artifact.write` (`L411`), `artifact.trash` (`L485`, `asked_to_delete`) |
| | `L500-665` | `session.status` (`L533`), `tab.open` (`L561`), `chat.pin` (`L626`), `chat.unpin` (`L650`) |
| `src/lectern/agent/turns.py` (616L) | `L40-84` | `Channel` (live turn event buffer & subscriber broadcast) |
| | `L93-239` | `TurnManager`: `send`, `_start`, `_new_run` |
| | `L243-471` | Follow-ups: `after_session`, `resume_follow_ups`, `run_follow_up_now`, `_hold`, `_follow_up` |
| | `L478-551` | `decide` (card answer), `stop_tool`, `stop`, `stop_project` |
| `web/src/project/chat.tsx` (997L) | `L35-360` | `ChatTab` (message input, `@file` autocomplete, Image toggle, attachments) |
| | `L363-610` | `groupReplies`, `Transcript`, `FinishedActions` (export buttons & OCR step links) |
| | `L617-880` | `UserMessage`, `ToolLine`, `Progress`, `ScriptCode`, `CardFields`, `ConfirmCard` |
| | `L883-997` | `LiveReply`, `PinnedStrip`, `UsageLine`, `CompactionNote`, `ModelWait` |
| `web/src/routes/settings/models.tsx` (890L) | `L52-295` | `ModelsPage`, `runsOnItsBuild`, `cleansWithTheChats` |
| | `L296-624` | `RoleRow` (role selector + download/licence prompt), `InstalledCard` (update/remove) |
| | `L625-890` | `MemoryGauge`, `SettingsCard` (MLX toggle), `WebSearchCard`, `BraveKey`, `HeldFor` |

## 3. HTTP & WebSocket routes

All `/api/*` routes except `/api/login` require `current_user` (`src/lectern/api/__init__.py:36`).

| Method & path | Auth | Handler (`file:line`) |
|---|---|---|
| `GET /health` | host check | `src/lectern/app.py:180 (health)` |
| `GET /view/{ticket}` | 1-use ticket | `src/lectern/api/view.py:106 (view)` |
| `GET /view/{ticket}/page` | frame ticket | `src/lectern/api/view.py:125 (view_page)` |
| `GET /api/login` | `open_router` | `src/lectern/api/accounts.py:116 (whoami)` |
| `POST /api/login` | `open_router` | `src/lectern/api/accounts.py:128 (sign_in)` |
| `POST /api/logout` | `current_user` | `src/lectern/api/accounts.py:144 (sign_out)` |
| `POST /api/password` | `current_user` | `src/lectern/api/accounts.py:153 (change_password)` |
| `GET /api/users` | `require_admin` | `src/lectern/api/accounts.py:180 (list_users)` |
| `POST /api/users` | `require_admin` | `src/lectern/api/accounts.py:185 (add_user)` |
| `POST /api/users/{login}/password` | `require_admin` | `src/lectern/api/accounts.py:199 (reset_password)` |
| `POST /api/users/{login}/disabled` | `require_admin` | `src/lectern/api/accounts.py:211 (set_disabled)` |
| `GET /api/system` | `current_user` | `src/lectern/api/system.py:35 (get_system)` |
| `GET /api/system/sandbox` | `current_user` | `src/lectern/api/system.py:42 (check_sandbox)` |
| `GET /api/first-run` | `current_user` | `src/lectern/api/firstrun.py:191 (get_first_run)` |
| `POST /api/first-run` | `require_admin` | `src/lectern/api/firstrun.py:201 (advance_first_run)` |
| `POST /api/first-run/engine` | `require_admin` | `src/lectern/api/firstrun.py:178 (download_engine)` |
| `GET /api/models` | `current_user` | `src/lectern/api/models.py:38 (list_models)` |
| `PUT /api/models/roles/{role}` | `require_admin` | `src/lectern/api/models.py:130 (set_role_model)` |
| `POST /api/models/{model_id}/download` | `require_admin` | `src/lectern/api/models.py:194 (start_download)` |
| `POST /api/models/{model_id}/update` | `require_admin` | `src/lectern/api/models.py:216 (update_model)` |
| `DELETE /api/models/{name}` | `require_admin` | `src/lectern/api/models.py:232 (remove_model)` |
| `GET /api/models/events` (SSE + WS) | `current_user` | `src/lectern/api/models.py:256 (model_events)`, `:263 (model_events_socket)` |
| `GET /api/runtime` | `current_user` | `src/lectern/api/runtime.py:28 (get_runtime)` |
| `GET /api/runtime/events` (SSE + WS) | `current_user` | `src/lectern/api/runtime.py:36 (runtime_events)`, `:42 (runtime_events_socket)` |
| `POST /api/runtime/unload` | `require_admin` | `src/lectern/api/runtime.py:53 (unload)` |
| `GET /api/runtime/settings` | `current_user` | `src/lectern/api/runtime.py:73 (get_settings)` |
| `PUT /api/runtime/settings` | `require_admin` | `src/lectern/api/runtime.py:91 (put_settings)` |
| `GET /api/projects` | `current_user` | `src/lectern/api/projects.py:72 (list_projects)` |
| `POST /api/projects` | `current_user` | `src/lectern/api/projects.py:77 (create_project)` |
| `GET /api/projects/{project_id}` | `current_user` | `src/lectern/api/projects.py:85 (get_project)` |
| `PATCH /api/projects/{project_id}` | `current_user` | `src/lectern/api/projects.py:90 (rename_project)` |
| `DELETE /api/projects/{project_id}` | `current_user` | `src/lectern/api/projects.py:98 (remove_project)` |
| `PUT /api/projects/{project_id}/files` | `current_user` | `src/lectern/api/projects.py:141 (import_file)` |
| `GET /api/projects/{project_id}/artifacts/{artifact_id}/content` | `current_user` | `src/lectern/api/projects.py:190 (artifact_content)` |
| `GET /api/projects/{project_id}/artifacts/{artifact_id}/markdown` | `current_user` | `src/lectern/api/projects.py:205 (artifact_markdown)` |
| `POST /api/projects/{project_id}/artifacts/{artifact_id}/view` | `current_user` | `src/lectern/api/projects.py:223 (artifact_view_link)` |
| `GET /api/projects/{project_id}/items` | `current_user` | `src/lectern/api/projects.py:239 (project_items)` |
| `GET /api/projects/{project_id}/search` | `current_user` | `src/lectern/api/projects.py:293 (search_project)` |
| `GET /api/projects/{project_id}/tabs` | `current_user` | `src/lectern/api/projects.py:330 (get_tabs)` |
| `PUT /api/projects/{project_id}/tabs` | `current_user` | `src/lectern/api/projects.py:357 (put_tabs)` |
| `GET /api/projects/{project_id}/events` (SSE + WS) | `current_user` | `src/lectern/api/sessions.py:244 (project_events)`, `:260 (project_events_socket)` |
| `POST /api/projects/{project_id}/sessions` | `current_user` | `src/lectern/api/sessions.py:105 (new_chat)` |
| `GET /api/projects/{project_id}/sessions/{session_id}` | `current_user` | `src/lectern/api/sessions.py:116 (get_chat)` |
| `PATCH /api/projects/{project_id}/sessions/{session_id}` | `current_user` | `src/lectern/api/sessions.py:182 (retitle)` |
| `POST /api/projects/{project_id}/sessions/{session_id}/messages` | `current_user` | `src/lectern/api/sessions.py:195 (send)` |
| `GET /api/projects/{project_id}/sessions/{session_id}/events` (SSE + WS) | `current_user` | `src/lectern/api/sessions.py:210 (events)`, `:228 (events_socket)` |
| `POST /api/projects/{project_id}/sessions/{session_id}/confirmations/{confirmation_id}` | `current_user` | `src/lectern/api/sessions.py:273 (decide)` |
| `POST /api/projects/{project_id}/sessions/{session_id}/stop` | `current_user` | `src/lectern/api/sessions.py:290 (stop)` |
| `POST /api/projects/{project_id}/sessions/{session_id}/tools/{call_id}/stop` | `current_user` | `src/lectern/api/sessions.py:296 (stop_tool)` |
| `POST /api/projects/{project_id}/sessions/{session_id}/followups/{followup_id}/run` | `current_user` | `src/lectern/api/sessions.py:308 (run_follow_up)` |
| `DELETE /api/projects/{project_id}/sessions/{session_id}/followups/{followup_id}` | `current_user` | `src/lectern/api/sessions.py:322 (cancel_follow_up)` |
| `DELETE /api/projects/{project_id}/sessions/{session_id}/pins/{pin_id}` | `current_user` | `src/lectern/api/sessions.py:336 (unpin)` |
| `GET /api/exporters` | `current_user` | `src/lectern/api/export.py:43 (exporters)` |
| `POST /api/projects/{project_id}/export` | `current_user` | `src/lectern/api/export.py:53 (export)` |
| `POST /api/projects/{project_id}/ocr` | `current_user` | `src/lectern/modules/ocr/api.py:167 (new_ocr)` |
| `GET /api/projects/{project_id}/ocr/models` | `current_user` | `src/lectern/modules/ocr/api.py:192 (ocr_models)` |
| `GET /api/projects/{project_id}/ocr/{session_id}` | `current_user` | `src/lectern/modules/ocr/api.py:209 (get_ocr)` |
| `GET /api/projects/{project_id}/ocr/{session_id}/pages/{number}/image` | `current_user` | `src/lectern/modules/ocr/api.py:215 (page_image)` |
| `GET /api/projects/{project_id}/ocr/{session_id}/pages/{number}/text` | `current_user` | `src/lectern/modules/ocr/api.py:227 (page_text)` |
| `GET /api/projects/{project_id}/ocr/{session_id}/document` | `current_user` | `src/lectern/modules/ocr/api.py:240 (get_document)` |
| `PUT /api/projects/{project_id}/ocr/{session_id}/doubts/{doubt_id}` | `current_user` | `src/lectern/modules/ocr/api.py:256 (answer_doubt)` |
| `POST /api/projects/{project_id}/ocr/{session_id}/doubts/{doubt_id}/others` | `current_user` | `src/lectern/modules/ocr/api.py:289 (answer_others)` |
| `DELETE /api/projects/{project_id}/ocr/{session_id}/doubts/{doubt_id}` | `current_user` | `src/lectern/modules/ocr/api.py:317 (reopen_doubt)` |
| `PATCH /api/projects/{project_id}/ocr/{session_id}/pages/{number}` | `current_user` | `src/lectern/modules/ocr/api.py:341 (change_page)` |
| `POST /api/projects/{project_id}/ocr/{session_id}/pages/{number}/rerun` | `current_user` | `src/lectern/modules/ocr/api.py:366 (rerun_page)` |
| `POST /api/projects/{project_id}/ocr/{session_id}/pages/{number}/clean` | `current_user` | `src/lectern/modules/ocr/api.py:387 (clean_page_again)` |
| `POST /api/projects/{project_id}/ocr/{session_id}/start-now` | `current_user` | `src/lectern/modules/ocr/api.py:400 (start_now)` |
| `POST /api/projects/{project_id}/ocr/{session_id}/pause` | `current_user` | `src/lectern/modules/ocr/api.py:409 (pause)` |
| `POST /api/projects/{project_id}/ocr/{session_id}/resume` | `current_user` | `src/lectern/modules/ocr/api.py:419 (resume)` |
| `GET /api/projects/{project_id}/ocr/{session_id}/events` (SSE + WS) | `current_user` | `src/lectern/modules/ocr/api.py:429 (events)`, `:443 (events_socket)` |
| `GET /api/web/settings` | `current_user` | `src/lectern/modules/web/api.py:40 (get_settings)` |
| `PUT /api/web/settings` | `require_admin` | `src/lectern/modules/web/api.py:50 (put_settings)` |
| `PUT /api/web/key` | `require_admin` | `src/lectern/modules/web/api.py:57 (put_key)` |
| `DELETE /api/web/key` | `require_admin` | `src/lectern/modules/web/api.py:64 (delete_key)` |
| `POST /api/chat` | `current_user` | `src/lectern/api/chat.py:51 (chat)` |
| `POST /api/dev/swap-test`, `GET /api/dev/swap-test` | `require_admin` | `src/lectern/api/dev.py:24 (start_swap_test)`, `:48 (get_swap_test)` |

## 4. Agent tools (`24` tools)

Models receive tool names with `_` in place of `.` (e.g. `project_find`).

| Tool name | Source | Confirmation card | Definition (`file:line`) |
|---|---|---|---|
| `project.list` | core | no | `src/lectern/agent/core_tools.py:140` |
| `project.find` | core | no | `src/lectern/agent/core_tools.py:205` |
| `project.search` | core | no | `src/lectern/agent/core_tools.py:240` |
| `document.outline` | core | no | `src/lectern/agent/core_tools.py:277` |
| `document.read` | core | no | `src/lectern/agent/core_tools.py:335` |
| `artifact.write` | core | when message didn't ask for a file | `src/lectern/agent/core_tools.py:411` |
| `artifact.trash` | core | always | `src/lectern/agent/core_tools.py:485` |
| `session.status` | core | no | `src/lectern/agent/core_tools.py:533` |
| `tab.open` | core | no | `src/lectern/agent/core_tools.py:561` |
| `chat.pin` | core | when words aren't the user's | `src/lectern/agent/core_tools.py:626` |
| `chat.unpin` | core | no | `src/lectern/agent/core_tools.py:650` |
| `export.formats` | core | no | `src/lectern/agent/export_tools.py:96` |
| `export.run` | core | when format loses capabilities | `src/lectern/agent/export_tools.py:104` |
| `task.after` | core | always | `src/lectern/agent/followups.py:306` |
| `code.run` | `sandbox` | no | `src/lectern/modules/sandbox/tools.py:295` |
| `code.write` | `sandbox` | only to build image / download coder | `src/lectern/modules/sandbox/tools.py:703` |
| `ocr.start` | `ocr` | always | `src/lectern/modules/ocr/tools.py:207` |
| `ocr.note` | `ocr` | no | `src/lectern/modules/ocr/tools.py:265` |
| `pdf.edit` | `pdf_edit` | no | `src/lectern/modules/pdf_edit/tools.py:53` |
| `pdf.fill` | `pdf_edit` | no | `src/lectern/modules/pdf_edit/tools.py:67` |
| `image.generate` | `image` | always (unless Image toggle used) | `src/lectern/modules/image/tools.py:214` |
| `web.search` | `web` | always | `src/lectern/modules/web/tools.py:120` |
| `wiki.lookup` | `web` | always | `src/lectern/modules/web/tools.py:176` |
| `web.fetch` | `web` | always | `src/lectern/modules/web/tools.py:280` |

## 5. Database tables

| Database & Alembic tree | Table name | ORM model (`file:line`) | Purpose |
|---|---|---|---|
| `lectern.sqlite` (`src/lectern/migrations/`) | `users` | `src/lectern/core/tables.py:38 (UserRow)` | Local user (`id=1`) and accounts (`login`, `password_hash`, `role`, `disabled`) |
| | `logins` | `src/lectern/core/tables.py:57 (LoginRow)` | Active browser logins (`id_hash` SHA-256, per-login `token`, expiry) |
| | `settings` | `src/lectern/core/tables.py:72 (SettingRow)` | App settings (role models, MLX toggle, web search enabled) |
| | `projects` | `src/lectern/core/tables.py:79 (ProjectRow)` | Projects (`owner_id`, `uuid`, `name`, `root_path`, `removed_at`) |
| | `downloads` | `src/lectern/core/tables.py:100 (DownloadRow)` | Model download status, progress, and installed Hugging Face `revision` |
| | `image_timings` | `src/lectern/core/tables.py:117 (ImageTimingRow)` | Last generation duration per image model and pixel count |
| `project.sqlite` (`src/lectern/project_migrations/`) | `sessions` | `src/lectern/projects/tables.py:50 (SessionRow)` | Chat (`kind='chat'`) and OCR (`kind='ocr'`) sessions |
| | `messages` | `src/lectern/projects/tables.py:66 (MessageRow)` | Chat transcript (`role`, `content`, `tool_calls`, `attachments`) |
| | `confirmations` | `src/lectern/projects/tables.py:87 (ConfirmationRow)` | Tool confirmation cards (`pending`, `approved`, `denied`) |
| | `artifacts` | `src/lectern/projects/tables.py:108 (ArtifactRow)` | Project files, MIME, text extraction status, provenance (`from_kind`, `from_id`) |
| | `script_runs` | `src/lectern/projects/tables.py:136 (ScriptRunRow)` | Sandbox script executions, input SHA-256, stdout/stderr, outputs |
| | `chat_state` | `src/lectern/projects/tables.py:156 (ChatStateRow)` | Per-chat pins, compaction summary, and queued `follow_ups` JSON |
| | `ui_state` | `src/lectern/projects/tables.py:171 (UiStateRow)` | Open and active project tabs (`key='tabs'`) |
| | `ocr_sessions` | `src/lectern/projects/tables.py:185 (OcrSessionRow)` | OCR job state, source artifact, page counts, published document artifact |
| | `ocr_pages` | `src/lectern/projects/tables.py:208 (OcrPageRow)` | Per-page OCR status, flags (`handwritten`, `cut_off`, `looped`), note, model |
| | `ocr_decisions` | `src/lectern/projects/tables.py:257 (OcrDecisionRow)` | Review answers (`accept`, `keep`, `edit`, `remove`) preserved across re-cleans |
| | `artifact_chunk_labels` | `src/lectern/projects/tables.py:243 (ChunkLabelRow)` | Maps `(artifact_id, chunk)` to Document block ids (`#b3.2`) |
| | `artifact_text` | `src/lectern/project_migrations/versions/0001_project.py:89` | SQLite FTS5 virtual table queried in `src/lectern/projects/search.py` |

## 6. Frontend routes & project tabs (`web/src/`)

| Route / tab | Source file | API / state hooks | Test file |
|---|---|---|---|
| Router & auth guard | `web/src/router.tsx` | `web/src/api/session.ts`, `web/src/api/client.ts` | `web/src/router.test.tsx` |
| `/login` | `web/src/routes/login.tsx` | `POST /api/login`, `POST /api/password` | `web/src/routes/login.test.tsx` |
| `/first-run` | `web/src/routes/first-run.tsx` | `/api/first-run`, `useDownloadEvents.ts` | `web/src/routes/first-run.test.tsx` |
| `/projects` | `web/src/routes/projects.tsx` | `/api/projects`, `useRuntime.ts` | `web/src/routes/projects.test.tsx` |
| `/projects/$projectId` | `web/src/routes/project.tsx` | `web/src/project/tabs.ts`, `useProjectEvents.ts` | `web/src/routes/project.test.tsx` |
| `/settings/models` | `web/src/routes/settings/models.tsx` | `/api/models`, `/api/runtime`, `/api/web/*` | `web/src/routes/settings/models.test.tsx` |
| `/settings/users` | `web/src/routes/settings/users.tsx` | `/api/users` | `web/src/routes/settings/users.test.tsx` |
| Tab: Overview | `web/src/project/overview.tsx` | `/api/projects/{id}/items`, `web/src/api/upload.ts` | `web/src/project/overview.test.tsx` |
| Tab: Chat session | `web/src/project/chat.tsx` | `useSessionEvents.ts`, `web/src/project/file-picker.tsx` | `web/src/project/chat.test.tsx` |
| Tab: OCR session | `web/src/project/ocr.tsx` | `web/src/project/ocr-api.ts`, `ocr-steps.tsx`, `ocr-line.tsx` | `web/src/project/ocr.test.tsx` |
| Tab: OCR review | `web/src/project/review.tsx` | `PUT /doubts/{id}`, `POST /doubts/{id}/others` | `web/src/project/review.test.tsx` |
| Tab: Document | `web/src/project/document.tsx` | `web/src/project/export-buttons.tsx` | `web/src/project/document.test.tsx` |
| Tab: File preview | `web/src/project/file.tsx` | `/artifacts/{id}/content`, `/markdown`, `/view` | `web/src/project/file.test.tsx` |
