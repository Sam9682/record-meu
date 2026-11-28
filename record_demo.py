"""
Demo recorder: automate a scripted demo, record it, export MP4 / GIF.

Targets
    web         a web site in Chromium (chatbot, forms, ...)
    vscode      VS Code (locally or on WSL), driven like a web page
    powershell  a PowerShell session, rendered as a terminal window
    bash        a bash session, rendered as a terminal window

A scenario (JSON file, by default in scenarios/) = a target + an ordered list
of steps. Steps can be automatic (ask a question, run a command, type in a
field, click...) or manual (log in yourself, approve an MFA prompt...).
Any step can be "hidden": it is executed but cut out of the final video
(login pages, credential setup, ...). Secrets are asked at run time and never
saved in the scenario.

Setup
    pip install playwright imageio-ffmpeg   # imageio-ffmpeg = bundled ffmpeg fallback
    playwright install chromium             # only if no local Chromium (see browser_path)

Run
    python record_demo.py                       # menu on scenarios/oracle.json
    python record_demo.py scenarios/x.json      # menu on another scenario
    python record_demo.py scenarios/x.json --run   # record directly, no menu
"""
import argparse
import base64
import codecs
import copy
import ctypes
import getpass
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Error as PlaywrightError

SCRIPT_DIR = Path(__file__).resolve().parent
SCENARIO_DIR = SCRIPT_DIR / "scenarios"
DEFAULT_SCENARIO = SCENARIO_DIR / "oracle.json"
VIDEO_DIR = SCRIPT_DIR / "videos"
# Locally installed Chromium (target of the Start Menu shortcut
# C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Chromium.lnk)
CHROMIUM_PATH = r"C:\Program Files\Chromium\Application\chrome.exe"
HEADLESS = False

WEB, VSCODE, TERMINALS = "web", "vscode", ("powershell", "bash")
TARGET_TYPES = (WEB, VSCODE) + TERMINALS
PAGES = {WEB, VSCODE}  # targets driven as a web page through Playwright

# --------------------------------------------------------------------------
# Scenario schema: (key, label, kind, default, choices)
# kind: str | int | float | bool | choice | list
# --------------------------------------------------------------------------
F = namedtuple("F", "key label kind default choices", defaults=(None,))

CHAT_FIELDS = [
    F("input_selectors", "Chat input selectors, first visible wins (separated by |)",
      "list", ["textarea", "input[type=text]", "input:not([type])",
               "[contenteditable=true]"]),
    F("send_selector", "Send button selector (empty = press Enter)", "str", ""),
    F("answer_selector", "Selector of one bot answer (optional)", "str",
      ".message.assistant"),
]
WSL_DISTRO = F("wsl_distro", "WSL distribution (empty = default one)", "str", "")

TARGET_FIELDS = {
    WEB: [
        F("url", "Start URL", "str", "https://opcp-psmc.com:6124/oracle"),
        *CHAT_FIELDS,
        F("ignore_https_errors", "Ignore HTTPS certificate errors", "bool", True),
    ],
    VSCODE: [
        F("code_path", "VS Code executable: auto | path to Code.exe", "str", "auto"),
        F("remote", "Open the folder: local | wsl (VS Code Remote - WSL)", "choice",
          "wsl", ["local", "wsl"]),
        WSL_DISTRO,
        F("folder", "Folder to open (Linux path when remote = wsl, empty = none)",
          "str", ""),
        F("profile_dir", "Demo VS Code profile (settings + extensions), relative to "
          "this script", "str", "vscode-demo-profile"),
        F("uninstall_first", "Extension ids uninstalled before the run, so the demo "
          "can install them (separated by |)", "list", []),
        *CHAT_FIELDS,
        F("close_at_end", "Close VS Code at the end", "bool", True),
    ],
    "terminal": [
        F("cwd", "Working directory (relative to this script)", "str", "."),
        F("prompt", "Prompt shown, {cwd} = current dir (empty = shell default)", "str", ""),
        F("title", "Window title (empty = shell default)", "str", ""),
        F("font_size", "Font size (px)", "int", 16),
        F("shell_exe", "Bash only: auto (first bash in PATH) | wsl | git | path to bash.exe",
          "str", "auto"),
        WSL_DISTRO,
        F("user", "Bash on WSL / Linux: run as this user (empty = default user)", "str", ""),
        F("load_profile", "Load the user's profile (~/.profile + ~/.bashrc / PowerShell "
          "profile): PATH, venv, aliases", "bool", False),
    ],
}

TIMING_FIELDS = [
    F("typing_delay_ms", "Typing delay per character (ms)", "int", 45),
    F("pause_after_step", "Pause after each answer / command, so viewers can read (s)",
      "float", 3),
    F("max_wait_per_answer", "Web: max wait for a chatbot answer (s)", "int", 120),
    F("answer_stable_for", "Web: answer is finished when unchanged for (s)", "float", 4),
    F("manual_timeout", "Max wait for a manual step (s, 0 = forever)", "int", 600),
]

OUTPUT_FIELDS = [
    F("name", "Output file name (no extension)", "str", "demo"),
    F("width", "Video width (px)", "int", 1280),
    F("height", "Video height (px)", "int", 720),
    F("mp4", "Export MP4", "bool", True),
    F("gif", "Export GIF", "bool", True),
    F("gif_speed", "GIF speed factor", "float", 2.0),
    F("gif_fps", "GIF frames per second", "int", 10),
    F("gif_width", "GIF width (px)", "int", 960),
    F("delete_raw", "Delete the raw .webm after export (it may show hidden steps)",
      "bool", True),
    F("browser_path", "Browser executable (empty = Playwright's Chromium)", "str",
      CHROMIUM_PATH),
]

HIDE = F("hide", "Hide this step in the final video", "bool", False)
ALL = set(TARGET_TYPES)
STEP_TYPES = {
    "ask": dict(targets=PAGES, help="Type a question in the chat box, send it, "
                "wait until the answer is complete", fields=[
        F("text", "Question", "str", "")]),
    "type": dict(targets=PAGES, help="Type text in a field (login, search...)", fields=[
        F("selector", "CSS selector of the field (empty = the focused element)", "str",
          "input[type=text]"),
        F("text", "Text to type", "str", ""),
        F("secret", "Secret: asked at run time, never saved (password)", "bool", False),
        F("press_enter", "Press Enter after typing", "bool", False)]),
    "key": dict(targets=PAGES, help="Press a key or a shortcut (Enter, Control+Shift+X...)",
                fields=[
        F("keys", "Keys, Playwright syntax, several separated by spaces", "str", "Enter")]),
    "click": dict(targets=PAGES, help="Click an element", fields=[
        F("selector", "CSS selector to click", "str", "button[type=submit]"),
        F("wait_answer", "Then wait until the page stops changing (AI generation...)",
          "bool", False)]),
    "select": dict(targets={WEB}, help="Choose an option in a drop-down list", fields=[
        F("selector", "CSS selector of the <select>", "str", "select"),
        F("option", "Option to choose (visible text)", "str", "")]),
    "wait_for": dict(targets=PAGES, help="Wait until an element is visible (end of an "
                     "install, page loaded...)", fields=[
        F("selector", "CSS selector to wait for", "str", ""),
        F("timeout", "Max seconds to wait", "int", 120)]),
    "scroll": dict(targets=PAGES, help="Scroll slowly through an element (or the page), "
                   "so viewers can read it", fields=[
        F("selector", "CSS selector of the element (empty = whole page)", "str", ""),
        F("seconds", "Duration of the scroll (s)", "float", 10)]),
    "goto": dict(targets={WEB}, help="Open a URL in the current tab", fields=[
        F("url", "URL", "str", "")]),
    "run": dict(targets=set(TERMINALS), help="Type a command, run it, wait until it "
                "finishes", fields=[
        F("command", "Command", "str", ""),
        F("timeout", "Max seconds to wait for the command to finish", "int", 300)]),
    "secret": dict(targets=set(TERMINALS), help="Run a command containing a secret asked "
                   "at run time (shown as ********)", fields=[
        F("prompt", "Question asked at run time", "str", "Password"),
        F("command", "Command, {value} = the secret", "str",
          "$env:OS_PASSWORD = '{value}'")]),
    "clear": dict(targets=set(TERMINALS), help="Clear the terminal screen", fields=[]),
    "manual": dict(targets=ALL, help="Pause: you act yourself (login, MFA...), the "
                   "script continues when the condition is met", fields=[
        F("message", "Instruction shown in the console", "str",
          "Do the manual action in the window"),
        F("until", "Continue when (web: enter/url/selector, terminal: enter)",
          "choice", "enter", ["enter", "url", "selector"]),
        F("value", "URL part or CSS selector to wait for (empty = target URL)",
          "str", "")], hide=True),
    "wait": dict(targets=ALL, help="Wait a few seconds", fields=[
        F("seconds", "Seconds", "float", 2)]),
    "title": dict(targets=ALL, help="Full-screen title card in big bold text, shown "
                  "for a few seconds (recorded in the video)", fields=[
        F("text", "Title", "str", "Hello ! Demonstration of OpenStack"),
        F("subtitle", "Subtitle (optional)", "str", ""),
        F("seconds", "Seconds on screen", "float", 4),
        F("font_size", "Title font size (px)", "int", 64)]),
}

