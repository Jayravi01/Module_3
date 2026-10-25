"""AWS Lambda entry point, triggered daily by EventBridge Scheduler.

After the run, the full-history CSV is written to S3 (if EXPORT_BUCKET is set)
so Tableau, Excel or anyone at Medibank can pull it directly — no approval step.
"""
from __future__ import annotations

import json
import logging
import os

from .config import Settings
from .export import records_to_csv
from .pipeline import run_daily
from .storage import get_store

logging.getLogger().setLevel(logging.INFO)


def lambda_handler(event, context):
    settings = Settings()
    store = get_store(settings)
    try:
        summary = run_daily(settings=settings, store=store)
        bucket = os.getenv("EXPORT_BUCKET")
        if bucket:
            import boto3
            body = records_to_csv(store.feed()).encode("utf-8-sig")
            s3 = boto3.client("s3")
            key = os.getenv("EXPORT_KEY", "module3/regulatory_records.csv")
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/csv")
            # dated copy as an immutable daily archive
            s3.put_object(Bucket=bucket, Key=f"module3/archive/{summary['run_date']}.csv",
                          Body=body, ContentType="text/csv")
            summary["export"] = f"s3://{bucket}/{key}"
        return {"statusCode": 200, "body": json.dumps(summary)}
    finally:
        store.close()
