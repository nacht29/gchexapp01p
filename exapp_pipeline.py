import pendulum
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.models.param import Param
from airflow.utils.log.logging_mixin import LoggingMixin

import os
import warnings
import subprocess
import calendar
import pandas as pd
from datetime import date, datetime
from google.cloud import bigquery as bq
from google.cloud import storage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.api_core.exceptions import Forbidden, NotFound

TIME_ZONE = pendulum.timezone('Asia/Singapore')
START_DATE = datetime(2025, 3, 11, tzinfo=TIME_ZONE)

# JSON_KEYS_PATH = 'json-keys/gch-prod-dwh01-data-pipeline.json'
JSON_KEYS_PATH = '/home/yanzhe/gchexapp01p/json-keys/gch-prod-dwh01-data-pipeline.json'
SERVICE_ACCOUNT = f'{JSON_KEYS_PATH}'

# Google Drive params
SCOPES = ['https://www.googleapis.com/auth/drive']
# POSSALES_RL_FOLDER_ID = '1LYITa9mHJZXQyC21_75Ip8_oMwBanfcF' # use this for the actual prod
POSSALES_RL_FOLDER_ID = '1iQDbpxsqa8zoEIREJANEWau6HEqPe7hF' # GCH Report > Supply Chain (mock drive)

# SQL_SCRIPTS_PATH = 'sql-scripts/sc-possalesrl/'
SQL_SCRIPTS_PATH = '/home/yanzhe/gchexapp01p/sql-scripts/sc-possalesrl/'

SLICE_BY_ROWS = 1000000 - 1

DEPARTMENTS = {
	'1': '1 - GROCERY',
	'2': '2 - FRESH',
	'3': '3 - PERISHABLES',
	'4': '4 - NON FOODS',
	'5': '5 - HEALTH & BEAUTY',
	'6': '6 - GMS'
}

# set up BQ credentials to query data
credentials = service_account.Credentials.from_service_account_file(JSON_KEYS_PATH)
bq_client = bq.Client(credentials=credentials, project=credentials.project_id)
bucket_client = storage.Client(credentials=credentials, project=credentials.project_id)

def file_type_in_dir(file_dir:str, file_type:str):
	if file_dir is None:
		files_in_dir = os.listdir()
	else:
		files_in_dir = os.listdir(file_dir)

	if file_type is None:
		return files_in_dir
	else:
		return [file for file in files_in_dir if file.endswith(file_type)]

def gen_file_name(infile_name:str, infile_type:str, outfile_type:str, ver:int):
	file_name = f"{infile_name.replace(infile_type,'')}_{date.today()}_{ver}.{outfile_type}"
	return file_name

# return (mm,yyyy)
def get_month_year() -> tuple:
	month = calendar.month_name[datetime.now().month]
	year = datetime.now().year

	return (month, year)

def query_data():
	sql_scripts = file_type_in_dir(SQL_SCRIPTS_PATH, '.sql')

	# run each script
	for script in sql_scripts:
		with open(f'{SQL_SCRIPTS_PATH}{script}', 'r') as cur_script:
			query = ' '.join([line for line in cur_script])
			results_df = bq_client.query(query).to_dataframe()

			print(f'SQL script: {script}')
			print(f'Results: {results_df.shape}')

			# slice the results of eac script
			for cur_row in range(0, len(results_df), SLICE_BY_ROWS):
				# file_ver: 1 -> (0,99), 2 -> (100, 199) etc
				file_ver = cur_row // SLICE_BY_ROWS + 1
				# get subset of full query result (sliced by rows)
				subset = results_df.iloc[cur_row:cur_row + SLICE_BY_ROWS]
				out_filename = gen_file_name(script, '.sql', '.csv', file_ver)
				# upload subset as csv
				subset.to_csv(f'{out_filename}', sep='|', encoding='utf-8', index=False, header=True)

def filepath_in_bucket(file_name:str):
	month, year = get_month_year()
	all_dept = DEPARTMENTS
	# possales_r1_10_2025-03-11.csv -> 10_2025-03-11.csv -> 10
	dept_id = file_name.replace('possales_rl_', '').split('_')[0]
	dept_name = all_dept[dept_id]
	return f'supply_chain/possales_rl/{year}/{month}/{dept_name}/{file_name}'