TITLE_JS = """([text, sub, size]) => {
  const d = document.createElement('div');
  d.id = '__demo_title__';
  d.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;' +
    'flex-direction:column;align-items:center;justify-content:center;gap:24px;' +
    'background:#0b1f3a;color:#fff;text-align:center;padding:40px;' +
    'font-family:"Segoe UI",Arial,sans-serif';
  const h = document.createElement('div');
  h.textContent = text;
  h.style.cssText = `font-size:${size}px;font-weight:800;line-height:1.2`;
  d.appendChild(h);
  if (sub) {
    const s = document.createElement('div');
    s.textContent = sub;
    s.style.cssText = `font-size:${Math.round(size * 0.45)}px;opacity:.8`;
    d.appendChild(s);
  }
  document.body.appendChild(d);
}"""


SCROLL_JS = """([sel, ms]) => new Promise(done => {
  const el = sel ? document.querySelector(sel) : null;
  const max = document.documentElement.scrollHeight - innerHeight;
  let from = scrollY, to = max;
  if (el) {
    const r = el.getBoundingClientRect();
    from = Math.min(max, Math.max(0, r.top + scrollY - 90));   // below a fixed header
    to = Math.min(max, Math.max(from, r.bottom + scrollY - innerHeight + 40));
    window.scrollTo(0, from);
  }
  const t0 = performance.now();
  const ease = x => x < .5 ? 2 * x * x : 1 - (-2 * x + 2) ** 2 / 2;
  (function step(now) {
    const k = Math.min(1, (now - t0) / Math.max(ms, 1));
    window.scrollTo(0, from + (to - from) * ease(k));
    k < 1 ? requestAnimationFrame(step) : done();
  })(t0);
})"""


def show_title(page, st):
    """Cover the page with a title card for st['seconds'], then remove it."""
    page.evaluate(TITLE_JS, [st["text"], st["subtitle"], st["font_size"]])
    page.wait_for_timeout(st["seconds"] * 1000)
    page.evaluate("() => document.getElementById('__demo_title__')?.remove()")


def target_group(target_type):
    return target_type if target_type in PAGES else "terminal"


def step_fields(step_type):
    return STEP_TYPES[step_type]["fields"] + [HIDE]


def new_step(step_type):
    step = {"type": step_type}
    for f in step_fields(step_type):
        step[f.key] = copy.deepcopy(f.default)
    step["hide"] = STEP_TYPES[step_type].get("hide", False)
    return step


def normalize(sc):
    """Fill missing keys with defaults (old / hand-written scenario files)."""
    sc.setdefault("target", {})
    sc["target"].setdefault("type", WEB)
    for group in TARGET_FIELDS.values():
        for f in group:
            sc["target"].setdefault(f.key, copy.deepcopy(f.default))
    for section, fields in (("timing", TIMING_FIELDS), ("output", OUTPUT_FIELDS)):
        sc.setdefault(section, {})
        for f in fields:
            sc[section].setdefault(f.key, copy.deepcopy(f.default))
    steps = []
    for st in sc.get("steps", []):
        if st.get("type") not in STEP_TYPES:
            print(f"  ! unknown step type ignored: {st}")
            continue
        full = new_step(st["type"])
        full.update(st)
        steps.append(full)
    sc["steps"] = steps
    return sc


def default_oracle_scenario():
    sc = normalize({"target": {"type": WEB}})
    login = new_step("manual")
    login.update(message="Log in to the oracle in the browser window "
                         "(the demo starts once the oracle page is loaded)",
                 until="url", value="")
    sc["steps"].append(login)
    for q in [
        "What is OVH OPCP?",
        "Is OVH OPCP based on OpenStack for the underlying layer ?",
        "Can you list the commands of OpenStack?",
        "Generate a paragraph to communicate on LinkedIn that this is an example of "
        "using OVH AI LLM on OVH tooling/OS on OVHcloud servers, a full first "
        "sovereign solution.",
    ]:
        st = new_step("ask")
        st["text"] = q
        sc["steps"].append(st)
    return sc


def default_powershell_scenario():
    sc = normalize({"target": {"type": "powershell", "prompt": "PS C:\\demo> "}})
    sc["output"]["name"] = "demo-openstack"
    setup = new_step("run")
    setup.update(command=". .\\venv\\Scripts\\Activate.ps1; . .\\creds-openstack.ps1",
                 hide=True)
    sc["steps"].append(setup)
    for cmd in ["openstack --version", "openstack server list", "openstack network list"]:
        st = new_step("run")
        st["command"] = cmd
        sc["steps"].append(st)
    return sc


def load_scenario(path):
    path = Path(path)
    if not path.exists():
        if path.resolve() == DEFAULT_SCENARIO.resolve():
            save_scenario(default_oracle_scenario(), path)
            example = SCENARIO_DIR / "openstack-powershell.json"
            if not example.exists():
                save_scenario(default_powershell_scenario(), example)
            print(f"Created {path} and {example}")
        else:
            raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as fh:
        return normalize(json.load(fh))


