import datetime
import io
import os
import zipfile
from zoneinfo import ZoneInfo

import requests
import pandas as pd

GTFS_STATIC_URL = "https://eu.ftp.opendatasoft.com/sncf/gtfs/export_gtfs_voyages.zip"
GTFS_RT_TRIP_UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"

CACHE_DIR = ".gtfs_cache"
STOPS_CACHE_FILE = os.path.join(CACHE_DIR, "stops.txt")
TRIPS_CACHE_FILE = os.path.join(CACHE_DIR, "trips.txt")
ROUTES_CACHE_FILE = os.path.join(CACHE_DIR, "routes.txt")

DATA_DIR = "data"

# Fenêtre de silence (heure locale Paris) : pas de collecte entre ces heures
SILENCE_START_HOUR = 1
SILENCE_END_HOUR = 4

# Seuil de retard en secondes pour être conservé (0 = tout retard, même 1 sec)
DELAY_THRESHOLD_SEC = 0

PARIS_TZ = ZoneInfo("Europe/Paris")


def is_in_silence_window(now_paris: datetime.datetime) -> bool:
    return SILENCE_START_HOUR <= now_paris.hour < SILENCE_END_HOUR


def ensure_static_gtfs():
    os.makedirs(CACHE_DIR, exist_ok=True)
    if all(os.path.exists(p) for p in (STOPS_CACHE_FILE, TRIPS_CACHE_FILE, ROUTES_CACHE_FILE)):
        return

    print("Téléchargement du GTFS statique SNCF (mis en cache)...")
    resp = requests.get(GTFS_STATIC_URL, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        for member, target in [
            ("stops.txt", STOPS_CACHE_FILE),
            ("trips.txt", TRIPS_CACHE_FILE),
            ("routes.txt", ROUTES_CACHE_FILE),
        ]:
            with z.open(member) as f_in, open(target, "wb") as f_out:
                f_out.write(f_in.read())


def fmt_time(epoch):
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch, tz=PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def collect_current_delays() -> pd.DataFrame:
    from google.transit import gtfs_realtime_pb2

    trips_df = pd.read_csv(TRIPS_CACHE_FILE, dtype=str).add_prefix("trip_")
    routes_df = pd.read_csv(ROUTES_CACHE_FILE, dtype=str).add_prefix("route_")
    stops_df = pd.read_csv(STOPS_CACHE_FILE, dtype=str).add_prefix("stop_")

    trips_df = trips_df.merge(
        routes_df, left_on="trip_route_id", right_on="route_route_id", how="left"
    )

    print(f"Téléchargement du flux temps réel : {GTFS_RT_TRIP_UPDATES_URL}")
    resp = requests.get(GTFS_RT_TRIP_UPDATES_URL, timeout=30)
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    collecte_ts = datetime.datetime.now(tz=PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")

    rows = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip = trip_update.trip

        for stu in trip_update.stop_time_update:
            arr_delay = stu.arrival.delay if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            dep_delay = stu.departure.delay if stu.HasField("departure") and stu.departure.HasField("delay") else None

            has_arr_delay = arr_delay is not None and abs(arr_delay) > DELAY_THRESHOLD_SEC
            has_dep_delay = dep_delay is not None and abs(dep_delay) > DELAY_THRESHOLD_SEC
            if not (has_arr_delay or has_dep_delay):
                continue

            rows.append({
                "collecte_horodatage": collecte_ts,
                "trip_id": trip.trip_id,
                "stop_id_rt": stu.stop_id,
                "arrival_time": fmt_time(stu.arrival.time) if stu.HasField("arrival") and stu.arrival.HasField("time") else "",
                "arrival_delay_sec": arr_delay if arr_delay is not None else "",
                "departure_time": fmt_time(stu.departure.time) if stu.HasField("departure") and stu.departure.HasField("time") else "",
                "departure_delay_sec": dep_delay if dep_delay is not None else "",
            })

    if not rows:
        return pd.DataFrame()

    rt_df = pd.DataFrame(rows)
    full_df = rt_df.merge(trips_df, on="trip_id", how="left")
    full_df = full_df.merge(
        stops_df, left_on="stop_id_rt", right_on="stop_stop_id", how="left"
    )
    return full_df


def main():
    now_paris = datetime.datetime.now(tz=PARIS_TZ)

    if is_in_silence_window(now_paris):
        print(f"Heure actuelle Paris {now_paris.strftime('%H:%M')} : fenêtre de silence, on ne fait rien.")
        return

    ensure_static_gtfs()
    new_df = collect_current_delays()

    os.makedirs(DATA_DIR, exist_ok=True)
    day_str = now_paris.strftime("%Y-%m-%d")
    filepath = os.path.join(DATA_DIR, f"retards_{day_str}.csv")

    if new_df.empty:
        print("Aucun retard actuellement, rien à ajouter.")
        return

    if os.path.exists(filepath):
        existing_df = pd.read_csv(filepath, sep=";", dtype=str)
        combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        combined_df = new_df

    combined_df.to_csv(filepath, index=False, sep=";", encoding="utf-8")
    print(f"✅ {len(new_df)} nouvelles lignes ajoutées à {filepath} (total: {len(combined_df)})")


if __name__ == "__main__":
    main()
