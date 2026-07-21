/* global localStorage, URLSearchParams, CustomEvent */

(() => {
  const LANG_KEY = "lmem_lang";
  const SUPPORTED = ["zh", "en", "ru"];
  let urlLanguageOverride = false;

  const MSG = {
    /* ---- Common ---- */
    "common.close":       { zh: "关闭", en: "Close", ru: "Закрыть" },
    "common.cancel":      { zh: "取消", en: "Cancel", ru: "Отмена" },
    "common.clear":       { zh: "清空", en: "Clear", ru: "Очистить" },
    "common.save":        { zh: "保存", en: "Save", ru: "Сохранить" },
    "common.refresh":     { zh: "刷新", en: "Refresh", ru: "Обновить" },
    "common.search":      { zh: "搜索", en: "Search", ru: "Поиск" },
    "common.confirm":     { zh: "确定", en: "Confirm", ru: "Подтвердить" },
    "common.loading":     { zh: "加载中...", en: "Loading...", ru: "Загрузка..." },
    "common.noData":      { zh: "暂无数据", en: "No data", ru: "Нет данных" },
    "common.unavailable": { zh: "暂不可用", en: "Unavailable", ru: "Недоступно" },
    "common.page":        { zh: "第 {0} / {1} 页 · 共 {2} 条", en: "Page {0}/{1} · {2} total", ru: "Стр. {0}/{1} · всего {2}" },
    "common.perPage":     { zh: "每页", en: "Per page", ru: "На стр." },
    "common.perPage20":   { zh: "20 条/页", en: "20 per page", ru: "20 на стр." },
    "common.perPage50":   { zh: "50 条/页", en: "50 per page", ru: "50 на стр." },
    "common.perPage100":  { zh: "100 条/页", en: "100 per page", ru: "100 на стр." },

    /* ---- Session picker ---- */
    "sessionPicker.open": { zh: "分层筛选", en: "Filter", ru: "Фильтр" },
    "sessionPicker.title": { zh: "分层选择会话", en: "Choose a session", ru: "Выбор сессии" },
    "sessionPicker.hint": { zh: "按机器人账号、聊天类型、更新时间和聊天对象逐层筛选；原文本框仍可自由填写。", en: "Filter by bot account, chat type, update time, then target. Free-text input remains available.", ru: "Фильтруйте по аккаунту бота, типу чата, времени и собеседнику. Ручной ввод сохраняется." },
    "sessionPicker.platform": { zh: "机器人主体账号", en: "Bot account", ru: "Аккаунт бота" },
    "sessionPicker.chatType": { zh: "群聊还是私聊", en: "Chat type", ru: "Тип чата" },
    "sessionPicker.updatedAfter": { zh: "最近更新时间起点（可空）", en: "Updated after (optional)", ru: "Обновлено после (необязательно)" },
    "sessionPicker.updatedBefore": { zh: "最近更新时间终点（可空）", en: "Updated before (optional)", ru: "Обновлено до (необязательно)" },
    "sessionPicker.targetSearch": { zh: "搜索聊天对象", en: "Search target", ru: "Поиск собеседника" },
    "sessionPicker.targetSearchPh": { zh: "输入群号、用户 ID 或关键字", en: "Group ID, user ID, or keyword", ru: "ID группы, пользователя или ключевое слово" },
    "sessionPicker.target": { zh: "聊天对象", en: "Chat target", ru: "Собеседник" },
    "sessionPicker.choosePlatform": { zh: "先选择机器人主体账号", en: "Choose a bot account first", ru: "Сначала выберите аккаунт бота" },
    "sessionPicker.chooseChatType": { zh: "再选择群聊或私聊", en: "Then choose chat type", ru: "Затем выберите тип чата" },
    "sessionPicker.chooseTarget": { zh: "最后选择聊天对象", en: "Finally choose a target", ru: "Наконец выберите собеседника" },
    "sessionPicker.group": { zh: "群聊", en: "Group chat", ru: "Групповой чат" },
    "sessionPicker.private": { zh: "私聊", en: "Private chat", ru: "Личный чат" },
    "sessionPicker.other": { zh: "其他", en: "Other", ru: "Другое" },
    "sessionPicker.resultCount": { zh: "匹配 {0} 个会话", en: "{0} sessions matched", ru: "Найдено сессий: {0}" },
    "sessionPicker.apply": { zh: "使用此会话", en: "Use session", ru: "Использовать" },
    "sessionPicker.chooseTargetFirst": { zh: "请先选择聊天对象", en: "Choose a chat target first", ru: "Сначала выберите собеседника" },
    "sessionPicker.loadFailed": { zh: "会话目录加载失败", en: "Failed to load sessions", ru: "Не удалось загрузить сессии" },
    "sessionPicker.manualStillAvailable": { zh: "目录暂不可用，仍可关闭窗口后手动填写。", en: "Catalog unavailable; close this dialog to enter a session manually.", ru: "Каталог недоступен; закройте окно и введите сессию вручную." },

    /* ---- Title / Header ---- */
    "page.title":         { zh: "LivingMemory 控制台", en: "LivingMemory Console", ru: "Консоль LivingMemory" },
    "header.title":       { zh: "LivingMemory 管理面板", en: "LivingMemory Dashboard", ru: "Панель LivingMemory" },
    "header.subtitle":    { zh: "长期记忆与会话管理 · 基于混合检索的智能记忆系统", en: "Long-term memory & session management · Hybrid retrieval system", ru: "Долгосрочная память и управление сессиями · Гибридная поисковая система" },
    "header.theme":       { zh: "切换主题", en: "Toggle theme", ru: "Сменить тему" },
    "header.lang":        { zh: "语言", en: "Language", ru: "Язык" },
    "language.current.zh": { zh: "中文", en: "Chinese", ru: "Китайский" },
    "language.current.en": { zh: "英文", en: "English", ru: "Английский" },
    "language.current.ru": { zh: "俄文", en: "Russian", ru: "Русский" },
    "language.toast":     { zh: "语言：{0}", en: "Language: {0}", ru: "Язык: {0}" },

    /* ---- Navigation ---- */
    "nav.memory":         { zh: "记忆管理", en: "Memory", ru: "Память" },
    "nav.topic":          { zh: "Topic 记忆", en: "Topic Memory", ru: "Тематическая память" },
    "nav.graph":          { zh: "知识图谱", en: "Knowledge Graph", ru: "Граф знаний" },
    "nav.recallTest":     { zh: "召回测试", en: "Recall Test", ru: "Тест поиска" },
    "nav.system":         { zh: "系统概览", en: "System", ru: "Система" },
    "nav.recall":         { zh: "召回测试", en: "Recall Test", ru: "Тест поиска" },
    "nav.models":         { zh: "模型测试", en: "Model Test", ru: "Тест моделей" },
    "nav.identities":     { zh: "人物资料", en: "Identity Profiles", ru: "Профили людей" },

    /* ---- Authoritative identities ---- */
    "identity.introTitle": { zh: "权威人物资料", en: "Authoritative identity profiles", ru: "Авторитетные профили людей" },
    "identity.intro": { zh: "使用平台与稳定账号 ID 约束 Timeline 和 Topic 生成中的人物身份与代词。保存后立即生效。", en: "Use platform and stable account IDs to constrain identities and pronouns in Timeline and Topic generation. Changes take effect immediately.", ru: "Используйте платформу и стабильный ID аккаунта для идентичности и местоимений в Timeline и Topic. Изменения применяются сразу." },
    "identity.warning": { zh: "资料只用于校正人物指代，不会自动新增为记忆事实。Topic 构建期间不能保存，以免同一任务使用两版资料。", en: "Profiles only correct references to people; they are not added as memory facts. Saving is blocked during a Topic build to keep one profile version per build.", ru: "Профили только уточняют ссылки на людей и не добавляются как факты памяти. Во время сборки Topic сохранение заблокировано." },
    "identity.add": { zh: "添加人物", en: "Add profile", ru: "Добавить профиль" },
    "identity.remove": { zh: "移除", en: "Remove", ru: "Удалить" },
    "identity.newProfile": { zh: "新人物资料", en: "New profile", ru: "Новый профиль" },
    "identity.unsaved": { zh: "有未保存的修改", en: "Unsaved changes", ru: "Есть несохранённые изменения" },
    "identity.unsavedFirst": { zh: "请先保存当前修改；重新加载页面可放弃修改", en: "Save the current changes first; reload the page to discard them", ru: "Сначала сохраните изменения; перезагрузите страницу, чтобы отменить их" },
    "identity.empty": { zh: "尚未配置人物资料。点击“添加人物”开始。", en: "No identity profiles yet. Select Add profile to begin.", ru: "Профили пока не настроены. Нажмите «Добавить профиль»." },
    "identity.platform": { zh: "平台", en: "Platform", ru: "Платформа" },
    "identity.platformPh": { zh: "例如 qq、qq_official、discord", en: "e.g. qq, qq_official, discord", ru: "например qq, qq_official, discord" },
    "identity.userId": { zh: "稳定账号 ID（必填）", en: "Stable account ID (required)", ru: "Стабильный ID аккаунта (обязательно)" },
    "identity.userIdPh": { zh: "平台提供的 sender ID，不要填写昵称", en: "Platform sender ID, not a nickname", ru: "ID отправителя платформы, не псевдоним" },
    "identity.displayName": { zh: "显示名", en: "Display name", ru: "Отображаемое имя" },
    "identity.displayNamePh": { zh: "例如 空雨", en: "e.g. 空雨", ru: "например 空雨" },
    "identity.aliases": { zh: "别名", en: "Aliases", ru: "Псевдонимы" },
    "identity.aliasesPh": { zh: "多个别名用逗号分隔", en: "Separate aliases with commas", ru: "Разделяйте псевдонимы запятыми" },
    "identity.gender": { zh: "性别/身份表述", en: "Gender / identity wording", ru: "Пол / формулировка идентичности" },
    "identity.genderPh": { zh: "例如 男性；可留空", en: "e.g. male; optional", ru: "например мужской; необязательно" },
    "identity.pronouns": { zh: "代词", en: "Pronouns", ru: "Местоимения" },
    "identity.pronounsPh": { zh: "例如 他, 他的", en: "e.g. he, him", ru: "например он, его" },
    "identity.notes": { zh: "补充事实", en: "Additional facts", ru: "Дополнительные факты" },
    "identity.notesPh": { zh: "只填写稳定的人物事实，不要填写提示指令", en: "Stable identity facts only; do not enter prompt instructions", ru: "Только стабильные факты; не вводите инструкции" },
    "identity.userIdRequired": { zh: "第 {0} 条人物资料缺少稳定账号 ID", en: "Profile {0} is missing a stable account ID", ru: "В профиле {0} отсутствует стабильный ID" },
    "identity.duplicate": { zh: "第 {0} 条人物资料的平台与账号 ID 重复", en: "Profile {0} duplicates a platform/account ID", ru: "Профиль {0} повторяет платформу и ID" },
    "identity.saved": { zh: "人物资料已保存并立即生效", en: "Identity profiles saved and active", ru: "Профили сохранены и применены" },
    "identity.loadFailed": { zh: "人物资料加载失败", en: "Failed to load identity profiles", ru: "Не удалось загрузить профили" },
    "identity.saveFailed": { zh: "人物资料保存失败", en: "Failed to save identity profiles", ru: "Не удалось сохранить профили" },
    "identity.fileError": { zh: "资料文件读取失败：{0}。保存将用当前页面内容覆盖该文件。", en: "Profile file could not be read: {0}. Saving will replace it with the current page contents.", ru: "Не удалось прочитать файл профилей: {0}. Сохранение заменит его текущими данными." },

    /* ---- Model test ---- */
    "models.introTitle": { zh: "LivingMemory 当前模型", en: "Current LivingMemory models", ru: "Текущие модели LivingMemory" },
    "models.intro": { zh: "这里展示插件运行时实际使用的模型。测试连接会发起一次真实模型请求，可能产生少量用量和费用。", en: "These are the models actually used by the plugin at runtime. A connection test makes a real request and may incur a small usage charge.", ru: "Показаны модели, реально используемые плагином. Проверка выполняет настоящий запрос и может повлечь небольшие расходы." },
    "models.role.llm": { zh: "语言模型（LLM）", en: "Language model (LLM)", ru: "Языковая модель (LLM)" },
    "models.role.embedding": { zh: "向量模型（Embedding）", en: "Embedding model", ru: "Модель эмбеддингов" },
    "models.role.rerank": { zh: "重排序模型（Rerank）", en: "Rerank model", ru: "Модель реранжирования" },
    "models.available": { zh: "可用", en: "Available", ru: "Доступно" },
    "models.unavailable": { zh: "未启用", en: "Not enabled", ru: "Не включено" },
    "models.configurationError": { zh: "配置异常", en: "Configuration error", ru: "Ошибка конфигурации" },
    "models.noModel": { zh: "未配置模型", en: "No model configured", ru: "Модель не настроена" },
    "models.notSpecified": { zh: "留空（自动选择）", en: "Empty (automatic)", ru: "Пусто (автоматически)" },
    "models.selection": { zh: "选择方式", en: "Selection", ru: "Способ выбора" },
    "models.selection.explicit": { zh: "插件显式配置", en: "Explicit plugin setting", ru: "Явная настройка плагина" },
    "models.selection.cloudflare": { zh: "插件内置 Cloudflare", en: "Built-in Cloudflare", ru: "Встроенный Cloudflare" },
    "models.selection.astrbot_default": { zh: "AstrBot 默认回退", en: "AstrBot default fallback", ru: "Модель AstrBot по умолчанию" },
    "models.selection.fallback": { zh: "配置不可用，已回退", en: "Configured model unavailable; fallback used", ru: "Настройка недоступна, используется резерв" },
    "models.selection.unavailable": { zh: "当前不可用", en: "Currently unavailable", ru: "Сейчас недоступно" },
    "models.selection.vector_only": { zh: "未配置（使用向量路径）", en: "Not configured (vector path)", ru: "Не настроено (векторный поиск)" },
    "models.configuredProvider": { zh: "配置 Provider ID", en: "Configured provider ID", ru: "Настроенный Provider ID" },
    "models.actualProvider": { zh: "实际 Provider ID", en: "Actual provider ID", ru: "Фактический Provider ID" },
    "models.providerType": { zh: "Provider 类型", en: "Provider type", ru: "Тип Provider" },
    "models.modelName": { zh: "模型名称", en: "Model name", ru: "Имя модели" },
    "models.runtimeClass": { zh: "运行时实现", en: "Runtime implementation", ru: "Реализация" },
    "models.dimension": { zh: "向量维度", en: "Vector dimension", ru: "Размерность вектора" },
    "models.baseUrl": { zh: "API 地址", en: "API base URL", ru: "Адрес API" },
    "models.fallbackProvider": { zh: "失败回退 Provider", en: "Failure fallback provider", ru: "Резервный Provider" },
    "models.initializationError": { zh: "初始化错误", en: "Initialization error", ru: "Ошибка инициализации" },
    "models.accountConfigured": { zh: "Account ID", en: "Account ID", ru: "Account ID" },
    "models.credentialSource": { zh: "凭据来源", en: "Credential source", ru: "Источник учётных данных" },
    "models.credential.configuration": { zh: "插件配置", en: "Plugin configuration", ru: "Настройки плагина" },
    "models.credential.environment": { zh: "环境变量", en: "Environment variable", ru: "Переменная окружения" },
    "models.credential.missing": { zh: "未提供", en: "Missing", ru: "Отсутствует" },
    "models.yes": { zh: "已填写", en: "Configured", ru: "Настроен" },
    "models.no": { zh: "未填写", en: "Missing", ru: "Не настроен" },
    "models.testConnection": { zh: "测试连接", en: "Test connection", ru: "Проверить подключение" },
    "models.testing": { zh: "测试中...", en: "Testing...", ru: "Проверка..." },
    "models.testingHint": { zh: "正在发送真实模型请求，请稍候。", en: "Sending a real model request...", ru: "Выполняется реальный запрос..." },
    "models.testSuccess": { zh: "连接成功 · {0} ms", en: "Connected · {0} ms", ru: "Подключено · {0} мс" },
    "models.testSuccessToast": { zh: "{0} 连接测试成功", en: "{0} connection test passed", ru: "Проверка {0} успешна" },
    "models.testFailed": { zh: "连接测试失败", en: "Connection test failed", ru: "Ошибка подключения" },
    "models.loadFailed": { zh: "模型信息加载失败", en: "Failed to load model information", ru: "Не удалось загрузить модели" },
    "models.resultDimension": { zh: "维度 {0}", en: "dimension {0}", ru: "размерность {0}" },
    "models.resultCount": { zh: "返回 {0} 项", en: "{0} results", ru: "результатов: {0}" },
    "models.resultScore": { zh: "最高分 {0}", en: "top score {0}", ru: "макс. оценка {0}" },

    /* ---- Topic memory ---- */
    "topic.space":        { zh: "选择记忆空间", en: "Select memory space", ru: "Выберите пространство памяти" },
    "topic.fullBuild":    { zh: "全量构建", en: "Full build", ru: "Полная сборка" },
    "topic.fullBuildConfirmTitle": { zh: "确认重新全量构建", en: "Confirm full rebuild", ru: "Подтвердить полную пересборку" },
    "topic.fullBuildConfirmWarning": { zh: "这是高耗时操作，会重新调用 LLM、Embedding 和 Rerank。", en: "This is an expensive operation and will call the LLM, embedding model, and reranker again.", ru: "Это ресурсоёмкая операция: LLM, модель эмбеддингов и reranker будут вызваны снова." },
    "topic.fullBuildConfirmMessage": { zh: "记忆空间 {1} 当前已有 {0} 条 Topic。确定要重新扫描全部 Timeline 吗？", en: "Memory space {1} currently contains {0} topics. Rescan every Timeline memory?", ru: "В пространстве {1} сейчас {0} тем. Повторно просканировать всю память Timeline?" },
    "topic.fullBuildModeLabel": { zh: "全量构建方式", en: "Full build mode", ru: "Режим полной сборки" },
    "topic.fullBuildPreserveTitle": { zh: "保留现有 Topic 后重新构建", en: "Rebuild while preserving topics", ru: "Пересобрать с сохранением тем" },
    "topic.fullBuildPreserveDetail": { zh: "匹配到的 Topic 保留 ID 并更新版本；新主题会新建，本轮未生成的旧 Topic 会归档。", en: "Matched topics keep their IDs and receive new revisions. New topics are created, while old topics absent from the completed rebuild are archived.", ru: "Совпавшие темы сохранят ID и получат новую версию. Новые темы будут созданы, отсутствующие — архивированы." },
    "topic.fullBuildClearTitle": { zh: "清空后全量构建", en: "Clear and rebuild from scratch", ru: "Очистить и пересобрать заново" },
    "topic.fullBuildClearDetail": { zh: "先永久删除该空间的全部 Topic、原子、来源、索引及旧构建断点，再像首次使用一样重新构建。", en: "Permanently remove this space's topics, atoms, provenance, links, and prior build checkpoints, then rebuild as if this were the first run.", ru: "Удалить темы, атомы, источники, связи и старые контрольные точки этого пространства, затем выполнить первую сборку заново." },
    "topic.fullBuildClearRisk": { zh: "清空不可撤销；如果随后构建失败，旧 Topic 不会自动恢复。", en: "Clearing cannot be undone. If the subsequent build fails, the old topics will not be restored automatically.", ru: "Очистку нельзя отменить. Если последующая сборка завершится ошибкой, старые темы не восстановятся автоматически." },
    "topic.fullBuildConfirmSubmit": { zh: "仍然全量构建", en: "Rebuild anyway", ru: "Всё равно пересобрать" },
    "topic.fullBuildClearSubmit": { zh: "清空并开始构建", en: "Clear and start building", ru: "Очистить и начать сборку" },
    "topic.resetBuildStarted": { zh: "Topic 已进入清空并从零构建流程，请查看上方进度", en: "Topics are being cleared and rebuilt from scratch; see progress above", ru: "Темы очищаются и пересобираются с нуля; следите за прогрессом выше" },
    "topic.maintenance":  { zh: "维护", en: "Maintenance", ru: "Обслуживание" },
    "topic.settings": { zh: "参数", en: "Settings", ru: "Параметры" },
    "topic.settingsTitle": { zh: "Topic 参数", en: "Topic settings", ru: "Параметры Topic" },
    "topic.settingsNotice": { zh: "召回参数保存后立即生效；构建参数只影响之后的新任务，不会自动改写已有 Topic。恢复默认会删除覆盖值，从而自动跟随代码中的最新默认值。", en: "Recall settings apply immediately. Build settings affect only new tasks. Resetting removes the override so future code defaults apply automatically.", ru: "Параметры поиска применяются сразу; параметры сборки — только к новым задачам. Сброс удаляет переопределение и включает актуальные значения по умолчанию." },
    "topic.resetAllDefaults": { zh: "全部恢复默认", en: "Reset all defaults", ru: "Сбросить всё" },
    "topic.resetDefault": { zh: "恢复默认", en: "Reset", ru: "Сбросить" },
    "topic.defaultValue": { zh: "默认", en: "Default", ru: "По умолчанию" },
    "topic.customValue": { zh: "自定义", en: "Custom", ru: "Пользовательское" },
    "topic.codeDefault": { zh: "代码默认", en: "Code default", ru: "Значение кода" },
    "topic.settingsCategory.recall": { zh: "召回策略", en: "Recall", ru: "Поиск" },
    "topic.settingsCategory.build": { zh: "构建与归并", en: "Build and grouping", ru: "Сборка и группировка" },
    "topic.settingsCategory.performance": { zh: "性能与容错", en: "Performance and resilience", ru: "Производительность" },
    "topic.settingsBuildActive": { zh: "Topic 构建正在运行，任务结束后才能修改参数。", en: "A Topic build is running. Settings can be changed after it finishes.", ru: "Выполняется сборка Topic. Изменение параметров временно недоступно." },
    "topic.settingsSaved": { zh: "Topic 参数已保存并应用", en: "Topic settings saved and applied", ru: "Параметры Topic сохранены" },
    "topic.settingsLoadFailed": { zh: "Topic 参数加载失败", en: "Failed to load Topic settings", ru: "Не удалось загрузить параметры Topic" },
    "topic.settingsSaveFailed": { zh: "Topic 参数保存失败", en: "Failed to save Topic settings", ru: "Не удалось сохранить параметры Topic" },
    "topic.invalidSetting": { zh: "参数值无效", en: "Invalid value", ru: "Недопустимое значение" },
    "topic.maintenanceTitle": { zh: "Topic 维护", en: "Topic maintenance", ru: "Обслуживание Topic" },
    "topic.maintenanceIntro": { zh: "检查尚未被活跃 Topic 以当前版本索引的 Timeline，确认后仅补建所选条目。", en: "Find active Timeline revisions not indexed by any active Topic, then build only the selected entries.", ru: "Найти активные версии Timeline без индекса в активных Topic и обработать только выбранные записи." },
    "topic.detectUnindexed": { zh: "重新检查", en: "Check again", ru: "Проверить снова" },
    "topic.detectingUnindexed": { zh: "正在检查未索引 Timeline…", en: "Checking for unindexed Timelines…", ru: "Поиск Timeline без индекса…" },
    "topic.unindexedDetected": { zh: "发现 {0} 条未索引 Timeline", en: "Found {0} unindexed Timelines", ru: "Найдено Timeline без индекса: {0}" },
    "topic.noUnindexed": { zh: "所有活跃 Timeline 的当前版本均已被 Topic 索引，无需补建。", en: "Every active Timeline revision is already indexed by a Topic. No maintenance is needed.", ru: "Все активные версии Timeline уже проиндексированы Topic. Обслуживание не требуется." },
    "topic.selectAllUnindexed": { zh: "全选未索引 Timeline", en: "Select all unindexed Timelines", ru: "Выбрать все Timeline без индекса" },
    "topic.selectedUnindexed": { zh: "已选择 {0}/{1}", en: "Selected {0}/{1}", ru: "Выбрано {0}/{1}" },
    "topic.noTimelineSummary": { zh: "无摘要", en: "No summary", ru: "Нет сводки" },
    "topic.maintenanceSubmit": { zh: "开始增量补建", en: "Build selected Timelines", ru: "Обработать выбранные Timeline" },
    "topic.detectUnindexedFailed": { zh: "检查未索引 Timeline 失败", en: "Failed to check unindexed Timelines", ru: "Не удалось проверить Timeline без индекса" },
    "topic.warning":      { zh: "Topic 记忆由 Timeline 自动派生且不允许手动编辑；启用 Topic 优先召回后，会以 Topic 为主并用少量 Timeline 补充。", en: "Topic memories are read-only Timeline derivatives. With Topic-first recall enabled, Topics are primary and a few Timelines provide detail.", ru: "Topic создаётся из Timeline и доступен только для чтения; при включённом поиске Topic является основным, а Timeline дополняет детали." },
    "topic.total":        { zh: "Topic 总数", en: "Total topics", ru: "Всего тем" },
    "topic.active":       { zh: "活跃 Topic", en: "Active topics", ru: "Активные темы" },
    "topic.atoms":        { zh: "独立原子", en: "Independent atoms", ru: "Независимые атомы" },
    "topic.links":        { zh: "Timeline 索引", en: "Timeline links", ru: "Связи Timeline" },
    "topic.relations":    { zh: "相关关系", en: "Related links", ru: "Связанные темы" },
    "topic.relatedTopics": { zh: "相关话题", en: "Related topics", ru: "Связанные темы" },
    "topic.chooseSpace":  { zh: "请选择记忆空间", en: "Select a memory space", ru: "Выберите пространство памяти" },
    "topic.empty":        { zh: "该空间尚未构建 Topic", en: "No topics built for this space", ru: "Для этого пространства тем пока нет" },
    "topic.readOnly":     { zh: "自动维护，只读", en: "Auto-maintained, read-only", ru: "Автоматически, только чтение" },
    "topic.topicAtoms":   { zh: "Topic 原子", en: "Topic atoms", ru: "Атомы темы" },
    "topic.sources":      { zh: "Timeline 来源", en: "Timeline sources", ru: "Источники Timeline" },
    "topic.none":         { zh: "无", en: "None", ru: "Нет" },
    "topic.buildDisabled":{ zh: "构建：未启用", en: "Build: disabled", ru: "Сборка: отключена" },
    "topic.buildEnabled": { zh: "构建：已启用", en: "Build: enabled", ru: "Сборка: включена" },
    "topic.autoOn":       { zh: "自动维护：开启", en: "Auto maintenance: on", ru: "Автообслуживание: вкл." },
    "topic.autoOff":      { zh: "自动维护：关闭", en: "Auto maintenance: off", ru: "Автообслуживание: выкл." },
    "topic.rerankOn":     { zh: "Rerank：可用", en: "Rerank: available", ru: "Rerank: доступен" },
    "topic.rerankOff":    { zh: "Rerank：未配置", en: "Rerank: not configured", ru: "Rerank: не настроен" },
    "topic.overallProgress": { zh: "总体进度", en: "Overall progress", ru: "Общий прогресс" },
    "topic.buildAlreadyRunning": { zh: "已有 Topic 构建任务正在运行", en: "A Topic build is already running", ru: "Сборка Topic уже выполняется" },
    "topic.resumeBuild": { zh: "从断点继续", en: "Resume from checkpoint", ru: "Продолжить с контрольной точки" },
    "topic.resumeStarted": { zh: "已从最近的持久化断点继续构建", en: "Build resumed from the latest persisted checkpoint", ru: "Сборка продолжена с последней контрольной точки" },
    "topic.resumeFailed": { zh: "无法继续 Topic 构建", en: "Unable to resume Topic build", ru: "Не удалось продолжить сборку Topic" },
    "topic.discardBuild": { zh: "取消任务", en: "Cancel task", ru: "Отменить задачу" },
    "topic.discardBuildTitle": { zh: "取消断点任务", en: "Cancel checkpoint task", ru: "Отменить задачу с контрольной точкой" },
    "topic.discardBuildWarning": { zh: "此操作会永久清除该任务已保存的候选扫描、片段、向量、匹配、合成结果和构建检查点，之后无法从该断点继续。", en: "This permanently removes the run's saved candidate scan, fragments, embeddings, matching, synthesis results, and checkpoints. The run can no longer be resumed.", ru: "Сохранённые кандидаты, фрагменты, эмбеддинги, сопоставления, результаты синтеза и контрольные точки будут удалены безвозвратно." },
    "topic.discardBuildMessage": { zh: "确定取消任务 {0} 并清除其中已完成的进度吗？", en: "Cancel task {0} and clear all of its completed progress?", ru: "Отменить задачу {0} и удалить весь сохранённый прогресс?" },
    "topic.discardBuildMaterialized": { zh: "已经正式写入的 Topic 会保留，避免误删或破坏任务开始前的 Topic；取消后可以重新执行全量或增量构建。", en: "Already materialized topics are preserved to avoid deleting or corrupting topics that existed before this run. You can start a new full or incremental build afterward.", ru: "Уже записанные Topic сохраняются, чтобы не удалить и не повредить темы, существовавшие до запуска. После отмены можно начать новую сборку." },
    "topic.discardBuildSubmit": { zh: "确认取消并清除进度", en: "Cancel and clear progress", ru: "Отменить и очистить прогресс" },
    "topic.discardBuildSuccess": { zh: "断点任务已取消，已清除 {0} 项中间进度", en: "Checkpoint task cancelled; {0} intermediate items removed", ru: "Задача отменена; удалено промежуточных элементов: {0}" },
    "topic.discardBuildFailed": { zh: "无法取消并清除断点任务", en: "Unable to cancel and clear the checkpoint task", ru: "Не удалось отменить и очистить задачу" },
    "topic.progress.elapsed": { zh: "已运行", en: "Elapsed", ru: "Прошло" },
    "topic.progress.updated": { zh: "距上次进度", en: "Since last progress", ru: "С последнего обновления" },
    "topic.progress.llmCall": { zh: "LLM 已完成", en: "LLM completed", ru: "LLM завершено" },
    "topic.progress.concurrency": { zh: "并发上限", en: "Concurrency limit", ru: "Лимит параллельности" },
    "topic.progress.rerankCompleted": { zh: "Rerank 已完成", en: "Rerank completed", ru: "Rerank завершено" },
    "topic.progress.rerankActive": { zh: "活跃 Rerank", en: "Active rerank", ru: "Активных Rerank" },
    "topic.progress.groupsCompleted": { zh: "候选组已完成", en: "Groups completed", ru: "Групп завершено" },
    "topic.progress.activeGroups": { zh: "活跃候选组", en: "Active groups", ru: "Активных групп" },
    "topic.progress.extractingGroup": { zh: "当前候选组", en: "Current candidate group", ru: "Текущая группа" },
    "topic.progress.synthesizingComponent": { zh: "正在调用 LLM 合成组件", en: "Calling LLM for component", ru: "LLM синтезирует компонент" },
    "topic.progress.reviewingComponent": { zh: "正在复核组件结构", en: "Reviewing component structure", ru: "Проверка структуры компонента" },
    "topic.progress.reviewedComponents": { zh: "已复核组件", en: "Reviewed components", ru: "Проверено компонентов" },
    "topic.progress.reviewOutputGroups": { zh: "输出分组", en: "Output groups", ru: "Выходных групп" },
    "topic.progress.timelines": { zh: "Timeline 数", en: "Timelines", ru: "Timeline" },
    "topic.progress.groupTimelines": { zh: "候选组 Timeline 数", en: "Group Timelines", ru: "Timeline в группе" },
    "topic.progress.fragments": { zh: "组件片段数", en: "Component fragments", ru: "Фрагментов в компоненте" },
    "topic.progress.currentBatch": { zh: "当前批次片段数", en: "Current batch fragments", ru: "Фрагментов в пакете" },
    "topic.progress.level": { zh: "合成层级", en: "Reduction level", ru: "Уровень синтеза" },
    "topic.progress.failedAt": { zh: "失败阶段：", en: "Failed during:", ru: "Ошибка на этапе:" },
    "topic.stage.pending": { zh: "等待开始", en: "Pending", ru: "Ожидание" },
    "topic.stage.candidate_scan": { zh: "扫描 Timeline 与生成候选组", en: "Scanning Timelines and building candidates", ru: "Сканирование Timeline" },
    "topic.stage.candidate_scan_completed": { zh: "候选扫描完成", en: "Candidate scan completed", ru: "Сканирование завершено" },
    "topic.stage.fragment_extraction": { zh: "提取 Topic 片段", en: "Extracting Topic fragments", ru: "Извлечение фрагментов" },
    "topic.stage.embedding": { zh: "生成片段向量", en: "Embedding fragments", ru: "Создание эмбеддингов" },
    "topic.stage.fragment_matching": { zh: "匹配片段与 Rerank", en: "Matching and reranking fragments", ru: "Сопоставление и реранжирование" },
    "topic.stage.component_review": { zh: "复核 Topic 组件结构", en: "Reviewing Topic component structure", ru: "Проверка структуры Topic" },
    "topic.stage.topic_synthesis": { zh: "合成 Topic 记忆", en: "Synthesizing Topic memories", ru: "Синтез Topic" },
    "topic.stage.materialization": { zh: "写入 Topic、原子与索引", en: "Writing Topics, atoms, and indexes", ru: "Запись Topic и индексов" },
    "topic.stage.completed": { zh: "构建完成", en: "Build completed", ru: "Сборка завершена" },
    "topic.stage.failed": { zh: "构建失败", en: "Build failed", ru: "Ошибка сборки" },
    "topic.stage.cancelled": { zh: "构建已取消", en: "Build cancelled", ru: "Сборка отменена" },
    "topic.stage.discarded": { zh: "断点任务已清除", en: "Checkpoint task discarded", ru: "Задача очищена" },
    "topic.uid":          { zh: "Topic UID", en: "Topic UID", ru: "UID темы" },
    "topic.title":        { zh: "标题", en: "Title", ru: "Название" },
    "topic.viewDetails":  { zh: "查看 Topic 详情", en: "View Topic details", ru: "Открыть сведения о теме" },
    "topic.status":       { zh: "状态", en: "Status", ru: "Статус" },
    "topic.importance":   { zh: "重要性", en: "Importance", ru: "Важность" },
    "topic.support":      { zh: "时间簇 / Timeline", en: "Clusters / Timelines", ru: "Кластеры / Timeline" },
    "topic.revision":     { zh: "版本", en: "Revision", ru: "Версия" },

    /* ---- Nuke ---- */
    "nuke.cancel":        { zh: "取消核爆", en: "Cancel Nuke", ru: "Отменить сброс" },
    "nuke.button":        { zh: "核爆清除", en: "Nuke Clear", ru: "Полный сброс" },
    "nuke.startToast":    { zh: "💥 核爆倒计时启动！", en: "💥 Nuke countdown started!", ru: "💥 Обратный отсчёт запущен!" },
    "nuke.cancelledToast":{ zh: " 核爆已取消！记忆保留", en: " Nuke cancelled! Memories preserved.", ru: " Сброс отменён! Память сохранена." },
    "nuke.cancelFail":    { zh: "取消失败，请稍后重试", en: "Cancel failed, please retry", ru: "Не удалось отменить, попробуйте позже" },
    "nuke.countdown":     { zh: "所有记忆将在 {0} 秒后被抹除。立即取消以中止核爆！", en: "All memories will be erased in {0}s. Cancel now to abort!", ru: "Вся память будет удалена через {0} сек. Отмените сейчас!" },
    "nuke.erasing":       { zh: "正在抹除所有记忆... 请保持窗口打开。", en: "Erasing all memories... Keep this window open.", ru: "Удаление всей памяти... Не закрывайте окно." },
    "nuke.doneTable":     { zh: " 核爆完成！所有记忆已被抹除。点击「刷新」重新加载。", en: " Nuke complete! All memories erased. Click Refresh to reload.", ru: " Сброс завершён! Вся память удалена. Нажмите Обновить." },
    "nuke.doneToast":     { zh: " 核爆完成！所有记忆已从界面移除（仅视觉效果）", en: " Nuke complete! All memories removed (visual only).", ru: " Сброс завершён! Память удалена (визуально)." },
    "nuke.cantStart":     { zh: "无法启动核爆模式", en: "Cannot start nuke mode", ru: "Не удалось запустить режим сброса" },

    /* ---- Stats ---- */
    "stats.total":        { zh: "总记忆", en: "Total", ru: "Всего" },
    "stats.active":       { zh: "活跃", en: "Active", ru: "Активно" },
    "stats.archived":     { zh: "已归档", en: "Archived", ru: "Архив" },
    "stats.deleted":      { zh: "已删除", en: "Deleted", ru: "Удалено" },
    "stats.sessions":     { zh: "活跃会话", en: "Active Sessions", ru: "Активных сессий" },
    "stats.graphNodes":   { zh: "图谱节点", en: "Graph Nodes", ru: "Узлы графа" },
    "stats.atoms":        { zh: "原子记忆", en: "Atoms", ru: "Атомы" },

    /* ---- Filter ---- */
    "filter.keyword":     { zh: "关键字（支持 memory_id / 内容搜索）", en: "Keyword (memory_id / content)", ru: "Ключевое слово (memory_id / контент)" },
    "filter.sessionId":   { zh: "会话 ID（可选）", en: "Session ID (optional)", ru: "ID сессии (опц.)" },
    "filter.statusAll":   { zh: "全部状态", en: "All Statuses", ru: "Все статусы" },
    "filter.statusActive":{ zh: "活跃", en: "Active", ru: "Активно" },
    "filter.statusArchived":{ zh: "已归档", en: "Archived", ru: "Архив" },
    "filter.statusDeleted":{ zh: "已删除", en: "Deleted", ru: "Удалено" },
    "filter.typeAll":     { zh: "全部类型", en: "All Types", ru: "Все типы" },
    "filter.apply":       { zh: "筛选", en: "Filter", ru: "Фильтр" },

    /* ---- Sort ---- */
    "sort.createdDesc":   { zh: "最新创建", en: "Newest first", ru: "Сначала новые" },
    "sort.createdAsc":    { zh: "最早创建", en: "Oldest first", ru: "Сначала старые" },
    "sort.updatedDesc":   { zh: "最近更新", en: "Recently updated", ru: "Недавно обновлено" },
    "sort.importanceDesc":{ zh: "重要性高到低", en: "Importance high to low", ru: "Важность по убыванию" },
    "sort.importanceAsc": { zh: "重要性低到高", en: "Importance low to high", ru: "Важность по возрастанию" },
    "sort.typeAsc":       { zh: "类型 A-Z", en: "Type A-Z", ru: "Тип A-Z" },

    /* ---- Table ---- */
    "table.id":           { zh: "记忆 ID", en: "Memory ID", ru: "ID памяти" },
    "table.summary":      { zh: "摘要", en: "Summary", ru: "Сводка" },
    "table.type":         { zh: "类型", en: "Type", ru: "Тип" },
    "table.importance":   { zh: "重要性", en: "Importance", ru: "Важность" },
    "table.status":       { zh: "状态", en: "Status", ru: "Статус" },
    "table.created":      { zh: "创建时间", en: "Created", ru: "Создано" },
    "table.lastAccess":   { zh: "最后访问", en: "Last Access", ru: "Доступ" },
    "table.actions":      { zh: "操作", en: "Actions", ru: "Действия" },
    "table.detail":       { zh: "详情", en: "Detail", ru: "Детали" },
    "table.noSummary":    { zh: "（无摘要）", en: "(No summary)", ru: "(Нет сводки)" },
    "table.noContent":    { zh: "（无内容）", en: "(No content)", ru: "(Нет контента)" },
    "table.noData":       { zh: "暂无数据", en: "No data", ru: "Нет данных" },
    "table.na":           { zh: "--", en: "--", ru: "--" },
    "table.updated":      { zh: "更新于 {0}", en: "Updated {0}", ru: "Обновлено {0}" },

    /* ---- Pagination ---- */
    "pagination.prev":    { zh: "上一页", en: "Previous", ru: "Пред." },
    "pagination.next":    { zh: "下一页", en: "Next", ru: "След." },
    "pagination.allLoaded":{ zh: "共 {0} 条记录（已加载全部）", en: "{0} records (all loaded)", ru: "{0} записей (загружено все)" },
    "pagination.filtering":{ zh: "筛选中:", en: "Filtering:", ru: "Фильтр:" },
    "pagination.byKeyword":{ zh: "关键词=\"{0}\"", en: "keyword=\"{0}\"", ru: "слово=\"{0}\"" },
    "pagination.byStatus":{ zh: "状态=\"{0}\"", en: "status=\"{0}\"", ru: "статус=\"{0}\"" },
    "pagination.bySession":{ zh: "会话=\"{0}\"", en: "session=\"{0}\"", ru: "сессия=\"{0}\"" },

    /* ---- Search / Results Toast ---- */
    "search.resultToast": { zh: "搜索结果：找到 {0} 条记忆，当前显示第 {1} 条", en: "Search: {0} memories found, showing {1}", ru: "Поиск: найдено {0}, показано {1}" },

    /* ---- Delete ---- */
    "delete.confirmTitle":{ zh: "️  确认删除？", en: "️  Confirm Delete?", ru: "️  Подтвердить удаление?" },
    "delete.confirmMsg":  { zh: "即将删除 {0} 条记忆。\n此操作无法撤销！\n\n点击\"确定\"继续删除，点击\"取消\"保留。", en: "About to delete {0} memories.\nThis cannot be undone!\n\nClick OK to proceed, Cancel to keep them.", ru: "Будет удалено {0} записей.\nЭто необратимо!\n\nНажмите ОК для удаления, Отмена для сохранения." },
    "delete.cancelled":   { zh: "已取消删除操作", en: "Deletion cancelled", ru: "Удаление отменено" },
    "delete.deleting":    { zh: "删除中...", en: "Deleting...", ru: "Удаление..." },
    "delete.allFailed":   { zh: " 删除失败：全部 {0} 条记忆无法删除\n失败ID: {1}\n请检查日志了解详情", en: " Delete failed: all {0} memories could not be deleted\nFailed IDs: {1}", ru: " Ошибка: все {0} записей не удалены\nID: {1}" },
    "delete.partialFailed":{ zh: "️ 部分删除失败：成功 {0} 条，失败 {1} 条\n失败ID: {2}", en: "️ Partial failure: {0} succeeded, {1} failed\nFailed IDs: {2}", ru: "️ Частичная ошибка: {0} удалено, {1} не удалено\nID: {2}" },
    "delete.success":     { zh: " 已成功删除 {0} 条记忆", en: " Successfully deleted {0} memories", ru: " Удалено {0} записей" },
    "delete.successOne":  { zh: "已删除记忆 #{0}", en: "Deleted memory #{0}", ru: "Удалена память #{0}" },
    "delete.none":        { zh: "️ 没有删除任何记忆", en: "️ No memories were deleted", ru: "️ Ничего не удалено" },
    "delete.error":       { zh: "删除失败，请稍后重试", en: "Delete failed, please try again later", ru: "Ошибка удаления, попробуйте позже" },

    /* ---- Archive ---- */
    "archive.success":    { zh: "已归档 {0} 条记忆", en: "Archived {0} memories", ru: "Архивировано {0} записей" },
    "archive.fail":       { zh: "归档失败", en: "Archive failed", ru: "Ошибка архивации" },
    "archive.error":      { zh: "归档失败", en: "Archive failed", ru: "Ошибка архивации" },

    /* ---- Detail Drawer ---- */
    "detail.title":       { zh: "记忆详情", en: "Memory Detail", ru: "Детали памяти" },
    "detail.edit":        { zh: "编辑记忆", en: "Edit Memory", ru: "Редактировать" },
    "detail.close":       { zh: "关闭详情", en: "Close detail", ru: "Закрыть" },
    "detail.memoryId":    { zh: "记忆 ID", en: "Memory ID", ru: "ID памяти" },
    "detail.source":      { zh: "来源", en: "Source", ru: "Источник" },
    "detail.sourceCustom":{ zh: "自定义存储", en: "Custom Storage", ru: "Пользовательское" },
    "detail.sourceVector":{ zh: "向量存储", en: "Vector Storage", ru: "Векторное" },
    "detail.status":      { zh: "状态", en: "Status", ru: "Статус" },
    "detail.importance":  { zh: "重要性", en: "Importance", ru: "Важность" },
    "detail.type":        { zh: "类型", en: "Type", ru: "Тип" },
    "detail.created":     { zh: "创建时间", en: "Created", ru: "Создано" },
    "detail.lastAccess":  { zh: "最后访问", en: "Last Access", ru: "Доступ" },
    "detail.notFound":    { zh: "未找到对应的记录", en: "Record not found", ru: "Запись не найдена" },

    /* ---- Edit Modal ---- */
    "edit.title":         { zh: "编辑记忆", en: "Edit Memory", ru: "Редактировать память" },
    "edit.field":         { zh: "编辑字段", en: "Edit Field", ru: "Поле" },
    "edit.fieldContent":  { zh: "内容", en: "Content", ru: "Содержимое" },
    "edit.fieldImportance":{ zh: "重要性", en: "Importance", ru: "Важность" },
    "edit.fieldType":     { zh: "类型", en: "Type", ru: "Тип" },
    "edit.fieldStatus":   { zh: "状态", en: "Status", ru: "Статус" },
    "edit.newContent":    { zh: "新内容", en: "New Content", ru: "Новое содержимое" },
    "edit.newContentPh":  { zh: "输入新的记忆内容", en: "Enter new memory content", ru: "Введите новое содержимое" },
    "edit.newImportance": { zh: "新重要性 (0-10)", en: "New Importance (0-10)", ru: "Новая важность (0-10)" },
    "edit.importanceHint":{ zh: "重要性越高，记忆被召回的优先级越高", en: "Higher importance → higher recall priority", ru: "Выше важность → выше приоритет" },
    "edit.newType":       { zh: "新类型", en: "New Type", ru: "Новый тип" },
    "edit.typePh":        { zh: "如: FACT, EVENT, PREFERENCE", en: "e.g. FACT, EVENT, PREFERENCE", ru: "напр. FACT, EVENT, PREFERENCE" },
    "edit.typeHint":      { zh: "记忆类型用于分类管理", en: "Memory type is used for categorization", ru: "Тип памяти для категоризации" },
    "edit.newStatus":     { zh: "新状态", en: "New Status", ru: "Новый статус" },
    "edit.statusPh":      { zh: "活跃", en: "Active", ru: "Активно" },
    "edit.statusArchived":{ zh: "已归档", en: "Archived", ru: "Архив" },
    "edit.statusDeleted": { zh: "已删除", en: "Deleted", ru: "Удалено" },
    "edit.statusHint":    { zh: "已删除的记忆不会被召回", en: "Deleted memories won't be recalled", ru: "Удалённая память не извлекается" },
    "edit.reason":        { zh: "更新原因 (可选)", en: "Update Reason (optional)", ru: "Причина (опц.)" },
    "edit.reasonPh":      { zh: "说明本次更新的原因", en: "Explain the reason for this update", ru: "Укажите причину обновления" },
    "edit.noItem":        { zh: "未找到当前记忆信息", en: "Current memory info not found", ru: "Информация о памяти не найдена" },
    "edit.enterValue":    { zh: "请输入新值", en: "Please enter a new value", ru: "Введите новое значение" },
    "edit.updateFailed":  { zh: "更新失败", en: "Update failed", ru: "Ошибка обновления" },
    "edit.success":       { zh: "更新成功", en: "Update successful", ru: "Обновлено успешно" },

    /* ---- Status pills ---- */
    "status.active":      { zh: "活跃", en: "Active", ru: "Активно" },
    "status.archived":    { zh: "已归档", en: "Archived", ru: "Архив" },
    "status.deleted":     { zh: "已删除", en: "Deleted", ru: "Удалено" },

    /* ---- Type labels ---- */
    "type.general":       { zh: "通用", en: "General", ru: "Общее" },
    "type.fact":          { zh: "事实", en: "Fact", ru: "Факт" },
    "type.factual":       { zh: "事实", en: "Factual", ru: "Факт" },
    "type.preference":    { zh: "偏好", en: "Preference", ru: "Предпочтение" },
    "type.event":         { zh: "事件", en: "Event", ru: "Событие" },
    "type.episodic":      { zh: "事件", en: "Episodic", ru: "Эпизод" },
    "type.relational":    { zh: "关系", en: "Relational", ru: "Связь" },
    "type.planned":       { zh: "计划", en: "Planned", ru: "План" },
    "type.opinion":       { zh: "观点", en: "Opinion", ru: "Мнение" },

    /* ---- Graph Hero ---- */
    "graph.kicker":       { zh: "Graph Memory Explorer", en: "Graph Memory Explorer", ru: "Graph Memory Explorer" },
    "graph.title":        { zh: "知识图谱视图", en: "Knowledge Graph View", ru: "Граф знаний" },
    "graph.subtitle":     { zh: "从双路四模式召回结果中观察人物、主题、事实与记忆之间的连接。", en: "Explore connections between people, topics, facts, and memories from dual-route four-mode recall.", ru: "Исследуйте связи между людьми, темами, фактами и памятью из двухмаршрутного четырёхрежимного поиска." },

    /* ---- Graph Toolbar ---- */
    "graph.queryLabel":   { zh: "图谱查询", en: "Graph Query", ru: "Запрос графа" },
    "graph.queryPh":      { zh: "输入人物、主题、事实或整句，查看召回到的图谱子图", en: "Enter a person, topic, fact or sentence to view the recalled subgraph", ru: "Введите персону, тему, факт или фразу для просмотра подграфа" },
    "graph.sessionLabel": { zh: "会话过滤", en: "Session Filter", ru: "Фильтр сессии" },
    "graph.sessionPh":    { zh: "可选：限定 session_id", en: "Optional: limit to session_id", ru: "Опц.: ограничить session_id" },
    "graph.personaLabel": { zh: "人格过滤", en: "Persona Filter", ru: "Фильтр персоны" },
    "graph.personaPh":    { zh: "可选：限定 persona_id", en: "Optional: limit to persona_id", ru: "Опц.: ограничить persona_id" },
    "graph.memoryIdLabel":{ zh: "记忆 ID", en: "Memory ID", ru: "ID памяти" },
    "graph.memoryIdPh":   { zh: "输入记忆 ID 定位局部子图", en: "Enter memory ID to locate subgraph", ru: "Введите ID памяти для поиска подграфа" },
    "graph.searchBtn":    { zh: "检索图谱", en: "Search Graph", ru: "Искать в графе" },
    "graph.focusBtn":     { zh: "定位记忆", en: "Focus Memory", ru: "Фокус памяти" },
    "graph.overviewBtn":  { zh: "最近概览", en: "Recent Overview", ru: "Обзор" },

    /* ---- Graph Stats ---- */
    "graph.visibleNodes": { zh: "可视节点", en: "Visible Nodes", ru: "Видимых узлов" },
    "graph.nodes":        { zh: "节点", en: "Nodes", ru: "Узлы" },
    "graph.edges":        { zh: "关系", en: "Relations", ru: "Связи" },
    "graph.visibleEdges": { zh: "关系边", en: "Relation Edges", ru: "Связей" },
    "graph.visibleEntries":{ zh: "图谱条目", en: "Graph Entries", ru: "Записей графа" },
    "graph.routeLabel":   { zh: "检索视角", en: "Retrieval Route", ru: "Маршрут поиска" },
    "graph.visibleMemories":{ zh: "关联记忆", en: "Related Memories", ru: "Связанных памятей" },

    /* ---- Graph Panels ---- */
    "graph.canvasTitle":  { zh: "图谱画布", en: "Graph Canvas", ru: "Холст графа" },
    "graph.canvasSubtitle":{ zh: "点击节点、记忆卡片或召回结果即可切换焦点。", en: "Click nodes, memory cards or retrieval results to switch focus.", ru: "Нажмите узел, карточку памяти или результат поиска для смены фокуса." },
    "graph.focusDetail":  { zh: "焦点详情", en: "Focus Detail", ru: "Детали фокуса" },
    "graph.topNodes":     { zh: "核心节点", en: "Top Nodes", ru: "Ключевые узлы" },
    "graph.relatedMemories":{ zh: "相关记忆", en: "Related Memories", ru: "Связанная память" },
    "graph.retrievalPath":{ zh: "召回路径", en: "Retrieval Path", ru: "Путь поиска" },

    /* ---- Graph Status / Modes ---- */
    "graph.modeOverview": { zh: "最近概览", en: "Recent Overview", ru: "Обзор" },
    "graph.modeQuery":    { zh: "检索视图", en: "Retrieval View", ru: "Вид поиска" },
    "graph.modeFocus":    { zh: "记忆聚焦", en: "Memory Focus", ru: "Фокус памяти" },
    "graph.modeUnknown":  { zh: "图谱视图", en: "Graph View", ru: "Вид графа" },
    "graph.routeDual":    { zh: "文档 + 图 · 关键词 + 向量", en: "Doc + Graph · Keyword + Vector", ru: "Док + Граф · Ключ + Вектор" },
    "graph.routeBrowse":  { zh: "图谱浏览", en: "Graph Browse", ru: "Обзор графа" },
    "graph.statusDefault":{ zh: "展示图记忆中的核心连接。", en: "Showing core connections in graph memory.", ru: "Показаны основные связи в графе памяти." },
    "graph.statusQuery":  { zh: "当前展示 \"{0}\" 的双路四模式召回对应子图。", en: "Showing dual-route four-mode subgraph for \"{0}\".", ru: "Показан подграф для \"{0}\" (два маршрута, четыре режима)." },
    "graph.statusFocus":  { zh: "当前聚焦记忆 #{0} 的关系子图。", en: "Focused on relation subgraph of memory #{0}.", ru: "Фокус на подграфе связей памяти #{0}." },
    "graph.filterSession":{ zh: "会话 {0}", en: "Session {0}", ru: "Сессия {0}" },
    "graph.filterPersona":{ zh: "人格 {0}", en: "Persona {0}", ru: "Персона {0}" },
    "graph.filterPrefix": { zh: " 过滤条件：{0}", en: " Filter: {0}", ru: " Фильтр: {0}" },

    /* ---- Graph Node Types ---- */
    "graph.nodeTopic":    { zh: "主题", en: "Topic", ru: "Тема" },
    "graph.nodePerson":   { zh: "人物", en: "Person", ru: "Человек" },
    "graph.nodeFact":     { zh: "事实", en: "Fact", ru: "Факт" },
    "graph.nodeSummary":  { zh: "摘要", en: "Summary", ru: "Сводка" },
    "graph.nodeUnknown":  { zh: "节点", en: "Node", ru: "Узел" },

    /* ---- Graph Score Labels ---- */
    "graph.scoreDocKW":   { zh: "文档关键词", en: "Doc Keyword", ru: "Ключ. слова док." },
    "graph.scoreDocVec":  { zh: "文档向量", en: "Doc Vector", ru: "Вектор док." },
    "graph.scoreGraphKW": { zh: "图关键词", en: "Graph Keyword", ru: "Ключ. слова графа" },
    "graph.scoreGraphVec":{ zh: "图向量", en: "Graph Vector", ru: "Вектор графа" },

    /* ---- Graph Disabled ---- */
    "graph.disabledBadge":{ zh: "图记忆未启用", en: "Graph Disabled", ru: "Граф отключён" },
    "graph.disabledMsg":  { zh: "当前实例未启用图记忆功能，请先开启图记忆并完成索引。", en: "Graph memory is not enabled. Enable it and complete indexing first.", ru: "Граф памяти не включён. Включите его и завершите индексацию." },
    "graph.disabledRoute":{ zh: "未启用", en: "Disabled", ru: "Отключено" },
    "graph.disabledLegend":{ zh: "暂无图数据", en: "No graph data", ru: "Нет данных графа" },
    "graph.disabledMemories":{ zh: "暂无可展示的图记忆", en: "No graph memories to display", ru: "Нет граф-памятей для показа" },
    "graph.disabledRetrieval":{ zh: "点击\"最近概览\"加载图谱，或直接输入检索词。", en: "Click Recent Overview to load graph, or enter a search term.", ru: "Нажмите Обзор для загрузки графа или введите запрос." },
    "graph.disabledInspector":{ zh: "请选择节点或记忆查看详细信息。", en: "Select a node or memory to view details.", ru: "Выберите узел или память для просмотра." },
    "graph.disabledCanvas":{ zh: "当前实例尚未启用图记忆。", en: "Graph memory is not yet enabled.", ru: "Граф памяти ещё не включён." },

    /* ---- Graph Error ---- */
    "graph.errorBadge":   { zh: "图谱加载失败", en: "Graph Load Failed", ru: "Ошибка загрузки графа" },
    "graph.errorLegend":  { zh: "请求失败", en: "Request Failed", ru: "Ошибка запроса" },
    "graph.errorFetch":   { zh: "无法加载图谱概览", en: "Cannot load graph overview", ru: "Не удалось загрузить обзор графа" },

    /* ---- Graph Canvas Messages ---- */
    "graph.canvasDefault":{ zh: "点击\"最近概览\"加载图谱，或直接输入检索词。", en: "Click Recent Overview to load graph, or enter a search term.", ru: "Нажмите Обзор для загрузки графа или введите запрос." },
    "graph.canvasNo3D":   { zh: "3D 图谱组件未加载，请刷新页面并检查静态资源。", en: "3D graph component not loaded. Refresh and check static assets.", ru: "3D компонент графа не загружен. Обновите страницу." },
    "graph.canvasEmpty":  { zh: "当前范围内暂无可视化图数据。", en: "No visible graph data in the current range.", ru: "Нет видимых данных графа в текущем диапазоне." },
    "graph.canvasNoScene":{ zh: "当前页面未能加载 3D 图谱组件，请刷新页面后重试。", en: "Failed to load 3D graph component. Refresh and retry.", ru: "Не удалось загрузить 3D компонент. Обновите страницу." },

    /* ---- Graph Loading ---- */
    "graph.loadingOverview":{ zh: "正在加载最近图谱概览...", en: "Loading recent graph overview...", ru: "Загрузка обзора графа..." },
    "graph.loadingQuery": { zh: "正在检索\"{0}\"相关图谱...", en: "Retrieving graph for \"{0}\"...", ru: "Поиск графа для \"{0}\"..." },
    "graph.loadingFocus": { zh: "正在聚焦记忆 #{0} 的关系图...", en: "Focusing on relation graph of memory #{0}...", ru: "Фокус на графе связей памяти #{0}..." },
    "graph.loadingGeneric":{ zh: "图谱载入中...", en: "Loading graph...", ru: "Загрузка графа..." },

    /* ---- Graph Errors (actions) ---- */
    "graph.queryFail":    { zh: "图谱检索失败", en: "Graph retrieval failed", ru: "Ошибка поиска в графе" },
    "graph.focusEmpty":   { zh: "请输入要定位的记忆 ID。", en: "Please enter a memory ID to focus.", ru: "Введите ID памяти для фокуса." },
    "graph.focusNotInt":  { zh: "记忆 ID 必须是整数。", en: "Memory ID must be an integer.", ru: "ID памяти должен быть целым числом." },
    "graph.focusFail":    { zh: "定位记忆失败", en: "Memory focus failed", ru: "Ошибка фокуса памяти" },
    "graph.statsFailed":  { zh: "获取图谱统计失败", en: "Failed to fetch graph stats", ru: "Не удалось получить статистику графа" },

    /* ---- Graph Legend ---- */
    "graph.legendEmpty":  { zh: "暂无图谱连接", en: "No graph connections", ru: "Нет соединений в графе" },

    /* ---- Graph Panels Content ---- */
    "graph.noTopNodes":   { zh: "暂无核心节点", en: "No top nodes", ru: "Нет ключевых узлов" },
    "graph.noRelatedMemories":{ zh: "暂无关联记忆", en: "No related memories", ru: "Нет связанной памяти" },
    "graph.noRetrieval":  { zh: "执行检索后，这里会展示文档 / 图 × 关键词 / 向量的召回细节。", en: "After retrieval, doc/graph × keyword/vector recall details appear here.", ru: "После поиска здесь появятся детали поиска док/граф × ключ/вектор." },
    "graph.noInspector":  { zh: "点击节点、记忆卡片或召回结果查看详细信息。", en: "Click a node, memory card or retrieval result to view details.", ru: "Нажмите узел, карточку памяти или результат поиска для деталей." },
    "graph.unnamedNode":  { zh: "未命名节点", en: "Unnamed Node", ru: "Безымянный узел" },
    "graph.noSummary":    { zh: "无摘要", en: "No summary", ru: "Нет сводки" },
    "graph.focusThisMemory":{ zh: "聚焦此记忆", en: "Focus This Memory", ru: "Фокус на память" },
    "graph.noSession":    { zh: "未设置会话", en: "No session set", ru: "Сессия не задана" },

    /* ---- Graph Inspector ---- */
    "graph.inspectorMemoryCount":{ zh: "关联记忆", en: "Related", ru: "Связано" },
    "graph.inspectorDegree":{ zh: "连接度", en: "Degree", ru: "Степень" },
    "graph.inspectorEntryCount":{ zh: "命中条目", en: "Hit Entries", ru: "Записей" },
    "graph.inspectorWeight":{ zh: "权重", en: "Weight", ru: "Вес" },
    "graph.inspectorRelatedMemories":{ zh: "相关记忆", en: "Related Memories", ru: "Связанная память" },
    "graph.inspectorNoRelatedMemories":{ zh: "暂无相关记忆", en: "No related memories", ru: "Нет связанной памяти" },
    "graph.inspectorRelatedEntries":{ zh: "相关条目", en: "Related Entries", ru: "Связанные записи" },
    "graph.inspectorNoRelatedEntries":{ zh: "暂无相关条目", en: "No related entries", ru: "Нет связанных записей" },
    "graph.inspectorNodeDist":{ zh: "节点分布", en: "Node Distribution", ru: "Распределение узлов" },
    "graph.inspectorNoNodes":{ zh: "暂无节点", en: "No nodes", ru: "Нет узлов" },
    "graph.inspectorGraphEntries":{ zh: "图谱条目", en: "Graph Entries", ru: "Записи графа" },
    "graph.inspectorNoGraphEntries":{ zh: "暂无图谱条目", en: "No graph entries", ru: "Нет записей графа" },
    "graph.inspectorNodeCount":{ zh: "节点", en: "Nodes", ru: "Узлы" },
    "graph.inspectorEntryCount2":{ zh: "条目", en: "Entries", ru: "Записи" },
    "graph.inspectorRelationCount":{ zh: "关系", en: "Relations", ru: "Связи" },
    "graph.inspectorImportance":{ zh: "重要性", en: "Importance", ru: "Важность" },
    "graph.inspectorMemory":{ zh: "记忆", en: "Memory", ru: "Память" },

    /* ---- Graph Tooltip ---- */
    "graph.tooltipMemory": { zh: "记忆 {0} · 关系 {1} · 条目 {2}", en: "Memory {0} · Rel {1} · Entries {2}", ru: "Память {0} · Связ {1} · Запис {2}" },

    /* ---- Graph Bridge Error ---- */
    "graph.bridgeError":  { zh: "当前页面必须运行在 AstrBot 官方插件 Page 内。", en: "This page must run inside an AstrBot plugin page.", ru: "Страница должна работать внутри страницы плагина AstrBot." },

    /* ---- Recall Test ---- */
    "recall.clearBtn":    { zh: "清空结果", en: "Clear Results", ru: "Очистить" },
    "recall.title":       { zh: "记忆召回功能测试", en: "Memory Recall Test", ru: "Тест поиска памяти" },
    "recall.subtitle":    { zh: "输入查询语句，测试混合检索引擎的召回能力", en: "Enter a query to test the hybrid retrieval engine", ru: "Введите запрос для теста гибридного поиска" },
    "recall.queryLabel":  { zh: "查询内容", en: "Query", ru: "Запрос" },
    "recall.queryPh":     { zh: "输入你的查询语句，系统将使用混合检索（BM25+向量相似度）进行召回", en: "Enter your query. The system uses hybrid retrieval (BM25 + vector similarity).", ru: "Введите запрос. Система использует гибридный поиск (BM25 + векторы)." },
    "recall.countLabel":  { zh: "返回数量", en: "Result Count", ru: "Кол-во результатов" },
    "recall.kLabel":      { zh: "结果数 (k)", en: "Results (k)", ru: "Результаты (k)" },
    "recall.countPh":     { zh: "返回的记忆数量", en: "Number of memories to return", ru: "Количество возвращаемых памятей" },
    "recall.sessionLabel":{ zh: "会话 ID (可选)", en: "Session ID (optional)", ru: "ID сессии (опц.)" },
    "recall.sessionPh":   { zh: "输入会话 ID 以过滤特定会话的记忆（支持多种格式）", en: "Enter session ID to filter memories (supports multiple formats)", ru: "Введите ID сессии для фильтрации (разные форматы)" },
    "recall.searchBtn":   { zh: "执行召回", en: "Run Recall", ru: "Запустить поиск" },
    "recall.resultTitle": { zh: "召回结果", en: "Recall Results", ru: "Результаты поиска" },
    "recall.resultCount": { zh: "召回数量", en: "Recall Count", ru: "Найдено" },
    "recall.resultsCount":{ zh: "{0} 条结果", en: "{0} results", ru: "{0} результатов" },
    "recall.time":        { zh: "查询耗时", en: "Query Time", ru: "Время запроса" },
    "recall.empty":       { zh: "暂无召回结果 · 请输入查询内容并执行召回", en: "No results · Enter a query and run recall", ru: "Нет результатов · Введите запрос и запустите поиск" },
    "recall.noMatch":     { zh: "未找到匹配的记忆", en: "No matching memories found", ru: "Совпадений не найдено" },
    "recall.noResults":   { zh: "未找到匹配的记忆", en: "No matching memories found", ru: "Совпадений не найдено" },
    "recall.enterQuery":  { zh: "请输入查询内容", en: "Please enter a query", ru: "Введите запрос" },
    "recall.queryRequired":{ zh: "请输入查询内容", en: "Please enter a query", ru: "Введите запрос" },
    "recall.searching":   { zh: "执行中...", en: "Running...", ru: "Поиск..." },
    "recall.successToast":{ zh: "成功召回 {0} 条记忆", en: "Recalled {0} memories", ru: "Найдено {0} памятей" },
    "recall.fail":        { zh: "召回失败", en: "Recall failed", ru: "Ошибка поиска" },
    "recall.testFailed":  { zh: "召回测试失败", en: "Recall test failed", ru: "Ошибка теста поиска" },
    "recall.timeElapsed": { zh: "耗时 {0} 秒", en: "{0}s elapsed", ru: "Затрачено {0} с" },
    "recall.diagnostics": { zh: "召回诊断", en: "Recall diagnostics", ru: "Диагностика поиска" },
    "recall.candidateSummary": { zh: "候选 {0} · 入选 {1}", en: "{0} candidates · {1} selected", ru: "Кандидатов {0} · выбрано {1}" },
    "recall.threshold": { zh: "本轮阈值", en: "Threshold", ru: "Порог" },
    "recall.overlapSuppressed": { zh: "上下文重叠过滤", en: "Context overlaps", ru: "Перекрытия контекста" },
    "recall.filteredCandidates": { zh: "查看 {0} 条未入选候选", en: "View {0} filtered candidates", ru: "Показать отфильтрованные: {0}" },
    "recall.relevance": { zh: "相关度", en: "Relevance", ru: "Релевантность" },
    "recall.branchCount": { zh: "命中查询分支", en: "Matched branches", ru: "Ветви запроса" },
    "recall.topicDiagnostics": { zh: "Topic 召回", en: "Topic recall", ru: "Поиск Topic" },
    "recall.topicContextSuppressed": { zh: "Topic 高覆盖过滤", en: "Topic context suppression", ru: "Фильтр Topic по контексту" },
    "recall.topicCandidates": { zh: "查看 {0} 条 Topic 候选", en: "View {0} Topic candidates", ru: "Кандидаты Topic: {0}" },
    "recall.selected": { zh: "已入选", en: "Selected", ru: "Выбрано" },
    "recall.filtered": { zh: "未入选", en: "Filtered", ru: "Отфильтровано" },

    /* ---- Recall Results Metadata ---- */
    "recall.resultId":    { zh: "记忆 ID:", en: "Memory ID:", ru: "ID памяти:" },
    "recall.resultScore": { zh: "相似度得分:", en: "Similarity Score:", ru: "Оценка схожести:" },
    "recall.resultSession":{ zh: "会话 UUID:", en: "Session UUID:", ru: "UUID сессии:" },
    "recall.resultImportance":{ zh: "重要性:", en: "Importance:", ru: "Важность:" },
    "recall.resultType":  { zh: "类型:", en: "Type:", ru: "Тип:" },
    "recall.resultStatus":{ zh: "状态:", en: "Status:", ru: "Статус:" },

    /* ---- Theme ---- */
    "theme.darkToast":    { zh: "🌙 已切换到深色模式", en: "🌙 Dark mode enabled", ru: "🌙 Тёмная тема включена" },
    "theme.lightToast":   { zh: "☀️ 已切换到浅色模式", en: "☀️ Light mode enabled", ru: "☀️ Светлая тема включена" },

    /* ---- Bridge Error ---- */
    "bridge.error":       { zh: "当前页面必须运行在 AstrBot 官方插件 Page 内", en: "This page must run inside an AstrBot plugin page", ru: "Страница должна работать внутри страницы плагина AstrBot" },

    /* ---- Misc ---- */
    "misc.requestFailed": { zh: "请求失败", en: "Request failed", ru: "Ошибка запроса" },
    "misc.initFail":      { zh: "初始化加载失败", en: "Initialization failed", ru: "Ошибка инициализации" },
    "misc.statsFail":     { zh: "获取统计信息失败", en: "Failed to fetch stats", ru: "Не удалось получить статистику" },
    "misc.statsUnavailable":{ zh: "无法获取统计信息", en: "Stats unavailable", ru: "Статистика недоступна" },
    "misc.fetchMemoriesFail":{ zh: "获取记忆失败", en: "Failed to fetch memories", ru: "Не удалось загрузить память" },
    "misc.loadFail":      { zh: "加载失败", en: "Load failed", ru: "Ошибка загрузки" },
    "misc.systemFail":    { zh: "系统概览加载失败", en: "Failed to load system overview", ru: "Не удалось загрузить обзор системы" },

    /* ---- System ---- */
    "system.importanceDistribution":{ zh: "重要性分布", en: "Importance Distribution", ru: "Распределение важности" },
    "system.atomTypes":   { zh: "原子类型", en: "Atom Types", ru: "Типы атомов" },
    "system.activeSessions":{ zh: "活跃会话", en: "Active Sessions", ru: "Активные сессии" },
    "system.versionBackups":{ zh: "版本备份", en: "Version Backups", ru: "Резервные копии" },
    "system.noActiveSessions":{ zh: "暂无活跃会话", en: "No active sessions", ru: "Нет активных сессий" },
    "system.noSessions":  { zh: "暂无会话", en: "No sessions", ru: "Нет сессий" },
    "system.noBackups":   { zh: "暂无备份", en: "No backups", ru: "Нет резервных копий" },
    "system.noAtoms":     { zh: "暂无原子数据", en: "No atom data", ru: "Нет данных атомов" },
    "system.files":       { zh: "个文件", en: "files", ru: "файлов" },
    "system.messages":    { zh: "条消息", en: "messages", ru: "сообщений" },
    "system.lastActive":  { zh: "最后活跃", en: "Last active", ru: "Посл. активность" },
    "system.fetchFailed": { zh: "获取系统数据失败", en: "Failed to fetch system data", ru: "Не удалось получить данные системы" },
    "system.atomFactual": { zh: "事实", en: "Factual", ru: "Фактическая" },
    "system.atomEpisodic":{ zh: "事件", en: "Episodic", ru: "Эпизодическая" },
    "system.atomPreference":{ zh: "偏好", en: "Preference", ru: "Предпочтения" },
    "system.atomRelational":{ zh: "关系", en: "Relational", ru: "Связи" },
    "system.atomPlanned": { zh: "计划", en: "Planned", ru: "Планы" },

    /* ---- Atom labels ---- */
    "atom.entity":        { zh: "实体", en: "Entity", ru: "Сущность" },
    "atom.event":         { zh: "事件", en: "Event", ru: "Событие" },
    "atom.preference":    { zh: "偏好", en: "Preference", ru: "Предпочтение" },
    "atom.topic":         { zh: "主题", en: "Topic", ru: "Тема" },

    /* ---- Memory Detail ---- */
    "detail.viewTitle":   { zh: "记忆详情", en: "Memory Detail", ru: "Детали памяти" },
    "detail.editTitle":   { zh: "编辑记忆", en: "Edit Memory", ru: "Редактировать память" },
    "detail.content":     { zh: "内容", en: "Content", ru: "Содержимое" },
    "detail.summary":     { zh: "记忆摘要", en: "Memory Summary", ru: "Сводка памяти" },
    "detail.metadata":    { zh: "元数据", en: "Metadata", ru: "Метаданные" },
    "detail.graphContext":{ zh: "知识图谱关联", en: "Knowledge Graph Context", ru: "Контекст графа знаний" },
    "detail.keyFacts":    { zh: "关键事实", en: "Key Facts", ru: "Ключевые факты" },
    "detail.topics":      { zh: "主题", en: "Topics", ru: "Темы" },
    "detail.participants":{ zh: "参与者", en: "Participants", ru: "Участники" },
    "detail.sentiment":   { zh: "情感", en: "Sentiment", ru: "Тональность" },
    "detail.sentiment.positive": { zh: "正面", en: "Positive", ru: "Позитивная" },
    "detail.sentiment.neutral": { zh: "中性", en: "Neutral", ru: "Нейтральная" },
    "detail.sentiment.negative": { zh: "负面", en: "Negative", ru: "Негативная" },
    "detail.onePerLine":  { zh: "每行一项；保存后会同步重建事实原子与图谱。", en: "One item per line. Saving rebuilds atoms and graph data.", ru: "Один элемент на строку. При сохранении атомы и граф перестраиваются." },
    "detail.itemEditHint": { zh: "编辑原有项目属于修改；删除和新增只影响当前记忆，不会查找关联记忆。", en: "Editing an existing item is a replacement. Deletions and additions affect only this memory and do not search related memories.", ru: "Редактирование существующего пункта считается заменой. Удаления и добавления влияют только на текущую память." },
    "detail.addItem": { zh: "+ 新增一项", en: "+ Add item", ru: "+ Добавить" },
    "detail.removeItem": { zh: "删除此项", en: "Remove item", ru: "Удалить" },
    "detail.editHistory": { zh: "编辑历史", en: "Edit History", ru: "История изменений" },
    "detail.editBtn":     { zh: "编辑", en: "Edit", ru: "Редактировать" },
    "detail.deleteBtn":   { zh: "删除", en: "Delete", ru: "Удалить" },
    "detail.saveBtn":     { zh: "保存修改", en: "Save Changes", ru: "Сохранить" },
    "detail.saveMode":    { zh: "保存方式", en: "Save mode", ru: "Режим сохранения" },
    "detail.saveMode.rebuild": { zh: "重新构建（生成新 ID）", en: "Rebuild (new ID)", ru: "Пересоздать (новый ID)" },
    "detail.saveMode.inPlace": { zh: "同 ID 原位重建", en: "Rebuild in place (same ID)", ru: "Пересоздать с тем же ID" },
    "detail.cancelBtn":   { zh: "取消", en: "Cancel", ru: "Отмена" },
    "detail.memoryTitle": { zh: "记忆 #{0}", en: "Memory #{0}", ru: "Память #{0}" },
    "detail.editingTitle":{ zh: "正在编辑记忆 #{0}", en: "Editing Memory #{0}", ru: "Редактирование памяти #{0}" },
    "detail.sessionId":   { zh: "会话 ID", en: "Session ID", ru: "ID сессии" },
    "detail.personaId":   { zh: "人格 ID", en: "Persona ID", ru: "ID персоны" },
    "detail.updated":     { zh: "更新时间", en: "Updated", ru: "Обновлено" },
    "detail.updateReason":{ zh: "更新原因（可选）", en: "Update Reason (optional)", ru: "Причина обновления (опц.)" },
    "detail.reasonPh":    { zh: "说明本次更新的原因", en: "Why this update?", ru: "Причина обновления" },
    "detail.contentHint": { zh: "摘要、主题和关键事实会整体重建索引；可选择生成新 ID 或保留原 ID。", en: "Summary, topics, and facts rebuild all indexes together; choose a new ID or preserve the current ID.", ru: "Сводка, темы и факты перестраивают все индексы; можно создать новый ID или сохранить текущий." },
    "detail.structuredUpdated": { zh: "结构化记忆已同步更新（新 ID：{0}）", en: "Structured memory updated (new ID: {0})", ru: "Структурированная память обновлена (новый ID: {0})" },
    "detail.structuredUpdatedInPlace": { zh: "结构化记忆已原位重建（ID：{0}）", en: "Structured memory rebuilt in place (ID: {0})", ru: "Память пересоздана с тем же ID: {0}" },
    "detail.noGraphData": { zh: "暂无图谱数据", en: "No graph data", ru: "Нет данных графа" },
    "detail.noChanges":   { zh: "没有检测到修改", en: "No changes", ru: "Нет изменений" },
    "detail.contentRequired":{ zh: "记忆内容不能为空", en: "Memory content cannot be empty", ru: "Содержимое памяти не может быть пустым" },
    "detail.contentUpdated":{ zh: "内容已更新（新 ID：{0}）", en: "Content updated (new ID: {0})", ru: "Содержимое обновлено (новый ID: {0})" },
    "detail.statusUpdated":{ zh: "状态 → {0}", en: "Status → {0}", ru: "Статус → {0}" },
    "detail.typeUpdated": { zh: "类型 → {0}", en: "Type → {0}", ru: "Тип → {0}" },
    "detail.importanceUpdated":{ zh: "重要性 → {0}", en: "Importance → {0}", ru: "Важность → {0}" },
    "detail.nodeMemories":{ zh: "关联记忆", en: "Memories", ru: "Память" },
    "detail.nodeDegree":  { zh: "连接度", en: "Degree", ru: "Степень" },
    "detail.nodeEntries": { zh: "条目", en: "Entries", ru: "Записи" },
    "detail.nodeWeight":  { zh: "权重", en: "Weight", ru: "Вес" },

    /* ---- Structured save dialog ---- */
    "saveDialog.title": { zh: "保存与关联记忆更新", en: "Save and update related memories", ru: "Сохранение и обновление связанных воспоминаний" },
    "saveDialog.dangerTitle": { zh: "危险操作", en: "Dangerous operation", ru: "Опасная операция" },
    "saveDialog.dangerMessage": { zh: "编辑记忆及更新关联记忆会重建事实原子、检索索引和记忆图谱，可能影响后续召回结果。建议操作前自行备份插件数据，并逐条核对修改预览。", en: "Editing memories and related memories rebuilds atoms, search indexes, and the memory graph and may affect future recall. Back up plugin data first and review every proposed change.", ru: "Изменение памяти перестраивает атомы, поисковые индексы и граф. Сначала создайте резервную копию данных плагина и проверьте каждое изменение." },
    "saveDialog.mode.sameId": { zh: "保留原 ID（原位重建）", en: "Keep original ID (in-place)", ru: "Сохранить исходный ID" },
    "saveDialog.mode.newId": { zh: "新建 ID（替换原记忆）", en: "Create a new ID (replace original)", ru: "Создать новый ID" },
    "saveDialog.relatedScope": { zh: "更新关联记忆", en: "Update related memories", ru: "Обновить связанные воспоминания" },
    "saveDialog.scope.current": { zh: "仅当前记忆", en: "Current memory only", ru: "Только текущее воспоминание" },
    "saveDialog.scope.session": { zh: "同会话中的记忆", en: "Memories in the same session", ru: "Воспоминания в этом сеансе" },
    "saveDialog.scope.persona": { zh: "当前人格的记忆", en: "Memories for the current persona", ru: "Воспоминания текущей персоны" },
    "saveDialog.detect": { zh: "检测关联记忆", en: "Detect related memories", ru: "Найти связанные воспоминания" },
    "saveDialog.detectRequired": { zh: "请先检测并核对关联记忆", en: "Detect and review related memories first", ru: "Сначала найдите и проверьте связанные воспоминания" },
    "saveDialog.detecting": { zh: "正在检测…", en: "Detecting…", ru: "Поиск…" },
    "saveDialog.detected": { zh: "生成了 {0} 条关联修改预览，请逐条核对", en: "Generated {0} related change previews; review each one", ru: "Создано вариантов изменений: {0}. Проверьте каждый" },
    "saveDialog.detectFailed": { zh: "检测关联记忆失败", en: "Failed to detect related memories", ru: "Не удалось найти связанные воспоминания" },
    "saveDialog.noRelated": { zh: "没有需要同步的明确字段修改。新增、删除及无法确定映射的变化只作用于当前记忆。", en: "No unambiguous changes need propagation. Additions, deletions, and ambiguous mappings affect only the current memory.", ru: "Нет однозначных изменений для синхронизации. Добавления, удаления и неоднозначные изменения применяются только к текущей памяти." },
    "saveDialog.field.key_facts": { zh: "关键事实", en: "Key fact", ru: "Ключевой факт" },
    "saveDialog.field.topics": { zh: "主题", en: "Topic", ru: "Тема" },
    "saveDialog.match.exact": { zh: "完全匹配", en: "Exact match", ru: "Точное совпадение" },
    "saveDialog.match.normalized_exact": { zh: "规范化匹配", en: "Normalized match", ru: "Нормализованное совпадение" },
    "saveDialog.match.near": { zh: "极高文本相似", en: "Very high text similarity", ru: "Очень высокая схожесть текста" },
    "saveDialog.action.exactReplace": { zh: "精确替换", en: "Exact replacement", ru: "Точная замена" },
    "saveDialog.action.nearReplace": { zh: "近似项替换", en: "Near-match replacement", ru: "Замена похожего значения" },
    "saveDialog.summaryChanged": { zh: "摘要中存在相同原文，将同步替换。", en: "The same text occurs in the summary and will also be replaced.", ru: "Тот же текст в сводке также будет заменён." },
    "saveDialog.rebuildImpact": { zh: "确认后将重建该记忆的事实原子、BM25/向量索引和图谱条目。", en: "Confirming rebuilds this memory's atoms, BM25/vector indexes, and graph entries.", ru: "Подтверждение перестроит атомы, индексы BM25/векторов и записи графа." },
    "saveDialog.riskConfirm": { zh: "我已了解风险，并已核对所选关联记忆的修改内容。", en: "I understand the risk and have reviewed the selected related-memory changes.", ru: "Я понимаю риск и проверил выбранные изменения связанной памяти." },
    "saveDialog.riskConfirmRequired": { zh: "请先确认已了解风险并核对关联修改内容", en: "Confirm that you understand the risk and reviewed the related changes", ru: "Подтвердите понимание риска и проверку изменений" },
    "saveDialog.progress": { zh: "已处理 {0}/{1}（{2}%）", en: "Processed {0}/{1} ({2}%)", ru: "Обработано {0}/{1} ({2}%)" },
    "saveDialog.processing": { zh: "正在重构 #{0}：{1}", en: "Rebuilding #{0}: {1}", ru: "Перестроение #{0}: {1}" },
    "saveDialog.finishing": { zh: "正在完成索引同步…", en: "Finishing index synchronization…", ru: "Завершение синхронизации индекса…" },
    "saveDialog.progressUnavailable": { zh: "进度连接已中断，后台任务仍可能继续。任务 ID：{0}", en: "Progress connection was lost; the background job may still be running. Job ID: {0}", ru: "Связь с прогрессом потеряна; фоновая задача может продолжаться. ID: {0}" },
    "saveDialog.completed": { zh: "更新完成：成功 {0} 条，失败 {1} 条", en: "Update complete: {0} succeeded, {1} failed", ru: "Обновление завершено: успешно {0}, ошибок {1}" },
    "saveDialog.failedItems": { zh: "以下记忆更新失败", en: "The following memories failed to update", ru: "Не удалось обновить следующие воспоминания" },
    "saveDialog.jobFailed": { zh: "当前记忆更新失败，关联记忆未继续处理", en: "Current memory update failed; related memories were not processed", ru: "Ошибка текущего воспоминания; связанные не обработаны" },

    /* ---- Confirm dialog ---- */
    "confirm.deleteTitle":{ zh: "确认删除？", en: "Confirm delete?", ru: "Подтвердить удаление?" },
    "confirm.deleteMessage":{ zh: "即将删除记忆 #{0}。此操作无法撤销。", en: "Memory #{0} will be deleted. This cannot be undone.", ru: "Память #{0} будет удалена. Это необратимо." },
    "memory.deleted":     { zh: "记忆已删除", en: "Memory deleted", ru: "Память удалена" },
    "memory.deleteFailed":{ zh: "删除记忆失败", en: "Failed to delete memory", ru: "Не удалось удалить память" },

    /* ---- Graph 2D ---- */
    "graph2d.noData":     { zh: "暂无图谱数据", en: "No graph data available", ru: "Нет данных графа" },
    "graph2d.loading":    { zh: "加载图谱中...", en: "Loading graph...", ru: "Загрузка графа..." },
    "graph2d.moduleFail": { zh: "2D 图谱模块未加载，请刷新页面重试。", en: "2D graph module not loaded. Refresh and retry.", ru: "2D модуль графа не загружен. Обновите страницу." },
  };

  /* ---- Engine ---- */
  let currentLang = "zh";

  function getBridgeLocale() {
    try {
      const bridge = window.AstrBotPluginPage;
      if (bridge) {
        const ctx = bridge.getContext();
        if (ctx && ctx.locale) {
          const lang = String(ctx.locale).split("-")[0];
          if (SUPPORTED.includes(lang)) return lang;
        }
      }
    } catch (_) { /* ignore */ }
    return null;
  }

  function detectLanguage() {
    try {
      const params = new URLSearchParams(window.location.search);
      const langParam = params.get("lang");
      if (langParam && SUPPORTED.includes(langParam)) {
        urlLanguageOverride = true;
        return langParam;
      }
    } catch (_) { /* ignore */ }

    try {
      const stored = localStorage.getItem(LANG_KEY);
      if (stored && SUPPORTED.includes(stored)) return stored;
    } catch (_) { /* ignore */ }

    const bridgeLocale = getBridgeLocale();
    if (bridgeLocale) return bridgeLocale;

    try {
      const nav = (navigator.language || "").split("-")[0];
      if (SUPPORTED.includes(nav)) return nav;
    } catch (_) { /* ignore */ }

    return "zh";
  }

  function listenBridgeLocale() {
    try {
      const bridge = window.AstrBotPluginPage;
      if (!bridge || typeof bridge.onContext !== "function") return;
      bridge.onContext(function (ctx) {
        if (!ctx || !ctx.locale) return;
        const lang = String(ctx.locale).split("-")[0];
        let hasLocalOverride = false;
        try {
          hasLocalOverride = SUPPORTED.includes(localStorage.getItem(LANG_KEY));
        } catch (_) { /* ignore */ }
        if (!urlLanguageOverride && !hasLocalOverride && SUPPORTED.includes(lang) && lang !== currentLang) {
          window.setLanguage(lang, { persist: false, source: "bridge" });
        }
      });
    } catch (_) { /* ignore */ }
  }

  /**
   * @param {string} key
   * @param {...(string|number)} args - positional replacements for {0}, {1}, ...
   */
  window.t = function (key, ...args) {
    const entry = MSG[key];
    let template = entry ? (entry[currentLang] || entry.zh || key) : key;
    args.forEach((arg, i) => {
      template = template.replace(new RegExp("\\{" + i + "\\}", "g"), String(arg ?? ""));
    });
    return template;
  };

  window.setLanguage = function (lang, options = {}) {
    if (!SUPPORTED.includes(lang)) return;
    currentLang = lang;
    if (options.persist !== false) {
      try { localStorage.setItem(LANG_KEY, lang); } catch (_) { /* ignore */ }
    }
    document.documentElement.setAttribute("lang", lang === "zh" ? "zh-CN" : lang === "ru" ? "ru" : "en");
    applyI18n();
    window.dispatchEvent(new CustomEvent("languagechange", { detail: { lang, source: options.source || "local" } }));
  };

  window.getLanguage = function () {
    return currentLang;
  };

  function applyI18n() {
    // data-i18n → textContent
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = window.t(el.getAttribute("data-i18n"));
    });
    // data-i18n-placeholder → placeholder
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", window.t(el.getAttribute("data-i18n-placeholder")));
    });
    // data-i18n-title → title
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", window.t(el.getAttribute("data-i18n-title")));
    });
    // data-i18n-aria → aria-label
    document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", window.t(el.getAttribute("data-i18n-aria")));
    });
  }

  // bootstrap
  currentLang = detectLanguage();
  document.documentElement.setAttribute("lang", currentLang === "zh" ? "zh-CN" : currentLang === "ru" ? "ru" : "en");
  document.addEventListener("DOMContentLoaded", () => {
    applyI18n();
    listenBridgeLocale();
  });
})();
