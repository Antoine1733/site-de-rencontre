from __future__ import annotations

import base64
import binascii
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import psycopg2
import psycopg2.extras
import requests
from pywebpush import WebPushException, webpush
from flask import Flask, g, has_app_context, jsonify, render_template_string, request, send_from_directory, session

BASE_DIR = Path(__file__).resolve().parent


def load_env() -> None:
    path = BASE_DIR / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()
DATABASE_URL = os.getenv("DATABASE_URL", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SUPABASE_SECRET_KEY", ""))
SUPABASE_STORAGE_BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "")
SIGNED_PHOTO_URL_TTL = 50 * 60
SIGNED_PHOTO_URL_CACHE: dict[str, tuple[float, str]] = {}
SIGNED_PHOTO_URL_CACHE_LOCK = threading.Lock()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-only-change-this")
app.config.update(MAX_CONTENT_LENGTH=10 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30), SESSION_REFRESH_EACH_REQUEST=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=(os.getenv("APP_ENV") == "production" or os.getenv("VERCEL") == "1"))


@app.before_request
def persist_existing_login():
    if session.get("email") and not session.permanent:
        session.permanent = True


class PgConnection:
    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL manque dans le fichier .env")
        self.request_scoped = has_app_context()
        self.connection = psycopg2.connect(DATABASE_URL)
        self.cursor = self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, query: str, params: tuple[Any, ...] = ()):
        self.cursor.execute(query, params)
        return self.cursor

    def close(self):
        self.cursor.close()
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        try:
            self.connection.rollback() if kind else self.connection.commit()
        finally:
            if not self.request_scoped:
                self.close()


