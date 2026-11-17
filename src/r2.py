"""Cloudflare R2 upload — Instagram needs a PUBLIC video_url, not a file.

R2 free tier: 10GB storage, 1M writes/mo, 10M reads/mo, and ZERO egress
charges forever. A 6MB Short means ~1,600 clips fit in the free tier.
Set storage.keep_renders_days and the janitor keeps you well under it.
"""
import boto3
from botocore.config import Config

from common import env, log


def client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def put_public(path, key, bucket, public_base):
    client().upload_file(
        str(path), bucket, key, ExtraArgs={"ContentType": "video/mp4"}
    )
    url = f"{public_base.rstrip('/')}/{key}"
    log.info("  r2 -> %s", url)
    return url


def delete(key, bucket):
    client().delete_object(Bucket=bucket, Key=key)
