#!/usr/bin/env python3
"""
Search kcidb tests for unprocessed tests and process them over logspec, update entries
"""

import os
import sys
import json

# postgres
import psycopg2
from psycopg2 import sql
from psycopg2.extras import DictCursor
import datetime
import shelve
import gzip

# md5
import hashlib
import requests
from logspec_api import generate_issues_and_incidents
import kcidb
import argparse
import time


def set_test_processed(cursor, test_id):
    """
    Mark the test as processed in the shelve database
    """
    # shelve database
    with shelve.open("/cache/processed_tests.db") as db:
        if test_id not in db:
            db[test_id] = True
            print(f"Test {test_id} marked as processed")
        else:
            print(f"Test {test_id} already processed")


def is_test_processed(test_id):
    """
    Check if the test is already processed
    """
    with shelve.open("/cache/processed_tests.db") as db:
        if test_id in db:
            return True
        else:
            return False


def set_build_processed(cursor, build_id):
    """
    Mark the build as processed in the shelve database
    """
    # shelve database
    with shelve.open("/cache/processed_builds.db") as db:
        if build_id not in db:
            db[build_id] = True
            print(f"Build {build_id} marked as processed")
        else:
            print(f"Build {build_id} already processed")


def is_build_processed(build_id):
    """
    Check if the build is already processed
    """
    with shelve.open("/cache/processed_builds.db") as db:
        if build_id in db:
            return True
        else:
            return False


def get_db_connection():
    """
    Connect to the PostgreSQL database
    """
    # pg_dsn = "postgresql://kcidb:kcidb@localhost:5432/kcidb"
    # check .pg_dsn file
    pg_dsn = None
    if os.path.exists(".pg_dsn"):
        with open(".pg_dsn", "r") as f:
            pg_dsn = f.read().strip()

    # Check if the environment variable is set
    if pg_dsn is None:
        pg_dsn = os.environ.get("PG_DSN")
        if pg_dsn is None:
            print(
                "No database connection string found. Please set the PG_DSN environment variable or create a .pg_dsn file."
            )
            sys.exit(1)

    # if pg_dsn contains prefix postgresql: (kcidb specific) - remove it
    if pg_dsn.startswith("postgresql:"):
        pg_dsn = pg_dsn[len("postgresql:") :]

    try:
        conn = psycopg2.connect(dsn=pg_dsn, cursor_factory=DictCursor)
        print("Connected to database")
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)


def get_unprocessed_tests(cursor):
    """
    Get unprocessed tests from the database
    """
    # last 24h
    last_24h = datetime.datetime.now() - datetime.timedelta(days=1)
    #
    try:
        cursor.execute(
            "SELECT * FROM tests WHERE _timestamp > %s AND log_url IS NOT NULL"
            " AND status != 'PASS'",
            (last_24h,),
        )
        tests = cursor.fetchall()
        return tests
    except Exception as e:
        print(f"Error fetching unprocessed tests: {e}")
        return []


def get_unprocessed_builds(cursor):
    """
    Get unprocessed builds from the database
    """
    # last 24h
    last_24h = datetime.datetime.now() - datetime.timedelta(days=1)
    #
    try:
        cursor.execute(
            "SELECT * FROM builds WHERE _timestamp > %s AND log_url IS NOT NULL"
            " AND status != 'PASS'",
            (last_24h,),
        )
        builds = cursor.fetchall()
        return builds
    except Exception as e:
        print(f"Error fetching unprocessed builds: {e}")
        return []


def fetch_log_id(log_url):
    # generate ID as hash of URL
    log_id = hashlib.md5(log_url.encode()).hexdigest()
    # check if log_id exists in cache
    cache_file = os.path.join("/cache", log_id)
    if os.path.exists(cache_file):
        return log_id
    # fetch log from URL
    try:
        response = requests.get(log_url)
        if response.status_code == 200:
            # save log to cache
            with open(cache_file, "wb") as f:
                f.write(response.content)
            print(f"Log {log_id} fetched and saved to cache")
            # decompress log if gzipped
            if log_url.endswith(".gz"):
                # rename to .gz
                os.rename(cache_file, cache_file + ".gz")
                with gzip.open(cache_file + ".gz", "rb") as f_in:
                    with open(cache_file, "wb") as f_out:
                        f_out.write(f_in.read())
                os.remove(cache_file + ".gz")
            return log_id
        else:
            print(f"Error fetching log {log_url}: {response.status_code}")
            return None
    except Exception as e:
        print(f"Error fetching log {log_url}: {e}")
        return None


# TODO: unify this with logspec_process_build
def logspec_process_test(test):
    """
    Process the test over logspec
    """
    log_url = test["log_url"]
    log_id = fetch_log_id(log_url)
    print(f"Log ID: {log_id}")
    # check if log_id is None
    if log_id is None:
        print(f"Error fetching log {log_url}")
        return
    log_file = os.path.join("/cache", log_id)
    parsed_node_id = test["id"]
    test_type = "boot"
    return generate_issues_and_incidents(test["id"], log_file, test_type)


