#!/usr/bin/env python3
"""Search allowed local documentation and code paths for review context.

Prints a JSON document with matches per query to stdout. The script no longer
writes intermediate files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

TEXT_EXTENSIONS = {
    ".adoc", ".cfg", ".conf", ".css", ".csv", ".env", ".go", ".graphql",
    ".groovy", ".html", ".ini", ".java", ".js", ".json", ".kt", ".md",
    ".mmd", ".properties", ".proto", ".py", ".rst", ".sql", ".ts", ".tsx",
    ".txt", ".xml", ".yaml", ".yml",
}
SKIP_DIRS = {
    ".git", ".idea", ".mvn", ".pytest_cache", ".venv", "build", "dist", "node_modules",
    "target", "venv", "__pycache__",
}
MAX_FILE_BYTES = 2 * 1024 * 1024


def iter_files(paths: List[Path]) -> Iterable[Path]:
    for base in paths:
        if not base.exists():
            continue
        if base.is_file():
            yield base
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            root_path = Path(root)
            for name in files:
                path = root_path / name
                if path.suffix.lower() in TEXT_EXTENSIONS:
                    yield path


def tokenize(query: str) -> List[str]:
    return [part.lower() for part in re.findall(r"[\wа-яё.-]+", query, flags=re.IGNORECASE) if len(part) >= 3]


def line_matches(line: str, query: str) -> bool:
    lower_line = line.lower()
    lower_query = query.lower().strip()
    if not lower_query:
        return False
    if lower_query in lower_line:
        return True
    terms = tokenize(query)
    if len(terms) <= 1:
        return False
    return any(term in lower_line for term in terms)


def read_lines(path: Path) -> List[str]:
    if path.stat().st_size > MAX_FILE_BYTES:
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def search(paths: List[Path], queries: List[str], max_results: int, context_lines: int) -> Dict[str, List[Dict[str, object]]]:
    results: Dict[str, List[Dict[str, object]]] = {query: [] for query in queries}
    for path in iter_files(paths):
        try:
            lines = read_lines(path)
        except OSError:
            continue
        if not lines:
            continue
        for index, line in enumerate(lines):
            for query in queries:
                if len(results[query]) >= max_results:
                    continue
                if line_matches(line, query):
                    start = max(0, index - context_lines)
                    end = min(len(lines), index + context_lines + 1)
                    results[query].append(
                        {
                            "path": str(path),
                            "line": index + 1,
                            "text": line.strip(),
                            "context": "\n".join(
                                f"{line_number + 1}: {lines[line_number]}" for line_number in range(start, end)
                            ),
                        }
                    )
    return results


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search local docs and code paths for review context. Prints JSON to stdout.")
    parser.add_argument("--paths", nargs="+", required=True, help="Allowed local files or directories")
    parser.add_argument("--query", action="append", required=True, help="Search query; can be passed multiple times")
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--context-lines", type=int, default=2)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    paths = [Path(value).expanduser().resolve() for value in args.paths]
    queries = [query.strip() for query in args.query if query.strip()]
    results = search(paths, queries, max_results=args.max_results, context_lines=args.context_lines)
    total = sum(len(items) for items in results.values())
    print(json.dumps(
        {
            "ok": True,
            "paths": [str(path) for path in paths],
            "queries": queries,
            "totalMatches": total,
            "results": results,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
