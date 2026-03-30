# Cursor Notes

## Working Guidelines

- Keep `bot.py` as the executable entrypoint used by run scripts.
- Prefer adding new shared constants in `src/config/constants.py`.
- Prefer adding persistent storage helpers in `src/storage/`.
- Keep runtime behavior backward compatible unless explicitly approved.

## Change History

Canonical change history is maintained in `CHANGELOG.md` using semantic versioning (`a.b.c`).
