<div align="center">

<p><a href="README_zh.md">中文</a> &nbsp;/&nbsp; <a href="README.md">English</a> &nbsp;/&nbsp; <strong>Русский</strong></p>

<h1>LivingMemory</h1>

<p><strong>Долговременная память AstrBot с отслеживаемыми источниками, тематической организацией и управляемым жизненным циклом.</strong></p>

<p>
  <a href="https://github.com/Eco404/astrbot_plugin_livingmemory/releases"><img src="https://img.shields.io/github/v/release/Eco404/astrbot_plugin_livingmemory?style=flat-square&color=187b78" alt="Последний релиз"></a>
  <img src="https://img.shields.io/badge/Python-3.10%2B-e8f2f1?style=flat-square&labelColor=264642" alt="Python 3.10 или новее">
  <img src="https://img.shields.io/badge/AstrBot-%3E%3D%204.24.2-f3eee4?style=flat-square&labelColor=544c3d" alt="AstrBot 4.24.2 или новее">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-f2e8e5?style=flat-square&labelColor=5b403a" alt="Лицензия AGPL-3.0"></a>
</p>

<img src="docs/assets/images/architecture-overview-en.svg" width="100%" alt="Архитектура памяти LivingMemory">

</div>

## Два уровня памяти

| Timeline | Topic | Recall |
| :--- | :--- | :--- |
| Сохраняет хронологический опыт, факты, эмоции, время, участников и снимки источников. | Объединяет связанные формальные фрагменты без потери связи с исходной Timeline. | Выбирает Topic по текущему запросу, добавляет только полезные факты и при необходимости возвращается к Timeline. |

## Основные возможности

- Редактируемая Timeline как источник и автоматически поддерживаемая Topic как производный слой.
- Полная трассировка фактов, участников, эмоций, фрагментов и ревизий.
- Полная и ограниченная инкрементальная сборка Topic с атомарной публикацией и контрольными точками.
- Поиск Topic по ключевым словам в заголовке, сводке и фактах либо по семантической близости Embedding.
- Продолжение активной темы после базового числа реплик с обязательным верхним пределом суммаризации.
- Опциональный Rerank через AstrBot Provider или встроенный клиент Cloudflare Workers AI.
- Единый центр обслуживания: реконструкция, проверка, аудит сессий, диагностика базы данных, история вызовов и тесты моделей.
- Инструменты агента `recall_long_term_memory` и `memorize_long_term_memory`.

## Быстрый старт

1. Установите LivingMemory из AstrBot Plugin Market либо через URL репозитория или ZIP в WebUI.
2. Перезагрузите AstrBot и выберите LLM и Embedding Provider. Пустой ID использует модель AstrBot по умолчанию.
3. Проверьте создание Timeline и модели, затем включите Topic и запустите первую полную сборку.

Рабочее пространство открывается через `Plugins -> LivingMemory -> Pages -> dashboard`. WebUI адаптирован для настольных и мобильных экранов; на телефоне страницы переключаются кнопкой навигации в левом верхнем углу. Для Plugin Pages требуется **AstrBot 4.24.2 или новее**.

Подробная документация доступна на [английском](https://eco404.github.io/astrbot_plugin_livingmemory/en/) и [китайском](https://eco404.github.io/astrbot_plugin_livingmemory/).

## Проект

[Документация](https://eco404.github.io/astrbot_plugin_livingmemory/en/) · [Релизы](https://github.com/Eco404/astrbot_plugin_livingmemory/releases) · [История изменений](CHANGELOG.md) · [Проблемы](https://github.com/Eco404/astrbot_plugin_livingmemory/issues)

LivingMemory распространяется по лицензии [AGPL-3.0](LICENSE).
