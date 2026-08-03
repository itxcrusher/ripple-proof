"""Check lightweight source-tree hygiene without third-party dependencies."""

from __future__ import annotations

import argparse
from pathlib import Path


TEXT_SUFFIXES = {".py", ".toml", ".txt", ".yaml", ".yml"}
SOURCE_ROOTS = ("src", "tests", "scripts", ".github")
ROOT_FILES = ("pyproject.toml", "requirements.txt", "requirements-validation.txt", "requirements-audit.txt")


def source_files(repository: Path):
    for root_name in SOURCE_ROOTS:
        root = repository / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                yield path
    for name in ROOT_FILES:
        path = repository / name
        if path.is_file():
            yield path


def hygiene_failures(repository: Path) -> list[str]:
    failures: list[str] = []
    for path in source_files(repository):
        relative = path.relative_to(repository).as_posix()
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            failures.append(f"{relative}: UTF-8 BOM is not allowed")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{relative}: invalid UTF-8 ({exc})")
            continue
        if "\r" in text.replace("\r\n", ""):
            failures.append(f"{relative}: bare CR line endings are not allowed")
        if text and not text.endswith("\n"):
            failures.append(f"{relative}: missing final newline")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.rstrip(" \t") != line:
                failures.append(f"{relative}:{line_number}: trailing whitespace")
            indentation = line[: len(line) - len(line.lstrip())]
            if "\t" in indentation:
                failures.append(f"{relative}:{line_number}: tab indentation")
    return failures


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Check RippleProof source hygiene.")
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args(argv)
    repository = args.repository.expanduser().resolve()
    failures = hygiene_failures(repository)
    if failures:
        print("Source hygiene failed:")
        for failure in failures:
            print(f"- {failure}")
        raise SystemExit(1)
    print("Source hygiene passed.")


if __name__ == "__main__":
    main()
