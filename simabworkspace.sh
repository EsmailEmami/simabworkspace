#!/usr/bin/env bash
# =============================================================================
#  simabworkspace — keep your projects on the shared server in sync with your
#  own machine (Linux / WSL / macOS / Windows), so builds and dev servers run locally.
#
#  Uses Mutagen (two-way, real-time file sync over SSH). Nothing has to be
#  installed on the server: Mutagen copies its own small agent there.
#
#  Usage:
#    simabworkspace                 interactive menu (status + all actions)
#    simabworkspace status          show connection + sync status and exit
#    simabworkspace add             add a project to sync (asks: on the server, or on this machine)
#    simabworkspace add-local [FOLDER] [--remote PATH] [--name NAME] [--git on|off] [--ignores LIST] [--yes] [--no-wait] [--paused]
#                                   upload a project from this machine to the server, then sync it
#    simabworkspace add-server [SERVER_FOLDER] [--local PATH] [--name NAME] [--git on|off] [--ignores LIST] [--yes] [--no-wait] [--paused]
#                                   download a project from the server to this machine, then sync it
#    simabworkspace pause   NAME    pause one project        (resume later)
#    simabworkspace resume  NAME    resume one project
#    simabworkspace flush   NAME    sync now and wait until done
#    simabworkspace remove  NAME    stop syncing a project (files are kept)
#    simabworkspace info    NAME    details + conflicts of a project
#    simabworkspace monitor NAME    live view of a project
#    simabworkspace move    NAME [--local PATH] [--remote PATH] [--yes] [--no-wait]
#                                   change the local and/or server folder of a project
#    simabworkspace git     NAME [on|off]  sync the project's .git folders or not
#    simabworkspace ignore  NAME [LIST|--default]  what is never synced (comma list, e.g. node_modules,dist)
#    simabworkspace pause-all | resume-all
#    simabworkspace disconnect      pause everything and stop the sync daemon
#    simabworkspace reconnect       start the daemon and resume everything
#    simabworkspace gui             open the workspace window (tabs, Claude, VS Code, run buttons)
#    simabworkspace self-install    install the `simabworkspace` command and window files (used by the installer)
#    simabworkspace update          download and install the latest release
#    simabworkspace gui stop|status|url   stop / check the window's background server
#    simabworkspace web --hosts SERVER   the workspace in the browser (tablet…), sign in with SSH user+password
#    simabworkspace ssh             open a shell on the server
#    simabworkspace key             choose / create the SSH key used for the server
#    simabworkspace setup           (re)check prerequisites, SSH key, settings
#    simabworkspace install mutagen|jq|curl|tar|ssh|git   install one prerequisite
#    simabworkspace uninstall [--yes]   remove all syncs, settings and this tool
#    simabworkspace help
#    simabworkspace version
#
#  Changelog:
#    1.65.19 2026-11-23  Project groups, colours and order are the same on every device; this computer's LAN address in the sidebar
#    1.65.20 2026-11-24  Web shows your computers' run commands (which computer / server, greyed when off); "Ask me" of a shared link answered on the phone; shared port edit
#    1.66.0  2026-11-25  Phone access: several devices (phone, tablet…) each with its own code, list + Remove, turn off without a code; drag & drop into a server / other-computer project works; no computer connected → commands run on the server
#    1.66.1  2026-11-25  A new computer: "Sync to this computer" (project ⋯) and "Sync all projects to this computer" (Ctrl+Shift+P) download the projects added on another device
#    1.66.2  2026-11-25  Linux: the app logo in the app menu and on the open window (dock), not a gear — the menu entry is made / fixed on every start
#    1.66.3  2026-11-25  Linux installer adds the app-menu entry; Share a port chooses the computer; only connected computers that have the project are offered; the sidebar mark names the computer
#    1.67.0  2026-11-25  Same on every device: starter message, project name and colour, stars, chat default mode/model/effort; Add project on a new computer starts with the Not synced list of that folder
#    1.68.0  2026-11-25  Several computers on: a chat runs on one (the one it was opened on, the next only while it is off); Settings → Sync: Sync all to this computer, Copy from another computer (starter, run commands, names, colours)
#    1.72.2  2026-11-28  Own server copy: folders the server user cannot read (a container database, docker/data/main-db) and Not synced paths are left out of the copy, and the unreadable ones join Not synced
#    1.72.1  2026-11-27  Sync to this computer: a folder that already has files can be merged (a "Merge" choice after the error) instead of a dead end
#    1.72.0  2026-11-27  Not synced: a change is safe (snapshot, recreate, guard counts start again, optional winner for the first round, then two-way-safe); new defaults (graphify-out, .dev-run, *.log); guard: a new list / recreated session / CLI resume is no mass delete, every pause logged, a long pause told; Mutagen left-out conflicts counted + one side wins all; sync list never blocks; git name/email check; onpc keeps remote URLs and ssh commands; a new project takes run commands from my other computers
#    1.71.0  2026-11-27  Two computers: a chat runs only on the computer it was opened on and its tab stays there; each computer gets its own server copy (folder@computer), changes cross via git push / pull, a Pull notice after the other one pushes
#    1.70.2  2026-11-27  Linux Wayland: the window opens under XWayland so the dock shows the logo, not a gear
#    1.70.1  2026-11-27  Tab drag: a gap shows where the tab lands and the tabs slide; a tab dragged out opens a smaller window under the mouse that fades in
#    1.70.0  2026-11-27  Sidebar: no file tree; a Files button under Usage opens the Files tab of the active project
#    1.69.3  2026-11-27  Mutagen 0.18: the Not synced list and Git setting are read again (ignore.paths / ignore.vcs) — the sync guard paused every project; the guard never counts node_modules/dist/… when comparing; toasts: ×N for repeats and a close ×
#    1.69.2  2026-11-27  Not synced: Add from .gitignore (nested git repos too); sync guard: Check again, and every paused project is checked again when the app starts
#    1.69.1  2026-11-27  Sync guard: file counts skip what the sync skips (docker/data and other paths with a /, .git when Git is off) — no more "This computer has N files, the server M" pause on every project
#    1.69.0  2026-11-26  Sync guard: a .simab-root marker on both sides; a folder that lost it (disk not mounted), a nearly empty side, a folder made by root (docker) or on the wrong disk (DISK2 → DISK21), a mass delete or a lost top folder pauses the sync; the root is never auto-created; onpc says when it rewrites a path (Resume anyway asks); server snapshots before each resume and every 30 min, syncguard.py restore
#    1.65.18 2026-11-23  A project added on another device can be removed from this list; setting "Keep the message box shown" in chats
#    1.65.17 2026-11-23  Mac: a project put in a group no longer jumps back for a moment (a refresh asked before the move is dropped)
#    1.65.16 2026-11-23  A project group never vanishes by itself (last project moved out, or a refresh with an older list); only Ungroup removes it
#    1.65.15 2026-11-23  Project groups: drag a project onto a group tab, onto All (out of its group) or onto + (a new group); + at the bottom of the strip makes a group
#    1.65.14 2026-11-23  Windows: a script whose download broke is fetched again (no more "reading 'FilesTab'"); project groups as vertical tabs with a colour each; Claude's "API Error" shown once with a plain reason and Retry
#    1.65.13 2026-11-23  Projects can sit under groups in the sidebar (rename a group, drag order really saved); the app no longer fails with "Cannot reach the app" when a script loads slowly
#    1.65.12 2026-11-23  Chat survives a shaky internet: a frozen link to the server is dropped and attached again within ~20 s, and a failed retry is tried again instead of ending the chat
#    1.65.11 2026-11-23  Local only: a chat mounts the project folder before Claude starts, and says whether the link, your computer or the server is the problem; terminal Ctrl+C / Ctrl+V copy and paste without Shift
#    1.65.10 2026-11-22  Share pages get a real design; deny list asks you instead of refusing; "onserver " runs a command on the server
#    1.65.9 2026-11-22  Local only chat badge counts its real connections and running commands (it always said 0)
#    1.65.8 2026-11-22  Web version / phone: Local only chats show their line (connections, round trip, colour) and the live output of commands on your computer; Changes / git of a Local only project work from anywhere (run on its computer)
#    1.65.7 2026-11-22  Local only: the computer a project lives on is recorded on the server at every connect, so the web version and the phone no longer show "PC · off"
#    1.65.6 2026-11-22  ~/.ssh/config with an unquoted key path with a space: every ssh of the app gets a repaired copy at once; the whole file is repaired on start (backup kept)
#    1.65.5 2026-11-22  macOS: a user name with a space ("simab co") no longer breaks ~/.ssh/config (key path quoted; an old bad line is repaired on start)
#    1.65.4 2026-11-22  A Local only project opened on another of your computers: its owner is known everywhere (no false "off"), @ lists the owner's files, nothing comes from this disk; work goes only to the owner
#    1.65.3 2026-11-22  Chat top right: the line badge and the computer chip sit side by side (they overlapped)
#    1.65.2 2026-11-22  Each Local only chat shows its line to your computer at the top right: connections, round trip, green / yellow / red
#    1.65.1 2026-11-22  Local only: one connection per chat kept open, tool calls side by side, heartbeat + fast reconnect + safe retry on weak lines; chat shows the model's share of each answer's time
#    1.65.0 2026-11-22  Local only projects fast on big projects and weak internet: Claude (still on the server) reads, edits and searches files on your computer in one request each; heavy folders hidden from the mount; compressed link; Computers window never scrolls sideways
#    1.64.6 2026-11-22  Windows: a ~/.ssh/config with 'bad' owner/permissions no longer stops the connection; git commands of a 'my computers' project run on the server (the copy there may have no .git)
#    1.64.5 2026-11-22  Files tab: edit the file right where it is shown, with colours and line numbers, like VS Code
#    1.64.4 2026-11-22  Remove any project from the sidebar (list only / delete folders); Files tab looks like VS Code (icons, guides, code colours)
#    1.64.3 2026-11-22  Windows: the app runs in Python UTF-8 mode (fixes "'charmap' codec can't decode")
#    1.64.2 2026-11-21  Share a port from a project lists what it shares already; stop the program on a shared port (■), see its output when the workspace started it
#    1.64.1 2026-11-21  Share: new tabs stay on the project, link window redesigned, Revoke works at once (also on the computer); question windows close only by their answer; Settings share list cleaned, share a port from the project ⋯ menu
#    1.64.0 2026-11-21  Share: one apps address (web --share-domain) https://APPS/<user>/<project>/<name>; shared links + dev servers in the sidebar; Computers in tabs
#    1.63.2 2026-11-21  Settings in tabs (one per section); dev servers show every port they serve; QR code in the authenticator setup dialogs
#    1.63.1 2026-11-21  Share on your domain in the app too (Settings), same list as the web (share.json on the server), live state per link; one answer per port is enough
#    1.63.0 2026-11-21  Share on your domain: https://DOMAIN/<project>/<name> opens a port of your computer (private or public), many projects on one domain
#    1.62.0 2026-11-21  Computers: phone access with the computer's own MFA code (terminal, run commands, answers); rules no longer bypassed from the server; who is in + Disconnect + log
#    1.61.3 2026-11-21  Sidebar marks: "…" until checked, asked again when this computer connects; this computer counts as on while its link is up
#    1.61.2 2026-11-21  Computers: "checking…" until the fresh state, not an old offline first
#    1.61.1 2026-11-21  Phone: the tools are a left column of the drawer, like on the laptop and tablet
#    1.61.0 2026-11-21  Web / phone: a terminal of your computer (project ⋯ → Terminal on your computer)
#    1.60.0 2026-11-21  Web / phone: start, watch and stop the run commands of your computer
#    1.59.0 2026-11-21  Speed test: real down / up / round trip between this computer and the server
#    1.58.0 2026-11-21  Health: server, computers, link, sync, containers and Claude at a glance
#    1.57.0 2026-11-21  History: search inside every Claude conversation, open it to continue
#    1.56.0 2026-11-21  Saved prompts: / in a chat, for all projects or one, shared by the app and the web version
#    1.55.0 2026-11-21  Setup check: everything needed, with a fix button each (opens by itself until all is fine)
#    1.54.3 2026-11-21  Port tunnels work even when ~/.ssh/config has a failing RemoteForward (Mac)
#    1.54.2 2026-11-21  Computers: choose which computer runs a project's commands
#    1.54.1 2026-11-21  Dev servers: open it when it is up (a message, or by itself with a setting)
#    1.54.0 2026-11-21  Questions of your computers in the web version and as phone notifications
#    1.53.1 2026-11-21  Local only chats reopen with their history (Claude folder name from the full path)
#    1.53.0 2026-11-21  Connect a computer from the web version: one line with a one-time link (no password)
#    1.52.0 2026-11-21  My computers = like Local only: every command of the project runs on your computer, files stay synced on the server
#    1.51.0 2026-11-21  Dev servers on your computer (onpc --bg) with ↗ / ■ in the chat; the chat says at once when your computer drops off
#    1.50.5 2026-11-21  Chat: a tiny PC / server tag on Bash cards of projects that work on your computer
#    1.50.4 2026-11-21  Local only: Bash commands about Claude's own files (~/.claude memory, plans) stay on the server
#    1.50.3 2026-11-21  Sidebar: link quality of this computer (5 bars) + live ↓/↑ speed, for Local only projects
#    1.50.2 2026-11-21  Chat: where the work runs (⌂ local / ⚒ builds on PC) and the live output of a command on your computer inside its Bash card
#    1.50.1 2026-11-21  Sidebar: different marks for Local only (⌂ local) and builds on your computers (⚒ PC)
#    1.50.0 2026-11-21  Local only projects: files stay on your computer, Claude on the server works on them with all its tools; run commands follow; mode in the question card
#    1.49.3 2026-11-21  Computers: connects even when ~/.ssh/config has a RemoteForward that fails on the server (Mac)
#    1.49.2 2026-11-21  Computers: a question goes only to the chat that ran the command (no dialog over other tabs), with sound
#    1.49.1 2026-11-21  Computers: reconnect fixed, clearer dialog, sidebar mark, question in the chat, modes like Claude (same as chat, ask, ask new, auto)
#    1.49.0 2026-11-21  Computers: builds run on your own computers (onpc), Claude stays on the server
#    1.48.2 2026-11-21  Chat: background agents stay listed (with progress) after the turn until they finish
#    1.48.1 2026-11-21  WSL: the window opens on the WSL address when Windows cannot reach 127.0.0.1 (VPN/firewall)
#    1.47.0 2026-11-20  ▶ read: Persian made on the server when the device has no Persian voice
#    1.48.0 2026-11-20  History: remove an entry or a whole conversation, Clear (all / one project / matches)
#                       (Piper, text stays on the server; or Microsoft voices), Settings → Sounds
#    1.46.0 2026-11-20  Git dialog: resolve merge conflicts in place like VS Code (Accept Current /
#                       Incoming / Both / Edit per conflict, or a whole side), Abort merge;
#                       projects and open tabs (chats, Files, Changes, Tasks) shared app ↔ web
#    1.45.5 2026-11-20  Git dialog: commit box right under the branch, a clearer Commit button;
#                       git errors (push rejected…) stay in the dialog with the fix (Pull & push)
#    1.45.4 2026-11-20  A narrow chat (tablet split, laptop side pane) shows one chip for mode /
#                       model / effort, like phones, so the bar never overflows
#    1.45.3 2026-11-20  @ and Find file see the files of repositories inside a repository (only
#                       the first level was found); such a new repository is listed once, not twice
#    1.45.2 2026-11-20  VS Code on the server opens the project again when its folder is
#                       "~/…" (VS Code got the ~ as is and opened an empty window); Git dialog's
#                       "Open in VS Code" opens the copy git runs on (this machine or server)
#    1.45.1 2026-11-20  Chat: a code block inside a list item (indented ```) shows as a code
#                       block with copy, not as raw text
#    1.45.0 2026-11-20  Git dialog redesigned for projects with many repositories: a list of
#                       every repository with its branch and what it needs (changes, ↑ push,
#                       ↓ pull, conflicts, not published), Fetch all / Pull all / Push all;
#                       one plain sentence per repository ("↓2 to pull", "Up to date") with
#                       the one button that fixes it, and when the remote was last checked
#    1.44.0 2026-11-19  Chat: "▶ read" under each answer reads it aloud (Persian or English, code
#                       skipped); "Read aloud" in the chat menu for one message
#                       Dictation keeps going: a guess Chrome never finishes is kept, short
#                       network drops are retried, English tech words said in Persian are
#                       written in English ("ری اکت" → React)
#    1.43.2 2026-11-19  Chat/terminal: a dropped connection reconnects with one socket only (two
#                       could open, then every message and tool showed twice)
#    1.43.1 2026-11-18  Sounds speak by default, in plain English: "Your task in shop is done",
#                       "Claude has a question for you in shop", "Claude needs your permission…".
#    1.43.0 2026-11-18  Dictation: a mic in the chat box (before Send) and a small round mic beside
#                       every free-text field on focus + hover (pages and dialogs); Chrome/Edge speech
#                       recognition, Persian by default, right-click the mic for English. Live text
#                       shows at once; only final words go into the field; long dictation keeps going.
#                       Sounds: Settings → Sounds, per device — a tune (or a voice: "shop: task done")
#                       when a chat you are not looking at finishes, asks, needs permission, has a plan
#                       or stops with an error; a different tune per kind, Off / Sound / Voice each.
#    1.42.1 2026-11-18  Git: a folder is a repository only if git says so (an empty/broken .git is
#                       skipped); a stray `git init` with no commits over the real repositories is not listed.
#                       History: Claude conversations show Claude's title, then what you wrote.
#    1.42.0 2026-11-18  Tasks is a board: Backlog / To do / In progress / Done; drag cards between
#                       columns (touch: press and hold); + New task and a click on a card open a dialog.
#    1.41.0 2026-11-18  Tasks: your to-do list in the rail (✓ Tasks), kept on the server so the
#                       app and the web version show the same list; done, project, priority,
#                       drag or Move up/down to reorder, Sort by priority, Ask Claude.
#    1.40.0 2026-11-17  Add project: "First copy" N at once (--paused; the window copies over N ssh
#                       tar streams, then resumes the sync); Files: drop-upload fixed (sent twice, crashed)
#    1.39.0 2026-11-17  Add project: edit the "Not synced" list before the first sync (--ignores);
#                       sync progress shows the whole transfer (size, time left), not the current file
#    1.38.0 2026-11-17  Sync: disconnect toast + Reconnect button (mutagen resume) in Sync details;
#                       sidebar project drag-drop reorder (saved to db, persists on restart);
#                       sync progress bar: size-first format, "items" instead of "files".
#    1.37.0 2026-11-17  Skills Registry: add GitHub repos as skill sources, browse .md skills
#                       and install them per-project (.claude/skills/); commit history tab in
#                       the Git dialog with per-hunk staging; Git dialog discard selected files;
#                       chat markdown renders incrementally during streaming (every 80 ms);
#                       chat box merged send/stop button; sidebar project row cleanup.
#    1.36.0 2026-11-17  Changes tab (review, tick files to commit, discard, pull/push) and a
#                       "N files changed — Review changes" line after each Claude turn; Ctrl+P
#                       go to file; web: phone notifications (Web Push) when Claude finishes or
#                       needs you, two-step sign-in (TOTP) and signed-in devices; run commands
#                       shared in the project's .simab.json, last result ✓/✗, Open for the
#                       address a command prints; a restart keeps chat/Files/Changes tabs (same
#                       ids); browser script errors go to the app log.
#    1.35.2 2026-11-17  Chat connection state is clear: a silently dead connection (phone asleep,
#                       CDN in between) is noticed by a ping within ~25 s; after the app restarts
#                       the chat opens the same Claude again by itself; while there is no
#                       connection the message box is dimmed and sending is off (text kept); the
#                       status says "reconnecting to Claude on the server…" instead of working.
#    1.35.1 2026-11-17  Run commands: Browse… picks the command's folder among the project's
#                       server folders; Suggest works in the web version (reads the server
#                       folder); the web editor shows that commands run on the server.
#    1.35.0 2026-11-17  Chat tabs show the name Claude gives the conversation (like the VS Code
#                       extension; saved in Claude's transcript); renaming a chat tab renames the
#                       conversation in Claude too. Tablet: the message box of chats you are not
#                       in hides too.
#    1.34.0 2026-11-16  Rail (activity bar) on the left beside the projects; Files panel in the
#                       sidebar shows the active project's files; drag files from your OS into a
#                       folder to upload them; create, rename, copy/cut/paste, delete and edit
#                       files from the Files panel or the Rail → Files button (server home).
#    1.33.1 2026-11-16  Stars: drag starred entries up and down (Ctrl+Shift+P and the ⋯ menu) to
#                       put them in your own order — mouse: the row, finger: the ⠿ grip.
#    1.33.0 2026-11-16  Stars: ☆ next to a command in Ctrl+Shift+P or an entry of a project's ⋯
#                       menu puts it first (menu stars count for every project); kept in the
#                       settings, so every device sees them.
#    1.32.0 2026-11-16  Not synced, per project: Sync details → Edit… sets the folders and files
#                       that are never copied between this machine and the server
#                       (node_modules, dist…); `simabworkspace ignore NAME LIST` does the same.
#                       An empty list stays empty (moving or git on/off keeps it).
#                       Copies already there of newly added entries are listed per side
#                       (server / this machine, with sizes) and can be deleted there.
#    1.31.1 2026-11-15  Tablet: the sidebar edge and the bars between sections can be dragged
#                       with a finger (pointer events, wider grip on touch screens).
#    1.31.0 2026-11-15  Rename a project in the list (⋯ → Rename…; its real name stays).
#                       Closing a working chat asks: close only the tab and let it finish,
#                       or stop it; a chat open on another device too is never stopped
#                       by closing it on this one.
#    1.30.1 2026-11-15  Chat box on phones: mode, model and effort fold into one chip that opens
#                       a menu, so the send button (and new / history) always fit.
#    1.30.0 2026-11-15  Claude's questions step by step, like Claude Code: one question at a
#                       time with a step bar on top, Back / Next (and ←/→, 1–9 keys), a choice
#                       moves on by itself, and a last step lists every answer before sending.
#    1.29.1 2026-11-15  Stuck chat after a reopen: when Claude was waiting for an answer to a
#                       question / permission, the reopened window now gets that question again
#                       (before, Claude waited for ever and new messages only queued up). And
#                       when a chat shows no sign of life for a minute, Interrupt / Restart
#                       Claude are offered next to its status.
#    1.29.0 2026-11-15  Menu → "Claude sessions — all projects": every Claude running on the
#                       server, grouped by project (and "No project"), with Show / Open /
#                       Watch / Continue here / Stop, and the latest conversations of all
#                       projects. Sidebar: a "Claude" button opens a chat without a project
#                       (in your home folder on the server).
#    1.28.0 2026-11-15  Starter message per project (⋯ → Starter message…): every new Claude chat
#                       of the project starts with it in the message box — you write your
#                       request after it. Not used when you continue a conversation.
#    1.27.4 2026-11-15  Chat after an update / reopen: while it reconnects to Claude on the
#                       server it says so ("reconnecting…") instead of looking dead, and when
#                       Claude was in the middle of work the tab shows it working right away
#                       ("Claude kept working on the server; following it again").
#    1.27.3 2026-11-15  Project colours quieter: a thin line on top of tabs (full colour only on
#                       the active one) and a faint frame round the work area, a little
#                       clearer in the section you work in — no more loud coloured borders.
#    1.27.2 2026-11-15  Chat: the "— resumed —" line (and restarted / reconnected / compacted)
#                       no longer shows as a big empty box — a CSS class clash with the
#                       History list.
#    1.27.1 2026-11-15  Chat: an agent's report and a skill's instructions are no longer shown
#                       as if you had written them — they are folded into their Agent / Skill
#                       card (click to read), like a command's output.
#    1.27.0 2026-11-15  Sidebar: the server's load right now — CPU, RAM and disk meters above
#                       the Claude usage (orange/red when high). Click: details and the
#                       processes using the most CPU and memory.
#    1.26.2 2026-11-15  Chat: type ~/ or /home/… and the server's folders and files are
#                       offered like @ (go into folders, pick a file). Project ⋯: Copy server
#                       path (~ written out in full) and Copy local path.
#    1.26.1 2026-11-15  Tabs: when Claude is done the tab no longer keeps blinking — a steady
#                       green dot until you look; blinking only while Claude waits for you,
#                       and it stops once answered; a chat you can see is never marked.
#                       The project's colour outlines its tabs and frames their work area.
#    1.26.0 2026-11-15  Claude sessions: see every Claude running on the server per project —
#                       here, in VS Code or in a terminal (✳N on the project, working/idle).
#                       Project ⋯ → Sessions: Show / Open / Watch (live, read-only) /
#                       Continue here (stops it there first) / Stop. Opening a conversation
#                       that runs elsewhere asks first — no second Claude on it.
#    1.25.0 2026-11-15  Git panel: a History tab — the branch's commits, each marked ↑ not
#                       pushed / ↓ not pulled / ✓ on both sides, with a summary of what to
#                       push or pull; click a commit to see it. .gitignore: Find file and @
#                       in the chat follow each repository's .gitignore (also when the
#                       project holds several); files named in .gitignore that git still
#                       tracks are marked, with "Stop tracking".
#    1.24.3 2026-11-15  Add project → From this machine: "Browse…" next to the server folder
#                       opens the server's folders — go in and out, then "Use this folder",
#                       "Inside it: <your folder's name>" or "New folder…". From the server:
#                       "Browse…" picks any folder, not only the ones in your home.
#    1.24.2 2026-11-15  Chat details stay clean: background SSH connections no longer open
#                       port forwardings from your ~/.ssh/config ("remote port forwarding
#                       failed for listen port …"), and the relay ends without "Fatal Python
#                       error: _enter_buffered_busy" when Claude stops.
#    1.24.1 2026-11-15  Chat on a server whose shell is zsh: "Claude stopped (exit 127)" —
#                       claude in ~/.local/bin (added to PATH only in ~/.zshrc) was not
#                       found. claude is now looked for in every usual place (~/.local/bin,
#                       ~/.claude/local, npm, bun, volta, Homebrew, nvm, VS Code) with sh;
#                       when it is really missing, the chat says so and how to install it.
#    1.24.0 2026-11-15  Branches: the project's git branch shows next to its name; click it
#                       (or the branch in the Git panel, or Search → "switch branch") to
#                       switch, check out a branch that is only on GitHub, or create one.
#                       Uncommitted changes in the way: offered to stash them first.
#    1.23.3 2026-11-15  Servers whose login shell is zsh (or fish): "no matches found:
#                       …/*/.git" when listing server folders — our commands now run with
#                       sh on the server (folders, repositories, files, git).
#    1.23.2 2026-11-15  macOS: "keyword controlpath extra arguments at end of line" — the
#                       shared SSH connection no longer lives under a home folder with a
#                       space (or a very long one); a private /tmp folder is used instead.
#    1.23.1 2026-11-15  Chat: switching tabs (or splitting, moving, reconnecting) no longer
#                       jumps to the first message — it stays at the end, or where you were
#                       reading. The message box hides after 3 s while you work elsewhere
#                       (the live status stays) and is back as soon as you return.
#    1.23.0 2026-11-15  Extra windows for a second monitor: tab menu → Move to new window,
#                       or drag a tab out of the window; drag tabs between windows; each
#                       window has its own tabs and splits. Chat fix: opening a conversation
#                       again (Reopen last session, History, the web version, another window)
#                       uses the Claude that is still working on it — no second Claude, and
#                       its live output shows in the chat.
#    1.22.0 2026-11-15  Files tab (project ⋯ → Files): the project's folders, git changes
#                       marked on files and folders, files shown read-only (text, images),
#                       their changes one click away, "Ask Claude" puts @file in the chat,
#                       Find file. Works in the web version too (reads the server).
#    1.21.2 2026-11-15  Chat: Claude keeps working on the server when the connection drops;
#                       the window reconnects by itself to the SAME Claude and shows what it
#                       did meanwhile (no second Claude on the conversation). Closing the
#                       tab stops it; one nobody uses is stopped after 30 idle minutes.
#    1.21.1 2026-11-15  Windows: "VS Code, local folder" works (code.cmd started by its full
#                       path); add project: Browse… (the system's folder dialog) and the full
#                       path under the field; project menu: Open folder (Explorer / Finder)
#    1.21.0 2026-11-15  Windows without WSL: install.ps1 installs Python + Git for Windows
#                       (winget) and runs the app on Windows itself — terminals in the
#                       Windows pseudo console (PowerShell locally), Mutagen/jq for Windows,
#                       Start menu entry, `simabworkspace` in PowerShell/cmd/Git Bash
#                       Chat: the model list shows real models (Opus 5.5 is the default,
#                       no vague "default model"); thinking effort picker (default medium)
#    1.20.0 2026-11-14  Web version: `simabworkspace web` serves the workspace to a browser
#                       (tablet…) over HTTPS; sign in with an SSH user and password of an
#                       allowed server (checked, never stored); one workspace per user;
#                       server-only (no local terminals, sync, VS Code, port forwarding).
#                       Add project: browse the server's folders and pick one.
#                       Phone/tablet layout (projects drawer, full-screen dialogs, keys
#                       bar for terminals, add to home screen); installed-app features
#                       shown disabled on the web. Storage moved to SQLite (simab.db).
#                       Installable as an app (PWA: service worker, icons); project row
#                       has only Claude, Git and ⋯; --cert-download for self-signed servers.
#    1.19.1 2026-11-14  port forwarding: server ports below 1024 get a local port + 10000
#                       (low ports need admin rights) and say so instead of "in use"
#                       Port forwarding lists the ports open on the server (click to add);
#                       sidebar shows everything: Docker / Ports / History under each project,
#                       Search / History / Usage / Menu at the bottom
#    1.19.0 2026-11-14  Window: Docker panel per project (containers on the server, logs,
#                       shell, restart, rebuild; a notice when one crashes), port forwarding
#                       (ssh tunnels to localhost, one click, reconnect by themselves) and
#                       Claude usage (5-hour / weekly limit in the sidebar, cost per day).
#    1.18.2 2026-11-14  WSL: the app keeps WSL running while it runs (closing the last
#                       terminal no longer kills it); lost connection → one notice and
#                       automatic reconnect instead of "Failed to fetch"; crash log
#    1.18.1 2026-11-14  setup never looks frozen: numbered steps, sudo asked first, a
#                       spinner with seconds and the latest output line for long steps
#    1.18.0 2026-11-14  updates: the window says when a new version is out and installs it
#                       in place (restarts and reloads by itself); `simabworkspace update`
#    1.17.0 2026-11-14  chat: send while Claude works (queued, picked up next), live list
#                       of running agents; public release: one-line installers for
#                       Linux/macOS/WSL and Windows, no built-in server address
#    1.16.3 2026-11-14  long Claude conversations: the last 40 messages load first, earlier
#                       ones as you scroll up
#    1.16.2 2026-11-14  History / Past chats open conversations in the window's own chat
#                       (with their messages); an open conversation is focused, not
#                       duplicated; release.sh builds a one-file installer
#    1.16.1 2026-11-14  Claude conversations open in a terminal from History, Past chats
#                       and the chat tab (exactly that conversation: --resume ID)
#    1.16.0 2026-11-14  window: sync details + resolve conflicts (keep this machine's /
#                       the server's version), app-menu / Start-menu shortcut, .git sync
#                       from the Git panel without a terminal
#    1.15.0 2026-11-14  in the window too: prerequisites (install, sudo password once),
#                       move a project's folders, uninstall; jq installs without sudo;
#                       `install`, `move --yes`, `uninstall --yes`; window calls never
#                       start the terminal wizard (SW_NONINTERACTIVE)
#    1.14.0 2026-11-14  every wizard is in the window: add project from the server / from
#                       this machine (no terminal), server connection, SSH key create +
#                       install (password once), sync service, autostart, .git sync, stop sync
#    1.13.0 2026-11-14  upload/download progress under the project + "finished" notice;
#                       transfer tabs can be closed (copy goes on); Claude remembers the
#                       mode and model you chose (per project)
#    1.12.0 2026-11-14  add-local: upload a project from this machine to the server and sync
#                       it; "Add project" asks where the project is (also in the window)
#    1.11.0 2026-11-14  git badges on every project: changes, ↑ to push, ↓ to pull,
#                       ! conflicts (checked in the background, fetch every 5 minutes)
#    1.10.0 2026-11-14  History of what you did (Claude messages, runs, terminals, git,
#                       VS Code, sync) with search and "continue / run again"; images in
#                       chat open large on click
#    1.9.1  2026-11-14  git: projects with several repositories inside (e.g. api + web)
#    1.9.0  2026-11-14  a colour per project (automatic or chosen); its tabs carry it
#    1.8.3  2026-11-14  updates are automatic: the installed command remembers the folder it
#                       came from and takes new versions from there; old windows say so
#    1.8.2  2026-11-14  fix: "this.group is not a function" in Claude chats (name clash)
#    1.8.1  2026-11-14  chat: no more doubled messages or stuck "sending…" (robust replay,
#                       echo matched by order, status reset on finish)
#    1.8.0  2026-11-14  Git per project (changes, diff, commit, pull, push; conflicts →
#                       VS Code); chat: live status line, your message shows at once,
#                       live thinking + smooth typing; Persian questions right-to-left
#    1.7.0  2026-11-14  window redesigned: English, minimal, terminal-first (no toolbars
#                       on terminals, actions in right-click / Ctrl+Shift+P), JetBrains
#                       Mono + WebGL rendering for terminals, Inter for the interface
#    1.6.3  2026-11-14  fix: split sections always fill the window; chat answer shown
#                       once (and always — falls back to the final result); errors shown
#    1.6.2  2026-11-14  work mode (Alt+M: only tabs, no menus/sidebar); run commands
#                       can run in a chosen folder of the project
#    1.6.1  2026-11-14  chat: @ file search, Claude's questions answered with buttons,
#                       plan approval card (approve / approve+auto-edit / keep planning)
#    1.6.0  2026-11-14  split view: sections side by side / top-bottom, resizable,
#                       drag tabs between sections or onto an edge, zoom, grid layout
#    1.5.0  2026-11-14  Claude chat panel like the VS Code extension: Persian-friendly
#                       messages, tool cards with diffs, permission buttons, modes,
#                       model, images, slash commands, past conversations (resume)
#    1.4.2  2026-11-14  VS Code button = this machine's VS Code connected to the server
#                       folder over Remote-SSH (WSL: host entry + key also for Windows)
#    1.4.1  2026-11-14  one Claude, always on the server (uses the VS Code extension's
#                       binary if the claude CLI is not installed); an outdated GUI
#                       still running in the background is restarted automatically
#    1.4.0  2026-11-14  "gui": Persian workspace window — tabbed terminals (local and
#                       server), Claude Code per project, open in VS Code, saved
#                       run buttons per project, sync status/actions
#    1.3.0  2026-11-12  .git folders are synced (asked per project, default yes);
#                       "Git sync on/off" for existing projects; GIT column
#    1.2.0  2026-11-12  live progress (files, size, %, time) for initial sync,
#                       "sync now" and move; Ctrl+C keeps syncing in background
#    1.1.1  2026-11-12  script kept in a folder on PATH (e.g. ~/bin) is symlinked,
#                       not copied, so edits apply immediately
#    1.1.0  2026-11-12  new UI (header box, project table, grouped menu, no
#                       emoji glyphs); move project folders; choose/create SSH
#                       key; shows hand-made Mutagen sessions too
#    1.0.0  2026-11-12  first release
# =============================================================================