def save_scenario(sc, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sc, fh, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Console menu
# --------------------------------------------------------------------------
def fmt_value(f, value):
    if f.kind == "bool":
        return "yes" if value else "no"
    if f.kind == "list":
        return " | ".join(value or [])
    return "" if value is None else str(value)


def prompt_value(f, current):
    """Ask a new value for field f. Enter keeps it, '-' empties a text field."""
    choices = f" ({'/'.join(f.choices)})" if f.choices else (" (y/n)" if f.kind == "bool"
                                                              else "")
    while True:
        raw = input(f"  {f.label}{choices} [{fmt_value(f, current)}]: ").strip()
        if raw == "":
            return current
        try:
            if raw == "-" and f.kind in ("str", "list"):
                return [] if f.kind == "list" else ""
            if f.kind == "int":
                return int(raw)
            if f.kind == "float":
                return float(raw)
            if f.kind == "bool":
                if raw.lower() in ("y", "yes", "o", "oui", "true", "1"):
                    return True
                if raw.lower() in ("n", "no", "non", "false", "0"):
                    return False
                raise ValueError
            if f.kind == "choice":
                if raw in f.choices:
                    return raw
                if raw.isdigit() and 1 <= int(raw) <= len(f.choices):
                    return f.choices[int(raw) - 1]
                raise ValueError
            if f.kind == "list":
                return [s.strip() for s in raw.split("|") if s.strip()]
            return raw
        except ValueError:
            print("    invalid value, try again")


def edit_fields(d, fields):
    for f in fields:
        d[f.key] = prompt_value(f, d.get(f.key, f.default))


def short(text, n=70):
    text = str(text)
    return text if len(text) <= n else text[: n - 3] + "..."


def describe_step(st):
    t = st["type"]
    if t == "ask":
        s = f'"{short(st["text"])}"'
    elif t == "type":
        s = f'{st["selector"] or "(focused)"} <- ' + ("<secret asked at run time>" if st["secret"]
                                        else f'"{short(st["text"], 40)}"')
        s += " + Enter" if st["press_enter"] else ""
    elif t == "key":
        s = st["keys"]
    elif t == "wait_for":
        s = f'{st["selector"]}  (max {st["timeout"]} s)'
    elif t == "click":
        s = st["selector"] + ("  + wait until the page stops changing"
                              if st["wait_answer"] else "")
    elif t == "select":
        s = f'{st["selector"]} = "{short(st["option"], 40)}"'
    elif t == "scroll":
        s = f'{st["selector"] or "page"} ({st["seconds"]} s)'
    elif t == "goto":
        s = st["url"]
    elif t == "run":
        s = short(st["command"])
    elif t == "secret":
        s = f'{short(st["command"], 50)}  ({st["prompt"]} asked at run time)'
    elif t == "manual":
        cond = {"enter": "press Enter in this console",
                "url": f"URL contains {st['value']}" if st["value"] else "target URL loaded",
                "selector": f"{st['value']} visible"}[st["until"]]
        s = f"{short(st['message'], 50)}  -> until {cond}"
    elif t == "wait":
        s = f"{st['seconds']} s"
    elif t == "title":
        s = f'"{short(st["text"], 50)}"  ({st["seconds"]} s)'
    else:
        s = ""
    return s


def show(sc, path, dirty):
    t = sc["target"]
    print("\n" + "=" * 78)
    print(f" Scenario: {path}" + ("   (modified, not saved)" if dirty else ""))
    if t["type"] == WEB:
        print(f" Target  : web  {t['url']}")
    elif t["type"] == VSCODE:
        where = f"wsl:{t['wsl_distro'] or 'default'}" if t["remote"] == "wsl" else "local"
        print(f" Target  : vscode  {where}  {t['folder']}")
    else:
        print(f" Target  : {t['type']}  (cwd: {t['cwd']})")
    o = sc["output"]
    outs = [x for x, on in ((".mp4", o["mp4"]), (".gif", o["gif"])) if on]
    print(f" Output  : videos/{o['name']}_<datetime>{' + '.join(outs)}  {o['width']}x{o['height']}")
    print("-" * 78)
    if not sc["steps"]:
        print("  (no steps - use 'a' to add one)")
    for i, st in enumerate(sc["steps"], 1):
        flags = " [hidden]" if st["hide"] else ""
        if t["type"] not in STEP_TYPES[st["type"]]["targets"]:
            flags += f" [!not for {t['type']}: skipped]"
        print(f" {i:>2}. {st['type']:<7} {describe_step(st)}{flags}")
    print("-" * 78)
    print(" r run | a [N] add | e N edit | d N delete | m N M move | c N copy |"
          " h N hide/show")
    print(" t target | o timing & output | i FILE import lines | s [FILE] save |"
          " l FILE load | n new | q quit")


def choose_step_type(target_type):
    types = [k for k, v in STEP_TYPES.items() if target_type in v["targets"]]
    for i, k in enumerate(types, 1):
        print(f"   {i}. {k:<7} {STEP_TYPES[k]['help']}")
    raw = input("  Step type (number or name): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(types):
        return types[int(raw) - 1]
    return raw if raw in types else None


def edit_target(sc):
    t = sc["target"]
    t["type"] = prompt_value(F("type", "Target type", "choice", WEB, list(TARGET_TYPES)),
                             t["type"])
    edit_fields(t, TARGET_FIELDS[target_group(t["type"])])


def parse_index(arg, sc, allow_end=False):
    try:
        n = int(arg)
    except (TypeError, ValueError):
        return None
    limit = len(sc["steps"]) + (1 if allow_end else 0)
    return n - 1 if 1 <= n <= limit else None


def menu(path):
    path = Path(path)
    sc = load_scenario(path)
    dirty = False
    while True:
        show(sc, path, dirty)
        try:
            cmd, _, arg = input("> ").strip().partition(" ")
        except EOFError:
            return
        args = arg.split()
        cmd = cmd.lower()
        steps = sc["steps"]
        try:
            if cmd == "q":
                if dirty and input("  Unsaved changes. Save first? (y/n) ").lower().startswith("y"):
                    save_scenario(sc, path)
                return
            elif cmd == "r":
                if dirty and input("  Save the scenario before running? (y/n) ").lower().startswith("y"):
                    save_scenario(sc, path)
                    dirty = False
                run_scenario(sc)
            elif cmd == "a":
                pos = parse_index(args[0], sc, allow_end=True) if args else len(steps)
                if pos is None:
                    print("  ! invalid position")
                    continue
                st_type = choose_step_type(sc["target"]["type"])
                if not st_type:
                    print("  ! unknown step type")
                    continue
                st = new_step(st_type)
                edit_fields(st, step_fields(st_type))
                steps.insert(pos, st)
                dirty = True
            elif cmd in ("e", "d", "c", "h"):
                i = parse_index(args[0] if args else None, sc)
                if i is None:
                    print("  ! give a valid step number, e.g. 'e 2'")
                    continue
                if cmd == "e":
                    print(f"  Editing step {i + 1} ({steps[i]['type']}) - Enter keeps "
                          "the value, '-' empties it")
                    edit_fields(steps[i], step_fields(steps[i]["type"]))
                elif cmd == "d":
                    steps.pop(i)
                elif cmd == "c":
                    steps.insert(i + 1, copy.deepcopy(steps[i]))
                else:
                    steps[i]["hide"] = not steps[i]["hide"]
                dirty = True
            elif cmd == "m":
                i = parse_index(args[0] if args else None, sc)
                j = parse_index(args[1] if len(args) > 1 else None, sc)
                if i is None or j is None:
                    print("  ! usage: m FROM TO, e.g. 'm 5 2'")
                    continue
                steps.insert(j, steps.pop(i))
                dirty = True
            elif cmd == "t":
                edit_target(sc)
                dirty = True
            elif cmd == "o":
                print("  -- Timing --")
                edit_fields(sc["timing"], TIMING_FIELDS)
                print("  -- Output --")
                edit_fields(sc["output"], OUTPUT_FIELDS)
                dirty = True
            elif cmd == "i":
                if not arg:
                    print("  ! usage: i FILE  (one question / command per line, "
                          "# = comment)")
                    continue
                kind = "ask" if sc["target"]["type"] in PAGES else "run"
                key = "text" if kind == "ask" else "command"
                n = 0
                for line in Path(arg.strip('"')).read_text(encoding="utf-8").splitlines():
                    if line.strip() and not line.lstrip().startswith("#"):
                        st = new_step(kind)
                        st[key] = line.strip()
                        steps.append(st)
                        n += 1
                print(f"  {n} '{kind}' steps imported")
                dirty = dirty or n > 0
            elif cmd == "s":
                if arg:
                    path = Path(arg.strip('"'))
                save_scenario(sc, path)
                dirty = False
                print(f"  saved to {path}")
            elif cmd == "l":
                if not arg:
                    print("  ! usage: l FILE")
                    continue
                if dirty and not input("  Discard unsaved changes? (y/n) ").lower().startswith("y"):
                    continue
                path = Path(arg.strip('"'))
                sc, dirty = load_scenario(path), False
            elif cmd == "n":
                if dirty and not input("  Discard unsaved changes? (y/n) ").lower().startswith("y"):
                    continue
                name = input("  New scenario file name (e.g. my-demo): ").strip() or "new"
                path = SCENARIO_DIR / (name if name.endswith(".json") else name + ".json")
                sc = normalize({})
                edit_target(sc)
                sc["output"]["name"] = path.stem
                dirty = True
            elif cmd:
                print("  ! unknown command")
        except KeyboardInterrupt:
            print("\n  (cancelled)")
        except (OSError, ValueError) as e:
            print(f"  ! {e}")


# --------------------------------------------------------------------------
# Run helpers
# --------------------------------------------------------------------------
Segment = namedtuple("Segment", "start end hidden")


def collect_secrets(sc):
    """Ask every secret before the recording starts. Returns {step index: value}."""
    secrets = {}
    for i, st in enumerate(sc["steps"]):
        if sc["target"]["type"] not in STEP_TYPES[st["type"]]["targets"]:
            continue
        if st["type"] == "secret":
            secrets[i] = getpass.getpass(f"  Step {i + 1} - {st['prompt']}: ")
        elif st["type"] == "type" and st["secret"]:
            secrets[i] = getpass.getpass(f"  Step {i + 1} - value for {st['selector']}: ")
    return secrets


def wait_for_enter(message, idle):
    """Wait for Enter in the console while idle() keeps the browser alive."""
    done = threading.Event()

    def reader():
        try:
            input(f"    {message}\n    >>> press Enter here to continue... ")
        except EOFError:
            pass
        done.set()

    threading.Thread(target=reader, daemon=True).start()
    while not done.is_set():
        idle()


def launch_browser(p, sc):
    o = sc["output"]
    kwargs = {"headless": HEADLESS}
    if o["browser_path"] and Path(o["browser_path"]).exists():
        kwargs["executable_path"] = o["browser_path"]
    browser = p.chromium.launch(**kwargs)
    viewport = {"width": o["width"], "height": o["height"]}
    context = browser.new_context(
        viewport=viewport,
        record_video_dir=str(VIDEO_DIR),
        record_video_size=viewport,
        ignore_https_errors=bool(sc["target"].get("ignore_https_errors", True)),
    )
    return browser, context


def compute_cuts(t0, segments):
    """Video intervals (s, relative to the recording start) to remove:
    everything before the first visible step (except 1 s) + hidden steps."""
    visible = [s for s in segments if not s.hidden]
    if not visible:
        return []
    cuts = []
    first = visible[0].start - t0
    if first > 1:
        cuts.append((0.0, first - 1))
    cuts += [(max(s.start - t0, 0.0), s.end - t0) for s in segments if s.hidden]
    cuts = sorted((a, b) for a, b in cuts if b > a)
    merged = []
    for a, b in cuts:
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def run_scenario(sc):
    ttype = sc["target"]["type"]
    skipped = [i + 1 for i, st in enumerate(sc["steps"])
               if ttype not in STEP_TYPES[st["type"]]["targets"]]
    if skipped:
        print(f"  ! steps {skipped} do not apply to a {ttype} target and will be skipped")
    if not any(not st["hide"] for st in sc["steps"]):
        print("  ! no visible step: nothing to record")
        return
    secrets = collect_secrets(sc)
    VIDEO_DIR.mkdir(exist_ok=True)
    # e.g. videos/demo_2026-11-25_14-30-05.mp4 - each run gets its own files
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = VIDEO_DIR / f"{sc['output']['name']}_{stamp}"
    raw = base.with_name(base.name + "_raw.webm")
    runner = {WEB: run_web, VSCODE: run_vscode}.get(ttype, run_terminal)
    if ttype == VSCODE:
        raw = raw.with_suffix(".mkv")  # encoded live from the DevTools screencast
    print("\n>>> Recording - Ctrl+C stops the scenario and keeps what was recorded\n")
    cuts = runner(sc, secrets, raw)
    if raw.exists():
        convert(raw, cuts, sc["output"], base)
    else:
        print("!!! No video recorded.")


def run_steps(sc, execute):
    """Run every applicable step with execute(index, step); return the segments."""
    segments = []
    ttype = sc["target"]["type"]
    steps = sc["steps"]
    try:
        for i, st in enumerate(steps):
            if ttype not in STEP_TYPES[st["type"]]["targets"]:
                continue
            tag = " (hidden)" if st["hide"] else ""
            print(f">>> [{i + 1}/{len(steps)}] {st['type']}: {describe_step(st)}{tag}")
            start = time.time()
            try:
                execute(i, st)
            except PlaywrightError as e:
                print(f"    ! error on this step, continuing: {short(e, 300)}")
            segments.append(Segment(start, time.time(), st["hide"]))
    except KeyboardInterrupt:
        print("\n>>> Stopped by user - exporting what was recorded")
    except TimeoutError as e:
        print(f"\n!!! {e} - stopping the scenario")
    return segments


# --------------------------------------------------------------------------
# Web target
# --------------------------------------------------------------------------
def url_matches(page_url, value, target_url):
    if value:
        return value in page_url
    cur, tgt = urlparse(page_url), urlparse(target_url)
    return (cur.hostname == tgt.hostname
            and cur.path.rstrip("/").startswith(tgt.path.rstrip("/")))


def search_frames(page, all_frames, skip_main=False):
    """Frames to look into: the page only, or (VS Code) the webviews first."""
    if not all_frames:
        return [page.main_frame]
    frames = [f for f in page.frames if f != page.main_frame]
    return frames if skip_main else frames + [page.main_frame]


def find_input(page, selectors, all_frames=False, skip_main=False):
    """Return the first visible + enabled chat input locator, or None."""
    for frame in search_frames(page, all_frames, skip_main):
        for sel in selectors:
            try:
                loc = frame.locator(sel)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible() and el.is_enabled():
                        return el
            except PlaywrightError:
                continue  # frame detached / navigating
    return None


def answer_count(page, selector):
    if not selector:
        return 0
    try:
        return page.locator(selector).count()
    except PlaywrightError:
        return 0


def body_text(page, all_frames=False):
    parts = []
    for frame in search_frames(page, all_frames):
        try:
            parts.append(frame.locator("body").inner_text(timeout=5000))
        except PlaywrightError:
            pass
    return "\n".join(parts)


def wait_for_answer(page, sc, previous_count, previous_text, scroll=True):
    """Wait until a new answer appears and its text stops changing (streaming done).
    Uses answer_selector when it matches, otherwise watches the whole page text
    (VS Code: the workbench and its webviews)."""
    tm, sel = sc["timing"], sc["target"]["answer_selector"]
    all_frames = sc["target"]["type"] == VSCODE
    deadline = time.time() + tm["max_wait_per_answer"]
    use_selector = None
    last_text, stable_since, started = None, time.time(), False
    while time.time() < deadline:
        if use_selector is None:
            if answer_count(page, sel) > previous_count:
                use_selector = True
            elif body_text(page, all_frames) != previous_text:
                use_selector = answer_count(page, sel) > previous_count
        if use_selector:
            try:
                text = page.locator(sel).last.inner_text(timeout=5000)
            except PlaywrightError:
                text = ""
        else:
            text = body_text(page, all_frames)
        if text != last_text:
            if last_text is not None:
                started = True
            last_text, stable_since = text, time.time()
        elif started and time.time() - stable_since >= tm["answer_stable_for"]:
            return True
        if scroll:
            page.mouse.wheel(0, 2000)  # keep the newest text in view
        page.wait_for_timeout(300)
    print(f"    ! no complete answer after {tm['max_wait_per_answer']}s, moving on")
    return False


def page_executor(sc, secrets, context, state):
    """Step runner shared by the web and VS Code targets.
    state["page"] = current page, state["recorded"] = page shown in the video.
    Returns execute(index, step)."""
    t, tm = sc["target"], sc["timing"]
    delay = tm["typing_delay_ms"]
    vscode = t["type"] == VSCODE

    def idle():
        pages = [pg for pg in context.pages if not pg.is_closed()]
        if pages:
            pages[0].wait_for_timeout(300)
        else:
            time.sleep(0.3)

    def switch_to(pg):
        if pg is not state["page"]:
            print(f"    -> continuing in tab {pg.url}")
        state["page"] = pg
        pg.bring_to_front()

    def manual(st):
        print(f"    {st['message']}")
        if st["until"] == "enter":
            wait_for_enter("When done:", idle)
            live = [pg for pg in context.pages if not pg.is_closed()]
            if state["page"].is_closed() and live:
                switch_to(live[-1])
            return
        timeout = tm["manual_timeout"]
        deadline = time.time() + timeout if timeout else None
        matched_since, last_urls = {}, None
        print("    (waiting automatically" + ("" if vscode else ", every tab is watched") + ")")
        while True:
            pages = [state["page"]] if vscode else \
                [pg for pg in context.pages if not pg.is_closed()]
            urls = [pg.url for pg in pages]
            if urls != last_urls and not vscode:
                print("    open tabs:", urls)
                last_urls = urls
            for pg in pages:
                try:
                    if st["until"] == "url":
                        ok = url_matches(pg.url, st["value"], t["url"])
                    else:
                        ok = pg.locator(st["value"]).first.is_visible()
                except PlaywrightError:
                    ok = False  # page is navigating, retry
                if not ok:
                    matched_since.pop(pg, None)
                    continue
                # must stay matched 2 s (ignores JS redirects to the login page)
                if time.time() - matched_since.setdefault(pg, time.time()) >= 2:
                    switch_to(pg)
                    return
            if deadline and time.time() > deadline:
                raise TimeoutError(f"manual step not completed after {timeout}s")
            idle()

    def ask(st):
        page = state["page"]
        chat_input, end = None, time.time() + 30
        while not chat_input and time.time() < end:
            # VS Code: chat UIs are webviews (iframes), the workbench has many other
            # text fields: look in the webviews only for the first 10 s
            webviews_only = vscode and time.time() < end - 20
            chat_input = find_input(page, t["input_selectors"], vscode, webviews_only)
            if not chat_input:
                page.wait_for_timeout(500)
        if not chat_input:
            print("    ! no chat input found (check input_selectors in 't')")
            return
        previous_count = answer_count(page, t["answer_selector"])
        chat_input.click()
        chat_input.press_sequentially(st["text"], delay=delay)
        previous_text = body_text(page, vscode)
        page.wait_for_timeout(500)
        if t["send_selector"]:
            page.click(t["send_selector"])
        else:
            chat_input.press("Enter")
        wait_for_answer(page, sc, previous_count, previous_text, scroll=not vscode)
        page.wait_for_timeout(tm["pause_after_step"] * 1000)

    def execute(i, st):
        if not st["hide"] and state["recorded"] is None:
            state["recorded"] = state["page"]
        page, kind = state["page"], st["type"]
        if kind == "manual":
            manual(st)
        elif kind == "ask":
            ask(st)
        elif kind == "type":
            text = secrets.get(i, st["text"])
            if st["selector"]:
                field = page.locator(st["selector"]).first
                field.click()
                field.press_sequentially(text, delay=delay)
                if st["press_enter"]:
                    field.press("Enter")
            else:  # type in the focused element (e.g. after a shortcut)
                page.keyboard.type(text, delay=delay)
                if st["press_enter"]:
                    page.keyboard.press("Enter")
            page.wait_for_timeout(800)
        elif kind == "key":
            for key in st["keys"].split():
                page.keyboard.press(key)
                page.wait_for_timeout(300)
            page.wait_for_timeout(800)
        elif kind == "click":
            previous_count = answer_count(page, t["answer_selector"])
            previous_text = body_text(page, vscode)
            page.locator(st["selector"]).first.click()
            if st["wait_answer"]:
                wait_for_answer(page, sc, previous_count, previous_text, scroll=False)
            page.wait_for_timeout(800)
        elif kind == "wait_for":
            page.locator(st["selector"]).first.wait_for(state="visible",
                                                        timeout=st["timeout"] * 1000)
            page.wait_for_timeout(500)
        elif kind == "select":
            field = page.locator(st["selector"]).first
            field.scroll_into_view_if_needed()
            field.select_option(label=st["option"])
            page.wait_for_timeout(800)
        elif kind == "scroll":
            page.evaluate(SCROLL_JS, [st["selector"], st["seconds"] * 1000])
            page.wait_for_timeout(tm["pause_after_step"] * 1000)
        elif kind == "goto":
            page.goto(st["url"])
        elif kind == "title":
            show_title(page, st)
        elif kind == "wait":
            page.wait_for_timeout(st["seconds"] * 1000)
        if state["recorded"] is not None and state["page"] is not state["recorded"]:
            print("    ! the demo moved to another tab: only the first tab "
                  "is in the video")

    return execute


def run_web(sc, secrets, raw):
    t = sc["target"]
    created = {}  # page -> time its video started

    with sync_playwright() as p:
        browser, context = launch_browser(p, sc)
        context.on("page", lambda pg: created.setdefault(pg, time.time()))
        state = {"page": context.new_page(), "recorded": None}
        created.setdefault(state["page"], time.time())
        execute = page_executor(sc, secrets, context, state)

        segments = []
        try:
            if t["url"]:
                try:
                    state["page"].goto(t["url"])  # may redirect to a login page
                except PlaywrightError as e:
                    print(f"    ! could not open {t['url']}: {short(e, 200)}")
            segments = run_steps(sc, execute)
        finally:
            recorded = state["recorded"]
            video = recorded.video if recorded else None
            context.close()  # finalizes all .webm files
            if video:
                video.save_as(str(raw))
            browser.close()
        # remove the other tabs' videos (login pages, credentials typed)
        for f in VIDEO_DIR.glob("*.webm"):
            if f != raw and f.name.endswith(".webm") and not f.name.endswith("_raw.webm"):
                f.unlink(missing_ok=True)
        t0 = created.get(recorded, segments[0].start if segments else 0)
        return compute_cuts(t0, segments)


# --------------------------------------------------------------------------
# VS Code target (Electron window driven over the DevTools protocol)
# --------------------------------------------------------------------------
# Settings of the demo profile, written once (edit the file afterwards):
# no welcome page, trust prompt, tips, release notes, Copilot chat...
DEMO_VSCODE_SETTINGS = {
    "security.workspace.trust.enabled": False,
    "workbench.startupEditor": "none",
    "workbench.tips.enabled": False,
    "workbench.enableExperiments": False,
    "workbench.secondarySideBar.defaultVisibility": "hidden",
    "window.restoreWindows": "none",
    "update.mode": "none",
    "update.showReleaseNotes": False,
    "extensions.autoUpdate": False,
    "extensions.ignoreRecommendations": True,
    "telemetry.telemetryLevel": "off",
    "chat.disableAIFeatures": True,
}


def find_code_exe(path):
    if path and path.lower() != "auto":
        return path if Path(path).exists() else None
    candidates = []
    cli = shutil.which("code")  # ...\Microsoft VS Code\bin\code.cmd
    if cli:
        candidates.append(Path(cli).resolve().parent.parent / "Code.exe")
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" /
                          "Microsoft VS Code" / "Code.exe")
    if os.environ.get("ProgramFiles"):
        candidates.append(Path(os.environ["ProgramFiles"]) / "Microsoft VS Code" / "Code.exe")
    return next((str(c) for c in candidates if c.exists()), None)


def vscode_env():
    """Environment without the variables of a VS Code we may be running in."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("VSCODE_") and k != "ELECTRON_RUN_AS_NODE"}


def code_cli(exe, ext_dir, *args):
    cli = Path(exe).parent / "bin" / ("code.cmd" if os.name == "nt" else "code")
    return subprocess.run([str(cli), "--extensions-dir", str(ext_dir), *args],
                          capture_output=True, text=True, env=vscode_env(), timeout=300)


def print_output(r):
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        print("    | " + out.replace("\n", "\n    | "))


def fix_line_endings(ext_dir):
    """The Remote - WSL extension runs its scripts/*.sh inside Linux: with CRLF
    line endings (git core.autocrlf on a committed profile) they fail with
    "/usr/bin/env: 'sh\\r'" and VS Code only says "WSL Daemon exited with code 0"."""
    for script in ext_dir.glob("ms-vscode-remote.remote-wsl-*/scripts/*.sh"):
        content = script.read_bytes()
        if b"\r\n" in content:
            script.write_bytes(content.replace(b"\r\n", b"\n"))
            print(f"    fixed CRLF line endings in {script.name}")


def prepare_vscode(t, exe, profile):
    """Demo profile (settings, Remote - WSL extension), extensions to uninstall.
    Returns (user data dir, extensions dir)."""
    data, ext_dir = profile / "data", profile / "extensions"
    settings = data / "User" / "settings.json"
    if not settings.exists():
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(DEMO_VSCODE_SETTINGS, indent=2), encoding="utf-8")
        print(f"    demo VS Code profile created in {profile}")
    ext_dir.mkdir(parents=True, exist_ok=True)
    if t["remote"] == "wsl" and not any(ext_dir.glob("ms-vscode-remote.remote-wsl-*")):
        print("    installing the Remote - WSL extension in the demo profile...")
        print_output(code_cli(exe, ext_dir, "--install-extension",
                              "ms-vscode-remote.remote-wsl"))
    if t["remote"] == "wsl":
        fix_line_endings(ext_dir)
    ids = [e for e in t["uninstall_first"] if e]
    if ids:
        print(f"    uninstalling {', '.join(ids)} (the demo installs it again)")
        if t["remote"] == "wsl":
            # all VS Code servers of the distribution share ~/.vscode-server/extensions
            script = ('for s in ~/.vscode-server/bin/*/bin/code-server '
                      '~/.vscode-server/cli/servers/*/server/bin/code-server; do '
                      '[ -x "$s" ] || continue; for e in "$@"; do '
                      '"$s" --uninstall-extension "$e"; done; break; done')
            distro = ["-d", t["wsl_distro"]] if t["wsl_distro"] else []
            print_output(subprocess.run(["wsl.exe", *distro, "-e", "bash", "-c", script,
                                         "_", *ids],
                                        capture_output=True, text=True, timeout=300))
        else:
            print_output(code_cli(exe, ext_dir, *[a for e in ids
                                                  for a in ("--uninstall-extension", e)]))
    return data, ext_dir


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch_vscode(p, sc):
    """Start VS Code with a DevTools port and connect Playwright to its window.
    Returns (process, browser, workbench page)."""
    t, o = sc["target"], sc["output"]
    exe = find_code_exe(t["code_path"])
    if not exe:
        raise RuntimeError("VS Code not found (set 'code_path' in the target)")
    profile = Path(t["profile_dir"] or "vscode-demo-profile")
    profile = profile if profile.is_absolute() else SCRIPT_DIR / profile
    data, ext_dir = prepare_vscode(t, exe, profile)
    port = free_port()
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={data}",
            f"--extensions-dir={ext_dir}", "--new-window", "--skip-welcome",
            "--skip-release-notes", "--disable-workspace-trust"]
    if t["folder"]:
        if t["remote"] == "wsl":
            distro = t["wsl_distro"] or subprocess.run(
                ["wsl.exe", "-e", "sh", "-c", "echo $WSL_DISTRO_NAME"],
                capture_output=True, text=True, timeout=120).stdout.strip()
            folder = "/" + t["folder"].lstrip("/")
            args += ["--folder-uri", f"vscode-remote://wsl+{distro}{folder}"]
        else:
            args.append(t["folder"])
    print(f"    starting {' '.join(args)}")
    proc = subprocess.Popen(args, env=vscode_env(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    browser, deadline = None, time.time() + 90
    while not browser:
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except PlaywrightError:
            if time.time() > deadline or proc.poll() is not None:
                close_vscode(proc)
                raise RuntimeError("could not connect to VS Code (is another VS Code "
                                   "already using this profile_dir?)")
            time.sleep(1)
    page = None
    while not page:  # the workbench window, not the shared / utility processes
        page = next((pg for ctx in browser.contexts for pg in ctx.pages
                     if "workbench" in pg.url), None)
        if not page:
            if time.time() > deadline:
                raise RuntimeError("VS Code window not found")
            time.sleep(0.5)
    page.locator(".monaco-workbench").first.wait_for(timeout=60000)
    resize_window(page, o["width"], o["height"])
    return proc, browser, page


def resize_window(page, width, height):
    """Best effort: give the window the size of the video (sharpest result)."""
    try:
        cdp = page.context.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": win,
                                             "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {
            "left": 40, "top": 40, "width": width, "height": height}})
        cdp.detach()
    except PlaywrightError as e:
        print(f"    ! could not resize the VS Code window: {short(e, 150)}")


