import os
from typing import Any

import psycopg2
from fastapi import HTTPException
from psycopg2 import Error
from psycopg2.extensions import connection, cursor

from helpers.hasura import untrack_table, track_table
from helpers.timer import Timer
from models import Metadata, BatchRequest, CreateTableResult

conn: connection = None
cur: cursor = None

try:
    conn = psycopg2.connect(user=os.environ.get('POSTGRES_USER'),
                            password=os.environ.get('POSTGRES_PASSWORD'),
                            host=os.environ.get('POSTGRES_HOST'),
                            port=os.environ.get('POSTGRES_PORT'),
                            database=os.environ.get('POSTGRES_DB'))
    cur = conn.cursor()
except (Exception, Error) as error:
    print("Error while connecting to PostgreSQL", error)
    if conn:
        conn.close()
        if cur:
            cur.close()
        print("PostgreSQL connection is closed")
    exit(1)


def execute_up_down(metadata: Metadata):
    # Create table with sql_up
    try:
        cur.execute(metadata.sql_up)
    except Error as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error while executing sql_up of '{metadata.table_name}':\n{e}"
        )

    # Test sql_down
    try:
        cur.execute(metadata.sql_down)
    except Error as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error while testing sql_down of '{metadata.table_name}':\n{e}"
        )

    try:
        cur.execute(metadata.sql_up)
    except Error as e:
        raise HTTPException(
            status_code=400,
            detail=f"sql_down of '{metadata.table_name}' does not fully undo sql_up:\n{e}"
        )


def create_table(metadata: Metadata) -> CreateTableResult:
    """
    Create table as specified in metadata.

    If table already exists, and sql_up is up-to-date, do nothing. If
    sql_up has been changed, run the stored sql_drop, and create table
    as specified in new sql_up.

    Returns whether the table was created or not.
    """

    # Initialise Tables table if not already
    cur.execute(open("app/init.sql", "r").read())

    cmd = r"SELECT up, down FROM Tables WHERE table_name = %s"
    metadata.table_name = metadata.table_name.lower()
    cur.execute(cmd, (metadata.table_name,))
    table_sql = cur.fetchone()
    if not table_sql:
        # Execute create table
        execute_up_down(metadata)

        # Store metadata
        cmd = r"INSERT INTO Tables(table_name, up, down) VALUES (%s, %s, %s)"
        cur.execute(cmd, (metadata.table_name, metadata.sql_up, metadata.sql_down))

        return CreateTableResult.CREATED
    elif table_sql[0] != metadata.sql_up:
        untrack_table(metadata.table_name)

        # Re-create
        cur.execute(table_sql[1])  # old sql_down
        execute_up_down(metadata)

        # Store new metadata
        cmd = r"UPDATE Tables SET up = %s, down = %s WHERE table_name = %s"
        cur.execute(cmd, (metadata.sql_up, metadata.sql_down, metadata.table_name))

        return CreateTableResult.UPDATED

    return CreateTableResult.NONE


def get_primary_key_columns(table_name: str) -> list[str]:
    cmd = f"""
        SELECT c.column_name
        FROM information_schema.columns c
            JOIN information_schema.key_column_usage kcu
                ON c.table_name = kcu.table_name
                AND c.column_name = kcu.column_name
            JOIN information_schema.table_constraints tc
                ON kcu.table_name = tc.table_name
                AND kcu.constraint_name = tc.constraint_name
        WHERE c.table_name = '{table_name}'
            AND tc.constraint_type = 'PRIMARY KEY';
    """
    cur.execute(cmd)

    return [row[0] for row in cur.fetchall()]


def execute_upsert(metadata: Metadata, payload: list[Any]):
    columns = [f'"{col}"' for col in metadata.columns]
    key_columns = [f'"{col}"' for col in get_primary_key_columns(metadata.table_name)]
    non_key_columns = [col for col in columns if col not in key_columns]

    cmd = f"""
        INSERT INTO {metadata.table_name}({", ".join(columns)})
        VALUES ({", ".join(["%s"] * len(columns))})
        ON CONFLICT ({", ".join(key_columns)})
        DO UPDATE SET {", ".join(f"{col} = EXCLUDED.{col}" for col in non_key_columns)};
    """
    values = [tuple(row[col] for col in metadata.columns) for row in payload]

    cur.executemany(cmd, values)


def execute_delete(metadata: Metadata, payload: list[Any]):
    key_columns = get_primary_key_columns(metadata.table_name)
    quoted_key_columns = [f'"{col}"' for col in key_columns]

    cmd = f"""
        DELETE FROM {metadata.table_name}
        WHERE ({", ".join(quoted_key_columns)}) NOT IN %s;
    """
    values = tuple(tuple(row[col] for col in key_columns) for row in payload)

    cur.execute(cmd, (values,))


def do_insert(metadata: Metadata, payload: list[Any]):
    t = Timer(f"sql_before of {metadata.table_name}").start()
    if metadata.sql_before:
        try:
            cur.execute(metadata.sql_before)
        except Error as e:
            raise HTTPException(
                status_code=400,
                detail=f"Error while executing sql_before of '{metadata.table_name}':\n{e}"
            )
    t.stop()

    t = Timer(f"insert of {metadata.table_name}").start()
    try:
        execute_upsert(metadata, payload)
        if metadata.write_mode == 'overwrite':
            # Delete rows not in payload
            execute_delete(metadata, payload)
    except Error as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error while inserting tuples into '{metadata.table_name}':\n{e}"
        )
    t.stop()

    t = Timer(f"sql_after of {metadata.table_name}").start()
    if metadata.sql_after:
        try:
            cur.execute(metadata.sql_after)
        except Error as e:
            raise HTTPException(
                status_code=400,
                detail=f"Error while executing sql_after of '{metadata.table_name}':\n{e}"
            )
    t.stop()


def do_batch_insert(requests: list[BatchRequest]):
    create_table_results = {}
    for request in requests:
        try:
            create_table_result = create_table(request.metadata)
            create_table_results[request.metadata.table_name.lower()] = create_table_result

            do_insert(request.metadata, request.payload)
        except HTTPException as e:
            print(e.detail)
            conn.rollback()
            raise e

    conn.commit()

    for table_name, create_table_result in create_table_results.items():
        # Run Hasura actions - must be done after transaction committed otherwise Hasura won't see the table
        if create_table_result == CreateTableResult.UPDATED:
            untrack_table(table_name)

        if create_table_result == CreateTableResult.UPDATED or create_table_result == CreateTableResult.CREATED:
            track_table(table_name)
