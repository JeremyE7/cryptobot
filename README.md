# Crypto Bot — Forward Paper Trader 7.0-aggressive

Bot de paper trading con datos públicos de Binance. **Nunca envía órdenes reales** ni usa claves de API, retiros ni permisos de trading.

> **Estado:** paper forward con perfil agresivo. No es dinero real.

## Qué cambió en esta versión

La versión anterior (6.0) quedó atrapada en efectivo: después de una pequeña pérdida, el mínimo de orden de $4.90 hacía imposible comprar cualquier posición de $5 o dos posiciones de ~$4.75. El bot siguió calculando señales, pero no podía ejecutar ninguna compra. Esta versión corrige ese bloqueo y sube el riesgo:

- **Zona mixta (NEUTRAL): 40% de exposición** (antes 0%).
- **Recuperación (RECOVERY): 70% en los 2 líderes** (antes 50% en 1).
- **Alcista fuerte (STRONG_BULL): 100% en los 2 líderes** (sin cambio).
- **Bajista (BEAR): 0%**, con salida inmediata (sin cambio).
- **Mínimo de orden simulado: $1.00** (antes $4.90), para que una cuenta de $10 nunca vuelva a bloquearse.
- **Dashboard nuevo:** ahora dice explícitamente si el bot compró, vendió, mantiene o está en efectivo, y explica el motivo.

El perfil sigue siendo defensivo en mercados bajistas. Si no hay ninguna señal válida, el bot espera en efectivo: no compra por comprar.

## Estrategia

- BTC 1D define el régimen estructural.
- BTC 4H define el régimen rápido.
- Deadband `+0.10% / -0.10%` alrededor de EMA20/EMA50.
- `STRONG_BULL`: 100% en los 2 activos mejor rankeados.
- `RECOVERY`: 70% en los 2 mejores.
- `NEUTRAL`: 40% en el líder, solo si cumple el filtro de tendencia.
- `BEAR`: 0% en efectivo.
- Confirmaciones: 1 día para riesgo-on, 2 días para salir de una zona mixta; BEAR es inmediato.
- Rebalanceo semanal los lunes UTC + cambios de régimen.
- Costos simulados: 0.10% por lado + 0.05% de slippage.
- Mínimo de orden simulado: $1.00.

## Validación histórica actual

Ventana completa de la evaluación: **2025-05-06 → 2026-11-06** (datos públicos de Binance).

| Métrica | Resultado |
|---|---:|
| Retorno | **+21.00%** |
| Equity final desde $10 | **$12.10** |
| Max drawdown | **-19.92%** |
| Exposición media | **31.27%** |
| Rebalanceos | 84 |
| Órdenes | 162 |
| Costos | $0.9062 |
| Walk-forward | **4/6 folds positivos** |
| Peor fold | -5.68% |
| Peor drawdown por fold | -13.74% |

Es evidencia histórica, no una promesa. El perfil agresivo tiene más drawdown que el perfil 6.0 anterior; esa es la contrapartida de participar más.

## Cómo se opera

`data/paper.db` es la cuenta ficticia persistente y el ledger inmutable. `data/market.db` son velas de mercado reconstruibles.

```text
market.db                 paper.db
─────────                 ────────
velas Binance             cash
reemplazable              posiciones
se puede reconstruir      decisiones
                          trades inmutables
                          snapshots de equity
```

No borres `data/paper.db` durante el experimento.

## Primer arranque

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

Resultado esperado: **53 passed**.

```bash
crypto-bot paper init --cash 10
crypto-bot paper status
```

La inicialización no importa posiciones históricas: empieza en $10 de efectivo y la primera decisión es el siguiente día UTC.

## Actualizar una cuenta que ya existe

Si `data/paper.db` fue creado con la versión 6.0, el bot se negará a operar hasta migrarlo. El comando de upgrade conserva todo el historial de decisiones y trades y adopta el perfil 7.0-aggressive:

```bash
crypto-bot paper upgrade
crypto-bot paper doctor
crypto-bot paper tick
```

El upgrade no borra ni reescribe el ledger. Solo actualiza la huella de la estrategia y el estado de régimen reconstruido.

## Operación diaria

```bash
crypto-bot paper tick
```

Un tick:

1. sincroniza velas públicas de Binance;
2. finaliza velas parciales;
3. procesa cada día UTC pendiente exactamente una vez;
4. reconstruye el régimen con información disponible antes de la apertura;
5. ejecuta la compra/venta simulada a la primera apertura de 15m del día;
6. guarda decisión, trades y cuenta de forma atómica;
7. rechaza duplicados.

Si la máquina estuvo apagada, el siguiente tick hace catch-up de los días faltantes.

```bash
crypto-bot paper status
crypto-bot paper doctor
```

## Automatización

### Linux / systemd

```bash
./scripts/install-paper-systemd.sh
systemctl status crypto-bot-paper.timer
journalctl -u crypto-bot-paper.service
```

### Windows

```powershell
.\scripts\install-paper-windows.ps1
```

### GitHub Actions

Ver [`CLOUD_SETUP.md`](CLOUD_SETUP.md). El workflow diario ejecuta el tick, valida el ledger, regenera `data/cloud-status.txt` y `site/data.json`, y publica el dashboard.

## Dashboard

`site/index.html` + `site/app.js` muestran:

- un bloque grande **AHORA MISMO** con `EN EFECTIVO`, `INVERTIDO`, `COMPRÓ` o `VENDIÓ`;
- la última acción ejecutada y la próxima revisión;
- el reglamento completo de los 4 regímenes;
- el historial de compras/ventas y las últimas decisiones explicadas en español;
- equity, exposición, cash, drawdown, posiciones y comparación contra BTC/BNB/ETH/LINK.

Para regenerar el snapshot público:

```bash
python scripts/build-dashboard.py
```

## Regla de investigación

No ajustes parámetros a mitad de una prueba forward sin documentarlo. Los bugs de software se corrigen; los cambios de estrategia se versionan y se migran con `paper upgrade`.

No hay claves de API, permisos de retiro ni dinero real en ningún punto del proyecto.
