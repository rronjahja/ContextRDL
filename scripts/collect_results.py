"""Package this computer's campaign outputs, including logs if a command failed.

Run from any directory: python scripts/collect_results.py
Creates ContextRDL-local-results.zip in the repository root. Source files and
previous result backups are excluded. No experimental outcomes are supplied by
this script; it only packages files produced by the user's run.
"""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    results = root / "results"
    if not (results / "regeneration.json").is_file():
        raise SystemExit("No campaign manifest. Run python scripts/regenerate.py --fresh first.")
    archive = root / "ContextRDL-local-results.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as output:
        for path in sorted(results.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(root).as_posix())
    print("Created " + str(archive))


if __name__ == "__main__":
    main()
