#!/usr/bin/env python3
"""Run trigger evaluation for a skill description.

Tests whether a skill's description causes CodeBuddy to trigger (read the skill)
for a set of queries. Outputs results as JSON.
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from scripts.utils import parse_skill_md

# Tools that count as "the model consulted a skill". CodeBuddy exposes
# `use_skill`; `Read` stays as a fallback for when the model reads SKILL.md
# directly instead of going through the skill tool.
SKILL_TOOL_NAMES = ("use_skill", "Read")

# Environment variables that mark an interactive coding-agent session. Dropping
# them lets a nested `codebuddy -p` call run from inside a session without
# tripping the "cannot nest" guard.
NESTED_SESSION_MARKERS = ("CLAUDECODE",)


def clean_env() -> dict[str, str]:
    """Return a copy of the environment safe for a nested CLI call."""
    return {k: v for k, v in os.environ.items() if k not in NESTED_SESSION_MARKERS}


def find_project_root() -> Path:
    """Find the project root by walking up from cwd looking for .codebuddy/.

    Mimics how CodeBuddy discovers its project root, so the candidate skill
    we create ends up where the CLI will look for it.
    """
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".codebuddy").is_dir():
            return parent
    return current


def run_single_query(
    query: str,
    skill_name: str,
    skill_description: str,
    timeout: int,
    project_root: str,
    model: str | None = None,
) -> bool:
    """Run a single query and return whether the skill was triggered.

    Writes a candidate skill to .codebuddy/skills/<name>/SKILL.md so it shows
    up in CodeBuddy's available skills list, then runs `codebuddy -p` with the
    raw query. Triggering is detected from stream events when the CLI emits
    partial messages, falling back to the final assistant or result message.
    """
    unique_id = uuid.uuid4().hex[:8]
    clean_name = f"{skill_name}-skill-{unique_id}"
    project_skills_dir = Path(project_root) / ".codebuddy" / "skills"
    skill_dir = project_skills_dir / clean_name
    skill_file = skill_dir / "SKILL.md"

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        # Use a YAML block scalar to avoid breaking on quotes in description
        indented_desc = "\n  ".join(skill_description.split("\n"))
        skill_content = (
            f"---\n"
            f"name: {clean_name}\n"
            f"description: |\n"
            f"  {indented_desc}\n"
            f"---\n\n"
            f"# {skill_name}\n\n"
            f"This skill handles: {skill_description}\n"
        )
        skill_file.write_text(skill_content)

        cmd = [
            "codebuddy",
            "-p", query,
            "--output-format", "stream-json",
            "--verbose",
            # Headless runs cannot answer permission prompts, so allow the
            # model to invoke the skill tool and read SKILL.md unattended.
            "--dangerously-skip-permissions",
        ]
        if model:
            cmd.extend(["--model", model])

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=project_root,
            env=clean_env(),
        )

        triggered = False
        start_time = time.time()
        # Track state for stream event detection
        pending_tool_name = None
        accumulated_json = ""

        # Read stdout on a background thread: `select.select` on a pipe is not
        # available on Windows, and a thread keeps this portable.
        line_queue: "queue.Queue[str | None]" = queue.Queue()

        def pump_stdout() -> None:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line_queue.put(raw_line.decode("utf-8", errors="replace"))
            line_queue.put(None)

        threading.Thread(target=pump_stdout, daemon=True).start()

        try:
            while time.time() - start_time < timeout:
                try:
                    line = line_queue.get(timeout=1.0)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Early detection via stream events (only when the CLI emits
                # partial messages, otherwise the fallback below handles it)
                if event.get("type") == "stream_event":
                    se = event.get("event", {})
                    se_type = se.get("type", "")

                    if se_type == "content_block_start":
                        cb = se.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            tool_name = cb.get("name", "")
                            if tool_name in SKILL_TOOL_NAMES:
                                pending_tool_name = tool_name
                                accumulated_json = ""
                            else:
                                return False

                    elif se_type == "content_block_delta" and pending_tool_name:
                        delta = se.get("delta", {})
                        if delta.get("type") == "input_json_delta":
                            accumulated_json += delta.get("partial_json", "")
                            if clean_name in accumulated_json:
                                return True

                    elif se_type in ("content_block_stop", "message_stop"):
                        if pending_tool_name:
                            return clean_name in accumulated_json
                        if se_type == "message_stop":
                            return False

                # Fallback: full assistant message
                elif event.get("type") == "assistant":
                    message = event.get("message", {})
                    for content_item in message.get("content", []):
                        if content_item.get("type") != "tool_use":
                            continue
                        tool_name = content_item.get("name", "")
                        tool_input = content_item.get("input", {})
                        if tool_name == "use_skill":
                            haystack = " ".join(
                                str(tool_input.get(key, ""))
                                for key in ("skill", "command", "name")
                            )
                            if clean_name in haystack:
                                triggered = True
                        elif tool_name == "Read" and clean_name in tool_input.get("file_path", ""):
                            triggered = True
                        return triggered

                elif event.get("type") == "result":
                    return triggered
        finally:
            # Clean up process on any exit path (return, exception, timeout)
            if process.poll() is None:
                process.kill()
                process.wait()

        return triggered
    finally:
        if skill_file.exists():
            skill_file.unlink()
        # Drop the candidate skill directory if we created it and it is empty
        if skill_dir.exists() and not any(skill_dir.iterdir()):
            skill_dir.rmdir()


def run_eval(
    eval_set: list[dict],
    skill_name: str,
    description: str,
    num_workers: int,
    timeout: int,
    project_root: Path,
    runs_per_query: int = 1,
    trigger_threshold: float = 0.5,
    model: str | None = None,
) -> dict:
    """Run the full eval set and return results."""
    results = []

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        future_to_info = {}
        for item in eval_set:
            for run_idx in range(runs_per_query):
                future = executor.submit(
                    run_single_query,
                    item["query"],
                    skill_name,
                    description,
                    timeout,
                    str(project_root),
                    model,
                )
                future_to_info[future] = (item, run_idx)

        query_triggers: dict[str, list[bool]] = {}
        query_items: dict[str, dict] = {}
        for future in as_completed(future_to_info):
            item, _ = future_to_info[future]
            query = item["query"]
            query_items[query] = item
            if query not in query_triggers:
                query_triggers[query] = []
            try:
                query_triggers[query].append(future.result())
            except Exception as e:
                print(f"Warning: query failed: {e}", file=sys.stderr)
                query_triggers[query].append(False)

    for query, triggers in query_triggers.items():
        item = query_items[query]
        trigger_rate = sum(triggers) / len(triggers)
        should_trigger = item["should_trigger"]
        if should_trigger:
            did_pass = trigger_rate >= trigger_threshold
        else:
            did_pass = trigger_rate < trigger_threshold
        results.append({
            "query": query,
            "should_trigger": should_trigger,
            "trigger_rate": trigger_rate,
            "triggers": sum(triggers),
            "runs": len(triggers),
            "pass": did_pass,
        })

    passed = sum(1 for r in results if r["pass"])
    total = len(results)

    return {
        "skill_name": skill_name,
        "description": description,
        "results": results,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Run trigger evaluation for a skill description")
    parser.add_argument("--eval-set", required=True, help="Path to eval set JSON file")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--description", default=None, help="Override description to test")
    parser.add_argument("--num-workers", type=int, default=10, help="Number of parallel workers")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout per query in seconds")
    parser.add_argument("--runs-per-query", type=int, default=3, help="Number of runs per query")
    parser.add_argument("--trigger-threshold", type=float, default=0.5, help="Trigger rate threshold")
    parser.add_argument("--model", default=None, help="Model to use for codebuddy -p (default: user's configured model)")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    args = parser.parse_args()

    eval_set = json.loads(Path(args.eval_set).read_text())
    skill_path = Path(args.skill_path)

    if not (skill_path / "SKILL.md").exists():
        print(f"Error: No SKILL.md found at {skill_path}", file=sys.stderr)
        sys.exit(1)

    name, original_description, content = parse_skill_md(skill_path)
    description = args.description or original_description
    project_root = find_project_root()

    if args.verbose:
        print(f"Evaluating: {description}", file=sys.stderr)

    output = run_eval(
        eval_set=eval_set,
        skill_name=name,
        description=description,
        num_workers=args.num_workers,
        timeout=args.timeout,
        project_root=project_root,
        runs_per_query=args.runs_per_query,
        trigger_threshold=args.trigger_threshold,
        model=args.model,
    )

    if args.verbose:
        summary = output["summary"]
        print(f"Results: {summary['passed']}/{summary['total']} passed", file=sys.stderr)
        for r in output["results"]:
            status = "PASS" if r["pass"] else "FAIL"
            rate_str = f"{r['triggers']}/{r['runs']}"
            print(f"  [{status}] rate={rate_str} expected={r['should_trigger']}: {r['query'][:70]}", file=sys.stderr)

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
