"""DOM-indexed browser control over CDP. 404 unless FLOW_CDP_ENABLED=1."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent import config
from agent.browser.actions import Step, WaitForTextStep, WaitStep
from agent.browser.cdp import CdpError
from agent.browser.session import get_browser_session

CDP_STATUS = {
    "CDP_UNREACHABLE": 503, "CDP_CLOSED": 503, "CDP_TIMEOUT": 504,
    "NO_FLOW_PAGE": 409, "PORT_CONFLICT": 409, "JS_ERROR": 502, "CDP_PROTOCOL": 502,
}
# A caller whose HTTP client gives up before the batch ends loses the response
# and may resend it, replaying a credit-spending click. Keep every batch well
# inside a typical 120 s tool/client timeout; poll long jobs in separate calls.
MAX_BATCH_WAIT_S = 120


def cdp_enabled():
    if not config.FLOW_CDP_ENABLED:
        raise HTTPException(404, "browser control disabled; set FLOW_CDP_ENABLED=1")


router = APIRouter(prefix="/browser", tags=["browser"], dependencies=[Depends(cdp_enabled)])


class ActRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[Step] = Field(min_length=1, max_length=20)
    snapshot: bool = True
    text_chars: int = Field(default=600, ge=0, le=6000)

    @model_validator(mode="after")
    def _bounded_wait(self):
        total = sum(
            s.ms / 1000 if isinstance(s, WaitStep)
            else s.timeout_s if isinstance(s, WaitForTextStep) else 0
            for s in self.steps
        )
        if total > MAX_BATCH_WAIT_S:
            raise ValueError(
                f"batch waits up to {total:g} s in total (wait + wait_for_text); "
                f"the limit is {MAX_BATCH_WAIT_S} s. Send long waits as separate "
                "act calls and never resend a batch whose response was lost"
            )
        return self


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