class ScreencastRecorder:
    """Record a window Playwright did not open (VS Code) through the DevTools
    screencast. ffmpeg encodes it live at a constant frame rate: the last frame
    is repeated while the screen does not change."""

    def __init__(self, page, out_path, width, height, fps=25):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg is needed to record VS Code "
                               "(winget install ffmpeg, or pip install imageio-ffmpeg)")
        self.fps, self.frame, self.t0 = fps, None, None
        self.stopped = threading.Event()
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
        self.proc = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(fps), "-i", "-",
             "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
             str(out_path)], stdin=subprocess.PIPE)
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90,
                                               "maxWidth": width, "maxHeight": height})
        self.thread = threading.Thread(target=self._writer, daemon=True)
        self.thread.start()

    def _on_frame(self, params):
        self.frame = base64.b64decode(params["data"])
        if self.t0 is None:
            self.t0 = time.time()
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except PlaywrightError:
            pass

    def _writer(self):
        written = 0
        while not self.stopped.is_set():
            if self.t0 is not None:
                due = int((time.time() - self.t0) * self.fps) + 1
                try:
                    while written < due:
                        self.proc.stdin.write(self.frame)
                        written += 1
                except OSError:
                    return
            time.sleep(0.5 / self.fps)

    def stop(self):
        try:
            self.cdp.send("Page.stopScreencast")
        except PlaywrightError:
            pass
        self.stopped.set()
        self.thread.join(timeout=5)
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait(timeout=120)


