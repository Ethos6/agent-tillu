# Security

Agent Tillu runs AI coworkers with real tools on your computer, so its security model matters more than most apps'.

## What it defends against

- **Prompt injection via web pages, webhooks and tool output.** The Action Firewall (`tillu/firewall.py`) classifies every tool call in code; the model cannot skip it. Once a run has read outside content it is *tainted*: standing approvals stop applying, and any destination the person did not name is flagged on the approval card.
- **Lies in the loop.** Approval cards are built from the actual action (the element the browser will click, the parsed shell command, the email recipient), never from the model's description. Right before dispatch the action is inspected again; if it changed since approval (a page swapped a button), it is held and re-asked.
- **Credential theft.** Coworkers cannot type into password or payment fields. Logins happen in the coworker's own browser profile through `ask_for_login`.
- **Workspace escape.** File tools are confined to `~/.tillu/workspaces/<coworker>/`.
- **Network exposure.** The server binds to 127.0.0.1, requires a random token, rejects foreign `Host` headers and requires an `X-Tillu` header on writes.

## What it does not defend against

- **Shell commands are not sandboxed.** Shell is off per coworker by default and asks before every command (Autopilot runs plain commands without asking), but a command that is approved runs as your user. Run Agent Tillu under a separate OS user or in a VM if you grant broad shell access.
- **A model that is socially engineered into asking you to approve something harmful.** Read the cards.
- **Misleading read-only output.** A page can lie to the coworker; the coworker can repeat the lie to you.

## Reporting

Open a private security advisory on GitHub, or email the maintainer listed in `pyproject.toml`. Please don't file public issues for vulnerabilities.
