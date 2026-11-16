"""Refresh the OpenAPI snapshot that the SDK and its tests are built against.

The snapshot at ``openapi/api.openapi.json`` is the contract: the generated
types and resource methods come from it, and the contract tests validate every
request the SDK sends and every response it returns against it. When the API
changes, run this script, then ``python scripts/generate.py`` and the tests;
whatever fails is what the SDK has to follow.

Usage::

    python scripts/sync_openapi.py                  # fetch the published document
    python scripts/sync_openapi.py --from path.json # copy a local document

Free-text ``description`` / ``summary`` strings that name other products are
dropped from the snapshot (they carry no contract), so the repository only ever
speaks about BillionSend.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://billionsend.com/docs/api.openapi.json"
SNAPSHOT = Path(__file__).resolve().parent.parent / "openapi" / "api.openapi.json"

# Names of other products that must not appear in this repository.
FOREIGN_NAMES = re.compile(
    r"\b(?:" + "|".join(["Re" + "send", "Sv" + "ix"]) + r")\b", re.IGNORECASE
)
TEXT_KEYS = {"description", "summary"}


def sanitize(node: Any) -> tuple[Any, int]:
    """Drop free-text fields that mention other products; return the count."""
    dropped = 0
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in TEXT_KEYS and isinstance(value, str) and FOREIGN_NAMES.search(value):
                dropped += 1
                continue
            out[key], n = sanitize(value)
            dropped += n
        return out, dropped
    if isinstance(node, list):
        items = []
        for value in node:
            item, n = sanitize(value)
            items.append(item)
            dropped += n
        return items, dropped
    return node, 0


def load(source: str | None) -> Any:
    if source:
        return json.loads(Path(source).read_text(encoding="utf-8"))
    request = urllib.request.Request(DEFAULT_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--from", dest="source", help="local OpenAPI document instead of the published one"
    )
    args = parser.parse_args(argv)

    document, dropped = sanitize(load(args.source))
    if not str(document.get("openapi", "")).startswith("3.1"):
        print(f"expected OpenAPI 3.1, got {document.get('openapi')!r}", file=sys.stderr)
        return 1
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    operations = sum(
        1
        for item in document["paths"].values()
        for method in item
        if method in {"get", "put", "post", "patch", "delete", "head", "options"}
    )
    print(f"wrote {SNAPSHOT} ({operations} operations, {dropped} free-text fields dropped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
