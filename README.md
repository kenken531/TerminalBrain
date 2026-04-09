# TerminalBrain 🧠

TerminalBrain is a **LLM-powered error watchdog for your terminal**: it wraps any command, captures its stdout and stderr live, and whenever an error appears it sends it to a local LLM and displays an inline fix suggestion — without interrupting the command's normal output. It's built for the **BUILDCORED ORCAS — Day 10** challenge.

## How it works

- Uses **subprocess.PIPE** to launch any command and capture its stdout and stderr as live byte streams.
- Runs **two separate reader threads** — one for stdout, one for stderr — so neither pipe blocks the other and no deadlock occurs.
- A third **watcher thread** monitors every stderr line for error keywords and Python traceback patterns.
- When an error is detected, it sends the error text plus recent output context to a **local LLM via ollama** and streams back a concise fix suggestion.
- Fix suggestions are displayed **inline using rich** — coloured panels that appear right below the error without interrupting normal output.
- **Error caching** prevents the LLM from being queried twice for the same error fingerprint within a session.

## Requirements

- Python 3.10.x
- [ollama](https://ollama.com/download) installed and running
- The model pulled locally (see Setup)

## Python packages:

```bash
pip install ollama rich
```

## Setup

1. Download and install ollama from [ollama.com/download](https://ollama.com/download).
2. In a **separate terminal**, start the ollama server:
```
ollama serve
```
3. Pull the model:
```
ollama pull qwen2.5:3b
```
4. Install the Python packages (see above or run:
```
pip install -r requirements.txt
```
after downloading `requirements.txt`)

## Usage

From the project folder:

```bash
python terminalbrain.py <your command here>
```

Examples:

```bash
python terminalbrain.py python myscript.py
python terminalbrain.py pip install some-package
python terminalbrain.py -- python -c "import nonexistent_module"
python terminalbrain.py --model llama3.2:3b python myscript.py
```

- Normal command output prints as usual.
- When an error is detected, a **yellow suggestion panel** appears inline.
- At exit, a summary shows how many unique errors were analysed.

## Options

| Flag | Default | Description |
|---|---|---|
| `--model` / `-m` | `qwen2.5:3b` | Ollama model to use for fix suggestions |

## Common fixes

**ollama not running** — open a separate terminal and run `ollama serve` before launching TerminalBrain.

**Model not pulled** — run `ollama pull qwen2.5:3b` and wait for the download to finish.

**No errors triggered** — run `python terminalbrain.py -- python -c "import nonexistent_module"` to force a `ModuleNotFoundError` and test the full pipeline.

**LLM too slow** — switch to a smaller model with `--model qwen2.5:1.5b`, or close other heavy applications to free RAM.

**Command not found** — make sure the wrapped command is installed and on your system PATH before passing it to TerminalBrain.

## Hardware concept

Hardware watchdog timers monitor a system's state and trigger a recovery handler when something fails — a microcontroller resets itself if its watchdog isn't fed within a deadline. TerminalBrain is the software equivalent: the wrapper is the watchdog, stderr is the heartbeat signal, and the LLM is the recovery handler. The same interrupt-driven feedback loop that keeps embedded firmware alive is what makes this work — errors interrupt the normal flow and dispatch to a handler that tries to recover.

## Credits

- Local LLM inference: [ollama](https://ollama.com)
- Terminal output formatting: [rich](https://github.com/Textualize/rich)
- Default model: [Qwen2.5 3B](https://ollama.com/library/qwen2.5)

Built as part of the **BUILDCORED ORCAS — Day 10: TerminalBrain** challenge.
