from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


DEFAULT_SERVER_URL = "http://127.0.0.1:9000"
OPENAPI_DIR = ROOT / "openapi"
YAML_PATH = OPENAPI_DIR / "openapi.yaml"


def build_openapi_schema(server_url: str = DEFAULT_SERVER_URL) -> dict[str, Any]:
    settings = Settings(
        base_dir=ROOT,
        public_base_url=server_url,
        trigger_token="openapi-trigger-placeholder",
        playback_token="openapi-playback-placeholder",
        worker_enabled=False,
    )
    return create_app(settings).openapi()


class _IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def render_yaml(schema: dict[str, Any]) -> str:
    return yaml.dump(
        schema,
        Dumper=_IndentedSafeDumper,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )


def expected_exports(server_url: str = DEFAULT_SERVER_URL) -> dict[Path, str]:
    schema = build_openapi_schema(server_url)
    yaml_text = render_yaml(schema)
    return {YAML_PATH: yaml_text}


def write_exports(server_url: str = DEFAULT_SERVER_URL) -> None:
    OPENAPI_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in expected_exports(server_url).items():
        path.write_text(content, encoding="utf-8")


def check_exports(server_url: str = DEFAULT_SERVER_URL) -> list[Path]:
    expected_schema = build_openapi_schema(server_url)
    stale: list[Path] = []
    for path in expected_exports(server_url):
        try:
            actual_schema = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, yaml.YAMLError):
            stale.append(path)
            continue
        if actual_schema != expected_schema:
            stale.append(path)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the canonical FastAPI OpenAPI schema to YAML.",
    )
    parser.add_argument(
        "--server-url",
        default=DEFAULT_SERVER_URL,
        help="Server URL embedded in the exported OpenAPI document.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when committed OpenAPI files are missing or stale.",
    )
    args = parser.parse_args()

    if args.check:
        stale = check_exports(args.server_url)
        if stale:
            for path in stale:
                print(f"stale: {path.relative_to(ROOT)}", file=sys.stderr)
            return 1
        print("OpenAPI exports are current.")
        return 0

    write_exports(args.server_url)
    for path in expected_exports(args.server_url):
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
