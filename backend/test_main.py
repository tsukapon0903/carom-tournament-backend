import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from main import app, get_db, Base
import os

# --- Test Database Setup ---
SQLALCHEMY_DATABASE_URL = "sqlite:///./test.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base.metadata.create_all(bind=engine)

def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

# --- Tests ---
def test_upload_players_csv():
    with TestClient(app) as client:
        # Reset the database before the test
        client.post("/reset")

        # Create a dummy CSV file in memory
        csv_content = "Alice\nBob\nCharlie"
        files = {"file": ("test.csv", csv_content, "text/csv")}
        
        response = client.post("/players/upload", files=files)
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        assert data[0]["name"] == "Alice"
        assert data[1]["name"] == "Bob"
        assert data[2]["name"] == "Charlie"

        # Verify players are in the database
        response = client.get("/players")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        
        # Reset the database after the test
        client.post("/reset")

def test_upload_players_csv_with_existing_players():
    with TestClient(app) as client:
        # Reset the database before the test
        client.post("/reset")

        # Add a player first
        client.post("/players", json={"name": "Alice"})

        # Upload a CSV with the same player and a new one
        csv_content = "Alice\nDavid"
        files = {"file": ("test.csv", csv_content, "text/csv")}
        
        response = client.post("/players/upload", files=files)
        
        assert response.status_code == 200
        # "Alice" should be skipped, only "David" should be created
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "David"

        # Verify players in the database
        response = client.get("/players")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        
        # Reset the database after the test
        client.post("/reset")

def test_upload_empty_csv():
    with TestClient(app) as client:
        # Reset the database before the test
        client.post("/reset")

        csv_content = ""
        files = {"file": ("test.csv", csv_content, "text/csv")}
        
        response = client.post("/players/upload", files=files)
        
        assert response.status_code == 400
        assert response.json()["detail"] == "CSV file is empty or contains no valid names."
        
        # Reset the database after the test
        client.post("/reset")

def test_upload_invalid_file_type():
    with TestClient(app) as client:
        # Reset the database before the test
        client.post("/reset")

        files = {"file": ("test.txt", "some content", "text/plain")}
        
        response = client.post("/players/upload", files=files)
        
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid file type. Please upload a CSV file."
        
        # Reset the database after the test
        client.post("/reset")