def close_vscode(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":  # the whole tree: renderers, extension host...
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    else:
        proc.terminate()


def run_vscode(sc, secrets, raw):
    o = sc["output"]
    with sync_playwright() as p:
        proc, browser, page = launch_vscode(p, sc)
        state = {"page": page, "recorded": None}
        execute = page_executor(sc, secrets, page.context, state)
        recorder, segments, t0 = None, [], None
        try:
            recorder = ScreencastRecorder(page, raw, o["width"], o["height"])
            segments = run_steps(sc, execute)
        finally:
            if recorder:
                recorder.stop()
                t0 = recorder.t0
            browser.close()  # only disconnects from VS Code
            if sc["target"]["close_at_end"]:
                close_vscode(proc)
        if t0 is None:
            raw.unlink(missing_ok=True)
            return []
        return compute_cuts(t0, segments)


# --------------------------------------------------------------------------
# Terminal target (PowerShell / bash rendered in the browser)
# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]")

SHELL_THEMES = {
    "powershell": dict(title="Windows PowerShell", prompt="PS {cwd}> ", bg="#012456",
                       fg="#eeedf0", p="#eeedf0", c="#f9f1a5"),
    "bash": dict(title="bash", prompt="user@demo:{cwd}$ ", bg="#1e1e1e", fg="#cccccc",
                 p="#16c60c", c="#ffffff"),
}

TERMINAL_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:BG}
#win{position:absolute;inset:0;display:flex;flex-direction:column;background:BG}
#bar{height:32px;flex:none;background:#1f1f1f;color:#ddd;display:flex;align-items:center;
  padding:0 14px;font:13px "Segoe UI",Arial,sans-serif}
