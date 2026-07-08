# Webify

[English](../README.md) | [Deutsch](README.de.md)

Adaptive Webrecherche fuer KI-Coding-Agenten. Durchsuchen Sie das Web, erstellen Sie semantische Graphen, erhalten Sie synthetisierte Antworten -- bei 5 % der Kosten herkoemmlicher Deep-Research-Tools.

Ein Skill von [GrapeRoot](https://graperoot.dev)

## Was es macht

| Tool | Zweck | Kosten |
|------|-------|--------|
| `web_find(query)` | Multi-Source-Websuche + Synthese | ~$0.003/Abfrage |
| `web_lookup(url, query)` | Einzelseiten-Graph-Abruf | ~$0.0005/Abfrage |

**web_find** durchsucht DuckDuckGo, erstellt parallele semantische Graphen aus mehreren Quellen, extrahiert relevante Inhalte via BM25 und synthetisiert mit Haiku. Die Suchtiefe passt sich der Komplexitaet der Anfrage an -- einfache Faktenabfragen nutzen 3 Quellen, mehrdimensionale Rechercheabfragen skalieren auf 6+ Quellen mit Multi-Aspekt-Retrieval.

**web_lookup** ruft eine einzelne Seite ab, erstellt einen Heading-Hierarchie-Graphen und liefert nur die relevanten Knoten (~250-750 Tokens statt 5.000-50.000).

## Benchmarks

Blinde A/B-Evaluation gegen Claudes Deep Research bei 15 unbekannten Abfragen. Bewertung: Sonnet, Punktevergabe fuer Genauigkeit + Vollstaendigkeit + Spezifitaet (1-5 je Kategorie, maximal 15/Abfrage).

| Metrik | Webify | Deep Research |
|--------|--------|--------------|
| Qualitaetswert | **68/75** (90,7 %) | 73/75 (97,3 %) |
| Kosten pro Abfrage | **~$0.003** | ~$0.05+ |
| Latenz | **30-90s** | 80-280s |
| Kosteneffizienz | **18x besser** | Baseline |

Webify erreicht 91 % der Deep-Research-Qualitaet bei 5 % der Kosten.

## Installation

### Schnellinstallation

```bash
```

### Manuell

```bash
git clone https://github.com/kunal12203/webify.git
cd webify
pip install "mcp>=1.3.0"
```

Voraussetzungen: Python 3.9+, pip, git

## Konfiguration

| Umgebungsvariable | Erforderlich | Beschreibung |
|-------------------|--------------|--------------|
| `ANTHROPIC_API_KEY` | Fuer `web_find` | Haiku-Synthese + Bandit-Learning |
| `BRAVE_SEARCH_API_KEY` | Empfohlen | Zuverlaessige Suche (kostenlos 2k Abfragen/Monat) |
| `WEBIFY_CACHE_DIR` | Nein | Cache-Speicherort (Standard: `~/.cache/webify`) |

## Lizenz

[MIT](../LICENSE) -- Copyright (c) 2026 GrapeRoot
