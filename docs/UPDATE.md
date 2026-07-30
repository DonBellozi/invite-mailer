# Обновление Invite Mailer

## Перед обновлением

До появления встроенной системы резервного копирования остановите контейнеры и создайте копию рабочего каталога:

```bash
cd /opt/invite-mailer
docker compose down
cp -a /opt/invite-mailer-storage /opt/invite-mailer-storage.backup-$(date +%Y%m%d-%H%M%S)
```

Особенно важен файл:

```text
/opt/invite-mailer-storage/state/invite_mailer.sqlite
```

Не копируйте активную SQLite во время записи без остановки контейнеров.

## Установка пакета измененных файлов

Архив обновления содержит только полностью измененные или новые файлы с сохраненной структурой каталогов.

```bash
cd /opt/invite-mailer
unzip -o invite-mailer-vX.Y.Z-changed-files.zip
```

Файлы, которые требуется удалить, перечисляются отдельно в сообщении к конкретному обновлению.

## Пересборка

```bash
docker compose up -d --build
```

Проверка:

```bash
docker compose ps
docker compose logs --tail=100 invite-mailer
docker compose logs --tail=100 invite-mailer-admin
```

## Проверка после обновления

```bash
docker exec -it invite-mailer python -m app.main report
docker exec -it invite-mailer python -m app.main run --dry-run --skip-fetch
```

Также проверьте:

- вход в административный раздел;
- открытие общих настроек;
- сохранение SMTP и Indigo;
- построение отчета;
- отсутствие ошибок миграции SQLite.

## Откат

```bash
cd /opt/invite-mailer
docker compose down
rm -rf /opt/invite-mailer-storage
mv /opt/invite-mailer-storage.backup-ДАТА /opt/invite-mailer-storage
```

После возврата исходных файлов проекта:

```bash
docker compose up -d --build
```


## После обновления до 2.3.11

Откройте «Настройки» – «Общие настройки» и проверьте блок получения данных по IMAP. При первом запуске версии существующие значения переносятся в SQLite. После проверки можно удалить устаревшие файлы `README_UPDATE.md` и `README_INDIGO_UPDATE.md`.
