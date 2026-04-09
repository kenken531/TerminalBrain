"""
TerminalBrain  —  Windows Edition
===================================
Wraps any terminal command. Captures stdout and stderr live.
When an error appears, sends it to a local LLM and displays
an inline fix suggestion — without interrupting normal output.

Prerequisites:
    1. Install ollama:        https://ollama.com/download
    2. Start ollama server:   ollama serve   (separate terminal)
    3. Pull a model:          ollama pull qwen2.5:3b
    4. Install Python deps:   pip install ollama rich

Usage:
    python terminalbrain.py python myscript.py
    python terminalbrain.py pip install something-broken
    python terminalbrain.py -- python -c "import nonexistent_module"
    python terminalbrain.py --model llama3.2:3b npm install
"""

import argparse
import subprocess
import threading
import sys
import os
import time
import hashlib
from collections import deque
from queue import Queue, Empty

try:
    import ollama
except ImportError:
    print("ERROR: ollama not installed. Run: pip install ollama")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.rule import Rule
    from rich.live import Live
    from rich import print as rprint
except ImportError:
    print("ERROR: rich not installed. Run: pip install rich")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "qwen2.5:3b"

# How many lines of context to include with the error when querying the LLM
CONTEXT_LINES = 20

# Cache: don't re-query the LLM for the same error pattern twice
ERROR_CACHE: dict[str, str] = {}

# Keywords that indicate a line is an error worth investigating
ERROR_KEYWORDS = [
    "error", "exception", "traceback", "failed", "fatal",
    "cannot", "no such", "not found", "permission denied",
    "syntaxerror", "nameerror", "typeerror", "valueerror",
    "importerror", "modulenotfounderror", "attributeerror",
    "crash", "abort", "killed", "segfault",
]

# ── Console ───────────────────────────────────────────────────────────────────

console = Console(highlight=False)

def is_error_line(line: str) -> bool:
    """Return True if the line looks like an error worth sending to the LLM."""
    lower = line.lower()
    return any(kw in lower for kw in ERROR_KEYWORDS)


def error_fingerprint(error_text: str) -> str:
    """Hash the first 200 chars of an error for cache keying."""
    return hashlib.md5(error_text[:200].encode()).hexdigest()


def get_llm_suggestion(model: str, error_text: str, context: str) -> str:
    """
    Send the error + surrounding context to the LLM.
    Returns a concise fix suggestion as a string.
    Streams the response and collects it fully before returning.
    """
    prompt = (
        f"A terminal command produced this error:\n\n"
        f"=== CONTEXT (recent output) ===\n{context}\n\n"
        f"=== ERROR ===\n{error_text}\n\n"
        f"Give a SHORT, direct fix suggestion (2-4 sentences max). "
        f"Focus on the exact cause and the command or code change needed to fix it. "
        f"No preamble, no markdown headers, no lengthy explanation."
    )

    try:
        stream   = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        response = ""
        for chunk in stream:
            content = chunk.get("message", {}).get("content", "")
            response += content
        return response.strip()
    except ollama.ResponseError as e:
        return f"[LLM error: {e}]"
    except Exception as e:
        return f"[Could not reach ollama: {e} — is 'ollama serve' running?]"


def display_suggestion(error_line: str, suggestion: str, elapsed: float):
    """Render the LLM suggestion as a rich panel inline."""
    console.print()
    console.print(Rule("[bold red]⚡ TerminalBrain — Error Detected[/bold red]", style="red"))

    error_text = Text(error_line.strip(), style="bold red")
    console.print(Text("  Error: ", style="dim") + error_text)
    console.print()

    panel = Panel(
        suggestion,
        title="[bold yellow]💡 Suggested Fix[/bold yellow]",
        border_style="yellow",
        padding=(0, 2),
    )
    console.print(panel)
    console.print(Text(f"  (LLM responded in {elapsed:.1f}s)", style="dim"))
    console.print(Rule(style="red"))
    console.print()

# ── Stream reader threads ─────────────────────────────────────────────────────

def stream_reader(stream, label: str, line_queue: Queue, stop_event: threading.Event):
    """
    Reads lines from a subprocess stream (stdout or stderr) and:
      - Prints them immediately to the console (passthrough)
      - Puts them on the shared queue for error detection
    Runs on its own thread so stdout and stderr don't block each other.
    """
    try:
        for raw_line in iter(stream.readline, b""):
            if stop_event.is_set():
                break
            try:
                line = raw_line.decode("utf-8", errors="replace")
            except Exception:
                line = str(raw_line)

            # Passthrough — print to console immediately
            if label == "stderr":
                console.print(Text(line, style="red"), end="")
            else:
                console.print(line, end="")

            # Push to queue for error detection
            line_queue.put((label, line))
    except Exception:
        pass
    finally:
        line_queue.put((label, None))   # sentinel: stream ended


