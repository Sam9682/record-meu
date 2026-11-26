# record-meu: scripted demo recorder

`record_demo.py` plays a scripted demo, records it, and exports a ready-to-share
**MP4** and/or **GIF**, for example for a LinkedIn post. It can drive:

| Target       | What is recorded                                                   |
|--------------|--------------------------------------------------------------------|
| `web`        | A web site in Chromium (chatbot, forms, documentation...)          |
| `powershell` | A real PowerShell session, rendered as a terminal window           |
| `bash`       | A real bash session (local, WSL or Git Bash), rendered as a terminal |

A demo is described by a **scenario**: a JSON file in `scenarios/` that holds a
target, timing and output settings, and an ordered list of **steps**.

## Installation

```bash
python -m venv venv
# Windows: .\venv\Scripts\Activate.ps1    Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # only if no local Chromium is set in output.browser_path
```

To convert the recording you need **ffmpeg**. The script uses the `ffmpeg` on
your PATH, or the copy bundled with `imageio-ffmpeg` if there isn't one. If it
finds neither, it keeps the raw `.webm` recording.

## Usage

```bash
python record_demo.py                                  # interactive menu on scenarios/oracle.json
python record_demo.py scenarios/openstack-powershell.json   # menu on another scenario
python record_demo.py scenarios/openstack-powershell.json --run   # record directly, no menu
```

Videos go to `videos/<name>_<YYYY-MM-DD_HH-MM-SS>.mp4` / `.gif`, so each run
gets its own files. Press **Ctrl+C** during a recording to stop the scenario
and keep what was recorded so far.

### Interactive menu

The menu shows the scenario and its numbered steps. It accepts these commands:

| Command      | Action                                                         |
|--------------|----------------------------------------------------------------|
| `r`          | Run (record) the scenario                                      |
| `a [N]`      | Add a step (at the end, or at position N)                      |
| `e N`        | Edit step N (Enter keeps a value, `-` empties it)              |
| `d N`        | Delete step N                                                  |
| `m N M`      | Move step N to position M                                      |
| `c N`        | Duplicate step N                                               |
| `h N`        | Toggle "hidden" on step N                                      |
| `t`          | Edit the target (type, URL, shell...)                          |
| `o`          | Edit timing and output settings                                |
| `i FILE`     | Import a text file: one question (web) or command (terminal) per line, `#` = comment |
| `s [FILE]`   | Save (optionally to another file)                              |
| `l FILE`     | Load another scenario                                          |
| `n`          | New scenario                                                   |
| `q`          | Quit                                                           |

## Scenario steps

| Step     | Targets  | Description                                                       |
|----------|----------|-------------------------------------------------------------------|
| `ask`    | web      | Type a question in the chat box, send it, wait until the answer is complete |
| `type`   | web      | Type text in a field; `secret: true` asks the value at run time   |
| `click`  | web      | Click an element, optionally wait until the page stops changing   |
| `select` | web      | Choose an option in a drop-down list                              |
| `scroll` | web      | Scroll slowly through an element or the page                      |
| `goto`   | web      | Open a URL in the current tab                                     |
| `run`    | terminal | Type a command, run it, wait until it finishes                    |
| `secret` | terminal | Run a command containing a secret asked at run time (`{value}`), displayed as `********` |
| `clear`  | terminal | Clear the terminal screen                                         |
| `manual` | all      | Pause while you act yourself (login, MFA...): continue on Enter, when the URL matches, or when a selector appears |
| `wait`   | all      | Wait a few seconds                                                |
| `title`  | all      | Show a full-screen title card (title + subtitle) for a few seconds |

Every step has a `hide` flag. A hidden step is executed but **cut out of the
final video**, which is useful for login pages or credential setup. Everything
recorded before the first visible step is also cut, except the last second.
Steps that don't apply to the current target type are skipped with a warning.

### Example

```json
{
  "target": { "type": "web", "url": "https://example.com/chat",
              "answer_selector": ".message.assistant" },
  "timing": { "typing_delay_ms": 45, "pause_after_step": 3 },
  "output": { "name": "demo", "mp4": true, "gif": true },
  "steps": [
    { "type": "manual", "message": "Log in, then press Enter", "until": "enter", "hide": true },
    { "type": "title",  "text": "Live demo", "subtitle": "Ask the oracle", "seconds": 4 },
    { "type": "ask",    "text": "What is OVH OPCP?" }
  ]
}
```

Missing keys get their default values when the file is loaded, so a
hand-written scenario only needs the fields it changes.

## Settings reference

**Target, web:** `url`, `input_selectors` (the first visible one is used),
`send_selector` (empty means press Enter), `answer_selector`, `ignore_https_errors`.

**Target, terminal:** `cwd` (relative to the script), `prompt` (`{cwd}`
placeholder), `title`, `font_size`, `load_profile` (load `~/.bashrc` / the
PowerShell profile). Bash only: `shell_exe` (`auto` | `wsl` | `git` | path to a
bash executable), `wsl_distro`, `user`.

**Timing:** `typing_delay_ms`, `pause_after_step` (time left for viewers to
read), `max_wait_per_answer` and `answer_stable_for` (web: an answer is
complete once it has stopped changing for this long), `manual_timeout`
(0 = wait forever).

**Output:** `name`, `width` / `height`, `mp4`, `gif`, `gif_speed`, `gif_fps`,
`gif_width`, `delete_raw` (delete the raw `.webm`, which may still show the
hidden steps), `browser_path` (empty means use Playwright's own Chromium).

## Secrets

Mark passwords and other secrets as secret so they're never stored in a
scenario. Secret values are asked in the console **before** the recording starts (`type` steps with `secret: true`
and `secret` steps). Credential scripts such as `creds-openstack*.{sh,ps1}`
are sourced by hidden steps. Keep them out of version control.

## Included scenarios

| File                                      | Target     | Content                                   |
|-------------------------------------------|------------|-------------------------------------------|
| `oracle.json`                             | web        | Questions to the OPCP oracle chatbot      |
| `opcp-documentation-augmented-olala.json` | web        | Login + OPCP "document adapter" (augmented documentation) |
| `openstack-powershell.json`               | powershell | OpenStack CLI commands from PowerShell    |
| `openstack-bash-opcp-simulator.json`      | bash       | OpenStack CLI against the OPCP simulator  |

## How it works

- **Web:** Playwright drives Chromium and records the viewport through its
  built-in video recording.
- **Terminals:** a persistent PowerShell or bash process runs the commands. A
  marker echoed after each command shows when it has finished. The output is
  streamed into a terminal look-alike HTML page that Playwright records.
- **Export:** each step is timed. ffmpeg then drops the hidden segments,
  encodes the MP4 (H.264, at 25 fps), and builds the GIF with a palette pass,
  sped up and scaled to `gif_width`.
