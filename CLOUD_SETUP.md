# Cloud paper-forward deployment (GitHub Actions)

Este paquete usa el perfil **7.0-aggressive** y mantiene la infraestructura de paper forward en GitHub Actions.

## Arquitectura

- GitHub Actions despierta todos los días a las **19:17 America/Guayaquil** (00:17 UTC).
- Los datos públicos vienen de `https://data-api.binance.vision`; no se usa cuenta ni API key de Binance.
- `data/paper.db` es la cuenta ficticia persistente y el ledger inmutable.
- `data/paper.db`, `data/cloud-status.txt` y `site/data.json` se commitean de vuelta al repositorio cuando cambia el estado.
- `data/market.db` es caché mutable de velas y no se commitea.
- `data/market-seed.db.gz` es una semilla de recuperación. Si la caché desaparece, el workflow la expande y sincroniza lo faltante.
- El workflow es idempotente y está serializado con un grupo de concurrencia.

## Configuración inicial

1. Crea un repositorio privado vacío en GitHub.
2. Sube esta carpeta a la rama por defecto (`main` recomendado).
3. En GitHub abre **Actions → Crypto Paper Forward → Run workflow** una vez.
4. El primer run crea $10 ficticios. No importa posiciones históricas.

No se necesitan secrets, Turso ni servidor propio.

## Qué se persiste

- `data/paper.db`: cuenta, posiciones, decisiones y trades inmutables.
- `data/cloud-status.txt`: resumen legible del último tick.
- `site/data.json`: snapshot que consume la landing.

No borres ni reescribas `data/paper.db` después de iniciar el experimento. Si vienes de la versión 6.0, ejecuta una vez:

```bash
crypto-bot paper upgrade
```

Ese comando conserva el historial y adopta el perfil 7.0-aggressive.

## Recuperación manual

Si se pierde la caché de Actions, el workflow expande `data/market-seed.db.gz`, sincroniza las velas faltantes y hace catch-up de los días pendientes exactamente una vez.

## Ver el bot

Abre el último run de **Crypto Paper Forward**, revisa `data/cloud-status.txt` o visita la página de GitHub Pages generada desde `site/`.

El bot puede permanecer en efectivo varios días si ninguna señal cumple las reglas. Eso es una decisión de protección, no un fallo. El dashboard ahora lo explica explícitamente.
