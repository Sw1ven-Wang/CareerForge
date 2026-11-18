import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import json
import tempfile
from pathlib import Path

from careerforge.api import create_app
from careerforge.application import Application
from careerforge.config import Settings

# Export schema without initializing production credentials, making requests or bootstrapping an account.
with tempfile.TemporaryDirectory() as d:
    service = Application(
        Settings(mode="development", database_url="sqlite+pysqlite:///" + d + "/schema.db")
    )
    schema = create_app(service).openapi()
    dest = Path("docs/openapi.json")
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    service.db.engine.dispose()
    print(dest)