set -u -o pipefail

VERSION="1.72.2"
APP="simabworkspace"
LABEL="simabworkspace=managed"

# A team can pre-fill its server for the installer: SIMAB_HOST=… SIMAB_PORT=…
DEFAULT_HOST="${SIMAB_HOST:-}"
DEFAULT_PORT="${SIMAB_PORT:-22}"

# Folders that are never synced (rebuilt locally / too big / server-only data).
DEFAULT_IGNORES="node_modules,.angular,dist,tmp,docker/data,.dart_tool,build,.next,.nuxt,.cache,__pycache__,.venv,.gradle,graphify-out,.dev-run,*.log"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
CONFIG_FILE="$CONFIG_DIR/config"
BIN_DIR="$HOME/.local/bin"
MUTAGEN_FALLBACK_VERSION="v0.18.1"
RC_MARK_BEGIN="# >>> $APP >>>"
RC_MARK_END="# <<< $APP <<<"

SSH_USER=""
SSH_HOST="$DEFAULT_HOST"
SSH_PORT="$DEFAULT_PORT"
SSH_KEY=""   # private key used for the server (empty = ssh defaults)

# ----------------------------------------------------------------------------- ui
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_R=$(tput setaf 1); C_G=$(tput setaf 2); C_Y=$(tput setaf 3); C_B=$(tput setaf 4)
  C_C=$(tput setaf 6); C_BOLD=$(tput bold); C_DIM=$(tput dim 2>/dev/null || true); C_0=$(tput sgr0)
