"""Run every tests/test_*.py and report which failed.

Each test is a standalone script (no pytest: it isn't in requirements.txt)
that makes its own throwaway SQLite database, so they run side by side, one
process each. The same command runs locally and in the GitHub workflow
(.github/workflows/tests.yml):

    python tests/run_all.py            # all of them
    python tests/run_all.py notices    # only files whose name contains "notices"

Exit status is non-zero if any test failed, and a failure's own output is
printed in full, since that output is the only diagnosis there is.
"""
import os
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 300


def run(path: pathlib.Path) -> tuple[pathlib.Path, int, str, float]:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    # A test must never reach a real database or a real webhook, whatever the
    # caller's environment holds. Each test sets its own DATABASE_URL first.
    for key in ("ALERTS_WEBHOOK_URL", "DISCORD_UPDATES_WEBHOOK_URL", "SCHEDULER_ENABLED"):
        env.pop(key, None)
    # And a test that forgets to set one gets an empty in-memory database, not
    # the live one: database.py's load_dotenv never overrides a value already
    # in the environment, so this beats whatever .env says.
    env["DATABASE_URL"] = "sqlite://"
    env.setdefault("SESSION_SECRET", "localtestsecret")
    started = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, str(path)], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
        code, output = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        code, output = 1, f"timed out after {TIMEOUT_SECONDS}s"
    return path, code, output, time.monotonic() - started


def main() -> int:
    wanted = sys.argv[1:]
    tests = sorted(
        p for p in (ROOT / "tests").glob("test_*.py")
        if not wanted or any(w in p.name for w in wanted)
    )
    if not tests:
        print("No tests matched.")
        return 1
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        results = list(pool.map(run, tests))

    failed = [r for r in results if r[1] != 0]
    for path, _code, output, _secs in failed:
        print(f"\n===== FAILED: {path.name} =====\n{output.rstrip()}\n")
        if os.environ.get("GITHUB_ACTIONS"):
            # An annotation shows on the run's summary page, which anyone can
            # read without opening (or being allowed to open) the full log.
            tail = "\n".join(output.rstrip().splitlines()[-40:])
            body = tail.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
            print(f"::error title={path.name} failed::{body}")
    for path, code, _output, secs in results:
        print(f"{'PASS' if code == 0 else 'FAIL'}  {path.name}  ({secs:.1f}s)")
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
