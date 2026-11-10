# ads-scrapper

Scraper local de la [Meta Ads Library](https://www.facebook.com/ads/library/) para
encontrar anuncios en tendencia: los que más copias tienen activas con el mismo
texto y contenido.

No lee el HTML de la página. Abre la búsqueda en un navegador real, hace scroll y
guarda el JSON que la propia página descarga (el primer lote viene embebido en el
HTML; los siguientes, del endpoint GraphQL). Todo queda en una base SQLite local.

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

## Uso

Buscar una palabra clave (abre una ventana de Chromium y hace scroll solo):

```bash
ads-scrapper scrape "faja reductora" --country CO --max-ads 300
```

Abrir el buscador local (solo accesible desde tu computador):

```bash
ads-scrapper web
```

Agrupa los anuncios que comparten el mismo texto y los ordena por copias activas.
Permite buscar por texto, anunciante o dominio (sin importar tildes) y filtrar por
búsqueda de origen, formato, fecha de inicio, mínimo de duplicados y estado. Cada
tarjeta enlaza al anuncio en la Ad Library y a su página de destino.

Lo mismo, resumido en la terminal:

```bash
ads-scrapper top
ads-scrapper top "faja" --limit 50
```

Cargar una respuesta JSON copiada a mano desde las herramientas del navegador:

```bash
ads-scrapper import respuesta.json --query "faja reductora" --country CO
```

Opciones de `scrape`: `--status active|inactive|all`, `--media all|image|video|meme|none`,
`--min-delay` / `--max-delay` (segundos entre scrolls, por defecto 2–5) y `--headless`.

## Datos

Se guardan en `data/` (ignorado por git):

- `data/ads.db`: base SQLite.
  - `ads`: un registro por anuncio (`ad_archive_id`), con texto, título, CTA, URL y
    dominio de destino, anunciante, fechas, plataformas, URLs de medios,
    `collation_count` (copias activas según Meta) y el JSON original completo.
  - `observations`: una fila cada vez que se vuelve a ver un anuncio, con su
    `collation_count` de ese momento. Sirve para medir crecimiento.
  - `scrape_runs`: historial de búsquedas.
- `data/browser-profile/`: perfil del navegador, para no parecer una sesión nueva
  en cada ejecución.

`content_hash` agrupa anuncios con el mismo texto aunque usen creativos, páginas
o tiendas distintas. Meta solo agrupa los que además comparten creativo, así que
el total de copias de un grupo suma los conteos de Meta de cada creativo.

Las vistas previas usan las URLs de Meta, que caducan a los pocos días; al
repetir la búsqueda se renuevan.

## Cuidado con Meta

- **No inicies sesión** en la ventana del scraper. La Ad Library es pública; sin
  cuenta, lo peor que puede pasar es un bloqueo temporal de IP.
- Haz pocas búsquedas por hora y no bajes los tiempos de espera.
- Si Meta redirige a login o verificación, el scraper se detiene y no reintenta.

## Tests

```bash
pytest
```

`tests/test_scraper.py` ejecuta el scraper completo contra una página local que
imita la paginación de la Ad Library; no toca Meta.
