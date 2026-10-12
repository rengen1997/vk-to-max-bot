import json
import os
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Ключи автоматически загружаются из GitHub Secrets
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
MAX_TOKEN = os.environ.get("MAX_TOKEN", "").strip()
MAX_CHAT_ID = int(os.environ.get("MAX_CHAT_ID", "0").strip())

VK_DOMAIN = "lider_kurkino"
VK_API_VERSION = "5.131"
MAX_API_BASE = "https://platform-api2.max.ru"
STATE_FILE = "state.json"


def load_state():
    """Загружает сохранённый номер события ВК (ts) и список отправленных постов."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка чтения {STATE_FILE}: {e}")
    return {"ts": None, "sent_posts": []}


def save_state(ts, sent_posts):
    """Сохраняет текущий номер события ВК (ts) и последние 100 ID постов."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "ts": str(ts),
            "sent_posts": sent_posts[-100:]
        }, f, ensure_ascii=False, indent=2)


def init_vk_group():
    """Получает ID группы ВК и проверяет настройки Long Poll."""
    url = "https://api.vk.com/method/groups.getById"
    params = {
        "group_id": VK_DOMAIN,
        "access_token": VK_TOKEN,
        "v": VK_API_VERSION
    }
    res = requests.get(url, params=params, timeout=15, verify=False).json()
    if "error" in res:
        raise RuntimeError(f"Ошибка VK API: {res['error'].get('error_msg')}")

    groups = res["response"] if isinstance(res["response"], list) else res["response"].get("groups", [])
    group_id = groups[0]["id"]
    group_name = groups[0].get("name", VK_DOMAIN)
    print(f"Подключено к группе ВК: «{group_name}» (ID: {group_id})")

    requests.get("https://api.vk.com/method/groups.setLongPollSettings", params={
        "group_id": group_id,
        "enabled": 1,
        "api_version": VK_API_VERSION,
        "wall_post_new": 1,
        "access_token": VK_TOKEN,
        "v": VK_API_VERSION
    }, timeout=15, verify=False)

    return group_id


def get_vk_longpoll_server(group_id):
    """Получает актуальные server, key и ts от ВКонтакте."""
    url = "https://api.vk.com/method/groups.getLongPollServer"
    params = {
        "group_id": group_id,
        "access_token": VK_TOKEN,
        "v": VK_API_VERSION
    }
    res = requests.get(url, params=params, timeout=15, verify=False).json()
    if "error" in res:
        raise RuntimeError(f"Ошибка Long Poll ВК: {res['error'].get('error_msg')}")
    return res["response"]


def extract_photo_url(photo_obj):
    """Извлекает ссылку на фото максимального качества."""
    sizes = photo_obj.get("sizes", [])
    if sizes:
        best = max(sizes, key=lambda x: x.get("width", 0) * x.get("height", 0))
        return best.get("url") or best.get("src")

    for key in ("photo_2560", "photo_1280", "photo_807", "photo_604", "photo_130"):
        if photo_obj.get(key):
            return photo_obj.get(key)
    return None


def upload_image_to_max(photo_url):
    """Загружает фото на сервер MAX через /uploads."""
    headers = {"Authorization": MAX_TOKEN}
    try:
        img_resp = requests.get(photo_url, timeout=20, verify=False)
        if img_resp.status_code == 200:
            init_resp = requests.post(
                f"{MAX_API_BASE}/uploads",
                params={"type": "image"},
                headers=headers,
                timeout=15,
                verify=False
            )
            if init_resp.status_code == 200:
                upload_url = init_resp.json().get("url")
                if upload_url:
                    files = {"data": ("photo.jpg", img_resp.content, "image/jpeg")}
                    up_resp = requests.post(upload_url, files=files, timeout=30, verify=False)
                    if up_resp.status_code == 200:
                        up_json = up_resp.json()
                        if "token" in up_json:
                            return {"type": "image", "payload": {"token": up_json["token"]}}
                        if "photos" in up_json and isinstance(up_json["photos"], dict):
                            for _, p_data in up_json["photos"].items():
                                if isinstance(p_data, dict) and "token" in p_data:
                                    return {"type": "image", "payload": {"token": p_data["token"]}}
    except Exception as e:
        print(f"Предупреждение при загрузке фото в MAX: {e}")

    return {"type": "image", "payload": {"url": photo_url}}


