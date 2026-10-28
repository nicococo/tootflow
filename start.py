"""TootFlow entry point — `uv run start.py`.

Loads .env, starts the app. The SQLite database (or PostgreSQL) is created
automatically on first startup by the app's lifespan hook.
"""
from dotenv import load_dotenv

load_dotenv()

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
