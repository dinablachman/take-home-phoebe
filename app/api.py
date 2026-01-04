from fastapi import APIRouter, FastAPI

router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app
