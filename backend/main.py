import json
import random
from fastapi import FastAPI, HTTPException, Depends, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from typing import List, Optional
import csv
import io
from contextlib import asynccontextmanager

from sqlalchemy import create_engine, Column, Integer, String, Boolean, Table, ForeignKey
from sqlalchemy.orm import sessionmaker, Session, relationship, declarative_base

# --- Database Setup ---
DATABASE_URL = "sqlite:///./tournament.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

played_opponents_association = Table('played_opponents_association', Base.metadata,
    Column('player_id', Integer, ForeignKey('players.id')),
    Column('opponent_id', Integer, ForeignKey('players.id'))
)

# --- SQLAlchemy Models ---
class MatchRecord(Base):
    __tablename__ = "match_records"
    id = Column(Integer, primary_key=True)
    winner_id = Column(Integer, ForeignKey("players.id"))
    loser_id = Column(Integer, ForeignKey("players.id"))

class PlayerDB(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    points = Column(Integer, default=0)
    has_received_bye = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True) # 棄権フラグ
    display_order = Column(Integer, default=0)
    
    played_opponents = relationship(
        "PlayerDB",
        secondary=played_opponents_association,
        primaryjoin=id==played_opponents_association.c.player_id,
        secondaryjoin=id==played_opponents_association.c.opponent_id,
        backref="played_by"
    )

class TournamentInfo(Base):
    __tablename__ = "tournament_info"
    id = Column(Integer, primary_key=True)
    current_round = Column(Integer, default=0)

class CurrentMatchesDB(Base):
    __tablename__ = "current_matches"
    id = Column(Integer, primary_key=True)
    matches_json = Column(String, default="[]")

# --- Pydantic Models ---
class Player(BaseModel):
    id: int
    name: str
    points: int
    has_received_bye: bool
    is_active: bool # 棄権フラグ
    display_order: int
    played_opponents: List[int] = []
    wins_against: List[int] = []
    losses_against: List[int] = []

    model_config = ConfigDict(from_attributes=True)

class PlayerCreate(BaseModel):
    name: str

class Match(BaseModel):
    player1: Player
    player2: Optional[Player] = None

class MatchResult(BaseModel):
    winner_id: int
    loser_id: Optional[int] = None

class TournamentState(BaseModel):
    current_round: int

# --- Lifespan Event ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    if db.query(TournamentInfo).count() == 0:
        db.add(TournamentInfo(current_round=0))
        db.commit()
    if db.query(CurrentMatchesDB).count() == 0:
        db.add(CurrentMatchesDB(matches_json="[]"))
        db.commit()
    db.close()
    yield

# --- FastAPI App ---
app = FastAPI(lifespan=lifespan)

