"""Build the AWS Lambda upload package: build/lambda.zip

    python scripts/build_lambda.py

Works the same on Mac, Windows and Linux. It downloads the Linux (x86_64, Python 3.12)
versions of every dependency, so the zip runs on Lambda even when it's built on a Mac.

The zip contains:
    visa_monitor/            the code
    visa_monitor/sources.yaml  a copy of config/sources.yaml (Lambda reads this one)
    psycopg, requests, ...   dependencies from requirements-lambda.txt

Upload it either way:
  * Console: Lambda -> your function -> Code -> Upload from -> .zip file
    Handler: visa_monitor.handler.lambda_handler   Runtime: Python 3.12 (x86_64)
  * SAM: see infra/deploy.md
If the zip is over 50 MB, upload it to S3 first and point Lambda at the S3 file.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
STAGE = BUILD / "lambda"
OUT = BUILD / "lambda.zip"

SKIP_DIRS = {"__pycache__", "tests", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--python-version", default="3.12")
    p.add_argument("--arch", choices=["x86_64", "arm64"], default="x86_64",
                   help="Must match the Lambda function's architecture")
    p.add_argument("--skip-deps", action="store_true", help="Code only (for quick checks)")
    args = p.parse_args()

    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)

    print("==> Copying code")
    shutil.copytree(ROOT / "src" / "visa_monitor", STAGE / "visa_monitor",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "config" / "sources.yaml", STAGE / "visa_monitor" / "sources.yaml")

    if not args.skip_deps:
        platform = "manylinux2014_x86_64" if args.arch == "x86_64" else "manylinux2014_aarch64"
        print(f"==> Downloading Linux dependencies ({platform}, Python {args.python_version})")
        subprocess.run([
            sys.executable, "-m", "pip", "install", "-q",
            "-r", str(ROOT / "requirements-lambda.txt"),
            "--target", str(STAGE),
            "--platform", platform,
            "--implementation", "cp",
            "--python-version", args.python_version,
            "--only-binary=:all:",
            "--upgrade",
        ], check=True)

    print("==> Zipping")
    OUT.unlink(missing_ok=True)
    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(STAGE.rglob("*")):
            rel = f.relative_to(STAGE)
            if f.is_dir() or f.suffix in SKIP_SUFFIXES or SKIP_DIRS.intersection(rel.parts):
                continue
            zf.write(f, rel.as_posix())
            count += 1
    size_mb = OUT.stat().st_size / 1_048_576
    print(f"\nBuilt {OUT}  ({count} files, {size_mb:.1f} MB)")
    print("Handler: visa_monitor.handler.lambda_handler   Runtime: Python "
          f"{args.python_version} ({args.arch})")
    if size_mb > 50:
        print("Over 50 MB: upload it to S3 and use 'Upload from -> Amazon S3 location'.")
    print("Environment variables to set on the function: STORAGE_BACKEND=postgres, DATABASE_URL, "
          "OPENAI_API_KEY, TIMEZONE=Australia/Melbourne, ENABLE_JS_RENDER=false "
          "(optional: ALERT_WEBHOOK_URL, EXPORT_BUCKET)")


if __name__ == "__main__":
    main()