def load_bucket():
	bucket = bucket_client.get_bucket('gch_extract_drive_01')

	csv_files = file_type_in_dir(None, '.csv')
	for csv_file in csv_files:
		path_in_bucket = f'{filepath_in_bucket(csv_file)}'
		# bucket.blob(path_in_bucket).upload()
		blob = bucket.blob(path_in_bucket)
		blob.upload_from_filename(csv_file)

def drive_autodetect_folders(service, parent_folder_id:str, folder_name:str):
	'''
	# searches if folder exists in drive
	# returns a list of dict
	# id = file/folder id
	# name = file/folder name
	files = [
		{'id':0, 'name':'A'},
		{'id':1, 'name':'B'}
	]
	'''

	query = f"""
	'{parent_folder_id}' in parents 
	and name='{folder_name}'
	and mimeType='application/vnd.google-apps.folder' 
	and trashed=false
	"""

	results = service.files().list(q=query, fields='files(id,name)').execute()
	files_in_drive = results.get('files') # files_in_drive = results.get('files', [])

	if files_in_drive:
		return files_in_drive[0]['id']
	else:
		file_metadata = {
			'name': folder_name,
			'mimeType': 'application/vnd.google-apps.folder',
			'parents': [parent_folder_id]
		}

		folder = service.files().create(
			body=file_metadata,
			fields='id'
		).execute()

		return folder['id']

def get_file_dept(file_name:str) -> str:
	dept = DEPARTMENTS

	# get the number after possales - department id
	file_name = file_name.replace('possales_rl_', '').split('_')[0]
	# return corresponding department name accoridng to department number
	return dept[file_name[0]]

def load_gdrive():
	# authenticate
	creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT, scopes=SCOPES)
	service = build('drive', 'v3', credentials=creds)
	month, year = get_month_year()

	# auto detect folders - create folder if destination folder does not exists
	year_folder_id = drive_autodetect_folders(service, POSSALES_RL_FOLDER_ID, year)
	month_folder_id = drive_autodetect_folders(service, year_folder_id, month)

	# get name of all files to be loaded
	csv_files = file_type_in_dir(None, '.csv')

	for csv_file in csv_files:
		dept = get_file_dept(csv_file)
		dept_folder_id = drive_autodetect_folders(service, month_folder_id, dept)

		query = f"""
		'{dept_folder_id}' in parents
		and name = '{csv_file}'
		and trashed=false
		"""

		results = service.files().list(q=query, fields='files(id, name)').execute()
		dup_files = results.get('files')

		if dup_files:
			for dup_file in dup_files:
				service.files().delete(fileId=dup_file['id']).execute()

		file_metadata = {
			'name':csv_file,
			'parents': [dept_folder_id]
		}

		file = service.files().create(
			body=file_metadata,
			media_body=csv_file
		).execute()

def remove_outfiles():
	csv_files = file_type_in_dir(None, '.csv')
	for csv_file in csv_files:
		os.remove(csv_file)

# query_data()
# load_bucket()
## remove_outfiles()
# try:
# 	load_gdrive()
# 	remove_outfiles()
# except Exception:
# 	remove_outfiles()
# 	raise

# 15 07 * * *
with DAG(
	'exapp_pipeline',
	start_date=START_DATE,
	schedule="15 07 * * *",
	catchup=True
) as dag:
	
	task_query_data = PythonOperator(
		task_id='query_data',
		python_callable=query_data,
	)

	task_load_bucket = PythonOperator(
		task_id='load_bucket',
		python_callable=load_bucket
	)

	task_load_gdrive = PythonOperator(
		task_id='load_gdrive',
		python_callable=load_gdrive
	)

	task_remove_outfiles = PythonOperator(
		task_id='remove_outfiles',
		python_callable=remove_outfiles
	)
	
	task_query_data >> [task_load_bucket, task_load_gdrive]
	[task_load_bucket, task_load_gdrive] >> task_remove_outfiles
