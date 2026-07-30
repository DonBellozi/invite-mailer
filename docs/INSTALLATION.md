# Установка Invite Mailer

## Требования

- Docker Engine и Docker Compose либо Portainer;
- доступ к Zimbra по IMAP и SMTP;
- постоянный каталог на хосте для рабочих данных;
- при использовании Indigo – сетевой доступ к PostgreSQL и учетная запись только для чтения.

## Каталоги

Рекомендуемая структура на хосте:

```text
/opt/invite-mailer
/opt/invite-mailer-storage
├── data
├── state
└── reports
```

Создание каталогов:

```bash
mkdir -p /opt/invite-mailer
mkdir -p /opt/invite-mailer-storage/{data,state,reports}
cd /opt/invite-mailer
```

## Подготовка окружения

```bash
cp .env.example .env
```

Заполните обязательные параметры IMAP, начальные параметры SMTP, адрес публикации отчета, `WORKER_HASH_SECRET`, `SESSION_SECRET` и учетную запись локального администратора.

Для `WORKER_HASH_SECRET` и `SESSION_SECRET` используйте разные длинные случайные строки.

## Запуск

```bash
docker compose up -d --build
```

Проверка:

```bash
docker compose ps
docker compose logs --tail=100 invite-mailer
docker compose logs --tail=100 invite-mailer-admin
```

Должны работать контейнеры:

```text
invite-mailer
invite-mailer-admin
invite-mailer-report
```

## Первая проверка

В контейнере `invite-mailer`:

```bash
python -m app.main fetch
python -m app.main run --dry-run --skip-fetch
python -m app.main indigo-sync
```

Последнюю команду выполняйте только после настройки Indigo.

## Portainer

При использовании Portainer переменные из `.env` можно задать в разделе Environment variables стека. Каталог `STORAGE_ROOT` должен существовать на Docker-хосте и быть доступен контейнерам на запись.
