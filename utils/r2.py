from series_manager.db import connect_mongodb, db
from series_manager.services.r2_service import (
    create_series_folder,
    delete_ep_folder,
    delete_object,
    delete_series_folder,
    get_s3_client,
    list_folder_contents,
    list_series_folders,
    upload_file_to_r2,
)
from series_manager.services.settings_service import load_config, save_config

connect_mongodb()
