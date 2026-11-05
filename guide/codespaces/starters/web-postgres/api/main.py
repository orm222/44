"""Small connectivity example; replace notes with the approved business model."""
import os
from pathlib import Path
from typing import List
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
import psycopg
from psycopg.rows import dict_row

app = FastAPI(title='Web/PostgreSQL starter')


def connect():
    password = Path(os.environ['DB_PASSWORD_FILE']).read_text(encoding='utf-8').strip()
    return psycopg.connect(host=os.environ.get('DB_HOST', 'db'),
        port=int(os.environ.get('DB_PORT', '5432')), dbname=os.environ.get('DB_NAME', 'app_local'),
        user=os.environ.get('DB_USER', 'app_local'), password=password, connect_timeout=3, row_factory=dict_row)


class NewNote(BaseModel):
    text: str = Field(min_length=1, max_length=1000)

    @field_validator('text')
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('Enter some text')
        return value


class Note(BaseModel):
    id: int
    text: str
    created_at: datetime


@app.get('/health/ready')
def ready():
    try:
        with connect() as connection:
            connection.execute('SELECT 1 FROM notes LIMIT 1')
        return {'status': 'ok'}
    except (psycopg.Error, OSError, KeyError):
        raise HTTPException(503, 'Database is not ready')


@app.get('/api/notes', response_model=List[Note])
def list_notes():
    try:
        with connect() as connection:
            return connection.execute('SELECT id, text, created_at FROM notes ORDER BY id DESC LIMIT 100').fetchall()
    except (psycopg.Error, OSError, KeyError):
        raise HTTPException(503, 'Database is unavailable')


@app.post('/api/notes', response_model=Note, status_code=201)
def add_note(note: NewNote):
    try:
        with connect() as connection:
            return connection.execute('INSERT INTO notes (text) VALUES (%s) RETURNING id, text, created_at',
                                      (note.text,)).fetchone()
    except (psycopg.Error, OSError, KeyError):
        raise HTTPException(503, 'Database is unavailable')
