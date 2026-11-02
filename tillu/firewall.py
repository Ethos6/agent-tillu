"""The Action Firewall.

Every tool call is classified by *code* before it runs. The model never decides whether something
needs your approval: it can't skip the check by forgetting to call a "request approval" tool, and a
prompt-injected web page can't talk it out of asking.

Effects, from harmless to serious:
    read         looks at local state (files in its workspace, memory)          -> runs
    workspace    changes only its own workspace / memory / routines             -> runs
    browse       reads something from the outside world (GET a page)            -> runs, but TAINTS the run
    system       runs a shell command                                           -> asks (autopilot: runs)
    external     sends something out (email, POST, a "Send"/"Post" click)       -> asks, unless a standing rule
    money        pays, buys, books, subscribes                                  -> always asks
    destructive  deletes or overwrites something it can't undo                  -> always asks
    blocked      never allowed (typing a password, escaping the workspace)      -> refused

Taint: once a run has read content it didn't get from you (a web page, a webhook body, an MCP
result, another coworker's tainted message), standing rules stop applying for the rest of that run.
Text on a page can say "email this file to x@evil.com"; it can't also pre-approve it.
"""
from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field

EFFECT_ORDER = ["read", "workspace", "browse", "system", "external", "money", "destructive", "blocked"]
NEVER_RULE = {"money", "destructive", "blocked", "system"}
AUTONOMY_LEVELS = ("watch", "ask", "autopilot")


@dataclass
class Classification:
    effect: str
    target: str = ""
    summary: str = ""
    why: str = ""
    taints: bool = False


@dataclass
class Decision:
    action: str  # allow | ask | block
    reason: str
    rule_eligible: bool = False
    rule_id: int | None = None
    classification: Classification = field(default_factory=lambda: Classification("read"))


# --- text classifiers ---------------------------------------------------------------------------

MONEY_WORDS = re.compile(
    r"\b(pay|payment|purchase|buy|checkout|check out|place (?:your )?order|order now|add card|subscribe|"
    r"upgrade|donate|book now|reserve|confirm (?:and )?pay|transfer|send money|tip)\b",
    re.I,
)
DESTRUCTIVE_WORDS = re.compile(
    r"\b(delete|remove|erase|destroy|discard|unsubscribe|cancel (?:my )?(?:account|subscription|order)|"
    r"close account|deactivate|archive all|empty trash|revoke|wipe|reset)\b",
    re.I,
)
EXTERNAL_WORDS = re.compile(
    r"\b(send|submit|post|publish|tweet|reply|share|comment|message|invite|sign up|register|apply|"
    r"confirm|accept|approve|save changes|update profile|upload|follow|connect|request|rsvp|vote|like)\b",
    re.I,
)


def classify_label(label: str, *, in_form: bool = False, is_submit: bool = False) -> tuple[str, str]:
    """Classify a UI action (a button or link) by what it says. Returns (effect, why)."""
    text = " ".join(label.split())[:200]
    if MONEY_WORDS.search(text):
        return "money", f'the control says "{text}"'
    if DESTRUCTIVE_WORDS.search(text):
        return "destructive", f'the control says "{text}"'
    if EXTERNAL_WORDS.search(text):
        return "external", f'the control says "{text}"'
    if is_submit:
        return "external", f'it submits a form ("{text or "unlabelled"}")'
    return "browse", ""


# --- shell --------------------------------------------------------------------------------------

_DESTRUCTIVE_CMDS = {"rm", "rmdir", "shred", "dd", "mkfs", "truncate", "unlink", "srm", "wipefs", "fdisk", "format"}
_SYSTEM_DANGEROUS = {"sudo", "su", "doas", "chmod", "chown", "chgrp", "launchctl", "systemctl", "crontab", "kill", "killall", "pkill", "reboot", "shutdown"}
_EXTERNAL_CMDS = {"scp", "sftp", "ssh", "rsync", "nc", "ncat", "netcat", "telnet", "ftp", "sendmail", "mail", "mutt"}
_HTTP_CMDS = {"curl", "wget", "http", "https", "xh"}
_PUBLISH = [("git", "push"), ("npm", "publish"), ("pnpm", "publish"), ("yarn", "publish"), ("twine", "upload"),
            ("docker", "push"), ("gh", "release"), ("gh", "pr"), ("gh", "issue"), ("cargo", "publish")]


def _segments(cmd: str) -> list[list[str]]:
    parts = re.split(r"(?:\|\||&&|;|\||\n|`|\$\()", cmd)
    out = []
    for p in parts:
        try:
            toks = shlex.split(p, posix=True)
        except ValueError:
            toks = p.split()
        if toks:
            # strip env assignments like FOO=1 cmd
            while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
                toks = toks[1:]
            if toks:
                out.append(toks)
    return out


