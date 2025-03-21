import os
import logging as log
import calendar
import pandas as pd
from datetime import date, datetime, timezone, timedelta
from google.cloud import bigquery as bq
from google.cloud import storage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.api_core.exceptions import Forbidden, NotFound

'''
DATETIME constants
'''
TIME_ZONE = timezone(timedelta(hours=8))
START_DATE = datetime(2025, 3, 11, tzinfo=TIME_ZONE)

'''
CREDENTIALS	
'''
JSON_KEYS_PATH = 'json-keys/gch-prod-dwh01-data-pipeline.json'
# JSON_KEYS_PATH = '/home/yanzhe/gch-prod-dwh01/json-keys/gch-prod-dwh01-data-pipeline.json'
SERVICE_ACCOUNT = f'{JSON_KEYS_PATH}'

# set up credentials for BQ and Drive to query data
credentials = service_account.Credentials.from_service_account_file(JSON_KEYS_PATH)
bq_client = bq.Client(credentials=credentials, project=credentials.project_id)
bucket_client = storage.Client(credentials=credentials, project=credentials.project_id)

'''
LOCAL FILE PATHS
'''
SQL_SCRIPTS_PATH = 'sql-scripts/sc-possalesrl'
# SQL_SCRIPTS_PATH = '/home/yanzhe/gch-prod-dwh01/sql-scripts/sc-possalesrl'

OUTFILES_DIR = '/mnt/c/Users/Asus/Desktop/outfiles'
# OUTFILES_DIR = '/home/yanzhe/outfiles'
os.makedirs(OUTFILES_DIR, exist_ok=True)

PY_LOGS_DIR = '/mnt/c/Users/Asus/Desktop/py_log'
# PY_LOGS_DIR = '/home/yanzhe/py_log'
os.makedirs(PY_LOGS_DIR, exist_ok=True)

'''
GOOGLE DRIVE PARAMS
'''
SCOPES = ['https://www.googleapis.com/auth/drive']

POSSALES_RL_FOLDER_ID = '1LYITa9mHJZXQyC21_75Ip8_oMwBanfcF' # use this for the actual prod
# POSSALES_RL_FOLDER_ID = '1iQDbpxsqa8zoEIREJANEWau6HEqPe7hF' # GCH Report > Supply Chain (mock drive)

'''
OUTPUT FILE CONFIG
'''
SLICE_BY_ROWS = 1000000 - 1

DEPARTMENTS = {
	'1': '1 - GROCERY',
	'2': '2 - FRESH',
	'3': '3 - PERISHABLES',
	'4': '4 - NON FOODS',
	'5': '5 - HEALTH & BEAUTY',
	'6': '6 - GMS'
}

DELIMITER = ','

'''
Logging
'''
month = calendar.month_name[datetime.now().month]
year = datetime.now().year

# create log dir for current month/year
LOG_DIR = f'{PY_LOGS_DIR}/{year}/{month}'
os.makedirs(LOG_DIR, exist_ok=True)

# create log file name with timestamp
log_file_name = f'{datetime.now().strftime("%Y%m%d_%H%M%S")}_pylog.txt'
log_file_fullpath = f'{LOG_DIR}/{log_file_name}'

# config logging
log.basicConfig(
    filename=log_file_fullpath,
    level=log.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# log to console for debugging
console_handler = log.StreamHandler()
console_handler.setLevel(log.INFO)
log.getLogger().addHandler(console_handler)

log.info('exapp_pipeline --initiated')

'''
Helper functions
'''

# get the names of specific file types in a dir
# if dir = None			: get from root
# if file_type = None	: get all file names
def file_type_in_dir(file_dir:str, file_type:str):
	if file_dir is None:
		files_in_dir = os.listdir()
	else:
		files_in_dir = os.listdir(file_dir)

	if file_type is None:
		return files_in_dir
	else:
		return [file for file in files_in_dir if file.endswith(file_type)]

# get current month and year
# return (mm,yyyy)
def get_month_year() -> tuple:
	month = calendar.month_name[datetime.now().month]
	year = datetime.now().year

	return (month, year)

# generate file name based on naming concentions
# infile:	possales_rl_{dept}.sql
# outfile:	possales_rl_{dept}_{date}_{ver}_{outfile_type}
# e.g. possales_rl_q.sql -> possales_rl_1_2025-03-16_2.csv
def gen_file_name(infile_name:str, infile_type:str, outfile_type:str, ver:int):
	file_name = f"{infile_name.replace(infile_type,'')}_{date.today()}_{ver}.{outfile_type}"
	return file_name

'''
Main processes
'''

# get data from BQ and export as CSV to outfiles/
# slices data by million rows
def query_data():
	sql_scripts = file_type_in_dir(SQL_SCRIPTS_PATH, '.sql')

	# run each script
	for script in sql_scripts:
		with open(f'{SQL_SCRIPTS_PATH}/{script}', 'r') as cur_script:
			query = ' '.join([line for line in cur_script])
			results_df = bq_client.query(query).to_dataframe()

			# print(f'SQL script: {script}')
			# print(f'Results: {results_df.shape}')

			# slice the results of eac script
			for cur_row in range(0, len(results_df), SLICE_BY_ROWS):
				# file_ver: 1 -> (0,99), 2 -> (100, 199) etc
				file_ver = cur_row // SLICE_BY_ROWS + 1
				# get subset of full query result (sliced by rows)
				subset = results_df.iloc[cur_row:cur_row + SLICE_BY_ROWS]
				out_filename = gen_file_name(script, '.sql', '.csv', file_ver)
				# upload subset as csv
				subset.to_csv(f'{OUTFILES_DIR}/{out_filename}', sep=DELIMITER, encoding='utf-8', index=False, header=True)