else
  C_R=""; C_G=""; C_Y=""; C_B=""; C_C=""; C_BOLD=""; C_DIM=""; C_0=""
fi

info()  { printf '%s\n' "  ${C_C}›${C_0} $*"; }
ok()    { printf '%s\n' "  ${C_G}✓${C_0} $*"; }
warn()  { printf '%s\n' "  ${C_Y}!${C_0} $*" >&2; }
err()   { printf '%s\n' "  ${C_R}✗${C_0} $*" >&2; }
die()   { err "$*"; exit 1; }
title() { printf '\n  %s\n  %s\n' "${C_BOLD}$*${C_0}" "${C_DIM}$(repeat '─' $(( ${#1} + 2 )))${C_0}"; }
pause_key() { [ -t 0 ] || return 0; printf '\n  %s' "${C_DIM}Press Enter to go back…${C_0}"; read -r _ || true; }

# ---- layout helpers (width-safe: bash printf pads by bytes, not characters)
repeat() { local s="" i=0; while [ $i -lt "$2" ]; do s="$s$1"; i=$((i + 1)); done; printf '%s' "$s"; }

# pad <text> <width>: cut with … or fill with spaces to exactly <width> chars
pad() {
  local t="$1" w="$2" n
  n=${#t}
  if [ "$n" -gt "$w" ]; then t="${t:0:$((w - 1))}…"; n=$w; fi
  printf '%s%s' "$t" "$(repeat ' ' $((w - n)))"
}

# lpad <text> <width>: like pad, but keeps the END of long text (good for paths)
lpad() {
  local t="$1" w="$2" n
  n=${#t}
  if [ "$n" -gt "$w" ]; then t="…${t:$((n - w + 1))}"; n=$w; fi
  printf '%s%s' "$t" "$(repeat ' ' $((w - n)))"
}

term_width() {
  local c
  c=$(tput cols 2>/dev/null || echo 80)
  case "$c" in ''|*[!0-9]*) c=80 ;; esac
  [ "$c" -gt 100 ] && c=100
  [ "$c" -lt 60 ] && c=60
  printf '%s' "$c"
}

# ask "Question" "default" -> prints answer
ask() {
  local q="$1" def="${2:-}" a
  if [ -n "$def" ]; then
    read -r -e -p "$q [${def}]: " a || a=""
    printf '%s' "${a:-$def}"
  else
    read -r -e -p "$q: " a || a=""
    printf '%s' "$a"
  fi
}

# confirm "Question" [y|n default]
confirm() {
  local q="$1" def="${2:-n}" a hint="[y/N]"
  [ "$def" = "y" ] && hint="[Y/n]"
  read -r -p "$q $hint " a || a=""
  a=$(printf '%s' "${a:-$def}" | tr '[:upper:]' '[:lower:]')
  [ "$a" = "y" ] || [ "$a" = "yes" ]
}

# ----------------------------------------------------------------------------- platform
OS="$(uname -s)"
IS_WSL=0
if [ "$OS" = "Linux" ] && grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then IS_WSL=1; fi
# Native Windows: this script runs in Git Bash (Git for Windows); Mutagen, jq and Python are
# Windows programs, so they get Windows paths (C:/Users/…) and no MSYS path rewriting.
IS_WIN=0
case "$OS" in MINGW*|MSYS*|CYGWIN*) IS_WIN=1 ;; esac
if [ $IS_WIN -eq 1 ]; then
  USER="${USER:-${USERNAME:-me}}"
  mutagen() { MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' command mutagen "$@"; }
  jq() { command jq -b "$@"; }          # -b: no CRLF line ends from the Windows jq.exe
fi
# winpath <path> -> C:/Users/me/… on Windows (what Windows programs understand), unchanged elsewhere
winpath() {
  if [ $IS_WIN -eq 1 ] && command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

SUDO=""
if [ $IS_WIN -eq 0 ] && [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="${SW_SUDO:-sudo}"; fi

# run_step "label" command…  — long steps never look frozen: a spinner, the seconds
# spent and the latest output line while it runs; the full output only on failure.
run_step() {
  local label="$1"; shift
  local log start pid rc last i=0 frames='|/-\'
  log=$(mktemp); start=$(date +%s)
  "$@" </dev/null >"$log" 2>&1 &
  pid=$!
  if [ -t 1 ]; then
    while kill -0 "$pid" 2>/dev/null; do
      last=$(tail -n 1 "$log" 2>/dev/null | tr -d '\r' | tr -s ' ' | cut -c1-60)
      printf '\r\033[K  %s %s  %ss  %s%s%s' "${frames:$((i % 4)):1}" "$label" "$(( $(date +%s) - start ))" "$C_DIM" "$last" "$C_0"
      i=$((i + 1))
      sleep 0.25
    done
    printf '\r\033[K'
  fi
  wait "$pid"; rc=$?
  if [ $rc -eq 0 ]; then
    ok "$label ($(( $(date +%s) - start ))s)"
  else
    err "$label — failed:"
    tail -n 15 "$log" | sed 's/^/      /' >&2
  fi
  rm -f "$log"
  return $rc
}

# ask for the sudo password up front (in the foreground), so the steps after it can run quietly
sudo_ready() {
  [ -n "$SUDO" ] || return 0
  sudo -n true 2>/dev/null && return 0
  [ -n "${SW_SUDO:-}" ] && { $SUDO -v; return; }
  info "Administrator password needed to install packages (sudo):"
  $SUDO -v
}

pkg_install() {
  # pkg_install <generic names...>  (curl tar gzip ssh jq)
  local pkgs=() p
  for p in "$@"; do
    case "$p" in
      ssh)
        if command -v apt-get >/dev/null 2>&1 || command -v zypper >/dev/null 2>&1; then pkgs+=(openssh-client)
        elif command -v apk >/dev/null 2>&1; then pkgs+=(openssh-client)
        elif command -v pacman >/dev/null 2>&1; then pkgs+=(openssh)
        else pkgs+=(openssh-clients); fi ;;
      *) pkgs+=("$p") ;;
    esac
  done
  if [ $IS_WIN -eq 1 ]; then
    # ssh, curl, tar, gzip and git all come with Git for Windows
    command -v winget >/dev/null 2>&1 || die "Install Git for Windows (https://git-scm.com/download/win), then try again."
    run_step "Installing Git for Windows (winget)" winget install --id Git.Git -e --source winget \
      --accept-package-agreements --accept-source-agreements --silent
    return
  fi
  info "Installing: ${pkgs[*]}"
  if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || die "Homebrew is required on macOS: https://brew.sh"
    run_step "brew install ${pkgs[*]}" brew install "${pkgs[@]}"
    return
  fi
  sudo_ready || { err "sudo was not accepted."; return 1; }
  if command -v apt-get >/dev/null 2>&1; then
    run_step "Updating the package list" $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update \
      && run_step "Installing ${pkgs[*]}" $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    run_step "Installing ${pkgs[*]}" $SUDO dnf install -y "${pkgs[@]}"
  elif command -v yum >/dev/null 2>&1; then
    run_step "Installing ${pkgs[*]}" $SUDO yum install -y "${pkgs[@]}"
  elif command -v pacman >/dev/null 2>&1; then
    run_step "Installing ${pkgs[*]}" $SUDO pacman -Sy --noconfirm --needed "${pkgs[@]}"
  elif command -v zypper >/dev/null 2>&1; then
    run_step "Installing ${pkgs[*]}" $SUDO zypper --non-interactive install "${pkgs[@]}"
  elif command -v apk >/dev/null 2>&1; then
    run_step "Installing ${pkgs[*]}" $SUDO apk add --no-cache "${pkgs[@]}"
  else
    die "No supported package manager found. Install manually: ${pkgs[*]}"
  fi
}

ensure_path() {
  case ":$PATH:" in *":$BIN_DIR:"*) return 0 ;; esac
  export PATH="$BIN_DIR:$PATH"
  local rc
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ -f "$rc" ] || continue
    grep -q "$APP PATH" "$rc" 2>/dev/null && continue
    printf '\nexport PATH="$HOME/.local/bin:$PATH"  # %s PATH\n' "$APP" >> "$rc"
  done
  if [ ! -f "$HOME/.bashrc" ] && [ ! -f "$HOME/.zshrc" ]; then
    printf 'export PATH="$HOME/.local/bin:$PATH"  # %s PATH\n' "$APP" >> "$HOME/.bashrc"
  fi
}

# Windows: the zip from GitHub (mutagen.exe + mutagen-agents.tar.gz) into ~/.local/bin
install_mutagen_windows() {
  local arch url tmp
  case "${PROCESSOR_ARCHITEW6432:-${PROCESSOR_ARCHITECTURE:-AMD64}}" in
    ARM64) arch=arm64 ;; x86) arch=386 ;; *) arch=amd64 ;;
  esac
  info "Downloading Mutagen (windows/$arch)…"
  url=$(curl -fsSL --max-time 20 https://api.github.com/repos/mutagen-io/mutagen/releases/latest 2>/dev/null \
        | grep -o "https://[^\"]*mutagen_windows_${arch}_v[^\"]*\.zip" | head -1 || true)
  if [ -z "$url" ]; then
    url="https://github.com/mutagen-io/mutagen/releases/download/${MUTAGEN_FALLBACK_VERSION}/mutagen_windows_${arch}_${MUTAGEN_FALLBACK_VERSION}.zip"
    warn "Could not query the latest release, using $MUTAGEN_FALLBACK_VERSION"
  fi
  tmp=$(mktemp -d)
  curl -fL --retry 3 --progress-bar "$url" -o "$tmp/mutagen.zip" || { rm -rf "$tmp"; die "Download failed: $url"; }
  mkdir -p "$BIN_DIR"
  if command -v unzip >/dev/null 2>&1; then
    unzip -o -q "$tmp/mutagen.zip" -d "$BIN_DIR"
  else
    powershell.exe -NoProfile -NonInteractive -Command \
      "Expand-Archive -Force -LiteralPath '$(cygpath -w "$tmp/mutagen.zip")' -DestinationPath '$(cygpath -w "$BIN_DIR")'"
  fi || { rm -rf "$tmp"; die "Could not extract Mutagen"; }
  rm -rf "$tmp"
  ensure_path
  ok "Mutagen installed: $("$BIN_DIR/mutagen.exe" version 2>/dev/null)"
}

