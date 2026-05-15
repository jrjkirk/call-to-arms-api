from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from database import get_session
from models import Player

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=r"https://call-to-arms-web.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/players")
def list_players(session: Session = Depends(get_session)):
    """Return all active players, ordered by name."""
    statement = select(Player).where(Player.active == True).order_by(Player.name)
    players = session.exec(statement).all()
    return players