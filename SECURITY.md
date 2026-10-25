# Security

## Reporting a vulnerability

Please don't open a public issue. Use **Report a vulnerability** on this repository's
**Security** tab: the report stays private between you and the maintainer until a fix is out.

Say what an attacker can do, from where (this computer, the local network, the internet
through a tunnel), and whether accounts were on (`lectern user init`). Steps to reproduce help
most. Leave out real access links, tokens and passwords: a report needs the shape of one, not
yours.

## Supported versions

Lectern is pre-MVP and has no releases yet. Only the latest `main` gets fixes; update with
`git pull && ./install.sh`.

## How Lectern is protected

What a report is measured against. The details are in the README's install sections, and
each defence, with where it is in the code, in [`docs/security.md`](docs/security.md).

- **It listens on `127.0.0.1` only**, unless `LECTERN_HOST=0.0.0.0` says otherwise, and that
  needs `LECTERN_PUBLIC_URL`. The model servers it starts stay on `127.0.0.1` either way.
- **Every request is checked.** The `Host` header must be `127.0.0.1` or `localhost` with
  Lectern's port, or exactly an address in `LECTERN_PUBLIC_URL` (this stops DNS rebinding);
  requests that change something, and every WebSocket, must come from Lectern's own origin;
  every `/api` request carries a token; every page has a strict Content-Security-Policy.
- **Until `lectern user init` has run, the access link is the only lock.** It carries a random
  per-install token, and whoever has the link can do everything. Turn accounts on before anyone
  else uses Lectern, and before it can be reached from the internet.
- **With accounts on**, people sign in. Passwords are stored only as Argon2id hashes, and
  after five wrong ones in a row each next try waits, twice as long each time. Over `https`
  the login cookie is `__Host-` bound, `Secure`, `HttpOnly` and `SameSite=Lax`. The access
  link then works on the computer Lectern runs on only, and opens the admin's workspace there.
- **Scripts the assistant writes** run in a Podman or Docker container with no network, a
  read-only filesystem, no capabilities and an unprivileged user, and only their project folder
  mounted.
- **Downloads are pinned.** llama.cpp builds and the catalogue's model files are checked
  against a size and a SHA-256 fixed in the source.

## What it doesn't protect against

- **Plain `http` on a network you don't control.** A password or the token can be read off
  the network. Use `https`: the Cloudflare Tunnel with Access described in the README, or a
  proxy with a certificate.
- **A tunnel or proxy that rewrites the `Host` header to `127.0.0.1`.** cloudflared, and a
  proxy on the same machine, connect to Lectern from `127.0.0.1`; with the header rewritten
  too, every request through them looks like this computer, and the access link would work
  from outside. Leave cloudflared's `httpHostHeader` unset and pass `Host` through unchanged.
- **Logs.** `lectern serve` prints the access link when it starts. On Linux the service's log
  holds it (`journalctl --user -u lectern`), and in Docker `docker compose logs` does; the Mac
  app leaves it out of its log. Don't paste those logs or `lectern url` output anywhere public.
- **People you've given an account.** A member can't change the install (models, the GPU,
  other people's accounts), but nothing limits how much disk their projects use. There is no
  second factor yet: put Cloudflare Access, or something like it, in front for that.