def parse_post_content(post):
    """Разбирает текст, репосты, картинки, опросы, видео и ссылки из поста ВК."""
    text_parts = []
    if post.get("text"):
        text_parts.append(post.get("text").strip())

    attachments = list(post.get("attachments", []))
    for copy_item in post.get("copy_history", []):
        if copy_item.get("text"):
            text_parts.append(copy_item.get("text").strip())
        attachments.extend(copy_item.get("attachments", []))

    max_attachments = []
    extra_lines = []

    for att in attachments:
        att_type = att.get("type")

        if att_type == "photo":
            photo_url = extract_photo_url(att.get("photo", {}))
            if photo_url and len(max_attachments) < 10:
                max_attachments.append(upload_image_to_max(photo_url))

        elif att_type == "poll":
            poll = att.get("poll", {})
            question = poll.get("question", "Опрос")
            answers = poll.get("answers", [])
            extra_lines.append(f"\n\n📊 Опрос: {question}")
            for ans in answers:
                extra_lines.append(f"\n• {ans.get('text')}")

        elif att_type == "video":
            video = att.get("video", {})
            v_title = video.get("title", "Видеозапись")
            owner_id = video.get("owner_id")
            v_id = video.get("id")
            if owner_id and v_id:
                extra_lines.append(f"\n\n🎬 {v_title}: https://vk.com/video{owner_id}_{v_id}")
            if not max_attachments:
                thumb_url = extract_photo_url(video)
                if thumb_url:
                    max_attachments.append(upload_image_to_max(thumb_url))

        elif att_type == "link":
            link = att.get("link", {})
            l_url = link.get("url")
            l_title = link.get("title", "Ссылка")
            if l_url and l_url not in "\n".join(text_parts):
                extra_lines.append(f"\n\n🔗 {l_title}: {l_url}")

        elif att_type == "doc":
            doc = att.get("doc", {})
            d_title = doc.get("title", "Документ")
            d_url = doc.get("url")
            if d_url:
                extra_lines.append(f"\n\n📎 {d_title}: {d_url}")

    base_text = "\n\n".join([t for t in text_parts if t])
    full_text = (base_text + "".join(extra_lines)).strip()[:4000]
    return full_text, max_attachments


def send_to_max(chat_id, text, max_attachments):
    """Отправляет пост и вложения в канал MAX."""
    url = f"{MAX_API_BASE}/messages"
    params = {"chat_id": chat_id}
    headers = {
        "Authorization": MAX_TOKEN,
        "Content-Type": "application/json"
    }

    payload = {}
    if text.strip():
        payload["text"] = text
    if max_attachments:
        payload["attachments"] = max_attachments
    if not payload:
        payload["text"] = "Новая запись в сообществе ВКонтакте"

    if max_attachments:
        time.sleep(1.5)

    response = requests.post(url, params=params, json=payload, headers=headers, timeout=25, verify=False)
    if response.status_code == 200:
        print("-> Пост успешно опубликован в канале MAX!")
        return True
    else:
        print(f"-> Ошибка отправки в MAX ({response.status_code}): {response.text}")
        return False


def main():
    group_id = init_vk_group()
    lp_data = get_vk_longpoll_server(group_id)
    server = lp_data["server"]
    key = lp_data["key"]
    current_ts = str(lp_data["ts"])

    state = load_state()
    saved_ts = state.get("ts")
    sent_posts = state.get("sent_posts", [])

    # Если это самый первый запуск на GitHub — сохраняем текущую точку отсчёта
    if not saved_ts:
        print(f"Первый запуск: зафиксирован начальный номер событий ВК (ts={current_ts}).")
        save_state(current_ts, sent_posts)
        return

    print(f"Проверка новых постов ВК начиная с события ts={saved_ts}...")
    lp_res = requests.get(
        f"{server}?act=a_check&key={key}&ts={saved_ts}&wait=1",
        timeout=20,
        verify=False
    ).json()

    if "failed" in lp_res:
        print(f"Сервер событий ВК обновил историю (код {lp_res.get('failed')}), синхронизируем ts={current_ts}.")
        save_state(current_ts, sent_posts)
        return

    new_ts = str(lp_res.get("ts", current_ts))
    updates = lp_res.get("updates", [])
    print(f"Получено событий от ВК: {len(updates)}")

    for event in updates:
        if event.get("type") == "wall_post_new":
            post = event.get("object", {})
            post_id = post.get("id")
            post_type = post.get("post_type", "post")

            if post_type == "post" and post_id not in sent_posts:
                print(f"Найден новый пост ВК (ID: {post_id})! Отправка в MAX...")
                text_content, max_atts = parse_post_content(post)
                if send_to_max(MAX_CHAT_ID, text_content, max_atts):
                    sent_posts.append(post_id)

    save_state(new_ts, sent_posts)


if __name__ == "__main__":
    main()
