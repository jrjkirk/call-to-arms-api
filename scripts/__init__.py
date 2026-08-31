"""Scheduled job entry points.

Deliberately a real package rather than an implicit namespace one: the live app
imports these now (see scheduler.py), and namespace resolution depends on the
working directory happening to be the repo root. It is in the container today
(`WORKDIR /app`), but that is not something the scheduler should be betting on.

Nothing is imported here — scheduler.py imports the individual modules lazily,
so a machine with the scheduler switched off never loads them at all.
"""
