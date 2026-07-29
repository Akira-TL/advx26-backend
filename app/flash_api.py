"""JSON API for the Lingguang flash-app frontend."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from .database import Database

CONTENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def create_flash_router(*, database: Database) -> APIRouter:
    router = APIRouter(tags=["flash"])

    @router.get("/api/flash/preview/{content_id}", include_in_schema=False)
    async def flash_preview(content_id: str, request: Request) -> dict[str, object]:
        if not CONTENT_ID_PATTERN.fullmatch(content_id):
            raise HTTPException(status_code=404, detail="内容不存在")
        with database.connect() as conn:
            row = conn.execute(
                "SELECT display_label, duration_ms, state, created_at FROM contents "
                "WHERE id=? AND deleted_at IS NULL",
                (content_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="内容不存在")
            if row["state"] != "READY":
                raise HTTPException(status_code=409, detail="内容处理中，请稍后再试")

            chain_row = conn.execute(
                "SELECT chain_state, token_id, tx_hash, contract_address FROM content_chain "
                "WHERE content_id=?",
                (content_id,),
            ).fetchone()
            editions_count = conn.execute(
                "SELECT COUNT(*) AS cnt FROM content_editions WHERE content_id=?",
                (content_id,),
            ).fetchone()["cnt"]

        base = str(request.base_url).rstrip("/")
        on_chain = chain_row is not None and chain_row["chain_state"] == "MINTED"
        return {
            "content_id": content_id,
            "title": row["display_label"],
            "duration_ms": row["duration_ms"],
            "created_at": row["created_at"] or "",
            "audio_url": f"{base}/preview/{content_id}/audio",
            "video_url": f"{base}/preview/{content_id}/video",
            "on_chain": on_chain,
            "token_id": chain_row["token_id"] if on_chain else None,
            "tx_hash": chain_row["tx_hash"] if on_chain else None,
            "contract_address": chain_row["contract_address"] if on_chain else None,
            "editions_count": editions_count,
        }

    return router
