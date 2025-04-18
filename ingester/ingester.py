import kcidb
import tempfile
import os
import argparse
from kcidb import io, db, mq, orm, oo, monitor, tests, unittest, misc # noqa
import json
import time

# default database
DATABASE = "postgresql:dbname=kcidb user=kcidb password=kcidb host=localhost port=5432"

def get_db_credentials():
    global DATABASE
    pgpass = os.environ.get("PG_PASS")
    if not pgpass:
        raise Exception("PGPASS environment variable not set")
    (pgpass_fd, pgpass_filename) = tempfile.mkstemp(suffix=".pgpass")
    with os.fdopen(pgpass_fd, mode="w", encoding="utf-8") as pgpass_file:
        pgpass_file.write(pgpass)
    os.environ["PGPASSFILE"] = pgpass_filename
    db_uri = os.environ.get("PG_DSN")
    if db_uri:
        DATABASE = db_uri


def get_db_client(database):
    get_db_credentials()
    db = kcidb.db.Client(database)
    return db

def ingest_submissions(spool_dir, db_client=None):
    if db_client is None:
        raise Exception("db_client is None")
    io_schema = db_client.get_schema()[1]
    # iterate over all files in the directory spool_dir
    for filename in os.listdir(spool_dir):
        print(f"Ingesting {filename}")
        try:
            with open(os.path.join(spool_dir, filename), "r") as f:
                fsize = os.path.getsize(os.path.join(spool_dir, filename))
                if fsize == 0:
                    print(f"File {filename} is empty, skipping, deleting")
                    os.remove(os.path.join(spool_dir, filename))
                    continue
                start_time = time.time()
                print(f"File size: {fsize}")
                data = json.loads(f.read())
                data = io_schema.validate(data)
                data = io_schema.upgrade(data, copy=False)
                db_client.load(data)
                ing_speed = fsize / (time.time() - start_time) / 1024
                print(f"Ingested {filename} in {ing_speed} KB/s")
                # delete the file
                os.remove(os.path.join(spool_dir, filename))
        except Exception as e:
            print(f"Error: {e}")
            print(f"File: {filename}")
            raise e

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spool-dir", type=str, required=True)
    args = parser.parse_args()
    get_db_credentials()
    db_client = get_db_client(DATABASE)
    while True:
        ingest_submissions(args.spool_dir, db_client)
        time.sleep(1)

if __name__ == "__main__":
    main()
