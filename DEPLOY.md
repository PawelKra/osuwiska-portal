# Wdrozenie aplikacji Portal Osuwisk

Instrukcja zaklada uruchomienie aplikacji na serwerze przez Docker Compose.

## Wymagania

- Linux server z Docker Engine i Docker Compose pluginem
- ok. kilka GB miejsca na dane w `dane/` i baze w `db_data/`
- otwarty port `8999` albo reverse proxy, np. Nginx/Caddy

## Pliki danych

Katalog `dane/` powinien zawierac:

- `osuwiska_pl.gpkg`
- pliki `g_inspectorate.*`, w tym `g_inspectorate.shp`
- paczki BDL `BDL_*.zip` dla nadlesnictw obslugiwanych w `scripts/import_data.py`

## Konfiguracja

Utworz plik `.env` w katalogu projektu:

```env
POSTGRES_DB=osuwiska
POSTGRES_USER=osuwiska
POSTGRES_PASSWORD=zmien_to_haslo
```

Haslo ustaw przed pierwszym startem bazy. Jesli zmienisz haslo po utworzeniu `db_data/`, trzeba zaktualizowac baze recznie albo odtworzyc wolumen/katalog danych.

## Pierwsze uruchomienie

```bash
docker compose up -d --build
```

Sprawdz status:

```bash
docker compose ps
docker compose logs -f dash
```

Aplikacja powinna byc dostepna pod:

```text
http://ADRES_SERWERA:8999
```

## Import danych

Po starcie kontenerow wykonaj pelny import:

```bash
docker compose exec dash python /scripts/import_data.py
```

Import wybranej czesci:

```bash
docker compose exec dash python /scripts/import_data.py --only osuwiska
docker compose exec dash python /scripts/import_data.py --only inspectorate
docker compose exec dash python /scripts/import_data.py --only bdl
```

Po imporcie aplikacja powinna sama korzystac z nowych danych. W razie potrzeby zrestartuj:

```bash
docker compose restart dash
```

## Aktualizacja kodu

```bash
git pull
docker compose up -d --build
```

Jesli zmienily sie dane w `dane/`, uruchom ponownie import odpowiedniej warstwy.

## Backup

Baza jest trzymana w katalogu `db_data/`. Najprostszy backup SQL:

```bash
docker compose exec -T postgis pg_dump -U osuwiska -d osuwiska > backup_osuwiska.sql
```

Odtworzenie na pustej bazie:

```bash
docker compose exec -T postgis psql -U osuwiska -d osuwiska < backup_osuwiska.sql
```

## Reverse proxy

Przykladowa konfiguracja Nginx:

```nginx
server {
    listen 80;
    server_name twoja-domena.pl;

    location / {
        proxy_pass http://127.0.0.1:8999;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

W takim wariancie mozna ograniczyc publikacje portu `8999` firewallem do localhost albo sieci administracyjnej.

## Diagnostyka

Status kontenerow:

```bash
docker compose ps
```

Logi aplikacji:

```bash
docker compose logs -f dash
```

Logi bazy:

```bash
docker compose logs -f postgis
```

Wejscie do bazy:

```bash
docker compose exec postgis psql -U osuwiska -d osuwiska
```

Przydatne zapytania kontrolne:

```sql
SELECT count(*) FROM osuwiska_pl;
SELECT count(*) FROM g_subarea;
SELECT count(*) FROM f_storey_species;
SELECT DISTINCT nadlesnictwo_name FROM g_subarea ORDER BY 1;
```

## Najczestsze problemy

- Pusta lista nadlesnictw: sprawdz, czy wykonano import BDL i czy tabela `g_subarea` ma dane.
- Brak osuwisk: sprawdz import `osuwiska_pl.gpkg` oraz przeciecia geometrii z `g_subarea`.
- Brak warstwy cieniowania: uslugi zewnetrzne Geoportalu/Esri moga byc czasowo niedostepne albo blokowane przez siec serwera.
- Zmiana hasla nie dziala: baza zapamietuje haslo przy pierwszym utworzeniu `db_data/`.
