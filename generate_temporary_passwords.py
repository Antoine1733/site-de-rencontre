"""Generate passwords only for legacy accounts; never print credentials."""
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.security import generate_password_hash
from app import get_db, init_db


def generate(output: Path) -> int:
    # Exclusive creation prevents overwriting a previous distribution list.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        with get_db() as db:
            rows = db.execute("SELECT id,email FROM public.profiles WHERE password_hash IS NULL ORDER BY email FOR UPDATE").fetchall()
            credentials = []
            for row in rows:
                password = secrets.token_urlsafe(18)
                db.execute("UPDATE public.profiles SET password_hash=%s, password_change_required=true, auth_version=auth_version+1 WHERE id=%s",
                           (generate_password_hash(password, method='scrypt'), row['id']))
                credentials.append({'email': row['email'], 'mot_de_passe_temporaire': password})
            json.dump(credentials, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        return len(credentials)


if __name__ == '__main__':
    init_db()
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    output = Path(__file__).resolve().parent / 'private_credentials' / f'mots-de-passe-{timestamp}.json'
    count = generate(output)
    print(f'{count} mots de passe temporaires générés. Fichier : {output}')
