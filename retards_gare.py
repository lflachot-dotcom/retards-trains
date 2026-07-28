"""
Retards Trains SNCF par gare - via GTFS statique + GTFS-RT
============================================================

Installation préalable :
    pip install requests gtfs-realtime-bindings

Usage :
    python retards_gare.py "Lyon Part Dieu"
    python retards_gare.py --stop-id StopArea:OCE87547000

Étape 1 : télécharge (et met en cache) le GTFS statique SNCF pour lire stops.txt
Étape 2 : cherche le(s) stop_id correspondant au nom de gare fourni
Étape 3 : télécharge le flux GTFS-RT Trip Updates et filtre les retards sur ces stop_id
"""

import argparse
import csv
import io
import os
import sys
import zipfile

import requests

# --- Sources de données -----------------------------------------------------

# Jeu de données statique GTFS SNCF (page du dataset sur transport.data.gouv.fr :
# https://transport.data.gouv.fr/datasets/horaires-sncf). L'URL de téléchargement
# direct du zip GTFS change parfois de version ; si ce lien est mort, allez sur
# la page du dataset et copiez le lien "Télécharger" du fichier GTFS.
GTFS_STATIC_URL = "https://eu.ftp.opendatasoft.com/sncf/gtfs/export_gtfs_voyages.zip"

GTFS_RT_TRIP_UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"

CACHE_DIR = ".gtfs_cache"
STOPS_CACHE_FILE = os.path.join(CACHE_DIR, "stops.txt")


# --- Étape 1 : récupérer stops.txt (avec cache local) -----------------------

def get_stops_txt() -> str:
    """Télécharge le GTFS statique si besoin et retourne le chemin vers stops.txt."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    if os.path.exists(STOPS_CACHE_FILE):
        return STOPS_CACHE_FILE

    print(f"Téléchargement du GTFS statique depuis {GTFS_STATIC_URL} ...")
    resp = requests.get(GTFS_STATIC_URL, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        with z.open("stops.txt") as f_in, open(STOPS_CACHE_FILE, "wb") as f_out:
            f_out.write(f_in.read())

    print("stops.txt mis en cache dans", STOPS_CACHE_FILE)
    return STOPS_CACHE_FILE


# --- Étape 2 : chercher le(s) stop_id d'une gare par son nom -----------------

def find_stop_ids(stops_path: str, station_name: str) -> list[dict]:
    """
    Cherche dans stops.txt les lignes dont stop_name contient station_name
    (recherche insensible à la casse et aux accents simples).

    On privilégie les stop_id avec location_type == 0 (arrêts "feuille"),
    conformément aux exigences du flux GTFS-RT.
    """
    matches = []
    needle = station_name.strip().lower()

    with open(stops_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("stop_name", "").lower()
            if needle in name:
                matches.append(row)

    return matches


def print_stop_matches(matches: list[dict]) -> None:
    print(f"\n{len(matches)} correspondance(s) trouvée(s) :\n")
    for row in matches:
        print(
            f"  stop_id={row.get('stop_id'):<28} "
            f"location_type={row.get('location_type', '0'):<2} "
            f"stop_name={row.get('stop_name')}"
        )


# --- Étape 3 : interroger le flux GTFS-RT et filtrer sur les stop_id --------

def get_delays_for_stop_ids(stop_ids: set[str]) -> None:
    try:
        from google.transit import gtfs_realtime_pb2
    except ImportError:
        sys.exit(
            "Le package gtfs-realtime-bindings n'est pas installé.\n"
            "Lancez : pip install gtfs-realtime-bindings"
        )

    print(f"\nTéléchargement du flux temps réel : {GTFS_RT_TRIP_UPDATES_URL}")
    resp = requests.get(GTFS_RT_TRIP_UPDATES_URL, timeout=30)
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    found = False
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        for stu in trip_update.stop_time_update:
            if stu.stop_id not in stop_ids:
                continue

            found = True
            delay_arr = stu.arrival.delay if stu.HasField("arrival") else None
            delay_dep = stu.departure.delay if stu.HasField("departure") else None

            def fmt(delay):
                if delay is None:
                    return "N/A"
                minutes = delay // 60
                return f"{minutes:+d} min" if delay else "à l'heure"

            print(
                f"  Train {trip_update.trip.trip_id:<40} "
                f"stop_id={stu.stop_id:<25} "
                f"retard arrivée={fmt(delay_arr):<12} "
                f"retard départ={fmt(delay_dep)}"
            )

    if not found:
        print("  Aucun train avec mise à jour temps réel pour cette gare actuellement.")
        print("  (Normal si aucun train n'est en circulation perturbée à cet instant.)")


# --- Point d'entrée -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Retards de trains SNCF par gare")
    parser.add_argument("station", nargs="?", help="Nom de la gare, ex: 'Lyon Part Dieu'")
    parser.add_argument("--stop-id", help="stop_id exact à utiliser directement (bypass la recherche)")
    args = parser.parse_args()

    if args.stop_id:
        stop_ids = {args.stop_id}
    elif args.station:
        stops_path = get_stops_txt()
        matches = find_stop_ids(stops_path, args.station)

        if not matches:
            sys.exit(f"Aucune gare trouvée pour '{args.station}'.")

        print_stop_matches(matches)

        # On ne garde que les stop_id location_type == 0, comme exigé
        # par la validation du flux GTFS-RT.
        stop_ids = {
            row["stop_id"] for row in matches
            if row.get("location_type", "0") == "0"
        }

        if not stop_ids:
            sys.exit("Aucun stop_id avec location_type=0 trouvé pour cette gare.")
    else:
        parser.print_help()
        sys.exit(1)

    print(f"\nRecherche de retards pour {len(stop_ids)} stop_id : {stop_ids}")
    get_delays_for_stop_ids(stop_ids)


if __name__ == "__main__":
    main()