def logspec_process_build(build):
    """
    Process the build over logspec
    """
    log_url = build["log_url"]
    log_id = fetch_log_id(log_url)
    print(f"Log ID: {log_id}")
    # check if log_id is None
    if log_id is None:
        print(f"Error fetching log {log_url}")
        return
    log_file = os.path.join("/cache", log_id)
    parsed_node_id = build["id"]
    test_type = "build"
    return generate_issues_and_incidents(build["id"], log_file, test_type)


def remove_none_fields(data):
    """Remove all keys with `None` values as KCIDB doesn't allow it"""
    if isinstance(data, dict):
        return {
            key: remove_none_fields(val) for key, val in data.items() if val is not None
        }
    if isinstance(data, list):
        return [remove_none_fields(item) for item in data]
    return data


def submit_to_kcidb(issues, incidents, spool_dir):
    """
    Submit issues and incidents to kcidb
    """
    revision = {
        "checkouts": [],
        "builds": [],
        "tests": [],
        "issues": issues,
        "incidents": incidents,
        "version": {"major": 4, "minor": 5},
    }
    # remove None fields
    revision = remove_none_fields(revision)
    # generate raw json
    raw_json = json.dumps(revision)
    # random part of filename
    random_part = os.urandom(8).hex()
    # generate filename
    filename = f"logspec_{random_part}.json"
    # drop it as json in spool_dir
    with open(os.path.join(spool_dir, filename), "w") as f:
        f.write(raw_json)


def process_tests(cursor, spool_dir):
    # code to get unprocessed tests
    unprocessed_tests = get_unprocessed_tests(cursor)
    if not unprocessed_tests:
        print("No unprocessed tests found")
        return
    # print the unprocessed tests
    for test in unprocessed_tests:
        # print formatted column names and values
        for column, value in test.items():
            print(f"{column}: {value}")
        # Check if the test is already processed
        if is_test_processed(test["id"]):
            print(f"Test {test['id']} already processed")
            continue
        # Process the test
        print(f"Processing test {test['id']}")
        # Call logspec
        res_nodes, new_status = logspec_process_test(test)
        if res_nodes["issue_node"] or res_nodes["incident_node"]:
            # submit to kcidb incident and issue
            print(f"Submitting to kcidb")
            submit_to_kcidb(
                res_nodes["issue_node"], res_nodes["incident_node"], spool_dir
            )
        else:
            print(f"No issues or incidents found in test {test['id']}")
        # mark the test as processed (TODO: must be in database)
        set_test_processed(cursor, test["id"])


def process_builds(cursor, spool_dir):
    """
    Process the builds
    """
    # get unprocessed builds
    unprocessed_builds = get_unprocessed_builds(cursor)
    if not unprocessed_builds:
        print("No unprocessed builds found")
        return
    for build in unprocessed_builds:
        # print formatted column names and values
        for column, value in build.items():
            print(f"{column}: {value}")
        # Check if the build is already processed
        if is_build_processed(build["id"]):
            print(f"Build {build['id']} already processed")
            continue
        # Process the build
        print(f"Processing build {build['id']}")
        # Call logspec
        res_nodes, new_status = logspec_process_build(build)
        if res_nodes["issue_node"] or res_nodes["incident_node"]:
            # submit to kcidb incident and issue
            print(f"Submitting to kcidb")
            submit_to_kcidb(
                res_nodes["issue_node"], res_nodes["incident_node"], spool_dir
            )
        else:
            print(f"No issues or incidents found in build {build['id']}")
        # mark the build as processed (TODO: must be in database)
        set_build_processed(cursor, build["id"])


def main():
    """
    Main function to process the logspec
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--spool-dir", type=str, required=True)
    args = parser.parse_args()
    spool_dir = args.spool_dir
    # check if spool_dir exists
    if not os.path.exists(spool_dir):
        print(f"Spool directory {spool_dir} does not exist")
        sys.exit(1)
    # verify if cache directory exists
    if not os.path.exists("/cache"):
        os.makedirs("/cache")

    if not os.path.exists("/cache/processed_tests.db"):
        with shelve.open("/cache/processed_tests.db") as db:
            db["processed_tests"] = {}
    if not os.path.exists("/cache/processed_builds.db"):
        with shelve.open("/cache/processed_builds.db") as db:
            db["processed_builds"] = {}

    # Connect to the database
    conn = get_db_connection()
    cursor = conn.cursor()

    while True:
        process_builds(cursor, spool_dir)
        process_tests(cursor, spool_dir)

    conn.close()
    cursor.close()


if __name__ == "__main__":
    while True:
        main()
        # sleep for 10 seconds
        time.sleep(10)
