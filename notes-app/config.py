"""
App settings.

- On your laptop: values come from the .env file (SQLite DB, no AWS needed).
- On EC2: set USE_SSM=true and everything is read from AWS SSM Parameter Store
  (no passwords in code or files).
"""
import os
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
SSM_PREFIX = os.getenv("SSM_PREFIX", "/notes-app")


def _load_from_ssm():
    import boto3

    ssm = boto3.client("ssm", region_name=AWS_REGION)
    keys = ["db/host", "db/name", "db/user", "db/password", "s3/bucket"]
    names = [f"{SSM_PREFIX}/{k}" for k in keys]
    resp = ssm.get_parameters(Names=names, WithDecryption=True)
    if resp.get("InvalidParameters"):
        raise RuntimeError(f"Missing SSM parameters: {resp['InvalidParameters']}")
    values = {p["Name"].replace(SSM_PREFIX + "/", ""): p["Value"] for p in resp["Parameters"]}
    return values


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
    AWS_REGION = AWS_REGION
    LOG_FILE = os.getenv("LOG_FILE", "")

    if os.getenv("USE_SSM", "false").lower() == "true":
        _v = _load_from_ssm()
        DB_ENGINE = os.getenv("DB_ENGINE", "postgresql")  # or "mysql+pymysql"
        DB_PORT = os.getenv("DB_PORT", "5432")
        from urllib.parse import quote_plus
        SQLALCHEMY_DATABASE_URI = (
            f"{DB_ENGINE}://{quote_plus(_v['db/user'])}:{quote_plus(_v['db/password'])}"
            f"@{_v['db/host']}:{DB_PORT}/{_v['db/name']}"
        )
        S3_BUCKET = _v["s3/bucket"]
    else:
        SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///local.db")
        S3_BUCKET = os.getenv("S3_BUCKET", "")
