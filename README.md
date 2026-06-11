# TFZ Scanner v2 — Trading From Zero (Krasnov) automatizado

Scanner de perpetuos USDT (MEXC + Bybit) que automatiza la estrategia completa
del curso *Trading From Zero*: seleccion de monedas, deteccion de niveles de
liquidez, las 4 formaciones, stop estructural, R:R minimo 1:3 y alertas a
Telegram con verificacion automatica de resultados.

**Filtros de seleccion (Rama 2):** movimiento ±10% en 24h y volumen 24h
> $100M, ambos extraidos **exclusivamente de Binance** (quoteVolume de spot +
perpetuos USDT-M sumados por moneda). Las monedas no listadas en Binance se
descartan.

## Que cambia respecto a v1 (tu repo actual)

| # | Cambio | Rama del curso | Antes (v1) | Ahora (v2) |
|---|--------|----------------|------------|------------|
| 1 | Fuente de volumen/movimiento | Rama 2 | CoinGecko | Binance (spot + futures), filtro solo 24h |
| 2 | Formacion 3 — Cascada | Rama 3 / 8 | No existia | 3+ niveles encadenados a <2%/3% → `F3_cascade` |
| 3 | Formacion 4 — Reclaim | Rama 9 | No existia | Sweep fuera del rango + reclamo → `F4_reclaim` |
| 4 | Stop-loss estructural | Rama 5 | 2% fijo del precio | Debajo de la consolidacion (o del extremo del sweep) + `SL_BUFFER` |
| 5 | Take-profit full sweep | Rama 6 | Primer nivel | Tras los **2** niveles objetivo (`TP_MODE=SWEEP`) |
| 6 | Filtro R:R minimo | Idea central | No existia | `RR_MIN=3.0` (1:3 del curso) |
| 7 | Numeros redondos | Rama 6 | No existia | Aviso en la alerta si hay un numero redondo entre entrada y TP |
| 8 | Setup 80% | Rama 10 | No existia | Etiqueta 🎯 cuando se cumple el checklist completo |
| 9 | Modo bear market | Anexo 14 | No existia | `BEAR_MODE=1` → TP anticipado a +5% limpio |
| 10 | Position sizing | Rama 5 | No existia | `ACCOUNT_SIZE` + `RISK_PCT=2` → riesgo, nocional y margen en la alerta |
| 11 | **Fix checker Bybit** | — | Las alertas de Bybit nunca se verificaban (siempre consultaba MEXC) | El checker usa la API del exchange de cada alerta |
| 12 | Stats por formacion | — | Solo direccion/nivel | Tambien por formacion, exchange y setup-80 |

El resto (niveles horizontales LTF/1h/4h, trendlines de 3 toques, filtro de
graficos intradeables del Anexo 3, distancia <15% al nivel, dedup/cooldown,
embudo de diagnostico) se mantiene igual que en tu version.

## Archivos

- `main.py` — scanner (solo stdlib, sin dependencias)
- `checker.py` — verifica WIN/LOSS/TIMEOUT y manda el resumen a Telegram (usa `requests`)
- `scanner.yml` — workflow de GitHub Actions (va en `.github/workflows/`)
- `requirements.txt`

## Como actualizar tu repo de GitHub

```bash
git clone https://github.com/TU_USUARIO/TU_REPO.git
cd TU_REPO
# copia encima main.py, checker.py, requirements.txt
# y scanner.yml a .github/workflows/scanner.yml
git add -A
git commit -m "feat: scanner v2 — formaciones 3 y 4, SL estructural, RR 1:3, fix checker Bybit"
git push
```

Los secrets que ya tienes (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) siguen valiendo.
El `alerts_log.json` existente es compatible: las alertas viejas conservan sus
campos y el checker v2 las sigue evaluando.

## Variables de entorno nuevas (todas opcionales)

| Variable | Default | Descripcion |
|----------|---------|-------------|
| `BINANCE_SPOT_BASE` | `https://data-api.binance.vision` | Endpoint spot (mirror publico oficial, funciona desde GitHub Actions) |
| `BINANCE_FUT_BASE` | `https://fapi.binance.com` | Endpoint futures USDT-M |
| `RR_MIN` | `3.0` | R:R minimo para alertar. El curso exige 1:3; si quieres mas alertas para probar, baja a `2.0` |
| `TP_MODE` | `SWEEP` | `SWEEP` = TP tras los 2 niveles (curso) · `L1` = primer nivel (como v1) |
| `SL_BUFFER` | `0.002` | Margen extra bajo la consolidacion para el stop |
| `SL_MAX_PCT` | `0.04` | Si el stop estructural queda a mas de este % → descartar setup |
| `CASCADE_MIN` | `3` | Niveles encadenados para etiquetar Formacion 3 |
| `RECLAIM_WINDOW` | `6` | Velas recientes donde buscar el sweep+reclaim (F4) |
| `BEAR_MODE` | `0` | Anexo 14: TP anticipado si hay >5% de camino limpio |
| `BEAR_MIN_CLEAN` | `0.05` | Umbral del movimiento limpio para el modo bear |
| `ACCOUNT_SIZE` | `0` | Capital en USD. Si >0, la alerta incluye riesgo/posicion/margen |
| `RISK_PCT` | `2.0` | % del capital arriesgado por trade (curso: 2%) |
| `LEVERAGE` | `10` | Ahora configurable por env |
| `INSECURE_SSL` | `0` | Desactiva la verificacion TLS. **Solo** para probar en local si tu antivirus/proxy intercepta HTTPS (en este PC hace falta). Nunca en GitHub Actions |

## Importante

- **Geo-bloqueo de Binance Futures:** `fapi.binance.com` devuelve HTTP 451
  desde IPs de EEUU, y los runners de GitHub Actions suelen estar en EEUU.
  El spot usa el mirror `data-api.binance.vision` que si funciona, asi que el
  scanner seguira operando con volumen solo-spot si futures falla. Si quieres
  el volumen de futures tambien en Actions, usa un runner self-hosted o un
  proxy y configura `BINANCE_FUT_BASE`. Comprueba en los logs del workflow si
  aparece `[WARN] GET raw fallo https://fapi.binance.com/...`.
- `RR_MIN=3.0` con stop estructural es **estricto** (es lo que dice el curso):
  habra menos alertas que en v1, pero de mas calidad. Si tras unos dias no sale
  nada, baja a `RR_MIN=2.0` o pon `TP_MODE=SWEEP` con `SL_MAX_PCT=0.05`.
- El `backtest.py` de tu repo testea la logica de v1; no es valido para medir
  v2 sin adaptarlo (los SL/TP se calculan distinto).
- Esto genera **alertas**, no ejecuta ordenes. La gestion activa del trade
  (mover stop a breakeven en el retest, early exit por muros del order book,
  funding…) sigue siendo manual — Ramas 5, 6 y 7 del curso.
