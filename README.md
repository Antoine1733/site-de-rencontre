# Site de rencontre

## Configuration

Dans Supabase, récupère l'URL PostgreSQL depuis **Project Settings > Database** et remplace le placeholder par le vrai mot de passe. Pour Render, utilise si besoin l'URL **Session pooler** fournie par Supabase.

Aucune clé `anon` n'est utilisée. `init_db()` dans `app.py` crée les tables directement dans Supabase au démarrage local. Sur Vercel, initialise les tables une fois depuis ton ordinateur, après avoir configuré `DATABASE_URL` dans `.env` :

```powershell
python -c "from app import init_db; init_db()"
```

L'application Vercel ne se connecte pas à PostgreSQL pendant son import ; ses fonctions peuvent ainsi démarrer même si la base est momentanément indisponible.

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

Importe le dépôt GitHub `Antoine1733/site-de-rencontre` dans Vercel. Le fichier `vercel.json` configure le point d'entrée Flask. Avant la première utilisation, initialise les tables depuis ton ordinateur avec la commande de configuration ci-dessus.

Dans **Project Settings > Environment Variables**, ajoute `DATABASE_URL` avec l'URI PostgreSQL Supabase (utilise l'URL Session Pooler si nécessaire) et `FLASK_SECRET_KEY` avec une valeur aléatoire longue. Ne pousse jamais `.env` sur GitHub. Après les avoir définies, redéploie le projet.

Pour activer les notifications, ajoute aussi `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` et `VAPID_SUBJECT` comme variables d'environnement sur Vercel, puis redéploie. Utilise la même paire de clés VAPID que dans ton `.env` local et ne partage ni ne publie jamais `VAPID_PRIVATE_KEY`.

`api/index.py` expose l'application Flask à Vercel. `python app.py` reste la commande pour la lancer en local.

La connexion actuelle identifie un profil par son adresse e-mail, comme dans le code fourni ; l'adresse n'est pas vérifiée.

## Notifications sur téléphone

Sur ordinateur et Android, connecte-toi puis va dans **Profil > Notifications sur ce téléphone > Activer les notifications** et accepte la demande. Sur iPhone/iPad, ajoute d'abord Rencontres à l'écran d'accueil depuis Safari, ouvre l'app depuis son icône, puis active les notifications depuis le profil.

Les notifications de message affichent le nom de la personne et un aperçu du message (500 caractères maximum). Un clic ouvre la conversation après connexion. Une notification est envoyée pour chaque nouveau like ; un clic ouvre l'onglet des likes reçus. Un match réciproque déclenche la notification de match habituelle. Pour tester les notifications en local, ouvre le site sur `http://localhost:8000` et configure les trois variables VAPID dans `.env`.