install_mutagen() {
  if [ $IS_WIN -eq 1 ]; then install_mutagen_windows; return; fi
  if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || die "Homebrew is required on macOS: https://brew.sh"
    brew install mutagen-io/mutagen/mutagen
    return
  fi
  local arch url tmp
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l|armv6l) arch=arm ;;
    i386|i686) arch=386 ;;
    *) die "Unsupported CPU architecture: $(uname -m)" ;;
  esac
  info "Downloading Mutagen (linux/$arch)…"
  url=$(curl -fsSL --max-time 20 https://api.github.com/repos/mutagen-io/mutagen/releases/latest 2>/dev/null \
        | grep -o "https://[^\"]*mutagen_linux_${arch}_v[^\"]*\.tar\.gz" | head -1 || true)
  if [ -z "$url" ]; then
    url="https://github.com/mutagen-io/mutagen/releases/download/${MUTAGEN_FALLBACK_VERSION}/mutagen_linux_${arch}_${MUTAGEN_FALLBACK_VERSION}.tar.gz"
    warn "Could not query the latest release, using $MUTAGEN_FALLBACK_VERSION"
  fi
  tmp=$(mktemp -d)
  curl -fL --retry 3 --progress-bar "$url" -o "$tmp/mutagen.tar.gz" || { rm -rf "$tmp"; die "Download failed: $url"; }
  mkdir -p "$BIN_DIR"
  # the archive holds `mutagen` + `mutagen-agents.tar.gz`; both must sit together
  tar -xzf "$tmp/mutagen.tar.gz" -C "$BIN_DIR" || { rm -rf "$tmp"; die "Could not extract Mutagen"; }
  chmod +x "$BIN_DIR/mutagen"
  rm -rf "$tmp"
  ensure_path
  ok "Mutagen installed: $("$BIN_DIR/mutagen" version 2>/dev/null)"
}

# install_jq: static binary into ~/.local/bin (no sudo), else the package manager
install_jq() {
  local arch os url
  case "$(uname -m)" in x86_64|amd64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; *) arch="" ;; esac
  local ext=""
  case "$OS" in Linux) os=linux ;; Darwin) os=macos ;; *) os="" ;; esac
  if [ $IS_WIN -eq 1 ]; then os=windows; ext=".exe"; [ -n "$arch" ] || arch=amd64; fi
  if [ -n "$arch" ] && [ -n "$os" ]; then
    url="https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-$os-$arch$ext"
    mkdir -p "$BIN_DIR"
    if run_step "Downloading jq" curl -fSL --retry 2 --max-time 120 "$url" -o "$BIN_DIR/jq.tmp$ext" \
       && chmod +x "$BIN_DIR/jq.tmp$ext" && "$BIN_DIR/jq.tmp$ext" --version >/dev/null 2>&1; then
      mv -f "$BIN_DIR/jq.tmp$ext" "$BIN_DIR/jq$ext"; ensure_path; ok "jq installed: $("$BIN_DIR/jq$ext" --version)"; return 0
    fi
    rm -f "$BIN_DIR/jq.tmp$ext"
    [ $IS_WIN -eq 1 ] && die "Could not download jq: $url"
    warn "Could not download jq; trying the package manager."
  fi
  pkg_install jq
}

# install_one NAME — one prerequisite, no questions (mutagen and jq need no sudo)
install_one() {
  case "${1:-}" in
    mutagen) install_mutagen ;;
    jq)      install_jq ;;
    curl|tar|gzip|ssh|git) pkg_install "$1" ;;
    *) err "Unknown: ${1:-}"; return 1 ;;
  esac
}

check_prereqs() {
  title "Prerequisites"
  local missing=() c
  for c in curl tar gzip ssh jq; do
    type -P "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  if [ $IS_WIN -eq 1 ] && [[ " ${missing[*]:-} " == *" jq "* ]]; then     # jq.exe from GitHub, not winget
    install_jq || die "Could not install jq"
    local rest=() m
    for m in "${missing[@]}"; do [ "$m" = "jq" ] || rest+=("$m"); done
    missing=("${rest[@]+"${rest[@]}"}")
  fi
  if [ ${#missing[@]} -gt 0 ]; then
    warn "Missing: ${missing[*]}"
    pkg_install "${missing[@]}" || die "Could not install: ${missing[*]}"
  fi
  ok "curl, tar, ssh, jq"

  [ -x "$BIN_DIR/mutagen" ] && ensure_path
  if ! type -P mutagen >/dev/null 2>&1; then
    install_mutagen
  else
    ok "Mutagen $(mutagen version 2>/dev/null)"
  fi
  type -P mutagen >/dev/null 2>&1 || die "Mutagen is not on PATH. Open a new terminal and run $APP again."

  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ok "Docker + compose (to run projects locally)"
  else
    warn "Docker is not available — syncing works without it, but you need it to run projects locally."
    if [ $IS_WIN -eq 1 ]; then
      warn "  Windows: install Docker Desktop: https://www.docker.com/products/docker-desktop/"
    elif [ $IS_WSL -eq 1 ]; then
      warn "  WSL: install Docker Desktop on Windows → Settings → Resources → WSL Integration → enable this distro."
    elif [ "$OS" = "Darwin" ]; then
      warn "  macOS: install Docker Desktop: https://www.docker.com/products/docker-desktop/"
    elif [ -t 0 ] && confirm "  Install Docker Engine now (official get.docker.com script)?" n; then
      curl -fsSL https://get.docker.com | $SUDO sh && $SUDO usermod -aG docker "$USER" \
        && ok "Docker installed — log out and back in so the docker group applies."
    fi
  fi
  if [ $IS_WSL -eq 1 ]; then
    ok "WSL detected — keep synced projects under $HOME (not /mnt/c…): faster, and file watching works"
  fi
}

# ----------------------------------------------------------------------------- config
load_config() {
  # shellcheck disable=SC1090
  [ -f "$CONFIG_FILE" ] && . "$CONFIG_FILE"
  SSH_HOST="${SSH_HOST:-$DEFAULT_HOST}"
  SSH_PORT="${SSH_PORT:-$DEFAULT_PORT}"
}

save_config() {
  mkdir -p "$CONFIG_DIR"
  {
    printf '# %s settings\n' "$APP"
    printf 'SSH_USER=%q\n' "$SSH_USER"
    printf 'SSH_HOST=%q\n' "$SSH_HOST"
    printf 'SSH_PORT=%q\n' "$SSH_PORT"
    printf 'SSH_KEY=%q\n' "$SSH_KEY"
  } > "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
}

configure() {
  title "Server settings"
  local u h p
  while :; do
    u=$(ask "Your SSH username on the server" "$SSH_USER")
    [ -n "$u" ] && break
    warn "Username is required."
  done
  while :; do
    h=$(ask "Server address (IP or host name)" "$SSH_HOST")
    [ -n "$h" ] && break
    warn "Server address is required."
  done
  p=$(ask "SSH port" "$SSH_PORT")
  case "$p" in ''|*[!0-9]*) warn "Invalid port, using $DEFAULT_PORT"; p="$DEFAULT_PORT" ;; esac
  SSH_USER="$u"; SSH_HOST="$h"; SSH_PORT="$p"
  save_config
  ok "Saved to $CONFIG_FILE"
}

remote() { printf '%s@%s' "$SSH_USER" "$SSH_HOST"; }

# ssh with the chosen key (if any) — used for everything except Mutagen,
# which reads ~/.ssh/config (see write_ssh_config)
sshk() {
  if [ -n "$SSH_KEY" ] && [ -f "$SSH_KEY" ]; then
    ssh -i "$SSH_KEY" "$@"
  else
    ssh "$@"
  fi
}

# run a command on the server non-interactively (key auth only)
rssh() {
  sshk -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new \
       -p "$SSH_PORT" "$(remote)" "$@"
}

ssh_ok() { rssh true >/dev/null 2>&1; }

SSH_CONFIG="$HOME/.ssh/config"

# key names ssh (and so Mutagen) tries on its own
is_default_key() {
  case "$(basename "$1")" in
    id_rsa|id_ecdsa|id_ecdsa_sk|id_ed25519|id_ed25519_sk|id_dsa) return 0 ;;
  esac
  return 1
}

remove_ssh_config() {
  [ -f "$SSH_CONFIG" ] || return 0
  sed -i.bak "/$RC_MARK_BEGIN/,/$RC_MARK_END/d" "$SSH_CONFIG" && rm -f "$SSH_CONFIG.bak"
}

# Mutagen runs plain `ssh`, so a key with a custom name must be in ~/.ssh/config.
# Only our own marked block is touched; the rest of the file is left alone.
write_ssh_config() {
  remove_ssh_config
  [ -n "$SSH_KEY" ] || return 0
  is_default_key "$SSH_KEY" && return 0
  touch "$SSH_CONFIG" && chmod 600 "$SSH_CONFIG"
  {
    printf '\n%s\n' "$RC_MARK_BEGIN"
    printf 'Host %s\n' "$SSH_HOST"
    # quoted when the path has a space (/Users/simab co/…): unquoted, ssh refuses the whole file
    _k=$(winpath "$SSH_KEY"); case "$_k" in *" "*) _k="\"$_k\"" ;; esac
    printf '    IdentityFile %s\n' "$_k"
    printf '%s\n' "$RC_MARK_END"
  } >> "$SSH_CONFIG"
  ok "Added your key for $SSH_HOST to ~/.ssh/config (marked block, so sync uses it too)"
}

# private keys in ~/.ssh that have a matching .pub, one per line
list_keys() {
  local pub
  for pub in "$HOME"/.ssh/*.pub; do
    [ -f "$pub" ] && [ -f "${pub%.pub}" ] && printf '%s\n' "$(winpath "${pub%.pub}")"
  done
}

# describe_key <private key> -> "ed25519  user@laptop"
describe_key() {
  local t c
  t=$(awk '{print $1}' "$1.pub" 2>/dev/null | sed -e 's/^ssh-//' -e 's/^ecdsa-sha2-/ecdsa-/')
  c=$(awk '{ $1=""; $2=""; sub(/^  */, ""); print }' "$1.pub" 2>/dev/null)
  printf '%-10s %s' "$t" "${c:-<no comment>}"
}

create_key() {
  local def="$HOME/.ssh/id_ed25519" path pp
  [ -e "$def" ] && def="$HOME/.ssh/id_ed25519_$APP"
  path=$(normalize_local "$(ask "File for the new key" "$def")")
  if [ -e "$path" ]; then
    err "$path already exists — pick another name."; return 1
  fi
  info "A passphrase protects the key, but you would have to type it (or use ssh-agent) every time sync connects." >&2
  if confirm "Protect the key with a passphrase?" n; then
    ssh-keygen -t ed25519 -C "$USER@$(hostname)" -f "$path" >&2 || return 1
  else
    ssh-keygen -t ed25519 -N "" -C "$USER@$(hostname)" -f "$path" >/dev/null || return 1
  fi
  ok "Key created: $path" >&2
  printf '%s' "$path"
}

# choose_key -> prints the chosen private key path
choose_key() {
  local keys n i=0 k sel
  keys=$(list_keys)
  if [ -z "$keys" ]; then
    warn "You do not have an SSH key yet. You need one first to connect without a password." >&2
    confirm "Create a new SSH key now?" y || return 1
    create_key
    return
  fi
  n=$(printf '%s\n' "$keys" | wc -l | tr -d ' ')
  info "Your SSH keys:" >&2
  while IFS= read -r k; do
    i=$((i + 1))
    printf '   %2d) %-40s %s\n' "$i" "$k" "$(describe_key "$k")" >&2
  done <<< "$keys"
  printf '    n) create a new key\n' >&2
  while :; do
    sel=$(ask "Which key should be used for the server? (1-$n or n)" "1")
    case "$sel" in
      n|N) create_key; return ;;
      *[!0-9]*|"") warn "Enter a number from the list, or n." ;;
      *) if [ "$sel" -ge 1 ] && [ "$sel" -le "$n" ]; then
           printf '%s\n' "$keys" | sed -n "${sel}p"; return 0
         fi
         warn "Enter a number from the list, or n." ;;
    esac
  done
}

# setup_ssh [force]  — force: choose a key even if login already works
setup_ssh() {
  title "SSH connection"
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  if [ "${1:-}" != "force" ] && ssh_ok; then
    ok "Connected to $(remote) (port $SSH_PORT)${SSH_KEY:+ with $SSH_KEY}"
    return 0
  fi
  [ "${1:-}" != "force" ] && info "Password-less login to $(remote) is not set up yet (ssh-copy-id was not done)."

  local key
  key=$(choose_key | tail -n 1) || { err "No key chosen."; return 1; }
  [ -n "$key" ] && [ -f "$key" ] || { err "No key chosen."; return 1; }

  SSH_KEY="$key"
  save_config
  write_ssh_config

  if ssh_ok; then
    ok "This key already works on the server."
    return 0
  fi
  info "Installing $key.pub on the server."
  info "You will be asked for your server password ONE time."
  if command -v ssh-copy-id >/dev/null 2>&1; then
    ssh-copy-id -o StrictHostKeyChecking=accept-new -i "$key.pub" -p "$SSH_PORT" "$(remote)" || true
  else
    ssh -o StrictHostKeyChecking=accept-new -p "$SSH_PORT" "$(remote)" \
      'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys' < "$key.pub" || true
  fi
  if ssh_ok; then
    ok "Key login works"
  else
    err "Still cannot log in to $(remote) on port $SSH_PORT."
    err "Check the username / password / port, or ask the admin."
    return 1
  fi
}

# ----------------------------------------------------------------------------- mutagen helpers
daemon_start() { mutagen daemon start >/dev/null 2>&1 || true; }

# Sync guard (gui/syncguard.py): a folder here that lost its .simab-root (disk not mounted, folder replaced),
# a nearly empty side or a mass delete keeps the session paused. Exit 3 = kept paused.
guard_ok() {
  local out rc
  out=$(run_gui_cmd guard-check "$1" 2>/dev/null); rc=$?
  [ $rc -eq 0 ] && return 0
  [ $rc -eq 3 ] && { err "'$1' kept paused: $out"; return 1; }
  [ $rc -eq 2 ] && return 0
  warn "Sync guard not available (Python 3 or the gui folder missing): resuming '$1' without the check."
  return 0
}
# start the daemon; sessions whose folder here lost its marker are paused at once
guard_start() {
  daemon_running && return 0
  run_gui_cmd guard-start 2>/dev/null || daemon_start
}

daemon_running() {
  # `daemon start` is idempotent; we only want to know without starting it
  if [ $IS_WIN -eq 1 ]; then
    MSYS_NO_PATHCONV=1 tasklist.exe /FI "IMAGENAME eq mutagen.exe" /NH 2>/dev/null | grep -qi 'mutagen\.exe'
    return
  fi
  pgrep -u "$(id -u)" -f 'mutagen daemon run' >/dev/null 2>&1
}

