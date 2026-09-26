import os

# A developer's own .env (real keys, their DATA_ROOT) must never leak into the test run.
os.environ["TESTATLAS_ENV_FILE"] = ""
