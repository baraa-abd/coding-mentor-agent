# Coding Mentor Agent

## Requirements

- Python 3.10+
- One of:
  - [Ollama](https://ollama.com/) running locally (for local models)
  - An [Anthropic API key](https://console.anthropic.com/) (for Claude)
- [Docker](https://www.docker.com/)
- sentence-transformers (for an embeddings model)


## Usage

### Run with Ollama (local)

```bash
python mentor.py
```

To specify a model or a custom Ollama server URL:

```bash
python mentor.py --ollama-model llama3.1:8b --ollama-url http://localhost:11434
```

Default model: `qwen3-coder:30b`  
Default URL: `http://localhost:11434`


### Run with Claude (Anthropic)

```bash
python mentor.py --anthropic
```

To specify a model or pass an API key inline:

```bash
python mentor.py --anthropic --anthropic-model claude-opus-4-6 --anthropic-api-key sk-ant-...
```

Default model: `claude-sonnet-4-6`


### Reset memory

This permanently deletes everything in the `memory/` directory. You will be prompted to confirm.

```bash
python mentor.py --reset-memory
```

---


## Example Transcript

------------------------------------------------------------

Mentor: Hey there, welcome! I'm your coding mentor, here to help you learn, grow, and work through whatever coding challenges or topics you're curious about — so what would you like to dive into today?

You: I want to build parallelizable programs in Python. What topics should we start with? I am comfortable coding in Pytthon generally but I haven't built a parallelized program before.

Mentor: Great goal! Parallelism in Python is a rich topic with some genuinely interesting nuances — especially since Python has a few different models for it, each suited to different problems.

Let me quickly check what's already in your learning history before I map out a path for you.
Loading weights: 100%|████████████████████████████████████████████████████████████| 103/103 [00:00<00:00, 13561.87it/s]

**Topic search results:**
["No matching topics found."]

Fresh slate — so we'll be building this knowledge base from scratch together. Let me also record your background.
Here's how I'd suggest we approach parallelism in Python — it builds up in a logical order:

---

### 🗺️ Recommended Learning Path

**1. Concurrency vs. Parallelism** *(concepts first)*
Understanding the *difference* between these two is crucial — Python actually handles them quite differently, and mixing them up leads to confusion. This is where we'd start.

**2. The GIL (Global Interpreter Lock)**
Python has a famous constraint called the GIL that affects how threads run. You need to understand it to make smart choices about *which* tool to use for a given problem.

**3. `threading` — I/O-bound concurrency**
Python's `threading` module lets you run tasks concurrently. It's great for I/O-bound work (network calls, file reads), but the GIL limits its use for CPU-heavy tasks.

**4. `multiprocessing` — CPU-bound parallelism**
This module sidesteps the GIL by spawning separate processes. It's the go-to for true parallel computation in Python.

**5. `concurrent.futures` — High-level interface**
A cleaner, more modern API that unifies threads and processes behind a simple interface. Very practical for real projects.

**6. `asyncio` — Async/await concurrency** *(optional, but powerful)*
A different model altogether — cooperative multitasking, great for high-concurrency I/O (e.g., web servers, many simultaneous network calls).

---

### Where to Start?

I'd suggest we begin with **Concurrency vs. Parallelism** and the **GIL** — they're short but they'll save you a *lot* of confusion later. Once those are solid, we can move into hands-on coding with `threading` and `multiprocessing`.

Does that order sound good to you? And is there a particular use case you have in mind (e.g., data processing, web scraping, scientific computation)? That'll help me tailor the examples!

You: asyncio sounds interesting to me. Also, please keep your responses short, I don't want to be overwhelmed

Mentor: Got it, shorter responses from here on!
Before we jump into `asyncio`, I want to make sure one concept is solid first: **the difference between concurrency and parallelism**. `asyncio` is a concurrency tool, and knowing that distinction will make it click much faster.

Can you take a guess — what do you think the difference is between the two?

You: My guess is that concurrency means things run at the same time but possible on separate devices or cpus, while parallelism is for threads on the same cpu?

Mentor: Good intuition, but it's actually the other way around! Here's the short version:

- **Parallelism** — tasks *literally* run at the same time, on multiple CPUs/cores
- **Concurrency** — tasks are *managed* together, but may take turns on a single CPU (not necessarily simultaneous)

A good analogy: concurrency is one chef juggling multiple dishes (switching between them), parallelism is multiple chefs each cooking their own dish simultaneously.

`asyncio` is concurrency — a single thread that *switches* between tasks whenever one is waiting (e.g., for a network response).

Does that distinction make sense? Any questions before we move on to the GIL?

You: Ah that makes sense. We can move on

Mentor: Great! Let me set up these new topics in memory first.

**Topic search results:**
["No matching topics found."]


**Topic search results:**
["No matching topics found."]




**The GIL** is a lock inside CPython (the standard Python interpreter) that only lets **one thread run Python code at a time** — even on a multi-core machine.

The key implication:
- Threads in Python can't achieve true parallelism for CPU-heavy work
- But they *can* run concurrently for I/O-bound work (the GIL is released while waiting on I/O)
- `asyncio` works around the GIL entirely by using just **one thread** cooperatively

One question: does the idea of a "lock" make sense to you, or would it help to explain that briefly?

You: No that makes sense. I would like to end the session now, we can continue next time. Bye

Mentor:


**Session recap:** We covered the difference between concurrency and parallelism, and got a clear picture of what the GIL is and why it matters for Python threading.

**Nice moment:** You quickly self-corrected on the concurrency/parallelism definitions — that distinction will serve you well as we get into `asyncio`.

**Next session, I'd suggest:**
1. **`asyncio` basics** — `async`/`await` syntax and the event loop, the heart of how `asyncio` works
2. **Event loop mechanics** — how Python actually switches between tasks under the hood

See you next time! 👋


[Memory] Running end-of-session updates...
  ✓ topic updates
  ✓ session record
  ✓ user profile
  ✓ agent self-assessment
[Memory] All updates complete.