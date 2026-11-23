"""Memory status and operator controls through the memory owner's public port."""
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent.plugin_composition import DashboardContext
from .contract import DEFAULT_MEMORY
from .api import Browse

inject = (DEFAULT_MEMORY,)


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["pause", "resume", "retry"]
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)


def register(app: FastAPI, context: DashboardContext) -> None:
    @app.get("/api/dashboard/default-memory")
    async def read() -> JSONResponse:
        return JSONResponse(await context.require(DEFAULT_MEMORY).status(), headers={"Cache-Control": "no-store"})

    @app.get("/api/dashboard/default-memory/scopes")
    async def scopes() -> dict[str, object]:
        return await context.require(DEFAULT_MEMORY).scopes_for_inspection()

    @app.get("/api/dashboard/default-memory/memories")
    async def memories(session_id: str, query: str = "", kind: str = "", status: str = "active",
                       source_ref: str = "", has_embedding: bool | None = None,
                       sort_by: str = "updated_at", sort_order: str = "desc",
                       offset: int = 0, limit: int = 25) -> dict[str, object]:
        try:
            request = Browse.model_validate({"session_id": session_id, "query": query, "kind": kind or None,
                                             "status": status, "source_ref": source_ref,
                                             "has_embedding": has_embedding, "sort_by": sort_by,
                                             "sort_order": sort_order, "offset": offset, "limit": limit})
            return await context.require(DEFAULT_MEMORY).browse(request)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/api/dashboard/default-memory/memories/{memory_id}")
    async def detail(memory_id: str, session_id: str) -> dict[str, object]:
        try:
            return await context.require(DEFAULT_MEMORY).inspect(session_id, memory_id)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/api/dashboard/default-memory/control")
    async def control(request: Control) -> dict[str, object]:
        memory = context.require(DEFAULT_MEMORY)
        try:
            if request.action == "retry":
                receipt = await memory.retry_failed(request_id=request.request_id, expected_version=request.expected_version)
            else:
                receipt = await memory.set_paused(request.action == "pause", request_id=request.request_id,
                                                  expected_version=request.expected_version)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {**await memory.status(), "control_receipt": receipt}
