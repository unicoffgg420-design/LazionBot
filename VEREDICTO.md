# Veredicto — AREPAPOWER 100x HUNTER BOT (v1)

**Nota:** tal como estaba, este bot no iba a mandarte ni una sola alerta.
No es una opinión — es matemática: el scoring solo podía sumar un máximo
de **7 puntos** de 10, pero la condición para alertar era `score >= 9`.
Ese umbral era literalmente inalcanzable con el código tal cual estaba escrito.

## Bugs críticos encontrados

1. **El scoring nunca podía llegar a 9/10.** Sumando todos los checks
   posibles (liquidez, no_mint, lp_lock, liq_dex, microcap, socials,
   overlap) el máximo real era 7. El bot iba a correr 24/7 en Railway
   consumiendo tus API keys gratuitas sin generar jamás una señal.

2. **Etherscan API v1 está muerta desde el 15 de agosto de 2025.**
   Confirmado: cualquier llamada a `api.etherscan.io/api?...` hoy
   devuelve `{"status":"0","message":"NOTOK","result":"...switch to
   Etherscan API V2"}`. Todo el lado ETH del bot (PEPE en tu lista)
   estaba muerto desde hace más de un año.

3. **GMGN bloquea requests que no parezcan navegador real** (Cloudflare).
   `requests.get()` sin headers de navegador tiene alta probabilidad de
   devolver 403, y el bot lo trataba como "0 puntos" en silencio — otra
   razón más por la que el score nunca subía.

4. **Sin manejo de errores en el ciclo principal.** Cualquier excepción
   (un JSON inesperado, un timeout) tumbaba el proceso completo. Railway
   solo reintenta 10 veces (`restartPolicyMaxRetries: 10`) y después el
   bot queda offline hasta que entrás manualmente a reiniciarlo.

5. **Sin rate limiting real.** Por cada wallet se hacían hasta 3 llamadas
   seguidas (activa, bot, tokens recientes) casi sin pausa. Con API keys
   gratuitas (Helius/Etherscan) esto dispara 429 rápido.

6. **Sin deduplicado de alertas.** Si un token cumplía las condiciones,
   se re-alertaba en cada ciclo (cada 10 min) mientras siguiera en overlap.

7. **Dependencias sin usar.** `requirements.txt` traía `solana`, `web3` y
   `python-telegram-bot`, pero el código nunca los importa — solo infla
   el build de Docker.

## Qué se corrigió en v2.0 ("Lazion")

| Problema | Fix |
|---|---|
| Scoring inalcanzable | Rediseñado: Dexscreener solo ya da hasta 7/10; GMGN suma hasta 3 extra. `ALERT_THRESHOLD` configurable (default 7) |
| Etherscan v1 muerto | Migrado a v2 (`api.etherscan.io/v2/api?chainid=1&...`) |
| GMGN bloqueado en silencio | Headers de navegador + se trata como fuente opcional, no bloqueante |
| Proceso se caía entero | try/except en cada moneda y en el loop principal — el bot no muere aunque una API falle |
| Sin rate limiting | 1 sola llamada por wallet (antes eran 2-3), pausas entre requests, backoff automático ante 429 |
| Alertas duplicadas | Estado persistido en `alerted_tokens.json` |
| Dependencias muertas | `requirements.txt` reducido a `requests` |

## Lo que NO se puede arreglar solo con código (para que lo sepas)

- **GMGN puede seguir fallando igual.** Cloudflare evoluciona sus
  bloqueos; el header de navegador ayuda pero no lo garantiza al 100%.
  Por eso el bot ahora funciona bien aunque GMGN falle.
- **Las API keys gratuitas de Helius/Etherscan tienen límites bajos.**
  Con 3 monedas históricas y ~20 wallets cada una, vas a rozar el límite
  free tier igual. Si ves muchos `[RATE LIMIT]` en los logs de Railway,
  el siguiente paso es subir de plan en esa API, no tocar el código.
- **El filesystem de Railway es efímero entre deploys.** El deduplicado
  de alertas (`alerted_tokens.json`) se resetea si redeployás. No es un
  bug, es una limitación de la plataforma — si te importa, se puede
  mover a una base de datos externa (fuera del alcance de "gratis").

## Veredicto final

**Antes: 2/10** — el bot corría, pero por diseño no podía cumplir su
único propósito (nunca iba a alertar). Además parte del código apuntaba
a una API muerta hace un año.

**Ahora (Lazion v2.0): 7.5/10** — funcionalmente correcto, resiliente a
fallos de API individuales, sin dependencias muertas. El 2.5 que falta
para 10 es inherente al problema (fuentes gratuitas con límites y
protecciones anti-bot), no algo que se resuelva con más código gratis.