# JSON array of all sync sessions on this machine — including ones created by
# hand with `mutagen sync create` (empty array on error / none)
sessions_json() {
  local out
  out=$(mutagen sync list --template '{{ json . }}' 2>/dev/null) || out="[]"
  [ -z "$out" ] && out="[]"
  if [ $IS_WIN -eq 1 ]; then      # Mutagen says C:\Users\me\x; normalize_local gives C:/Users/me/x
    printf '%s' "$out" | jq -c 'if type=="array" then map(if .alpha.path then .alpha.path |= gsub("\\\\"; "/") else . end
      | if .beta.path then .beta.path |= gsub("\\\\"; "/") else . end) else [] end' 2>/dev/null || printf '[]'
    return
  fi
  printf '%s' "$out" | jq -c 'if type=="array" then . else [] end' 2>/dev/null || printf '[]'
}

# one line per session: its name, or its identifier when it has no name
session_names() { sessions_json | jq -r '.[] | (.name // .identifier) | select(. != null and . != "")'; }

# status_style <paused> <status>  -> sets ST_COLOR and ST_TEXT
status_style() {
  if [ "$1" = "true" ]; then ST_COLOR="$C_Y"; ST_TEXT="paused"; return; fi
  case "$2" in
    watching)                    ST_COLOR="$C_G"; ST_TEXT="in sync" ;;
    scanning|waiting-for-rescan) ST_COLOR="$C_C"; ST_TEXT="scanning" ;;
    reconciling|staging-alpha|staging-beta|transitioning|saving)
                                 ST_COLOR="$C_C"; ST_TEXT="syncing" ;;
    connecting*)                 ST_COLOR="$C_Y"; ST_TEXT="connecting" ;;
    disconnected)                ST_COLOR="$C_R"; ST_TEXT="disconnected" ;;
    halted*)                     ST_COLOR="$C_R"; ST_TEXT="halted" ;;
    "")                          ST_COLOR="$C_DIM"; ST_TEXT="unknown" ;;
    *)                           ST_COLOR="$C_DIM"; ST_TEXT="$2" ;;
  esac
}

# dot <color> <text> <width>  ->  "● text" padded, colored dot
dot() { printf '%s●%s %s' "$1" "$C_0" "$(pad "$2" "$(( $3 - 2 ))")"; }

# shorten home folders to ~ (local $HOME, and /home/<user> on the server)
short_local()  { case "$1" in "$HOME") printf '~' ;; "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac; }
short_remote() { local h="/home/$SSH_USER"; case "$1" in "$h") printf '~' ;; "$h"/*) printf '~/%s' "${1#"$h"/}" ;; *) printf '%s' "$1" ;; esac; }

header() {
  local W inner left right gap
  W=$(term_width); inner=$((W - 4))
  left="$APP"; right="${SSH_USER:-?}@$SSH_HOST:$SSH_PORT"
  gap=$((inner - ${#left} - ${#VERSION} - ${#right} - 4))
  [ $gap -lt 1 ] && gap=1
  printf '\n %s╭%s╮%s\n' "$C_DIM" "$(repeat '─' "$inner")" "$C_0"
  printf ' %s│%s %s%s%s %sv%s%s%s%s %s│%s\n' "$C_DIM" "$C_0" \
    "$C_BOLD$C_C" "$left" "$C_0" "$C_DIM" "$VERSION" "$C_0" "$(repeat ' ' "$gap")" "$right" "$C_DIM" "$C_0"
  printf ' %s╰%s╯%s\n' "$C_DIM" "$(repeat '─' "$inner")" "$C_0"
}

show_status() {
  header
  local ssh_c ssh_t d_c d_t a_c a_t
  if [ -n "$SSH_USER" ] && ssh_ok; then ssh_c="$C_G"; ssh_t="connected"; else ssh_c="$C_R"; ssh_t="no connection"; fi
  if daemon_running; then d_c="$C_G"; d_t="running"; else d_c="$C_Y"; d_t="stopped"; fi
  if autostart_enabled; then a_c="$C_G"; a_t="on"; else a_c="$C_DIM"; a_t="off"; fi
  printf '  %sSSH%s %s   %sSYNC%s %s   %sAUTOSTART%s %s\n' \
    "$C_DIM" "$C_0" "$(dot "$ssh_c" "$ssh_t" 15)" \
    "$C_DIM" "$C_0" "$(dot "$d_c" "$d_t" 11)" \
    "$C_DIM" "$C_0" "$(dot "$a_c" "$a_t" 5)"

  local json count W rest wl wr
  if daemon_running; then json=$(sessions_json); else json="[]"; fi
  count=$(printf '%s' "$json" | jq 'length')
  title "PROJECTS ($count)"
  if ! daemon_running; then
    info "Sync is stopped — choose ${C_BOLD}Reconnect${C_0} to start it again."
    return
  fi
  if [ "$count" -eq 0 ]; then
    info "No projects yet — choose ${C_BOLD}Add project${C_0}."
    return
  fi

  # columns: "  # " 4 | NAME 16 | STATUS 17 | GIT 5 | LOCAL | SERVER
  W=$(term_width); rest=$((W - 4 - 16 - 17 - 5 - 4))
  wl=$((rest / 2)); wr=$((rest - wl))
  printf '  %s%s%s%s%s%s%s\n' "$C_DIM" "$(pad '#' 3)" "$(pad 'NAME' 16)" "$(pad 'STATUS' 17)" "$(pad 'GIT' 5)" "$(pad 'LOCAL' "$((wl + 1))")" "SERVER$C_0"

  local i=0 name paused st lpath rpath conf lerr vcs gitc
  while IFS=$'\t' read -r name paused st lpath rpath conf vcs lerr; do
    [ "$lerr" = "-" ] && lerr=""
    i=$((i + 1))
    status_style "$paused" "$st"
    case "$vcs" in ignore|1) vcs=off; gitc="$C_DIM" ;; *) vcs=on; gitc="$C_G" ;; esac
    printf '  %s%s%s%s%s%s%s %s\n' \
      "$C_C" "$(pad "$i" 3)" "$C_0" \
      "$C_BOLD$(pad "$name" 16)$C_0" \
      "$(dot "$ST_COLOR" "$ST_TEXT" 17)" \
      "$gitc$(pad "$vcs" 5)$C_0" \
      "$(lpad "$(short_local "$lpath")" "$wl")" \
      "$(lpad "$(short_remote "$rpath")" "$wr")"
    [ "$conf" != "0" ] && printf '     %s! %s conflict(s) — open Details%s\n' "$C_Y" "$conf" "$C_0"
    [ -n "$lerr" ] && printf '     %s! %s%s\n' "$C_R" "$(pad "$lerr" $((W - 8)))" "$C_0"
  done < <(printf '%s' "$json" | jq -r '.[] | [
      (.name // .identifier),
      ((.paused // false) | tostring),
      (.status // ""),
      (.alpha.path // ""),
      (.beta.path // ""),
      ((.conflicts // []) | length | tostring),
      ((.configuration.ignoreVCSMode // (if .ignore.vcs == true then "ignore" else null end) // "default") | tostring),
      ((.lastError // "") | gsub("[\t\n]"; " ") | if . == "" then "-" else . end)
    ] | @tsv')
}

# pick_project ["prompt"] -> prints session name (from number or name)
pick_project() {
  local prompt="${1:-Project number or name}" names n sel
  names=$(session_names)
  [ -z "$names" ] && { warn "No projects."; return 1; }
  n=$(printf '%s\n' "$names" | wc -l | tr -d ' ')
  sel=$(ask "$prompt (1-$n)")
  [ -z "$sel" ] && return 1
  case "$sel" in
    *[!0-9]*) printf '%s\n' "$names" | grep -qx -- "$sel" && { printf '%s' "$sel"; return 0; } ;;
    *) [ "$sel" -ge 1 ] && [ "$sel" -le "$n" ] && { printf '%s\n' "$names" | sed -n "${sel}p"; return 0; } ;;
  esac
  warn "No such project: $sel"
  return 1
}

sanitize_name() {
  local s
  s=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed -e 's/--*/-/g' -e 's/^-//' -e 's/-$//')
  case "$s" in [0-9]*) s="p-$s" ;; esac
  [ -z "$s" ] && s="project"
  printf '%s' "$s"
}