#bar .t{flex:1} #bar .w{letter-spacing:22px;color:#aaa}
#screen{flex:1;margin:0;padding:10px 14px;overflow:hidden;color:FG;
  font:FONTpx/1.35 "Cascadia Mono",Consolas,"Courier New",monospace;
  white-space:pre-wrap;word-break:break-all}
.p{color:PCOL} .c{color:CCOL}
#cur{display:inline-block;width:.6em;background:FG;animation:b 1s steps(1) infinite}
@keyframes b{50%{opacity:0}}
</style></head><body><div id="win"><div id="bar"><span class="t">TITLE</span>
<span class="w">&#8212;&#9744;&#10005;</span></div><pre id="screen"></pre></div>
<script>
const scr = document.getElementById('screen');
const cursor = document.createElement('span'); cursor.id = 'cur'; cursor.innerHTML = '&nbsp;';
let line;
function newLine() { line = document.createElement('span'); scr.appendChild(line); }
newLine();
window.term = {
  write(text, cls) {
    for (const part of text.split(/(\\r\\n|\\n|\\r)/)) {
      if (part === '\\n' || part === '\\r\\n') { line.appendChild(document.createTextNode('\\n')); newLine(); }
      else if (part === '\\r') { line.textContent = ''; }
      else if (part) { const s = document.createElement('span'); if (cls) s.className = cls;
                       s.textContent = part; line.appendChild(s); }
    }
    while (scr.childNodes.length > 3000) scr.removeChild(scr.firstChild);
    line.appendChild(cursor);
    scr.scrollTop = scr.scrollHeight;
  },
  clear() { scr.textContent = ''; newLine(); line.appendChild(cursor); },
};
term.write('', '');
</script></body></html>"""


def git_bash():
    git = shutil.which("git")
    candidates = [Path(git).resolve().parent.parent / "usr" / "bin" / "bash.exe"] if git else []
    candidates.append(Path(r"C:\Program Files\Git\usr\bin\bash.exe"))
    return next((str(c) for c in candidates if c.exists()), None)


def bash_command(t, cwd, profile):
    """Return (exe, argv) for the bash selected in the target."""
    choice = (t.get("shell_exe") or "auto").strip()
    user = (t.get("user") or "").strip()
    flags = ["--noediting", "-li"] if profile else ["--noprofile", "--norc"]
    if choice.lower() == "wsl":
        exe = shutil.which("wsl")
        args = [exe or "wsl.exe"]
        if t.get("wsl_distro"):
            args += ["-d", t["wsl_distro"]]
        if user:
            args += ["-u", user]
        return exe, args + ["--cd", str(cwd), "--", "bash", *flags]
    if choice.lower() == "git":
        exe = git_bash()
    elif choice.lower() == "auto":
        exe = shutil.which("bash")
    else:
        exe = choice if Path(choice).exists() else shutil.which(choice)
    if user and os.name != "nt":
        return exe, ["sudo", "-u", user, "--", exe, *flags]
    if user:
        print(f"    ! 'user' is only used with shell_exe = wsl (or on Linux), ignored")
    return exe, [exe, *flags]


class Shell:
    """A persistent PowerShell / bash process. Each command is followed by a
    marker echo, so we know exactly when it has finished (and the new cwd)."""

    def __init__(self, kind, cwd, target=None):
        self.kind = kind
        t = target or {}
        profile = bool(t.get("load_profile"))
        env = dict(os.environ)
        if kind == "powershell":
            exe = shutil.which("pwsh") or shutil.which("powershell")
            args = [exe, "-NoLogo", *([] if profile else ["-NoProfile"]),
                    "-ExecutionPolicy", "Bypass", "-Command", "-"]
        else:
            exe, args = bash_command(t, cwd, profile)
            env["CHERE_INVOKING"] = "1"  # Git Bash login shell: stay in cwd, not $HOME
        if not exe:
            raise RuntimeError(f"{kind} not found (check 'shell_exe' in the target)")
        print(f"    shell: {' '.join(args)}")
        if kind == "powershell" and os.name == "nt":
            # Python tools (openstack CLI...) print in the same code page as PowerShell
            env.setdefault("PYTHONIOENCODING", f"cp{ctypes.windll.kernel32.GetConsoleOutputCP() or ctypes.windll.kernel32.GetOEMCP()}")
        else:
            env.setdefault("PYTHONIOENCODING", "utf-8")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, cwd=str(cwd), env=env)
        self.q = queue.Queue()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.buf, self.marker, self.cwd = "", None, str(cwd)
        self.stdin_encoding = "utf-8"
        threading.Thread(target=self._reader, daemon=True).start()
        if kind == "powershell":
            # Windows PowerShell reads stdin / writes stdout in the console code
            # pages (e.g. cp850), whatever [Console]::OutputEncoding says
            cps = self.run_silent("[Console]::InputEncoding.CodePage; "
                                  "[Console]::OutputEncoding.CodePage").split()
            if len(cps) == 2 and all(c.isdigit() for c in cps):
                try:
                    self.stdin_encoding = codecs.lookup(f"cp{cps[0]}").name
                    self.decoder = codecs.getincrementaldecoder(f"cp{cps[1]}")(
                        errors="replace")
                except LookupError:
                    pass
            self.run_silent("$ProgressPreference = 'SilentlyContinue'")
        else:
            # an interactive shell (-i, needed so ~/.bashrc is read) prints its
            # prompt on stderr: blank it (also after a venv "activate" changes it),
            # and disable "!" history expansion
            self.run_silent("stty -echo 2>/dev/null; "
                            "PS1=''; PS2=''; PROMPT_COMMAND=\"PS1=''\"; set +H 2>/dev/null; "
                            "export TERM=dumb", timeout=120)

    def _reader(self):
        while True:
            data = self.proc.stdout.read1(4096)
            if not data:
                self.q.put(None)
                return
            self.q.put(data)

    def start(self, command):
        self.marker = f"__DEMO_DONE_{uuid.uuid4().hex[:10]}__"
        # split in the command text, so a shell echoing its input (WSL gives
        # bash a pseudo-terminal) is not mistaken for the end marker
        m1, m2 = self.marker[:7], self.marker[7:]
        if self.kind == "powershell":
            end = f'Write-Output ("{m1}" + "{m2}" + (Get-Location).Path)'
        else:
            end = f'echo "{m1}""{m2}$PWD"'
        self.proc.stdin.write((command + "\n" + end + "\n").encode(self.stdin_encoding,
                                                                    errors="replace"))
        self.proc.stdin.flush()

    def poll(self):
        """Return (new output text, finished)."""
        exited = False
        while True:
            try:
                data = self.q.get_nowait()
            except queue.Empty:
                break
            if data is None:
                exited = True
                break
            self.buf += self.decoder.decode(data)
        idx = self.buf.find(self.marker)
        if idx >= 0:
            nl = self.buf.find("\n", idx)
            if nl >= 0:
                text = self.buf[:idx]
                self.cwd = self.buf[idx + len(self.marker):nl].strip() or self.cwd
                self.buf = self.buf[nl + 1:]
                return ANSI_RE.sub("", text), True
            text, self.buf = self.buf[:idx], self.buf[idx:]
            return ANSI_RE.sub("", text), exited
        # keep back a possible beginning of the marker
        keep = 0
        for k in range(min(len(self.marker) - 1, len(self.buf)), 0, -1):
            if self.marker.startswith(self.buf[-k:]):
                keep = k
                break
        text = self.buf[: len(self.buf) - keep]
        self.buf = self.buf[len(self.buf) - keep:]
        if exited:
            text, self.buf = text + self.buf, ""
        return ANSI_RE.sub("", text), exited

    def run_silent(self, command, timeout=60):
        self.start(command)
        out, deadline = [], time.time() + timeout
        while time.time() < deadline:
            text, done = self.poll()
            out.append(text)
            if done:
                break
            time.sleep(0.05)
        return "".join(out)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


def run_terminal(sc, secrets, raw):
    t, tm = sc["target"], sc["timing"]
    theme = SHELL_THEMES[t["type"]]
    prompt_tpl = t["prompt"] or theme["prompt"]
    cwd = Path(t["cwd"] or ".")
    cwd = cwd if cwd.is_absolute() else (SCRIPT_DIR / cwd).resolve()
    print(f"    starting {t['type']} in {cwd}")
    shell = Shell(t["type"], cwd, t)

    html = TERMINAL_HTML
    for k, v in (("BG", theme["bg"]), ("FG", theme["fg"]), ("PCOL", theme["p"]),
                 ("CCOL", theme["c"]), ("FONT", str(t["font_size"]))):
        html = html.replace(k, v)
    html = html.replace("TITLE", (t["title"] or theme["title"])
                        .replace("&", "&amp;").replace("<", "&lt;"))

    with sync_playwright() as p:
        browser, context = launch_browser(p, sc)
        page = context.new_page()
        t0 = time.time()
        view = {"line_start": True}

        def write(text, cls=""):
            if text:
                page.evaluate("([t, c]) => term.write(t, c)", [text, cls])
                view["line_start"] = text.endswith("\n")

        def show_prompt(redraw=False):
            if redraw:
                write("\r")
            elif not view["line_start"]:
                write("\n")
            write(prompt_tpl.replace("{cwd}", shell.cwd), "p")

        def run_command(real, shown, hidden, timeout):
            old_cwd = shell.cwd
            if not hidden:
                for ch in shown:
                    write(ch, "c")
                    page.wait_for_timeout(tm["typing_delay_ms"])
                page.wait_for_timeout(400)
                write("\n")
            shell.start(real)
            deadline = time.time() + timeout
            while True:
                text, done = shell.poll()
                if text:
                    if hidden:
                        print("    | " + text.rstrip().replace("\n", "\n    | "))
                    else:
                        write(text)
                if done:
                    break
                if time.time() > deadline:
                    print(f"    ! still running after {timeout}s, moving on")
                    break
                page.wait_for_timeout(50)
            if hidden:
                if shell.cwd != old_cwd:
                    show_prompt(redraw=True)
                return
            show_prompt()
            page.wait_for_timeout(tm["pause_after_step"] * 1000)

        def execute(i, st):
            kind = st["type"]
            if kind == "run":
                run_command(st["command"], st["command"], st["hide"], st["timeout"])
            elif kind == "secret":
                real = st["command"].replace("{value}", secrets.get(i, ""))
                shown = st["command"].replace("{value}", "********")
                run_command(real, shown, st["hide"], 60)
            elif kind == "clear":
                page.evaluate("() => term.clear()")
                view["line_start"] = True
                show_prompt()
            elif kind == "manual":
                print(f"    {st['message']}")
                wait_for_enter("When done:", lambda: page.wait_for_timeout(300))
            elif kind == "title":
                show_title(page, st)
            elif kind == "wait":
                page.wait_for_timeout(st["seconds"] * 1000)

        try:
            page.set_content(html)
            show_prompt()
            page.wait_for_timeout(1000)
            segments = run_steps(sc, execute)
        finally:
            video = page.video
            context.close()
            if video:
                video.save_as(str(raw))
            browser.close()
            shell.close()
        for f in VIDEO_DIR.glob("*.webm"):
            if not f.name.endswith("_raw.webm"):
                f.unlink(missing_ok=True)
        return compute_cuts(t0, segments)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------
def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


def convert(webm, cuts, out, name):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print(f"!!! ffmpeg not found - raw video kept at {webm}")
        print("    Install it (winget install ffmpeg, or pip install imageio-ffmpeg).")
        return
    # constant 25 fps, then drop the hidden / login parts and close the gaps
    base = "fps=25"
    if cuts:
        expr = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in cuts)
        base += f",select='not({expr})',setpts=N/25/TB"
        print("    cut from the video (s): " +
              ", ".join(f"{a:.1f}-{b:.1f}" for a, b in cuts))
    quiet = ["-hide_banner", "-loglevel", "error"]
    done = []
    if out["mp4"]:  # best for LinkedIn: autoplays, sharp, small
        subprocess.run([ffmpeg, *quiet, "-y", "-i", str(webm), "-vf", base,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
                        f"{name}.mp4"], check=True)
        done.append(f"{name}.mp4")
    if out["gif"]:
        filters = (f"{base},setpts=PTS/{out['gif_speed']},fps={out['gif_fps']},"
                   f"scale={out['gif_width']}:-1:flags=lanczos")
        palette = VIDEO_DIR / "palette.png"
        subprocess.run([ffmpeg, *quiet, "-y", "-i", str(webm), "-vf",
                        f"{filters},palettegen=stats_mode=diff", str(palette)], check=True)
        subprocess.run([ffmpeg, *quiet, "-y", "-i", str(webm), "-i", str(palette),
                        "-lavfi", f"[0:v]{filters}[x];[x][1:v]paletteuse=dither=bayer:"
                        "bayer_scale=5", f"{name}.gif"], check=True)
        palette.unlink(missing_ok=True)
        done.append(f"{name}.gif")
    if out["delete_raw"] and done:
        webm.unlink(missing_ok=True)
    print("Done: " + (", ".join(done) if done else f"raw video only: {webm}"))


def main():
    global HEADLESS
    ap = argparse.ArgumentParser(description="Automate and record a web or terminal demo.")
    ap.add_argument("scenario", nargs="?", default=str(DEFAULT_SCENARIO),
                    help="scenario JSON file (default: scenarios/oracle.json)")
    ap.add_argument("--run", action="store_true", help="record directly, without the menu")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    HEADLESS = args.headless
    if args.run:
        run_scenario(load_scenario(args.scenario))
    else:
        menu(args.scenario)


if __name__ == "__main__":
    main()
