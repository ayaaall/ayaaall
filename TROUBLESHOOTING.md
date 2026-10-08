# Guide de dépannage — déploiement `ocr-app` derrière un proxy d'entreprise

Ce document réunit les problèmes rencontrés lors du déploiement sur une VM
Docker derrière le proxy SAES Holding, et leurs solutions. À lire **avant**
de redéployer ce projet sur une nouvelle machine.

## 1. Build de l'image Docker : `apt-get` / `pip` échouent

**Symptôme** : `Unable to connect to deb.debian.org` ou timeout `pip install`.

**Cause** : le build Docker ne passe pas automatiquement par le proxy
d'entreprise, même si le conteneur final l'a dans son `environment:`.

**Solution** : passer le proxy comme argument de build, dans `docker-compose.yml` :

```yaml
services:
  app:
    build:
      context: .
      args:
        HTTP_PROXY: http://algpxy:8080
        HTTPS_PROXY: http://algpxy:8080
        NO_PROXY: localhost,127.0.0.1
```

Le `Dockerfile` doit déclarer ces `ARG` en tout début (déjà fait dans ce projet) :
```dockerfile
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}
ENV NO_PROXY=${NO_PROXY}
```

Puis reconstruire : `docker compose build app`.

⚠️ Le build de `requirements-heavy.txt` (torch, transformers, docling,
marker-pdf) prend **20 à 50 minutes** derrière ce proxy. C'est normal,
ne pas interrompre.

---

## 2. `qwen_ollama` échoue avec `504 Unknown Host` ou `403 Forbidden`

**Symptôme** : les appels vers `http://host.docker.internal:11434/api/chat`
échouent alors qu'Ollama tourne bien sur l'hôte.

**Cause** : le proxy d'entreprise intercepte aussi les appels internes vers
`host.docker.internal`, qu'il ne connaît pas (nom Docker interne, pas une
vraie adresse internet).

**Solution** : exclure explicitement ces adresses du proxy, dans
`docker-compose.yml`, service `app` :

```yaml
    environment:
      NO_PROXY: "host.docker.internal,localhost,127.0.0.1"
      no_proxy: "host.docker.internal,localhost,127.0.0.1"
```

