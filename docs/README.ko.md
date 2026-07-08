# Webify

[English](../README.md) | [한국어](README.ko.md)

AI 코딩 에이전트를 위한 적응형 웹 리서치. 웹을 검색하고, 시맨틱 그래프를 구축하며, 합성된 답변을 제공합니다 — 딥 리서치 도구 비용의 5%로.

[GrapeRoot](https://graperoot.dev)의 스킬

## 기능

| 도구 | 용도 | 비용 |
|------|------|------|
| `web_find(query)` | 다중 소스 웹 검색 + 합성 | ~$0.003/쿼리 |
| `web_lookup(url, query)` | 단일 페이지 그래프 검색 | ~$0.0005/쿼리 |

**web_find**는 DuckDuckGo를 검색하고, 여러 소스에서 병렬로 시맨틱 그래프를 구축하며, BM25를 통해 관련 콘텐츠를 추출하고, Haiku로 합성합니다. 쿼리 복잡도에 따라 깊이를 조절합니다 — 단순한 사실 확인 쿼리는 3개 소스를 사용하고, 다차원 리서치 쿼리는 다중 관점 검색으로 6개 이상의 소스까지 확장됩니다.

**web_lookup**은 단일 페이지를 가져와 제목 계층 구조 그래프를 구축하고, 관련 노드만 반환합니다 (5,000-50,000 토큰 대신 ~250-750 토큰).

## 벤치마크

15개의 미확인 쿼리에 대해 Claude의 Deep Research와 블라인드 A/B 평가를 수행했습니다. 평가자: Sonnet, 정확성 + 완전성 + 구체성 점수 (각 1-5점, 쿼리당 최대 15점).

| 지표 | Webify | Deep Research |
|------|--------|--------------|
| 품질 점수 | **68/75** (90.7%) | 73/75 (97.3%) |
| 쿼리당 비용 | **~$0.003** | ~$0.05+ |
| 지연 시간 | **30-90초** | 80-280초 |
| 비용 효율성 | **18배 우수** | 기준선 |

Webify는 비용의 5%로 Deep Research 품질의 91%를 달성합니다.

## 설치

### 빠른 설치

```bash
```

### 수동 설치

```bash
git clone https://github.com/kunal12203/webify.git
cd webify
pip install "mcp>=1.3.0"
```

요구 사항: Python 3.9+, pip, git

## 설정

| 환경 변수 | 필수 | 설명 |
|-----------|------|------|
| `ANTHROPIC_API_KEY` | `web_find` 사용 시 | Haiku 합성 + 밴딧 학습 |
| `BRAVE_SEARCH_API_KEY` | 권장 | 안정적인 검색 (무료 월 2,000건 쿼리) |
| `WEBIFY_CACHE_DIR` | 아니오 | 캐시 위치 (기본값: `~/.cache/webify`) |

## 라이선스

[MIT](../LICENSE) — Copyright (c) 2026 GrapeRoot
