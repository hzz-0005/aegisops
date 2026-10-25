"""Apply ordered, transactional SQL migrations to the lab business database."""

import os
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).with_name("migrations")


def main() -> None:
    with psycopg.connect(os.environ["LAB_DATABASE_URL"]) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public.lab_schema_migrations (
                name text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        connection.commit()
        for path in sorted(MIGRATIONS.glob("*.sql")):
            with connection.transaction():
                applied = connection.execute(
                    "SELECT 1 FROM public.lab_schema_migrations WHERE name = %s", (path.name,)
                ).fetchone()
                if applied:
                    continue
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO public.lab_schema_migrations (name) VALUES (%s)",
                    (path.name,),
                )
                print(f"applied lab migration {path.name}", flush=True)


if __name__ == "__main__":
    main()
