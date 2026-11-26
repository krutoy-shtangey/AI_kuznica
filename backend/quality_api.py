"""Bearer-protected quality workflow and authenticated appeal creation."""

from hmac import compare_digest
from time import time
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from backend.evidence import conditions_map
from backend.storage import AccessError, digest

Status = Literal["new", "in_work", "draft", "approved", "closed"]
QualityBearer = HTTPBearer(auto_error=False, scheme_name="QualityBearer")


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: int = Field(ge=1)


class Version(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class StatusInput(Version):
    status: Literal["in_work", "closed"]


class DraftInput(Version):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2400)]
    department: Literal["ib", "tech", "dbrct"] | None = None
    stage: Literal["presale", "implementation"] | None = None
    conditions: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None

    @field_validator("conditions")
    @classmethod
    def normalize(cls, value):
        clauses = conditions_map(value)
        return ";".join(f"{key}={text}" for key, text in clauses.items()) or None


def routes(quality, token_dependency, secret: str | None, actor: str):
    router = APIRouter()
    expected = digest(secret) if secret and len(secret) >= 32 else None

    def authenticate(request: Request, credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(QualityBearer)
    ]) -> str:
        if expected is None:
            raise AccessError("quality_disabled")
        token = credentials.credentials if credentials else ""
        # Do not trust arbitrary client headers for the peer address.
        peer = request.client.host if request.client else "unknown"
        with quality.store.write() as db:
            kind = "quality_auth_" + digest(peer)
            now = time()
            db.execute("DELETE FROM activity WHERE instant < ?", (now - 86400,))
            count, = db.execute("SELECT count(*) FROM activity WHERE kind=? AND instant>=?",
                                (kind, now - 60)).fetchone()
            if count >= 5:
                raise AccessError("quality_login_limit")
            if len(token) <= 256 and compare_digest(expected, digest(token)):
                return actor
            db.execute("INSERT INTO activity VALUES (?,?,?,0)", (str(uuid4()), kind, now))
        raise AccessError("quality_unauthorized")

    @router.post("/dialogues/{dialogue_id}/appeals", status_code=201, tags=["appeals"])
    def create_appeal(dialogue_id: UUID, body: Turn, response: Response, token: str = Depends(token_dependency)):
        card, new = quality.create(str(dialogue_id), token, body.turn_id)
        if not new:
            response.status_code = 200
        return quality.public(card)

    @router.get("/dialogues/{dialogue_id}/appeals/{appeal_id}", tags=["appeals"])
    def appeal_status(dialogue_id: UUID, appeal_id: UUID, token: str = Depends(token_dependency)):
        return quality.user_detail(str(dialogue_id), token, str(appeal_id))

    admin = APIRouter(prefix="/quality", tags=["quality"], dependencies=[Depends(authenticate)])

    @admin.get("/appeals")
    def list_appeals(status: Status | None = None, offset: Annotated[int, Query(ge=0)] = 0,
                     limit: Annotated[int, Query(ge=1, le=100)] = 30):
        return {"items": quality.listing(status, offset, limit)}

    @admin.get("/appeals/{appeal_id}")
    def get_appeal(appeal_id: UUID):
        return quality.detail(str(appeal_id))

    @admin.patch("/appeals/{appeal_id}/status")
    def set_status(appeal_id: UUID, body: StatusInput):
        return quality.status(str(appeal_id), body.version, body.status)

    @admin.put("/appeals/{appeal_id}/draft")
    def set_draft(appeal_id: UUID, body: DraftInput, author: str = Depends(authenticate)):
        payload = body.model_dump(exclude={"version"}, exclude_unset=True) | {"author": author}
        return quality.draft(str(appeal_id), body.version, payload)

    @admin.post("/appeals/{appeal_id}/approve")
    def approve(appeal_id: UUID, body: Version, response: Response, author: str = Depends(authenticate)):
        card = quality.change_publication(str(appeal_id), body.version, author, "upsert")
        if card["publication"]["state"] != "indexed":
            response.status_code = 202
        return card

    @admin.post("/appeals/{appeal_id}/revoke")
    def revoke(appeal_id: UUID, body: Version, response: Response, author: str = Depends(authenticate)):
        card = quality.change_publication(str(appeal_id), body.version, author, "delete")
        if card["publication"]["state"] != "indexed":
            response.status_code = 202
        return card

    @admin.post("/appeals/{appeal_id}/retry-index")
    def retry(appeal_id: UUID, body: Version, response: Response):
        card = quality.retry_index(str(appeal_id), body.version)
        if card["publication"]["state"] != "indexed":
            response.status_code = 202
        return card

    @admin.get("/explanations")
    def explanations(offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 30):
        return {"items": quality.listing(None, offset, limit, explanations=True)}

    router.include_router(admin)
    return router