origins = ["https://incredible-khapse-09fe41.netlify.app"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DB Dependency ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Helper functions ---
def convert_player_to_pydantic(player_db: PlayerDB) -> Player:
    return Player(
        id=player_db.id,
        name=player_db.name,
        points=player_db.points,
        has_received_bye=player_db.has_received_bye,
        is_active=player_db.is_active,
        display_order=player_db.display_order,
        played_opponents=[opp.id for opp in player_db.played_opponents]
    )

def enrich_player_data(players_db: List[PlayerDB], db: Session) -> List[Player]:
    all_records = db.query(MatchRecord).all()
    players_data = []
    for p in players_db:
        player_pydantic = convert_player_to_pydantic(p)
        player_pydantic.wins_against = [r.loser_id for r in all_records if r.winner_id == p.id]
        player_pydantic.losses_against = [r.winner_id for r in all_records if r.loser_id == p.id]
        players_data.append(player_pydantic)
    return players_data

# --- API Endpoints ---

@app.get("/")
async def read_root():
    return {"message": "Carom Tournament API is running with SQLite backend!"}

@app.get("/state", response_model=TournamentState)
async def get_tournament_state(db: Session = Depends(get_db)):
    state = db.query(TournamentInfo).first()
    return TournamentState(current_round=state.current_round)

@app.post("/players", response_model=Player)
async def create_player(player_data: PlayerCreate, db: Session = Depends(get_db)):
    db_player = db.query(PlayerDB).filter(PlayerDB.name == player_data.name).first()
    if db_player:
        raise HTTPException(status_code=400, detail="Player with this name already exists")
    
    current_player_count = db.query(PlayerDB).count()
    
    # Set has_received_bye to True for new players in later rounds
    state = db.query(TournamentInfo).first()
    has_received_bye = state.current_round > 0

    new_player_db = PlayerDB(
        name=player_data.name,
        display_order=current_player_count,
        has_received_bye=has_received_bye
    )
    db.add(new_player_db)
    db.commit()
    db.refresh(new_player_db)
    return convert_player_to_pydantic(new_player_db)

@app.post("/players/upload", response_model=List[Player])
async def upload_players_csv(file: UploadFile, db: Session = Depends(get_db)):
    if file.content_type not in ["text/csv", "application/vnd.ms-excel"]:
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload a CSV file.")

    contents = await file.read()
    # Handle potential BOM in UTF-8 files
    if contents.startswith(b'\ufeff'):
        contents = contents[3:]
        
    text_stream = io.StringIO(contents.decode("utf-8"))
    csv_reader = csv.reader(text_stream)
    
    players_to_add = []
    for row in csv_reader:
        if not row:  # Skip empty rows
            continue
        name = row[0].strip()
        if name:
            players_to_add.append(PlayerCreate(name=name))

    if not players_to_add:
        raise HTTPException(status_code=400, detail="CSV file is empty or contains no valid names.")

    created_players = []
    for player_data in players_to_add:
        db_player = db.query(PlayerDB).filter(PlayerDB.name == player_data.name).first()
        if db_player:
            # Skip if player already exists
            continue
        
        current_player_count = db.query(PlayerDB).count()
        state = db.query(TournamentInfo).first()
        has_received_bye = state.current_round > 0

        new_player_db = PlayerDB(
            name=player_data.name,
            display_order=current_player_count,
            has_received_bye=has_received_bye
        )
        db.add(new_player_db)
        db.commit()
        db.refresh(new_player_db)
        created_players.append(convert_player_to_pydantic(new_player_db))

    return created_players

@app.get("/players", response_model=List[Player])
async def get_players(db: Session = Depends(get_db)):
    players_db = db.query(PlayerDB).order_by(PlayerDB.display_order).all()
    return enrich_player_data(players_db, db)

@app.post("/players/{player_id}/withdraw", response_model=Player)
async def withdraw_player(player_id: int, db: Session = Depends(get_db)):
    player_db = db.query(PlayerDB).filter(PlayerDB.id == player_id).first()
    if not player_db:
        raise HTTPException(status_code=404, detail="Player not found")
    player_db.is_active = False
    db.commit()
    db.refresh(player_db)
    
    player_pydantic = convert_player_to_pydantic(player_db)
    wins = db.query(MatchRecord).filter(MatchRecord.winner_id == player_db.id).all()
    losses = db.query(MatchRecord).filter(MatchRecord.loser_id == player_db.id).all()
    player_pydantic.wins_against = [w.loser_id for w in wins]
    player_pydantic.losses_against = [l.winner_id for l in losses]
    return player_pydantic

@app.post("/players/shuffle", response_model=List[Player])
async def shuffle_players(db: Session = Depends(get_db)):
    players_db = db.query(PlayerDB).filter(PlayerDB.is_active == True).all()
    random.shuffle(players_db)
    
    for i, player in enumerate(players_db):
        player.display_order = i
    
    db.commit()
    
    shuffled_players_db = db.query(PlayerDB).order_by(PlayerDB.display_order).all()
    return enrich_player_data(shuffled_players_db, db)

@app.post("/matches/generate", response_model=List[Match])
async def generate_matches(db: Session = Depends(get_db)):
    matches_state = db.query(CurrentMatchesDB).first()
    if json.loads(matches_state.matches_json):
         raise HTTPException(status_code=400, detail="All matches must be completed before generating a new round.")

    state = db.query(TournamentInfo).first()
    state.current_round += 1
    
    players_db = db.query(PlayerDB).filter(PlayerDB.is_active == True).all()
    all_players = enrich_player_data(players_db, db)
    
    if state.current_round == 1:
        sorted_players = sorted(all_players, key=lambda p: p.display_order)
    else:
        sorted_players = sorted(all_players, key=lambda p: p.points, reverse=True)
    unpaired_players = list(sorted_players)
    new_matches = []

    if len(unpaired_players) % 2 != 0:
        # Players who have already had a bye are prioritized for matches.
        # New players who joined mid-tournament are not eligible for a bye in their first round.
        eligible_for_bye = [p for p in unpaired_players if not p.has_received_bye]
        
        bye_player_pydantic = None
        if eligible_for_bye:
            # The player with the lowest score among those who have not had a bye receives a bye.
            bye_player_pydantic = min(eligible_for_bye, key=lambda p: p.points)
        else:
            # If all players have had a bye, the player with the lowest score gets the bye.
            bye_player_pydantic = min(unpaired_players, key=lambda p: p.points)
        
        if bye_player_pydantic:
            bye_player_db = db.query(PlayerDB).filter(PlayerDB.id == bye_player_pydantic.id).first()
            bye_player_db.points += 1
            bye_player_db.has_received_bye = True
            # Find the full player object from unpaired_players to remove
            player_to_remove = next((p for p in unpaired_players if p.id == bye_player_pydantic.id), None)
            if player_to_remove:
                unpaired_players.remove(player_to_remove)
            new_matches.append(Match(player1=bye_player_pydantic, player2=None))

    while len(unpaired_players) >= 2:
        player1 = unpaired_players.pop(0)
        
        # Note: played_opponents in player1 does not have the latest data from this session
        # We should re-fetch played opponents based on wins/losses for an accurate check
        played_opponent_ids = player1.wins_against + player1.losses_against
        
        possible_opponents = [p for p in unpaired_players if p.id not in played_opponent_ids]
        
        if not possible_opponents:
            # If all remaining players have been played, just pick the first one
            if unpaired_players:
                possible_opponents = unpaired_players

        if possible_opponents:
            # Sort by points to match with the closest opponent
            best_opponent = sorted(possible_opponents, key=lambda p: p.points)[0]
            unpaired_players.remove(best_opponent)
            new_matches.append(Match(player1=player1, player2=best_opponent))

    matches_state.matches_json = json.dumps([m.dict() for m in new_matches])
    db.commit()
    return new_matches

@app.get("/matches", response_model=List[Match])
async def get_matches(db: Session = Depends(get_db)):
    matches_state = db.query(CurrentMatchesDB).first()
    matches_dict = json.loads(matches_state.matches_json)
    return [Match(**m) for m in matches_dict]

@app.post("/matches/result")
async def record_match_result(result: MatchResult, db: Session = Depends(get_db)):
    winner = db.query(PlayerDB).filter(PlayerDB.id == result.winner_id).first()

    # Check if the match being recorded is a bye match
    is_bye_match_in_current_matches = False
    matches_state = db.query(CurrentMatchesDB).first()
    current_matches = [Match(**m) for m in json.loads(matches_state.matches_json)]
    
    bye_match_to_remove = None
    for match in current_matches:
        if match.player2 is None and match.player1.id == result.winner_id:
            is_bye_match_in_current_matches = True
            bye_match_to_remove = match
            break

    if is_bye_match_in_current_matches:
        # If it's a bye match, we don't need a loser.
        # We just need to remove it from current_matches.
        if not winner:
            raise HTTPException(status_code=404, detail="Winner player not found for bye.")
        
        # Remove the bye match from current_matches
        if bye_match_to_remove:
            current_matches.remove(bye_match_to_remove)
        
        # No points or played_opponents update needed here for bye, as it's done during generation.
        # No MatchRecord for bye.

    else:
        # This is a regular match, so we need a valid loser.
        loser = db.query(PlayerDB).filter(PlayerDB.id == result.loser_id).first()
        if not winner or not loser:
            raise HTTPException(status_code=404, detail="Player not found for regular match.")

        # Check if this match has already been recorded to prevent duplicate point additions
        existing_record = db.query(MatchRecord).filter(
            ((MatchRecord.winner_id == result.winner_id) & (MatchRecord.loser_id == result.loser_id)) |
            ((MatchRecord.winner_id == result.loser_id) & (MatchRecord.loser_id == result.winner_id))
        ).first()

        if not existing_record:
            winner.points += 1
            if loser not in winner.played_opponents:
                 winner.played_opponents.append(loser)
            if winner not in loser.played_opponents:
                 loser.played_by.append(winner)
            
            new_match_record = MatchRecord(winner_id=result.winner_id, loser_id=result.loser_id)
            db.add(new_match_record)

        # Remove the regular match from current_matches
        match_to_remove = None
        for match in current_matches:
            if match.player2 and (
                (match.player1.id == winner.id and match.player2.id == loser.id) or
                (match.player1.id == loser.id and match.player2.id == winner.id)
            ):
                match_to_remove = match
                break
        if match_to_remove:
            current_matches.remove(match_to_remove)

    # Check if only bye matches are left
    only_byes_left = True
    if not current_matches:
        only_byes_left = False
    else:
        for match in current_matches:
            if match.player2 is not None:
                only_byes_left = False
                break
    
    if only_byes_left:
        current_matches = []

    # Save the updated current_matches back to DB
    matches_state.matches_json = json.dumps([m.dict() for m in current_matches])
    db.commit()
    return {"message": "Match result recorded successfully"}

@app.post("/reset")
async def reset_tournament(db: Session = Depends(get_db)):
    db.execute(played_opponents_association.delete())
    db.query(MatchRecord).delete()
    db.query(PlayerDB).delete()
    db.query(TournamentInfo).delete()
    db.query(CurrentMatchesDB).delete()
    
    db.add(TournamentInfo(current_round=0))
    db.add(CurrentMatchesDB(matches_json="[]"))
    
    db.commit()
    return {"message": "Tournament has been reset"}