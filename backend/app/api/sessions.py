from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.schemas import SessionDetailOut, SessionOut, TextTurnIn, TurnOut
from app.services.session_service import SessionClosed, SessionNotFound, SessionService

router = APIRouter()


def get_session_service(request: Request) -> SessionService:
    service: SessionService = request.app.state.session_service
    return service


Service = Annotated[SessionService, Depends(get_session_service)]


def _not_found(exc: SessionNotFound) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))


def _closed(exc: SessionClosed) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(service: Service) -> SessionOut:
    return SessionOut.model_validate(service.create_session())


@router.get("/sessions/{session_id}")
def get_session(session_id: str, service: Service) -> SessionDetailOut:
    try:
        return SessionDetailOut.model_validate(service.get_session(session_id))
    except SessionNotFound as exc:
        raise _not_found(exc) from exc


@router.post("/sessions/{session_id}/turns", status_code=status.HTTP_201_CREATED)
def add_text_turn(session_id: str, body: TextTurnIn, service: Service) -> TurnOut:
    try:
        return TurnOut.model_validate(service.add_text_turn(session_id, body.text))
    except SessionNotFound as exc:
        raise _not_found(exc) from exc
    except SessionClosed as exc:
        raise _closed(exc) from exc


@router.post("/sessions/{session_id}/end")
def end_session(session_id: str, service: Service) -> SessionOut:
    try:
        return SessionOut.model_validate(service.end_session(session_id))
    except SessionNotFound as exc:
        raise _not_found(exc) from exc
    except SessionClosed as exc:
        raise _closed(exc) from exc
