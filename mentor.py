#!/usr/bin/env python3
import argparse
import os
import shutil
import sys
from pathlib import Path


def _parse_args():
    parser = argparse.ArgumentParser(description="Coding Mentor Agent")
    parser.add_argument("--reset-memory", action="store_true",
                        help="Wipe all memory files")
    # Ollama options
    parser.add_argument("--ollama-model", default=None,
                        help="Ollama model name")
    parser.add_argument("--ollama-url", default=None,
                        help="Ollama server URL (default: http://localhost:11434)")
    # Anthropic options
    parser.add_argument("--anthropic", action="store_true",
                        help="Use the Anthropic API (Claude) instead of Ollama")
    parser.add_argument("--anthropic-model", default=None,
                        help="Anthropic model name (default: claude-sonnet-4-5)")
    parser.add_argument("--anthropic-api-key", default=None,
                        help="Anthropic API key (defaults to ANTHROPIC_API_KEY env var)")
    return parser.parse_args()


def _build_llm_client(args):
    """Instantiate the appropriate LLM client based on CLI flags."""
    if args.anthropic:
        return _build_anthropic_client(args)
    return _build_ollama_client(args)


def _build_ollama_client(args):
    try:
        from llm import OllamaClient, DEFAULT_MODEL, DEFAULT_URL
        model    = args.ollama_model or DEFAULT_MODEL
        base_url = args.ollama_url   or DEFAULT_URL
        client   = OllamaClient(model=model, base_url=base_url)
        print(f"[Ollama] Using model '{model}' at {base_url}")
        return client
    except ConnectionError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def _build_anthropic_client(args):
    try:
        from llm import AnthropicClient, DEFAULT_ANTHROPIC_MODEL
        model   = args.anthropic_model   or DEFAULT_ANTHROPIC_MODEL
        api_key = args.anthropic_api_key or None  # falls back to env var inside client
        client  = AnthropicClient(model=model, api_key=api_key)
        print(f"[Anthropic] Using model '{model}'")
        return client
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)


def _reset_memory():
    """Wipe the memory/ directory after user confirmation."""
    memory_path = Path("memory")

    print("WARNING: This will permanently delete all memory files:")
    print(f"  {memory_path.resolve()}")
    confirm = input("Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("Reset cancelled.")
        sys.exit(0)

    shutil.rmtree(memory_path)
    print("Memory wiped.")


def main():
    args = _parse_args()

    if args.reset_memory:
        _reset_memory()
        sys.exit(0)

    llm = _build_llm_client(args)

    from harness.mentor_harness import MentorHarness
    harness = MentorHarness(llm=llm)

    print("-" * 60)
    print("\nMentor: ", end="", flush=True)
    harness.greet()
    print()

    try:
        while not harness.done:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\nMentor: It looks like we got cut off. See you next time!")
                break

            if not user_input:
                continue

            print("\nMentor: ", end="", flush=True)
            harness.step(user_input)
            print()

    finally:
        if not harness.done:
            from harness.mentor_harness import SessionState
            harness._state = SessionState.DONE

        harness.run_end_of_session_updates()


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    main()