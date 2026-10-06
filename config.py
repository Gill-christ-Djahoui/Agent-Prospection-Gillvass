"""Chemins et journalisation. Aucune valeur métier ici : tout vit dans la table `reglages`."""
import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "prospect.db"
ENV_PATH = BASE_DIR / ".env"
LOG_DIR = DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "prospect.log"

# Serveur local : accessible depuis cette machine uniquement
HOST = "127.0.0.1"
PORT = 5000

# Endpoints compatibles OpenAI proposés en suggestion dans les Réglages (le choix reste libre)
ENDPOINTS_IA_SUGGERES = [
    "https://api.groq.com/openai/v1/chat/completions",
    "https://api.x.ai/v1/chat/completions",
    "https://api.openai.com/v1/chat/completions",
]


def configurer_logging():
    """Branche le journal horodaté (data/logs/prospect.log). Sans effet si déjà fait."""
    journal = logging.getLogger("prospect")
    if journal.handlers:
        return journal
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    journal.addHandler(handler)
    journal.setLevel(logging.INFO)
    return journal
