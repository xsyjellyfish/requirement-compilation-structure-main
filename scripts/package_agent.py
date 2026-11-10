"""Build a submission ZIP from an explicit source allowlist."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
output = root / "dist/mini_swe_arcbench_adapter.zip"
output.parent.mkdir(exist_ok=True)
with ZipFile(output, "w", ZIP_DEFLATED) as archive:
    for name in [
        "main.py",
        "arc_adapter.py",
        "progress.py",
        "arc_dify_pipeline.py",
        "mini6_dify_client_streaming.py",
        "mini6_dify_batch.py",
        "mini6_dify_run_tests.py",
        "requirements.txt",
        "pytest.ini",
        "README.md",
        "DIFY_INTEGRATION.md",
        "scripts",
        "tests",
        "web_template",
    ]:
        entry = root / name
        for path in [entry] if entry.is_file() else sorted(entry.rglob("*")):
            if not path.is_file() or any(
                part in {".env", ".DS_Store", "__pycache__", "node_modules", "dist", "outputs", "input", ".npmrc"}
                or part.startswith(".env.")
                for part in path.relative_to(root).parts
            ):
                continue
            archive.write(path, path.relative_to(root))
print(output)
