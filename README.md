# Site de rencontre

## Configuration

Dans Supabase, récupère l'URL PostgreSQL depuis **Project Settings > Database** et remplace le placeholder par le vrai mot de passe. Pour Render, utilise si besoin l'URL **Session pooler** fournie par Supabase.

Les photos sont stockées dans un bucket Supabase Storage privé ; PostgreSQL ne conserve que leurs chemins. Configure `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (clé serveur secrète, jamais publiée) et `SUPABASE_STORAGE_BUCKET` dans `.env`. Pour Storage privé, utilise la clé JWT `service_role` de **Project Settings > API Keys > Legacy API Keys**, pas une clé `sb_secret_`. L'URL du projet est dans **Project Settings > API**, et le nom du bucket dans **Storage > Files**. Configure les mêmes variables dans Vercel. Ne rends pas le bucket public.

Aucune clé `anon` n'est utilisée. `init_db()` dans `app.py` crée les tables directement dans Supabase au démarrage local. Sur Vercel, initialise les tables une fois depuis ton ordinateur, après avoir configuré `DATABASE_URL` dans `.env` :

```powershell
python -c "from app import init_db; init_db()"
```

Pour appliquer les nouveaux index à une base déjà initialisée, relance cette commande une fois.

Après avoir configuré Storage, migre les photos existantes une seule fois depuis ton ordinateur :

```powershell
python -c "from app import migrate_existing_photos; print(f'{migrate_existing_photos()} profils migrés')"
```

Les nouveaux profils et les nouvelles photos de profil sont ensuite envoyés automatiquement au bucket privé ; l'application génère des URLs signées temporaires pour les afficher. Pour accélérer le chargement, ces liens sont réutilisés pendant 50 minutes et les liens des photos d'une liste sont générés en parallèle. Les photos restent dans le bucket privé.

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

Ouvre http://localhost:8000. Les profils, swipes, matchs et messages sont stockés dans PostgreSQL Supabase ; les photos sont dans Supabase Storage.

## Déployer sur Vercel

Importe le dépôt GitHub `Antoine1733/site-de-rencontre` dans Vercel. Le fichier `vercel.json` configure le point d'entrée Flask. Avant la première utilisation, initialise les tables depuis ton ordinateur avec la commande de configuration ci-dessus.

Dans **Project Settings > Environment Variables**, ajoute `DATABASE_URL` avec l'URI PostgreSQL Supabase (utilise l'URL Session Pooler si nécessaire) et `FLASK_SECRET_KEY` avec une valeur aléatoire longue. Ne pousse jamais `.env` sur GitHub. Après les avoir définies, redéploie le projet.

Pour activer les notifications, ajoute aussi `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` et `VAPID_SUBJECT` comme variables d'environnement sur Vercel, puis redéploie. Utilise la même paire de clés VAPID que dans ton `.env` local et ne partage ni ne publie jamais `VAPID_PRIVATE_KEY`.

`api/index.py` expose l'application Flask à Vercel. `python app.py` reste la commande pour la lancer en local.

La connexion actuelle identifie un profil par son adresse e-mail, comme dans le code fourni ; l'adresse n'est pas vérifiée.

Depuis **Profil > Supprimer mon compte**, tu peux supprimer ton profil après confirmation. Les likes, matchs, conversations, messages et abonnements aux notifications liés au compte sont supprimés automatiquement. La confirmation par e-mail évite une suppression accidentelle, mais ne vérifie pas l'identité : la connexion actuelle n'exige pas de mot de passe et ne vérifie pas l'adresse.

## Notifications sur téléphone

Sur ordinateur et Android, connecte-toi puis va dans **Profil > Notifications sur ce téléphone > Activer les notifications** et accepte la demande. Sur iPhone/iPad, ajoute d'abord Rencontres à l'écran d'accueil depuis Safari, ouvre l'app depuis son icône, puis active les notifications depuis le profil.

Les notifications de message affichent le nom de la personne et un aperçu du message (500 caractères maximum). Un clic ouvre la conversation après connexion. Une notification est envoyée pour chaque nouveau like ; un clic ouvre l'onglet des likes reçus. Un match réciproque déclenche la notification de match habituelle. Pour tester les notifications en local, ouvre le site sur `http://localhost:8000` et configure les trois variables VAPID dans `.env`.
