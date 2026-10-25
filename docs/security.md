# Security design

How Lectern defends itself, threat by threat, and where in the code. What it doesn't protect
against, and how to report a vulnerability: [`SECURITY.md`](../SECURITY.md).

The checks sit in one place, not route by route: the `LocalGuard` middleware
(`src/lectern/core/security.py`) and two dependencies, `current_user` and `require_admin`
(`core/auth.py`).

## Other websites calling the local API

- **Where it listens.** `127.0.0.1` by default. `0.0.0.0` (`--host` or `LECTERN_HOST`) needs
  `LECTERN_PUBLIC_URL`, the addresses people will use. The model servers stay on `127.0.0.1`
  either way.
- **Host.** The `Host` header must be `127.0.0.1` or `localhost` on Lectern's port, or
  exactly an address in `LECTERN_PUBLIC_URL`; anything else gets 421. This stops DNS
  rebinding.
- **Origin.** A `POST`, `PUT`, `PATCH` or `DELETE` from another origin gets 403, and so does
  every WebSocket.
- **Token.** Every `/api` request carries a token in `X-Lectern-Token`. Without accounts it is
  the per-install token: 32 random bytes in `<data>/token`, readable by the owner only. The
  browser gets it from the access link's `#token=` fragment (`lectern url` prints the link)
  and keeps it in the tab's session storage. This token check is the CSRF protection.
- **Headers.** Every response carries a strict Content-Security-Policy (`default-src 'self'`,
  no inline scripts or styles, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`
  and `Referrer-Policy: no-referrer`. The OpenAPI pages are off.

## Accounts

Off until `lectern user init <name>` runs on the server: then the local user becomes the
first admin, keeping its projects, and everyone signs in (`core/accounts.py`).

- **No sign-up.** An admin adds people (`lectern user add`, or Settings → Users). Roles are
  `admin` and `member`. Sign-in names are 2–32 characters of `a-z 0-9 . _ -`, and also name
  the person's folder.
- **Passwords** are at least 15 characters, may not contain the sign-in name, and are stored
  only as Argon2id hashes (`core/passwords.py`). They're typed at a prompt, never passed as
  an argument. A new password ends all of that person's logins.
- **A login** is a random id in a cookie: `HttpOnly`, `SameSite=Lax`, and over https
  `__Host-lectern` with `Secure`. The server keeps only the id's SHA-256. It lasts 30 days,
  or 7 unused. Every `/api` request also needs the login's own token beside the cookie, so
  the cookie alone does nothing for another site; only `/api/login` takes the cookie alone,
  to hand a new tab its token.
- **Refused sign-ins** all get the same answer, and an unknown name costs the same hashing
  work as a known one. Five wrong passwords in a row make the account wait a minute, then
  twice as long after each further one, up to 15 minutes; tries during the wait aren't
  counted. At most two sign-ins are checked at once, with eight waiting; more get 429.
- **The access link** with accounts on works only from the computer Lectern runs on, and opens
  the admin's workspace.
- **Ownership.** Projects, their files, sessions and search are looked up through their owner:
  someone else's answers 404. Changing the install (models, the runtime, first run, web search
  settings, accounts, dev tools) also takes `require_admin`. Disabling someone ends their
  logins; the last admin can't be disabled.

## WebSockets

Browsers don't apply same-origin rules to WebSockets, so every upgrade needs an allowed Origin
and the token, sent as the subprotocol `lectern.token.<token>` (a browser can't set headers
on a WebSocket); with accounts on, the login's token and its cookie. A socket for someone
else's chat or OCR session is closed as not found. An open socket, and a long event stream, is
checked again every ten seconds and closed once its sign-in no longer holds: signed out, a new
password, the account disabled (`api/ws.py`, `api/sse.py`).

## Files and paths

- **Inside the project.** Every path is resolved and refused if it lands outside the project
  folder or inside its `.lectern/` (`projects/files.py`).
- **Imports** keep only the last part of the name; hidden names, `.`, `..` and characters
  Windows can't store are refused, names are at most 180 characters, and a file at most
  2 GB. A taken name gets ` (2)`, ` (3)`.
- **Serving.** Files are typed by extension. Only plain text, Markdown, CSV and PNG, JPEG,
  GIF and WebP images are shown in the browser; everything else downloads as
  `application/octet-stream`. With `nosniff` and the CSP, an imported HTML or SVG file can't
  run in Lectern's origin (`api/projects.py`).
- **Markdown** in the UI goes through react-markdown without raw HTML.

## Instructions hidden in documents

Document text, OCR output, file contents and tool results can say anything. The system prompt
says text in them is data, never an instruction; `document.read` and the web tools add a note
after what they return saying the same. What actually protects the project is the cards:
deleting a file, starting an OCR session, queuing a follow-up, and every web search or page
ask the person, whatever the model decided. The trash card says when the person's message
didn't ask for a deletion.

## Code the models write

No page or API opens a shell or a terminal, on the host or in a container. The only code
Lectern runs that it didn't ship is the scripts its coding model writes, and those run only in
the [sandbox](agent.md#the-script-sandbox): no network, a read-only filesystem, no
capabilities, an unprivileged user, limits on processes, memory and CPU, the inputs read-only,
killed after 60 seconds.

## Data leaving the machine

No telemetry and no cloud models. Lectern connects out to download models (Hugging Face) and
llama.cpp (GitHub), each file checked against a size and SHA-256 pinned in the source; and,
once an admin switches web search on and a person allows each one on a card, to the search
engine named on the card (api.search.brave.com with a key; otherwise html.duckduckgo.com,
then search.yahoo.com), `<language>.wikipedia.org`, or the page's own host.

### Pages the chat opens

The model writes the address, so it could name this computer, the local network or a cloud
metadata service. `web.fetch` (`modules/web/fetch.py`) takes only http and https, on port 80,
443 or 1024 and above, with no user name or password in the address. `localhost` and
non-public literal addresses are refused before any lookup; the name is then resolved, and
every address it gives must be public (an IPv4 address inside IPv6 judged as IPv4). The
request goes to the address that was checked, with the name kept for TLS, so a second DNS
answer can't swap one in. Each redirect is checked again, five at most. No proxy from the
environment, no cookies, 2 MB and 20 seconds at most, and only HTML, plain text and PDFs (a
PDF checked by its content) are taken.

## Secrets on disk

The per-install token, the Brave Search key and each model runner's API key are files only
the owner can read. Settings are in `~/.lectern/lectern.env` when installed. None of them is
logged; `lectern serve` prints the access link once when it starts.
