import os
import boto3
from botocore.exceptions import ClientError
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from dotenv import load_dotenv

# Load .env file for local development
load_dotenv()

# MongoDB Setup
db = None

def connect_mongodb(uri=None):
    global db
    
    # 1. Try URI from Environment Variable (Best for Render/VPS)
    if not uri:
        uri = os.environ.get('MONGODB_URI')
        
    if uri:
        try:
            client = MongoClient(uri, server_api=ServerApi('1'))
            # Use 'url_series' as the database name
            db = client.get_database('url_series')
            print("Successfully connected to MongoDB")
            return True
        except Exception as e:
            print(f"MongoDB Connection Error: {e}")
            db = None
    return False

# Initial connection attempt
connect_mongodb()

def load_config():
    # Load config exclusively from MongoDB
    if db is not None:
        try:
            config = db.settings.find_one({'type': 'config'})
            if config:
                config_data = dict(config)
                config_data.pop('_id', None)
                config_data.pop('type', None)
                return config_data
        except Exception as e:
            print(f"Error loading config from MongoDB: {e}")
    return {}

def save_config(config_data):
    # Save config exclusively to MongoDB
    if db is not None:
        try:
            db.settings.update_one(
                {'type': 'config'},
                {'$set': config_data},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"Error saving config to MongoDB: {e}")
    return False

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
        region_name='apac'
    )

def list_series_folders():
    s3 = get_s3_client()
    config = load_config()
    bucket = config.get('r2_bucket_name', 'data-series')
    if not s3: return []
    
    try:
        result = s3.list_objects_v2(Bucket=bucket, Prefix='series/', Delimiter='/')
        folders = []
        if 'CommonPrefixes' in result:
            for prefix in result['CommonPrefixes']:
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
                if key == prefix: continue
                
                sub_path = key[len(prefix):]
                parts = sub_path.split('/')
                
                if len(parts) == 1:
                    images.append(sub_path)
                elif len(parts) >= 2:
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
    if content_type: extra_args['ContentType'] = content_type
        
    try:
        s3.upload_file(local_path, bucket, s3_key, ExtraArgs=extra_args)
        return True
    except ClientError as e:
        print(f"Error uploading {local_path}: {e}")
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
            if result.get('IsTruncated'):
                result = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, ContinuationToken=result.get('NextContinuationToken'))
            else: break
        return True
    except ClientError as e:
        print(f"Error deleting series: {e}")
        return False
