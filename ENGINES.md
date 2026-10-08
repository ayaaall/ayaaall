# Guide des moteurs OCR — `ocr-app`

Ce projet propose 4 moteurs d'extraction de texte, sélectionnables via le
paramètre `engine` dans `options` lors de la création d'un job
(`POST /api/jobs`). Ce document explique le principe de fonctionnement de
chacun, ses prérequis, et quand l'utiliser.

## Vue d'ensemble

| Moteur | Type | Vitesse (CPU) | Dépendance externe |
|---|---|---|---|
| `rapidocr` | OCR classique (détection + reconnaissance) | Rapide (~1-3s/page) | Aucune |
| `qwen_ollama` | Modèle de langage vision (VLM) via Ollama | Moyen (~30s-3min/page) | Serveur Ollama sur l'hôte |
| `qwen35_vl` | Même modèle VLM, chargé directement en Python | Lent (~5-20min/page) | `torch` + `transformers` |
| `docling` | Parsing PDF natif (mise en page + tableaux) | Moyen (~15-40s/page) | `docling` + modèles HF |

---

## 1. `rapidocr` — OCR classique

**Principe** : c'est de l'OCR "traditionnel", en deux étapes bien séparées :
1. **Détection** : repérer où se trouvent les zones de texte sur l'image
   (des rectangles autour de chaque ligne)
2. **Reconnaissance** : lire caractère par caractère le contenu de chaque
   zone détectée

Ces deux étapes utilisent des petits modèles spécialisés (PP-OCR, ~10-20 Mo
chacun), optimisés pour tourner très vite sur CPU via le moteur
`onnxruntime`. Pas de "compréhension" du texte — juste de la reconnaissance
visuelle de caractères.

**Fichier** : `app/services/ocr_engine.py`, fonction implicite via la
librairie `rapidocr-onnxruntime` (voir `requirements.txt`).

**Quand l'utiliser** : documents imprimés nets, mise en page simple. C'est
le moteur par défaut recommandé pour l'usage courant.

---

## 2. `qwen_ollama` — VLM via serveur Ollama

**Principe** : ici on utilise un **modèle de langage avec vision** (VLM),
pas de l'OCR classique. Le modèle "regarde" l'image entière et génère du
texte en réponse à une consigne (`prompt`), comme le ferait un assistant
IA à qui on montre une photo. Il ne détecte pas des zones de texte — il
"comprend" l'image et rédige sa transcription.

`app/services/ocr_engine.py`, fonction `_run_qwen_ollama` :
- Envoie l'image + le prompt en HTTP (`POST {OLLAMA_HOST}/api/chat`) à un
  serveur **Ollama**, séparé de l'app, tournant sur l'hôte (la VM)
- Ollama gère le chargement du modèle et l'inférence lui-même
- Le modèle est stocké au format **GGUF quantifié** (compressé, ex. 4 bits
  par poids au lieu de 16) — plus rapide et plus léger, au prix d'une
  précision théorique légèrement réduite

**Prérequis de déploiement** :
```bash
# Sur l'hôte (pas dans le conteneur) :
ollama pull qwen3.5:2b
```
Variables d'env (`docker-compose.yml`) : `OLLAMA_HOST`, `OLLAMA_VL_MODEL`.

**Quand l'utiliser** : documents où `rapidocr` peine (mise en page complexe,
tableaux, écriture dégradée) et où la vitesse relative d'Ollama est
acceptable.

---

## 3. `qwen35_vl` — même VLM, via `transformers` directement

**Principe** : strictement le **même modèle Qwen3.5** que `qwen_ollama`,
mais chargé et exécuté **directement dans le processus Python de l'app**,
via la bibliothèque `transformers` de HuggingFace — sans passer par un
serveur externe.

`app/services/ocr_engine.py`, fonction `_run_qwen35_vl` :
1. `AutoProcessor` transforme l'image en tenseurs numériques
2. `AutoModelForImageTextToText` charge les poids du modèle en mémoire
   (`device_map="auto"` : GPU si disponible, sinon CPU)
3. `model.generate(...)` produit le texte, token par token
   (`temperature=0.0` : sortie déterministe, pas de créativité — important
   pour une transcription fidèle)

Le modèle est ici au format **HuggingFace natif** (`.safetensors`, pleine
précision), plus volumineux (~4,5 Go) que la version Ollama (~2,7 Go).

**Prérequis de déploiement** — les poids ne sont PAS inclus dans l'image
Docker (téléchargés à la demande, ou pré-téléchargés une fois) :
```bash
docker compose run --rm -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 app \
  python -c "from huggingface_hub import snapshot_download; \
  snapshot_download(repo_id='Qwen/Qwen3.5-2B', max_workers=1)"
```
⚠️ `max_workers=1` est important derrière un proxy d'entreprise — voir
`TROUBLESHOOTING.md` §3. Nécessite `requirements-heavy.txt`
(`torch`, `transformers`) installé dans l'image.

**Quand l'utiliser** : si Ollama n'est pas disponible sur l'infrastructure
cible, ou pour comparer la précision avec la version quantifiée.

---

## 4. `docling` — parsing PDF natif (mise en page + tableaux)

**Principe** : contrairement aux trois moteurs précédents (qui travaillent
sur une **image** de la page), `docling` analyse le **PDF lui-même** :
structure du document, position des blocs, détection de tableaux via un
modèle de vision spécialisé (`docling-layout-heron`) et reconstruction de
leur structure (`docling-models` / TableFormer). Il combine plusieurs
modèles spécialisés (détection de mise en page, structure de tableau,
OCR de secours si le PDF n'a pas de texte natif — ici `rapidocr` sert de
moteur OCR interne à `docling`).

`app/services/ocr_engine.py`, fonction `_run_docling` :
```python
from docling.document_converter import DocumentConverter
converter = DocumentConverter()
doc = converter.convert(pdf_path).document
```

**Prérequis de déploiement** — deux modèles HuggingFace, dont un avec une
**révision précise obligatoire** :
```bash
docker compose run --rm -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 app \
  python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='docling-project/docling-layout-heron', max_workers=1)
snapshot_download(repo_id='docling-project/docling-models', revision='v2.3.0', max_workers=1)
"
```
⚠️ Si la révision `v2.3.0` a changé côté `docling` (mise à jour de la
bibliothèque), retrouver la nouvelle valeur attendue via :
```bash
docker compose run --rm app grep -rn "revision=" \
  /usr/local/lib/python3.11/site-packages/docling/models/stages/table_structure/table_structure_model.py
```

**Quand l'utiliser** : PDF avec tableaux complexes ou mise en page à
plusieurs colonnes, où préserver la structure du document compte plus que
la vitesse.

---

## Prérequis communs après un `git pull` / nouveau déploiement

1. `docker compose build app` (long si `requirements-heavy.txt` inclus)
2. Prefetch des modèles nécessaires (§2 et §3 ci-dessus) — **une seule
   fois**, les poids restent dans le volume `hf_cache`
3. `ollama pull qwen3.5:2b` sur l'hôte si `qwen_ollama` est utilisé
4. Voir `TROUBLESHOOTING.md` en cas d'erreur réseau/proxy pendant ces étapes
