from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from .schemas import ErrorResponse


ERROR_DESCRIPTIONS = {
    400: "The request body or parameters are invalid.",
    401: "A required Bearer Token is missing or invalid.",
    403: "The authenticated role is not allowed to use this operation.",
    404: "The content or media object does not exist or is not visible to this role.",
    409: "The requested content state transition is not allowed.",
    413: "The uploaded audio exceeds the configured byte limit.",
    416: "The requested byte range is invalid or outside the object.",
    500: "An unexpected internal server error occurred.",
    503: "A required service dependency or immutable object is unavailable.",
}


def error_responses(*status_codes: int) -> dict[int, dict[str, Any]]:
    return {
        status_code: {
            "model": ErrorResponse,
            "description": ERROR_DESCRIPTIONS[status_code],
        }
        for status_code in status_codes
    }


def playback_asset_responses(*, include_body: bool = True) -> dict[int, dict[str, Any]]:
    immutable_headers = {
        "Accept-Ranges": {
            "description": "Indicates byte range support.",
            "schema": {"type": "string", "const": "bytes"},
        },
        "Content-Length": {
            "description": "Exact number of bytes in this response body.",
            "schema": {"type": "integer", "minimum": 0},
        },
        "ETag": {
            "description": "Strong SHA-256-derived entity tag.",
            "schema": {"type": "string"},
        },
        "Cache-Control": {
            "description": "Private immutable cache policy for READY media.",
            "schema": {
                "type": "string",
                "const": "private, max-age=31536000, immutable",
            },
        },
        "Vary": {
            "description": "The representation varies by Authorization.",
            "schema": {"type": "string", "const": "Authorization"},
        },
    }
    binary_content = {
        "video/mp4": {"schema": {"type": "string", "format": "binary"}},
        "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
        "application/octet-stream": {
            "schema": {"type": "string", "format": "binary"}
        },
    }
    responses: dict[int, dict[str, Any]] = {
        200: {
            "description": "Complete immutable media object.",
            "headers": deepcopy(immutable_headers),
            "content": deepcopy(binary_content),
        },
        206: {
            "description": "One satisfiable byte range.",
            "headers": {
                **deepcopy(immutable_headers),
                "Content-Range": {
                    "description": "Returned inclusive byte range and total length.",
                    "schema": {"type": "string", "example": "bytes 0-1023/4096"},
                },
            },
            "content": deepcopy(binary_content),
        },
        416: {
            "description": ERROR_DESCRIPTIONS[416],
            "headers": {
                **deepcopy(immutable_headers),
                "Content-Range": {
                    "description": "Unsatisfied range with the current object length.",
                    "schema": {"type": "string", "example": "bytes */4096"},
                },
            },
        },
    }
    if not include_body:
        responses[200].pop("content", None)
        responses[206].pop("content", None)
    responses.update(error_responses(401, 403, 404, 503))
    return responses


def install_openapi(app: FastAPI, *, public_base_url: str) -> None:
    """Install the canonical OpenAPI builder used by runtime and static export."""

    def build_schema() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
        )
        schema["info"]["x-service-id"] = "advx26-cloud-media"
        schema["externalDocs"] = {
            "description": "Interactive Swagger UI",
            "url": f"{public_base_url}/docs",
        }
        schemes = schema.setdefault("components", {}).setdefault(
            "securitySchemes",
            {},
        )
        role_metadata = {
            "UserToken": ("user", "Issued by POST /api/v1/sessions after email/password login."),
            "TriggerToken": ("trigger", "Fixed Token provisioned to trigger-01."),
            "PlaybackToken": ("playback", "Fixed Token provisioned to playback-01."),
        }
        for name, (role, provisioning) in role_metadata.items():
            if name in schemes:
                schemes[name]["x-advx-role"] = role
                schemes[name]["x-provisioning"] = provisioning

        asset_path = schema.get("paths", {}).get(
            "/api/v1/contents/{content_id}/assets/{asset_kind}",
            {},
        )
        for operation in asset_path.values():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "asset_kind":
                    parameter["schema"] = {
                        "type": "string",
                        "enum": ["video", "audio", "audio-index"],
                    }
                    parameter["description"] = (
                        "Immutable asset type: H.264 MP4, normalized MP3, or AIX1 index."
                    )
        app.openapi_schema = schema
        return schema

    app.openapi = build_schema
