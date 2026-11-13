This project uses Flask-Migrate/Alembic. App startup does not mutate the schema.

Clean database:

```powershell
python -m flask --app run.py db upgrade
```

Existing database created before Alembic tracking:

1. Create and verify a protected SQLite backup.
2. Confirm the database contains the expected application tables and records.
3. Stamp the frozen baseline: `python -m flask --app run.py db stamp bc3c6351086c`.
4. Apply later additive changes: `python -m flask --app run.py db upgrade`.

Do not stamp an empty, corrupt, or unverified database. The baseline represents the model schema before security-only metadata changes. Later revisions must be reviewed for row-preserving SQLite operations before they are applied to an existing database. Do not use `db downgrade` as a restore mechanism; recover from a verified backup instead.
