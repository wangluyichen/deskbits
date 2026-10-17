# Security Policy

DeskBits (拾碎-懒人桌面工具箱) is a **fully offline** Windows desktop tool:
it organizes desktop icons and bundles small system utilities. It makes no
network calls and uploads nothing — everything lives in the app's own `config\`
directory on your machine, and personal data can be wiped from inside the app.

## Reporting a vulnerability

Please use **private vulnerability reporting** on the Security tab
(<https://github.com/wangluyichen/deskbits/security/advisories/new>) instead of
opening a public issue. I read reports there and will usually reply within a
few days.

This is a one-person hobby project, so there is no security team, no bug bounty
and no fixed SLA — but anything real gets fixed, and a fixed release is published
as normal through the [Releases page](https://github.com/wangluyichen/deskbits/releases).

## What counts as in scope

- A code path that runs a command, deletes a file or changes a system setting
  that **you did not ask for** (the app deliberately can create scheduled tasks,
  run configured commands and lock the screen — those are features, not findings).
- Anything that lets a remote host affect your machine: the app has no network
  surface, so if a build ever reaches out, that is a bug.
- Corruption or loss of your desktop icons / layout / config caused by the app
  itself, when it is not the result of a documented risky operation.
- A way to make the app write outside its own directories.

## What is out of scope by design

- Attacks that already require physical access to your machine or your signed-in
  Windows session — at that point the attacker has the app's config anyway.
- Data you deliberately put in the app's own folders (`config\`, the
  `桌面盒子deskbits\` box folders), and anything lost because a built-in backup
  was not used before a risky operation.
- Windows SmartScreen / antivirus warnings on first run: builds are **unsigned**,
  that is expected for a free hobby tool, not a vulnerability.
- Third-party components of your own environment (Python, pywin32, Tkinter,
  the shell) — dependency advisories are tracked by Dependabot on this repo.

## Version support

Only the latest released version is maintained; there is no back-port policy for
older builds. Please re-test against the current release before reporting.
