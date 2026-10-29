from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template_string, request, session

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
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "local-only-change-this")
app.config.update(MAX_CONTENT_LENGTH=10 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=(os.getenv("APP_ENV") == "production" or os.getenv("VERCEL") == "1"))


class PgConnection:
    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL manque dans le fichier .env")
        self.connection = psycopg2.connect(DATABASE_URL)
        self.cursor = self.connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, query: str, params: tuple[Any, ...] = ()):
        self.cursor.execute(query, params)
        return self.cursor

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        try:
            self.connection.rollback() if kind else self.connection.commit()
        finally:
            self.cursor.close()
            self.connection.close()


def get_db() -> PgConnection:
    return PgConnection()


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


def as_json(row):
    result = dict(row)
    for key, value in result.items():
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        elif hasattr(value, "hex") and not isinstance(value, str):
            result[key] = str(value)
    return result


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


HTML = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rencontres</title>
<style>
:root{font-family:Arial,sans-serif;color:#25251f;background:#f6f5f0}*{box-sizing:border-box}body{margin:0;min-width:320px}button,input,textarea{font:inherit}button{cursor:pointer;color:inherit}.hide{display:none!important}.shell{max-width:900px;min-height:100vh;margin:auto;padding:0 22px}.top{height:60px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #deded5}.top strong{font-size:15px}.top span{color:#77786e;font-size:12px}.content{max-width:650px;margin:auto;padding:26px 0 90px}.heading{display:flex;align-items:end;justify-content:space-between;margin-bottom:18px}.heading h1,.heading h2{margin:0;font-size:25px;font-weight:600}.heading p{margin:6px 0 0;color:#77786e;font-size:13px}.form{max-width:420px;margin:7vh auto}.form h1{margin:0 0 8px;font-size:27px}.form>p{color:#77786e;font-size:13px}.field{display:grid;gap:6px;margin:13px 0}.field label{font-size:12px;font-weight:600}.field small{color:#77786e;font-weight:400}.field input,.field textarea,.send input{width:100%;padding:11px;border:1px solid #d7d7ce;border-radius:3px;background:white}.field input[type=file]{padding:9px}.field textarea{resize:vertical}.primary,.send button{min-height:44px;padding:0 16px;border:0;border-radius:3px;background:#d65e49;color:white;font-weight:600}.primary{width:100%;margin-top:8px}.preview{display:flex;gap:7px;flex-wrap:wrap}.preview img{width:56px;height:56px;object-fit:cover;border-radius:3px}.tabs{display:flex;gap:8px;margin:18px 0;border-bottom:1px solid #deded5}.tab{padding:10px 12px;border:0;border-bottom:2px solid transparent;background:none;color:#77786e}.tab.active{border-bottom-color:#d65e49;color:#25251f}.card{position:relative;display:flex;align-items:end;height:min(60vh,570px);min-height:350px;overflow:hidden;border-radius:4px;background:#e8e8e2}.card>img{position:absolute;width:100%;height:100%;object-fit:cover}.card-info{position:relative;width:100%;padding:22px;color:white;background:linear-gradient(transparent,#000b)}.card-info h2{margin:0;font-size:25px}.card-info p{margin:8px 0 0;font-size:14px;line-height:1.45}.photo-hit{position:absolute;inset:0;display:flex}.photo-hit button{width:50%;border:0;background:transparent}.actions{display:flex;justify-content:center;gap:18px;margin-top:16px}.actions button{width:54px;height:54px;border:1px solid #d5d5cb;border-radius:50%;background:white;font-size:21px}.actions .like{border-color:#d65e49;background:#d65e49;color:white}.hint{text-align:center;color:#88897e;font-size:11px}.matches{display:flex;gap:7px;overflow:auto;padding:4px 0 14px}.match{flex:none;padding:9px 13px;border:1px solid #deded5;border-radius:3px;background:white}.match.active{border-color:#d65e49}.chat{max-height:52vh;min-height:220px;overflow:auto;padding:12px 0}.bubble{width:fit-content;max-width:80%;margin:7px 0;padding:10px 13px;border-radius:5px;background:#e5e5dd;overflow-wrap:anywhere;font-size:14px}.bubble.mine{margin-left:auto;background:#dfe8ce}.bubble time{display:block;margin-top:5px;color:#6d6e65;font-size:10px}.send{display:flex;gap:8px}.send button{flex:none}.empty{text-align:center;padding:30px 12px;color:#77786e;font-size:13px}.notice{position:fixed;z-index:5;left:50%;bottom:22px;max-width:calc(100% - 30px);padding:11px 15px;border-radius:3px;background:#25251f;color:white;font-size:12px;transform:translateX(-50%)}
@media(max-width:600px){.shell{padding:0 14px}.content{padding-top:20px}.heading h1{font-size:23px}.card{height:57vh;min-height:320px}.tabs{position:fixed;z-index:3;inset:auto 0 0;justify-content:space-around;margin:0;padding:6px 8px max(6px,env(safe-area-inset-bottom));border-top:1px solid #deded5;background:#f6f5f0}.tab{flex:1;padding:11px 4px;font-size:12px}.form{margin:8vh auto}}
.matches{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:7px;max-height:260px;overflow:auto;margin-bottom:12px}.match{display:flex;align-items:center;gap:11px;width:100%;min-width:0;padding:10px;border:1px solid #e2e1d9;border-radius:4px;background:#fffefa;text-align:left;transition:border-color .15s,background .15s}.match:hover,.match.active{border-color:#d65e49;background:#fff}.match-photo{width:48px;height:48px;flex:none;object-fit:cover;border-radius:50%;background:#e8e8e2}.match-info{display:grid;gap:4px;min-width:0}.match-name,.match-last{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.match-name{font-size:13px;font-weight:600}.match-last{color:#77786e;font-size:11px}
@media(max-width:600px){.matches{grid-template-columns:1fr;max-height:210px;gap:5px}.match{padding:8px}.match-photo{width:44px;height:44px}}
</style></head><body><main class="shell"><header class="top"><strong>Rencontres</strong><span id="account"></span><button id="top-logout" class="hide" type="button">Se déconnecter</button></header>
<section id="signup" class="form"><h1>Créer un profil</h1><p>Inscription réservée aux personnes de 18 ans et plus.</p><form id="signup-form"><div class="field"><label>Prénom</label><input name="first_name" maxlength="40" required></div><div class="field"><label>Adresse e-mail</label><input name="email" type="email" required></div><div class="field"><label>Âge</label><input name="age" type="number" min="18" max="100" required></div><div class="field"><label>Description</label><textarea name="bio" rows="3" maxlength="500" required></textarea></div><div class="field"><label>Photos <small>1 à 3 photos</small></label><input id="photos" type="file" accept="image/jpeg,image/png,image/webp" multiple required><div class="preview" id="preview"></div></div><button class="primary">Continuer</button></form></section>
<section id="app-view" class="content hide"><nav class="tabs"><button class="tab active" data-view="discover">Découvrir</button><button class="tab" data-view="messages">Messages <span id="match-count"></span></button><button class="tab" data-view="profile">Profil</button></nav><div id="discover-view"><div class="heading"><div><h1>Profils</h1><p>Choisis les profils qui t'intéressent.</p></div><span id="profile-count"></span></div><div id="card-area"></div><div id="actions" class="actions"><button id="pass" aria-label="Passer">&#10005;</button><button id="like" class="like" aria-label="Aimer">&#9829;</button></div><p class="hint">Glisse ou utilise les boutons</p></div><div id="messages-view" class="hide"><div class="heading"><h1>Messages</h1></div><div id="matches" class="matches"></div><div id="conversation"><p class="empty">Choisis un match.</p></div></div><div id="profile-view" class="hide"><div class="heading"><h1>Mon profil</h1></div><div id="my-profile"></div><button id="logout" class="primary">Se déconnecter</button></div></section></main><div id="notice" class="notice hide"></div>
<script>
const $=q=>document.querySelector(q),s={user:null,profiles:[],matches:[],likes:[],active:null,photo:0,messages:[],poll:null};
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function note(t){const e=$('#notice');e.textContent=t;e.classList.remove('hide');setTimeout(()=>e.classList.add('hide'),3000)}
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}}),d=await r.json();if(!r.ok)throw Error(d.error||'Erreur serveur.');return d}
async function photoData(file){const bitmap=await createImageBitmap(file),scale=Math.min(1,1400/Math.max(bitmap.width,bitmap.height)),canvas=document.createElement('canvas');canvas.width=Math.round(bitmap.width*scale);canvas.height=Math.round(bitmap.height*scale);canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();return canvas.toDataURL('image/jpeg',.82)}
async function enter(user){s.user=user;$('#signup').classList.add('hide');$('#app-view').classList.remove('hide');$('#account').textContent=user.first_name;$('#top-logout').classList.remove('hide');await Promise.all([loadProfiles(),loadMatches()])}
async function loadProfiles(){s.profiles=await api('/api/profiles');s.photo=0;renderCard()}
async function loadLikes(){s.likes=await api('/api/likes');$('#like-count').textContent=s.likes.length?`(${s.likes.length})`:'';$('#likes-list').innerHTML=s.likes.length?s.likes.map(p=>`<article style="display:grid;grid-template-columns:78px 1fr auto;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid #deded5"><img src="${esc(p.photo_paths[0])}" alt="Photo de ${esc(p.first_name)}" style="width:78px;height:84px;object-fit:cover;border-radius:3px"><div><strong>${esc(p.first_name)}, ${p.age}</strong><p style="margin:6px 0;color:#77786e;font-size:12px">${esc(p.bio)}</p></div><div style="display:grid;gap:6px"><button class="like-reply" data-id="${p.id}" data-liked="1">Aimer</button><button class="like-reply" data-id="${p.id}" data-liked="0">Passer</button></div></article>`).join(''):'<p class="empty">Aucun like reçu pour le moment.</p>'}
function renderCard(){const p=s.profiles[0];$('#profile-count').textContent=p?`${s.profiles.length} profil${s.profiles.length===1?'':'s'}`:'';$('#actions').classList.toggle('hide',!p);if(!p){$('#card-area').innerHTML='<p class="empty">Aucun profil disponible.</p>';return}$('#card-area').innerHTML=`<article class="card" id="card"><img src="${p.photo_paths[s.photo%p.photo_paths.length]}" alt="Photo de ${esc(p.first_name)}">${p.photo_paths.length>1?'<div class="photo-hit"><button data-step="-1" aria-label="Photo précédente"></button><button data-step="1" aria-label="Photo suivante"></button></div>':''}<div class="card-info"><h2>${esc(p.first_name)}, ${p.age}</h2><p>${esc(p.bio)}</p></div></article>`;let x=0;$('#card').ontouchstart=e=>x=e.changedTouches[0].clientX;$('#card').ontouchend=e=>{const dx=e.changedTouches[0].clientX-x;if(Math.abs(dx)>70)swipe(dx>0)}}
async function swipe(liked){const p=s.profiles[0];if(!p)return;try{const r=await api('/api/swipes',{method:'POST',body:JSON.stringify({target_id:p.id,liked})});s.profiles.shift();renderCard();if(r.match){note('Nouveau match.');await loadMatches()}}catch(e){note(e.message)}}
async function loadMatches(){s.matches=await api('/api/matches');$('#match-count').textContent=s.matches.length?`(${s.matches.length})`:'';$('#matches').innerHTML=s.matches.length?s.matches.map(m=>`<button class="match ${s.active?.id===m.id?'active':''}" data-id="${m.id}"><img class="match-photo" src="${esc(m.profile.photo_paths?.[0]||'')}" alt=""><span class="match-info"><strong class="match-name">${esc(m.profile.first_name)}</strong><span class="match-last">${esc(m.last_message||'Nouveau match')}</span></span></button>`).join(''):'<p class="empty">Aucun match.</p>';if(s.active){s.active=s.matches.find(m=>m.id===s.active.id)||null;if(s.active)await loadMessages(false)}}
async function openMatch(id){s.active=s.matches.find(m=>m.id===id);if(!s.active)return;await loadMessages(true);if(s.poll)clearInterval(s.poll);s.poll=setInterval(()=>loadMessages(false),3000);$('#matches').querySelectorAll('.match').forEach(b=>b.classList.toggle('active',b.dataset.id===id))}
function messageBubbles(){return s.messages.map(m=>{const mine=String(m.sender_id).toLowerCase()===String(s.user.id).toLowerCase();return `<div class="bubble ${mine?'mine':'received'}" style="align-self:${mine?'flex-end':'flex-start'};margin:0">${esc(m.body)}<time>${new Date(m.created_at).toLocaleTimeString('fr-FR',{hour:'2-digit',minute:'2-digit'})}</time></div>`}).join('')}
async function loadMessages(scroll){if(!s.active)return;const matchId=s.active.id,container=$('#conversation'),sameMatch=container.dataset.matchId===matchId,existingChat=sameMatch?$('#chat'):null,near=existingChat&&existingChat.scrollHeight-existingChat.scrollTop-existingChat.clientHeight<45;try{const messages=await api(`/api/matches/${matchId}/messages`);if(!s.active||s.active.id!==matchId)return;s.messages=messages;if(existingChat){existingChat.innerHTML=messageBubbles();if(scroll||near)existingChat.scrollTop=existingChat.scrollHeight}else drawMessages(scroll)}catch(e){note(e.message)}}
function drawMessages(scroll){const p=s.active.profile,photo=p.photo_paths?.[0],container=$('#conversation');container.dataset.matchId=s.active.id;container.innerHTML=`<div style="display:flex;align-items:center;gap:11px;margin:0 0 12px;padding:4px 0"><button id="back-matches" type="button" style="padding:8px 10px;border:1px solid #deded5;border-radius:3px;background:transparent;color:#55564e;white-space:nowrap">← Tous les matchs</button><img src="${esc(photo||'')}" alt="Photo de ${esc(p.first_name)}" style="width:44px;height:44px;flex:none;object-fit:cover;border-radius:50%;background:#e8e8e2"><h2 style="margin:0;font-size:18px;font-weight:600">${esc(p.first_name)}</h2></div><div id="chat" class="chat" style="display:flex;flex-direction:column;gap:8px">${messageBubbles()}</div><form id="send-form" class="send"><input id="message-input" maxlength="2000" placeholder="Votre message" required autocomplete="off"><button>Envoyer</button></form>`;if(scroll)$('#chat').scrollTop=$('#chat').scrollHeight}
function backToMatches(){if(s.poll)clearInterval(s.poll);s.poll=null;s.active=null;s.messages=[];delete $('#conversation').dataset.matchId;$('#conversation').innerHTML='<p class="empty">Choisis un match.</p>';loadMatches()}
async function send(e){e.preventDefault();const body=$('#message-input').value.trim();if(!body)return;try{await api(`/api/matches/${s.active.id}/messages`,{method:'POST',body:JSON.stringify({body})});$('#message-input').value='';await loadMessages(true)}catch(err){note(err.message)}}
function showView(v){['discover','likes','messages','profile'].forEach(x=>$('#'+x+'-view').classList.toggle('hide',x!==v));document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.view===v));if(v==='likes')loadLikes();if(v==='messages')loadMatches();if(v==='profile')renderProfile()}
const likesTab=document.createElement('button');likesTab.className='tab';likesTab.dataset.view='likes';likesTab.innerHTML='Likes <span id="like-count"></span>';likesTab.onclick=()=>showView('likes');document.querySelector('.tabs').append(likesTab);const likesView=document.createElement('div');likesView.id='likes-view';likesView.className='hide';likesView.innerHTML='<div class="heading"><div><h1>Likes reçus</h1><p>Les personnes qui t’ont aimé.</p></div></div><div id="likes-list"></div>';$('#app-view').append(likesView);
$('#likes-list').onclick=async e=>{const button=e.target.closest('.like-reply');if(!button)return;try{const result=await api('/api/swipes',{method:'POST',body:JSON.stringify({target_id:button.dataset.id,liked:button.dataset.liked==='1'})});if(result.match)note('Vous avez un match !');await Promise.all([loadLikes(),loadMatches()])}catch(err){note(err.message)}};
function renderProfile(){const p=s.user;$('#my-profile').innerHTML=`<div class="field"><strong>${esc(p.first_name)}, ${p.age}</strong><p>${esc(p.bio)}</p><div class="preview">${p.photo_paths.map(x=>`<img src="${x}" alt="Photo">`).join('')}</div><p>${esc(p.email)}</p></div><button id="edit-profile" class="primary">Modifier le profil</button><form id="edit-form" class="hide"><div class="field"><label>Prénom</label><input name="first_name" maxlength="40" value="${esc(p.first_name)}" required></div><div class="field"><label>Âge</label><input name="age" type="number" min="18" max="100" value="${p.age}" required></div><div class="field"><label>Description</label><textarea name="bio" rows="3" maxlength="500" required>${esc(p.bio)}</textarea></div><div class="field"><label>Remplacer les photos <small>Facultatif, 1 à 3 photos</small></label><input name="photos" type="file" accept="image/jpeg,image/png,image/webp" multiple></div><button class="primary">Enregistrer</button></form>`;$('#edit-profile').onclick=()=>$('#edit-form').classList.toggle('hide');$('#edit-form').onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,files=[...f.photos.files];if(files.length>3||files.some(file=>file.size>8*1024*1024)){note('Choisis au maximum 3 photos de 8 Mo chacune.');return}try{const updated=await api('/api/profile',{method:'PUT',body:JSON.stringify({first_name:f.first_name.value.trim(),age:Number(f.age.value),bio:f.bio.value.trim(),photo_paths:files.length?await Promise.all(files.map(photoData)):p.photo_paths})});s.user=updated;$('#account').textContent=updated.first_name;await loadProfiles();renderProfile();note('Profil enregistré.')}catch(err){note(err.message)}}}
const signupForm=$('#signup-form'),authSwitch=document.createElement('div'),loginForm=document.createElement('form');authSwitch.style.cssText='display:flex;gap:8px;margin:18px 0;border-bottom:1px solid #deded5';authSwitch.innerHTML='<button type="button" data-mode="signup" style="flex:1;padding:10px;border:0;border-bottom:2px solid #d65e49;background:none">Créer un compte</button><button type="button" data-mode="login" style="flex:1;padding:10px;border:0;border-bottom:2px solid transparent;background:none;color:#77786e">Se connecter</button>';loginForm.id='login-form';loginForm.className='hide';loginForm.innerHTML='<div class="field"><label>Adresse e-mail</label><input name="email" type="email" required></div><button class="primary">Se connecter</button>';signupForm.parentNode.insertBefore(authSwitch,signupForm);signupForm.parentNode.append(loginForm);
function authMode(mode){const creating=mode==='signup';signupForm.classList.toggle('hide',!creating);loginForm.classList.toggle('hide',creating);$('#signup h1').textContent=creating?'Créer un profil':'Se connecter';$('#signup > p').textContent=creating?'Inscription réservée aux personnes de 18 ans et plus.':'Entre l’adresse e-mail de ton compte.';authSwitch.querySelectorAll('button').forEach(button=>{const active=button.dataset.mode===mode;button.style.borderBottomColor=active?'#d65e49':'transparent';button.style.color=active?'#25251f':'#77786e'})}
authSwitch.onclick=e=>{const button=e.target.closest('[data-mode]');if(button)authMode(button.dataset.mode)};loginForm.onsubmit=async e=>{e.preventDefault();try{const user=await api('/api/login',{method:'POST',body:JSON.stringify({email:loginForm.elements.email.value.trim()})});await enter(user)}catch(err){note(err.message)}};
signupForm.onsubmit=async e=>{e.preventDefault();const f=e.currentTarget,files=[...$('#photos').files];if(files.length<1||files.length>3){note('Ajoute entre 1 et 3 photos.');return}if(files.some(x=>x.size>8*1024*1024)){note('Chaque photo doit faire 8 Mo maximum.');return}try{const user=await api('/api/signup',{method:'POST',body:JSON.stringify({first_name:f.first_name.value.trim(),email:f.email.value.trim(),age:Number(f.age.value),bio:f.bio.value.trim(),photo_paths:await Promise.all(files.map(photoData))})});await enter(user)}catch(err){note(err.message)}};
$('#photos').onchange=async()=>{try{$('#preview').innerHTML=(await Promise.all([...$('#photos').files].slice(0,3).map(photoData))).map(x=>`<img src="${x}" alt="Aperçu">`).join('')}catch{note('Photo invalide.')}};document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>showView(x.dataset.view));$('#pass').onclick=()=>swipe(false);$('#like').onclick=()=>swipe(true);$('#card-area').onclick=e=>{const b=e.target.closest('[data-step]');if(b){s.photo=(s.photo+Number(b.dataset.step)+s.profiles[0].photo_paths.length)%s.profiles[0].photo_paths.length;renderCard()}};$('#matches').onclick=e=>{const b=e.target.closest('[data-id]');if(b)openMatch(b.dataset.id)};$('#conversation').onclick=e=>{if(e.target.closest('#back-matches'))backToMatches()};$('#conversation').onsubmit=e=>{if(e.target.id==='send-form')send(e)};const logout=async()=>{await api('/api/logout',{method:'POST'});location.reload()};$('#logout').onclick=logout;$('#top-logout').onclick=logout;api('/api/me').then(u=>{if(u)enter(u)}).catch(()=>{});
</script></body></html>'''


@app.get("/")
def index():
    return render_template_string(HTML)


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
    try:
        with get_db() as db:
            row = db.execute("""
                INSERT INTO public.profiles(email, first_name, age, bio, photo_paths)
                VALUES(%s,%s,%s,%s,%s)
                RETURNING id,email,first_name,age,bio,photo_paths
            """, (email, name, age, bio, photos)).fetchone()
        session.clear()
        session["email"] = email
        return jsonify(as_json(row))
    except psycopg2.errors.UniqueViolation:
        return fail(ValueError("Cette adresse a déjà un compte. Choisis l'onglet Se connecter."), 409)
    except (RuntimeError, psycopg2.Error) as error:
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
        photo_paths = data.get("photo_paths", user["photo_paths"])
        if not 1 <= len(first_name) <= 40 or not 18 <= age <= 100 or not 1 <= len(bio) <= 500:
            return fail(ValueError("Vérifie le prénom, l'âge et la description."))
        if not isinstance(photo_paths, list) or not 1 <= len(photo_paths) <= 3:
            return fail(ValueError("Le profil doit avoir entre 1 et 3 photos."))
        if any(not isinstance(photo, str) or not photo.startswith("data:image/jpeg;base64,") for photo in photo_paths):
            return fail(ValueError("Format de photo invalide."))
        with get_db() as db:
            row = db.execute("""
                UPDATE public.profiles SET first_name=%s, age=%s, bio=%s, photo_paths=%s
                WHERE id=%s
                RETURNING id,email,first_name,age,bio,photo_paths
            """, (first_name, age, bio, photo_paths, user["id"])).fetchone()
        if not row:
            return fail(ValueError("Profil introuvable."), 404)
        return jsonify(as_json(row))
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
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
        return jsonify(as_json(row))
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Login failed")
        return fail(error, 503)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/profiles")
def profiles():
    try:
        user = require_user()
        with get_db() as db:
            rows = db.execute("""
                SELECT p.id,p.first_name,p.age,p.bio,p.photo_paths FROM public.profiles p
                WHERE p.id<>%s AND NOT EXISTS (
                    SELECT 1 FROM public.swipes s WHERE s.actor_id=%s AND s.target_id=p.id
                ) ORDER BY p.created_at DESC
            """, (user["id"], user["id"])).fetchall()
        return jsonify([as_json(row) for row in rows])
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
        with get_db() as db:
            db.execute("""INSERT INTO public.swipes(actor_id,target_id,liked) VALUES(%s,%s,%s)
                ON CONFLICT(actor_id,target_id) DO UPDATE SET liked=excluded.liked""", (user["id"], target, liked))
            reciprocal = db.execute("SELECT 1 FROM public.swipes WHERE actor_id=%s AND target_id=%s AND liked=true", (target, user["id"])).fetchone() if liked else None
            match_id = None
            if reciprocal:
                row = db.execute("""INSERT INTO public.matches(user_a,user_b)
                    VALUES(LEAST(%s::uuid,%s::uuid),GREATEST(%s::uuid,%s::uuid))
                    ON CONFLICT(user_a,user_b) DO UPDATE SET user_a=excluded.user_a RETURNING id""",
                    (user["id"], target, user["id"], target)).fetchone()
                match_id = str(row["id"])
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
        return jsonify([as_json(row) for row in rows])
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
        result = []
        for row in rows:
            item = as_json(row)
            item["profile"] = {key: item.pop(key) for key in ("profile_id", "first_name", "age", "bio", "photo_paths")}
            item["profile"]["id"] = item.pop("profile_id", None)
            result.append(item)
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
            row = db.execute("INSERT INTO public.messages(match_id,sender_id,body) VALUES(%s,%s,%s) RETURNING id,sender_id,body,created_at", (str(match_id), user["id"], body)).fetchone()
        return jsonify(as_json(row)), 201
    except PermissionError as error:
        return fail(error, 401)
    except (RuntimeError, psycopg2.Error) as error:
        app.logger.exception("Message insert failed")
        return fail(error, 503)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
