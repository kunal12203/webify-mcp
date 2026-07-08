# Webify

[English](../README.md) | [Français](README.fr.md)

Recherche web adaptative pour les agents de codage IA. Recherchez sur le web, construisez des graphes semantiques, obtenez des reponses synthetisees -- a 5 % du cout des outils de recherche approfondie.

Un skill par [GrapeRoot](https://graperoot.dev)

## Ce que ca fait

| Outil | Objectif | Cout |
|-------|----------|------|
| `web_find(query)` | Recherche web multi-sources + synthese | ~$0.003/requete |
| `web_lookup(url, query)` | Extraction par graphe d'une page unique | ~$0.0005/requete |

**web_find** effectue une recherche sur DuckDuckGo, construit des graphes semantiques a partir de plusieurs sources en parallele, extrait le contenu pertinent via BM25, et synthetise avec Haiku. Il adapte la profondeur en fonction de la complexite de la requete -- les requetes factuelles simples interrogent 3 sources, tandis que les requetes de recherche multidimensionnelles montent a 6+ sources avec une extraction multi-aspect.

**web_lookup** recupere une seule page, construit un graphe de hierarchie de titres, et renvoie uniquement les noeuds pertinents (~250-750 tokens au lieu de 5 000-50 000).

## Benchmarks

Evaluation en aveugle A/B contre Claude Deep Research sur 15 requetes inedites. Juge : Sonnet, notant la precision + l'exhaustivite + la specificite (1-5 chacune, max 15/requete).

| Metrique | Webify | Deep Research |
|----------|--------|--------------|
| Score de qualite | **68/75** (90,7 %) | 73/75 (97,3 %) |
| Cout par requete | **~$0.003** | ~$0.05+ |
| Latence | **30-90s** | 80-280s |
| Efficacite-cout | **18x meilleur** | reference |

Webify atteint 91 % de la qualite de Deep Research a 5 % du cout.

## Installation

### Installation rapide

```bash
```

### Manuelle

```bash
git clone https://github.com/kunal12203/webify-mcp.git
cd webify
pip install "mcp>=1.3.0"
```

Prerequis : Python 3.9+, pip, git

## Configuration

| Variable d'env | Requise | Description |
|----------------|---------|-------------|
| `ANTHROPIC_API_KEY` | Pour `web_find` | Synthese Haiku + apprentissage bandit |
| `BRAVE_SEARCH_API_KEY` | Recommandee | Recherche fiable (2k requetes/mois gratuites) |
| `WEBIFY_CACHE_DIR` | Non | Emplacement du cache (par defaut : `~/.cache/webify`) |

## Licence

[MIT](../LICENSE) -- Copyright (c) 2026 GrapeRoot
