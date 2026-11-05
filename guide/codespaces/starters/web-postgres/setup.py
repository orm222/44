"""Create a local development secret once. Never change an existing DB password."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent
directory = root / '.secrets'
directory.mkdir(mode=0o700, exist_ok=True)
target = directory / 'db_password.txt'
try:
    with target.open('x', encoding='utf-8') as stream:
        stream.write(secrets.token_urlsafe(32) + '\n')
    # The private parent directory protects the host file; Compose mounts the
    # file for both the non-root API user and PostgreSQL's different UID.
    target.chmod(0o444)
    print('Created local DB secret. Value is not printed.')
except FileExistsError:
    if not target.is_file() or not target.read_text(encoding='utf-8').strip():
        raise SystemExit('Existing secret must be a nonempty regular file.')
    print('Existing local DB secret preserved.')