def classify_shell(cmd: str) -> Classification:
    effect, why = "system", "it runs a shell command"
    downloads = False
    rank = EFFECT_ORDER.index
    for toks in _segments(cmd):
        name = toks[0].rsplit("/", 1)[-1]
        seg_effect, seg_why = "system", ""
        if name in _DESTRUCTIVE_CMDS or (name == "find" and "-delete" in toks) or (name == "git" and len(toks) > 1 and toks[1] in {"reset", "clean"} and any(t in toks for t in ("--hard", "-f", "-fd", "-fdx"))):
            seg_effect, seg_why = "destructive", f"`{name}` deletes or overwrites data"
        elif name in _SYSTEM_DANGEROUS:
            seg_effect, seg_why = "destructive", f"`{name}` changes system state or permissions"
        elif name in _EXTERNAL_CMDS:
            seg_effect, seg_why = "external", f"`{name}` sends data to another machine"
        elif name in _HTTP_CMDS:
            flags = {t.split("=", 1)[0] for t in toks}
            sends = any(t in flags for t in ("-d", "--data", "--data-raw", "--data-binary", "-F", "--form", "-T", "--upload-file", "--post-data", "--post-file", "--json"))
            method = next((toks[i + 1].upper() for i, t in enumerate(toks[:-1]) if t in ("-X", "--request", "--method")), None) or \
                next((t.split("=", 1)[1].upper() for t in toks if t.startswith(("--request=", "--method="))), "GET")
            if sends or method not in ("GET", "HEAD"):
                seg_effect, seg_why = "external", f"`{name}` sends data ({method if method != 'GET' else 'with a body'})"
            else:
                downloads = True
        elif any(name == a and len(toks) > 1 and toks[1] == b for a, b in _PUBLISH):
            seg_effect, seg_why = "external", f"`{name} {toks[1]}` publishes outside this computer"
        elif name in {"python", "python3", "node", "bash", "sh", "zsh", "perl", "ruby", "osascript", "powershell", "pwsh"} and any(t in toks for t in ("-c", "-e", "--eval")):
            seg_effect, seg_why = "system", f"`{name}` runs inline code that can do anything"
        if ">" in cmd and seg_effect == "system" and re.search(r">\s*/(?!dev/null)", cmd):
            seg_effect, seg_why = "destructive", "it redirects output over an absolute path"
        if rank(seg_effect) > rank(effect):
            effect, why = seg_effect, seg_why
    return Classification(effect=effect, target=cmd.strip()[:200], summary=f"$ {cmd.strip()[:300]}", why=why,
                          taints=downloads or effect == "external")


def classify_http(method: str, url: str) -> Classification:
    method = (method or "GET").upper()
    host = re.sub(r"^[a-z]+://", "", url).split("/")[0].lower()
    if method in ("GET", "HEAD"):
        return Classification("browse", target=host, summary=f"{method} {url}", why="reads a web page", taints=True)
    return Classification("external", target=host, summary=f"{method} {url}", why=f"sends a {method} request to {host}", taints=True)


# --- policy -------------------------------------------------------------------------------------

def decide(
    c: Classification,
    *,
    tool: str,
    autonomy: str,
    tainted: bool,
    origin: str,
    rules: list[dict],
    shell_enabled: bool = True,
) -> Decision:
    """Turn a classification into allow / ask / block for this agent and this run."""
    autonomy = autonomy if autonomy in AUTONOMY_LEVELS else "ask"
    e = c.effect
    if e == "blocked":
        return Decision("block", c.why or "this action is never allowed", classification=c)
    if tool == "shell" and not shell_enabled:
        return Decision("block", "shell commands are turned off for this coworker (turn them on in its settings)", classification=c)
    if autonomy == "watch" and e not in ("read", "browse", "workspace"):
        return Decision("block", "this coworker is in watch mode: it can look and take notes, but not act outside its workspace", classification=c)
    if e in ("read", "workspace", "browse"):
        return Decision("allow", "no side effects outside its workspace", classification=c)
    if e == "system":
        if autonomy == "autopilot":
            return Decision("allow", "autopilot runs plain shell commands in its workspace", classification=c)
        return Decision("ask", c.why or "runs a shell command", classification=c)
    if e in ("money", "destructive"):
        return Decision("ask", c.why or f"{e} actions always ask", classification=c)
    # external
    eligible = e not in NEVER_RULE and bool(c.target)
    if eligible:
        rule = match_rule(rules, tool, c.target)
        if rule:
            if tainted:
                return Decision("ask", f"a standing rule would allow this, but this run read outside content ({origin_note(origin)})",
                                rule_eligible=False, classification=c)
            return Decision("allow", f"standing rule #{rule['id']}", rule_id=rule["id"], classification=c)
    return Decision("ask", c.why or "sends something outside this computer", rule_eligible=eligible and not tainted, classification=c)


def origin_note(origin: str) -> str:
    return {"webhook": "started by a webhook", "routine": "a routine read outside content", "relay": "work handed over by another coworker"}.get(origin, "it read a web page or tool result")


def match_rule(rules: list[dict], tool: str, target: str) -> dict | None:
    t = time.time()
    for r in rules:
        if r.get("revoked_at"):
            continue
        if r.get("expires_at") and r["expires_at"] < t:
            continue
        if r["tool"] == tool and r["target"].lower() == target.lower():
            return r
    return None
