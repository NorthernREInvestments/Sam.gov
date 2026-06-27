"""Step 2: test PostgreSQL and create GovTracker tables."""

from database import init_db, test_connection


def main() -> None:
    print("Testing PostgreSQL connection...")
    test_connection()
    print("Connected.")

    print("Creating tables (fresh GovTracker schema)...")
    init_db()
    print("Done. Table 'contracts' is ready.")


if __name__ == "__main__":
    main()
