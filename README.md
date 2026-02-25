# price-drop-intelligence

Monitoramento de preços multi-país/loja com detecção de queda, persistência em SQLite, alertas Telegram, relatórios e execução automatizada via GitHub Actions.

## Funcionalidades
- Watchlist em `watchlist.yaml` com `country`, `store`, `url`, `currency`, `tags` e regras por item.
- Arquitetura pluggable de providers (`app/providers`) com fallback `generic_html`.
- Robustez de coleta: timeout curto, retry com backoff, rate limit por domínio e circuit breaker.
- Histórico em `data/prices.db` com tabelas `products`, `snapshots`, `alerts`, `runs`.
- Engine de detecção configurável (`config.yaml`): `reference_mode`, limites percentuais/absolutos, outlier e cooldown.
- Alertas no Telegram com batching (até 10 alertas/mensagem) e modo dry-run.
- Relatórios em `reports/latest.md` e `reports/latest.json`.
- Dashboard opcional em `site/` com publicação no GitHub Pages.

## Estrutura

```txt
app/
  providers/
    base.py
    generic_html.py
  db.py
  detector.py
  models.py
  reporting.py
  run.py
  telegram.py
  utils.py
.github/workflows/
  run.yml
  pages.yml
watchlist.yaml
config.yaml
tests/
```

## Setup local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Rodar local (sem enviar Telegram)

```bash
python -m app.run --mode dry --debug
```

Saídas:
- `data/prices.db`
- `reports/latest.md`
- `reports/latest.json`
- `logs/run_<timestamp>.log`

### Rodar com filtros

```bash
python -m app.run --mode dry --only-country BR --only-store amazon --only-tags airfryer,kitchen --limit 10
```

## Telegram

### 1) Criar bot
1. Converse com `@BotFather`.
2. Execute `/newbot` e copie o token.

### 2) Obter chat id
- Abra conversa com seu bot e envie uma mensagem.
- Acesse:
  `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
- Use o campo `chat.id`.

### 3) Configurar variáveis

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python -m app.run --mode live
```

## GitHub Actions

### Secrets necessários
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### Variável opcional
- `COMMIT_REPORTS=true` para commitar `reports/latest.md` e `reports/latest.json` no `main`.

Workflow principal: `.github/workflows/run.yml`
- cron a cada 6h
- dispatch manual
- instala dependências
- executa `python -m app.run --mode live`
- publica artifacts (`data/prices.db`, `reports/*`, `logs/*`)

Workflow Pages: `.github/workflows/pages.yml`
- publica `site/` no GitHub Pages

## Ajuste de regras
Edite `config.yaml`:
- `drop_percent_min`
- `drop_amount_min` por moeda
- `lookback_days`
- `reference_mode`: `last_price`, `rolling_max`, `rolling_median`
- `cooldown_hours`
- `realert_extra_drop_percent`
- `min_price_threshold` e `max_price_threshold`

Overrides por item em `watchlist.yaml > rules`.

## Adicionar novo provider por loja
1. Crie `app/providers/<store>.py` implementando `PriceProvider`.
2. Implemente `fetch_product(product) -> PriceSnapshot`.
3. Registre no factory em `app/providers/__init__.py`.

## Testes

```bash
pytest -q
```

Cobertura mínima atual:
- parse de preço (`BRL` e `USD`)
- detector (cooldown/outlier)
- render e batching do Telegram
