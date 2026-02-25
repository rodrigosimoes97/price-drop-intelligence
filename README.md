# price-drop-intelligence

Sistema de monitoramento de preços com discovery automático (Shopee), histórico em SQLite, detecção de queda e alertas Telegram.

## Principais comandos

```bash
# discovery automático (não persiste)
python -m app.discovery.run_discovery --store shopee --dry --debug

# discovery live (persiste até 100 ativos por padrão)
python -m app.discovery.run_discovery --store shopee --country BR --max-active 100

# monitoramento usando DB (totalmente automático)
python -m app.run --mode dry --source db --limit 5

# modo auto: usa watchlist; se vazia, usa DB
python -m app.run --mode dry --source auto
```

## Arquitetura

- `app/discovery/`: engine de descoberta pluggable.
- `app/discovery/shopee.py`: provider Shopee com parsing por múltiplas estratégias.
- `app/affiliate/shopee.py`: adapter de link de afiliado (template/api com fallback).
- `app/db.py`: SQLite com `products`, `snapshots`, `alerts`, `runs` + colunas de discovery (`canonical_url`, `discovered_at_utc`, `expires_at_utc`, `score`, etc.).
- `app/run.py`: monitor principal (watchlist/db/auto).

## Configuração

### `discovery.yaml`
Controla categorias, filtros, HTTP e afiliado.

- `max_active_products`: limite global (default 100)
- `ttl_hours`: expiração automática
- `categories[*].take`: corte por categoria antes do limite global
- `filters`: min/max price, min sold/rating, exclusão por keywords

### `watchlist.yaml`
Pode ficar vazio para operação 100% automática:

```yaml
products: []
```

## Affiliate Shopee

Config em `discovery.yaml > shopee > affiliate`:

- `mode: template` com `template` (usa `{url}`)
- `mode: api` usando env vars opcionais:
  - `SHOPEE_AFFILIATE_API_BASE`
  - `SHOPEE_AFFILIATE_TOKEN`

Se API/template falhar, o fluxo mantém `canonical_url`.

## Workflows

- `.github/workflows/discovery.yml`: roda discovery a cada 12h + manual.
- `.github/workflows/run.yml`: roda monitor a cada 6h + manual.
- `.github/workflows/pages.yml`: publica dashboard estático.

Artifacts incluem DB, reports e logs.

## Relatórios gerados

- `reports/discovery_latest.json`
- `reports/discovery_latest.md`
- `reports/latest.json`
- `reports/latest.md`

## Testes

```bash
pytest -q
```
