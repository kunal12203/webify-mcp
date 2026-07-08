# Webify

[English](../README.md) | [Português](README.pt-BR.md)

Pesquisa web adaptativa para agentes de IA de programação. Pesquise na web, construa grafos semânticos, obtenha respostas sintetizadas — a 5% do custo de ferramentas de pesquisa profunda.

Uma skill da [GrapeRoot](https://graperoot.dev)

## O que faz

| Ferramenta | Finalidade | Custo |
|------------|-----------|-------|
| `web_find(query)` | Busca web multi-fonte + síntese | ~$0.003/consulta |
| `web_lookup(url, query)` | Recuperação via grafo de página única | ~$0.0005/consulta |

**web_find** pesquisa no DuckDuckGo, constrói grafos semânticos a partir de múltiplas fontes em paralelo, extrai conteúdo relevante via BM25 e sintetiza com Haiku. Adapta a profundidade com base na complexidade da consulta — consultas factuais simples acessam 3 fontes, consultas de pesquisa multidimensionais escalam para 6+ fontes com recuperação multi-aspecto.

**web_lookup** busca uma única página, constrói um grafo de hierarquia de cabeçalhos e retorna apenas os nós relevantes (~250-750 tokens em vez de 5.000-50.000).

## Benchmarks

Avaliação cega A/B contra o Deep Research do Claude em 15 consultas inéditas. Juiz: Sonnet, avaliando precisão + completude + especificidade (1-5 cada, máximo 15/consulta).

| Métrica | Webify | Deep Research |
|---------|--------|--------------|
| Pontuação de qualidade | **68/75** (90,7%) | 73/75 (97,3%) |
| Custo por consulta | **~$0.003** | ~$0.05+ |
| Latência | **30-90s** | 80-280s |
| Eficiência de custo | **18x melhor** | referência |

Webify atinge 91% da qualidade do Deep Research a 5% do custo.

## Instalação

### Instalação Rápida

```bash
```

### Manual

```bash
git clone https://github.com/kunal12203/webify.git
cd webify
pip install "mcp>=1.3.0"
```

Requisitos: Python 3.9+, pip, git

## Configuração

| Variável de ambiente | Obrigatória | Descrição |
|---------------------|-------------|-----------|
| `ANTHROPIC_API_KEY` | Para `web_find` | Síntese com Haiku + aprendizado bandit |
| `BRAVE_SEARCH_API_KEY` | Recomendada | Busca confiável (2k consultas/mês grátis) |
| `WEBIFY_CACHE_DIR` | Não | Local do cache (padrão: `~/.cache/webify`) |

## Licença

[MIT](../LICENSE) — Copyright (c) 2026 GrapeRoot
