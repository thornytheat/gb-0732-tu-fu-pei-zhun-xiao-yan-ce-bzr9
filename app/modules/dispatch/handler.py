import unicodedata
from typing import Annotated

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.dispatch.service import close_dispatch_order, generate_dispatch

router = APIRouter(prefix='/api/review')
RunId = Annotated[str, Path(pattern=r'^[0-9a-f]{64}$')]
OrderId = Annotated[str, Path(pattern=r'^[0-9a-f]{12}-[0-9]{2}$')]


class ClosureInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    closer: Annotated[str, Field(min_length=1, max_length=30)]

    @field_validator('closer', mode='before')
    @classmethod
    def closer_name(cls, name):
        if not isinstance(name, str):
            return name
        if any(unicodedata.category(char).startswith('C') for char in name):
            raise ValueError('核对人姓名包含控制字符')
        return name.strip()


@router.post('/runs/{run_id}/dispatch')
def generate(request: Request, run_id: RunId):
    booklet, reused = generate_dispatch(request.app.state.store, run_id)
    return {'reused': reused, **booklet}


@router.get('/runs/{run_id}/dispatch')
def read(request: Request, run_id: RunId):
    return request.app.state.store.get_dispatch(run_id)


@router.post('/runs/{run_id}/dispatch/{order_id}/closure')
def close(request: Request, run_id: RunId, order_id: OrderId, form: ClosureInput):
    return close_dispatch_order(request.app.state.store, run_id, order_id, form.closer)