def error_watcher(line_queue: Queue, model: str, context_buf: deque):
    """
    Watches the shared line queue for error patterns.
    When an error is detected, queries the LLM and displays the suggestion inline.
    Runs on its own thread so it never blocks the stream readers.
    """
    stderr_done = False
    stdout_done = False

    # Accumulate multi-line error blocks (e.g. Python tracebacks)
    error_accumulator = []
    in_traceback      = False
    traceback_timeout = 0.0

    while not (stderr_done and stdout_done):
        try:
            label, line = line_queue.get(timeout=0.2)
        except Empty:
            # Flush pending traceback if we haven't seen new lines for 0.5s
            if in_traceback and time.time() - traceback_timeout > 0.5:
                _flush_error(error_accumulator, model, context_buf)
                error_accumulator = []
                in_traceback = False
            continue

        if line is None:
            if label == "stderr":
                stderr_done = True
            else:
                stdout_done = True
            # Flush any pending error
            if error_accumulator:
                _flush_error(error_accumulator, model, context_buf)
                error_accumulator = []
            continue

        # Update rolling context buffer (for all lines, not just errors)
        context_buf.append(line.rstrip())

        if label != "stderr":
            continue

        lower = line.lower().strip()

        # Detect start of Python traceback
        if "traceback (most recent call last)" in lower:
            in_traceback     = True
            traceback_timeout = time.time()
            error_accumulator = [line]
            continue

        if in_traceback:
            error_accumulator.append(line)
            traceback_timeout = time.time()
            # End of traceback: a line that doesn't start with whitespace
            # and isn't the "File" line = the actual error message
            if line.strip() and not line.startswith(" ") and not line.startswith("File"):
                _flush_error(error_accumulator, model, context_buf)
                error_accumulator = []
                in_traceback = False
            continue

        # Single-line errors (not part of a traceback)
        if is_error_line(line):
            _flush_error([line], model, context_buf)


def _flush_error(error_lines: list, model: str, context_buf: deque):
    """Send accumulated error lines to the LLM and display the result."""
    error_text = "".join(error_lines).strip()
    if not error_text:
        return

    fp = error_fingerprint(error_text)
    if fp in ERROR_CACHE:
        # Already seen this error — show cached suggestion
        console.print()
        console.print(Text("  [cached] ", style="dim yellow"), end="")
        display_suggestion(error_text.splitlines()[-1], ERROR_CACHE[fp], 0.0)
        return

    context = "\n".join(list(context_buf)[-CONTEXT_LINES:])

    t0         = time.time()
    suggestion = get_llm_suggestion(model, error_text, context)
    elapsed    = time.time() - t0

    ERROR_CACHE[fp] = suggestion
    display_suggestion(error_text.splitlines()[-1], suggestion, elapsed)

# ── Banner ────────────────────────────────────────────────────────────────────

def print_banner(command: list, model: str):
    console.print()
    console.print(Rule("[bold cyan]TerminalBrain[/bold cyan]", style="cyan"))
    console.print(f"  [bold]Command :[/bold] [yellow]{' '.join(command)}[/yellow]")
    console.print(f"  [bold]Model   :[/bold] [yellow]{model}[/yellow]")
    console.print(f"  [bold]Watching:[/bold] stderr for errors → LLM fix suggestions inline")
    console.print(Rule(style="cyan"))
    console.print()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TerminalBrain — LLM-powered error watchdog for terminal commands",
        usage="%(prog)s [--model MODEL] command [args...]",
    )
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL,
                        help=f"Ollama model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="Command to run and watch")
    args = parser.parse_args()

    # Strip leading '--' separator if present
    command = args.command
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        parser.print_help()
        sys.exit(1)

    # Verify ollama is reachable
    try:
        ollama.list()
    except Exception as e:
        console.print(f"[bold red]ERROR:[/bold red] Cannot reach ollama server.")
        console.print(f"  Run [yellow]ollama serve[/yellow] in a separate terminal first.")
        console.print(f"  Details: {e}")
        sys.exit(1)

    print_banner(command, args.model)

    # ── Launch subprocess ──
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,              # unbuffered — critical for live streaming
            shell=False,
        )
    except FileNotFoundError:
        console.print(f"[bold red]ERROR:[/bold red] Command not found: [yellow]{command[0]}[/yellow]")
        console.print("  Make sure the command is installed and on your PATH.")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]ERROR:[/bold red] Could not launch command: {e}")
        sys.exit(1)

    # ── Shared state ──
    line_queue   = Queue()
    stop_event   = threading.Event()
    context_buf  = deque(maxlen=CONTEXT_LINES * 2)

    # ── Start threads ──
    # Two separate reader threads: one for stdout, one for stderr.
    # They MUST be separate — reading both from one thread causes deadlock.
    t_stdout = threading.Thread(
        target=stream_reader,
        args=(proc.stdout, "stdout", line_queue, stop_event),
        daemon=True,
    )
    t_stderr = threading.Thread(
        target=stream_reader,
        args=(proc.stderr, "stderr", line_queue, stop_event),
        daemon=True,
    )
    t_watcher = threading.Thread(
        target=error_watcher,
        args=(line_queue, args.model, context_buf),
        daemon=True,
    )

    t_stdout.start()
    t_stderr.start()
    t_watcher.start()

    # ── Wait for command to finish ──
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        console.print("\n[dim]  Interrupted.[/dim]")

    stop_event.set()
    t_stdout.join(timeout=5)
    t_stderr.join(timeout=5)
    t_watcher.join(timeout=10)   # give LLM time to finish responding

    exit_code = proc.returncode
    console.print()
    console.print(Rule(style="dim"))

    if exit_code == 0:
        console.print(f"  [bold green]✓ Command finished successfully (exit 0)[/bold green]")
    else:
        console.print(f"  [bold red]✗ Command exited with code {exit_code}[/bold red]")

    if ERROR_CACHE:
        console.print(f"  [dim]{len(ERROR_CACHE)} unique error(s) analysed by LLM[/dim]")

    console.print()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()