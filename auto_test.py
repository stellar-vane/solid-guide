import requests
from bs4 import BeautifulSoup
import datetime
import time
import re
import sys
import random
import threading
import functools

print = functools.partial(print, flush=True)

TEST_MODE = True

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 6.2; WOW64; rv:116.0.1) Gecko/20100101 Firefox/116.0.1"
}

WHITELIST_LEAGUES = [
    'чемпионат мира', 'лига чемпионов', 'лига конференций', 'лига европы'
]

MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
    'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
}


def fetch_live_matches():
    url = "https://www.pimpletv.ru/category/football/"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        raise Exception(f"HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, 'html.parser')
    matches = []

    for a in soup.find_all('a', class_=lambda c: c and 'match-item' in c):
        href = a.get('href')
        if not href:
            continue
        if href.startswith('/'):
            href = f"https://www.pimpletv.ru{href}"

        league_div = a.find('div', class_=lambda c: c and 'match-item__title-tournament' in c)
        league = league_div.text.strip() if league_div else "Разное"

        home_span = a.find('span', class_=lambda c: c and 'table-item__home-name' in c)
        away_span = a.find('span', class_=lambda c: c and 'table-item__away-name' in c)

        home_name = home_span.text.strip() if home_span else "Команда 1"
        away_name = away_span.text.strip() if away_span else "Команда 2"

        matches.append({
            "href": href,
            "name": f"{home_name} — {away_name}",
            "league": league
        })

    return matches


def parse_match_datetime_utc(soup):
    date_div = soup.find('div', class_=lambda c: c and 'match-info__date' in c)
    if not date_div:
        return None

    iso = date_div.get('content')
    if iso:
        try:
            dt = datetime.datetime.fromisoformat(iso)
            return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass

    text = date_div.get_text(strip=True)
    m = re.search(r'(\d{1,2})\s+([а-яё]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})', text, re.IGNORECASE)
    if not m:
        return None

    month = MONTHS.get(m.group(2).lower())
    if not month:
        return None

    local_dt = datetime.datetime(int(m.group(3)), month, int(m.group(1)),
                                 int(m.group(4)), int(m.group(5)))
    return local_dt - datetime.timedelta(hours=3)


def check_match_page(match_url):
    try:
        resp = requests.get(match_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        match_dt = parse_match_datetime_utc(soup)

        hash_match = re.search(r'acestream://([a-fA-F0-9]{40})', resp.text, re.IGNORECASE)
        acestream_hash = hash_match.group(1) if hash_match else None

        return {
            "datetime_utc": match_dt,
            "hash": acestream_hash
        }
    except Exception as e:
        print(f"[Ошибка] check_match_page({match_url}): {e}")
        return None


def check_stream(hash_str):
    try:
        init_res = requests.get(f"http://127.0.0.1:6878/ace/getstream?id={hash_str}&format=json", timeout=10)
        init_data = init_res.json()

        if init_data.get("error"):
            return None

        stat_url = init_data.get("response", {}).get("stat_url")
        command_url = init_data.get("response", {}).get("command_url")
        playback_url = init_data.get("response", {}).get("playback_url")
    except Exception:
        return None

    if not stat_url or not playback_url:
        return None

    time.sleep(20)

    try:
        stat_res = requests.get(stat_url, timeout=10)
        stat_data = stat_res.json()
        peers = stat_data.get("response", {}).get("peers", 0)
        return {"peers": peers, "playbackUrl": playback_url, "commandUrl": command_url}
    except Exception:
        return None


def consume_stream(url, stop_event):
    try:
        with requests.get(url, stream=True, timeout=10) as r:
            for chunk in r.iter_content(chunk_size=8192):
                if stop_event.is_set():
                    break
    except Exception:
        pass


def main():
    while True:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

        is_working_hours = now.hour >= 20 or now.hour < 2

        if not is_working_hours and not TEST_MODE:
            print("Смена окончена (вне окна 20:00 - 02:00 GMT). Завершаем работу.")
            sys.exit(0)

        if now.hour >= 20:
            exit_time = (now + datetime.timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
        else:
            exit_time = now.replace(hour=2, minute=0, second=0, microsecond=0)

        print("--- Новый цикл поиска ---")

        try:
            matches = fetch_live_matches()
        except Exception as e:
            print("Ошибка при получении данных с сайта. Ждем 5 минут...")
            time.sleep(5 * 60)
            continue

        filtered_matches = []
        for m in matches:
            l = m['league'].lower()
            if any(wl in l for wl in WHITELIST_LEAGUES):
                filtered_matches.append(m)

        print(f"Всего подходящих матчей из Whitelist: {len(filtered_matches)}")

        active_matches = []
        upcoming_matches = []

        for match in filtered_matches:
            match_info = check_match_page(match['href'])
            if not match_info:
                print(f"[Отказ] {match['name']}: не удалось загрузить страницу матча.")
                continue

            match_time = match_info["datetime_utc"]
            if not match_time:
                print(f"[Отказ] {match['name']}: не удалось распарсить время на странице.")
                continue

            match['hash'] = match_info["hash"]
            match['matchTimeObj'] = match_time

            msk_time = match_time + datetime.timedelta(hours=3)
            time_str_msk = f"{msk_time.hour:02d}:{msk_time.minute:02d}"

            if match_time > exit_time and not TEST_MODE:
                print(f"[Отказ по времени] {match['name']}: Старт в {time_str_msk} (МСК). Не попадает в смену до 02:00 GMT.")
                continue

            diff_minutes = (now - match_time).total_seconds() / 60

            if diff_minutes > 120:
                print(f"[Отказ по времени] {match['name']}: Старт был {round(diff_minutes)} мин. назад. Матч уже закончился.")
            elif -15 <= diff_minutes <= 120:
                active_matches.append(match)
            elif diff_minutes < -15:
                upcoming_matches.append(match)

        print(f"Итог: Активных: {len(active_matches)}, Будущих: {len(upcoming_matches)}")

        needy_matches = []

        for match in active_matches:
            if not match.get('hash'):
                print(f"[Отказ] {match['name']}: хеш не найден на странице.")
                continue

            stream_info = check_stream(match['hash'])
            if not stream_info:
                print(f"[Отказ] {match['name']}: ошибка инициализации потока.")
                continue

            peers = stream_info['peers']
            if peers == 0:
                print(f"[Отказ] {match['name']}: мертвая трансляция (0 пиров).")
                if stream_info['commandUrl']:
                    try:
                        requests.get(f"{stream_info['commandUrl']}?method=stop", timeout=5)
                    except:
                        pass
            elif peers > 30:
                print(f"[Отказ] {match['name']}: людей достаточно ({peers} пиров).")
                if stream_info['commandUrl']:
                    try:
                        requests.get(f"{stream_info['commandUrl']}?method=stop", timeout=5)
                    except:
                        pass
            else:
                print(f"[Подходит] {match['name']}: нуждается в помощи ({peers} пиров).")
                needy_matches.append({"match": match, "streamInfo": stream_info})

        if len(needy_matches) > 0:
            selected = random.choice(needy_matches)

            print(f"*** Выбрана случайная трансляция: {selected['match']['name']} ***")

            for nm in needy_matches:
                if nm != selected and nm['streamInfo']['commandUrl']:
                    try:
                        requests.get(f"{nm['streamInfo']['commandUrl']}?method=stop", timeout=5)
                    except:
                        pass

            print(f"Раздаем трафик для: {selected['match']['name']} (30 минут)...")

            stop_event = threading.Event()
            t = threading.Thread(target=consume_stream, args=(selected['streamInfo']['playbackUrl'], stop_event))
            t.start()

            time.sleep(30 * 60)

            stop_event.set()
            t.join()

            if selected['streamInfo']['commandUrl']:
                try:
                    requests.get(f"{selected['streamInfo']['commandUrl']}?method=stop", timeout=5)
                except:
                    pass

            continue

        if len(active_matches) > 0:
            print("Активные матчи идут, но помощь никому не нужна (или нет хешей). Ждем 5 минут...")
            time.sleep(5 * 60)
            continue

        if len(upcoming_matches) > 0:
            upcoming_matches.sort(key=lambda x: x['matchTimeObj'])
            next_match = upcoming_matches[0]
            wait_seconds = (next_match['matchTimeObj'] - now).total_seconds()

            sleep_for = min(max(wait_seconds, 0), 5 * 60)
            print(f"До старта матча {next_match['name']} ещё {round(wait_seconds / 60)} мин. "
                  f"Спим {round(sleep_for / 60)} мин и перепроверяем...")
            time.sleep(sleep_for)
            continue

        if TEST_MODE:
            print("ТЕСТ РЕЖИМ: Матчей нет, ждем 5 минут...")
            time.sleep(5 * 60)
            continue

        print("Нет активных и будущих матчей до 02:00 GMT. Завершаем работу.")
        sys.exit(0)


if __name__ == "__main__":
    main()
