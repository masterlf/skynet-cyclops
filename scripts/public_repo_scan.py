#!/usr/bin/env python3
"""Reject common public-repository data leaks using only the standard library."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat

# Used only for a fixed read-only Git invocation with shell disabled.
import subprocess  # nosec B404
import sys
from pathlib import Path

MAX_FILE_BYTES = 1024 * 1024
_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    "htmlcov",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_GENERATED_FILES = {".coverage", "coverage.json"}


def _rules() -> list[tuple[str, re.Pattern[str]]]:
    private_key = "PRIVATE" + r"\s+" + "KEY"
    home_path = (
        r"(?:/(?:ho"
        + r"me|Users)/[^/\s]+|/ro"
        + r"ot)(?:/[^\s`'\"]+)+"
        + r"|[A-Za-z]:\\Users\\[^\\\s]+(?:\\[^\s`'\"]+)+"
    )
    email = r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    private_ip = (
        r"\b(?:" + "10" + r"\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3})\b"
    )
    return [
        (
            "private key material",
            re.compile(
                r"-----BEGIN\s+(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED)\s+)?"
                + private_key
                + r"-----|-----BEGIN\s+PGP\s+PRIVATE\s+KEY\s+BLOCK-----"
            ),
        ),
        ("absolute private home path", re.compile(home_path)),
        ("email address", re.compile(email)),
        ("private or link-local IP address", re.compile(private_ip)),
        ("AWS access key", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
        ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b")),
        (
            "generic bearer token",
            re.compile(
                r"(?i)\b(?:authorization\s*:\s*bearer|api[_-]?key\s*[=:])\s*['\"]?[A-Za-z0-9_./+~-]{16,}"
            ),
        ),
    ]


def _candidate_paths(root: Path) -> list[Path]:
    result: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if name not in _EXCLUDED_DIRS
            and not (current_path == root and name in {"build", "dist"})
        )
        for name in sorted(files):
            if name in _GENERATED_FILES or name in _EXCLUDED_DIRS:
                continue
            result.append(current_path / name)
        for name in sorted(directories):
            candidate = current_path / name
            if candidate.is_symlink():
                result.append(candidate)
    return result


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    root = root.resolve()
    rules = _rules()
    for path in _candidate_paths(root):
        relative = path.relative_to(root)
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                resolved = path.resolve(strict=True)
                if os.path.commonpath((root, resolved)) != str(root):
                    findings.append(f"{relative}: symlink escapes repository")
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > MAX_FILE_BYTES:
                findings.append(f"{relative}: file exceeds size limit")
                continue
            if path.suffix.lower() in _SENSITIVE_SUFFIXES and stat.S_IMODE(info.st_mode) & 0o077:
                findings.append(f"{relative}: sensitive fixture permissions are too broad")
            data = path.read_bytes()
        except (OSError, ValueError):
            findings.append(f"{relative}: file could not be safely inspected")
            continue
        text = data.decode("utf-8", errors="ignore")
        for label, pattern in rules:
            for match in pattern.finditer(text):
                if label == "email address":
                    address = match.group(0).lower()
                    if address.endswith("@example.invalid") or address.startswith("noreply@"):
                        continue
                findings.append(f"{relative}: detected {label}")
                break
    git = shutil.which("git")
    if git is None:
        findings.append("git history: git executable is unavailable")
        return findings
    try:
        history = subprocess.run(  # noqa: S603 - fixed Git read-only argv
            [
                git,
                "-C",
                str(root),
                "log",
                "--all",
                "--format=",
                "--no-ext-diff",
                "--no-renames",
                "-p",
            ],
            shell=False,  # nosec B603
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        findings.append("git history: could not be safely inspected")
    else:
        if history.returncode == 0:
            if len(history.stdout) > 32 * MAX_FILE_BYTES:
                findings.append("git history: output exceeds size limit")
            else:
                text = history.stdout.decode("utf-8", errors="ignore")
                for label, pattern in rules:
                    if pattern.search(text):
                        findings.append(f"git history: detected {label}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args(argv)
    root = Path(args.path)
    if not root.is_dir():
        print("public scan error: target must be a directory", file=sys.stderr)
        return 2
    findings = scan(root)
    if findings:
        for finding in findings:
            print(f"public scan failed: {finding}", file=sys.stderr)
        return 1
    print(f"public scan passed: {len(_candidate_paths(root.resolve()))} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
