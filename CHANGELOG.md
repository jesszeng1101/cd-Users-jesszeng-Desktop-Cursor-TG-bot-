# Changelog

All notable changes to this project are documented in this file.

The format follows [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

## [0.4.0] - 2026-03-26

### Added
- AI chat + skills layer in `bot.py` using `gpt-4o-mini`.
- Daily AI call limiter and per-user short memory window.
- Background run scripts: `run.sh`, `stop.sh`, `logs.sh`.
- Persistent files: `memory.json`, `portfolio.json`.
- Portfolio and setup command flows (`/portfolio`, `/addposition`, `/addsetup`, etc.).
- Startup online confirmation task and periodic portfolio alert checks.

### Changed
- Switched news ingestion to RSS and removed CryptoPanic dependency.
- Reworked status/analyse/review behavior for live market reads and safer fallback handling.
- Removed `XAUTUSDT` tracking from Binance paths; gold handled through market data sources.
- Lowered default strategy thresholds to be less strict in quieter markets.

### Fixed
- Retry behavior and non-crash guarantees for external API failures.
- `/analyse` formatting issues and RSI label boundaries.
- Improved startup and process-management ergonomics for background execution.

## [0.5.0] - 2026-03-26

### Added
- Initial structural foundation under `src/`:
  - `src/config/constants.py`
  - `src/storage/json_store.py`
  - `src/README.md`
- Lint/format project configuration in `pyproject.toml`.

### Changed
- Centralized core magic-number constants into package-level configuration for better visibility.
- Kept root entrypoints and runtime behavior backward compatible.
