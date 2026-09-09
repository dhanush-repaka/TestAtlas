FROM python:3.13-slim

# git is required at runtime to clone/fetch Azure DevOps repos (ado/client.py)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Everything that must survive a restart -- the SQLite DB, run history, stored
# graphs, the PAT encryption key (data/), and cloned Azure DevOps repos
# (workspace/) -- lives under one mount point, since most single-VM platforms
# (Fly Machines included) only support one volume per machine. Set DATA_ROOT
# to point data/ and workspace/ elsewhere; defaults to the repo root, i.e. this
# VOLUME, when unset.
VOLUME ["/app/persist"]

EXPOSE 8000

# BASE_PATH: set to e.g. "/testatlas" when reverse-proxied under a sub-path (see README).
# TESTATLAS_PASSWORD: set this before exposing the container publicly -- see server/auth.py.
# DATA_ROOT: set to /app/persist to match the VOLUME above in production.
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
