import json
from pathlib import Path

from careerforge.api.app import app

root = Path(__file__).resolve().parents[1]
(root / "docs/api").mkdir(parents=True, exist_ok=True)
(root / "docs/api/openapi.json").write_text(
    json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("Exported docs/api/openapi.json")