# ----------------------------------------------------------------------------- actions
# add_local_project [FOLDER] [--remote PATH] [--name NAME] [--git on|off] [--yes]
#   The reverse of add_project: the project exists on THIS machine; it is copied
#   to the server by the first sync, then both sides stay in sync.
add_local_project() {
  title "Upload a project from this machine"
  local lpath="" rpath="" name="" git="" yes=0 nowait=0 ign_given=0 ign_list="" paused=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --remote) rpath="${2:-}"; shift 2 ;;
      --ignores) ign_given=1; ign_list="${2:-}"; shift 2 ;;
      --name)   name="${2:-}"; shift 2 ;;
      --git)    git="${2:-}"; shift 2 ;;
      --yes|-y) yes=1; shift ;;
      --no-wait) nowait=1; shift ;;
      --paused) paused=1; shift ;;
      *)        lpath="$1"; shift ;;
    esac
  done
  ssh_ok || { err "No SSH connection — run Setup first."; return 1; }
  daemon_start

  [ -z "$lpath" ] && lpath=$(ask "Project folder on THIS machine" "$PWD")
  lpath=$(normalize_local "$lpath")
  [ -d "$lpath" ] || { err "Folder does not exist: $lpath"; return 1; }
  [ -n "$(ls -A "$lpath" 2>/dev/null)" ] || { err "Folder is empty: $lpath"; return 1; }
  if [ $IS_WSL -eq 1 ]; then
    case "$lpath" in /mnt/*) warn "$lpath is on the Windows drive: slow, and file watching does not work well." ;; esac
  fi

  local json
  json=$(sessions_json)
  if printf '%s' "$json" | jq -e --arg p "$lpath" 'any(.[]; .alpha.path == $p or .beta.path == $p)' >/dev/null; then
    err "This folder is already synced."; return 1
  fi

  local rhome
  rhome=$(rssh 'printf %s "$HOME"') || { err "Could not read your home folder on the server."; return 1; }
  [ -z "$name" ] && name=$(sanitize_name "$(basename "$lpath")") && [ $yes -eq 0 ] && name=$(sanitize_name "$(ask "Project name (letters, digits, -)" "$name")")
  name=$(sanitize_name "$name")
  if session_names | grep -qx -- "$name"; then err "A project named '$name' already exists."; return 1; fi

  [ -z "$rpath" ] && { rpath="$rhome/$(basename "$lpath")"; [ $yes -eq 0 ] && rpath=$(ask "Folder on the server" "$rpath"); }
  case "$rpath" in "~") rpath="$rhome" ;; "~/"*) rpath="$rhome/${rpath#\~/}" ;; /*) ;; *) rpath="$rhome/$rpath" ;; esac
  rpath="${rpath%/}"
  if printf '%s' "$json" | jq -e --arg p "$rpath" 'any(.[]; .beta.path == $p or .alpha.path == $p)' >/dev/null; then
    err "That server folder is already synced."; return 1
  fi

  local q state
  q=$(printf '%q' "$rpath")
  state=$(rssh "if [ ! -e $q ]; then echo missing; elif [ -d $q ] && [ -z \"\$(ls -A $q 2>/dev/null)\" ]; then echo empty; elif [ -d $q ]; then echo full; else echo file; fi") \
    || { err "Could not check $rpath on the server."; return 1; }
  case "$state" in
    file) err "$rpath exists on the server and is not a folder."; return 1 ;;
    full)
      warn "$rpath already has files on the server. They will be MERGED with this folder (both directions);"
      warn "files that differ on both sides become conflicts (nothing is overwritten)."
      if [ $yes -eq 1 ]; then err "Not merging automatically — pick an empty or new server folder."; return 1; fi
      confirm "Merge anyway?" n || return 1 ;;
    missing)
      rssh "mkdir -p $q" || { err "Could not create $rpath on the server."; return 1; }
      ok "Created $rpath on the server" ;;
  esac

  local ignores="$DEFAULT_IGNORES" extra
  [ $ign_given -eq 1 ] && ignores="$ign_list"
  info "Not uploaded: ${ignores:-nothing}"
  if [ $yes -eq 0 ] && [ $ign_given -eq 0 ]; then
    extra=$(ask "Extra folders/patterns to skip, comma-separated (empty = none)" "")
    [ -n "$extra" ] && ignores="$ignores,$(printf '%s' "$extra" | tr -d ' ')"
  fi
  if [ -z "$git" ]; then
    git=on
    if [ $yes -eq 0 ]; then git_warning; confirm "Sync git folders (.git) too?" y || git=off; fi
  fi
  case "$git" in on|off) ;; *) err "--git must be on or off"; return 1 ;; esac

  local url="$SSH_USER@$SSH_HOST:$SSH_PORT:$rpath"
  info "Creating sync:  $lpath  ⇄  $url   (git: $git)"
  create_session "$name" "$lpath" "$url" "$ignores" "$git" "$paused" || { err "Could not create the sync (see above)."; return 1; }
  ok "Project '$name' added."
  [ $nowait -eq 1 ] && { info "Uploading in the background."; return 0; }
  info "Uploading to the server — big projects take a few minutes…"
  wait_sync "$name" && ok "Upload finished. From now on changes sync in real time (both ways)."
  return 0
}

# Add project: ask where the project is now
add_project_menu() {
  title "Add project"
  printf '   1) It is on the server   (download it to this machine, then sync)\n'
  printf '   2) It is on this machine (upload it to the server, then sync)\n'
  local c
  c=$(ask "Where is the project now? (1/2)" "1")
  case "$c" in
    2) add_local_project ;;
    *) add_project ;;
  esac
}

# add_project [SERVER_FOLDER] [--local PATH] [--name NAME] [--git on|off] [--yes] [--no-wait] [--merge]
#   --merge: the local folder may already have files; they are merged (differences become conflicts).
#   Download a project from the server to this machine, then sync it.
#   Without arguments it asks (folder list, local folder, …); --yes never asks.
add_project() {
  title "Add project"
  local rpath="" lpath="" name="" git="" yes=0 nowait=0 ign_given=0 ign_list="" paused=0 merge=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --merge)   merge=1; shift ;;
      --local)   lpath="${2:-}"; shift 2 ;;
      --ignores) ign_given=1; ign_list="${2:-}"; shift 2 ;;
      --name)    name="${2:-}"; shift 2 ;;
      --git)     git="${2:-}"; shift 2 ;;
      --yes|-y)  yes=1; shift ;;
      --no-wait) nowait=1; shift ;;
      --paused) paused=1; shift ;;
      *)         rpath="$1"; shift ;;
    esac
  done
  ssh_ok || { err "No SSH connection — run Setup first."; return 1; }
  daemon_start

  local rhome
  rhome=$(rssh 'printf %s "$HOME"') || { err "Could not read your home folder on the server."; return 1; }

  if [ -z "$rpath" ]; then
    [ $yes -eq 1 ] && { err "Give the server folder."; return 1; }
    info "Folders in your home on the server ($rhome):"
    local dirs i=0 d sel
    # through sh: a zsh login shell on the server stops at a glob that matches nothing
    local list_cmd='for d in "$HOME"/*/; do [ -d "$d" ] && printf "%s\n" "${d%/}"; done'
    dirs=$(rssh "sh -c '$list_cmd'" 2>/dev/null || true)
    if [ -n "$dirs" ]; then
      while IFS= read -r d; do i=$((i + 1)); printf '   %2d) %s\n' "$i" "$d"; done <<< "$dirs"
    else
      info "   (none)"
    fi
    sel=$(ask "Project on the server: number from the list, or a path (e.g. ~/myproject)")
    [ -z "$sel" ] && { warn "Cancelled."; return 1; }
    case "$sel" in
      *[!0-9]*) rpath="$sel" ;;
      *) rpath=$(printf '%s\n' "$dirs" | sed -n "${sel}p") ;;
    esac
    [ -z "$rpath" ] && { warn "Invalid choice."; return 1; }
  fi
  case "$rpath" in
    "~")   rpath="$rhome" ;;
    "~/"*) rpath="$rhome/${rpath#\~/}" ;;
    /*)    ;;
    *)     rpath="$rhome/$rpath" ;;
  esac
  rpath="${rpath%/}"
  if ! rssh "test -d $(printf '%q' "$rpath") && test -r $(printf '%q' "$rpath")"; then
    err "Folder does not exist (or is not readable) on the server: $rpath"
    return 1
  fi

  local json
  json=$(sessions_json)
  if printf '%s' "$json" | jq -e --arg p "$rpath" 'any(.[]; .beta.path == $p or .alpha.path == $p)' >/dev/null; then
    err "This server folder is already synced:"
    printf '%s' "$json" | jq -r --arg p "$rpath" '.[] | select(.beta.path == $p or .alpha.path == $p) | "   \(.name) → \(.alpha.path)"' >&2
    return 1
  fi

  [ -z "$name" ] && { name=$(sanitize_name "$(basename "$rpath")"); [ $yes -eq 0 ] && name=$(ask "Project name (letters, digits, -)" "$name"); }
  name=$(sanitize_name "$name")
  if session_names | grep -qx -- "$name"; then
    err "A project named '$name' already exists."
    return 1
  fi

  [ -z "$lpath" ] && { lpath="$HOME/$(basename "$rpath")"; [ $yes -eq 0 ] && lpath=$(ask "Where should it live on THIS machine" "$lpath"); }
  lpath=$(normalize_local "$lpath")
  if [ $IS_WSL -eq 1 ]; then
    case "$lpath" in /mnt/*)
      warn "$lpath is on the Windows drive: slow, and dev servers will not see changes."
      [ $yes -eq 1 ] || confirm "Use it anyway?" n || return 1 ;;
    esac
  fi
  if printf '%s' "$json" | jq -e --arg p "$lpath" 'any(.[]; .alpha.path == $p or .beta.path == $p)' >/dev/null; then
    err "This local folder is already used by another project."
    return 1
  fi
  if [ -d "$lpath" ] && [ -n "$(ls -A "$lpath" 2>/dev/null)" ]; then
    warn "$lpath is not empty. Its files will be MERGED with the server copy (both directions)."
    warn "Files that differ on both sides become conflicts (nothing is overwritten)."
    if [ $yes -eq 1 ] && [ $merge -eq 0 ]; then err "Not merging automatically — pick an empty or new local folder."; return 1; fi
    [ $yes -eq 1 ] || confirm "Continue?" n || return 1
  fi
  # never make the parents: a missing /media/…/DISK2 means the disk is not mounted, not "create it"
  if [ ! -d "$(dirname "$lpath")" ]; then
    err "$(dirname "$lpath") does not exist (a disk not mounted?). Create it or mount the disk, then try again."
    return 1
  fi
  mkdir "$lpath" 2>/dev/null || [ -d "$lpath" ] || { err "Cannot create $lpath"; return 1; }

  local ignores="$DEFAULT_IGNORES" extra
  [ $ign_given -eq 1 ] && ignores="$ign_list"
  info "Not synced: ${ignores:-nothing}"
  if [ $yes -eq 0 ] && [ $ign_given -eq 0 ]; then
    extra=$(ask "Extra folders/patterns to skip, comma-separated (empty = none)" "")
    [ -n "$extra" ] && ignores="$ignores,$(printf '%s' "$extra" | tr -d ' ')"
  fi
  if [ -z "$git" ]; then
    git=on
    if [ $yes -eq 0 ]; then git_warning; confirm "Sync git folders (.git) too?" y || git=off; fi
  fi
  case "$git" in on|off) ;; *) err "--git must be on or off"; return 1 ;; esac

  local url="$SSH_USER@$SSH_HOST:$SSH_PORT:$rpath"
  info "Creating sync:  $lpath  ⇄  $url   (git: $git)"
  if ! create_session "$name" "$lpath" "$url" "$ignores" "$git" "$paused"; then
    err "Could not create the sync (see the message above)."
    return 1
  fi
  ok "Project '$name' added."
  [ $nowait -eq 1 ] && { info "Downloading in the background."; return 0; }
  info "Initial sync (downloading the project) — big projects take a few minutes…"
  wait_sync "$name" && ok "Initial sync finished. From now on changes sync in real time."
  return 0
}

# hsize <bytes> -> "12.3 MB"
hsize() { awk -v b="${1:-0}" 'BEGIN{ split("B KB MB GB TB",u," "); i=1; while (b>=1024 && i<5) { b/=1024; i++ }; printf (i==1 ? "%d %s" : "%.1f %s"), b, u[i] }'; }

# hms <seconds> -> "01:23" / "1:02:03"
hms() { local t="$1"; if [ "$t" -ge 3600 ]; then printf '%d:%02d:%02d' $((t/3600)) $((t%3600/60)) $((t%60)); else printf '%02d:%02d' $((t/60)) $((t%60)); fi; }

# wait_sync <name> [flush]  — live progress line until a full sync cycle is done.
#   flush: force a new cycle first (for "sync now"); otherwise wait for the first one.
#   Ctrl+C stops waiting only — syncing continues in the background.
#   returns 0 done, 1 error, 2 stopped waiting
wait_sync() {
  local name="$1" fpid="" start el s st paused cycles rf ef rs es lerr line pct rc=0 stop=0
  if [ "${2:-}" = "flush" ]; then
    mutagen sync flush "$name" >/dev/null 2>&1 &
    fpid=$!
  fi
  start=$(date +%s)
  trap 'stop=1' INT
  printf '  %sCtrl+C (or closing this window/tab) = keep syncing in the background%s\n' "$C_DIM" "$C_0"
  while :; do
    s=$(sessions_json | jq -c --arg n "$name" 'first(.[] | select((.name // .identifier) == $n)) // empty')
    if [ -z "$s" ]; then printf '\n'; err "Project '$name' not found."; rc=1; break; fi
    IFS=$'\t' read -r st paused cycles rf ef rs es lerr <<< "$(printf '%s' "$s" | jq -r '
      ([.. | objects | select(has("expectedFiles"))][0]) as $p
      | [ ((.status // "") | if . == "" then "unknown" else . end), ((.paused // false) | tostring), ((.successfulCycles // 0) | tostring),
          (($p.receivedFiles // 0) | tostring), (($p.expectedFiles // 0) | tostring),
          (($p.receivedSize // 0) | tostring), (($p.expectedSize // 0) | tostring),
          ((.lastError // "") | gsub("[\t\n]"; " ")) ] | @tsv')"
    el=$(( $(date +%s) - start ))
    status_style "$paused" "$st"
    line="$ST_TEXT"
    if [ "${ef:-0}" -gt 0 ] 2>/dev/null; then
      pct=$(( rf * 100 / ef ))
      line="$line   $rf/$ef files  $pct%"
      [ "${es:-0}" -gt 0 ] 2>/dev/null && line="$line   $(hsize "$rs") / $(hsize "$es")"
    fi
    [ -n "$lerr" ] && [ "$st" != "watching" ] && line="$line   ${C_R}$(pad "$lerr" 40)${C_0}"
    printf '\r\033[K  %s●%s %s   %s%s%s' "$ST_COLOR" "$C_0" "$line" "$C_DIM" "$(hms "$el")" "$C_0"

    if [ "$paused" = "true" ]; then printf '\n'; warn "Project is paused — resume it to sync."; rc=1; break; fi
    case "$st" in halted*) printf '\n'; err "Sync halted: ${lerr:-see Details}"; rc=1; break ;; esac
    if [ -n "$fpid" ]; then
      if ! kill -0 "$fpid" 2>/dev/null; then
        wait "$fpid"; rc=$?; printf '\n'; break
      fi
    elif [ "$st" = "watching" ] && [ "${cycles:-0}" -gt 0 ] 2>/dev/null; then
      printf '\n'; rc=0; break
    fi
    if [ $stop -eq 1 ]; then
      printf '\n'; info "Stopped waiting — '$name' keeps syncing in the background (status is in the menu)."
      rc=2; break
    fi
    sleep 1
  done
  trap - INT
  [ -n "$fpid" ] && kill "$fpid" 2>/dev/null
  return $rc
}

project_action() {
  # project_action <pause|resume|flush|remove|info|monitor> [name]
  local act="$1" name="${2:-}"
  daemon_running || daemon_start
  if [ -z "$name" ]; then
    name=$(pick_project) || return 1
  elif ! session_names | grep -qx -- "$name"; then
    err "No such project: $name"; return 1
  fi
  case "$act" in
    pause)   mutagen sync pause "$name"  && ok "'$name' paused (nothing syncs until you resume)." ;;
    resume)  guard_ok "$name" && mutagen sync resume "$name" && ok "'$name' resumed." ;;
    flush)   info "Syncing '$name' now…"; wait_sync "$name" flush && ok "'$name' is in sync." ;;
    info)    mutagen sync list --long "$name" ;;
    monitor) info "Live view — Ctrl+C to go back"; mutagen sync monitor "$name" || true ;;
    remove)
      warn "Stop syncing '$name'? Files stay on BOTH sides; they just stop syncing."
      confirm "Remove '$name' from sync?" n || return 1
      mutagen sync terminate "$name" && ok "'$name' is no longer synced."
      ;;
  esac
}

# create_session <name> <local path> <remote URL user@host:port:path> <ignores csv> [git on|off] [paused 0|1]
#   paused 1: created paused — the window copies the files first (parallel tar over ssh), then resumes it
#   git on  (default): .git folders are synced too
#   git off          : .git / .svn / .hg are skipped (--ignore-vcs)
#   ignores ""       : nothing is ignored
#   winner local|server: one first round where that side wins every conflict (two-way-resolved: alpha wins,
#     so the winner is made alpha); callers recreate it two-way-safe once it is in sync (ignore_set --winner)
create_session() {
  local vcs="--no-ignore-vcs" ign=() mode=two-way-safe a="$2" b="$3"
  [ "${5:-on}" = "off" ] && vcs="--ignore-vcs"
  [ -n "$4" ] && ign=(--ignore "$4")
  [ "${6:-0}" = "1" ] && ign+=(--paused)   # not an ignore — rides along in the same array
  case "${7:-}" in
    local)  mode=two-way-resolved ;;
    server) mode=two-way-resolved; a="$3"; b="$2" ;;
  esac
  mutagen sync create \
    --name "$1" \
    --label "$LABEL" \
    --sync-mode "$mode" \
    "$vcs" \
    ${ign[@]+"${ign[@]}"} \
    "$a" "$b"
}

# session_git <session json> -> "on" | "off"
#   Mutagen's default (no flag) syncs VCS folders; only "ignore" skips them.
session_git() {
  case "$(printf '%s' "$1" | jq -r '(.configuration.ignoreVCSMode // (if .ignore.vcs == true then "ignore" else null end) // "default") | tostring')" in
    ignore|1) printf 'off' ;;
    *)        printf 'on' ;;
  esac
}

git_warning() {
  info "Syncing .git keeps commits/branches identical on both sides."
  warn "Do not run git commands (commit, pull, checkout…) on the server and on this"
  warn "machine at the same moment — wait until the sync shows “in sync” first."
}

# normalize_local <path> -> absolute path, no trailing slash
normalize_local() {
  local p="$1"
  case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#\~/}" ;; esac
  if [ $IS_WIN -eq 1 ]; then
    case "$p" in /*|[A-Za-z]:*) ;; *) p="$PWD/$p" ;; esac
    p=$(winpath "$p")
    case "$p" in [A-Za-z]:/) printf '%s' "$p"; return ;; esac
    printf '%s' "${p%/}"; return
  fi
  case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
  printf '%s' "${p%/}"
}

# normalize_remote <path> <remote home> -> absolute path, no trailing slash
normalize_remote() {
  local p="$1" h="$2"
  case "$p" in "~") p="$h" ;; "~/"*) p="$h/${p#\~/}" ;; /*) ;; *) p="$h/$p" ;; esac
  printf '%s' "${p%/}"
}

# Change the local and/or server folder of a project. Mutagen cannot edit a
# session's endpoints, so: sync → terminate → move folder(s) → recreate with
# the same name and ignores. Works for sessions created by hand, too.
move_project() {
  title "Change project folders"
  ssh_ok || { err "No SSH connection — run Setup first."; return 1; }
  daemon_running || daemon_start
  local name="" yes=0 nowait=0 arg_l="" arg_r="" merge=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --merge-server) merge=1; shift ;;   # the new server folder already has the same files (own copy / back)
      --local)   arg_l="${2:-}"; shift 2 ;;
      --remote)  arg_r="${2:-}"; shift 2 ;;
      --yes|-y)  yes=1; shift ;;
      --no-wait) nowait=1; shift ;;
      *)         name="$1"; shift ;;
    esac
  done
  if [ -z "$name" ]; then
    [ $yes -eq 1 ] && { err "Give the project name."; return 1; }
    name=$(pick_project "Project to move") || return 1
  fi
  local s
  s=$(sessions_json | jq -c --arg n "$name" 'first(.[] | select((.name // .identifier) == $n)) // empty')
  [ -z "$s" ] && { err "No such project: $name"; return 1; }

  # which endpoint is this machine? (a hand-made session may have them reversed)
  local lside=alpha rside=beta
  if [ -n "$(printf '%s' "$s" | jq -r '.alpha.host // empty')" ] && [ -z "$(printf '%s' "$s" | jq -r '.beta.host // empty')" ]; then
    lside=beta; rside=alpha
  fi
  local old_l old_r r_user r_host r_port ignores
  old_l=$(printf '%s' "$s" | jq -r ".$lside.path // empty")
  old_r=$(printf '%s' "$s" | jq -r ".$rside.path // empty")
  r_user=$(printf '%s' "$s" | jq -r ".$rside.user // empty"); r_user="${r_user:-$SSH_USER}"
  r_host=$(printf '%s' "$s" | jq -r ".$rside.host // empty"); r_host="${r_host:-$SSH_HOST}"
  r_port=$(printf '%s' "$s" | jq -r ".$rside.port // 0")
  ignores=$(printf '%s' "$s" | jq -r '(.configuration.ignores // .ignore.paths // []) | join(",")')
  local git; git=$(session_git "$s")
  if [ "$r_host" != "$SSH_HOST" ] || [ "$r_user" != "$SSH_USER" ]; then
    err "'$name' syncs with $r_user@$r_host, not with your configured server ($SSH_USER@$SSH_HOST)."
    err "Change Settings to that user/server first."
    return 1
  fi
  [ "$r_port" = "0" ] && r_port="$SSH_PORT"

  local new_name rhome
  new_name=$(sanitize_name "$name")
  case "$name" in sync_*) new_name=$(sanitize_name "$(basename "$old_r")") ;; esac
  rhome=$(rssh 'printf %s "$HOME"') || { err "Could not read your home folder on the server."; return 1; }

  printf '  %-8s %s\n' "local:" "$old_l"
  printf '  %-8s %s\n' "server:" "$old_r"
  info "Press Enter to keep a folder as it is."

  local new_l new_r
  if [ $yes -eq 1 ]; then
    new_l=$(normalize_local "${arg_l:-$old_l}")
    new_r=$(normalize_remote "${arg_r:-$old_r}" "$rhome")
  else
    new_l=$(normalize_local "$(ask "New local folder" "${arg_l:-$old_l}")")
    new_r=$(normalize_remote "$(ask "New server folder" "${arg_r:-$old_r}")" "$rhome")
  fi
  if [ "$new_l" = "$old_l" ] && [ "$new_r" = "$old_r" ]; then
    info "Nothing changed."
    return 0
  fi

  # ---- plan the local side
  local lmode=keep
  if [ "$new_l" != "$old_l" ]; then
    case "$new_l/" in "$old_l"/*) err "The new local folder cannot be inside the old one."; return 1 ;; esac
    if [ $IS_WSL -eq 1 ]; then
      case "$new_l" in /mnt/*)
        warn "$new_l is on the Windows drive: slow, and dev servers will not see changes."
        [ $yes -eq 1 ] || confirm "Use it anyway?" n || return 1 ;;
      esac
    fi
    if [ -e "$new_l" ] && [ ! -d "$new_l" ]; then
      err "$new_l exists and is not a folder."; return 1
    elif [ -d "$new_l" ] && [ -n "$(ls -A "$new_l" 2>/dev/null)" ]; then
      warn "$new_l is not empty — it will be used as is and MERGED with the server copy."
      [ $yes -eq 1 ] && { err "$new_l is not empty — pick a new or empty folder."; return 1; }
      confirm "Use that folder (the old local folder is left untouched)?" n || return 1
      lmode=use
    elif [ -d "$old_l" ]; then
      lmode=move
    else
      lmode=use
    fi
  fi

  # ---- plan the server side
  local rmode=keep q_old q_new
  q_old=$(printf '%q' "$old_r"); q_new=$(printf '%q' "$new_r")
  if [ "$new_r" != "$old_r" ]; then
    case "$new_r/" in "$old_r"/*) err "The new server folder cannot be inside the old one."; return 1 ;; esac
    if rssh "test -d $q_new && [ -n \"\$(ls -A $q_new 2>/dev/null)\" ]"; then
      warn "$new_r already has files on the server — it will be used as is and MERGED."
      [ $yes -eq 1 ] && [ $merge -eq 0 ] && { err "$new_r already has files on the server — pick a new or empty folder."; return 1; }
      [ $merge -eq 1 ] || confirm "Use that server folder (the old one is left untouched)?" n || return 1
      rmode=use
    elif rssh "test -e $q_new && ! test -d $q_new"; then
      err "$new_r exists on the server and is not a folder."; return 1
    else
      rmode=move
    fi
  fi

  title "Plan for '$name'"
  case "$lmode" in
    move) info "local : move  $old_l  →  $new_l" ;;
    use)  info "local : sync with existing $new_l" ;;
    keep) info "local : unchanged ($old_l)" ;;
  esac
  case "$rmode" in
    move) info "server: move  $old_r  →  $new_r" ;;
    use)  info "server: sync with existing $new_r" ;;
    keep) info "server: unchanged ($old_r)" ;;
  esac
  [ "$new_name" != "$name" ] && info "name  : $name → $new_name"
  warn "Stop anything running from this project first (e.g. its docker compose):"
  warn "containers keep pointing at the old folder."
  [ $yes -eq 1 ] || confirm "Go ahead?" n || return 1

  info "Syncing pending changes first…"
  if ! wait_sync "$name" flush; then
    warn "Could not finish a sync cycle — un-synced changes stay in their folders and merge later."
    [ $yes -eq 1 ] || confirm "Continue anyway?" n || return 1
  fi
  mutagen sync terminate "$name" || { err "Could not stop the old sync."; return 1; }

  local old_url new_url
  old_url="$r_user@$r_host:$r_port:$old_r"
  new_url="$r_user@$r_host:$r_port:$new_r"

  # server first (more likely to fail on permissions), then local; roll back on error
  if [ "$rmode" = "move" ]; then
    if ! rssh "mkdir -p \"\$(dirname $q_new)\" && { [ ! -d $q_new ] || rmdir $q_new; } && mv $q_old $q_new"; then
      err "Could not move the folder on the server. Restoring the old sync."
      create_session "$new_name" "$old_l" "$old_url" "$ignores" "$git" >/dev/null
      return 1
    fi
    ok "server folder moved"
  fi
  if [ "$lmode" = "move" ]; then
    if ! { mkdir -p "$(dirname "$new_l")" && { [ ! -d "$new_l" ] || rmdir "$new_l"; } && mv "$old_l" "$new_l"; }; then
      err "Could not move the local folder."
      if [ "$rmode" = "move" ]; then
        rssh "mv $q_new $q_old" && info "server folder moved back"
      fi
      create_session "$new_name" "$old_l" "$old_url" "$ignores" "$git" >/dev/null && info "old sync restored"
      return 1
    fi
    ok "local folder moved"
  else
    mkdir -p "$new_l"
  fi

  if ! create_session "$new_name" "$new_l" "$new_url" "$ignores" "$git"; then
    err "Folders are moved but the new sync could not be created."
    err "Try:  mutagen sync create --name $new_name $new_l $new_url"
    return 1
  fi
  ok "'$new_name' now syncs  $new_l  ⇄  $new_r"
  [ $nowait -eq 1 ] && return 0
  wait_sync "$new_name" && ok "In sync."
}

# git_toggle [name] [on|off] — turn syncing of .git folders on/off for a project.
# Mutagen cannot change this on a live session, so the session is recreated
# with the same name, folders and ignores (files are not moved or deleted).
git_toggle() {
  title "Git sync"
  daemon_running || daemon_start
  local name="${1:-}" want="${2:-}"
  [ -z "$name" ] && { name=$(pick_project "Project") || return 1; }
  local s
  s=$(sessions_json | jq -c --arg n "$name" 'first(.[] | select((.name // .identifier) == $n)) // empty')
  [ -z "$s" ] && { err "No such project: $name"; return 1; }

  local lside=alpha rside=beta
  if [ -n "$(printf '%s' "$s" | jq -r '.alpha.host // empty')" ] && [ -z "$(printf '%s' "$s" | jq -r '.beta.host // empty')" ]; then
    lside=beta; rside=alpha
  fi
  local cur lpath rpath r_user r_host r_port ignores url new_name
  cur=$(session_git "$s")
  lpath=$(printf '%s' "$s" | jq -r ".$lside.path // empty")
  rpath=$(printf '%s' "$s" | jq -r ".$rside.path // empty")
  r_user=$(printf '%s' "$s" | jq -r ".$rside.user // empty"); r_user="${r_user:-$SSH_USER}"
  r_host=$(printf '%s' "$s" | jq -r ".$rside.host // empty"); r_host="${r_host:-$SSH_HOST}"
  r_port=$(printf '%s' "$s" | jq -r ".$rside.port // 0"); [ "$r_port" = "0" ] && r_port="$SSH_PORT"
  ignores=$(printf '%s' "$s" | jq -r '(.configuration.ignores // .ignore.paths // []) | join(",")')
  url="$r_user@$r_host:$r_port:$rpath"
  new_name=$(sanitize_name "$name")
  case "$name" in sync_*) new_name=$(sanitize_name "$(basename "$rpath")") ;; esac

  info "'$name' — git sync is currently ${C_BOLD}$cur${C_0}"
  if [ -z "$want" ]; then
    if [ "$cur" = "on" ]; then want=off; else want=on; fi
    confirm "Turn git sync $want?" y || return 1
  fi
  case "$want" in on|off) ;; *) err "Use on or off."; return 1 ;; esac
  [ "$want" = "$cur" ] && { ok "Already $cur."; return 0; }
  [ "$want" = "on" ] && git_warning

  info "Syncing pending changes first…"
  wait_sync "$name" flush || { confirm "Continue anyway?" n || return 1; }
  mutagen sync terminate "$name" >/dev/null || { err "Could not stop the old sync."; return 1; }
  if ! create_session "$new_name" "$lpath" "$url" "$ignores" "$want" >/dev/null; then
    err "Could not recreate the sync. Restoring the old setting."
    create_session "$new_name" "$lpath" "$url" "$ignores" "$cur" >/dev/null
    return 1
  fi
  ok "'$new_name': git sync $want"
  wait_sync "$new_name" && ok "In sync."
}

# ignore_set [name] [list|--default] [--winner local|server] [--force] — what is never synced for a project
# (comma list of Mutagen ignore patterns: "node_modules" anywhere, "/dist" only at the top, "*.log"…).
# Mutagen cannot change this on a live session, so it is recreated with the same name,
# folders and git setting. Files already copied stay where they are.
# A new session has no history: every file that differs on the two sides is a conflict (two-way-safe).
#   --winner local|server: the first round lets that side win them all (two-way-resolved), then the
#                          session is recreated two-way-safe (the sides are equal then: no conflicts)
#   --force              : recreate even when the list is the same (a winner round, back to two-way-safe)
ignore_set() {
  title "Not synced"
  daemon_running || daemon_start
  local name="${1:-}" want="${2-__ask__}" winner="" force=0
  shift 2 2>/dev/null || shift $#
  while [ $# -gt 0 ]; do
    case "$1" in
      --winner) winner="${2:-}"; shift ;;
      --force)  force=1 ;;
    esac
    shift
  done
  case "$winner" in ""|local|server) ;; *) err "--winner: local or server"; return 1 ;; esac
  [ -z "$name" ] && { name=$(pick_project "Project") || return 1; }
  local s
  s=$(sessions_json | jq -c --arg n "$name" 'first(.[] | select((.name // .identifier) == $n)) // empty')
  [ -z "$s" ] && { err "No such project: $name"; return 1; }

  local lside=alpha rside=beta
  if [ -n "$(printf '%s' "$s" | jq -r '.alpha.host // empty')" ] && [ -z "$(printf '%s' "$s" | jq -r '.beta.host // empty')" ]; then
    lside=beta; rside=alpha
  fi
  local cur git lpath rpath r_user r_host r_port url new_name
  cur=$(printf '%s' "$s" | jq -r '(.configuration.ignores // .ignore.paths // []) | join(",")')
  git=$(session_git "$s")
  lpath=$(printf '%s' "$s" | jq -r ".$lside.path // empty")
  rpath=$(printf '%s' "$s" | jq -r ".$rside.path // empty")
  r_user=$(printf '%s' "$s" | jq -r ".$rside.user // empty"); r_user="${r_user:-$SSH_USER}"
  r_host=$(printf '%s' "$s" | jq -r ".$rside.host // empty"); r_host="${r_host:-$SSH_HOST}"
  r_port=$(printf '%s' "$s" | jq -r ".$rside.port // 0"); [ "$r_port" = "0" ] && r_port="$SSH_PORT"
  url="$r_user@$r_host:$r_port:$rpath"
  new_name=$(sanitize_name "$name")
  case "$name" in sync_*) new_name=$(sanitize_name "$(basename "$rpath")") ;; esac

  info "'$name' — not synced now: ${cur:-(nothing)}"
  if [ "$want" = "__ask__" ]; then
    want=$(ask "New list (comma separated, - for the defaults)" "${cur:-$DEFAULT_IGNORES}")
  fi
  case "$want" in --default|-) want="$DEFAULT_IGNORES" ;; esac
  # tidy: split on commas/new lines, trim, drop empty and repeated entries, keep the order
  want=$(printf '%s' "$want" | tr '\n' ',' | awk -v RS=',' '{ gsub(/^[ \t\r]+|[ \t\r]+$/, ""); if ($0 != "" && !seen[$0]++) printf "%s%s", (n++ ? "," : ""), $0 }')
  [ "$want" = "$cur" ] && [ -z "$winner" ] && [ $force -eq 0 ] && { ok "No change."; return 0; }

  info "Syncing pending changes first…"
  wait_sync "$name" flush || { confirm "Continue anyway?" n || return 1; }
  mutagen sync terminate "$name" >/dev/null || { err "Could not stop the old sync."; return 1; }
  if ! create_session "$new_name" "$lpath" "$url" "$want" "$git" 0 "$winner" >/dev/null; then
    err "Could not recreate the sync (check the list). Restoring the old one."
    create_session "$new_name" "$lpath" "$url" "$cur" "$git" >/dev/null
    return 1
  fi
  if [ -n "$winner" ]; then
    info "First round: $( [ "$winner" = local ] && echo "this computer's" || echo "the server's") version wins every conflict…"
    if ! wait_sync "$new_name"; then
      warn "The first round did not finish: '$new_name' stays two-way-resolved — run: simabworkspace ignore $new_name '$want' --force"
      return 1
    fi
    mutagen sync terminate "$new_name" >/dev/null || { err "Could not stop the first round."; return 1; }
    create_session "$new_name" "$lpath" "$url" "$want" "$git" >/dev/null || { err "Could not recreate the sync two-way-safe."; return 1; }
    ok "Both sides equal — back to two-way-safe."
  fi
  ok "'$new_name': not synced: ${want:-(nothing)}"
  info "Copies made before stay on both sides — delete them by hand where they are not needed."
}

all_action() {
  local act="$1" names n
  daemon_running || daemon_start
  names=$(session_names)
  [ -z "$names" ] && { info "No projects."; return 0; }
  while IFS= read -r n; do
    case "$act" in
      pause)  mutagen sync pause "$n"  >/dev/null && ok "paused  $n" ;;
      resume) guard_ok "$n" && mutagen sync resume "$n" >/dev/null && ok "resumed $n" ;;
    esac
  done <<< "$names"
}

disconnect_all() {
  title "Disconnect"
  info "Pauses all projects and stops the sync daemon. Projects are kept; use Reconnect to continue."
  confirm "Disconnect now?" y || return 1
  daemon_running && all_action pause
  mutagen daemon stop >/dev/null 2>&1 || true
  ok "Disconnected."
}

reconnect_all() {
  title "Reconnect"
  guard_start
  daemon_running && ok "Sync daemon running" || { err "Could not start the daemon."; return 1; }
  all_action resume
}

autostart_enabled() { grep -qF "$RC_MARK_BEGIN" "$HOME/.bashrc" 2>/dev/null || grep -qF "$RC_MARK_BEGIN" "$HOME/.zshrc" 2>/dev/null; }

toggle_autostart() {
  local rc
  if autostart_enabled; then
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$rc" ] && sed -i.bak "/$RC_MARK_BEGIN/,/$RC_MARK_END/d" "$rc" && rm -f "$rc.bak"
    done
    ok "Autostart disabled."
  else
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$rc" ] || continue
      printf '\n%s\nif command -v simabworkspace >/dev/null 2>&1; then simabworkspace guard-start >/dev/null 2>&1; elif command -v mutagen >/dev/null 2>&1; then mutagen daemon start >/dev/null 2>&1; fi\n%s\n' \
        "$RC_MARK_BEGIN" "$RC_MARK_END" >> "$rc"
    done
    ok "Autostart enabled: syncing starts automatically when you open a terminal."
  fi
}

# Make `simabworkspace` runnable from anywhere.
#  - script lives in a folder on PATH (e.g. ~/bin): ~/.local/bin/simabworkspace
#    becomes a SYMLINK to it, so edits to that file apply immediately
#  - otherwise (e.g. ~/Downloads/simabworkspace.sh): it is COPIED there
self_install() {
  local self dir target="$BIN_DIR/$APP"
  dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || return 0
  self="$dir/$(basename "$0")"
  [ "$self" = "$target" ] && return 0     # running the installed command itself
  [ -f "$self" ] || return 0              # e.g. bash <(curl …)
  mkdir -p "$BIN_DIR"
  case ":$PATH:" in
    *":$dir:"*)
      if [ "$dir" != "$BIN_DIR" ] && [ "$(readlink "$target" 2>/dev/null)" != "$self" ]; then
        rm -f "$target" && ln -s "$self" "$target"
        ensure_path
        ok "'$APP' → $self (linked: edits to that file apply immediately)"
      fi
      ;;
    *)
      if [ -L "$target" ] || [ ! -f "$target" ] || ! cmp -s "$self" "$target"; then
        rm -f "$target" && cp "$self" "$target" && chmod +x "$target"
        ensure_path
        ok "Installed as '$APP' → $target (just type $APP from now on)"
      fi
      [ $IS_WIN -eq 1 ] && write_windows_launcher
      # the window's files go with the copy (they are found next to the script otherwise)
      if [ -f "$dir/gui/server.py" ]; then
        mkdir -p "$CONFIG_DIR" && printf '%s\n' "$dir" > "$CONFIG_DIR/source"   # remembered for updates
        sync_gui_files "$dir"
      fi
      ;;
  esac
}

# Windows: simabworkspace.cmd next to the script, so the command also works in PowerShell / cmd
write_windows_launcher() {
  local bash_w
  bash_w="$(cygpath -w /)"
  bash_w="${bash_w%\\}\\bin\\bash.exe"
  printf '@"%s" "%%~dp0%s" %%*\r\n' "$bash_w" "$APP" > "$BIN_DIR/$APP.cmd"
}

# ----------------------------------------------------------------------------- updates
GITHUB_REPO="EsmailEmami/simabworkspace"

# self_update: download the latest release installer and run it (settings are kept)
self_update() {
  title "Update"
  local latest tmp
  latest=$(curl -fsSL --max-time 15 "https://api.github.com/repos/$GITHUB_REPO/releases/latest" 2>/dev/null \
           | sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -n 1)
  [ -n "$latest" ] || { err "Could not reach GitHub to check for updates."; return 1; }
  if [ "$(printf '%s\n%s\n' "$VERSION" "$latest" | sort -V | tail -n 1)" = "$VERSION" ]; then
    ok "Up to date ($VERSION)."; return 0
  fi
  info "Updating $VERSION → $latest…"
  tmp=$(mktemp -d)
  curl -fsSL --retry 3 "https://github.com/$GITHUB_REPO/releases/download/v$latest/simabworkspace-install.sh" -o "$tmp/install.sh" \
    || { rm -rf "$tmp"; err "Download failed."; return 1; }
  bash "$tmp/install.sh" --no-open || { rm -rf "$tmp"; err "Update failed."; return 1; }
  rm -rf "$tmp"
  ok "Updated to $latest. If the window is open, restart it: simabworkspace gui"
}

# ----------------------------------------------------------------------------- GUI
GUI_SHARE="${XDG_DATA_HOME:-$HOME/.local/share}/$APP/gui"

# resolve symlinks without readlink -f (not on older macOS)
real_path() {
  local p="$1" l
  while [ -L "$p" ]; do
    l=$(readlink "$p")
    case "$l" in /*) p="$l" ;; *) p="$(dirname "$p")/$l" ;; esac
  done
  printf '%s/%s' "$(cd "$(dirname "$p")" 2>/dev/null && pwd)" "$(basename "$p")"
}

# copy the window's files from a source folder when they changed
sync_gui_files() {
  local src="$1"
  [ -f "$src/gui/server.py" ] || return 0
  diff -rq "$src/gui" "$GUI_SHARE" >/dev/null 2>&1 && return 0
  mkdir -p "$(dirname "$GUI_SHARE")" && rm -rf "$GUI_SHARE" && cp -R "$src/gui" "$GUI_SHARE" \
    && ok "Workspace window updated → $GUI_SHARE"
}

# The installed command (a copy in ~/.local/bin) remembers the folder it was
# installed from; a newer version copied into that folder is picked up here.
update_from_source() {
  local src self target="$BIN_DIR/$APP"
  [ -f "$CONFIG_DIR/source" ] || return 0
  src=$(head -n 1 "$CONFIG_DIR/source")
  [ -n "$src" ] && [ -f "$src/$APP.sh" ] || return 0
  sync_gui_files "$src"
  self=$(real_path "$0")
  if [ "$self" = "$target" ] && ! cmp -s "$src/$APP.sh" "$target"; then
    cp "$src/$APP.sh" "$target.new" && chmod +x "$target.new" && mv -f "$target.new" "$target" \
      && ok "'$APP' updated from $src (takes effect next run)"
  fi
}

# folder holding server.py: next to the real script, else the installed copy
gui_dir() {
  local d
  d="$(dirname "$(real_path "$0")")/gui"
  [ -f "$d/server.py" ] && { printf '%s' "$d"; return 0; }
  [ -f "$GUI_SHARE/server.py" ] && { printf '%s' "$GUI_SHARE"; return 0; }
  return 1
}

# find_python -> a Python 3 that really runs (on Windows `python3` can be the Store's placeholder)
find_python() {
  local c
  for c in python3 python py; do
    command -v "$c" >/dev/null 2>&1 || continue
    "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1 && { printf '%s' "$c"; return 0; }
  done
  return 1
}

run_gui() {
  local d py
  update_from_source
  py=$(find_python) || { err "The window needs Python 3 (Linux: sudo apt install python3; Windows: winget install Python.Python.3.12)."; return 1; }
  d=$(gui_dir) || { err "GUI files not found. Keep the 'gui' folder next to $APP.sh and run it once from there."; return 1; }
  "$py" "$(winpath "$d/server.py")" "${1:-open}"
}

# server.py with arguments (pair <link>: connect this computer with a one-time link from the web version)
run_gui_cmd() {
  local d py
  py=$(find_python) || { err "Python 3 is needed."; return 1; }
  d=$(gui_dir) || { err "GUI files not found."; return 1; }
  "$py" "$(winpath "$d/server.py")" "$@"
}

# The workspace in the browser, for a tablet or another computer (see `simabworkspace web --help`).
run_web() {
  local d py
  py=$(find_python) || { err "The web version needs python3."; return 1; }
  d=$(gui_dir) || { err "GUI files not found. Keep the 'gui' folder next to $APP.sh."; return 1; }
  "$py" "$(winpath "$d/server.py")" web "$@"
}

uninstall_all() {
  title "Uninstall"
  warn "This stops syncing ALL projects (your files stay on both sides),"
  warn "removes settings and the $APP command. Mutagen itself is kept."
  [ "${1:-}" = "--yes" ] || confirm "Uninstall $APP?" n || return 1
  daemon_start
  local n
  while IFS= read -r n; do
    [ -n "$n" ] && mutagen sync terminate "$n" >/dev/null 2>&1 && ok "removed $n"
  done <<< "$(session_names)"
  mutagen daemon stop >/dev/null 2>&1 || true
  autostart_enabled && toggle_autostart
  remove_ssh_config
  run_gui stop >/dev/null 2>&1 || true
  [ -f "$SSH_CONFIG" ] && sed -i.bak '/# >>> simabworkspace-gui >>>/,/# <<< simabworkspace-gui <<</d' "$SSH_CONFIG" && rm -f "$SSH_CONFIG.bak"
  rm -rf "$(dirname "$GUI_SHARE")"
  rm -rf "$CONFIG_DIR"
  rm -f "$BIN_DIR/$APP"
  if [ $IS_WIN -eq 1 ]; then
    rm -f "$BIN_DIR/$APP.cmd"
    rm -f "$APPDATA/Microsoft/Windows/Start Menu/Programs/Simab Workspace.lnk" \
          "$APPDATA/Microsoft/Windows/Start Menu/Programs/Startup/simabworkspace-sync.cmd" 2>/dev/null || true
  fi
  ok "Uninstalled."
  exit 0
}

run_setup() {
  printf '\n  %s[1/4]%s Tools this machine needs\n' "$C_BOLD$C_C" "$C_0"
  check_prereqs
  printf '\n  %s[2/4]%s Your server\n' "$C_BOLD$C_C" "$C_0"
  if [ -z "$SSH_USER" ] || [ -z "$SSH_HOST" ]; then configure; else ok "Server: $SSH_USER@$SSH_HOST:$SSH_PORT"; fi
  printf '\n  %s[3/4]%s SSH key (log in without a password)\n' "$C_BOLD$C_C" "$C_0"
  setup_ssh || true
  printf '\n  %s[4/4]%s Sync service and the simabworkspace command\n' "$C_BOLD$C_C" "$C_0"
  run_step "Starting the sync service" mutagen daemon start || true
  self_install
  printf '\n'
  ok "Setup finished."
}

# item <key> <label> <width>  -> "  1  Add project" padded to width
item() { printf '%s%s%s %s' "$C_C$C_BOLD" "$(printf '%3s' "$1")" "$C_0" "$(pad "$2" $(( $3 - 4 )))"; }

menu() {
  while :; do
    clear 2>/dev/null || true
    show_status

    local W cw auto
    W=$(term_width); cw=$(( (W - 2) / 3 ))
    if autostart_enabled; then auto="Autostart: on"; else auto="Autostart: off"; fi

    printf '\n  %s%s%s%s\n' "$C_BOLD" "$(pad 'PROJECT' "$cw")" "$(pad 'ALL PROJECTS' "$cw")" "SETUP$C_0"
    printf '  %s%s%s\n' "$C_DIM" "$(repeat '─' $((cw * 3 - 2)))" "$C_0"
    printf '  %s%s%s\n' "$(item 1 'Add project' "$cw")"   "$(item 10 'Pause all'  "$cw")" "$(item 14 'SSH shell on server' "$cw")"
    printf '  %s%s%s\n' "$(item 2 'Pause' "$cw")"         "$(item 11 'Resume all' "$cw")" "$(item 15 'Settings' "$cw")"
    printf '  %s%s%s\n' "$(item 3 'Resume' "$cw")"        "$(item 12 'Disconnect' "$cw")" "$(item 16 'SSH key' "$cw")"
    printf '  %s%s%s\n' "$(item 4 'Sync now' "$cw")"      "$(item 13 'Reconnect'  "$cw")" "$(item 17 'Prerequisites' "$cw")"
    printf '  %s%s%s\n' "$(item 5 'Details / conflicts' "$cw")" "$(pad '' "$cw")"      "$(item 18 "$auto" "$cw")"
    printf '  %s%s%s\n' "$(item 6 'Live monitor' "$cw")"  "$(pad '' "$cw")"           "$(item 19 'Uninstall' "$cw")"
    printf '  %s\n'     "$(item 7 'Move folders' "$cw")"
    printf '  %s\n'     "$(item 8 'Git sync on/off' "$cw")"
    printf '  %s\n'     "$(item 9 'Remove from sync' "$cw")"
    printf '\n  %sg%s  %sOpen the workspace window%s  (tabs, Claude, VS Code, run buttons)\n' "$C_C$C_BOLD" "$C_0" "$C_BOLD" "$C_0"
    printf '\n  %sEnter%s refresh   %s0%s exit\n' "$C_C$C_BOLD" "$C_0" "$C_C$C_BOLD" "$C_0"

    local c
    printf '\n'
    read -r -p "  › " c || exit 0
    printf '\n'
    case "$c" in
      1)  add_project_menu; pause_key ;;
      2)  project_action pause; pause_key ;;
      3)  project_action resume; pause_key ;;
      4)  project_action flush; pause_key ;;
      5)  project_action info; pause_key ;;
      6)  project_action monitor ;;
      7)  move_project; pause_key ;;
      8)  git_toggle; pause_key ;;
      9)  project_action remove; pause_key ;;
      10) all_action pause; pause_key ;;
      11) all_action resume; pause_key ;;
      12) disconnect_all; pause_key ;;
      13) reconnect_all; pause_key ;;
      14) sshk -p "$SSH_PORT" "$(remote)" ;;
      15) configure; setup_ssh; pause_key ;;
      16) setup_ssh force; pause_key ;;
      17) run_setup; pause_key ;;
      18) toggle_autostart; pause_key ;;
      19) uninstall_all; pause_key ;;
      g|G) run_gui open; pause_key ;;
      "") ;;
      0|q|Q|exit) clear 2>/dev/null || true; exit 0 ;;
      *) warn "Unknown choice: $c"; pause_key ;;
    esac
  done
}

usage() { sed -n '2,/^# =====/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^=====/d'; }

# ----------------------------------------------------------------------------- main
main() {
  load_config
  local cmd="${1:-menu}"
  case "$cmd" in
    help|-h|--help) usage; exit 0 ;;
    version|--version) echo "$APP $VERSION"; exit 0 ;;
    setup)     [ -x "$BIN_DIR/mutagen" ] && ensure_path; run_setup; exit $? ;;
    update)    self_update; exit $? ;;
    # installer: put the command + window files in place, nothing else
    self-install) self_install; [ -x "$BIN_DIR/mutagen" ] && ensure_path; ensure_path; exit 0 ;;
    # used by the window: never start the interactive setup for these
    install)   [ -x "$BIN_DIR/mutagen" ] && ensure_path; install_one "${2:-}"; exit $? ;;
    gui)       [ -x "$BIN_DIR/mutagen" ] && ensure_path; run_gui "${2:-open}"; exit $? ;;
    guard-start) [ -x "$BIN_DIR/mutagen" ] && ensure_path; type -P mutagen >/dev/null 2>&1 && guard_start; exit 0 ;;
    web)       shift; run_web "$@"; exit $? ;;
    pair)      run_gui_cmd pair "${2:-}"; exit $? ;;
    shortcut)  run_gui_cmd shortcut; exit $? ;;   # install.sh: the app-menu entry with the logo
  esac

  # first run, or a tool went missing: full setup (prereqs → user → SSH key)
  [ -x "$BIN_DIR/mutagen" ] && ensure_path
  if [ ! -f "$CONFIG_FILE" ] || [ -z "$SSH_USER" ] \
     || ! type -P mutagen >/dev/null 2>&1 || ! type -P jq >/dev/null 2>&1; then
    if [ -n "${SW_NONINTERACTIVE:-}" ]; then      # called by the window: no wizard here
      err "Setup is not complete (server settings, mutagen or jq missing) — open Settings in the window."
      exit 1
    fi
    run_setup
    pause_key
  fi

  self_install

  case "$cmd" in
    menu)        guard_start; menu ;;
    status)      show_status ;;
    add)         add_project_menu ;;
    add-local)   shift; add_local_project "$@" ;;
    add-server)  shift; add_project "$@" ;;
    pause|resume|flush|remove|info|monitor) project_action "$cmd" "${2:-}" ;;
    move)        shift; move_project "$@" ;;
    git)         git_toggle "${2:-}" "${3:-}" ;;
    ignore)      shift; ignore_set "$@" ;;
    pause-all)   all_action pause ;;
    resume-all)  all_action resume ;;
    disconnect)  disconnect_all ;;
    reconnect)   reconnect_all ;;
    ssh)         sshk -p "$SSH_PORT" "$(remote)" ;;
    gui)         run_gui "${2:-open}" ;;
    key)         setup_ssh force ;;
    setup)       run_setup ;;
    config)      configure; setup_ssh ;;
    uninstall)   uninstall_all "${2:-}" ;;
    install)     install_one "${2:-}" ;;
    *)           err "Unknown command: $cmd"; usage; exit 1 ;;
  esac
}

main "$@"
