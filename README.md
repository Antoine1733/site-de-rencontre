# Site de rencontre

## Configuration

Dans Supabase, récupère l'URL PostgreSQL depuis **Project Settings > Database** et remplace le placeholder par le vrai mot de passe. Pour Render, utilise si besoin l'URL **Session pooler** fournie par Supabase.

Aucune clé `anon` n'est utilisée. `init_db()` dans `app.py` crée les tables directement dans Supabase au démarrage.

## Lancer en local

Dans PowerShell, depuis ce dossier :

```powershell
Copy-Item .env.example .env
```

Remplace les valeurs d'exemple dans `.env`, puis lance :

```powershell
python app.py
```

Ouvre http://localhost:8000. Les profils, swipes, matchs, messages et photos sont stockés dans PostgreSQL Supabase.

## Déployer sur Vercel

Importe le dépôt GitHub `Antoine1733/site-de-rencontre` dans Vercel. Le fichier `vercel.json` configure le point d'entrée Flask.

Dans **Project Settings > Environment Variables**, ajoute `DATABASE_URL` avec l'URI PostgreSQL Supabase (utilise l'URL Session Pooler si nécessaire) et `FLASK_SECRET_KEY` avec une valeur aléatoire longue. Ne pousse jamais `.env` sur GitHub. Après les avoir définies, redéploie le projet.

`api/index.py` expose l'application Flask à Vercel. `python app.py` reste la commande pour la lancer en local.

La connexion actuelle identifie un profil par son adresse e-mail, comme dans le code fourni ; l'adresse n'est pas vérifiée.
