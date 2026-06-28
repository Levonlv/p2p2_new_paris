# Деплой P2P-бота на VPS (Ubuntu/Debian) через systemd

Инструкция для чистого VPS на Ubuntu 22.04+ / Debian 12+.
Бот ставится в `/opt/p2p-bot` и работает под отдельным пользователем `p2pbot`
как systemd-сервис с автозапуском и автоперезапуском.

> Важно: бот работает через **polling**. Нельзя одновременно держать два
> запущенных экземпляра с одним токеном (на сервере и локально) — будет
> конфликт. Перед запуском на сервере останови локальный.

---

## 0. Что нужно

- VPS с Ubuntu 22.04+ или Debian 12+ (нужен **Python ≥ 3.10**)
- Доступ по SSH с правами `sudo`
- Токен бота от @BotFather
- Свой Telegram ID и ID мерчантов

Проверь версию Python на сервере:
```bash
python3 --version    # должно быть 3.10 или выше
```

---

## 1. Установить системные пакеты

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

---

## 2. Создать пользователя и папку

```bash
sudo useradd --system --create-home --home-dir /opt/p2p-bot --shell /usr/sbin/nologin p2pbot
sudo mkdir -p /opt/p2p-bot
```

---

## 3. Залить файлы проекта

С локальной машины (из папки проекта) скопируй на сервер:

```bash
scp simple_bot.py requirements.txt .env.example p2p-bot.service ВАШ_ЮЗЕР@IP_СЕРВЕРА:/tmp/
```

На сервере перенеси в рабочую папку:
```bash
sudo mv /tmp/simple_bot.py /tmp/requirements.txt /tmp/.env.example /opt/p2p-bot/
sudo mv /tmp/p2p-bot.service /tmp/p2p-bot.service   # оставим в /tmp, поставим на шаге 6
```

---

## 4. Создать виртуальное окружение и поставить зависимости

```bash
sudo python3 -m venv /opt/p2p-bot/venv
sudo /opt/p2p-bot/venv/bin/pip install --upgrade pip
sudo /opt/p2p-bot/venv/bin/pip install -r /opt/p2p-bot/requirements.txt
```

---

## 5. Настроить `.env`

```bash
sudo cp /opt/p2p-bot/.env.example /opt/p2p-bot/.env
sudo nano /opt/p2p-bot/.env
```

Заполни как минимум `BOT_TOKEN` и `ADMIN_IDS`. Проверь, что
`STATE_FILE=/opt/p2p-bot/state.json` (абсолютный путь).

> Формат строгий: `KEY=value`, **без кавычек и пробелов** вокруг `=`.

---

## 6. Назначить права и поставить systemd-сервис

```bash
# Владелец всех файлов — p2pbot (чтобы бот мог писать state.json)
sudo chown -R p2pbot:p2pbot /opt/p2p-bot

# .env не должен читаться посторонними (в нём токен)
sudo chmod 600 /opt/p2p-bot/.env

# Установить unit
sudo cp /tmp/p2p-bot.service /etc/systemd/system/p2p-bot.service
sudo systemctl daemon-reload
sudo systemctl enable p2p-bot      # автозапуск при загрузке сервера
sudo systemctl start p2p-bot       # запустить сейчас
```

---

## 7. Проверить, что бот живой

```bash
sudo systemctl status p2p-bot          # должно быть active (running)
sudo journalctl -u p2p-bot -f          # живой лог; ищи "Bot is running"
```

Healthcheck (только локально на сервере):
```bash
curl http://127.0.0.1:5000/health      # вернёт OK
```

В Telegram напиши боту `/start` — должен ответить.

---

## Обновление бота (новая версия кода)

```bash
# Залить новый simple_bot.py на сервер (в /tmp), затем:
sudo mv /tmp/simple_bot.py /opt/p2p-bot/simple_bot.py
sudo chown p2pbot:p2pbot /opt/p2p-bot/simple_bot.py
sudo systemctl restart p2p-bot
sudo journalctl -u p2p-bot -f
```

Если менялись зависимости — перед рестартом:
```bash
sudo /opt/p2p-bot/venv/bin/pip install -r /opt/p2p-bot/requirements.txt
```

---

## Полезные команды

| Команда | Что делает |
|---------|------------|
| `sudo systemctl status p2p-bot` | Статус сервиса |
| `sudo systemctl restart p2p-bot` | Перезапуск |
| `sudo systemctl stop p2p-bot` | Остановить |
| `sudo journalctl -u p2p-bot -f` | Живой лог |
| `sudo journalctl -u p2p-bot --since "1 hour ago"` | Лог за час |

---

## Бэкап данных

Все заявки, чаты и рейтинги — в одном файле `/opt/p2p-bot/state.json`.
Запись атомарная, при краше не бьётся. Для бэкапа достаточно копировать его:

```bash
sudo cp /opt/p2p-bot/state.json ~/state-backup-$(date +%F).json
```

Можно повесить на cron раз в сутки.
