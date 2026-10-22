import os

# A developer's own .env (real keys, their DATA_ROOT) must never leak into the test run.
os.environ["TESTATLAS_ENV_FILE"] = ""

# Every mocked LLM call in the test suite would otherwise write a real line to
# the developer's own data/llm_calls.log (llm_logging.py computes its path at
# import time, independent of any per-test DATA_DIR/DB_PATH mocking) and spam
# stdout -- neither is wanted from a mock. See llm_logging.py's own check.
os.environ["TESTATLAS_DISABLE_LLM_LOG"] = "1"
