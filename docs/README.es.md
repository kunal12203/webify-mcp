# Webify

[English](../README.md) | [Español](README.es.md)

Investigacion web adaptativa para agentes de programacion con IA. Busca en la web, construye grafos semanticos, obtiene respuestas sintetizadas — al 5% del costo de las herramientas de investigacion profunda.

Un skill de [GrapeRoot](https://graperoot.dev)

## Que hace

| Herramienta | Proposito | Costo |
|-------------|-----------|-------|
| `web_find(query)` | Busqueda web multi-fuente + sintesis | ~$0.003/consulta |
| `web_lookup(url, query)` | Recuperacion de grafo de pagina unica | ~$0.0005/consulta |

**web_find** busca en DuckDuckGo, construye grafos semanticos a partir de multiples fuentes en paralelo, extrae contenido relevante mediante BM25 y sintetiza con Haiku. Adapta la profundidad segun la complejidad de la consulta — las consultas factuales simples acceden a 3 fuentes, mientras que las consultas de investigacion multidimensional escalan a 6+ fuentes con recuperacion multi-aspecto.

**web_lookup** obtiene una sola pagina, construye un grafo de jerarquia de encabezados y devuelve unicamente los nodos relevantes (~250-750 tokens en lugar de 5,000-50,000).

## Benchmarks

Evaluacion ciega A/B contra Deep Research de Claude en 15 consultas no vistas. Juez: Sonnet, puntuando precision + completitud + especificidad (1-5 cada una, maximo 15/consulta).

| Metrica | Webify | Deep Research |
|---------|--------|--------------|
| Puntuacion de calidad | **68/75** (90.7%) | 73/75 (97.3%) |
| Costo por consulta | **~$0.003** | ~$0.05+ |
| Latencia | **30-90s** | 80-280s |
| Eficiencia de costo | **18x mejor** | referencia |

Webify alcanza el 91% de la calidad de Deep Research al 5% del costo.

## Instalacion

### Instalacion rapida

```bash
```

### Manual

```bash
git clone https://github.com/kunal12203/webify.git
cd webify
pip install "mcp>=1.3.0"
```

Requisitos: Python 3.9+, pip, git

## Configuracion

| Variable de entorno | Requerida | Descripcion |
|---------------------|-----------|-------------|
| `ANTHROPIC_API_KEY` | Para `web_find` | Sintesis con Haiku + aprendizaje bandit |
| `BRAVE_SEARCH_API_KEY` | Recomendada | Busqueda confiable (2k consultas/mes gratis) |
| `WEBIFY_CACHE_DIR` | No | Ubicacion del cache (por defecto: `~/.cache/webify`) |

## Licencia

[MIT](../LICENSE) — Copyright (c) 2026 GrapeRoot