def get_db() -> PgConnection:
    if has_app_context():
        if "database" not in g:
            g.database = PgConnection()
        return g.database
    return PgConnection()


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("database", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Create the application tables directly in the Supabase PostgreSQL database."""
    if not DATABASE_URL:
        app.logger.warning("DATABASE_URL absent; database initialization skipped")
        return
    with get_db() as db:
        db.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        db.execute("""
            CREATE TABLE IF NOT EXISTS public.profiles (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email TEXT NOT NULL UNIQUE,
                first_name TEXT NOT NULL,
                age SMALLINT NOT NULL CHECK (age BETWEEN 18 AND 100),
                bio TEXT NOT NULL,
                photo_paths TEXT[] NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        db.execute("ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS email TEXT")
        db.execute("ALTER TABLE public.profiles ALTER COLUMN id SET DEFAULT gen_random_uuid()")
        db.execute("ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_id_fkey")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS profiles_email_idx ON public.profiles(email)")
        db.execute("CREATE INDEX IF NOT EXISTS profiles_created_idx ON public.profiles(created_at DESC, id DESC)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS public.swipes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                actor_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
                target_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
                liked BOOLEAN NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (actor_id, target_id), CHECK (actor_id <> target_id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS swipes_likes_idx ON public.swipes(target_id, created_at DESC) WHERE liked=true")
        db.execute("""
            CREATE TABLE IF NOT EXISTS public.matches (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_a UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
                user_b UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (user_a, user_b), CHECK (user_a < user_b)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS public.messages (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                match_id UUID NOT NULL REFERENCES public.matches(id) ON DELETE CASCADE,
                sender_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
                body TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS messages_match_idx ON public.messages(match_id, created_at)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS public.push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS push_subscriptions_user_idx ON public.push_subscriptions(user_id)")


def send_push(user_id, title, body, match_id=None, url=None) -> None:
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT):
        app.logger.warning("Push notification not sent: VAPID environment variables are not configured")
        return
    try:
        with get_db() as db:
            subscriptions = db.execute(
                "SELECT endpoint,p256dh,auth FROM public.push_subscriptions WHERE user_id=%s",
                (user_id,),
            ).fetchall()
    except (RuntimeError, psycopg2.Error):
        app.logger.exception("Could not load push subscriptions")
        return

    payload = json.dumps({
        "title": title,
        "body": body,
        "url": url or (f"/?match={match_id}" if match_id else "/"),
    }, ensure_ascii=False)
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription["endpoint"],
                    "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                timeout=3,
                ttl=60,
            )
        except WebPushException as error:
            response = getattr(error, "response", None)
            if response is not None and response.status_code in (404, 410):
                try:
                    with get_db() as db:
                        db.execute("DELETE FROM public.push_subscriptions WHERE endpoint=%s", (subscription["endpoint"],))
                except (RuntimeError, psycopg2.Error):
                    app.logger.exception("Could not remove expired push subscription")
            else:
                app.logger.warning("Push delivery failed: %s", error)
        except (requests.RequestException, ValueError):
            app.logger.exception("Push delivery failed")


def as_json_many(rows):
    results = [dict(row) for row in rows]
    for result in results:
        if "photo_paths" in result:
            result["photo_paths"] = list(result["photo_paths"])
    photo_references = [
        (row_index, photo_index, path)
        for row_index, result in enumerate(results)
        for photo_index, path in enumerate(result.get("photo_paths", []))
    ]
    if photo_references:
        signed_urls = signed_photo_urls([path for _, _, path in photo_references])
        for (row_index, photo_index, _), signed_url in zip(photo_references, signed_urls):
            results[row_index]["photo_paths"][photo_index] = signed_url

    for result in results:
        for key, value in result.items():
            if hasattr(value, "isoformat"):
                result[key] = value.isoformat()
            elif hasattr(value, "hex") and not isinstance(value, str):
                result[key] = str(value)
    return results


def as_json(row):
    return as_json_many([row])[0]


def storage_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and SUPABASE_STORAGE_BUCKET)


def storage_headers(content_type: str = "") -> dict[str, str]:
    headers = {"apikey": SUPABASE_SERVICE_ROLE_KEY}
    if not SUPABASE_SERVICE_ROLE_KEY.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def storage_object_url(object_path: str, action: str) -> str:
    if not storage_configured():
        raise RuntimeError("Configure SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY et SUPABASE_STORAGE_BUCKET.")
    bucket = quote(SUPABASE_STORAGE_BUCKET, safe="")
    path = quote(object_path, safe="/")
    action_path = f"/{action}" if action else ""
    return f"{SUPABASE_URL}/storage/v1/object{action_path}/{bucket}/{path}"


def upload_photo(data_url: str, profile_id: str) -> str:
    prefix = "data:image/jpeg;base64,"
    if not isinstance(data_url, str) or not data_url.startswith(prefix):
        raise ValueError("Format de photo invalide.")
    try:
        image = base64.b64decode(data_url[len(prefix):], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("Photo JPEG invalide.") from error
    if not image or len(image) > 8 * 1024 * 1024:
        raise ValueError("Photo vide ou supérieure à 8 Mo.")
    object_path = f"profiles/{profile_id}/{uuid4().hex}.jpg"
    try:
        response = requests.post(
            storage_object_url(object_path, ""),
            headers={**storage_headers("image/jpeg"), "x-upsert": "true"},
            data=image,
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        response = error.response
        if response is not None:
            app.logger.error("Supabase photo upload failed (HTTP %s): %s", response.status_code, response.text[:500])
        else:
            app.logger.exception("Supabase photo upload failed")
        raise RuntimeError("Impossible d’envoyer la photo vers Supabase Storage.") from error
    return object_path


def signed_photo_url(object_path: str) -> str:
    if object_path.startswith("data:image/"):
        raise RuntimeError("Les anciennes photos doivent être migrées vers Supabase Storage.")
    now = time.monotonic()
    with SIGNED_PHOTO_URL_CACHE_LOCK:
        cached = SIGNED_PHOTO_URL_CACHE.get(object_path)
        if cached and cached[0] > now:
            return cached[1]
    try:
        response = requests.post(
            storage_object_url(object_path, "sign"),
            headers=storage_headers("application/json"),
            json={"expiresIn": 3600},
            timeout=10,
        )
        response.raise_for_status()
        signed_url = response.json().get("signedURL")
    except (requests.RequestException, ValueError) as error:
        app.logger.exception("Supabase photo URL signing failed")
        raise RuntimeError("Impossible de générer un lien temporaire pour une photo.") from error
    if not isinstance(signed_url, str) or not signed_url:
        raise RuntimeError("Supabase n’a pas renvoyé d’URL signée pour une photo.")
    if signed_url.startswith("https://"):
        result = signed_url
    else:
        result = f"{SUPABASE_URL}/storage/v1/{signed_url.lstrip('/')}"
    with SIGNED_PHOTO_URL_CACHE_LOCK:
        now = time.monotonic()
        expired = [path for path, (expiry, _) in SIGNED_PHOTO_URL_CACHE.items() if expiry <= now]
        for path in expired:
            del SIGNED_PHOTO_URL_CACHE[path]
        while len(SIGNED_PHOTO_URL_CACHE) >= 4096:
            SIGNED_PHOTO_URL_CACHE.pop(next(iter(SIGNED_PHOTO_URL_CACHE)))
        SIGNED_PHOTO_URL_CACHE[object_path] = (now + SIGNED_PHOTO_URL_TTL, result)
    return result


def signed_photo_urls(object_paths: list[str]) -> list[str]:
    if not object_paths:
        return []
    with ThreadPoolExecutor(max_workers=min(12, len(object_paths))) as executor:
        return list(executor.map(signed_photo_url, object_paths))


def delete_photo(object_path: str) -> None:
    response = requests.delete(
        storage_object_url(object_path, ""),
        headers=storage_headers(),
        timeout=10,
    )
    response.raise_for_status()


def migrate_existing_photos() -> int:
    if not storage_configured():
        raise RuntimeError("Configure SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY et SUPABASE_STORAGE_BUCKET.")
    with get_db() as db:
        profiles = db.execute("SELECT id,photo_paths FROM public.profiles").fetchall()
    migrated = 0
    for profile in profiles:
        old_paths = profile["photo_paths"]
        if not any(path.startswith("data:image/") for path in old_paths):
            continue
        new_paths = [
            upload_photo(path, str(profile["id"])) if path.startswith("data:image/") else path
            for path in old_paths
        ]
        with get_db() as db:
            db.execute("UPDATE public.profiles SET photo_paths=%s WHERE id=%s", (new_paths, profile["id"]))
        migrated += 1
    return migrated


def current_user():
    email = session.get("email")
    if not email:
        return None
    with get_db() as db:
        return db.execute("SELECT id,email,first_name,age,bio,photo_paths FROM public.profiles WHERE email=%s", (email,)).fetchone()


def require_user():
    user = current_user()
    if not user:
        raise PermissionError("Connecte-toi pour continuer.")
    return user


def fail(error, status=400):
    return jsonify({"error": str(error)}), status


HTML = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><meta name="theme-color" content="#a94e49"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/static/icon.svg" type="image/svg+xml"><link rel="stylesheet" href="/static/chat.css?v=20261031-like-profiles"><title>Rencontres</title>
<style>
:root{font-family:Arial,sans-serif;color:#25251f;background:#f6f5f0}*{box-sizing:border-box}body{margin:0;min-width:320px}button,input,textarea{font:inherit}button{cursor:pointer;color:inherit}.hide{display:none!important}.shell{max-width:900px;min-height:100vh;margin:auto;padding:0 22px}.top{height:60px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #deded5}.top strong{font-size:15px}.top span{color:#77786e;font-size:12px}.content{max-width:650px;margin:auto;padding:26px 0 90px}.heading{display:flex;align-items:end;justify-content:space-between;margin-bottom:18px}.heading h1,.heading h2{margin:0;font-size:25px;font-weight:600}.heading p{margin:6px 0 0;color:#77786e;font-size:13px}.form{max-width:420px;margin:7vh auto}.form h1{margin:0 0 8px;font-size:27px}.form>p{color:#77786e;font-size:13px}.field{display:grid;gap:6px;margin:13px 0}.field label{font-size:12px;font-weight:600}.field small{color:#77786e;font-weight:400}.field input,.field textarea,.send input{width:100%;padding:11px;border:1px solid #d7d7ce;border-radius:3px;background:white}.field input[type=file]{padding:9px}.field textarea{resize:vertical}.primary,.send button{min-height:44px;padding:0 16px;border:0;border-radius:3px;background:#d65e49;color:white;font-weight:600}.primary{width:100%;margin-top:8px}.preview{display:flex;gap:7px;flex-wrap:wrap}.preview img{width:56px;height:56px;object-fit:cover;border-radius:3px}.tabs{display:flex;gap:8px;margin:18px 0;border-bottom:1px solid #deded5}.tab{padding:10px 12px;border:0;border-bottom:2px solid transparent;background:none;color:#77786e}.tab.active{border-bottom-color:#d65e49;color:#25251f}.card{position:relative;display:flex;align-items:end;height:min(60vh,570px);min-height:350px;overflow:hidden;border-radius:4px;background:#e8e8e2}.card>img{position:absolute;width:100%;height:100%;object-fit:cover}.card-info{position:relative;width:100%;padding:22px;color:white;background:linear-gradient(transparent,#000b)}.card-info h2{margin:0;font-size:25px}.card-info p{margin:8px 0 0;font-size:14px;line-height:1.45}.photo-hit{position:absolute;inset:0;display:flex}.photo-hit button{width:50%;border:0;background:transparent}.actions{display:flex;justify-content:center;gap:18px;margin-top:16px}.actions button{width:54px;height:54px;border:1px solid #d5d5cb;border-radius:50%;background:white;font-size:21px}.actions .like{border-color:#d65e49;background:#d65e49;color:white}.hint{text-align:center;color:#88897e;font-size:11px}.matches{display:flex;gap:7px;overflow:auto;padding:4px 0 14px}.match{flex:none;padding:9px 13px;border:1px solid #deded5;border-radius:3px;background:white}.match.active{border-color:#d65e49}.chat{max-height:52vh;min-height:220px;overflow:auto;padding:12px 0}.bubble{width:fit-content;max-width:80%;margin:7px 0;padding:10px 13px;border-radius:5px;background:#e5e5dd;overflow-wrap:anywhere;font-size:14px}.bubble.mine{margin-left:auto;background:#dfe8ce}.bubble time{display:block;margin-top:5px;color:#6d6e65;font-size:10px}.send{display:flex;gap:8px}.send button{flex:none}.empty{text-align:center;padding:30px 12px;color:#77786e;font-size:13px}.notice{position:fixed;z-index:5;left:50%;bottom:22px;max-width:calc(100% - 30px);padding:11px 15px;border-radius:3px;background:#25251f;color:white;font-size:12px;transform:translateX(-50%)}
@media(max-width:600px){.shell{padding:0 14px}.content{padding-top:20px}.heading h1{font-size:23px}.card{height:57vh;min-height:320px}.tabs{position:fixed;z-index:3;inset:auto 0 0;justify-content:space-around;margin:0;padding:6px 8px max(6px,env(safe-area-inset-bottom));border-top:1px solid #deded5;background:#f6f5f0}.tab{flex:1;padding:11px 4px;font-size:12px}.form{margin:8vh auto}}
.matches{display:grid;grid-template-columns:1fr;gap:8px;margin-bottom:12px}.match{display:flex;align-items:center;gap:11px;width:100%;min-width:0;padding:10px;border:1px solid #e2e1d9;border-radius:4px;background:#fffefa;text-align:left;transition:border-color .15s,background .15s}.match:hover,.match.active{border-color:#d65e49;background:#fff}.match-photo{width:48px;height:48px;flex:none;object-fit:cover;border-radius:50%;background:#e8e8e2}.match-info{display:grid;gap:4px;min-width:0}.match-name,.match-last{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.match-name{font-size:13px;font-weight:600}.match-last{color:#77786e;font-size:11px}
@media(max-width:600px){.matches{gap:6px}.match{padding:8px}.match-photo{width:44px;height:44px}}
</style><style>
:root{font-family:Arial,sans-serif;color:#29211f;background:#f8f5f0;font-synthesis:none;text-rendering:optimizeLegibility;--ink:#29211f;--muted:#847a74;--line:#e9e1da;--paper:#fffdfa;--rose:#ad514b;--rose-dark:#883d3b;--green:#59765e}
body{min-width:320px;background:radial-gradient(ellipse at 9% 0%,#f1e5dd 0,transparent 34%),radial-gradient(ellipse at 93% 80%,#eee9df 0,transparent 30%),#f8f5f0}
button,input,textarea{font:inherit}button{transition:transform .18s ease,background .18s ease,border-color .18s ease,box-shadow .18s ease}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:3px solid #ad514b55;outline-offset:2px}button:disabled{cursor:wait;opacity:.72}
.shell{max-width:1120px;padding:0 34px}.top{height:78px;border:0;border-bottom:1px solid #e8dfd8;gap:14px}.top strong{display:flex;align-items:center;gap:11px;font-size:17px;font-weight:700;letter-spacing:-.4px}.top strong:before{content:"♡";display:grid;place-items:center;width:37px;height:37px;border-radius:13px;background:#a94e49;color:#fff;font-size:25px;line-height:1}.top span{margin-left:auto;color:var(--muted);font-size:13px}.top button:not(.hide){border:1px solid var(--line);border-radius:999px;background:var(--paper);padding:10px 15px;font-size:12px;font-weight:600}
.content{max-width:920px;padding:36px 0 70px}.heading{align-items:center;margin-bottom:23px}.heading h1,.heading h2,.form h1{font-family:Georgia,serif;letter-spacing:-.5px}.heading h1,.heading h2{font-size:32px}.heading p{color:var(--muted);font-size:14px}.heading>span{padding:8px 12px;border:1px solid var(--line);border-radius:999px;background:#fffdfa;color:var(--muted);font-size:12px}
.tabs{width:fit-content;gap:5px;margin:0 auto 30px;padding:5px;border:1px solid #e9e1da;border-radius:999px;background:#fffdfa;box-shadow:0 5px 20px #49302408}.tab{padding:10px 18px;border:0;border-radius:999px;background:transparent;color:var(--muted);font-size:13px;font-weight:600}.tab:hover{color:var(--ink)}.tab.active{border:0;background:#f2e7e2;color:var(--rose-dark)}
.card{width:min(100%,570px);height:min(64vh,640px);min-height:390px;margin:0 auto;border-radius:22px;background:#e8ddd3;box-shadow:0 25px 65px #38201820}.card>img{inset:0;object-position:center 42%;transition:transform .6s}.card:hover>img{transform:scale(1.015)}.card-info{padding:30px;background:linear-gradient(180deg,transparent 0%,#130c0bd9 100%)}.card-info h2{font-family:Georgia,serif;font-size:36px;letter-spacing:-.5px}.card-info p{max-width:450px;color:#ffffffde;font-size:15px;line-height:1.6}.actions{gap:16px;margin:20px auto 9px}.actions button{width:58px;height:58px;border:1px solid #e7ddd6;background:#fffdfa;color:#786a63;font-size:21px;box-shadow:0 7px 20px #39241a12}.actions button:active{transform:none}@media(hover:hover) and (pointer:fine){.actions button:hover{transform:translateY(-3px);box-shadow:0 11px 25px #39241a1c}}.actions .like{border-color:var(--rose);background:var(--rose);color:white}.actions .like:hover{background:var(--rose-dark)}.hint{color:#948780;font-size:12px}
#profile-count{font-weight:600}
.form{max-width:1000px;min-height:600px;margin:7vh auto;display:grid;grid-template-columns:.9fr 1.1fr;overflow:hidden;border:1px solid #e9e1da;border-radius:26px;background:var(--paper);box-shadow:0 28px 90px #3a231515}.auth-aside{position:relative;display:flex;flex-direction:column;justify-content:space-between;overflow:hidden;padding:38px;color:white;background:linear-gradient(155deg,#7d3b3b,#ad514b 58%,#c77d68)}.auth-aside:before,.auth-aside:after{position:absolute;content:"";pointer-events:none;border:1px solid #ffffff25;border-radius:50%}.auth-aside:before{width:440px;height:440px;right:-190px;top:-95px;box-shadow:0 0 0 35px #ffffff08,0 0 0 75px #ffffff06}.auth-aside:after{width:250px;height:250px;left:-125px;bottom:-75px;background:#ffffff08}.auth-brand{position:relative;z-index:1;display:flex;align-items:center;gap:10px;font-size:14px;font-weight:700;letter-spacing:.1em;text-transform:uppercase}.auth-brand span{display:grid;place-items:center;width:34px;height:34px;border:1px solid #ffffff75;border-radius:12px;font-size:22px}.auth-copy{position:relative;z-index:1;margin:0 0 30px}.auth-copy small{color:#ffffffad;font-size:11px;font-weight:700;letter-spacing:.16em;text-transform:uppercase}.auth-copy h2{max-width:370px;margin:14px 0;font-family:Georgia,serif;font-size:clamp(34px,4vw,48px);font-weight:500;line-height:1.12;letter-spacing:-1px}.auth-copy p{max-width:330px;margin:0;color:#ffede7d9;font-size:14px;line-height:1.7}.auth-main{align-self:center;padding:46px clamp(28px,5vw,62px)}.form h1{margin:0 0 7px;font-size:34px}.form>p{margin:0 0 23px;color:var(--muted);font-size:13px;line-height:1.6}.auth-main #signup-form,.auth-main #login-form{display:grid;gap:3px}.field{gap:8px;margin:9px 0}.field label{color:#51453f;font-size:12px;font-weight:600}.field small{color:#9a8d85}.field input,.field textarea,.send input{padding:12px 13px;border:1px solid #e5ddd6;border-radius:11px;background:#fff;color:var(--ink);font-size:13px;transition:border-color .15s,box-shadow .15s}.field input:focus,.field textarea:focus,.send input:focus{border-color:#bf7770;box-shadow:0 0 0 4px #ad514b12;outline:0}.field input[type=file]{padding:10px;font-size:11px}.field textarea{min-height:86px}.primary,.send button{min-height:46px;border:0;border-radius:11px;background:var(--rose);color:white;font-size:13px;font-weight:700;box-shadow:0 7px 16px #ad514b24}.primary:hover,.send button:hover{transform:translateY(-1px);background:var(--rose-dark);box-shadow:0 10px 20px #ad514b30}.primary{margin-top:11px}.auth-switch{display:flex;gap:4px;margin:23px 0 16px;padding:4px;border:1px solid var(--line);border-radius:12px;background:#f8f5f0}.auth-switch button{flex:1;padding:10px;border:0;border-radius:9px;background:transparent;color:var(--muted);font:600 12px Arial,sans-serif}.auth-switch button.active{background:#fffdfa;color:var(--rose-dark);box-shadow:0 2px 6px #39241a12}
.preview{gap:9px}.preview img{width:61px;height:61px;border:2px solid white;border-radius:13px;box-shadow:0 3px 12px #29171220}
#messages-view{display:block;min-height:0}#conversation{display:none;flex-direction:column;min-width:0;min-height:440px;padding:16px;border:1px solid #ebe3dd;border-radius:20px;background:#fffdfa;box-shadow:0 8px 28px #39241a08}#messages-view.has-active-chat>.heading,#messages-view.has-active-chat>.matches{display:none}#messages-view.has-active-chat #conversation{display:flex}.matches{display:flex;flex-direction:column;gap:8px;max-height:490px;margin:0;padding:0 4px 0 0}.match{padding:11px;border:1px solid #eee6df;border-radius:15px;background:#fffdfa;box-shadow:0 3px 10px #38201808}.match:hover,.match.active{border-color:#d6a29a;background:#fff9f6;box-shadow:0 5px 18px #ad514b10}.match-photo{width:48px;height:48px}.match-name{color:var(--ink);font-size:13px}.match-last{color:var(--muted);font-size:11px}.chat-header{display:flex;align-items:center;gap:12px;padding:1px 0 15px;border-bottom:1px solid var(--line)}.back-matches-button{flex:none;min-height:40px;padding:0 13px;border:1px solid var(--line);border-radius:11px;background:#fff;color:var(--ink);font-size:12px;font-weight:600}.back-matches-button:hover{border-color:#d6a29a;background:#fff8f5}.chat-avatar{width:46px;height:46px;flex:none;object-fit:cover;border-radius:50%;background:#eee7e1}.chat-person{display:grid;gap:4px;min-width:0}.chat-person h2{margin:0;font:600 19px Georgia,serif}.chat-person span{color:var(--muted);font-size:11px}.chat{flex:1;max-height:55vh;min-height:260px;padding:15px 3px}.bubble{max-width:78%;margin:7px 0;padding:11px 15px;border-radius:17px 17px 17px 5px;background:#f2eeea;color:#433833;font-size:13px;line-height:1.5}.bubble.mine{border-radius:17px 17px 5px 17px;background:#a94e49;color:white}.bubble time{color:#9a8b83;font-size:10px}.bubble.mine time{color:#ffffffb8}.send{gap:9px;padding-top:8px}.send input{flex:1;min-width:0;border-radius:999px}.send button{padding:0 20px;border-radius:999px}.empty{align-self:center;margin:auto;color:var(--muted);line-height:1.6}
#profile-view>.heading{margin-bottom:20px}
#my-profile{display:grid;grid-template-columns:1fr;gap:14px}
.profile-card{overflow:hidden;border:1px solid var(--line);border-radius:20px;background:var(--paper);box-shadow:0 8px 28px #39241a08}
.profile-summary{display:flex;align-items:center;gap:19px;padding:24px}
.profile-avatar{width:112px;height:132px;flex:none;object-fit:cover;border-radius:15px;background:#eee7e1}
.profile-identity{min-width:0}.profile-identity h2{margin:0;font:600 27px Georgia,serif;letter-spacing:-.4px}.profile-identity p{margin:7px 0 0;color:var(--muted);font-size:13px}.profile-private{display:flex;align-items:center;gap:9px;padding:14px 24px;border-top:1px solid var(--line);color:var(--muted);font-size:12px}.profile-private strong{color:var(--ink);font-weight:600}.profile-actions{display:flex;flex-direction:column;gap:10px;align-items:stretch}.profile-actions #view-own-profile{width:100%;min-height:50px}
.profile-edit-toggle{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;min-height:50px;margin:0;padding:0 20px;border:1px solid var(--rose);border-radius:12px;background:var(--rose);color:white;font-weight:700;box-shadow:0 7px 16px #ad514b24}
.profile-edit-toggle:before{content:"✎";font-size:18px}.profile-edit-toggle:hover{transform:translateY(-1px);background:var(--rose-dark);box-shadow:0 10px 20px #ad514b30}
#edit-form{padding:23px;border:1px solid var(--line);border-radius:20px;background:var(--paper);box-shadow:0 8px 28px #39241a08}
.edit-heading h2{margin:0;font:600 23px Georgia,serif}.edit-heading p{margin:6px 0 17px;color:var(--muted);font-size:12px;line-height:1.5}.edit-actions{display:flex;gap:10px;margin-top:18px}.edit-actions button{width:auto;min-width:140px;margin:0;padding:0 18px}.secondary-button{min-height:46px;padding:0 18px;border:1px solid var(--line);border-radius:11px;background:white;color:var(--ink);font-weight:600}.secondary-button:hover{border-color:#cdbdb2;background:#f8f5f0}
#likes-list{display:grid;gap:10px}#likes-list article{align-items:center!important;padding:14px!important;border:1px solid var(--line);border-radius:16px;background:#fffdfa}#likes-list article img{border-radius:13px!important}#likes-list article p{line-height:1.5}#likes-list .like-reply{padding:8px 13px;border:1px solid var(--line);border-radius:999px;background:#fffdfa;font-size:12px;font-weight:600}#likes-list .like-reply[data-liked="1"]{border-color:var(--rose);background:var(--rose);color:white}#likes-list .like-reply:hover{transform:translateY(-1px)}
.notice{bottom:26px;padding:13px 18px;border:1px solid #ffffff20;border-radius:14px;background:#302523;color:white;font-size:13px;box-shadow:0 12px 35px #21120f30}
.conversation-pending{display:flex;flex:1;flex-direction:column;align-items:center;justify-content:center;gap:14px;min-height:290px;color:var(--muted);font-size:13px}.conversation-pending img{width:72px;height:72px;object-fit:cover;border-radius:50%;background:#eee7e1}.conversation-pending p{margin:0}.conversation-spinner{width:22px;height:22px;border:2px solid #eadbd2;border-top-color:var(--rose);border-radius:50%;animation:conversation-spin .8s linear infinite}@keyframes conversation-spin{to{transform:rotate(360deg)}}.conversation-error{display:grid;justify-items:center;gap:12px;margin:auto;padding:28px 14px;text-align:center;color:var(--muted);font-size:13px}.conversation-error button{padding:10px 15px;border:1px solid var(--line);border-radius:10px;background:white;color:var(--ink);font-weight:600}
.match-overlay[hidden]{display:none}.match-overlay{position:fixed;z-index:100;inset:0;display:grid;place-items:center;padding:max(16px,env(safe-area-inset-top)) 16px max(16px,env(safe-area-inset-bottom));background:#241b23a8;backdrop-filter:blur(10px);opacity:0;transition:opacity .22s ease}.match-overlay.is-open{opacity:1}.match-dialog{position:relative;width:min(100%,420px);max-height:100%;overflow:auto;overscroll-behavior:contain;padding:52px 28px 24px;border:1px solid #ffffffc9;border-radius:28px;background:radial-gradient(ellipse at 50% 0%,#f4d9d4 0%,#fffaf6 58%);text-align:center;box-shadow:0 24px 80px #24121f40;transform:translateY(14px) scale(.98);transition:transform .3s ease}.is-open .match-dialog{transform:none}.match-close{position:absolute;top:12px;right:12px;display:grid;width:36px;height:36px;place-items:center;padding:0;border:0;border-radius:50%;background:#ffffffa6;color:#796962;font-size:24px;line-height:1}.match-close:hover{background:white}.match-avatars{position:relative;display:flex;justify-content:center;align-items:center;width:100%;margin:8px auto 28px;padding:10px 0}.match-avatar{display:block;flex:none;width:112px;height:112px;object-fit:cover;border:5px solid #fffaf6;border-radius:50%;background:#e8d8d1;box-shadow:0 8px 24px #673f3821}.match-avatar-self{margin-right:-10px;transform:rotate(-7deg)}#match-avatar{transform:rotate(7deg)}.match-heart-badge{position:absolute;left:50%;bottom:0;display:grid;width:42px;height:42px;place-items:center;transform:translateX(-50%);border:4px solid #fffaf6;border-radius:50%;background:var(--rose);color:white;font-size:20px;box-shadow:0 4px 14px #a94e4933}.match-eyebrow{margin:0 0 10px;color:var(--rose-dark);font-size:10px;font-weight:700;letter-spacing:.2em;text-transform:uppercase}.match-dialog h2{margin:0;color:var(--ink);font:500 clamp(32px,8vw,42px)/1.12 Georgia,serif;letter-spacing:-1.2px}.match-caption{max-width:300px;margin:16px auto 26px;color:var(--muted);font-size:14px;line-height:1.6;overflow-wrap:anywhere}.match-dialog-actions{display:grid;gap:8px}.match-dialog-actions button{width:100%;min-height:48px;margin:0;padding:12px 16px;border-radius:14px;font-size:13px;font-weight:600;white-space:normal}.match-message-button{border:0;background:var(--rose);color:white;box-shadow:0 6px 18px #ad514b24}.match-message-button:hover{background:var(--rose-dark)}.match-continue-button{border:1px solid transparent;background:transparent;color:var(--muted)}.match-continue-button:hover{background:#efe4dd}.match-dialog button:focus-visible{outline:3px solid var(--rose);outline-offset:3px}
@media(max-height:550px){.match-dialog{padding:38px 22px 16px}.match-avatars{margin:0 auto 16px;padding:4px 0 10px}.match-avatar{width:76px;height:76px}.match-heart-badge{width:34px;height:34px;font-size:15px}.match-dialog h2{font-size:30px}.match-caption{margin:10px auto 16px}.match-dialog-actions button{min-height:44px}}
@media(prefers-reduced-motion:reduce){.match-overlay,.match-dialog,.match-avatar,.match-heart-badge,.conversation-spinner{animation:none!important;transition:none!important}.match-dialog{transform:none}.match-overlay{opacity:1}}
@media(max-width:760px){.shell{padding:0 20px}.form{grid-template-columns:.72fr 1.28fr;min-height:560px}.auth-aside{padding:27px}.auth-copy h2{font-size:34px}.auth-main{padding:32px 26px}.content{padding-top:27px}#messages-view{gap:0 14px;grid-template-columns:minmax(190px,.75fr) minmax(0,1.25fr)}}
@media(max-width:600px){.shell{padding:0 16px}.top{height:66px}.top strong{font-size:15px}.top strong:before{width:33px;height:33px}.top span{font-size:12px}.top button:not(.hide){padding:8px 11px;font-size:11px}.content{padding:24px 0 90px}.heading{margin-bottom:17px}.heading h1,.heading h2{font-size:27px}.tabs{position:fixed;z-index:3;inset:auto 12px max(12px,env(safe-area-inset-bottom));width:auto;justify-content:space-around;margin:0;padding:6px;border:1px solid #e9e1da;border-radius:18px;background:#fffdfaf2;box-shadow:0 8px 30px #34221c20;backdrop-filter:blur(14px)}.tab{flex:1;padding:11px 5px;font-size:11px}.card{width:100%;height:min(61vh,610px);min-height:370px;border-radius:19px}.card-info{padding:23px}.card-info h2{font-size:31px}.card-info p{font-size:14px}.actions{margin-top:15px}.actions button{width:54px;height:54px}.form{display:flex;flex-direction:column;min-height:0;margin:20px auto;border-radius:22px}.auth-aside{min-height:170px;padding:22px 24px}.auth-brand{font-size:11px}.auth-brand span{width:29px;height:29px}.auth-copy{margin:23px 0 0}.auth-copy small{font-size:9px}.auth-copy h2{max-width:290px;margin:8px 0 5px;font-size:29px}.auth-copy p{max-width:310px;font-size:12px}.auth-main{width:100%;padding:24px}.form h1{font-size:29px}.form>p{margin-bottom:15px}.field{margin:7px 0}.field textarea{min-height:72px}.primary{min-height:45px}.preview img{width:54px;height:54px}#messages-view{display:flex;flex-direction:column;min-height:0}.matches{display:grid;grid-template-columns:1fr;max-height:190px;gap:6px;margin-bottom:12px;padding:0}.match{padding:8px 10px;border-radius:13px}.match-photo{width:42px;height:42px}#conversation{min-height:390px;padding:12px;border-radius:17px}.chat{min-height:230px;max-height:43vh}.send button{padding:0 14px}.send input{font-size:12px}.profile-summary{gap:14px;padding:17px}.profile-avatar{width:84px;height:104px;border-radius:12px}.profile-identity h2{font-size:23px}.profile-private{padding:13px 17px;flex-wrap:wrap}#edit-form{padding:17px}.edit-actions{flex-direction:column-reverse}.edit-actions button{width:100%}.notice{bottom:84px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
@media(max-width:600px){.shell{padding:0 16px}.top{height:66px}.top strong{font-size:14px}.top strong:before{width:33px;height:33px}.top span{font-size:11px}.content{padding:24px 0 90px}.heading h1,.heading h2{font-size:27px}.tabs{position:static;width:100%;margin:0 0 23px;border-radius:15px}.tab{flex:1;padding:10px 6px;font-size:12px}.card{width:100%;height:62vh;min-height:360px;border-radius:18px}.card-info{padding:22px}.card-info h2{font-size:31px}.form{grid-template-columns:1fr;min-height:0;margin:20px auto 80px;border-radius:20px}.auth-aside{min-height:180px;padding:24px}.auth-copy{margin:28px 0 0}.auth-copy h2{font-size:34px}.auth-copy p{font-size:12px}.auth-main{padding:28px 23px 30px}.form h1{font-size:29px}#conversation{min-height:calc(100dvh - 185px);padding:11px;border-radius:15px}.chat{min-height:0;max-height:none}.chat-header{gap:8px}.back-matches-button{min-height:38px;padding:0 9px;font-size:11px}.chat-avatar{width:40px;height:40px}.chat-person h2{font-size:17px}.send button{padding:0 15px}.profile-summary{gap:14px;padding:17px}.profile-avatar{width:86px;height:106px}.profile-identity h2{font-size:22px}.profile-private{flex-wrap:wrap;padding:12px 17px}.profile-edit-toggle{min-height:48px}#edit-form{padding:18px}.edit-heading h2{font-size:24px}.edit-actions{flex-direction:column-reverse}.edit-actions button{width:100%}.notice{bottom:84px}}
.push-settings{display:grid;grid-template-columns:1fr auto;align-items:center;gap:8px 18px;padding:20px 22px;border:1px solid var(--line);border-radius:18px;background:var(--paper);box-shadow:0 8px 28px #39241a08}.push-settings h2{margin:0;font:600 19px Georgia,serif}.push-settings p{margin:5px 0 0;color:var(--muted);font-size:12px;line-height:1.5}.push-settings button{grid-column:2;grid-row:1/3;min-height:42px;padding:0 15px;border:1px solid var(--rose);border-radius:11px;background:var(--rose);color:white;font-size:12px;font-weight:700}.push-settings button:disabled{border-color:var(--line);background:#eee9e4;color:var(--muted)}#push-status{grid-column:1/-1;margin-top:3px}
@media(max-width:600px){.push-settings{grid-template-columns:1fr;padding:17px}.push-settings button{grid-column:1;grid-row:auto;width:100%}#push-status{grid-column:1}}
@media(max-width:600px){.field input:not([type=file]),.field textarea,.send input{font-size:16px}}
.card .card-backdrop{filter:blur(18px) brightness(.62);transform:scale(1.06);pointer-events:none}
.card .card-photo{object-fit:contain;padding:8px;pointer-events:none}
.card-info{z-index:1}
#likes-list article img{object-fit:contain!important;background:#eee7e1}
.chat-profile-gallery img{object-fit:contain}
.swipe-profile-dialog{width:min(100%,570px);max-height:92dvh;overflow:visible;border:0;background:transparent;box-shadow:none}
.swipe-profile-dialog .card{width:100%;height:min(76dvh,680px);min-height:min(420px,70dvh);margin:0}
.own-profile-preview-close{position:absolute;z-index:3;top:14px;right:14px;display:grid;width:40px;height:40px;place-items:center;border:1px solid #ffffffad;border-radius:50%;background:#fffdfae8;color:var(--ink);font-size:24px}
.swipe-profile-dialog .photo-hit{z-index:2}
.swipe-profile-dialog .photo-hit button{pointer-events:auto}
</style></head><body><main class="shell"><header class="top"><strong>Rencontres</strong><span id="account"></span><button id="top-logout" class="hide" type="button">Se déconnecter</button></header>
<section id="signup" class="form"><aside class="auth-aside"><div class="auth-brand"><span>♡</span> RENCONTRES</div><div class="auth-copy"><h2>Un site de rencontre</h2></div></aside><div class="auth-main"><h1>Créer un profil</h1><p>Inscription réservée aux personnes de 18 ans et plus.</p><form id="signup-form"><div class="field"><label>Prénom</label><input name="first_name" maxlength="40" autocomplete="given-name" placeholder="Comment t’appelles-tu ?" required></div><div class="field"><label>Adresse e-mail</label><input name="email" type="email" autocomplete="email" placeholder="toi@exemple.fr" required></div><div class="field"><label>Âge</label><input name="age" type="number" min="18" max="100" placeholder="Ton âge" required></div><div class="field"><label>Description</label><textarea name="bio" rows="3" maxlength="500" placeholder="Quelques mots pour te présenter…" required></textarea></div><div class="field"><label>Photos <small>1 à 3 photos</small></label><input id="photos" type="file" accept="image/jpeg,image/png,image/webp" multiple required><div class="preview" id="preview"></div></div><button class="primary">Créer mon profil <span aria-hidden="true">→</span></button></form></div></section>
<section id="app-view" class="content hide"><nav class="tabs"><button class="tab active" data-view="discover">Découvrir</button><button class="tab" data-view="messages">Matchs <span id="match-count"></span></button><button class="tab" data-view="profile">Profil</button></nav><div id="discover-view"><div class="heading"><div><h1>Profils</h1><p>Choisis les profils qui t'intéressent.</p></div><span id="profile-count"></span></div><div id="card-area"></div><div id="actions" class="actions"><button id="pass" aria-label="Passer">&#10005;</button><button id="like" class="like" aria-label="Aimer">&#9829;</button></div><p class="hint">Utilise les boutons pour aimer ou passer</p></div><div id="messages-view" class="hide"><div class="heading"><h1>Matchs</h1></div><div id="matches" class="matches"></div><div id="conversation"><p class="empty">Choisis un match.</p></div></div><div id="profile-view" class="hide"><div class="heading"><div><h1>Mon profil</h1><p>Consulte et mets à jour tes informations.</p></div></div><div id="my-profile"></div><section class="push-settings"><div><h2>Notifications sur ce téléphone</h2><p>Pour recevoir les notifications, ajoute Rencontres à l’écran d’accueil puis ouvre-le depuis son icône. Si l’option reste indisponible, cet appareil ne les prend pas en charge.</p></div><button id="push-toggle" type="button" disabled>Chargement…</button><p id="push-status" role="status" aria-live="polite">Vérification de la disponibilité…</p></section></div></section></main><div id="match-overlay" class="match-overlay" hidden aria-hidden="true"><section class="match-dialog" role="dialog" aria-modal="true" aria-labelledby="match-title" aria-describedby="match-caption" tabindex="-1"><button id="match-close" class="match-close" type="button" aria-label="Fermer">×</button><div class="match-avatars"><img id="match-self-avatar" class="match-avatar match-avatar-self" alt="Votre photo"><img id="match-avatar" class="match-avatar" alt=""><span class="match-heart-badge" aria-hidden="true">♥</span></div><p class="match-eyebrow">C’est réciproque</p><h2 id="match-title">Nouveau match&nbsp;!</h2><p id="match-caption" class="match-caption"></p><div class="match-dialog-actions"><button id="match-message" class="match-message-button" type="button">Envoyer un message</button><button id="match-continue" class="match-continue-button" type="button">Continuer à découvrir</button></div></section></div><div id="notice" class="notice hide"></div>
<script>
const $=q=>document.querySelector(q),s={user:null,profiles:[],matches:[],likes:[],active:null,photo:0,messages:[],messageLoad:0,poll:null,swiping:false};
const chatProfileOverlay=document.createElement('div');chatProfileOverlay.id='chat-profile-overlay';chatProfileOverlay.className='chat-profile-overlay';chatProfileOverlay.hidden=true;chatProfileOverlay.setAttribute('aria-hidden','true');document.body.append(chatProfileOverlay);
let profileLoads=0;
function beginProfilesLoading(){const area=$('#card-area');profileLoads++;area.classList.add('is-loading');area.setAttribute('aria-busy','true');return()=>{profileLoads=Math.max(0,profileLoads-1);if(!profileLoads){area.classList.remove('is-loading');area.setAttribute('aria-busy','false')}}}
let chatProfilePhotoIndex=0,chatProfilePreviousFocus=null;
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function note(t){const e=$('#notice');e.textContent=t;e.classList.remove('hide');setTimeout(()=>e.classList.add('hide'),3000)}
let matchCloseTimer,matchPreviousFocus;
function celebrateMatch(matchId,profile){const overlay=$('#match-overlay'),name=profile?.first_name||'cette personne',photo=profile?.photo_paths?.[0]||'';clearTimeout(matchCloseTimer);matchPreviousFocus=document.activeElement;overlay.dataset.matchId=matchId;for(const [selector,src] of [['#match-self-avatar',s.user?.photo_paths?.[0]],['#match-avatar',photo]]){const image=$(selector);image.onerror=()=>{image.onerror=null;image.src='/static/icon.svg'};image.src=src||'/static/icon.svg'}$('#match-avatar').alt=`Photo de ${name}`;$('#match-title').textContent='C’est un match !';$('#match-caption').textContent=`Toi et ${name}, vous vous plaisez. Et si vous faisiez connaissance ?`;overlay.hidden=false;overlay.setAttribute('aria-hidden','false');overlay.classList.remove('is-open');void overlay.offsetWidth;requestAnimationFrame(()=>overlay.classList.add('is-open'));$('#match-close').focus()}
function closeMatchCelebration(){const overlay=$('#match-overlay');if(overlay.hidden)return;overlay.classList.remove('is-open');overlay.setAttribute('aria-hidden','true');clearTimeout(matchCloseTimer);matchCloseTimer=setTimeout(()=>{overlay.hidden=true;if(matchPreviousFocus?.isConnected)matchPreviousFocus.focus()},260)}
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}}),d=await r.json();if(!r.ok)throw Error(d.error||'Erreur serveur.');return d}

async function photoData(file){const bitmap=await createImageBitmap(file),scale=Math.min(1,1400/Math.max(bitmap.width,bitmap.height)),canvas=document.createElement('canvas');canvas.width=Math.round(bitmap.width*scale);canvas.height=Math.round(bitmap.height*scale);canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();return canvas.toDataURL('image/jpeg',.82)}
function decodeVapidKey(value){const base64=(value+'='.repeat((4-value.length%4)%4)).replace(/-/g,'+').replace(/_/g,'/'),raw=atob(base64);return Uint8Array.from(raw,char=>char.charCodeAt(0))}
async function loadPushSettings(){const button=$('#push-toggle'),status=$('#push-status');if(!button||!status)return;button.disabled=true;try{const supported=window.isSecureContext&&'serviceWorker'in navigator&&'PushManager'in window&&'Notification'in window;if(!supported){status.textContent='Ajoute Rencontres à l’écran d’accueil et ouvre-le depuis son icône pour recevoir les notifications.';button.textContent='Indisponible';return}const config=await api('/api/push/config');if(!config.configured){status.textContent='Les notifications ne sont pas encore configurées sur le serveur.';button.textContent='Indisponible';return}const registration=await navigator.serviceWorker.getRegistration('/'),subscription=registration?await registration.pushManager.getSubscription():null,isSubscribed=subscription?(await api('/api/push/status',{method:'POST',body:JSON.stringify({endpoint:subscription.endpoint})})).subscribed:false;button.dataset.subscribed=String(isSubscribed);button.textContent=isSubscribed?'Désactiver les notifications':'Activer les notifications';button.disabled=false;status.textContent=isSubscribed?'Les notifications sont activées sur ce téléphone.':'Active les notifications pour être prévenu(e) des matchs et messages.'}catch(error){status.textContent=`Impossible de vérifier les notifications : ${error.message}`;button.textContent='Réessayer';button.disabled=false}}
async function togglePushSettings(){const button=$('#push-toggle'),status=$('#push-status');button.disabled=true;try{const shouldEnable=button.dataset.subscribed!=='true';if(shouldEnable){if(Notification.permission==='denied')throw Error('Autorisation refusée. Modifie-la dans les réglages du navigateur.');const permission=Notification.permission==='granted'?'granted':await Notification.requestPermission();if(permission!=='granted')throw Error('Autorisation de notification non accordée.')}const config=await api('/api/push/config');if(!config.configured)throw Error('Les notifications ne sont pas configurées sur le serveur.');const registration=await navigator.serviceWorker.register('/service-worker.js'),existing=await registration.pushManager.getSubscription(),isSubscribed=existing?(await api('/api/push/status',{method:'POST',body:JSON.stringify({endpoint:existing.endpoint})})).subscribed:false;if(existing&&isSubscribed){await api('/api/push/subscription',{method:'DELETE',body:JSON.stringify({endpoint:existing.endpoint})});await existing.unsubscribe();status.textContent='Notifications désactivées sur ce téléphone.'}else{const subscription=existing||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:decodeVapidKey(config.public_key)});await api('/api/push/subscription',{method:'POST',body:JSON.stringify(subscription.toJSON())});status.textContent='Notifications activées sur ce téléphone.'}await loadPushSettings()}catch(error){status.textContent=error.message;button.disabled=false}}
async function enter(user){s.user=user;$('#signup').classList.add('hide');$('#app-view').classList.remove('hide');document.body.classList.add('discover-open');$('#account').textContent=user.first_name;$('#top-logout').classList.remove('hide');await Promise.all([loadProfiles(),loadMatches()]);const params=new URLSearchParams(location.search),matchId=params.get('match');if(matchId){history.replaceState(null,'',location.pathname);showView('messages',false);await openMatch(matchId)}else if(params.get('view')==='likes'){history.replaceState(null,'',location.pathname);showView('likes')}}
async function loadProfiles(){const stopLoading=beginProfilesLoading();try{s.profiles=await api('/api/profiles');s.photo=0;renderCard();await waitForCardPhoto()}finally{stopLoading()}}
async function loadLikes(){s.likes=await api('/api/likes');$('#like-count').textContent=s.likes.length?`(${s.likes.length})`:'';$('#likes-list').innerHTML=s.likes.length?s.likes.map(p=>`<article style="display:grid;grid-template-columns:78px minmax(0,1fr) auto;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid #deded5"><button type="button" class="like-profile-photo" data-profile-id="${esc(p.id)}" aria-label="Voir le profil de ${esc(p.first_name)}" aria-haspopup="dialog"><img src="${esc(p.photo_paths[0])}" alt="Photo de ${esc(p.first_name)}"></button><div><strong>${esc(p.first_name)}, ${p.age}</strong><p style="margin:6px 0;color:#77786e;font-size:12px">${esc(p.bio)}</p><button type="button" class="like-profile-link" data-profile-id="${esc(p.id)}" aria-haspopup="dialog">Voir le profil</button></div><div style="display:grid;gap:6px"><button class="like-reply" data-id="${p.id}" data-liked="1">Aimer</button><button class="like-reply" data-id="${p.id}" data-liked="0">Passer</button></div></article>`).join(''):'<p class="empty">Aucun like reçu pour le moment.</p>'}
function profileCardMarkup(person,photoIndex,stepAttribute='data-step',cardId='card'){const photos=person.photo_paths||[],photo=photos[photoIndex];return `<article class="card"${cardId?` id="${cardId}"`:''}><img class="card-backdrop" src="${esc(photo)}" alt="" aria-hidden="true"><img class="card-photo" src="${esc(photo)}" alt="Photo de ${esc(person.first_name)}" decoding="async" fetchpriority="high">${photos.length>1?`<div class="photo-hit"><button ${stepAttribute}="-1" aria-label="Photo précédente"><span aria-hidden="true">‹</span></button><button ${stepAttribute}="1" aria-label="Photo suivante"><span aria-hidden="true">›</span></button><span class="card-photo-count" aria-live="polite">${photoIndex+1} / ${photos.length}</span></div>`:''}<div class="card-info"><h2>${esc(person.first_name)}, ${person.age}</h2><p>${esc(person.bio)}</p></div></article>`}
function renderCard(){const p=s.profiles[0];$('#profile-count').textContent=p?`${s.profiles.length} profil${s.profiles.length===1?'':'s'}`:'';$('#actions').classList.toggle('hide',!p);if(!p){$('#card-area').innerHTML='<p class="empty">Aucun profil disponible.</p>';return}$('#card-area').innerHTML=profileCardMarkup(p,s.photo%p.photo_paths.length)}
function setSwipePending(pending){s.swiping=pending;$('#pass').disabled=pending;$('#like').disabled=pending}
function waitForCardPhoto(){const image=$('#card-area .card-photo');if(!image||image.complete)return Promise.resolve();return new Promise(resolve=>{image.addEventListener('load',resolve,{once:true});image.addEventListener('error',resolve,{once:true})})}
async function swipe(liked){if(s.swiping)return;const p=s.profiles[0];if(!p)return;setSwipePending(true);try{const r=await api('/api/swipes',{method:'POST',body:JSON.stringify({target_id:p.id,liked})});s.profiles.shift();renderCard();await waitForCardPhoto();if(r.match){celebrateMatch(r.match,p);try{await loadMatches()}catch(error){note(`Match créé, mais les conversations n’ont pas pu être actualisées : ${error.message}`)}}}catch(e){note(e.message)}finally{setSwipePending(false)}}
async function loadMatches(){s.matches=await api('/api/matches');$('#match-count').textContent=s.matches.length?`(${s.matches.length})`:'';$('#matches').innerHTML=s.matches.length?s.matches.map(m=>`<button class="match ${s.active?.id===m.id?'active':''}" data-id="${m.id}"><img class="match-photo" src="${esc(m.profile.photo_paths?.[0]||'')}" alt=""><span class="match-info"><strong class="match-name">${esc(m.profile.first_name)}</strong><span class="match-last">${esc(m.last_message||'Nouveau match')}</span></span></button>`).join(''):'<p class="empty">Aucun match.</p>';if(s.active){s.active=s.matches.find(m=>m.id===s.active.id)||null;if(s.active)await loadMessages(false);else $('#messages-view').classList.remove('has-active-chat')}}
function showConversationLoading(match){const person=match.profile,container=$('#conversation');container.dataset.matchId=match.id;container.innerHTML=`<div class="conversation-pending" role="status"><img src="${esc(person.photo_paths?.[0]||'')}" alt=""><span class="conversation-spinner" aria-hidden="true"></span><p>Ouverture de la conversation avec ${esc(person.first_name)}…</p></div>`}
function showConversationError(matchId,error){const container=$('#conversation');container.innerHTML=`<div class="conversation-error" role="alert"><p>Impossible de charger cette conversation : ${esc(error.message)}</p><button id="retry-conversation" type="button">Réessayer</button></div>`;$('#retry-conversation').onclick=()=>openMatch(matchId)}
async function openMatch(id){const match=s.matches.find(m=>m.id===id);if(!match)return;if(s.poll)clearInterval(s.poll);s.poll=null;s.active=match;document.body.classList.remove('matches-open');document.body.classList.add('chat-open');$('#messages-view').classList.add('has-active-chat');showConversationLoading(match);await loadMessages(true);if(!s.active||s.active.id!==id)return;s.poll=setInterval(()=>loadMessages(false),3000)}
function messageBubbles(){let previousDate='';return s.messages.map(m=>{const date=new Date(m.created_at),dateKey=`${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`,today=new Date(),yesterday=new Date(today.getFullYear(),today.getMonth(),today.getDate()-1),dateLabel=dateKey===`${today.getFullYear()}-${today.getMonth()}-${today.getDate()}`?'Aujourd’hui':dateKey===`${yesterday.getFullYear()}-${yesterday.getMonth()}-${yesterday.getDate()}`?'Hier':date.toLocaleDateString('fr-FR',{day:'numeric',month:'long',year:'numeric'}),separator=dateKey!==previousDate?`<div class="chat-date-separator"><span>${dateLabel}</span></div>`:'';previousDate=dateKey;const mine=String(m.sender_id).toLowerCase()===String(s.user.id).toLowerCase();return `${separator}<div class="bubble ${mine?'mine':'received'}" style="align-self:${mine?'flex-end':'flex-start'};margin:0">${esc(m.body)}<time>${date.toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit'})}</time></div>`}).join('')}
async function loadMessages(scroll){if(!s.active)return;const matchId=s.active.id,loadId=++s.messageLoad,container=$('#conversation'),sameMatch=container.dataset.matchId===matchId,existingChat=sameMatch?$('#chat'):null,near=existingChat&&existingChat.scrollHeight-existingChat.scrollTop-existingChat.clientHeight<45;try{const messages=await api(`/api/matches/${matchId}/messages`);if(loadId!==s.messageLoad||!s.active||s.active.id!==matchId)return;s.messages=messages;if(existingChat){existingChat.innerHTML=messageBubbles();if(scroll||near)existingChat.scrollTop=existingChat.scrollHeight}else drawMessages(scroll)}catch(e){if(loadId===s.messageLoad&&s.active?.id===matchId&&!existingChat)showConversationError(matchId,e);note(e.message)}}
function drawMessages(scroll){const p=s.active.profile,photo=p.photo_paths?.[0],container=$('#conversation');container.dataset.matchId=s.active.id;container.innerHTML=`<div class="chat-header"><button id="back-matches" class="back-matches-button" type="button" aria-label="Retour aux matchs">Matchs</button><img class="chat-avatar" src="${esc(photo||'')}" alt="Photo de ${esc(p.first_name)}"><div class="chat-person"><h2>${esc(p.first_name)}</h2><span>Votre conversation</span></div><button id="view-chat-profile" class="view-chat-profile" type="button" aria-haspopup="dialog">Voir le profil</button></div><div id="chat" class="chat" style="display:flex;flex-direction:column;gap:8px">${messageBubbles()}</div><form id="send-form" class="send"><input id="message-input" maxlength="2000" placeholder="Votre message" required autocomplete="off"><button>Envoyer</button></form>`;if(scroll)$('#chat').scrollTop=$('#chat').scrollHeight}
function openSwipeProfilePreview(person,label){const photos=Array.isArray(person?.photo_paths)?person.photo_paths:[];if(!person||!photos.length)return;chatProfilePhotoIndex=0;chatProfilePreviousFocus=document.activeElement;chatProfileOverlay.innerHTML=`<section class="chat-profile-dialog swipe-profile-dialog" role="dialog" aria-modal="true" aria-label="${esc(label)}" tabindex="-1">${profileCardMarkup(person,0,'data-preview-step','')}<button class="own-profile-preview-close" type="button" aria-label="Fermer le profil">×</button></section>`;chatProfileOverlay.dataset.photos=JSON.stringify(photos);chatProfileOverlay.hidden=false;chatProfileOverlay.setAttribute('aria-hidden','false');requestAnimationFrame(()=>chatProfileOverlay.classList.add('is-open'));chatProfileOverlay.querySelector('.own-profile-preview-close').focus();if(photos.length>1)updateChatProfilePhoto(0)}
function openChatProfile(){if(s.active)openSwipeProfilePreview(s.active.profile,'Profil de votre match')}
function openOwnProfilePreview(){openSwipeProfilePreview(s.user,'Aperçu de mon profil')}
function updateChatProfilePhoto(step){const photos=JSON.parse(chatProfileOverlay.dataset.photos||'[]');if(!photos.length)return;chatProfilePhotoIndex=(chatProfilePhotoIndex+step+photos.length)%photos.length;const image=chatProfileOverlay.querySelector('#chat-profile-photo, .swipe-profile-dialog .card-photo');image.src=photos[chatProfilePhotoIndex];const count=chatProfileOverlay.querySelector('#chat-profile-photo-count, .swipe-profile-dialog .card-photo-count');if(count)count.textContent=`${chatProfilePhotoIndex+1} / ${photos.length}`}
function closeChatProfile(){if(chatProfileOverlay.hidden)return;chatProfileOverlay.classList.remove('is-open');chatProfileOverlay.setAttribute('aria-hidden','true');chatProfileOverlay.hidden=true;if(chatProfilePreviousFocus?.isConnected)chatProfilePreviousFocus.focus()}
function backToMatches(){closeChatProfile();if(s.poll)clearInterval(s.poll);s.poll=null;s.messageLoad++;s.active=null;s.messages=[];document.body.classList.remove('chat-open');document.body.classList.add('matches-open');$('#messages-view').classList.remove('has-active-chat');delete $('#conversation').dataset.matchId;$('#conversation').innerHTML='<p class="empty">Choisis un match.</p>';loadMatches()}
async function send(e){e.preventDefault();const form=e.target;if(form.dataset.sending==='true')return;const input=$('#message-input'),button=form.querySelector('button'),body=input.value.trim(),matchId=s.active?.id;if(!body||!matchId)return;form.dataset.sending='true';input.disabled=true;button.disabled=true;try{const sent=await api(`/api/matches/${matchId}/messages`,{method:'POST',body:JSON.stringify({body})});input.value='';if(s.active?.id===matchId){s.messageLoad++;s.messages.push(sent);const chat=$('#chat');if(chat){chat.innerHTML=messageBubbles();chat.scrollTop=chat.scrollHeight}const matchButton=[...$('#matches').querySelectorAll('.match')].find(item=>item.dataset.id===matchId);if(matchButton)matchButton.querySelector('.match-last').textContent=sent.body}}catch(err){note(err.message)}finally{form.dataset.sending='false';input.disabled=false;button.disabled=false;button.textContent='Envoyer';input.focus()}}
function showView(v,refresh=true){['discover','likes','messages','profile'].forEach(x=>$('#'+x+'-view').classList.toggle('hide',x!==v));document.body.classList.toggle('discover-open',v==='discover');document.body.classList.toggle('chat-open',v==='messages'&&$('#messages-view').classList.contains('has-active-chat'));document.body.classList.toggle('matches-open',v==='messages'&&!$('#messages-view').classList.contains('has-active-chat'));document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.view===v));const pending=[];if(refresh&&v==='likes')pending.push(loadLikes());if(refresh&&v==='messages')pending.push(loadMatches());if(v==='profile')renderProfile();return Promise.all(pending)}
const likesTab=document.createElement('button');likesTab.className='tab';likesTab.dataset.view='likes';likesTab.innerHTML='Likes <span id="like-count"></span>';document.querySelector('.tabs').append(likesTab);const likesView=document.createElement('div');likesView.id='likes-view';likesView.className='hide';likesView.innerHTML='<div class="heading"><div><h1>Likes reçus</h1><p>Les personnes qui t’ont aimé.</p></div></div><div id="likes-list"></div>';$('#app-view').append(likesView);
$('#likes-list').onclick=async e=>{const preview=e.target.closest('[data-profile-id]');if(preview){const person=s.likes.find(item=>String(item.id)===preview.dataset.profileId);if(person)openSwipeProfilePreview(person,`Profil de ${person.first_name}`);return}const button=e.target.closest('.like-reply');if(!button)return;try{const profile=s.likes.find(item=>item.id===button.dataset.id),result=await api('/api/swipes',{method:'POST',body:JSON.stringify({target_id:button.dataset.id,liked:button.dataset.liked==='1'})}),remaining=s.profiles.filter(item=>String(item.id)!==button.dataset.id);if(remaining.length!==s.profiles.length){s.profiles=remaining;renderCard()}if(result.match)celebrateMatch(result.match,profile);await Promise.all([loadLikes(),loadMatches()])}catch(err){note(err.message)}};
function renderProfile(){
const p=s.user,photo=p.photo_paths?.[0];
$('#my-profile').innerHTML=`<article class="profile-card"><div class="profile-summary"><img class="profile-avatar" src="${esc(photo||'')}" alt="Photo de ${esc(p.first_name)}"><div class="profile-identity"><h2>${esc(p.first_name)}, ${p.age} ans</h2><p>${esc(p.bio)}</p></div></div><div class="profile-private"><strong>Adresse e-mail</strong><span>${esc(p.email)}</span><span>· Privée</span></div></article><div class="profile-actions"><button id="view-own-profile" class="secondary-button" type="button">Voir mon profil</button><button id="edit-profile" class="profile-edit-toggle" type="button" aria-expanded="false" aria-controls="edit-form">Modifier mon profil</button></div><form id="edit-form" class="hide"><div class="edit-heading"><h2>Modifier mon profil</h2><p>Mets à jour tes informations. Tu peux conserver tes photos ou en choisir de nouvelles.</p></div><div class="field"><label for="edit-first-name">Prénom</label><input id="edit-first-name" name="first_name" maxlength="40" value="${esc(p.first_name)}" required></div><div class="field"><label for="edit-age">Âge</label><input id="edit-age" name="age" type="number" min="18" max="100" value="${p.age}" required></div><div class="field"><label for="edit-bio">Description</label><textarea id="edit-bio" name="bio" rows="3" maxlength="500" required>${esc(p.bio)}</textarea></div><div class="field"><label for="edit-photos">Photos <small>Facultatif · jusqu’à 3 photos, 8 Mo chacune</small></label><input id="edit-photos" name="photos" type="file" accept="image/jpeg,image/png,image/webp" multiple></div><div class="edit-actions"><button id="cancel-edit" class="secondary-button" type="button">Annuler</button><button class="primary" type="submit">Enregistrer les modifications</button></div></form>`;
$('#push-toggle').onclick=togglePushSettings;loadPushSettings();
if(!$('#delete-account-dialog'))$('#profile-view .push-settings').insertAdjacentHTML('afterend','<section class="delete-account"><div><h2>Supprimer mon compte</h2><p>Cette action est définitive. Ton profil, tes likes, tes matchs, tes conversations et tes notifications seront supprimés.</p></div><button id="delete-account" type="button">Supprimer mon compte</button></section><div id="delete-account-dialog" class="delete-account-overlay" hidden><section class="delete-account-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-account-title" aria-describedby="delete-account-warning"><span class="delete-account-icon" aria-hidden="true">!</span><p class="delete-account-eyebrow">Suppression définitive</p><h2 id="delete-account-title">Supprimer ton compte ?</h2><p id="delete-account-warning">Ton profil et toutes les données associées seront supprimés, y compris tes likes, matchs et conversations. Cette action est irréversible.</p><form id="delete-account-confirmation"><label for="delete-account-email">Pour confirmer, saisis l’adresse e-mail de ton compte</label><input id="delete-account-email" name="email" type="email" autocomplete="email" placeholder="toi@exemple.fr" required><p id="delete-account-error" class="delete-account-error" role="alert" hidden></p><div class="delete-account-actions"><button id="cancel-delete-account" class="secondary-button" type="button">Annuler</button><button id="confirm-delete-account" class="confirm-delete-account" type="submit">Supprimer définitivement</button></div></form></section></div>');
const deleteDialog=$('#delete-account-dialog'),deleteEmail=$('#delete-account-email'),deleteError=$('#delete-account-error');
const closeDeleteDialog=()=>{deleteDialog.hidden=true;deleteError.hidden=true;$('#delete-account').focus()};
$('#delete-account').onclick=()=>{deleteDialog.hidden=false;deleteEmail.value='';deleteError.hidden=true;deleteEmail.focus()};
$('#cancel-delete-account').onclick=closeDeleteDialog;
deleteDialog.onclick=event=>{if(event.target===deleteDialog)closeDeleteDialog()};
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!deleteDialog.hidden)closeDeleteDialog()});
$('#delete-account-confirmation').onsubmit=async event=>{event.preventDefault();const submit=$('#confirm-delete-account');submit.disabled=true;deleteError.hidden=true;try{await api('/api/account',{method:'DELETE',body:JSON.stringify({email:deleteEmail.value})})}catch(error){deleteError.textContent=error.message;deleteError.hidden=false;submit.disabled=false;return}try{const registration=navigator.serviceWorker?await navigator.serviceWorker.getRegistration('/'):null,subscription=registration?await registration.pushManager.getSubscription():null;if(subscription)await subscription.unsubscribe()}catch(error){console.error('Le compte est supprimé, mais le désabonnement des notifications a échoué.',error)}location.replace('/?account_deleted=1')};
$('#edit-profile').onclick=()=>{const form=$('#edit-form'),opening=form.classList.contains('hide');form.classList.toggle('hide',!opening);$('#edit-profile').setAttribute('aria-expanded',String(opening));$('#edit-profile').textContent=opening?'Annuler la modification':'Modifier mon profil';if(opening)$('#edit-first-name').focus()};
$('#cancel-edit').onclick=renderProfile;
$('#edit-form').onsubmit=async e=>{e.preventDefault();const form=e.currentTarget,files=[...form.photos.files],submit=form.querySelector('[type="submit"]');if(files.length>3||files.some(file=>file.size>8*1024*1024)){note('Choisis au maximum 3 photos de 8 Mo chacune.');return}submit.disabled=true;submit.textContent='Enregistrement…';let updated;try{const payload={first_name:form.first_name.value.trim(),age:Number(form.age.value),bio:form.bio.value.trim()};if(files.length)payload.photo_paths=await Promise.all(files.map(photoData));updated=await api('/api/profile',{method:'PUT',body:JSON.stringify(payload)})}catch(err){note(err.message);submit.disabled=false;submit.textContent='Enregistrer les modifications';return}s.user=updated;$('#account').textContent=updated.first_name;renderProfile();note('Profil mis à jour.');try{await loadProfiles()}catch(err){note(`Profil mis à jour, mais la liste des profils n’a pas pu être actualisée : ${err.message}`)}}
}
const signupForm=$('#signup-form'),signupProgress=document.createElement('div'),authSwitch=document.createElement('div'),loginForm=document.createElement('form');signupProgress.className='signup-progress';signupProgress.hidden=true;signupProgress.setAttribute('role','progressbar');signupProgress.setAttribute('aria-label','Création du compte');signupProgress.innerHTML='<span>Création du compte…</span><span class="signup-progress-track"><span></span></span>';signupForm.insertBefore(signupProgress,signupForm.querySelector('button[type="submit"],button:not([type])'));authSwitch.className='auth-switch';authSwitch.innerHTML='<button type="button" data-mode="signup" class="active">Créer un compte</button><button type="button" data-mode="login">Se connecter</button>';loginForm.id='login-form';loginForm.className='hide';loginForm.innerHTML='<div class="field"><label>Adresse e-mail</label><input name="email" type="email" required></div><button class="primary">Se connecter</button>';signupForm.parentNode.insertBefore(authSwitch,signupForm);signupForm.parentNode.append(loginForm);
function authMode(mode){const creating=mode==='signup';signupForm.classList.toggle('hide',!creating);loginForm.classList.toggle('hide',creating);$('.auth-main h1').textContent=creating?'Créer un profil':'Se connecter';$('.auth-main > p').textContent=creating?'Inscription réservée aux personnes de 18 ans et plus.':'Entre l’adresse e-mail de ton compte.';authSwitch.querySelectorAll('button').forEach(button=>button.classList.toggle('active',button.dataset.mode===mode))}
authSwitch.onclick=e=>{const button=e.target.closest('[data-mode]');if(button)authMode(button.dataset.mode)};loginForm.onsubmit=async e=>{e.preventDefault();try{const user=await api('/api/login',{method:'POST',body:JSON.stringify({email:loginForm.elements.email.value.trim()})});await enter(user)}catch(err){note(err.message)}};
signupForm.onsubmit=async e=>{e.preventDefault();const f=e.currentTarget;if(f.dataset.submitting==='true')return;const files=[...$('#photos').files];if(files.length<1||files.length>3){note('Ajoute entre 1 et 3 photos.');return}if(files.some(x=>x.size>8*1024*1024)){note('Chaque photo doit faire 8 Mo maximum.');return}f.dataset.submitting='true';const submit=f.querySelector('button[type="submit"],button:not([type])');submit.disabled=true;submit.textContent='Création du compte…';signupProgress.hidden=false;try{const user=await api('/api/signup',{method:'POST',body:JSON.stringify({first_name:f.first_name.value.trim(),email:f.email.value.trim(),age:Number(f.age.value),bio:f.bio.value.trim(),photo_paths:await Promise.all(files.map(photoData))})});await enter(user)}catch(err){note(err.message)}finally{delete f.dataset.submitting;submit.disabled=false;submit.innerHTML='Créer mon profil <span aria-hidden="true">→</span>';signupProgress.hidden=true}};
$('#photos').onchange=async()=>{try{$('#preview').innerHTML=(await Promise.all([...$('#photos').files].slice(0,3).map(photoData))).map(x=>`<img src="${x}" alt="Aperçu">`).join('')}catch{note('Photo invalide.')}};document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>showView(x.dataset.view).catch(error=>note(error.message)));$('#pass').onclick=()=>swipe(false);$('#like').onclick=()=>swipe(true);$('#my-profile').onclick=e=>{if(e.target.closest('#view-own-profile'))openOwnProfilePreview()};$('#card-area').onclick=e=>{const step=e.target.closest('[data-step]');if(step){s.photo=(s.photo+Number(step.dataset.step)+s.profiles[0].photo_paths.length)%s.profiles[0].photo_paths.length;renderCard();return}};$('#matches').onclick=e=>{const b=e.target.closest('[data-id]');if(b)openMatch(b.dataset.id)};$('#conversation').onclick=e=>{if(e.target.closest('#back-matches'))backToMatches();if(e.target.closest('#view-chat-profile'))openChatProfile()};chatProfileOverlay.onclick=e=>{if(e.target===chatProfileOverlay||e.target.closest('#close-chat-profile')||e.target.closest('#return-to-chat'))closeChatProfile();else if(e.target.closest('#chat-profile-prev'))updateChatProfilePhoto(-1);else if(e.target.closest('#chat-profile-next'))updateChatProfilePhoto(1)};document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!chatProfileOverlay.hidden)closeChatProfile()});$('#conversation').onsubmit=e=>{if(e.target.id==='send-form')send(e)};const logout=async()=>{let pushError=null;try{const registration=navigator.serviceWorker?await navigator.serviceWorker.getRegistration('/'):null,subscription=registration?await registration.pushManager.getSubscription():null;if(subscription){await api('/api/push/subscription',{method:'DELETE',body:JSON.stringify({endpoint:subscription.endpoint})});await subscription.unsubscribe()}}catch(error){pushError=error}try{await api('/api/logout',{method:'POST'});if(pushError){note(`Session fermée, mais la désactivation des notifications a échoué : ${pushError.message}`);setTimeout(()=>location.reload(),1500)}else location.reload()}catch(error){note(error.message)}};$('#top-logout').onclick=logout;
$('#match-close').onclick=closeMatchCelebration;$('#match-continue').onclick=closeMatchCelebration;$('#match-overlay').onclick=e=>{if(e.target===$('#match-overlay'))closeMatchCelebration()};document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#match-overlay').hidden)closeMatchCelebration()});
$('#match-message').onclick=async()=>{const matchId=$('#match-overlay').dataset.matchId;closeMatchCelebration();showView('messages',false);try{await loadMatches();if(s.matches.some(match=>match.id===matchId))await openMatch(matchId);else note('Ce match est enregistré, mais il est introuvable dans la liste des conversations.')}catch(err){note(err.message)}};
const accountDeleted=new URLSearchParams(location.search).get('account_deleted');if(accountDeleted==='1'){history.replaceState(null,'',location.pathname);note('Votre compte a été supprimé avec succès.')}api('/api/me').then(u=>{if(u)enter(u)}).catch(()=>{});
chatProfileOverlay.onclick=event=>{if(event.target===chatProfileOverlay||event.target.closest('#close-chat-profile')||event.target.closest('#return-to-chat')||event.target.closest('.own-profile-preview-close'))closeChatProfile();else if(event.target.closest('[data-preview-step]'))updateChatProfilePhoto(Number(event.target.closest('[data-preview-step]').dataset.previewStep));else if(event.target.closest('#chat-profile-prev'))updateChatProfilePhoto(-1);else if(event.target.closest('#chat-profile-next'))updateChatProfilePhoto(1)};
$('#card-area').onclick=event=>{const step=event.target.closest('[data-step]');if(!step||!s.profiles[0])return;s.photo=(s.photo+Number(step.dataset.step)+s.profiles[0].photo_paths.length)%s.profiles[0].photo_paths.length;renderCard()};
</script></body></html>'''


@app.get("/")
def index():
    response = app.make_response(render_template_string(HTML))
    response.cache_control.no_store = True
    return response


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(BASE_DIR / "static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/service-worker.js")
def service_worker():
    response = send_from_directory(BASE_DIR / "static", "service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    if not session.get("email"):
        return jsonify(None)
    try:
        return jsonify(as_json(require_user()))
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Profile lookup failed")
        return fail(error, 503)


@app.post("/api/signup")
def create_account():
    data = request.get_json(force=True)
    name = str(data.get("first_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    bio = str(data.get("bio", "")).strip()
    photos = data.get("photo_paths")
    try:
        age = int(data.get("age", 0))
    except (TypeError, ValueError):
        return fail(ValueError("Âge invalide."))
    if not name or len(name) > 40 or not 18 <= age <= 100 or not bio or len(bio) > 500:
        return fail(ValueError("Vérifie le prénom, l'âge et la description."))
    if "@" not in email or len(email) > 254:
        return fail(ValueError("Adresse e-mail invalide."))
    if not isinstance(photos, list) or not 1 <= len(photos) <= 3 or any(not isinstance(p, str) or not p.startswith("data:image/jpeg;base64,") for p in photos):
        return fail(ValueError("Ajoute entre 1 et 3 photos valides."))
    profile_id = str(uuid4())
    uploaded_paths = []
    try:
        uploaded_paths = [upload_photo(photo, profile_id) for photo in photos]
        with get_db() as db:
            row = db.execute("""
                INSERT INTO public.profiles(id,email,first_name,age,bio,photo_paths)
                VALUES(%s,%s,%s,%s,%s,%s)
                RETURNING id,email,first_name,age,bio,photo_paths
            """, (profile_id, email, name, age, bio, uploaded_paths)).fetchone()
        session.clear()
        session["email"] = email
        session.permanent = True
        return jsonify(as_json(row))
    except psycopg2.errors.UniqueViolation:
        for path in uploaded_paths:
            try:
                delete_photo(path)
            except (RuntimeError, requests.RequestException):
                app.logger.exception("Could not remove an orphaned uploaded photo")
        return fail(ValueError("Cette adresse a déjà un compte. Choisis l'onglet Se connecter."), 409)
    except (RuntimeError, psycopg2.Error, requests.RequestException) as error:
        app.logger.exception("Profile save failed")
        return fail(error, 503)


@app.put("/api/profile")
def update_profile():
    try:
        user = require_user()
        data = request.get_json(force=True)
        first_name = str(data.get("first_name", "")).strip()
        bio = str(data.get("bio", "")).strip()
        try:
            age = int(data.get("age", 0))
        except (TypeError, ValueError):
            return fail(ValueError("Âge invalide."))
        photos = data.get("photo_paths")
        if not 1 <= len(first_name) <= 40 or not 18 <= age <= 100 or not 1 <= len(bio) <= 500:
            return fail(ValueError("Vérifie le prénom, l'âge et la description."))
        if photos is not None and (
            not isinstance(photos, list)
            or not 1 <= len(photos) <= 3
            or any(not isinstance(photo, str) or not photo.startswith("data:image/jpeg;base64,") for photo in photos)
        ):
            return fail(ValueError("Le profil doit avoir entre 1 et 3 photos JPEG valides."))
        photo_paths = [upload_photo(photo, str(user["id"])) for photo in photos] if photos is not None else user["photo_paths"]
        old_paths = user["photo_paths"]
        with get_db() as db:
            row = db.execute("""
                UPDATE public.profiles SET first_name=%s, age=%s, bio=%s, photo_paths=%s
                WHERE id=%s
                RETURNING id,email,first_name,age,bio,photo_paths
            """, (first_name, age, bio, photo_paths, user["id"])).fetchone()
        if not row:
            return fail(ValueError("Profil introuvable."), 404)
        if photos is not None:
            for path in old_paths:
                try:
                    delete_photo(path)
                except (RuntimeError, requests.RequestException):
                    app.logger.exception("Could not remove a replaced profile photo")
        return jsonify(as_json(row))
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error, requests.RequestException) as error:
        app.logger.exception("Profile update failed")
        return fail(error, 503)


@app.post("/api/login")
def login():
    email = str((request.get_json(force=True) or {}).get("email", "")).strip().lower()
    if "@" not in email or len(email) > 254:
        return fail(ValueError("Adresse e-mail invalide."))
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT id,email,first_name,age,bio,photo_paths FROM public.profiles WHERE email=%s",
                (email,),
            ).fetchone()
        if not row:
            return fail(ValueError("Aucun compte avec cette adresse. Crée un profil."), 404)
        session.clear()
        session["email"] = email
        session.permanent = True
        return jsonify(as_json(row))
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Login failed")
        return fail(error, 503)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.delete("/api/account")
def delete_account():
    try:
        user = require_user()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return fail(ValueError("Confirmation invalide."))
        confirmation = str(data.get("email", "")).strip().lower()
        if confirmation != user["email"].lower():
            return fail(ValueError("L’adresse e-mail ne correspond pas à ton compte."))
        photo_paths = user["photo_paths"]
        with get_db() as db:
            deleted = db.execute(
                "DELETE FROM public.profiles WHERE id=%s RETURNING id",
                (user["id"],),
            ).fetchone()
        if not deleted:
            return fail(ValueError("Compte introuvable."), 404)
        session.clear()
        for path in photo_paths:
            try:
                delete_photo(path)
            except (RuntimeError, requests.RequestException):
                app.logger.exception("Could not remove a deleted account photo")
        return jsonify({"ok": True})
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Account deletion failed")
        return fail(error, 503)


def valid_push_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        return False
    try:
        parsed = urlsplit(endpoint)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    allowed_host = hostname in {
        "fcm.googleapis.com",
        "web.push.apple.com",
        "updates.push.services.mozilla.com",
    } or hostname.endswith(".notify.windows.com") or hostname.endswith(".push.services.mozilla.com")
    return parsed.scheme == "https" and allowed_host and parsed.username is None and parsed.password is None and port in (None, 443)


def valid_subscription_key(value, expected_size):
    if not isinstance(value, str) or not value or len(value) > 200:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error):
        return False
    return len(decoded) == expected_size


@app.get("/api/push/config")
def push_config():
    try:
        user = require_user()
        configured = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT)
        return jsonify({"configured": configured, "public_key": VAPID_PUBLIC_KEY if configured else ""})
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Push configuration lookup failed")
        return fail(error, 503)


@app.post("/api/push/status")
def push_subscription_status():
    try:
        user = require_user()
        endpoint = (request.get_json(force=True) or {}).get("endpoint")
        if not valid_push_endpoint(endpoint):
            return fail(ValueError("Abonnement de notification invalide."))
        with get_db() as db:
            subscribed = bool(db.execute(
                "SELECT 1 FROM public.push_subscriptions WHERE user_id=%s AND endpoint=%s",
                (user["id"], endpoint),
            ).fetchone())
        return jsonify({"subscribed": subscribed})
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Push subscription status lookup failed")
        return fail(error, 503)


@app.post("/api/push/subscription")
def save_push_subscription():
    try:
        user = require_user()
        data = request.get_json(force=True) or {}
        endpoint = data.get("endpoint")
        keys = data.get("keys")
        if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY or not VAPID_SUBJECT:
            return fail(ValueError("Les notifications ne sont pas configurées sur ce serveur."), 503)
        if not valid_push_endpoint(endpoint):
            return fail(ValueError("Service de notification non pris en charge."))
        if not isinstance(keys, dict) or not valid_subscription_key(keys.get("p256dh"), 65) or not valid_subscription_key(keys.get("auth"), 16):
            return fail(ValueError("Abonnement de notification invalide."))
        with get_db() as db:
            db.execute("""
                INSERT INTO public.push_subscriptions(endpoint,user_id,p256dh,auth)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,p256dh=excluded.p256dh,auth=excluded.auth
            """, (endpoint, user["id"], keys["p256dh"], keys["auth"]))
        return jsonify({"ok": True})
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Push subscription save failed")
        return fail(error, 503)


@app.delete("/api/push/subscription")
def delete_push_subscription():
    try:
        user = require_user()
        endpoint = (request.get_json(force=True) or {}).get("endpoint")
        if not valid_push_endpoint(endpoint):
            return fail(ValueError("Abonnement de notification invalide."))
        with get_db() as db:
            db.execute("DELETE FROM public.push_subscriptions WHERE endpoint=%s AND user_id=%s", (endpoint, user["id"]))
        return jsonify({"ok": True})
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Push subscription removal failed")
        return fail(error, 503)


@app.get("/api/profiles")
def profiles():
    try:
        user = require_user()
        with get_db() as db:
            rows = db.execute("""
                SELECT p.id,p.first_name,p.age,p.bio,p.photo_paths FROM public.profiles p
                WHERE p.id<>%s AND NOT EXISTS (
                    SELECT 1 FROM public.swipes s WHERE s.actor_id=%s AND s.target_id=p.id
                )
                ORDER BY p.created_at DESC
            """, (user["id"], user["id"])).fetchall()
        return jsonify(as_json_many(rows))
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Profile list failed")
        return fail(error, 503)


@app.post("/api/swipes")
def swipe():
    try:
        user = require_user()
        data = request.get_json(force=True)
        target = str(data.get("target_id", ""))
        liked = bool(data.get("liked"))
        if target == str(user["id"]):
            return fail(ValueError("Tu ne peux pas swiper ton profil."))
        matched_profiles = {}
        new_match = False
        new_like = False
        with get_db() as db:
            swipe_change = db.execute("""INSERT INTO public.swipes AS existing_swipe(actor_id,target_id,liked) VALUES(%s,%s,%s)
                ON CONFLICT(actor_id,target_id) DO UPDATE SET liked=excluded.liked
                WHERE existing_swipe.liked IS DISTINCT FROM excluded.liked
                RETURNING liked""", (user["id"], target, liked)).fetchone()
            new_like = liked and swipe_change is not None
            reciprocal = db.execute("SELECT 1 FROM public.swipes WHERE actor_id=%s AND target_id=%s AND liked=true", (target, user["id"])).fetchone() if liked else None
            match_id = None
            if reciprocal:
                row = db.execute("""INSERT INTO public.matches(user_a,user_b)
                    VALUES(LEAST(%s::uuid,%s::uuid),GREATEST(%s::uuid,%s::uuid))
                    ON CONFLICT(user_a,user_b) DO NOTHING RETURNING id""",
                    (user["id"], target, user["id"], target)).fetchone()
                new_match = row is not None
                if row:
                    match_id = str(row["id"])
                else:
                    existing = db.execute(
                        "SELECT id FROM public.matches WHERE user_a=LEAST(%s::uuid,%s::uuid) AND user_b=GREATEST(%s::uuid,%s::uuid)",
                        (user["id"], target, user["id"], target),
                    ).fetchone()
                    match_id = str(existing["id"])
                if new_match:
                    rows = db.execute(
                        "SELECT id,first_name FROM public.profiles WHERE id IN (%s,%s)",
                        (user["id"], target),
                    ).fetchall()
                    matched_profiles = {str(profile["id"]): profile["first_name"] for profile in rows}
        if new_match:
            send_push(user["id"], "Nouveau match !", f"Tu as matché avec {matched_profiles.get(target, 'une personne')}.", match_id)
            send_push(target, "Nouveau match !", f"Tu as matché avec {matched_profiles.get(str(user['id']), 'une personne')}.", match_id)
        elif new_like:
            send_push(target, "Nouveau like", f"{user['first_name']} aime ton profil.", url="/?view=likes")
        return jsonify({"match": match_id})
    except PermissionError as error:
        return fail(error, 401)
    except psycopg2.Error as error:
        app.logger.exception("Swipe failed")
        return fail(error, 400)


@app.get("/api/likes")
def likes():
    try:
        user = require_user()
        with get_db() as db:
            rows = db.execute("""
                SELECT p.id,p.first_name,p.age,p.bio,p.photo_paths
                FROM public.swipes s
                JOIN public.profiles p ON p.id=s.actor_id
                WHERE s.target_id=%s AND s.liked=true
                  AND NOT EXISTS (
                    SELECT 1 FROM public.swipes mine
                    WHERE mine.actor_id=%s AND mine.target_id=s.actor_id
                  )
                ORDER BY s.created_at DESC
            """, (user["id"], user["id"])).fetchall()
        return jsonify(as_json_many(rows))
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Incoming likes lookup failed")
        return fail(error, 503)


@app.get("/api/matches")
def matches():
    try:
        user = require_user()
        with get_db() as db:
            rows = db.execute("""
                SELECT m.id,m.user_a,m.user_b,m.created_at,p.id AS profile_id,p.first_name,p.age,p.bio,p.photo_paths,
                (SELECT body FROM public.messages x WHERE x.match_id=m.id ORDER BY x.created_at DESC LIMIT 1) AS last_message
                FROM public.matches m JOIN public.profiles p ON p.id=CASE WHEN m.user_a=%s THEN m.user_b ELSE m.user_a END
                WHERE m.user_a=%s OR m.user_b=%s ORDER BY m.created_at DESC
            """, (user["id"], user["id"], user["id"])).fetchall()
        result = as_json_many(rows)
        for item in result:
            item["profile"] = {key: item.pop(key) for key in ("profile_id", "first_name", "age", "bio", "photo_paths")}
            item["profile"]["id"] = item.pop("profile_id", None)
        return jsonify(result)
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Match list failed")
        return fail(error, 503)


def match_member(db, match_id, user_id):
    return bool(db.execute("SELECT 1 FROM public.matches WHERE id=%s AND (user_a=%s OR user_b=%s)", (match_id, user_id, user_id)).fetchone())


@app.get("/api/matches/<uuid:match_id>/messages")
def messages(match_id):
    try:
        user = require_user()
        with get_db() as db:
            if not match_member(db, str(match_id), user["id"]):
                return fail(PermissionError("Conversation inaccessible."), 403)
            rows = db.execute("SELECT id,sender_id,body,created_at FROM public.messages WHERE match_id=%s ORDER BY created_at", (str(match_id),)).fetchall()
        return jsonify([as_json(row) for row in rows])
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Message lookup failed")
        return fail(error, 503)


@app.post("/api/matches/<uuid:match_id>/messages")
def send_message(match_id):
    try:
        user = require_user()
        body = str((request.get_json(force=True) or {}).get("body", "")).strip()
        if not 1 <= len(body) <= 2000:
            return fail(ValueError("Message trop court ou trop long."))
        with get_db() as db:
            if not match_member(db, str(match_id), user["id"]):
                return fail(PermissionError("Conversation inaccessible."), 403)
            recipient = db.execute(
                "SELECT CASE WHEN user_a=%s THEN user_b ELSE user_a END AS id FROM public.matches WHERE id=%s",
                (user["id"], str(match_id)),
            ).fetchone()["id"]
            row = db.execute("INSERT INTO public.messages(match_id,sender_id,body) VALUES(%s,%s,%s) RETURNING id,sender_id,body,created_at", (str(match_id), user["id"], body)).fetchone()
        send_push(str(recipient), "Nouveau message", f"{user['first_name']} : {body[:500]}", str(match_id))
        return jsonify(as_json(row)), 201
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Message insert failed")
        return fail(error, 503)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
