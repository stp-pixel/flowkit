"""DOM-indexed browser control over CDP. 404 unless FLOW_CDP_ENABLED=1."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from agent import config
from agent.browser.actions import Step
from agent.browser.cdp import CdpError
from agent.browser.session import get_browser_session

CDP_STATUS = {
    "CDP_UNREACHABLE": 503, "CDP_CLOSED": 503, "CDP_TIMEOUT": 504,
    "NO_FLOW_PAGE": 409, "PORT_CONFLICT": 409, "JS_ERROR": 502, "CDP_PROTOCOL": 502,
}


def cdp_enabled():
    if not config.FLOW_CDP_ENABLED:
        raise HTTPException(404, "browser control disabled; set FLOW_CDP_ENABLED=1")


router = APIRouter(prefix="/browser", tags=["browser"], dependencies=[Depends(cdp_enabled)])


class ActRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[Step] = Field(min_length=1, max_length=20)
    snapshot: bool = True
    text_chars: int = Field(default=600, ge=0, le=6000)


def _http_error(exc: CdpError) -> HTTPException:
    return HTTPException(
        CDP_STATUS.get(exc.code, 502),
        {"code": exc.code, "message": exc.message, "hint": exc.hint},
    )


@router.get("/status")
async def status():
    return await get_browser_session().status()


@router.get("/snapshot")
async def snapshot(text_chars: int = Query(600, ge=0, le=6000)):
    try:
        return await get_browser_session().snapshot(text_chars=text_chars)
    except CdpError as exc:
        raise _http_error(exc)


@router.post("/act")
async def act(req: ActRequest):
    try:
        return await get_browser_session().act(
            req.steps, snapshot=req.snapshot, text_chars=req.text_chars)
    except CdpError as exc:
        raise _http_error(exc)
