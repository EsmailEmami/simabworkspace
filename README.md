# Simab Workspace

Work on projects that live on a remote server from your own machine — in one window:
terminals on the server and locally, a Claude Code chat, VS Code, Git and run buttons,
with each project folder kept in sync both ways.

- **Two-way sync** (Mutagen) between the server and your machine: the server runs the
  databases and backend, your machine runs the frontend — no copying by hand.
- **Tabs and split view**: server and local terminals side by side, drag tabs between
  sections, grid view of everything that is open.
- **Claude Code chat** on the server, per project: live streaming, tool cards with
  diffs, permission and question buttons, plan approval, images, `@` files, `/`
  commands, past conversations — right-to-left text (Persian/Arabic) shown correctly.
- **Git** per project (also several repositories inside one project): changes, diff,
  commit, pull, push; badges show what is waiting.
- **Run buttons** per project (`npm start`, `docker compose logs -f`, …), local or on
  the server, in any sub-folder.
- **VS Code** opens the project on the server (Remote-SSH) or the synced local copy.
- **Docker** per project: the containers on the server with their state and ports —
  logs, shell, restart, rebuild — and a notice when one crashes.
- **Port forwarding**: reach a server port on `localhost` (database, API, web page)
  through SSH with one click; it reconnects by itself.
- **Claude usage**: the 5-hour and weekly limits in the sidebar, cost per day and project.
- **History** of what you did, project colours, a minimal dark UI.

Runs on **Linux**, **macOS** and **Windows** (natively — no WSL needed; WSL works too). The server only needs SSH.

## Install

**Linux / macOS / WSL**

```bash
curl -fsSL https://raw.githubusercontent.com/EsmailEmami/simabworkspace/main/install.sh | bash
```

**Windows** (PowerShell — runs on Windows itself; installs Python and Git for Windows with winget if missing):

```powershell
irm https://raw.githubusercontent.com/EsmailEmami/simabworkspace/main/install.ps1 | iex
```

The installer downloads the latest release, installs the `simabworkspace` command and
then asks, in the terminal:

1. the tools it needs (Mutagen, jq, …) — installed for you,
2. your **SSH user**, the **server address** and **SSH port**,
3. which **SSH key** to use (or creates one) and installs it on the server — your
   server password is typed **once**, password-less login from then on.

Then:

```bash
simabworkspace          # menu in the terminal
simabworkspace gui      # the workspace window
```

On Windows the window is also in the Start menu (“Simab Workspace”); `simabworkspace` works in
PowerShell, cmd and Git Bash. Local terminals use PowerShell (change it in Settings).
Needs Windows 10 (1809) or newer.

> **Teams:** pre-fill your server so people only type their user name:
> `curl -fsSL …/install.sh | SIMAB_HOST=10.0.0.5 SIMAB_PORT=2222 bash`
> (PowerShell: set `$env:SIMAB_HOST` / `$env:SIMAB_PORT` before the command).

Requirements: `python3` and `curl` on your machine (on Windows: Python and Git for Windows — the installer adds them).
Downloads are also on the [Releases](https://github.com/EsmailEmami/simabworkspace/releases) page.

## Use

In the window, **+ add project** in the sidebar:

- **From the server** — pick a folder; it is downloaded and kept in sync.
- **From this machine** — upload a local folder to the server and keep it in sync.
- **Server only** — work on it on the server without copying it.

Each project has **Claude**, **Terminal**, **Code** and **Git**, its ▶ run commands, and
more under **⋯** (past chats, **Changes** — review, commit, discard —, sync details and
conflicts, move folders, colour, history). After each Claude turn that wrote files, the chat
lists them with a link to review the changes.

Run commands can be **shared with the project**: they are saved in `.simab.json` in the
project folder (commit it), so every device and everyone on the project gets them.
Everything the installer asked can be changed later in **⚙ Settings**.

| | |
|---|---|
| Search everything | `Ctrl+Shift+P` |
| Go to a file of the project | `Ctrl+P` |
| New tab / close tab | `Alt+T` / `Alt+W` |
| Split right / down | `Alt+\` / `Alt+-` |
| Hide the sidebar | `Alt+M` |
| Persian input box in a terminal | `Alt+F` |

Closing the window keeps terminals and chats running; open it again to continue.

## Web version (tablet, another computer)

Run the workspace on a server and open it in any browser:

```bash
simabworkspace web --hosts 127.0.0.1:22 --port 8443 --name my.server.com
```

Open `https://my.server.com:8443` and sign in with an **SSH user and password** of a
server in `--hosts` (empty = any server — better to list yours). The password is only
used to sign in and to put a key of the workspace on that server; it is never stored.
Five wrong passwords from one address lock it out for 15 minutes. In **⚙ Settings** you can
turn on **two-step sign-in** (a code from an authenticator app), see and sign out the
**signed-in devices**, and turn on **notifications on this device** — when Claude finishes or
needs you, even with the page closed.

Each user@server gets its own projects, chats and history. Everything runs on the server:
Claude, server terminals, Git, Docker and server run commands. Things that need your own
machine are not offered: local terminals, sync, VS Code, port forwarding, updates.

Without `--cert/--key` a self-signed certificate is made (the browser warns once). Behind
nginx or Caddy: `--proxy --bind 127.0.0.1` (plain HTTP inside, the proxy does HTTPS and
must pass `Host`, `X-Real-IP` and the WebSocket upgrade).

## Update · uninstall

Run the install command again to update — settings and projects are kept.
Uninstall from **⚙ Settings → Uninstall** or `simabworkspace uninstall`
(stops syncing; your files stay on both sides).

## How it works

- `simabworkspace.sh` — the command: setup, SSH key, Mutagen sync sessions, menu.
- `gui/server.py` — a small local web server (Python standard library only) that runs
  terminals in PTYs, talks to Claude Code in stream-json mode over SSH, and serves the
  window on `127.0.0.1` with a random token.
- `gui/static/` — the window (plain JavaScript, xterm.js, no build step).

Development: `python3 -m unittest discover -s tests` · build the release files with
`bash release.sh` · pushing a tag `vX.Y.Z` publishes a release (GitHub Actions).

## License

MIT
