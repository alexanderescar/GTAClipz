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


def fetch(url, public_base, bucket, dest):
    """Pull a clip we uploaded earlier back down (each cloud run starts empty)."""
    key = url[len(public_base.rstrip("/")) + 1:]
    client().download_file(bucket, key, str(dest))
    return dest


def delete(key, bucket):
    client().delete_object(Bucket=bucket, Key=key)


def selftest():
    """Upload a small file and fetch it back over the public URL the way
    different clients would. Leaves selftest/check.txt in place so you can
    open it in a browser."""
    import time
    import urllib.request
    from common import cfg
    st = cfg()["storage"]
    key = "selftest/check.txt"
    path = "/tmp/r2_selftest.txt"
    with open(path, "w") as fh:
        fh.write(f"gtaclipz r2 ok {int(time.time())}\n")
    client().upload_file(path, st["r2_bucket"], key,
                         ExtraArgs={"ContentType": "text/plain"})
    print("  upload: OK")
    url = f"{st['r2_public_base'].rstrip('/')}/{key}"
    print(f"  open this in your browser: {url}")
    agents = {
        "python default": None,
        "browser": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "instagram fetcher": "facebookexternalhit/1.1",
    }
    for name, ua in agents.items():
        req = urllib.request.Request(url, headers={"User-Agent": ua} if ua else {})
        try:
            body = urllib.request.urlopen(req, timeout=20).read().decode().strip()
            print(f"  fetch as {name:<18} OK  ({body!r})")
        except Exception as e:  # noqa: BLE001
            print(f"  fetch as {name:<18} FAILED  {e}")
