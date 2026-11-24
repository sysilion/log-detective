import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# keep tests hermetic: no gateway discovery, no local ollama probing
os.environ["LOGDET_DISCOVER"] = "0"
os.environ["LOGDET_AUTH_FILE"] = "/nonexistent/auth.json"
os.environ.setdefault("LOGDET_TZ", "Asia/Seoul")
for k in ("PINECONE_API_KEY", "OLLAMA_API_KEY", "GEMINI_API_KEY", "LOGDET_GEMINI_GATEWAY", "LOGDET_LLM", "LOGDET_PINECONE_GATEWAY", "LOGDET_OLLAMA_GATEWAY"):
    os.environ.pop(k, None)
