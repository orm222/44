-- Only applied when PostgreSQL initializes an empty named volume.
CREATE TABLE notes (
    id BIGSERIAL PRIMARY KEY,
    text TEXT NOT NULL CHECK (char_length(btrim(text)) BETWEEN 1 AND 1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
