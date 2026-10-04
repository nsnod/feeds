# Pinned test config

The test suite and the recorded-day replay load **this** folder, not the repository's
`config/` folder. `config/` is yours to edit (feeds, tuning, blocklist); pinning the tests
to a fixed copy means a valid edit there can never turn CI red. CI validates your `config/`
on its own with `python -m gembot check-config`.

These files started as copies of the shipped defaults (feeds empty). Change them only
when a test should run against different settings.