Puis recréer le conteneur (`restart` ne suffit pas pour une nouvelle
variable d'env) :
```bash
docker compose up -d --force-recreate app
```

---

## 3. Téléchargement de modèles HuggingFace (`qwen35_vl`, `docling`) :
### `SSL: UNEXPECTED_EOF_WHILE_READING` / `Server disconnected without sending a response`

**Symptôme** : le téléchargement d'un modèle (Qwen3.5-2B, docling-models…)
coupe de façon aléatoire, parfois après avoir déjà téléchargé 90% du fichier.

**Cause identifiée** : `huggingface_hub` télécharge **plusieurs fichiers en
parallèle** (plusieurs connexions HTTPS simultanées). Le proxy d'entreprise
gère mal ce parallélisme et coupe une partie des connexions au hasard.
Un simple `curl https://huggingface.co` fonctionne très bien (`HTTP 200`) —
ce n'est donc **pas** un blocage du domaine, juste du parallélisme.

**Solution** : forcer le téléchargement fichier par fichier
(`max_workers=1`), à la main, une seule fois, en autorisant temporairement
la sortie internet (le conteneur tourne normalement avec
`HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`, à ne PAS retirer du
`docker-compose.yml` — juste à surcharger ponctuellement) :

```bash
docker compose run --rm \
  -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
  app python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='Qwen/Qwen3.5-2B', max_workers=1)
"
```

Une fois les fichiers dans le volume `hf_cache`, l'app fonctionne
normalement en mode hors-ligne (`HF_HUB_OFFLINE=1`) — plus besoin
d'internet pour ce modèle.

---

## 4. `docling` : `Cannot find an appropriate cached snapshot folder`
### malgré un téléchargement réussi juste avant

**Cause** : `docling` a besoin d'une **révision (version) précise** de
`docling-project/docling-models` (actuellement `v2.3.0`), différente de la
révision par défaut (`main`) que `snapshot_download` prend si on ne précise
rien. Deux dossiers de cache différents, donc `docling` ne "voit" pas ce
qui a été téléchargé sans préciser la bonne version.

**Comment retrouver la version exacte attendue** : lire le traceback de
l'erreur, qui pointe vers le fichier exact dans la bibliothèque `docling`
installée (`/usr/local/lib/python3.11/site-packages/docling/...`), puis
l'inspecter :
```bash
docker compose run --rm app cat \
  /usr/local/lib/python3.11/site-packages/docling/models/stages/table_structure/table_structure_model.py \
  | grep -n revision
```

**Solution** : prefetch en précisant explicitement cette révision :
```bash
docker compose run --rm \
  -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
  app python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='docling-project/docling-layout-heron', max_workers=1)
snapshot_download(repo_id='docling-project/docling-models', revision='v2.3.0', max_workers=1)
"
```

---

## 5. Disque plein (`No space left on device`) pendant le build/téléchargement

**Symptôme** : `write ... no space left on device`, ou avertissement
`Not enough free disk space` pendant un téléchargement HuggingFace.

**Diagnostic** :
```bash
df -h /                              # espace disque global
docker system df                     # espace utilisé par Docker
sudo du -h --max-depth=1 / | sort -rh | head -15   # gros dossiers
sudo du -sh /usr/share/ollama/.ollama              # modèles Ollama (souvent le + gros)
```

**Nettoyage courant** :
```bash
ollama list                          # voir les modèles installés
ollama rm <nom:tag>                  # supprimer ceux inutilisés
docker image prune -a                # images Docker orphelines
```

Sur cette VM, les modèles Ollama à eux seuls occupaient ~26 Go pour 6
modèles — vérifier régulièrement lesquels sont vraiment utilisés par
`OLLAMA_VL_MODEL` avant d'en installer de nouveaux.

---

## 6. Postgres : `relation "users" does not exist` malgré `Database initialised`

**Cause** : le code de migration (`init_db()` dans `app/core/database.py`)
faisait `CREATE TABLE` et `ALTER TABLE` dans **la même transaction**.
Postgres annule toute la transaction si une seule instruction échoue (ex :
colonne déjà existante) — contrairement à SQLite, qui ne l'annule pas. Donc
sur Postgres, l'échec silencieux de l'`ALTER TABLE` annulait aussi la
création des tables, qui avait pourtant réussi.

**Solution (déjà appliquée dans ce repo)** : séparer `create_all` et
chaque `ALTER TABLE` de migration dans des transactions distinctes
(`async with engine.begin() as conn:` séparés).

---

## 7. HTTPS : ports déjà pris (`address already in use`)

Cette VM héberge plusieurs projets, chacun avec son propre nginx/port :
- `admintrack` utilise déjà `8080` (→ 443 interne)
- Un nginx système (hors Docker) occupe déjà `80` et `443`

**Solution retenue pour `ocr-app`** : port `8443` au lieu de `443` standard :
```yaml
  nginx:
    ports:
      - "8443:443"
```
Accès : `https://<IP_VM>:8443/docs` (certificat auto-signé, avertissement
navigateur normal). Le port doit être ouvert côté pare-feu réseau en plus
de Docker (demande à faire à l'équipe IT si l'accès externe échoue alors
que `curl -k https://localhost:8443` fonctionne depuis la VM elle-même).

---

## Checklist de déploiement sur une nouvelle machine

1. `git clone` le repo, créer `.env` à partir de `.env.example`
   (JWT_SECRET et POSTGRES_PASSWORD générés, jamais les valeurs par défaut)
2. Adapter le proxy dans `docker-compose.yml` (`build.args`) si nécessaire
3. `docker compose build app` (patience, 20-50 min si `requirements-heavy.txt`)
4. `docker compose up -d`
5. Si usage de `docling`/`qwen35_vl` : lancer le prefetch (§3 et §4 ci-dessus)
6. Si usage de `qwen_ollama` : installer Ollama sur l'hôte, `ollama pull
   qwen3.5:2b`, vérifier `NO_PROXY` inclut `host.docker.internal`
7. Vérifier l'espace disque disponible (`df -h /`) avant tout téléchargement
   de modèle volumineux