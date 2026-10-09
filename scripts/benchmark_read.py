#!/usr/bin/env python3
"""Benchmark allowlisted Teams reads without retaining response contents.

Run sequentially on an otherwise idle machine. JSON stdout/stderr is captured
only in process memory and reduced to timings, status and counts. The legacy
CLI can launch a browser even with --no-input: default token preflight refuses
expired auth. Use --skip-preflight only with the updated CLI's silent auth.
Cold metadata mode temporarily backs up metadata.json (never auth or ID maps),
clears only that file before each sample, and restores it even after failure.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


COMMAND_NAMES = ("version", "chats", "chat", "unread", "summary", "user-search", "search")
METADATA_FILENAME = "metadata.json"


def build_commands(executable: str, user_query: str, search_query: str) -> dict[str, list[str]]:
    """No arbitrary CLI command/flags can enter the benchmark subprocess."""
    prefix = [executable, "--no-input"]
    return {
        "version": [executable, "--version"],
        "chats": [*prefix, "chats", "-n", "25", "--json"],
        "chat": [*prefix, "chat", "1", "-n", "25", "--json"],
        "unread": [*prefix, "unread", "--json"],
        "summary": [*prefix, "summary", "--json"],
        "user-search": [*prefix, "user-search", user_query, "--json"],
        "search": [*prefix, "search", search_query, "--json"],
        "search-local": [*prefix, "search", search_query, "--local", "--json"],
    }


def _expiry(token: str) -> float:
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return float(claims["exp"])
    except (ValueError, IndexError, KeyError, TypeError, json.JSONDecodeError):
        return 0.0


def preflight_tokens(cache_dir: Path, minimum_lifetime: float = 120.0) -> bool:
    """Inspect expiry privately; never return a token, claims, or exception text."""
    try:
        tokens = json.loads((cache_dir / "tokens.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tokens = {}
    if not isinstance(tokens, dict):
        return False
    override = os.environ.get("TEAMS_IC3_TOKEN")
    if override:
        tokens["ic3"] = override
    if not tokens.get("ic3"):
        return False
    deadline = time.time() + minimum_lifetime
    # Any expired secondary token could trigger legacy automatic interactive auth.
    for audience in ("ic3", "graph", "presence", "csa", "substrate"):
        token = tokens.get(audience)
        if token and (not isinstance(token, str) or _expiry(token) <= deadline):
            return False
    return True


def classify_output(stdout: str, stderr: str, returncode: int) -> dict:
    """Only finite status labels and numeric counts leave captured CLI output."""
    result: dict = {"ok": returncode == 0, "result_count": None, "error": None}
    try:
        envelope = json.loads(stdout)
    except (ValueError, TypeError):
        envelope = None
    if isinstance(envelope, dict):
        result["ok"] = returncode == 0 and envelope.get("ok") is True
        data = envelope.get("data")
        if isinstance(data, list):
            result["result_count"] = len(data)
        elif isinstance(data, dict):
            recent = data.get("recent")
            if isinstance(recent, list):
                result["result_count"] = len(recent)
            for field in ("unread_count", "unread_messages"):
                value = data.get(field)
                if type(value) is int and value >= 0:
                    result[field] = value
    if not result["ok"]:
        lowered = (stdout + stderr).lower()
        if returncode in (3, 4) or any(term in lowered for term in ("auth_required", "token expired", "token_expired", "login required", "authentication required", "re-login", "reauthentication", "interaction required")):
            result["error"] = "auth_required"
        elif returncode == 7 or "429" in lowered or "rate limit" in lowered or "rate_limited" in lowered:
            result["error"] = "rate_limited"
        elif returncode == 2:
            result["error"] = "usage_error"
        elif not isinstance(envelope, dict):
            result["error"] = "invalid_output"
        else:
            result["error"] = "command_error"
    return result


def run_sample(argv: list[str], timeout: float) -> dict:
    started = time.perf_counter()
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                   timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"elapsed_s": round(time.perf_counter() - started, 6), "returncode": None,
                "ok": False, "result_count": None, "error": "timeout"}
    except OSError:
        return {"elapsed_s": round(time.perf_counter() - started, 6), "returncode": None,
                "ok": False, "result_count": None, "error": "execution_error"}
    reduced = classify_output(completed.stdout, completed.stderr, completed.returncode)
    reduced.update(elapsed_s=round(time.perf_counter() - started, 6), returncode=completed.returncode)
    if argv[-1] == "--version" and reduced["ok"]:
        match = re.search(r"\bversion\s+(\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*))\b", completed.stdout)
        if match:
            reduced["version"] = match.group(1)
    return reduced


@contextmanager
def cold_metadata(cache_dir: Path, enabled: bool):
    """Back up/restore precisely one known metadata file; leave auth untouched."""
    if not enabled:
        yield lambda: None
        return
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = cache_dir / METADATA_FILENAME
    if metadata.is_symlink() or (metadata.exists() and not metadata.is_file()):
        raise ValueError("unsafe_metadata_path")
    with tempfile.TemporaryDirectory(prefix="benchmark-metadata-", dir=cache_dir) as folder:
        backup = Path(folder) / METADATA_FILENAME
        had_metadata = metadata.exists()
        if had_metadata:
            shutil.copyfile(metadata, backup)
            backup.chmod(0o600)

        def clear():
            if metadata.is_symlink() or (metadata.exists() and not metadata.is_file()):
                raise ValueError("unsafe_metadata_path")
            metadata.unlink(missing_ok=True)

        try:
            yield clear
        finally:
            clear()
            if had_metadata:
                os.replace(backup, metadata)
                metadata.chmod(0o600)


def secure_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teams", default=shutil.which("teams"), help="Installed CLI executable")
    parser.add_argument("--label", required=True, help="Public label, e.g. baseline or optimized")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--user-query", default="Yusuf", help="Passed privately; not persisted")
    parser.add_argument("--search-query", default="toplantı", help="Passed privately; not persisted")
    parser.add_argument("--commands", nargs="+", choices=COMMAND_NAMES, default=list(COMMAND_NAMES))
    parser.add_argument("--cache-mode", choices=("existing", "cold", "warm"), default="existing")
    parser.add_argument("--include-local", action="store_true", help="Also benchmark read-only indexed search after sync")
    parser.add_argument("--skip-preflight", action="store_true", help="Only for updated CLI with silent auth")
    parser.add_argument("--output", type=Path, help="Default: private cache benchmark-label-mode.json")
    args = parser.parse_args(argv)
    if not args.teams or args.runs < 1 or args.runs > 100 or args.timeout <= 0:
        parser.error("Executable, 1-100 runs and positive timeout required")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.label):
        parser.error("Label must contain only letters, digits, dot, underscore or hyphen")
    if any(query.startswith("-") for query in (args.user_query, args.search_query)):
        parser.error("Queries may not start with a dash")
    cache_dir = Path(os.environ.get("TEAMS_CLI_CACHE", Path.home() / ".cache" / "teams-cli"))
    executable = str(Path(args.teams).resolve())
    commands = build_commands(executable, args.user_query, args.search_query)
    if args.include_local:
        args.commands.append("search-local")
    if not args.skip_preflight and any(name != "version" for name in args.commands):
        if not preflight_tokens(cache_dir):
            print("Benchmark stopped: auth preflight failed; no live reads or browser launched.", file=sys.stderr)
            return 3
    report = {"schema_version": "1.0", "label": args.label, "cache_mode": args.cache_mode,
              "executable": executable, "started_utc": datetime.now(timezone.utc).isoformat(),
              "machine": {"platform": sys.platform, "python": f"{sys.version_info.major}.{sys.version_info.minor}"},
              "runs": args.runs, "samples": [], "summary": {}}
    exit_code = 0
    try:
        with cold_metadata(cache_dir, args.cache_mode == "cold") as clear_metadata:
            for name in args.commands:
                if args.cache_mode == "warm" and name != "version":
                    prime = run_sample(commands[name], args.timeout)
                    if not prime["ok"]:
                        report["samples"].append({"command": name, "run": 0, "warmup": True, **prime})
                        exit_code = 1
                        if prime["error"] in ("auth_required", "rate_limited", "timeout"):
                            break
                for run in range(1, args.runs + 1):
                    clear_metadata()
                    if not args.skip_preflight and name != "version" and not preflight_tokens(cache_dir):
                        sample = {"ok": False, "elapsed_s": 0.0, "returncode": 3, "result_count": None,
                                  "error": "auth_required", "executed": False}
                    else:
                        sample = run_sample(commands[name], args.timeout)
                    report["samples"].append({"command": name, "run": run, **sample})
                    if not sample["ok"]:
                        exit_code = 1
                        if sample["error"] in ("auth_required", "rate_limited", "timeout"):
                            break
                if report["samples"] and report["samples"][-1]["error"] in ("auth_required", "rate_limited", "timeout"):
                    break
    except (OSError, ValueError):
        print("Benchmark stopped: private metadata backup or restore failed.", file=sys.stderr)
        return 1
    for name in args.commands:
        samples = [s for s in report["samples"] if s["command"] == name and s["run"] > 0 and s["ok"]]
        if samples:
            timings = [s["elapsed_s"] for s in samples]
            report["summary"][name] = {"successful_runs": len(samples), "median_s": round(statistics.median(timings), 6),
                                       "min_s": min(timings), "max_s": max(timings),
                                       "result_counts": [s["result_count"] for s in samples]}
    output = args.output or cache_dir / f"benchmark-{args.label}-{args.cache_mode}.json"
    try:
        secure_json_write(output, report)
    except OSError:
        print("Benchmark stopped: private report write failed.", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
