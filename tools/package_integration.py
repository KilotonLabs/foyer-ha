"""Package only integration source, never caches, notes, or credentials."""
from pathlib import Path
import hashlib
import json
import zipfile

root = Path(__file__).resolve().parents[1]
dist = root / "dist"
dist.mkdir(exist_ok=True)
source = root / "custom_components" / "foyer"
with zipfile.ZipFile(dist / "foyer-integration.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".json", ".yaml"} and "__pycache__" not in path.parts:
            archive.write(path, path.relative_to(root).as_posix())
    archive.write(root / "LICENSE", "custom_components/foyer/LICENSE")  # GPL: the license travels with every copy
checksums = {}
for path in sorted(dist.glob("*.zip")):
    checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
(dist / "checksums.json").write_text(json.dumps(checksums, indent=2) + "\n", encoding="utf-8")
print(f"Packaged integration: {len(list(source.rglob('*.py')))} Python modules; SHA-256 checksums written.")
