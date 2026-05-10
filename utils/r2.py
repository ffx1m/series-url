import json
import os
import boto3
from botocore.exceptions import ClientError

CONFIG_FILE = 'data/config.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_config(config_data):
    os.makedirs('data', exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config_data, f)

def get_s3_client():
    config = load_config()
    account_id = config.get('cloudflare_account_id', '')
    access_key = config.get('cloudflare_access_key', '')
    secret_key = config.get('cloudflare_secret_key', '')
    
    if not all([account_id, access_key, secret_key]):
        return None

    return boto3.client(
        's3',
        endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name='apac' # or 'auto'
    )

def list_series_folders():
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return []
    
    try:
        # We simulate folders by looking for common prefixes under series/
        result = s3.list_objects_v2(Bucket=bucket, Prefix='series/', Delimiter='/')
        folders = []
        if 'CommonPrefixes' in result:
            for prefix in result['CommonPrefixes']:
                # prefix will be like "series/Name/"
                folder_name = prefix['Prefix'].split('/')[-2]
                folders.append(folder_name)
        return folders
    except ClientError as e:
        print(f"Error: {e}")
        return []

def create_series_folder(series_name):
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return False
    
    try:
        # Just put a dummy 0-byte object or let the "folder" be implied when uploading items
        s3.put_object(Bucket=bucket, Key=f'series/{series_name}/')
        return True
    except ClientError as e:
        print(f"Error: {e}")
        return False

def list_folder_contents(series_name):
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return {'images': [], 'eps': []}
    
    prefix = f'series/{series_name}/'
    try:
        result = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        images = []
        eps = set()
        
        if 'Contents' in result:
            for item in result['Contents']:
                key = item['Key']
                # Skip the exact folder placeholder
                if key == prefix:
                    continue
                
                # Check what type of item it is
                sub_path = key[len(prefix):] # e.g., "EP1/playlist.m3u8" or "cover.webp"
                parts = sub_path.split('/')
                
                if len(parts) == 1:
                    # File at root of series folder (usually image)
                    images.append(sub_path)
                elif len(parts) >= 2:
                    # Inside a subfolder (EP)
                    eps.add(parts[0])
        
        return {'images': images, 'eps': list(eps)}
    except ClientError as e:
        print(f"Error: {e}")
        return {'images': [], 'eps': []}

def delete_object(key):
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return False
    try:
        s3.delete_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        print(f"Error: {e}")
        return False

def upload_file_to_r2(local_path, s3_key, content_type=None):
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return False
    
    extra_args = {}
    if content_type:
        extra_args['ContentType'] = content_type
        
    try:
        s3.upload_file(local_path, bucket, s3_key, ExtraArgs=extra_args)
        return True
    except ClientError as e:
        print(f"Error uploading {local_path} to {s3_key}: {e}")
        return False

def delete_ep_folder(series_name, ep_name):
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return False
    
    prefix = f'series/{series_name}/{ep_name}/'
    try:
        result = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        if 'Contents' in result:
            objects_to_delete = [{'Key': obj['Key']} for obj in result['Contents']]
            s3.delete_objects(Bucket=bucket, Delete={'Objects': objects_to_delete})
        return True
    except ClientError as e:
        print(f"Error deleting EP folder: {e}")
        return False

def delete_series_folder(series_name):
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return False
    
    prefix = f'series/{series_name}/'
    try:
        result = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        while result.get('KeyCount', 0) > 0:
            objects_to_delete = [{'Key': obj['Key']} for obj in result['Contents']]
            s3.delete_objects(Bucket=bucket, Delete={'Objects': objects_to_delete})
            # Handle pagination if more than 1000 objects
            if result.get('IsTruncated'):
                result = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, ContinuationToken=result.get('NextContinuationToken'))
            else:
                break
        return True
    except ClientError as e:
        print(f"Error deleting series folder: {e}")
        return False
