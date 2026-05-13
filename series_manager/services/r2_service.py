import boto3
from botocore.exceptions import ClientError

from .settings_service import load_config
from .validation import validate_name


def get_s3_client():
    config = load_config()
    account_id = config.get("cloudflare_account_id", "")
    access_key = config.get("cloudflare_access_key", "")
    secret_key = config.get("cloudflare_secret_key", "")

    if not all([account_id, access_key, secret_key]):
        return None

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="apac",
    )


def bucket_name():
    return load_config().get("r2_bucket_name", "data-series")


def public_domain():
    return load_config().get("worker_domain", "https://series.film01-thirx.workers.dev").rstrip("/")


def list_series_folders():
    s3 = get_s3_client()
    if not s3:
        return []
    try:
        result = s3.list_objects_v2(Bucket=bucket_name(), Prefix="series/", Delimiter="/")
        return [
            prefix["Prefix"].split("/")[-2]
            for prefix in result.get("CommonPrefixes", [])
        ]
    except ClientError as exc:
        print(f"Error listing series folders: {exc}")
        return []


def create_series_folder(series_name):
    s3 = get_s3_client()
    if not s3:
        return False
    series_name = validate_name(series_name, "Series name")
    try:
        s3.put_object(Bucket=bucket_name(), Key=f"series/{series_name}/")
        return True
    except ClientError as exc:
        print(f"Error creating series folder: {exc}")
        return False


def list_folder_contents(series_name):
    s3 = get_s3_client()
    if not s3:
        return {"images": [], "eps": []}
    series_name = validate_name(series_name, "Series name")
    prefix = f"series/{series_name}/"
    try:
        result = s3.list_objects_v2(Bucket=bucket_name(), Prefix=prefix)
        images = []
        eps = set()
        for item in result.get("Contents", []):
            key = item["Key"]
            if key == prefix:
                continue
            sub_path = key[len(prefix):]
            parts = sub_path.split("/")
            if len(parts) == 1:
                images.append(sub_path)
            elif len(parts) >= 2:
                eps.add(parts[0])
        return {"images": sorted(images), "eps": sorted(eps)}
    except ClientError as exc:
        print(f"Error listing folder contents: {exc}")
        return {"images": [], "eps": []}


def upload_file_to_r2(local_path, s3_key, content_type=None):
    s3 = get_s3_client()
    if not s3:
        return False
    extra_args = {"ContentType": content_type} if content_type else {}
    try:
        s3.upload_file(local_path, bucket_name(), s3_key, ExtraArgs=extra_args)
        return True
    except ClientError as exc:
        print(f"Error uploading {local_path}: {exc}")
        return False


def delete_object(key):
    s3 = get_s3_client()
    if not s3 or not key or not key.startswith("series/"):
        return False
    try:
        s3.delete_object(Bucket=bucket_name(), Key=key)
        return True
    except ClientError as exc:
        print(f"Error deleting object: {exc}")
        return False


def delete_prefix(prefix):
    s3 = get_s3_client()
    if not s3 or not prefix.startswith("series/"):
        return False
    try:
        token = None
        while True:
            kwargs = {"Bucket": bucket_name(), "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            result = s3.list_objects_v2(**kwargs)
            objects = [{"Key": obj["Key"]} for obj in result.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket_name(), Delete={"Objects": objects})
            if not result.get("IsTruncated"):
                break
            token = result.get("NextContinuationToken")
        return True
    except ClientError as exc:
        print(f"Error deleting prefix {prefix}: {exc}")
        return False


def delete_ep_folder(series_name, ep_name):
    series_name = validate_name(series_name, "Series name")
    ep_name = validate_name(ep_name, "Episode name")
    return delete_prefix(f"series/{series_name}/{ep_name}/")


def delete_series_folder(series_name):
    series_name = validate_name(series_name, "Series name")
    return delete_prefix(f"series/{series_name}/")
