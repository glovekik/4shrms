from fastapi import APIRouter

router = APIRouter()

@router.get("/manual")
def test():
    return {"message": "Manual route working"}