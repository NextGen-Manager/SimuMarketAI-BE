from fastapi import APIRouter

from app.api.v1 import analyses, auth, businesses, education, health, operations

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(businesses.router)
api_router.include_router(operations.router)
api_router.include_router(education.router)
api_router.include_router(analyses.router)
