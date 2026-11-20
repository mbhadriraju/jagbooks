import os

# Tests supply their own settings and must not load local credentials on import.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
