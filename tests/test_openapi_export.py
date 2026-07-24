from __future__ import annotations

import unittest

import yaml

from scripts.export_openapi import YAML_PATH, build_openapi_schema, check_exports


class OpenApiExportTests(unittest.TestCase):
    def test_schema_has_stable_roles_operations_and_asset_contract(self) -> None:
        schema = build_openapi_schema()
        operations = [
            operation
            for path_item in schema["paths"].values()
            for operation in path_item.values()
            if isinstance(operation, dict) and "operationId" in operation
        ]
        operation_ids = [operation["operationId"] for operation in operations]

        self.assertEqual(schema["openapi"], "3.1.0")
        self.assertEqual(schema["info"]["x-service-id"], "advx26-cloud-media")
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        self.assertEqual(
            set(schema["components"]["securitySchemes"]),
            {"UserToken", "TriggerToken", "PlaybackToken"},
        )
        self.assertEqual(
            schema["components"]["securitySchemes"]["TriggerToken"]["x-advx-role"],
            "trigger",
        )

        asset = schema["paths"][
            "/api/v1/contents/{content_id}/assets/{asset_kind}"
        ]["get"]
        asset_kind = next(
            parameter
            for parameter in asset["parameters"]
            if parameter["name"] == "asset_kind"
        )
        self.assertEqual(
            asset_kind["schema"]["enum"],
            ["video", "audio", "audio-index"],
        )
        self.assertTrue({"200", "206", "416", "503"}.issubset(asset["responses"]))
        self.assertEqual(
            asset["responses"]["206"]["headers"]["Accept-Ranges"]["schema"]["const"],
            "bytes",
        )

    def test_committed_yaml_matches_runtime_schema(self) -> None:
        schema = build_openapi_schema()

        self.assertEqual(yaml.safe_load(YAML_PATH.read_text(encoding="utf-8")), schema)
        self.assertEqual(check_exports(), [])


if __name__ == "__main__":
    unittest.main()
