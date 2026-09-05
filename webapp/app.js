/* ДОЧА — кабинет автоматизаций Telegram.
   Общается с /api/* и передаёт initData в заголовке X-Telegram-Init-Data. */

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
const initData = tg ? tg.initData || '' : '';

/* Демо-режим: вне Telegram (нет window.Telegram.WebApp) или по адресу /app/?demo=1.
   Позволяет посмотреть кабинет глазами без запуска бота: данные статичные,
   запросы на сервер не уходят, но переключатели и списки работают по-настоящему. */
const DEMO = new URLSearchParams(location.search).has('demo') || !tg;

const state = {
  me: null,
  accounts: [],
  pendingLogin: null,
  // Шаг, на котором сейчас стоит шторка подключения аккаунта. Настоящее
  // состояние входа помнит сервер (строка в pending_logins), здесь лежит
  // только то, что нужно нарисовать: какой вопрос задать и сколько попыток.
  login: { stage: 'phone', phone: '', attemptsLeft: null },
  commands: [],
  commandGroups: [],   // порядок и подписи блоков каталога (из /api/commands)
  commandGroup: null,  // выбранный чипс-фильтр каталога (null — все команды)
  promptHits: [],      // что нашёл умный поиск на «Главной»
  tab: 'home',
  build: '',           // метка сборки — показываем в «Ещё», чтобы видеть, что открыто
  tasks: [],
  // Все задачи пользователя, разложенные по статусу — нужны для агрегации
  // бейджей команд: видим «N на паузе» даже когда пользователь смотрит
  // вкладку «Активные», и наоборот.
  tasksByStatus: { active: [], paused: [], done: [] },
  taskCounts: {}, // kind → { active, paused, done }
  taskStatus: 'active',
  chatTag: null,
  mode: 'copy',
  botUrl: '',
  activeCommand: null, // команда, под которую сейчас собрана шторка
  editTask: null,      // задача, которую правит открытая шторка (null — создаём новую)
  // Ссылка на чат → его название. Форма и её итоги показывают имена, а в поле
  // лежат ссылки: у готовой задачи это числовые id, и без этой карты правка
  // предлагала бы выбирать «-1001234567890».
  chatNames: {},
  lastResultsId: null, // для кнопки «Повторить» в шторке результатов
  bankedDays: 0,
  chats: [],                // последний список чатов с бэка
  selectedChats: [],       // объекты выбранных чатов (полные, не только id) —
                           // иначе при поиске выборка «исчезает» с экрана,
                           // и действия над выбранным работать перестают.
  // Шторка выбора: чаты для поля формы (mode 'chats') или сохранённые
  // сообщения для рассылки (mode 'library'). Одна шторка на оба случая —
  // список с отметками и поиском у них одинаковый.
  picker: { mode: 'chats', key: null, multi: false, chosen: [], chats: [] },
  library: [],             // сохранённые сообщения (/api/library)
  libraryPick: [],         // id сообщений, выбранных в форме рассылки
  // Запись библиотеки, которую правят прямо сейчас: {id, post}. Правка идёт тем
  // же полем, что и добавление, поэтому подписи и кнопка смотрят сюда.
  libraryEdit: null,
  features: {
    account_login_enabled: true,
    account_login_status: 'ready',
  },
};

/* Ключ поля из needs/optional (приходит из /api/commands) → как его нарисовать.
   Порядок полей в шторке задаёт сама команда, а не этот словарь. */
const FIELD_SPEC = {
  account: { label: 'Аккаунт', control: 'select' },
  // pick — у поля есть кнопка «выбрать чат»: 'one' ставит один чат, 'many'
  // собирает список через запятую. Руками вписать тоже можно: поле обычное.
  source: {
    label: 'Источник',
    placeholder: '@channel или ссылка t.me/...',
    note: 'кнопка «выбрать» покажет чаты аккаунта',
    pick: 'one',
  },
  target: {
    label: 'Приёмник',
    placeholder: '@my_channel или ссылка',
    note: 'кнопка «выбрать» покажет чаты аккаунта',
    pick: 'one',
  },
  targets: {
    label: 'Чаты',
    placeholder: '@chan1, @chan2, t.me/+invite',
    note: 'кнопка «выбрать» умеет отметить все чаты сразу',
    pick: 'many',
  },
  target_user: { label: 'За кем следим', placeholder: '@username или ссылка на профиль' },
  keywords: {
    label: 'Ключевые слова',
    placeholder: 'чек, подарок, gift',
    note: 'через запятую; пусто — ловим всё подряд',
  },
  reaction: { label: 'Реакция', placeholder: '👍', note: 'любой эмодзи' },
  limit: { label: 'Сколько участников', control: 'number', placeholder: '200', note: 'не больше 10 000' },
  mode: { label: 'Режим', control: 'mode' },
  message: {
    label: 'Сообщение',
    control: 'textarea',
    placeholder: 'Текст сообщения — переносы строк сохраняются.\n\nПустая строка = второе сообщение.',
    note: 'пустая строка делит сообщения, простой перенос — нет',
  },
  interval: { label: 'Интервал (мин)', control: 'number', placeholder: '2', note: 'минимум 1 минута' },
  start: { label: 'Начало (ЧЧ:ММ)', placeholder: '00:00', note: 'по вашим часам — кабинет берёт их с этого устройства' },
  end: { label: 'Конец (ЧЧ:ММ)', placeholder: '23:59', note: 'после этого времени круги ждут до утра' },
  gap: { label: 'Пауза между чатами (сек)', control: 'number', placeholder: '5', note: 'быстрее секунды Telegram всё равно не даст' },
  cycle: { label: 'Пауза перед новым кругом (сек)', control: 'number', placeholder: '10' },
  repeats: { label: 'Сколько кругов', control: 'number', placeholder: '1', note: '0 — крутить без конца' },
  typing: { label: 'Показывать «печатает» перед отправкой', control: 'check' },
  random_pick: { label: 'Брать сообщение наугад, а не по очереди', control: 'check' },
};

/* Заголовок шторки результатов для каждого типа задачи. */
const RESULTS_TITLES = {
  parser: 'Собранная аудитория',
  checks: 'Пойманные чеки',
  dialogs: 'Входящие сообщения',
};

const CHAT_TAGS = ['реклама', 'вопросы', 'продажа', 'работа', 'объявления'];

/* Цвет иконки по типу задачи: список команд читается как набор инструментов,
   а не как одна простыня. Классы описаны в styles.css (.ico--*). */
const KIND_ICO = {
  forward: 'ico--pink',
  broadcast: 'ico--blue',
  poster: 'ico--cyan',
  mailing: 'ico--teal',
  parser: 'ico--violet',
  autosubscribe: 'ico--teal',
  checks: 'ico--amber',
  dialogs: 'ico--blue',
  baiting: 'ico--violet',
  mute: 'ico--pink',
};

const icoClass = (kind) => KIND_ICO[kind] || 'ico--pink';

/* Эмодзи по типу задачи — тем же набором, что и в каталоге команд. Держим
   отдельным словарём, а не берём из /api/commands: карточки задач рисуются
   раньше, чем каталог успевает приехать. */
const KIND_EMOJI = {
  forward: '🔁',
  broadcast: '📣',
  poster: '📤',
  mailing: '📨',
  parser: '🕵️',
  autosubscribe: '🤝',
  checks: '🧾',
  dialogs: '💬',
  baiting: '🎣',
  mute: '🔇',
};

const kindEmoji = (kind) => KIND_EMOJI[kind] || '⚙️';

/* Задачи, которые шлют СВОИ сообщения: их тексты лежат в библиотеке, а не в
   настройках задачи. Тот же набор, что OWN_TEXT_KINDS на сервере. От него
   зависит кнопка «📚 из библиотеки» в форме и сброс выбора при правке. */
const OWN_TEXT_KINDS = ['poster', 'mailing'];

/* Плитки «быстрый старт» на Главной: восемь слотов, последний — весь каталог.
   Подписи короткие: на 390 px в четыре столбца длинное название не влезает.
   Двух «пересылок» и двух «рассылок» здесь быть не должно — плитки называются
   так же, как задачи в каталоге: копия канала, один пост во все чаты,
   постинг по расписанию и рассылка по очереди. */
const TILES = [
  { id: 'copy_channel', name: 'Копия' },
  { id: 'broadcast', name: 'В чаты' },
  { id: 'poster', name: 'Постинг' },
  { id: 'mailing', name: 'Рассылка' },
  { id: 'parser', name: 'Парсер' },
  { id: 'autosubscribe', name: 'Подписка' },
  { id: 'checks', name: 'Чеки' },
  { id: null, name: 'Все', emoji: '☰', ico: 'ico--violet', tab: 'commands' },
];

/* Умный поиск по командам работает локально: фраза → слова → команды.
   Никакого внешнего AI и ключей — значит, ничего не стоит и не отваливается.
   Ключевые слова подобраны под то, как о задачах говорят вслух. Одно слово на
   две задачи не вешаем: «рассылка» — это свои сообщения по чатам (mailing), а
   один пост из источника во все чаты ищут словами «в чаты» и «во все». */
const SMART_WORDS = {
  copy_channel: ['перес', 'копир', 'дубл', 'зеркал', 'репост', 'канал в канал'],
  broadcast: ['в чаты', 'во все', 'один пост', 'из канала в чаты', 'массов'],
  poster: ['пост', 'публик', 'по расписан', 'кажд', 'таймер', 'автопост', 'интервал'],
  mailing: ['рассыл', 'разосл', 'по чатам', 'отправ', 'прогрев', 'спам', 'реклам', 'всем'],
  parser: ['парс', 'собра', 'участник', 'аудитор', 'база', 'юзер', 'подписчик'],
  autosubscribe: ['подпис', 'вступ', 'войти', 'инвайт', 'присоедин'],
  checks: ['чек', 'подар', 'gift', 'ловец', 'халяв', 'промо'],
  dialogs: ['личк', 'диалог', 'входящ', 'сообщен мне', 'уведомл', 'дм'],
  baiting: ['байт', 'реакц', 'эмодзи', 'лайк'],
  mute: ['мут', 'удал', 'модер', 'бан', 'молч'],
};

/* Что показать, когда человек просто открыл поле и ничего не набрал. */
const SMART_FALLBACK = ['copy_channel', 'broadcast', 'poster'];

const SETTINGS = [
  { emoji: '👥', title: 'Рефералы', desc: 'Ссылка, зеркала и выплаты', start: 'referrals' },
  { emoji: '🌐', title: 'Язык', desc: 'Русский', start: 'language' },
  { emoji: '📖', title: 'Гайды', desc: 'Инструкции по основным сценариям', start: 'guides' },
  { emoji: '🛟', title: 'Ресурсы', desc: 'Чат, канал и поддержка', start: 'resources' },
];

/* Экран «Ещё»: то, что убрали из навигации ради короны в центре. Разделы
   кабинета открываются здесь же (data-tab), остальное — в боте (SETTINGS). */
const MORE_ITEMS = [
  { emoji: '👤', title: 'Аккаунты и подписка', desc: 'Номера, копилка дней, оплата', tab: 'accounts' },
  { emoji: '💬', title: 'Чаты', desc: 'Выбрать чаты и запустить задачу по ним', tab: 'chats' },
  { emoji: '📚', title: 'Библиотека сообщений', desc: 'Тексты для рассылки и постинга', tab: 'library' },
  { emoji: '📦', title: 'Архив задач', desc: 'Завершённые и остановленные', tab: 'tasks', status: 'done' },
];

/* ───────────────────────────── Утилиты ───────────────────────────────── */

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let toastTimer = null;

/* Внизу экрана тесно: навигация, плавающая кнопка «Запустить задачу», панель
   выбранных чатов, кнопка открытой шторки. Тост ложился прямо на них — сообщение
   об успехе закрывало подпись кнопки, которую человек только что нажал. Поэтому
   считаем, сколько места занято снизу прямо сейчас, и поднимаем тост над этим.
   Считаем по экрану (getBoundingClientRect), поэтому вырез снизу уже учтён. */
const TOAST_FLOATING = ['addTaskBtn', 'chatBar'];

function toastLift() {
  const view = window.innerHeight;
  const nodes = TOAST_FLOATING.map($).concat(
    // Кнопка открытой шторки: она последняя в панели, то есть у самого низа.
    Array.from(document.querySelectorAll('.sheet.is-open .sheet__panel .btn--block'))
  );
  let lift = 0;
  for (const node of nodes) {
    if (!node) continue;
    const box = node.getBoundingClientRect();
    // Скрытое (hidden, закрытая шторка) высоты не имеет и низ не занимает.
    if (!box.height || box.top >= view) continue;
    lift = Math.max(lift, view - box.top + 10);
  }
  // Тост — сообщение снизу: выше середины экрана не поднимаем, иначе он уедет
  // в пустоту от того места, где человек работает.
  return Math.min(lift, Math.round(view * 0.45));
}

function toast(text) {
  const node = $('toast');
  node.textContent = text;
  node.style.removeProperty('--toast-lift');
  const base = parseFloat(getComputedStyle(node).bottom) || 0;
  const lift = toastLift();
  if (lift > base) node.style.setProperty('--toast-lift', `${lift}px`);
  node.classList.add('is-show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('is-show'), 2600);
}

/* ───────────────────────── Демо-режим (mock API) ─────────────────────── */

const DEMO_STATE = {
  tasks: [
    // edit — значения для формы «⚙️ Настроить»: те же поля, что принимает
    // /api/tasks (на сервере их отдаёт _edit_view). Без них у карточки не было
    // бы кнопки настройки, и в демо правку задачи посмотреть было бы нельзя.
    // health — журнал задачи (на сервере repo.task_health): когда последний раз
    // сработала и на чём сломалась. В демо показываем все три случая: работает,
    // сломана сейчас, сбой уже в прошлом.
    { id: 1, title: 'Новости театра → Мой канал', kind: 'forward', kind_label: 'пересылка',
      source: 'Новости театра', target: 'Мой канал', archived: false, oneshot: false,
      enabled: true, mode: 'copy', delay: 60, forwarded: 842, account_id: 1,
      progress: { done: 842, total: null },
      health: { ok_at: demoAgo(4), error: null, error_at: null, failing: false },
      edit: { account_id: 1, names: {}, source: 'Новости театра', target: 'Мой канал', mode: 'copy' },
      created_at: '2026-08-12T10:20:00' },
    { id: 2, title: 'Афиша → Зеркало афиши', kind: 'forward', kind_label: 'пересылка',
      source: 'Афиша', target: 'Зеркало афиши', archived: false, oneshot: false,
      enabled: true, mode: 'forward', delay: 0, forwarded: 317, account_id: 1,
      progress: { done: 317, total: null },
      health: {
        ok_at: demoAgo(60 * 26),
        error: 'не ушло в Зеркало афиши: ChatWriteForbiddenError',
        error_at: demoAgo(12),
        failing: true,
      },
      edit: { account_id: 1, names: {}, source: 'Афиша', target: 'Зеркало афиши', mode: 'forward' },
      created_at: '2026-08-18T09:05:00' },
    { id: 3, title: 'Подборки → Черновики', kind: 'forward', kind_label: 'пересылка',
      source: 'Подборки', target: 'Черновики', archived: false, oneshot: false,
      enabled: false, mode: 'copy', delay: 300, forwarded: 125, account_id: 1,
      progress: { done: 125, total: null },
      health: { ok_at: null, error: null, error_at: null, failing: false },
      edit: { account_id: 1, names: {}, source: 'Подборки', target: 'Черновики', mode: 'copy' },
      created_at: '2026-08-21T18:40:00' },
    { id: 4, title: 'Парсер аудитории: Конкуренты', kind: 'parser', kind_label: 'парсер аудитории',
      source: 'Конкуренты', target: 'Конкуренты', archived: true, oneshot: true,
      enabled: false, mode: 'copy', delay: 0, forwarded: 640, account_id: 1,
      progress: { done: 640, total: 1000 },
      health: {
        ok_at: demoAgo(60 * 40),
        error: 'FloodWaitError: 42',
        error_at: demoAgo(60 * 44),
        failing: false,
      },
      edit: { account_id: 1, names: {}, source: 'Конкуренты', limit: 1000 },
      created_at: '2026-08-25T11:00:00' },
    // Рассылка своих сообщений: единственная задача, которая берёт тексты из
    // библиотеки. Без неё в демо не видно ни счёта сообщений на карточке, ни
    // строки «рассылают» в библиотеке — а именно из-за неё удаление записи там
    // и опасно. library_ids — то же, что filters.library_ids у правила.
    { id: 5, title: 'Рассылка по очереди: 2 чата', kind: 'mailing', kind_label: 'рассылка по очереди',
      source: 'рассылка по очереди', target: 'Команда (чат)', archived: false, oneshot: false,
      enabled: true, mode: 'copy', delay: 0, forwarded: 4, account_id: 1,
      progress: { done: 4, total: 2 * 3 },
      health: { ok_at: demoAgo(9), error: null, error_at: null, failing: false },
      library_ids: [1],
      targets_count: 2,
      chats: [{ id: '1006', title: 'Команда (чат)' }, { id: '1005', title: 'Подборки' }],
      mailing: {
        recipients: 2, messages_count: 1, whole_library: false, messages_gone: 0,
        gap_seconds: 8, cycle_seconds: 60, repeats: 3, typing: true, random_pick: false,
      },
      edit: {
        account_id: 1, names: { 1006: 'Команда (чат)', 1005: 'Подборки' },
        targets: ['1006', '1005'], message: 'Напоминаем: показ сегодня в 19:00.',
        library_ids: [], gap: 8, cycle: 60, repeats: 3, typing: true, random_pick: false,
      },
      created_at: '2026-08-29T14:15:00' },
    // Разовая задача в работе: без неё в демо не было ни кнопки «Запустить», ни
    // её итога — единственная разовая задача лежала в архиве. Полоса выполнения
    // считает вступления из счётчика задачи, «всего» — длину списка каналов
    // (_task_view: у автоподписки без источника total = len(subscribe_to)).
    { id: 6, title: 'Автоподписка: 5 кан.', kind: 'autosubscribe', kind_label: 'автоподписка',
      source: 'все чаты аккаунта', target: '', archived: false, oneshot: true,
      enabled: true, mode: 'copy', delay: 0, forwarded: 0, account_id: 1,
      progress: { done: 0, total: 5 },
      health: { ok_at: null, error: null, error_at: null, failing: false },
      edit: {
        account_id: 1, names: {},
        targets: ['@theatre_one', '@theatre_two', '@closed', '+AbCdEf123', '@afisha'],
      },
      created_at: '2026-09-01T08:30:00' },
  ],
  nextId: 7,
};

/* Метки времени в демо считаем от «сейчас»: зашитая дата через месяц показала бы
   «30 дней назад» вместо живого «4 минуты назад». */
function demoAgo(minutes) {
  return new Date(Date.now() - minutes * 60000).toISOString();
}

/* Итог разового запуска — в журнал демо-задачи, как это делает сервер
   (jobs.record_oneshot + repo.task_health). Без этого демо врало о главном:
   нажал «Запустить» — и на карточке по-прежнему ноль и ни одной даты, будто
   задачу не запускали. Порядок тот же: сбой пишется раньше успеха, поэтому
   проход с помехами не краснеет, но причина на карточке остаётся. */
function demoJournalRun(task, run) {
  const problems = run.problems || [];
  const done = (run.joined || 0) + (run.already || 0) + (run.collected || 0);
  const wroteError = !run.ok || problems.length > 0;
  const wroteOk = !!run.ok && (done > 0 || problems.length === 0);
  const health = task.health || (task.health = {});
  if (wroteError) {
    health.error = run.ok
      ? problems[0] + (problems.length > 1 ? ` — и ещё ${problems.length - 1}` : '')
      : run.error || 'Запуск не удался';
    health.error_at = demoAgo(0);
  }
  if (wroteOk) health.ok_at = demoAgo(0);
  health.failing = wroteError && !wroteOk;
  if (run.joined) task.forwarded = (task.forwarded || 0) + run.joined;
  const total = (task.progress || {}).total || null;
  task.progress = run.collected != null
    ? { done: run.collected, total: total || 200 }
    : { done: task.forwarded || 0, total };
}

/* Демо-каталог повторяет COMMANDS и COMMAND_GROUPS из app/webapp_api.py:
   в демо-режиме кабинет должен выглядеть точно так же, как с сервером. */
const DEMO_COMMAND_GROUPS = [
  { id: 'publish', title: 'чужие посты' },
  { id: 'own', title: 'свои сообщения' },
  { id: 'audience', title: 'аудитория' },
  { id: 'inbox', title: 'входящее' },
  { id: 'moderation', title: 'модерация' },
];

const DEMO_COMMANDS = [
  { id: 'copy_channel', group: 'publish', kind: 'forward', emoji: '🔁', title: 'Копирование канала', status: 'ready',
    needs: ['account', 'source', 'target'], optional: ['mode'],
    description: 'Один канал — в один ваш: новый пост появился в источнике и сразу выходит у вас, с заменами текста.',
    tags: ['чужие посты', 'один канал → один'] },
  { id: 'broadcast', group: 'publish', kind: 'broadcast', emoji: '📣', title: 'Пересылка в несколько чатов', status: 'ready',
    needs: ['account', 'source', 'targets'], optional: [],
    description: 'Тот же канал — сразу в десятки чатов: пост из источника уходит во все выбранные одним залпом, как только вышел.',
    hint: 'Источник — откуда берём пост, чаты — куда он уйдёт. Отмечайте кнопкой «выбрать» — сколько нужно, хоть все сразу. Свой текст здесь не нужен: уходит то, что вышло в источнике.',
    tags: ['чужие посты', 'все чаты разом', 'по факту поста'] },
  { id: 'parser', group: 'audience', kind: 'parser', emoji: '🕵️', title: 'Парсер аудитории', status: 'ready',
    needs: ['account', 'source'], optional: ['limit'],
    description: 'Собирает участников чужого чата в список по вашей команде.',
    hint: 'Чат-источник отмечайте кнопкой «выбрать» у поля или заранее во вкладке «Чаты». Запускается сразу, результат — кнопкой «Результаты».',
    tags: ['список участников', 'запуск вручную'] },
  { id: 'autosubscribe', group: 'audience', kind: 'autosubscribe', emoji: '🤝', title: 'Автоподписка', status: 'ready',
    needs: ['account', 'targets'], optional: ['source'],
    description: 'Вступает в каналы из списка и подхватывает ссылки из источника.',
    hint: 'Каналы — через запятую: @chan1, t.me/+invite.',
    tags: ['вступает сама', 'ссылки из источника'] },
  { id: 'checks', group: 'inbox', kind: 'checks', emoji: '🧾', title: 'Ловец чеков', status: 'ready',
    needs: ['account', 'source', 'target'], optional: ['keywords'],
    description: 'Ловит чеки и подарочные ссылки в чатах и складывает в одно место.',
    tags: ['чеки и подарки', 'в один чат'] },
  { id: 'dialogs', group: 'inbox', kind: 'dialogs', emoji: '💬', title: 'Уведомления из диалогов', status: 'ready',
    needs: ['account', 'target'], optional: ['keywords'],
    description: 'Присылает входящие личные сообщения в выбранный чат.',
    hint: 'Источник не нужен: задача слушает все личные диалоги аккаунта.',
    tags: ['личные сообщения', 'источник не нужен'] },
  { id: 'baiting', group: 'moderation', kind: 'baiting', emoji: '🎣', title: 'Байтинг', status: 'ready',
    needs: ['account', 'source', 'target_user'], optional: ['reaction'],
    description: 'Ставит реакцию на сообщения выбранного человека в общем чате.',
    tags: ['один человек', 'реакция'] },
  { id: 'mute', group: 'moderation', kind: 'mute', emoji: '🔇', title: 'Мут', status: 'ready',
    needs: ['account', 'source', 'target_user'], optional: ['keywords'],
    description: 'Удаляет сообщения выбранного человека в чате, где вы администратор.',
    tags: ['один человек', 'нужны права админа'] },
  { id: 'poster', group: 'own', kind: 'poster', emoji: '📤', title: 'Постинг по расписанию', status: 'ready',
    needs: ['account', 'targets', 'message'], optional: ['interval', 'start', 'end'],
    description: 'Ваше объявление висит в чатах постоянно: сам шлёт его во все выбранные каждые N минут, пока открыто окно времени. Текст берётся здесь или из библиотеки.',
    hint: 'Чаты отмечайте кнопкой «выбрать» — сколько нужно, хоть все сразу; круг идёт по очереди, с паузой между чатами. Текст наберите здесь либо возьмите из библиотеки — она общая с рассылкой, и правка записи меняет обе задачи. Переносы строк внутри сообщения сохраняются как есть — прайс уйдёт целиком. Нужно второе сообщение — отделите его пустой строкой: за круг уходит одно, следующий круг возьмёт следующее. Интервал в минутах, окно — ЧЧ:ММ по вашим часам.',
    tags: ['ваш текст', 'каждые N минут', 'окно времени'] },
  { id: 'mailing', group: 'own', kind: 'mailing', emoji: '📨', title: 'Рассылка по очереди', status: 'ready',
    needs: ['account', 'targets', 'message'], optional: ['gap', 'cycle', 'repeats', 'typing', 'random_pick'],
    description: 'Обход чатов по одному: чат — пауза — следующий, и так круг за кругом. Текст берётся здесь или из библиотеки.',
    hint: 'Получателей отмечайте кнопкой «выбрать» — или заранее во вкладке «Чаты». Текст наберите здесь либо возьмите из библиотеки — она общая с постингом: переносы строк сохраняются, а пустая строка делит текст на два сообщения — уходят по очереди, первое всем, затем второе. Пауза между чатами в секундах, «кругов 0» — крутить без конца.',
    tags: ['ваш текст', 'по одному чату', 'пауза и круги'] },
];

const DEMO_FEATURES = { account_login_enabled: true, account_login_status: 'ready' };

/* Копилка подписок в демо-режиме: живёт между запросами, чтобы кнопки
   «заморозить» и «распределить» было видно в работе. */
const DEMO_BANK = { banked: 0, days_left: 27 };

/* Подарок за подписку: канал настоящий, начисление — местное. Держим состояние
   между запросами, иначе карточку «подарок получен» в демо не увидеть. */
const DEMO_BONUS = { channel: '@papin4_do4a', days: 3, claimed: false, claimed_at: null };

function demoBonusUrl() {
  return `https://t.me/${DEMO_BONUS.channel.replace(/^@/, '')}`;
}

function demoMe() {
  return {
    id: 1,
    username: 'demo',
    name: 'Демо',
    photo_url: null,
    is_admin: true,
    subscription: { active: true, until: '2026-09-27T12:00:00', days_left: DEMO_BANK.days_left },
    stats: {
      rules: DEMO_STATE.tasks.filter((task) => !task.archived).length,
      accounts: DEMO_ACCOUNTS.length,
      forwarded: 1284,
    },
    tariffs: { rub: 990, stars: 299, usdt: 12, trial_days: 3, max_rules_free: 3 },
    features: DEMO_FEATURES,
    // Демо показывает оба контура: звёзды внутри Telegram, карта и крипта — на
    // сайте. Ссылку демо не выдаёт: подписать её может только сервер.
    pay: { mode: 'external', inline: ['stars', 'manual'], external: ['yookassa', 'usdt'], url: null },
    bonus: {
      enabled: true,
      channel: DEMO_BONUS.channel,
      url: demoBonusUrl(),
      days: DEMO_BONUS.days,
      claimed: DEMO_BONUS.claimed,
      claimed_at: DEMO_BONUS.claimed_at,
    },
  };
}

/* Аккаунты и незавершённый вход в демо живут между запросами: иначе демо-вход
   заканчивался бы ничем, а список аккаунтов не показывал бы результат.
   Код и пароль фиксированные — их видно в подсказке под полем. */
const DEMO_ACCOUNTS = [{
  id: 1,
  phone: '+7 916 •••• 12 34',
  is_active: true,
  online: true,
  last_error: null,
  needs_login: false,
  created_at: '2026-08-12T10:00:00',
}, {
  // Второй аккаунт нарочно офлайн: иначе не видно ни причины, ни кнопки
  // повтора, а это самое частое состояние, из-за которого стоят задачи.
  id: 2,
  phone: '+7 903 •••• 55 01',
  is_active: true,
  online: false,
  last_error: 'Telegram не отдал данные аккаунта — пробуем снова',
  needs_login: false,
  created_at: '2026-09-01T09:20:00',
}];
const DEMO_LOGIN = { pending: null, nextId: 3, sent: {} };
const DEMO_MAX_ATTEMPTS = 5;
const DEMO_CODE = '11111';
const DEMO_PASSWORD = 'doca';
// Пауза перед новым кодом на тот же номер — как на сервере: она принадлежит
// номеру, поэтому «Отмена» и «Другой номер» её не снимают.
const DEMO_RESEND_PAUSE_MS = 60000;

/* Отказ в демо выглядит как отказ сервера: тот же status, тот же текст.
   Так шторка входа проверяется целиком, включая «осталось попыток». */
function demoFail(status, message, details) {
  const error = new Error(message);
  error.status = status;
  error.data = { error: message, ...(details || {}) };
  throw error;
}

function demoAccounts() {
  const pending = DEMO_LOGIN.pending;
  return {
    accounts: DEMO_ACCOUNTS,
    subscription: {
      active: true,
      until: '2026-09-27T12:00:00',
      days_left: DEMO_BANK.days_left,
      piggy_bank_days: DEMO_BANK.banked,
    },
    pending_login: pending
      ? {
        exists: true,
        phone: pending.phone,
        stage: `waiting_${pending.stage}`,
        step: pending.stage,
        attempts_left: Math.max(DEMO_MAX_ATTEMPTS - pending.attempts, 0),
      }
      : { exists: false, phone: null, stage: null, step: null, attempts_left: null },
    bot_url: 'https://t.me/papina_do4a_bot',
    features: DEMO_FEATURES,
  };
}

const DEMO_CHATS = [
  { id: 1001, title: 'Новости театра', username: 'teatr_news', is_channel: true, is_group: false },
  { id: 1002, title: 'Афиша', username: 'afisha_demo', is_channel: true, is_group: false },
  { id: 1003, title: 'Мой канал', username: 'my_demo_channel', is_channel: true, is_group: false },
  { id: 1004, title: 'Зеркало афиши', is_channel: true, is_group: false },
  { id: 1005, title: 'Подборки', is_channel: false, is_group: false },
  { id: 1006, title: 'Команда (чат)', is_channel: false, is_group: true },
  // Массовка: постить и рассылать можно в любое число чатов, поэтому демо
  // обязано показывать длинный список — на шести чатах не видно ни прокрутки
  // шторки, ни счётчика «выбрано», ни кнопки «выбрать все».
  ...Array.from({ length: 144 }, (_, n) => ({
    id: 2001 + n,
    title: `Чат ${n + 1} · ${CHAT_TAGS[n % CHAT_TAGS.length]}`,
    username: n % 3 === 0 ? `demo_chat_${n + 1}` : '',
    is_channel: n % 4 === 0,
    is_group: n % 4 !== 0,
  })),
];

function demoChats(path) {
  const params = new URLSearchParams(path.split('?')[1] || '');
  const query = (params.get('q') || '').toLowerCase().replace(/^@/, '');
  const limit = Number(params.get('limit') || 0);
  let chats = query
    ? DEMO_CHATS.filter(
      (c) => c.title.toLowerCase().includes(query)
        || String(c.username || '').toLowerCase().includes(query),
    )
    : DEMO_CHATS;
  if (limit > 0) chats = chats.slice(0, limit);
  return { chats, total: chats.length, online: true };
}

/* Библиотека сообщений в демо: живёт в памяти страницы, как и задачи. */
const DEMO_LIBRARY = {
  nextId: 4,
  items: [
    // Запись без текста — сохранённый пост: его не набрать руками, поэтому в
    // форме рассылки он стоит чипсом, а не текстом в поле.
    { id: 3, title: 'Готовый пост из канала', text: '', chat_id: -1002001, message_id: 314, created_at: '2026-09-04T11:00:00' },
    { id: 2, title: 'Приглашение на спектакль', text: 'Приглашаем на премьеру! Билеты по ссылке в описании.', chat_id: 0, message_id: 0, created_at: '2026-09-03T18:10:00' },
    { id: 1, title: 'Короткое напоминание', text: 'Напоминаем: показ сегодня в 19:00.', chat_id: 0, message_id: 0, created_at: '2026-09-02T09:30:00' },
  ],
};

/* Кто отправляет записи библиотеки — тем же правилом, что _library_usage на
   сервере: считаем только живые рассылки и постинги (архивная задача не
   работает, и пугать ею при удалении незачем), а задача с пустым списком записей
   держит всю библиотеку целиком, включая запись, которую добавят следующей. */
function demoLibraryUsage() {
  const used = new Map();
  const whole = [];
  DEMO_STATE.tasks.forEach((task) => {
    if (!OWN_TEXT_KINDS.includes(task.kind) || task.archived) return;
    const ids = (task.library_ids || []).map(Number).filter(Boolean);
    if (!ids.length) {
      whole.push(task.title);
      return;
    }
    ids.forEach((id) => {
      if (!used.has(id)) used.set(id, []);
      used.get(id).push(task.title);
    });
  });
  return { used, whole };
}

function demoLibraryItems() {
  const { used, whole } = demoLibraryUsage();
  return DEMO_LIBRARY.items.map((item) => ({
    ...item,
    used_by: [...(used.get(item.id) || []), ...whole],
  }));
}

function demoLibraryItem(item) {
  const { used, whole } = demoLibraryUsage();
  return { ...item, used_by: [...(used.get(item.id) || []), ...whole] };
}

function demoLibrary(clean, options, method) {
  if (method === 'POST') {
    const body = JSON.parse(options.body || '{}');
    const text = String(body.text || '').trim();
    if (!text) demoFail(400, 'Дайте текст сообщения или ссылку на пост');
    const item = {
      id: DEMO_LIBRARY.nextId++,
      title: messageTitle(body.title || text),
      text,
      chat_id: 0,
      message_id: 0,
      created_at: new Date().toISOString(),
    };
    DEMO_LIBRARY.items.unshift(item);
    return { item: demoLibraryItem(item) };
  }
  // Правка записи на месте: id остаётся, и исправленный текст сразу уходит из
  // всех задач, где запись выбрана. Отказы те же, что у сервера, — иначе демо
  // обещало бы то, чего кабинет с сервером не делает.
  if (method === 'PATCH') {
    const body = JSON.parse(options.body || '{}');
    const id = Number(clean.split('/')[3]);
    const item = DEMO_LIBRARY.items.find((row) => row.id === id);
    if (!item) demoFail(404, 'Сообщение не найдено');
    const post = Boolean(Number(item.chat_id || 0) && Number(item.message_id || 0));
    if ('text' in body) {
      const text = String(body.text || '').trim();
      if (!text) demoFail(400, 'Текст пустой: чтобы убрать сообщение, удалите запись');
      if (post) demoFail(400, 'Это готовый пост: его правят в канале, где он лежит');
      // Имя, собранное из текста, идёт за текстом; заданное руками — остаётся.
      const followsText = String(item.title || '') === messageTitle(item.text || '');
      item.text = text;
      if (followsText && !('title' in body)) item.title = messageTitle(text);
    }
    if ('title' in body) item.title = messageTitle(body.title, 128);
    return { item: demoLibraryItem(item) };
  }
  if (method === 'DELETE') {
    const id = Number(clean.split('/')[3]);
    const idx = DEMO_LIBRARY.items.findIndex((item) => item.id === id);
    if (idx < 0) demoFail(404, 'Сообщение не найдено');
    DEMO_LIBRARY.items.splice(idx, 1);
    return { ok: true };
  }
  return { items: demoLibraryItems() };
}


/* Карточка рассылки и постинга читает библиотеку заново на каждом показе — как
   _task_view с _edit_view на сервере. Задача держит только ссылки, а текст и
   счёт живых записей лежат в библиотеке: без этого правка текста до карточки не
   доходила бы, и в демо она выглядела бы бесполезной. */
function demoOwnTextsRefresh(task) {
  if (!OWN_TEXT_KINDS.includes(task.kind)) return task;
  const ids = (task.library_ids || []).map(Number).filter(Boolean);
  const items = ids
    .map((id) => DEMO_LIBRARY.items.find((row) => row.id === id))
    .filter(Boolean);
  // У рассылки счёт живёт в task.mailing (там же паузы и круги), у постинга — в
  // самой карточке рядом с расписанием: так их читает taskMetaLines.
  const info = task.kind === 'mailing' ? (task.mailing || (task.mailing = {})) : task;
  info.messages_count = items.length;
  // Пустой список ссылок планировщик читает как «вся библиотека», а пропавшие
  // записи — это «отправлять нечего»: два разных случая, и путать их нельзя.
  info.whole_library = !ids.length;
  info.messages_gone = ids.length - items.length;
  const edit = task.edit || (task.edit = {});
  edit.message = items.map(libraryText).filter(Boolean).join('\n\n');
  edit.library_ids = items.filter((item) => !libraryText(item)).map((item) => item.id);
  return task;
}

function demoTasks(path) {
  const status = new URLSearchParams(path.split('?')[1] || '').get('status') || 'active';
  const pick = (filter) => DEMO_STATE.tasks.filter(filter).map(demoOwnTextsRefresh);
  if (status === 'active') {
    return { tasks: pick((t) => t.enabled && !t.archived) };
  }
  if (status === 'paused') {
    return { tasks: pick((t) => !t.enabled && !t.archived) };
  }
  return { tasks: pick((t) => t.archived) };
}


const DEMO_RESULTS = {
  4: {
    total: 3,
    items: [
      { id: 1, payload: { user_id: 501, username: 'art_deco_fan', name: 'Аня К.' }, created_at: '2026-08-25T11:02:00' },
      { id: 2, payload: { user_id: 502, username: 'night_owl', name: 'Марк' }, created_at: '2026-08-25T11:02:00' },
      { id: 3, payload: { user_id: 503, username: null, name: 'Ольга' }, created_at: '2026-08-25T11:02:00' },
    ],
  },
};

/* Список чатов задачи — так же, как его собирает сервер (_split_chats): без
   повторов и без источника, потому что пересылать пост в тот же чат, откуда он
   взят, незачем. Сервер сравнивает уже найденные id, демо — сами ссылки: этого
   хватает, чтобы карточка в демо не обещала на один чат больше, чем создастся. */
function demoChatList(body, kind) {
  const source = String(body.source || '').trim().toLowerCase();
  const chats = [];
  [body.target, ...(body.targets || [])].forEach((raw) => {
    const ref = String(raw || '').trim();
    const key = ref.toLowerCase();
    if (!ref || chats.some((item) => item.toLowerCase() === key)) return;
    if (kind === 'broadcast' && key === source) return;
    chats.push(ref);
  });
  return chats;
}

/* Задачи «в любое число чатов»: у них список получателей, счёт чатов в карточке
   и один и тот же расчёт заголовка. Тот же набор, что MULTI_CHAT_KINDS на сервере. */
const DEMO_MULTI_CHAT = ['broadcast', 'poster', 'mailing'];

/* Команда каталога по id из тела запроса (или по типу задачи — так приходит
   правка готовой задачи и старые формы без command). */
function demoCommand(id, kind) {
  return DEMO_COMMANDS.find((item) => item.id === id)
    || DEMO_COMMANDS.find((item) => item.kind === kind)
    || DEMO_COMMANDS[0];
}

/* Название чата по ссылке. Сервер помнит имена чатов в самой задаче
   (filters.chat_titles), демо ищет их в своём списке чатов — важно, чтобы форма
   правки показывала имена, а не «2001». */
function demoChatName(ref) {
  const key = String(ref || '').trim().replace(/^@/, '').toLowerCase();
  if (!key) return '';
  const chat = DEMO_CHATS.find((item) => String(item.username || '').toLowerCase() === key
    || String(item.id) === key
    || String(item.title).toLowerCase() === key);
  return chat ? chat.title : String(ref);
}

/* Свои тексты (рассылка, постинг) в демо живут там же, где на сервере: в
   библиотеке. Набранный текст становится её записями, а задача держит ссылки на
   них — поэтому правка тем же текстом не плодит копий (на сервере это делает
   _own_texts). Готовые посты (записи без текста) руками не набрать: они приходят
   списком id и остаются при задаче, даже когда текст поменяли. */
function demoOwnTexts(body) {
  const picked = (body.library_ids || []).map(Number).filter(Boolean)
    .filter((id) => DEMO_LIBRARY.items.some((item) => item.id === id));
  const msgs = splitMessages(body.message);
  // Текста нет — уйдут выбранные записи. Пусто = вся библиотека.
  if (!msgs.length) return picked;
  const posts = picked.filter((id) => {
    const item = DEMO_LIBRARY.items.find((row) => row.id === id);
    return item && !libraryText(item);
  });
  const texts = msgs.map((text) => {
    const found = DEMO_LIBRARY.items.find((item) => libraryText(item) === text);
    if (found) return found.id;
    const item = {
      id: DEMO_LIBRARY.nextId++,
      title: messageTitle(text),
      text,
      chat_id: 0,
      message_id: 0,
      created_at: new Date().toISOString(),
    };
    DEMO_LIBRARY.items.unshift(item);
    return item.id;
  });
  return texts.concat(posts);
}

/* Что уйдёт из библиотеки: живые записи, повисшие ссылки, «вся библиотека».
   Один расчёт на рассылку и постинг — как _own_texts_state на сервере. */
function demoOwnTextsState(libraryIds) {
  const alive = libraryIds.filter((id) => DEMO_LIBRARY.items.some((item) => item.id === id));
  return {
    messages_count: alive.length,
    // Пустой список записей планировщик читает как «вся библиотека», и карточка
    // обязана сказать это словами, а не показывать ноль.
    whole_library: !libraryIds.length,
    messages_gone: libraryIds.length - alive.length,
  };
}

/* Текст своих сообщений для формы правки: он лежит в библиотеке, но в поле стоит
   он сам — как _own_texts_edit на сервере. Чипсами рядом остаются только записи
   без текста, готовые посты: их руками не набрать. */
function demoOwnTextsEdit(libraryIds) {
  const items = libraryIds
    .map((id) => DEMO_LIBRARY.items.find((row) => row.id === id))
    .filter(Boolean);
  return {
    message: items.map(libraryText).filter(Boolean).join('\n\n'),
    library_ids: items.filter((item) => !libraryText(item)).map((item) => item.id),
  };
}

/* Тело запроса → поля задачи. Одна функция и на создание, и на правку: демо
   повторяет здесь _task_view сервера, и второй такой расчёт разошёлся бы с ним
   на первой же новой настройке. */
function demoTaskFill(task, body, command) {
  const kind = command.kind;
  const chats = demoChatList(body, kind);
  const libraryIds = OWN_TEXT_KINDS.includes(kind) ? demoOwnTexts(body) : [];
  const done = task.progress ? task.progress.done || 0 : 0;
  Object.assign(task, {
    kind,
    kind_label: command.title.toLowerCase(),
    title: demoTaskTitle(body, command),
    source: String(body.source || ''),
    target: String(chats[0] || body.target || body.source || ''),
    mode: kind === 'forward' ? (body.mode || 'copy') : 'copy',
    account_id: Number(body.account_id) || task.account_id || 1,
  });
  // Дальше — то же, что показывает сервер: счёт чатов у задач «в несколько
  // чатов», расписание у постинга, паузы и круги у рассылки.
  if (DEMO_MULTI_CHAT.includes(kind)) {
    task.targets_count = chats.length;
    task.chats = chats.map((ref) => ({ id: ref, title: demoChatName(ref) }));
  }
  if (kind === 'parser') task.progress = { done, total: Number(body.limit) || 200 };
  // Ссылки на записи библиотеки держит сама задача (на сервере это
  // filters.library_ids). По ним библиотека и говорит, кто её отправляет.
  if (OWN_TEXT_KINDS.includes(kind)) task.library_ids = libraryIds.slice();
  if (kind === 'poster') {
    task.interval_min = Number(body.interval) || 2;
    task.window_start = body.start || '00:00';
    task.window_end = body.end || '23:59';
    // Чьи часы у окна: смещение от UTC приходит вместе с задачей (на сервере это
    // filters.window_tz). Без него окно считалось бы по часам сервера.
    task.window_tz = body.tz === undefined ? browserTz() : windowTz(body.tz);
    // Счёт сообщений у постинга стоит в самой карточке, рядом с расписанием.
    Object.assign(task, demoOwnTextsState(libraryIds));
  }
  if (kind === 'mailing') {
    const repeats = body.repeats === undefined ? 1 : Number(body.repeats) || 0;
    task.mailing = {
      recipients: chats.length,
      ...demoOwnTextsState(libraryIds),
      gap_seconds: Number(body.gap) || 5,
      cycle_seconds: Number(body.cycle) || 10,
      repeats,
      typing: Boolean(body.typing),
      random_pick: Boolean(body.random_pick),
    };
    if (repeats > 0 && chats.length) task.progress = { done, total: chats.length * repeats };
  }
  task.edit = demoTaskEdit(body, command, chats, libraryIds);
  return task;
}

/* Значения задачи для формы правки — ровно те поля, что принимает /api/tasks
   (на сервере их собирает _edit_view). Форма правки в кабинете одна на демо и на
   сервер, поэтому и набор полей обязан быть один. */
function demoTaskEdit(body, command, chats, libraryIds) {
  const kind = command.kind;
  const fields = [...(command.needs || []), ...(command.optional || [])];
  const names = {};
  [...chats, body.source, body.target, body.target_user].forEach((raw) => {
    const ref = String(raw || '').trim();
    if (!ref || names[ref]) return;
    const name = demoChatName(ref);
    if (name && name !== ref) names[ref] = name;
  });
  const edit = { account_id: Number(body.account_id) || 1, names };
  if (fields.includes('targets')) {
    // У автоподписки в списке стоят ссылки, по которым она вступает: id у
    // ненайденного канала ещё нет, и подставлять в форму нечего кроме них.
    edit.targets = kind === 'autosubscribe'
      ? (body.targets || []).map(String)
      : chats.slice();
  }
  if (body.source) edit.source = String(body.source);
  if (fields.includes('target') && (body.target || chats[0])) {
    edit.target = String(body.target || chats[0]);
  }
  if (body.target_user) edit.target_user = String(body.target_user);
  if (kind === 'forward') edit.mode = body.mode || 'copy';
  else if (kind === 'parser') edit.limit = Number(body.limit) || 200;
  else if (kind === 'baiting') edit.reaction = body.reaction || '👍';
  else if (['checks', 'dialogs', 'mute'].includes(kind)) {
    edit.keywords = (body.keywords || []).join(', ');
  } else if (kind === 'poster') {
    // Текст постинга лежит в библиотеке — тем же полем, что у рассылки.
    Object.assign(edit, demoOwnTextsEdit(libraryIds));
    edit.interval = Number(body.interval) || 2;
    edit.start = body.start || '00:00';
    edit.end = body.end || '23:59';
    edit.tz = body.tz === undefined ? browserTz() : windowTz(body.tz);
  } else if (kind === 'mailing') {
    Object.assign(edit, demoOwnTextsEdit(libraryIds));
    edit.gap = Number(body.gap) || 5;
    edit.cycle = Number(body.cycle) || 10;
    edit.repeats = body.repeats === undefined ? 1 : Number(body.repeats) || 0;
    edit.typing = Boolean(body.typing);
    edit.random_pick = Boolean(body.random_pick);
  }
  return edit;
}

function demoTaskTitle(body, command) {
  if (command) {
    if (command.kind === 'parser') return `Парсер аудитории: ${body.source}`;
    if (command.kind === 'autosubscribe') {
      const channels = (body.targets || []).length;
      if (channels) return `Автоподписка: ${channels} кан.` + (body.source ? ` из «${body.source}»` : '');
      return `Автоподписка: ${body.source || 'все чаты аккаунта'}`;
    }
    if (command.kind === 'dialogs') return `Уведомления из диалогов → ${body.target}`;
    if (command.kind === 'baiting') return `Байтинг: ${body.target_user} в ${body.source}`;
    if (command.kind === 'mute') return `Мут: ${body.target_user} в ${body.source}`;
    if (command.kind === 'checks') return `Ловец чеков: ${body.source} → ${body.target}`;
    // Постинг, рассылка и пересылка в чаты ходят в любое число чатов: в
    // заголовке счёт, а имя чата — только когда он один. Тот же расчёт, что
    // task_title на сервере.
    if (DEMO_MULTI_CHAT.includes(command.kind)) {
      const chats = demoChatList(body, command.kind);
      const many = chats.length > 1 ? `${chats.length} чат.` : (chats[0] || '');
      if (command.kind === 'broadcast') return `Пересылка: ${body.source} → ${many}`;
      const name = command.kind === 'poster' ? 'Постинг по расписанию' : 'Рассылка по очереди';
      if (!chats.length) return name;
      return chats.length > 1 ? `${name}: ${many}` : `${name} → ${many}`;
    }
  }
  return `${body.source} → ${body.target}`;
}

/* Вход в демо повторяет сервер по шагам и по ошибкам: номер → код → пароль 2FA.
   Демо всегда спрашивает пароль, чтобы все три шага можно было увидеть. */
function demoLogin(clean, options) {
  const body = JSON.parse(options.body || '{}');
  const pending = DEMO_LOGIN.pending;

  if (clean === '/api/accounts/login/cancel') {
    DEMO_LOGIN.pending = null;
    return { ok: true, dropped: Boolean(pending) };
  }

  if (clean === '/api/accounts/login/start') {
    const phone = String(body.phone || '').replace(/[\s\-()]+/g, '');
    if (!/^\+?\d{10,15}$/.test(phone)) {
      demoFail(400, 'Нужен номер в международном формате, например +79001234567.');
    }
    const full = phone.startsWith('+') ? phone : `+${phone}`;
    const sentAt = DEMO_LOGIN.sent[full] || 0;
    const wait = Math.ceil((DEMO_RESEND_PAUSE_MS - (Date.now() - sentAt)) / 1000);
    if (sentAt && wait > 0) {
      if (pending && pending.phone === full) {
        demoFail(
          409,
          `Код на ${full} уже отправлен. Введите его или подождите ${wait} сек, чтобы запросить новый.`,
          { stage: pending.stage, phone: full, wait, attempts_left: DEMO_MAX_ATTEMPTS - pending.attempts }
        );
      }
      demoFail(
        409,
        `Код на ${full} отправляли меньше минуты назад. Подождите ${wait} сек: частые запросы ` +
          'Telegram считает флудом и может закрыть вход на этот номер на несколько часов.',
        { stage: 'phone', phone: full, wait }
      );
    }
    DEMO_LOGIN.sent[full] = Date.now();
    DEMO_LOGIN.pending = { phone: full, stage: 'code', attempts: 0 };
    return { stage: 'code', phone: DEMO_LOGIN.pending.phone, attempts_left: DEMO_MAX_ATTEMPTS };
  }

  if (!pending) demoFail(409, 'Незавершённого входа нет. Начните заново: «Подключить аккаунт».');

  if (clean === '/api/accounts/login/code') {
    if (pending.stage !== 'code') demoFail(409, 'Шаг входа не тот: сервис ждёт облачный пароль.');
    const code = String(body.code || '').replace(/\D/g, '');
    if (!code) demoFail(400, 'В коде только цифры — пришлите их подряд, без пробелов.');
    if (code !== DEMO_CODE) {
      pending.attempts += 1;
      const left = DEMO_MAX_ATTEMPTS - pending.attempts;
      if (left <= 0) {
        DEMO_LOGIN.pending = null;
        demoFail(409, 'Код не подошёл слишком много раз. Начните подключение заново.');
      }
      demoFail(400, `Код не подошёл. Осталось попыток: ${left}.`, { attempts_left: left });
    }
    pending.stage = 'password';
    pending.attempts = 0;
    return { stage: 'password', phone: pending.phone };
  }

  // /api/accounts/login/password
  if (pending.stage !== 'password') demoFail(409, 'Шаг входа не тот: сервис ждёт код из Telegram.');
  if (!String(body.password || '').trim()) demoFail(400, 'Пароль пустой.');
  if (String(body.password) !== DEMO_PASSWORD) {
    demoFail(400, 'Пароль не подошёл (PasswordHashInvalidError). Попробуйте снова.');
  }
  const account = {
    id: DEMO_LOGIN.nextId++,
    phone: pending.phone,
    is_active: true,
    online: true,
    last_error: null,
    needs_login: false,
    created_at: new Date().toISOString(),
  };
  DEMO_ACCOUNTS.push(account);
  DEMO_LOGIN.pending = null;
  return { stage: 'done', phone: account.phone, account_id: account.id, name: 'Демо-аккаунт' };
}

function demoApi(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const clean = path.split('?')[0];
  const query = new URLSearchParams(path.split('?')[1] || '');
  const taskMatch = clean.match(/^\/api\/tasks\/(\d+)(?:\/(.+))?$/);

  if (taskMatch) {
    const id = Number(taskMatch[1]);
    const tail = taskMatch[2] || '';
    const task = DEMO_STATE.tasks.find((t) => t.id === id);

    if (tail === 'toggle' && task) {
      if (task.archived) return { ok: false, error: 'Задача в архиве' };
      task.enabled = !task.enabled;
      return { ok: true };
    }
    if (tail === 'mode' && task) {
      if (task.kind !== 'forward') {
        return { ok: false, error: 'Режим переключается только у обычной пересылки' };
      }
      task.mode = task.mode === 'copy' ? 'forward' : 'copy';
      return { ok: true };
    }
    if (tail === 'archive' && task) {
      task.archived = query.get('undo') !== '1';
      task.enabled = task.archived ? false : true;
      return { ok: true };
    }
    if (tail === 'run' && task) {
      // Отказы — как у сервера (409 и текст): кабинет показывает их через catch,
      // а не через сводку запуска.
      if (!task.oneshot) {
        demoFail(409, 'Эта задача работает по сообщениям — запуск вручную не нужен');
      }
      if (!task.enabled || task.archived) demoFail(409, 'Задача не активна');
      const run = task.kind === 'parser'
        ? { ok: true, collected: 640, limit: 200 }
        : {
            ok: true,
            joined: 3,
            total: 5,
            already: 1,
            problems: ['не пустили в @closed (ChatAdminRequiredError)'],
          };
      demoJournalRun(task, run);
      // Сводку сервер отдаёт полем run (POST /api/tasks/{id}/run → {"run": …}) —
      // в демо было иначе, и кабинет показывал безликое «Готово» вместо итога.
      return { ok: true, run };
    }
    if (tail === 'results') {
      return Object.assign({ kind: task ? task.kind : 'parser', total: 0, items: [] }, DEMO_RESULTS[id] || {});
    }
    if (method === 'DELETE' && !tail) {
      DEMO_STATE.tasks = DEMO_STATE.tasks.filter((t) => t.id !== id);
      return { ok: true };
    }
    // Правка задачи: меняется только присланное, остальное берём из её же формы —
    // ровно так же считает сервер (partial=True в _apply_task_settings).
    if (method === 'PATCH' && !tail) {
      if (!task) demoFail(404, 'Задача не найдена');
      if (task.archived) {
        demoFail(409, 'Задача в архиве: верните её из архива, чтобы менять настройки');
      }
      const patch = JSON.parse(options.body || '{}');
      const body = Object.assign({}, task.edit, patch);
      demoTaskFill(task, body, demoCommand(patch.command, task.kind));
      return { ok: true, task };
    }
    return { ok: true };
  }

  if (clean === '/api/tasks' && method === 'POST') {
    const body = JSON.parse(options.body || '{}');
    const command = demoCommand(body.command, body.kind);
    const kind = command.kind;
    const oneshot = kind === 'parser' || kind === 'autosubscribe';
    const task = {
      id: DEMO_STATE.nextId++,
      archived: false,
      oneshot,
      enabled: !oneshot,
      delay: 0,
      forwarded: 0,
      progress: { done: 0, total: null },
      // Журнала у новой задачи ещё нет — как и на сервере.
      health: { ok_at: null, error: null, error_at: null, failing: false },
      account_id: Number(body.account_id) || 1,
      created_at: new Date().toISOString(),
    };
    // Название, чаты, расписание и форма правки — там же, где у правки задачи.
    demoTaskFill(task, body, command);
    DEMO_STATE.tasks.push(task);
    const run = kind === 'parser'
      ? { ok: true, collected: 128, limit: Number(body.limit) || 200 }
      : kind === 'autosubscribe'
        ? { ok: true, joined: 2, already: 0, total: (body.targets || []).length, problems: [] }
        : null;
    // Первый запуск разовой задачи сервер тоже пишет в её журнал — новая
    // карточка не должна выглядеть как ни разу не запущенная.
    if (run) demoJournalRun(task, run);
    return { ok: true, id: task.id, task, run };
  }

  if (clean === '/api/tasks') return demoTasks(path);
  if (clean === '/api/me') return demoMe();
  if (clean === '/api/commands') return { commands: DEMO_COMMANDS, groups: DEMO_COMMAND_GROUPS };
  if (clean === '/api/accounts') return demoAccounts();
  if (clean.startsWith('/api/accounts/login/')) return demoLogin(clean, options);
  const retryMatch = clean.match(/^\/api\/accounts\/(\d+)\/retry$/);
  if (retryMatch) {
    // В демо повтор всегда удаётся: показать надо не отказ, а что кнопка
    // возвращает аккаунт в работу и метка сразу меняется на «на связи».
    const account = DEMO_ACCOUNTS.find((item) => item.id === Number(retryMatch[1]));
    if (!account) demoFail(404, 'Аккаунт не найден');
    account.online = true;
    account.is_active = true;
    account.last_error = null;
    account.needs_login = false;
    return { ok: true, phone: account.phone, online: true, error: null };
  }
  if (clean.startsWith('/api/accounts/') && method === 'DELETE') {
    const id = Number(clean.split('/')[3]);
    const idx = DEMO_ACCOUNTS.findIndex((item) => item.id === id);
    if (idx < 0) demoFail(404, 'Аккаунт не найден');
    const [removed] = DEMO_ACCOUNTS.splice(idx, 1);
    return { ok: true, phone: removed.phone };
  }
  if (clean === '/api/chats') return demoChats(path);
  if (clean === '/api/library' || clean.startsWith('/api/library/')) {
    return demoLibrary(clean, options, method);
  }

  if (clean === '/api/subscription/bank') {
    const body = JSON.parse(options.body || '{}');
    const days = Number(body.days) || 0;
    const moved = days > 0 ? days : 7;
    DEMO_BANK.banked += moved;
    DEMO_BANK.days_left = Math.max(DEMO_BANK.days_left - moved, 1);
    return { ok: true, moved, banked_days: DEMO_BANK.banked };
  }
  if (clean === '/api/subscription/distribute') {
    const body = JSON.parse(options.body || '{}');
    const days = Number(body.days) || 0;
    if (DEMO_BANK.banked <= 0) return { ok: false, error: 'В копилке нет дней' };
    const moved = days > 0 ? Math.min(days, DEMO_BANK.banked) : DEMO_BANK.banked;
    DEMO_BANK.banked -= moved;
    DEMO_BANK.days_left += moved;
    return { ok: true, moved, banked_days: DEMO_BANK.banked, active: true, days_left: DEMO_BANK.days_left };
  }
  if (clean === '/api/subscription/bonus') {
    // Подписку в демо проверить негде, поэтому первое нажатие всегда дарит дни,
    // а второе отвечает ровно как сервер — 409 и тот же текст.
    if (DEMO_BONUS.claimed) {
      demoFail(409, 'Подарок за подписку уже получен: он даётся один раз на аккаунт.');
    }
    DEMO_BONUS.claimed = true;
    DEMO_BONUS.claimed_at = new Date().toISOString();
    DEMO_BANK.days_left += DEMO_BONUS.days;
    return {
      status: 'granted',
      granted: true,
      days: DEMO_BONUS.days,
      until: null,
      channel: DEMO_BONUS.channel,
      url: demoBonusUrl(),
      message: `Готово! Подарок за подписку — ${DEMO_BONUS.days} дн.`,
    };
  }
  if (clean === '/api/subscription') {
    return {
      active: true,
      until: '2026-09-27T12:00:00',
      days_left: DEMO_BANK.days_left,
      banked_days: DEMO_BANK.banked,
      tariffs: { rub: 990, stars: 299, usdt: 12, trial_days: 3 },
    };
  }

  return { ok: true };
}

async function api(path, options = {}) {
  if (DEMO) return demoApi(path, options);

  const response = await fetch(path, {
    ...options,
    headers: {
      'X-Telegram-Init-Data': initData,
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    /* пустой ответ — просто передадим статус */
  }
  if (!response.ok) {
    const error = new Error(data.error || 'Ошибка запроса');
    error.status = response.status;
    // Тело отказа несёт машиночитаемые поля (например, attempts_left) — они
    // нужны обработчику, а вытаскивать их из текста сообщения нельзя.
    error.data = data;
    throw error;
  }
  return data;
}

function openBot(startParam) {
  const base = state.botUrl || '';
  if (!base) {
    toast('Откройте бота в Telegram');
    return;
  }
  const url = startParam ? `${base}?start=${startParam}` : base;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, '_blank');
}

/* ─────────────────────────────── Тема ────────────────────────────────── */

/* Кабинет живёт в собственной неоновой палитре (см. :root в styles.css), а не в
   цветах темы Telegram: иначе оформление разъезжалось бы у каждого пользователя.
   Telegram-овские theme_params намеренно НЕ перекрывают брендовые переменные. */
const BRAND_BG = '#0A0510';

function applyTheme() {
  if (!tg) return;
  if (tg.setHeaderColor) tg.setHeaderColor(BRAND_BG);
  if (tg.setBackgroundColor) tg.setBackgroundColor(BRAND_BG);
  if (tg.setBottomBarColor) tg.setBottomBarColor(BRAND_BG);
}

/* ────────────────────────────── Навигация ────────────────────────────── */

const TABS = ['home', 'commands', 'tasks', 'chats', 'accounts', 'library', 'more'];

/* В навигации пять слотов, а экранов больше: «Чаты», «Аккаунты» и «Библиотека»
   открываются из «Ещё» и из мастера задач. Пока открыт такой экран, подсвечиваем
   «Ещё» — иначе панель выглядит так, будто мы никуда не переходили. */
const NAV_FOR_TAB = { chats: 'more', accounts: 'more', library: 'more' };

function switchTab(name) {
  const tab = TABS.includes(name) ? name : 'home';
  state.tab = tab;
  TABS.forEach((item) => {
    $(`tab-${item}`).classList.toggle('hidden', item !== tab);
  });
  const navName = NAV_FOR_TAB[tab] || tab;
  document.querySelectorAll('.nav__item').forEach((item) => {
    const active = item.dataset.tab === navName;
    item.classList.toggle('is-active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  // FAB живёт вне .tab (см. комментарий в index.html), поэтому скрываем
  // его явно — иначе он «прилипнет» поверх любой вкладки.
  const fab = $('addTaskBtn');
  const onTasks = tab === 'tasks';
  fab.classList.toggle('hidden', !onTasks);
  fab.setAttribute('aria-hidden', onTasks ? 'false' : 'true');
  updateChatBar();
  syncFloatingPad();
  if (tab === 'home') renderHome();
  if (tab === 'tasks') loadTasks();
  if (tab === 'chats') loadChats();
  if (tab === 'accounts') loadAccounts();
  if (tab === 'library') loadLibrary();
  // Прокрутка у документа общая: без сброса новый экран открывается с середины.
  window.scrollTo({ top: 0, behavior: 'auto' });
}

/* У нижнего запаса один хозяин: на задачах — FAB, на чатах — панель выбора.
   Иначе две функции наперегонки ставили и снимали отступ, и на задачах он
   пропадал. Высоту берём с живого элемента: она зависит от переносов подписей. */
function syncFloatingPad() {
  const onTasks = !$('tab-tasks').classList.contains('hidden');
  const onChats = !$('tab-chats').classList.contains('hidden');
  const floating = onTasks
    ? $('addTaskBtn')
    : onChats && state.selectedChats.length
    ? $('chatBar')
    : null;
  const content = document.querySelector('.content');
  content.classList.toggle('content--float', Boolean(floating));
  // +26 px — просвет между кнопкой и последней карточкой плюс её отрыв от навигации.
  content.style.setProperty('--float-h', floating ? `${floating.offsetHeight + 26}px` : '0px');
}

/* ─────────────────────────────── Шапка ───────────────────────────────── */

function renderHeader() {
  const me = state.me;
  if (!me) return;
  state.features = me.features || state.features;
  if (!state.features.account_login_enabled) {
    $('headerSub').textContent = 'кабинет готов · вход аккаунтов на настройке';
    setHeroStatus('вход аккаунтов на настройке');
    return;
  }
  const sub = me.subscription || {};
  $('headerSub').textContent = sub.active
    ? `подписка: ${sub.days_left} дн.`
    : 'подписка не активна';
  setHeroStatus(sub.active ? `подписка ${sub.days_left} дн.` : 'подписка не активна');
}

/* Подпись в герой-блоке: то же, что в шапке, но фразой для человека. */
function setHeroStatus(text) {
  const node = $('heroStatus');
  if (node) node.textContent = text;
}

/* ─────────────────────────────── Главная ─────────────────────────────── */

/* Плитки быстрого старта. Команду берём из каталога — если сервер её не отдал
   (выключена флагом), плитку не рисуем вовсе, чтобы не вести в тупик. */
function renderTiles() {
  const holder = $('homeTiles');
  if (!holder) return;
  const items = TILES.map((tile) => {
    if (tile.tab) return tile;
    const command = state.commands.find((item) => item.id === tile.id);
    if (!command) return null;
    return { ...tile, emoji: command.emoji, ico: icoClass(command.kind), title: command.title };
  }).filter(Boolean);

  holder.innerHTML = items
    .map(
      (tile) => `
      <button class="tile" ${tile.tab ? `data-goto="${tile.tab}"` : `data-command="${tile.id}"`}
              title="${esc(tile.title || tile.name)}">
        <span class="tile__ico ${tile.ico}" aria-hidden="true">${tile.emoji}</span>
        <span class="tile__name">${esc(tile.name)}</span>
      </button>`
    )
    .join('');
}

/* Главная показывает три задачи, которые реально идут прямо сейчас: остальное
   живёт на своей вкладке. Пусто — значит пусто, выдуманных карточек нет. */
function renderHomeTasks() {
  const holder = $('homeTasks');
  if (!holder) return;
  const active = (state.tasksByStatus.active || []).slice(0, 3);
  if (!active.length) {
    const paused = (state.tasksByStatus.paused || []).length;
    holder.innerHTML = emptyHtml(
      '🌙',
      'Пока ничего не работает',
      paused
        ? `${paused} задач(и) на паузе — снимите с паузы или создайте новую.`
        : 'Нажмите корону внизу или «создать задачу» — соберём первую вместе.'
    );
    return;
  }
  holder.innerHTML = active.map((task) => taskCardHtml(task, { compact: true })).join('');
}

function renderHome() {
  renderTiles();
  renderHomeTasks();
}

/* ────────────────── Умный поиск по командам (локальный) ───────────────── */

/* Считаем совпадения фразы с ключевыми словами команд и с их названиями.
   Никакого внешнего AI: разбор строчный, работает офлайн и мгновенно. */
function smartMatch(query) {
  const text = String(query || '').toLowerCase().trim();
  if (!text) {
    return SMART_FALLBACK.map((id) => state.commands.find((item) => item.id === id))
      .filter(Boolean)
      .map((command) => ({ command, why: 'чаще всего запускают' }));
  }

  const words = text.split(/[^a-zа-яё0-9@+]+/i).filter((word) => word.length > 2);
  const scored = state.commands.map((command) => {
    const hints = SMART_WORDS[command.id] || [];
    let score = 0;
    let why = '';
    hints.forEach((hint) => {
      if (text.includes(hint)) {
        score += 3;
        // Показывать сам стем («похоже на «собра…»») бессмысленно — человек
        // видит обрубок слова и не понимает, что произошло.
        if (!why) why = 'по вашей фразе';
      }
    });
    const title = command.title.toLowerCase();
    const description = (command.description || '').toLowerCase();
    words.forEach((word) => {
      if (title.includes(word)) {
        score += 2;
        if (!why) why = 'совпало название';
      } else if (description.includes(word)) {
        score += 1;
        if (!why) why = 'совпало описание';
      }
    });
    return { command, score, why: why || 'по описанию' };
  });

  const ranked = scored
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
  // Если фраза попала по смыслу (стем-подсказка — это 3 балла), слабые
  // однословные совпадения только мешают выбрать: «собрать участников чата»
  // не должно тянуть за собой «Рассылку по чатам» из-за слова «чата».
  const strong = ranked.filter((item) => item.score >= 3);
  return (strong.length ? strong : ranked).slice(0, 3);
}

function renderPromptHits() {
  const holder = $('promptHits');
  if (!holder) return;
  const query = ($('promptInput').value || '').trim();
  // Пустое поле — молчим: подсказки под пустым вопросом выглядят как мусор.
  if (!query) {
    state.promptHits = [];
    holder.innerHTML = '';
    return;
  }
  state.promptHits = smartMatch(query);
  if (!state.promptHits.length) {
    holder.innerHTML = `
      <button class="prompt-hit" data-goto="commands">
        <span class="tile__ico ico--violet" aria-hidden="true">🔍</span>
        <span>
          <span class="prompt-hit__title">Не подобрала команду</span><br>
          <span class="prompt-hit__why">Откройте каталог — там все девять</span>
        </span>
      </button>`;
    return;
  }
  holder.innerHTML = state.promptHits
    .map(
      ({ command, why }) => `
      <button class="prompt-hit" data-command="${command.id}">
        <span class="tile__ico ${icoClass(command.kind)}" aria-hidden="true">${command.emoji}</span>
        <span>
          <span class="prompt-hit__title">${esc(command.title)}</span><br>
          <span class="prompt-hit__why">${esc(why)}</span>
        </span>
      </button>`
    )
    .join('');
}

/* Enter в поле — сразу открыть лучшую догадку: лишний тап не нужен. */
function runSmartSearch() {
  const query = ($('promptInput').value || '').trim();
  if (!query) {
    switchTab('commands');
    return;
  }
  const hits = smartMatch(query);
  if (!hits.length) {
    $('commandSearch').value = query;
    switchTab('commands');
    renderCommands();
    return;
  }
  openCommand(hits[0].command.id);
}

/* Открыть команду по id: одна дорога для плиток, подсказок и каталога. */
function openCommand(id) {
  const command = state.commands.find((item) => item.id === id);
  if (!command) {
    toast('Каталог команд ещё не загружен');
    return;
  }
  if (command.status === 'ready') {
    openTaskSheet(command);
  } else if (command.status === 'setup_required') {
    toast('Подключение аккаунтов на настройке. Кабинет работает, команды включатся вместе с ним.');
  } else {
    toast(`${command.title} — выключено в настройках сервиса`);
  }
}

/* ─────────────────────────────── Команды ─────────────────────────────── */

async function loadCommands() {
  const holder = $('commandList');
  beginLoad(holder, 'card', 4);
  try {
    const data = await api('/api/commands');
    state.commands = data.commands;
    state.commandGroups = data.groups || [];
    endLoad(holder);
    renderCommands();
  } catch (error) {
    failLoad(holder, error, 'loadCommands');
  }
}

/* Статус команды с точки зрения пользователя, а не сервиса.
   Раньше метка была «включено / на настройке» — и читалась как «задача активна»,
   хотя это значило лишь «сервис поддерживает команду». Теперь честно считаем
   по задачам пользователя: «не запущено» по умолчанию, «N активных» если есть,
   «M на паузе» если только на паузе, «выключено» / «на настройке» — по флагам
   сервера. Так пользователь видит, что реально работает, а что — пусто. */
const STATUS_LABELS = {
  off: 'выключено',
  setup: 'на настройке',
  idle: 'не запущено',
  ready: 'работает',
  paused: 'на паузе',
};

function computeCommandState(command) {
  if (command.status === 'setup_required') {
    return { label: STATUS_LABELS.setup, kind: 'setup', active: 0, paused: 0 };
  }
  if (command.status === 'disabled') {
    return { label: STATUS_LABELS.off, kind: 'off', active: 0, paused: 0 };
  }
  // status === 'ready': берём готовый агрегат по всем трём спискам задач
  const counts = state.taskCounts[command.kind] || { active: 0, paused: 0, done: 0 };
  if (counts.active > 0) {
    const parts = [`${counts.active} активн.`];
    if (counts.paused > 0) parts.push(`${counts.paused} пауз.`);
    if (counts.done > 0) parts.push(`${counts.done} архив`);
    return { label: parts.join(' · '), kind: 'ready', ...counts };
  }
  if (counts.paused > 0) {
    return { label: `${counts.paused} на паузе`, kind: 'paused', ...counts };
  }
  if (counts.done > 0) {
    return { label: `${counts.done} в архиве`, kind: 'paused', ...counts };
  }
  return { label: STATUS_LABELS.idle, kind: 'off', active: 0, paused: 0, done: 0 };
}

/* Карточка команды: эмодзи, название, описание, статус — по одной строке
   каждое. Раньше статус стоял рядом с названием и на узком экране срывался на
   вторую строку, разъезжая с эмодзи и шевроном; подсказка «как пользоваться»
   лежала прямо в списке и делала его нечитаемым. Подсказка теперь живёт в
   шторке команды (там она и нужна — при заполнении полей). */
function commandCardHtml(command) {
  const meta = computeCommandState(command);
  // Карточка кликабельна ТОЛЬКО если команда доступна на сервере.
  // «На настройке» — карточка остаётся видимой, но реагирует тостом.
  const clickable = command.status === 'ready';
  const tag = `card--cmd${clickable ? '' : ' card--cmd--locked'}`;
  // Метки — то, чем команда отличается от соседней по списку (чей текст, как
  // расходится, когда срабатывает). Без них четыре команды «в несколько чатов»
  // читались как одна и та же. Сервер их может не прислать (старый бэкенд) —
  // тогда подвал остаётся со одним статусом, как раньше.
  const tags = (command.tags || [])
    .map((item) => `<span class="cmd__tag">${esc(item)}</span>`)
    .join('');
  return `
    <button class="${tag}" data-command="${command.id}">
      <div class="cmd__ico ${icoClass(command.kind)}" aria-hidden="true">${command.emoji}</div>
      <div class="cmd__main">
        <div class="cmd__title">${esc(command.title)}</div>
        <div class="cmd__desc">${esc(command.description)}</div>
        <div class="cmd__foot">
          <span class="status status--${meta.kind}">
            <span class="status__dot" aria-hidden="true"></span>${esc(meta.label)}
          </span>
          ${tags}
        </div>
      </div>
      <span class="cmd__chevron" aria-hidden="true">›</span>
    </button>`;
}

/* Чипсы каталога: «все» + блоки, которые прислал сервер. Счётчик в чипсе
   считается по тем же данным, что и список, — расходиться им негде. */
function renderCommandTags() {
  const holder = $('commandTags');
  if (!holder) return;
  const groups = state.commandGroups;
  if (!groups.length) {
    holder.innerHTML = '';
    return;
  }
  const chip = (id, title, count) => `
    <button class="chip${state.commandGroup === id ? ' is-active' : ''}"
            data-group="${id || ''}" role="tab"
            aria-selected="${state.commandGroup === id ? 'true' : 'false'}">
      ${esc(title)} <b>${count}</b>
    </button>`;
  holder.innerHTML =
    chip(null, 'все', state.commands.length) +
    groups
      .map((group) =>
        chip(group.id, group.title, state.commands.filter((item) => item.group === group.id).length)
      )
      .join('');
}

function renderCommands() {
  const query = ($('commandSearch').value || '').toLowerCase();
  const list = state.commands.filter(
    (command) =>
      (!state.commandGroup || command.group === state.commandGroup) &&
      (!query ||
        command.title.toLowerCase().includes(query) ||
        command.description.toLowerCase().includes(query))
  );
  renderCommandTags();

  if (!list.length) {
    $('commandList').innerHTML = emptyHtml(
      '🔍',
      'Ничего не найдено',
      state.commandGroup ? 'Попробуйте другой запрос или снимите фильтр' : 'Попробуйте другой запрос'
    );
    return;
  }

  // Блоки идут в порядке, который задал сервер; пустые (например, всё
  // отфильтровано поиском) не рисуем вовсе — заголовок без карточек не нужен.
  // При выбранном чипсе заголовки не нужны: блок ровно один, и он уже подписан.
  const byGroups = Boolean(state.commandGroups.length) && !state.commandGroup;
  const groups = byGroups ? state.commandGroups : [{ id: null, title: '' }];
  const blocks = groups.map((group) => ({
    title: group.title,
    items: list.filter((command) => (group.id ? command.group === group.id : true)),
  }));
  // Команда с неизвестной группой (сервер завёл новую, кабинет ещё не знает)
  // не должна пропадать из каталога — собираем такие в «прочее». При активном
  // чипсе этого блока нет: единственный блок уже содержит все карточки.
  if (byGroups) {
    const known = new Set(state.commandGroups.map((group) => group.id));
    const rest = list.filter((command) => !known.has(command.group));
    if (rest.length) blocks.push({ title: 'прочее', items: rest });
  }

  $('commandList').innerHTML = blocks
    .filter((block) => block.items.length)
    .map(
      (block) =>
        (block.title ? `<div class="section-label">${esc(block.title)}</div>` : '') +
        block.items.map(commandCardHtml).join('')
    )
    .join('');
}

/* ──────────────────────────────── Задачи ─────────────────────────────── */

async function loadTasks(targetStatus) {
  const status = targetStatus || state.taskStatus;
  const holder = $('taskList');
  // Скелетон рисуем только когда это активная вкладка и в ней ещё нет данных.
  if (status === state.taskStatus && !state.tasksByStatus[status].length) {
    beginLoad(holder, 'plain', 3);
  }
  try {
    const data = await api(`/api/tasks?status=${status}`);
    state.tasksByStatus[status] = data.tasks || [];
    if (status === state.taskStatus) state.tasks = state.tasksByStatus[status];
    // Все списки под рукой — пересчитываем агрегат по командам.
    aggregateTaskCounts();
    if (status === state.taskStatus) {
      endLoad(holder);
      renderTasks(state.tasksByStatus[status]);
    }
    // «Главная» показывает активные задачи — обновляем её вместе со списком.
    if (status === 'active') renderHomeTasks();
    renderCommands();
  } catch (error) {
    if (status === state.taskStatus) failLoad(holder, error, 'loadTasks');
  }
}

/* Переключение сегмента «Активные / На паузе / Завершённые». Отдельной
   функцией, потому что дорога сюда не одна: сам сегмент, «Архив задач» из
   «Ещё» и обновление после действий над задачей. */
function setTaskStatus(status) {
  state.taskStatus = status;
  document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
    const active = seg.dataset.status === status;
    seg.classList.toggle('is-active', active);
    seg.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  // Если данные для этого статуса уже подгружены (агрегатор на boot),
  // показываем их без сети; иначе подгружаем.
  const cached = state.tasksByStatus[status];
  if (cached && cached.length) renderTasks(cached);
  else loadTasks();
}

function aggregateTaskCounts() {
  const counts = {};
  for (const list of Object.values(state.tasksByStatus)) {
    for (const task of list) {
      const kind = task.kind || 'forward';
      if (!counts[kind]) counts[kind] = { active: 0, paused: 0, done: 0 };
      if (task.enabled && !task.archived) counts[kind].active++;
      else if (!task.archived) counts[kind].paused++;
      else counts[kind].done++;
    }
  }
  state.taskCounts = counts;
}

/* Метка состояния задачи. «Нет связи» важнее «работает»: включённая задача при
   отключённом аккаунте не делает ничего, и об этом надо сказать прямо. Сбой —
   там же: задача, которая последние разы только падала, «работает» лишь на
   бумаге. Сравнение строгое (=== false): в демо-данных поля просто нет. */
/* Признак «абонемент кончился» — общий для всех задач, поэтому берётся из
   состояния кабинета, а не из полей задачи: сервер уже отдаёт его в /api/me,
   и второе такое же поле в каждой задаче было бы тем же самым дважды.
   Строгое `=== false`: у демо-данных и старого ответа поля нет, и молчать про
   абонемент там правильнее, чем гадать. */
function subscriptionStopped() {
  const sub = (state.me || {}).subscription;
  return !!sub && sub.active === false;
}

function taskBadge(task) {
  if (task.archived) return { kind: 'done', label: 'завершена' };
  if (!task.enabled) return { kind: 'paused', label: 'пауза' };
  // Кончившийся абонемент — впереди «нет связи»: пересылка выключена целиком,
  // и связь с аккаунтом тут уже ничего не меняет. Иначе карточка писала
  // «работает» ровно тогда, когда сервис молча ничего не делает.
  if (subscriptionStopped()) return { kind: 'error', label: 'нет абонемента' };
  if (task.account_online === false) return { kind: 'error', label: 'нет связи' };
  if (task.health && task.health.failing) return { kind: 'error', label: 'сбой' };
  if (task.oneshot) return { kind: 'plan', label: 'по кнопке' };
  return { kind: 'live', label: 'работает' };
}

/* «5 минут назад» — по метке времени с сервера (она приходит в UTC с явной
   пометкой, иначе браузер прочитал бы её как местное время). Ровно то, что
   человеку нужно от журнала: давно ли задача что-то делала. */
function timeAgo(iso) {
  const at = iso ? new Date(iso).getTime() : 0;
  if (!at || Number.isNaN(at)) return '';
  const sec = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (sec < 60) return 'только что';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} ${pluralRu(min, 'минуту', 'минуты', 'минут')} назад`;
  const hours = Math.round(min / 60);
  if (hours < 24) return `${hours} ${pluralRu(hours, 'час', 'часа', 'часов')} назад`;
  const days = Math.round(hours / 24);
  return `${days} ${pluralRu(days, 'день', 'дня', 'дней')} назад`;
}

/* Русский счёт: 1 минуту / 2 минуты / 5 минут. */
function pluralRu(n, one, few, many) {
  const tail = n % 100;
  if (tail >= 11 && tail <= 14) return many;
  const last = n % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

/* Часы этого устройства: смещение от UTC в минутах (Москва — 180). Именно его
   кабинет отдаёт задаче вместе с окном постинга: сервер стоит в UTC, и без
   смещения московское «окно 10:00–20:00» работало на нём 13:00–23:00 по Москве —
   последний круг уходил людям в полночь. */
function browserTz() {
  return -new Date().getTimezoneOffset();
}

/* Смещение из ответа сервера. Непонятное значение — как отсутствие: значит окно
   считается по часам сервера, и карточка обязана сказать это словами (тем же
   правилом, что jobs.window_tz_minutes). */
function windowTz(value) {
  if (value === null || value === undefined || value === '') return null;
  const minutes = Number(value);
  if (!Number.isFinite(minutes) || Math.abs(minutes) > 14 * 60) return null;
  return Math.trunc(minutes);
}

/* «UTC+3» / «UTC−4:30» / «UTC» — подпись часового пояса окна. */
function tzLabel(minutes) {
  const total = Math.abs(minutes);
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  if (!minutes) return 'UTC';
  return `UTC${minutes > 0 ? '+' : '−'}${hours}${rest ? `:${String(rest).padStart(2, '0')}` : ''}`;
}

/* Окно постинга на карточке: время и чьи это часы. Своё смещение не подписываем —
   человек и так смотрит на свои часы; чужое (задачу ставили в другом поясе) и
   серверное (задача старше настройки) называем прямо, иначе окно читается как
   местное, а работает на три часа позже. */
function windowLine(task) {
  const line = `окно ${task.window_start}–${task.window_end}`;
  const tz = windowTz(task.window_tz);
  if (tz === null) return `${line} по часам сервера`;
  return tz === browserTz() ? line : `${line} (${tzLabel(tz)})`;
}

/* Приписка к подсказке правки: окно этой задачи считается по чужим часам.
   Сохранение поставит часы этого устройства — сказать об этом надо до, а не
   после того, как круги уехали на три часа. */
function windowMigrationHint(task) {
  if (!task || (task.kind || '') !== 'poster' || !task.edit) return '';
  const stored = windowTz(task.edit.tz);
  const mine = browserTz();
  if (stored === mine) return '';
  const was = stored === null ? 'по часам сервера' : `по ${tzLabel(stored)}`;
  return ` Окно этой задачи считалось ${was} — сохраните, и оно станет по вашим ` +
    `часам (${tzLabel(mine)}).`;
}

/* Строка сбоя на карточке. Раньше причину было видно только в логе службы на
   сервере: человек смотрел на бодрое «работает» и ждал сообщений, которых нет.
   Показываем и починенный сбой — «в три чата не ушло» надо знать, даже когда
   остальные сто получили; тогда строка спокойнее по цвету. */
function taskAlertHtml(task) {
  // Кончившийся абонемент объясняем прямо на карточке: значка «нет абонемента»
  // мало — из него не видно, что делать. Только у задач, которые иначе шли бы:
  // архив и пауза стоят по своей причине, и абонемент им ничего не менял.
  if (!task.archived && task.enabled && subscriptionStopped()) {
    return `
      <div class="task__alert">
        <span>⛔ Абонемент закончился — задача стоит. Продлите на вкладке «Оплата»,
        и она пойдёт сама: настройки на месте.</span>
      </div>`;
  }
  const health = task.health || {};
  if (!health.error) return '';
  const when = timeAgo(health.error_at);
  return `
    <div class="task__alert${health.failing ? '' : ' task__alert--past'}">
      <span>⚠️ ${esc(health.error)}</span>
      ${when ? `<i>${esc(when)}</i>` : ''}
    </div>`;
}

/* Строка под названием: что это за задача и как настроена. */
function taskMetaLines(task) {
  const kind = task.kind || 'forward';
  const isForward = kind === 'forward';
  const lines = [task.kind_label || (isForward ? 'пересылка' : kind)];
  if (isForward) lines.push(task.mode === 'copy' ? 'копия без метки' : 'обычный форвард');
  // Сколько чатов у задачи — первым делом: у постинга и рассылки это главное
  // число задачи, и в заголовке оно есть только когда чатов больше одного.
  if (task.targets_count) lines.push(`${task.targets_count} ${chatWord(task.targets_count)}`);
  if (kind === 'poster') {
    // Авто-постер: показываем расписание вместо «задержки в секундах».
    lines.push(`каждые ${task.interval_min || 1} мин`);
    if (task.window_start && task.window_end) {
      lines.push(windowLine(task));
    }
    // Что уходит — теми же словами, что у рассылки: тексты обеих задач лежат в
    // библиотеке, поэтому и «вся библиотека», и повисшие ссылки бывают у обеих.
    if (task.messages_count) lines.push(`${task.messages_count} сообщ.`);
    else if (task.whole_library) lines.push('вся библиотека');
    else if (task.messages_gone) lines.push('постить нечего');
  } else if (kind === 'mailing') {
    // Рассылка: её расписание — это паузы и число кругов, а не «задержка».
    const info = task.mailing || {};
    lines.push(`пауза ${info.gap_seconds || 5} сек`);
    if (info.messages_count) lines.push(`${info.messages_count} сообщ.`);
    else if (info.whole_library) lines.push('вся библиотека');
    // Сообщения удалили из библиотеки, а рассылка на них ссылается: без этой
    // строки карточка бодро «работает», а в чаты ничего не уходит.
    else if (info.messages_gone) lines.push('рассылать нечего');
    lines.push(info.repeats ? `${info.repeats} круг(ов)` : 'круги без конца');
  } else if (task.oneshot) {
    lines.push('запуск по кнопке');
  } else {
    lines.push(`задержка ${task.delay} сек`);
  }
  return lines;
}

/* Полоса прогресса. Долю рисуем только там, где сервер знает «сколько всего»
   (парсер — из лимита, автоподписка — из списка ссылок). У постоянных задач
   конца нет, и вместо выдуманной доли идёт бегунок: честнее пустой шкалы.
   Справа — когда задача последний раз сработала: по одному счётчику не понять,
   идёт работа прямо сейчас или встала неделю назад. */
function taskProgressHtml(task) {
  const progress = task.progress || {};
  const done = Number(progress.done || 0);
  const total = Number(progress.total || 0);
  const ago = timeAgo((task.health || {}).ok_at);
  const when = ago ? `<span class="task__ago">${esc(ago)}</span>` : '';
  if (!total) {
    return `
      <div class="task__progress task__progress--endless"><span></span></div>
      <div class="task__nums"><b>${done}</b><i>обработано</i>${when}</div>`;
  }
  const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
  return `
    <div class="task__progress"><span style="width:${pct}%"></span></div>
    <div class="task__nums">
      <b>${done}</b><i>из ${total}</i>${when}<span class="task__pct">${pct}%</span>
    </div>`;
}

/* Главная кнопка карточки: «Пауза» для постоянных задач и «Запустить» для
   разовых. Вынесена отдельно, потому что нужна и полной карточке, и краткой
   на «Главной» — двух разных реализаций тут быть не должно. */
function taskPauseButton(task) {
  if (task.oneshot) {
    return `<button class="btn" data-action="run" data-id="${task.id}">▶️ Запустить</button>`;
  }
  return (
    `<button class="btn" data-action="toggle" data-id="${task.id}">` +
    (task.enabled ? '⏸ Пауза' : '▶️ Запустить') +
    '</button>'
  );
}

/* Кнопки карточки. Один и тот же набор работает и на «Задачах», и на
   «Главной»: слушатель делегирован на оба списка (см. bindEvents). */
function taskActionsHtml(task) {
  const kind = task.kind || 'forward';
  const acts = [];
  if (task.archived) {
    acts.push(`<button class="btn" data-action="unarchive" data-id="${task.id}">↩︎ Из архива</button>`);
  } else {
    acts.push(taskPauseButton(task));
    // «Настроить» — вместо «удалить и создать заново»: у пересозданной задачи
    // обнулялись счётчики, а у рассылки терялось место в круге.
    if (task.edit) {
      acts.push(`<button class="btn" data-action="edit" data-id="${task.id}">⚙️ Настроить</button>`);
    }
    if (kind === 'forward') {
      acts.push(`<button class="btn" data-action="mode" data-id="${task.id}">🔁 Режим</button>`);
    }
    if (RESULTS_TITLES[kind] || task.oneshot) {
      acts.push(`<button class="btn" data-action="results" data-id="${task.id}">📄 Результаты</button>`);
    }
    acts.push(`<button class="btn" data-action="archive" data-id="${task.id}">📦 Архив</button>`);
  }
  // Корзина стоит отдельным столбцом, а не в общем ряду: иначе при переносе
  // она уезжала на пустую строку одна, и карточка выглядела оборванной.
  return `
    <div class="task__actions">
      <div class="task__acts">${acts.join('')}</div>
      <button class="btn btn--danger" data-action="delete" data-id="${task.id}">🗑</button>
    </div>`;
}

function taskCardHtml(task, options) {
  const kind = task.kind || 'forward';
  const badge = taskBadge(task);
  // На «Главной» карточка — только сводка: полный набор кнопок живёт на вкладке
  // «Задачи», иначе главный экран превращается в её копию. Пауза остаётся —
  // это то действие, которое нужно срочно и на бегу.
  const compact = Boolean(options && options.compact);
  const actions = compact
    ? `<div class="task__actions">
         <div class="task__acts">${taskPauseButton(task)}</div>
         <button class="btn" data-goto="tasks" type="button">открыть</button>
       </div>`
    : taskActionsHtml(task);
  return `
    <div class="task${task.archived ? ' task--archived' : ''}">
      <div class="task__top">
        <span class="task__ico ${icoClass(kind)}" aria-hidden="true">${task.emoji || kindEmoji(kind)}</span>
        <div class="task__head">
          <div class="task__title">${esc(task.title)}</div>
          <div class="task__meta">${esc(taskMetaLines(task).join(' · '))}</div>
        </div>
        <span class="badge badge--${badge.kind}">${badge.label}</span>
      </div>
      ${taskProgressHtml(task)}
      ${taskAlertHtml(task)}
      ${actions}
    </div>`;
}

function renderTasks(tasks) {
  const holder = $('taskList');
  // Подпись экрана честно считает по всем трём спискам, а не по видимому.
  // Без абонемента включённые задачи не работают, а стоят — иначе подпись
  // писала «4 работают» прямо над четырьмя карточками «нет абонемента».
  const counts = state.tasksByStatus;
  const live = (counts.active || []).length;
  $('taskSummary').textContent =
    `${live} ${subscriptionStopped() ? 'ждут абонемента' : 'работают'} · ` +
    `${(counts.paused || []).length} на паузе · ${(counts.done || []).length} в архиве`;

  // tasksByStatus[active] может быть пустым просто потому, что у пользователя
  // нет активных рассылок. Скелетон в этом случае не нужен — покажем сразу
  // дружелюбное пустое состояние.
  if (!tasks || !tasks.length) {
    const texts = {
      active: ['✅', 'Нет задач', 'Здесь появятся активные рассылки и триггеры. Запустите первую — она будет работать, даже когда вы офлайн.'],
      paused: ['⏸', 'Нет задач на паузе', 'Остановленные задачи можно вернуть в работу одним нажатием.'],
      done: ['📦', 'Завершённых задач нет', 'Архив появится здесь после первых запусков.'],
    }[state.taskStatus];
    holder.innerHTML = emptyHtml(texts[0], texts[1], texts[2]);
    return;
  }

  holder.innerHTML = tasks.map(taskCardHtml).join('');
}

const ACTION_MESSAGES = {
  delete: 'Задача удалена',
  archive: 'Задача в архиве',
  unarchive: 'Задача вернулась в работу',
  mode: 'Режим переключён',
  toggle: 'Готово',
};

/* Любое действие, меняющее состав задач — обновляет не только текущую
   вкладку, но и счётчики по всем трём статусам, чтобы бейджи команд
   оставались честными. */
async function refreshAllTaskLists() {
  const visible = state.taskStatus;
  await Promise.all([
    loadTasks('active'),
    loadTasks('paused'),
    loadTasks('done'),
  ]);
  if (state.taskStatus !== visible) {
    state.taskStatus = visible;
    document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
      const active = seg.dataset.status === visible;
      seg.classList.toggle('is-active', active);
      seg.setAttribute('aria-selected', active ? 'true' : 'false');
    });
  }
  renderTasks(state.tasksByStatus[visible]);
  renderHomeTasks();
  renderCommands();
}

async function taskAction(action, id, button) {
  try {
    if (action === 'edit') {
      openTaskEdit(id);
      return;
    }
    if (action === 'toggle') await withLoading(button, () => api(`/api/tasks/${id}/toggle`, { method: 'POST' }));
    if (action === 'mode') await withLoading(button, () => api(`/api/tasks/${id}/mode`, { method: 'POST' }));
    if (action === 'archive') await withLoading(button, () => api(`/api/tasks/${id}/archive`, { method: 'POST' }));
    if (action === 'unarchive') await withLoading(button, () => api(`/api/tasks/${id}/archive?undo=1`, { method: 'POST' }));
    if (action === 'delete') await withLoading(button, () => api(`/api/tasks/${id}`, { method: 'DELETE' }));
    if (action === 'results') {
      await openResults(id);
      return;
    }
    if (action === 'run') {
      const data = await withLoading(button, () => api(`/api/tasks/${id}/run`, { method: 'POST' }));
      toast(runMessage(data.run));
      await refreshAllTaskLists();
      return;
    }
    toast(ACTION_MESSAGES[action] || 'Готово');
    await refreshAllTaskLists();
  } catch (error) {
    toast(error.message);
  }
}

/* Сводка запуска разовой задачи → понятная фраза для тоста. Заходы в чаты
   объясняем целиком: «вступили в 0 из 5» человек читает как поломку, хотя в
   четырёх аккаунт уже сидел, а в пятый его не пустил админ. Отказ на середине
   («подождите 40 сек») тоже не отменяет сделанного — сколько успели, столько и
   говорим. Полностью тот же итог остаётся на карточке: сервер пишет его в
   журнал задачи, и подсказка больше не единственное место, где он был. */
function runMessage(run) {
  if (!run) return 'Готово';
  const done = [];
  if (run.joined) done.push(`вступили в ${run.joined}`);
  if (run.already) done.push(`уже были в ${run.already}`);
  if (!run.ok) {
    const reason = run.error || 'Запуск не удался';
    return done.length ? `${reason} — ${done.join(', ')}` : reason;
  }
  if (run.collected != null) return `Собрано участников: ${run.collected}`;
  if (run.joined != null) {
    const parts = [`Вступили в чаты: ${run.joined} из ${run.total}`];
    if (run.already) parts.push(`уже были в ${run.already}`);
    if ((run.problems || []).length) parts.push(run.problems[0]);
    return parts.join(', ');
  }
  return 'Готово';
}

async function openResults(id) {
  const task = (state.tasks || []).find((item) => item.id === Number(id));
  $('resultsTitle').textContent = RESULTS_TITLES[task ? task.kind : ''] || 'Результаты';
  const body = $('resultsBody');
  state.lastResultsId = id;
  beginLoad(body, 'plain', 4);
  $('resultsSheet').classList.add('is-open');

  try {
    const data = await api(`/api/tasks/${id}/results?limit=100`);
    endLoad(body);
    if (!data.items.length) {
      body.innerHTML = emptyHtml(
        '📭',
        'Пока пусто',
        'Задача ещё ничего не насобирала. Запустите её и вернитесь сюда.'
      );
      return;
    }
    const rows = data.items.map((item) => {
      const payload = item.payload || {};
      if (payload.username || payload.user_id != null) {
        const name = payload.name || payload.username || `ID ${payload.user_id}`;
        const handle = payload.username ? `@${payload.username}` : (payload.phone || '');
        return `<div class="result">
          <div class="result__title">${esc(name)}</div>
          <div class="result__sub">${esc(handle || 'без ника')} · ID ${esc(payload.user_id)}</div>
        </div>`;
      }
      const text = payload.text || payload.link || payload.preview || JSON.stringify(payload);
      return `<div class="result">
        <div class="result__title">${esc(String(text).slice(0, 140))}</div>
        <div class="result__sub">${esc((item.created_at || '').replace('T', ' ').slice(0, 16))}</div>
      </div>`;
    });
    body.innerHTML =
      `<div class="results__meta">Всего: ${data.total}${data.total > data.items.length ? ` · показаны последние ${data.items.length}` : ''}</div>` +
      rows.join('');
  } catch (error) {
    failLoad(body, error, 'openResults');
  }
}

/* ───────────────────────────────── Чаты ──────────────────────────────── */

/* Как передать выбранный чат в форму задачи: приоритет у @username (его
   сервер резолвит надёжно по manager.resolve_chat). Если username нет —
   подставляем «id» числом: бэк понимает любой из этих форматов. */
function chatToRef(chat) {
  if (!chat) return '';
  if (chat.username) return `@${String(chat.username).replace(/^@/, '')}`;
  return String(chat.id);
}

function chatTitle(chat) {
  if (!chat) return '';
  return chat.title || String(chat.id || '');
}

/* Текущая выборка чатов в читабельном виде: «Aльфа, Браво, +2». */
function selectedChatsSummary() {
  const n = state.selectedChats.length;
  if (n === 0) return '';
  const first = state.selectedChats.slice(0, 2).map(chatTitle).join(', ');
  return n > 2 ? `${first}, +${n - 2}` : first;
}

function getSelectedChats() {
  return state.selectedChats;
}

function isChatSelected(id) {
  return state.selectedChats.some((chat) => chat.id === id);
}

function toggleChatSelection(chat) {
  if (!chat || chat.id == null) return;
  const idx = state.selectedChats.findIndex((item) => item.id === chat.id);
  if (idx >= 0) state.selectedChats.splice(idx, 1);
  else state.selectedChats.push(chat);
  updateChatBar();
}

function clearChatSelection() {
  state.selectedChats = [];
  // Перерисовать отметки на текущем списке, не дёргая сервер
  document.querySelectorAll('#chatList .chat.is-selected').forEach((node) => {
    node.classList.remove('is-selected');
    const check = node.querySelector('.chat__check');
    if (check) check.textContent = '';
  });
  updateChatBar();
}

function updateChatBar() {
  const bar = $('chatBar');
  if (!bar) return;
  const n = state.selectedChats.length;
  const onChats = !$('tab-chats').classList.contains('hidden');
  const shown = onChats && n > 0;
  // Панель вынесена из вкладки (см. index.html), поэтому вместе с вкладкой уже
  // не исчезает — гасим руками, иначе она всплывёт поверх задач и аккаунтов.
  bar.classList.toggle('hidden', !onChats);
  bar.classList.toggle('is-visible', shown);
  bar.setAttribute('aria-hidden', shown ? 'false' : 'true');
  const countEl = $('chatBarCount');
  if (countEl) countEl.textContent = String(n);
  const sumEl = $('chatBarSummary');
  if (sumEl) sumEl.textContent = n ? selectedChatsSummary() : '';
  // Панель висит поверх списка, как и FAB: пока она видна, последнему чату
  // нужен запас снизу, иначе выбранный чат прячется под своей же кнопкой.
  // Считаем после подписи — от неё зависит высота панели.
  syncFloatingPad();
}

/* Тип чата словами, а не только картинкой: «канал» и «группа» ведут себя
   по-разному (в канал нужно право публиковать, из группы можно собрать людей),
   и по одному эмодзи это не читается. */
function chatKind(chat) {
  if (chat.is_channel) return { emoji: '📢', label: 'канал' };
  if (chat.is_group) return { emoji: '👥', label: 'группа' };
  return { emoji: '💬', label: 'диалог' };
}

async function loadChats() {
  const holder = $('chatList');
  const account = state.accounts[0];

  if (!state.features.account_login_enabled) {
    holder.innerHTML = emptyHtml('⚙️', 'Нужен MTProto-вход', 'Список чатов появится после заполнения API_ID/API_HASH и подключения аккаунта по телефону.');
    updateChatBar();
    return;
  }

  if (!account) {
    holder.innerHTML = emptyHtml('👤', 'Нет аккаунта', 'Подключите аккаунт во вкладке «Аккаунты».');
    updateChatBar();
    return;
  }

  const query = encodeURIComponent($('chatSearch').value || '');
  beginLoad(holder, 'plain', 5);
  try {
    const data = await api(`/api/chats?account_id=${account.id}&q=${query}`);
    endLoad(holder);
    state.chats = data.chats || [];
    rememberChatNames(state.chats);
    if (!data.online) {
      holder.innerHTML = emptyHtml('📴', 'Аккаунт не в сети', 'Перезапустите аккаунт в боте.');
      updateChatBar();
      return;
    }
    if (!state.chats.length) {
      holder.innerHTML = emptyHtml('💬', 'Ничего не найдено', 'Измените запрос или тег.');
      updateChatBar();
      return;
    }
    // Рисуем чаты КАК КНОПКИ: теперь их можно выбрать мышкой.
    // Выделение сохраняется между поисковыми запросами — хранится в state,
    // поэтому id чата не «теряется» после фильтра поиска.
    holder.innerHTML = state.chats.map((chat) => {
      const selected = isChatSelected(chat.id);
      const kind = chatKind(chat);
      return `
        <button class="chat${selected ? ' is-selected' : ''}" data-chat-id="${chat.id}" type="button">
          <div class="chat__emoji">${kind.emoji}</div>
          <div class="chat__body">
            <div class="chat__title">${esc(chat.title)}</div>
            <div class="chat__sub"><code>${chat.id}</code>${chat.username ? ' · @' + esc(chat.username) : ''}</div>
          </div>
          <span class="chat__kind">${kind.label}</span>
          <span class="chat__check" aria-hidden="true">${selected ? '✓' : ''}</span>
        </button>`;
    }).join('');
    updateChatBar();
  } catch (error) {
    failLoad(holder, error, 'loadChats');
    updateChatBar();
  }
}

/* Из выбранных чатов — открыть шторку задачи с уже заполненными полями.
   Куда подставить выбранное, спрашиваем у самой команды, а не у её типа: поля
   приходят в needs/optional, и разбор по kind разъезжался с сервером при каждой
   правке каталога (постинг оброс списком чатов — и «выбрать все» складывало
   двести чатов в поле одного приёмника).
     • есть «источник» и нет приёмников (парсер) → выбранный чат в источник;
     • есть «получатели» и нет «приёмника» (постинг, рассылка) → все в список;
     • есть и то и другое (пересылка в чаты) → первый в приёмник, прочие в список;
     • только «приёмник» (пересылка, ловец чеков) → первый выбранный чат. */
function openTaskForSelection(kind) {
  const selected = getSelectedChats();
  if (!selected.length) return;
  const refs = selected.map(chatToRef);
  const command =
    state.commands.find((item) => item.id === kind)
    || state.commands.find((item) => item.kind === kind)
    || state.commands.find((item) => item.id === 'copy_channel');
  if (!command) {
    toast('Каталог команд ещё не загружен');
    return;
  }
  const fields = [...(command.needs || []), ...(command.optional || [])];
  const prefill = {};
  if (fields.includes('targets')) {
    // Приёмник заполняем только когда поле есть у команды: у постинга и рассылки
    // его нет вовсе, и первый чат из выборки просто исчез бы.
    if (fields.includes('target')) {
      prefill.target = refs[0];
      prefill.targets = refs.slice(1);
    } else {
      prefill.targets = refs;
    }
  } else if (fields.includes('target')) {
    prefill.target = refs[0];
  } else if (fields.includes('source')) {
    // Парсер собирает участников ВЫБРАННОГО чата — он и есть источник.
    prefill.source = refs[0];
  }
  openTaskSheet(command, prefill);
}

function renderChatTags() {
  $('chatTags').innerHTML = CHAT_TAGS.map(
    (tag) => `<button class="chip" data-tag="${tag}">${tag}</button>`
  ).join('');
}

/* ─────────────────────── Библиотека сообщений ────────────────────────── */

/* Тексты, которые шлёт «Рассылка по очереди». Экран кабинета вместо двух
   пунктов настроек («Сообщения» и «Библиотека сообщений»), которые лишь
   открывали бота и читались как одно и то же. */

async function loadLibrary() {
  const holder = $('libraryList');
  beginLoad(holder, 'plain', 3);
  try {
    const data = await api('/api/library');
    endLoad(holder);
    state.library = data.items || [];
    renderLibrary();
  } catch (error) {
    failLoad(holder, error, 'loadLibrary');
  }
}

function libraryPreview(item, max = 90) {
  const text = (item.text || '').replace(/\s+/g, ' ').trim();
  if (text) return text.length > max ? `${text.slice(0, max)}…` : text;
  // Пост из канала лежит ссылкой (chat_id + message_id), своего текста у него нет.
  return item.message_id ? `готовый пост · ${item.chat_id}/${item.message_id}` : 'пустое сообщение';
}

/* Две строки записи библиотеки. Заголовок у сохранённых из формы — это начало
   того же текста, и печатать одну строку дважды незачем: вторую показываем
   только когда заголовок — настоящее имя, а не обрезок текста. */
function libraryLines(item, max = 90) {
  const preview = libraryPreview(item, max);
  const title = (item.title || '').trim();
  const sameThing = !title || preview === title || preview.startsWith(title);
  return sameThing ? { head: preview, sub: '' } : { head: title, sub: preview };
}

/* Кто отправляет запись — строкой в её карточке. Библиотека одна на рассылку и
   постинг, поэтому правка и удаление здесь меняют то, что уходит из работающей
   задачи: без этой строки удаление читалось как безобидная уборка. */
function libraryUsers(item) {
  return (item.used_by || []).filter(Boolean);
}

function libraryUseLine(item) {
  const users = libraryUsers(item);
  if (!users.length) return '';
  const word = users.length === 1 ? 'задача' : 'задачи';
  return `<div class="lib__use" title="${esc(users.join(', '))}">📨 отправляют: ${users.length} ${word}</div>`;
}

function renderLibrary() {
  const holder = $('libraryList');
  const label = $('libraryLabel');
  if (label) {
    label.textContent = state.library.length
      ? `сохранённые · ${state.library.length}`
      : 'сохранённые';
  }
  if (!state.library.length) {
    holder.innerHTML = emptyHtml(
      '📚',
      'Библиотека пуста',
      'Добавьте первый текст — он появится в выборе сообщений у рассылки.'
    );
    return;
  }
  holder.innerHTML = state.library.map((item) => {
    const { head, sub } = libraryLines(item);
    const editing = state.libraryEdit && Number(state.libraryEdit.id) === Number(item.id);
    return `
    <div class="lib${editing ? ' is-editing' : ''}">
      <div class="lib__body">
        <div class="lib__title">${esc(head)}</div>
        ${sub ? `<div class="lib__text">${esc(sub)}</div>` : ''}
        ${libraryUseLine(item)}
      </div>
      <div class="lib__acts">
        <button class="lib__del" data-action="edit-library" data-id="${item.id}"
                aria-label="Исправить сообщение" title="Исправить на месте">✏️</button>
        <button class="lib__del" data-action="delete-library" data-id="${item.id}"
                aria-label="Удалить сообщение" title="Удалить из библиотеки">🗑</button>
      </div>
    </div>`;
  }).join('');
}

/* Сохранить набранное в библиотеку. Сообщения делит пустая строка — как и в
   форме задачи: многострочный текст (прайс, объявление в два абзаца) остаётся
   одной записью, а несколько текстов можно сохранить за один раз. */
async function addLibraryItems(button) {
  const field = $('libraryText');
  const blocks = splitMessages(field.value);
  if (!blocks.length) {
    toast('Напишите текст сообщения');
    field.focus();
    return;
  }
  try {
    await withLoading(button, async () => {
      for (const text of blocks) {
        await api('/api/library', {
          method: 'POST',
          body: JSON.stringify({ text }),
        });
      }
    });
    field.value = '';
    renderLibraryDraft();
    toast(blocks.length === 1 ? 'Сообщение сохранено' : `Сохранено сообщений: ${blocks.length}`);
    await loadLibrary();
  } catch (error) {
    toast(error.message);
  }
}

/* Счёт под полем библиотеки: столько записей появится, если нажать «сохранить».
   Без него правило «делит пустая строка» приходится проверять на результате. */
function renderLibraryDraft() {
  const holder = $('libraryDraft');
  if (!holder) return;
  // В правке запись одна, и пустая строка внутри исправленного текста её не
  // делит: счёт «столько записей появится» здесь только сбивал бы с толку.
  const blocks = state.libraryEdit ? [] : splitMessages(($('libraryText') || {}).value);
  holder.hidden = !blocks.length;
  if (!blocks.length) {
    holder.innerHTML = '';
    return;
  }
  const heads = blocks.slice(0, 2).map((block) => messageTitle(block, 24));
  const tail = blocks.length > 2 ? ` и ещё ${blocks.length - 2}` : '';
  holder.innerHTML = `<b>${blocks.length} ${messageWord(blocks.length)}</b>
    <span>${esc(heads.join(' · ') + tail)}</span>`;
}

/* Правка записи идёт тем же полем, что и добавление: запись меняется на месте,
   id остаётся, и исправленный текст сразу уходит из всех задач, где эта запись
   выбрана. Раньше опечатку исправляли «удалить и добавить заново»: у новой
   записи новый id, задача помнила старый и молча оставалась без сообщения. */
function startLibraryEdit(id) {
  const item = state.library.find((row) => Number(row.id) === Number(id));
  if (!item) return;
  // У готового поста своего текста нет — он лежит в канале, и править здесь
  // можно только имя, по которому его узнают в списке.
  const post = !libraryText(item);
  state.libraryEdit = { id: Number(item.id), post };
  const field = $('libraryText');
  field.value = post ? item.title || '' : item.text || '';
  applyLibraryMode();
  renderLibrary();
  field.focus();
  field.scrollIntoView({ block: 'center' });
}

function cancelLibraryEdit() {
  state.libraryEdit = null;
  $('libraryText').value = '';
  applyLibraryMode();
  renderLibrary();
}

/* Подписи поля и кнопок под то, что ими сейчас делают: добавляют или правят.
   Одно поле на два дела без подписей читалось бы как «добавить ещё одну». */
function applyLibraryMode() {
  const edit = state.libraryEdit;
  const label = $('libraryFieldLabel');
  const note = $('libraryNote');
  const add = $('libraryAdd');
  const cancel = $('libraryCancel');
  if (label) {
    label.textContent = edit
      ? (edit.post ? 'Имя готового поста' : 'Правим сообщение')
      : 'Новое сообщение';
  }
  if (note) {
    note.textContent = edit
      ? (edit.post
        ? 'сам пост правят в канале, где он лежит'
        : 'запись меняется на месте: текст поменяется во всех задачах, где она выбрана')
      : 'пустая строка делит сообщения, простой перенос — нет';
  }
  if (add) add.textContent = edit ? '💾 Сохранить правку' : '＋ Сохранить в библиотеку';
  if (cancel) cancel.hidden = !edit;
  renderLibraryDraft();
}

/* Кнопка под полем: в правке сохраняет запись, иначе добавляет новые. */
function submitLibraryField(button) {
  return state.libraryEdit ? saveLibraryEdit(button) : addLibraryItems(button);
}

async function saveLibraryEdit(button) {
  const edit = state.libraryEdit;
  const field = $('libraryText');
  const value = field.value.trim();
  if (!value) {
    // Пустое поле — это удаление записи, а не правка: так и говорим, а стирать
    // текст у работающей рассылки молча не станем.
    toast(edit.post ? 'Дайте посту имя' : 'Текст пустой: чтобы убрать сообщение, удалите запись');
    field.focus();
    return;
  }
  try {
    const data = await withLoading(button, () => api(`/api/library/${edit.id}`, {
      method: 'PATCH',
      body: JSON.stringify(edit.post ? { title: value } : { text: value }),
    }));
    const users = ((data.item || {}).used_by || []).length;
    state.libraryEdit = null;
    field.value = '';
    applyLibraryMode();
    toast(users ? `Исправлено — уйдёт в задачах: ${users}` : 'Сообщение исправлено');
    await loadLibrary();
  } catch (error) {
    toast(error.message);
  }
}

async function deleteLibraryItem(id, button) {
  const item = state.library.find((row) => Number(row.id) === Number(id));
  const users = item ? libraryUsers(item) : [];
  // Называем последствие: если запись держит рассылка или постинг, после
  // удаления задаче может стать нечего отправлять. Обещать «задачи не
  // остановятся» в этом случае — неправда: задача останется в работе, но с
  // пустой очередью.
  const shown = users.slice(0, 2).join(', ');
  const tail = users.length > 2 ? ` и ещё ${users.length - 2}` : '';
  const agreed = await confirmAction(
    users.length
      ? `Убрать сообщение из библиотеки? Его отправляют: ${shown}${tail}. Останутся без него — если других сообщений в задаче нет, отправлять будет нечего.`
      : 'Убрать сообщение из библиотеки? Задачи не остановятся — возьмут то, что осталось.'
  );
  if (!agreed) return;
  try {
    await withLoading(button, () => api(`/api/library/${id}`, { method: 'DELETE' }));
    // Выбор в открытой форме тоже чистим: id больше не существует.
    state.libraryPick = state.libraryPick.filter((row) => String(row) !== String(id));
    renderLibraryPicks();
    if (state.libraryEdit && Number(state.libraryEdit.id) === Number(id)) {
      state.libraryEdit = null;
      $('libraryText').value = '';
      applyLibraryMode();
    }
    await loadLibrary();
  } catch (error) {
    toast(error.message);
  }
}

/* ─────────────────────────────── Аккаунты ────────────────────────────── */

async function loadAccounts() {
  const holder = $('accountList');
  beginLoad(holder, 'plain', 2);
  try {
    const data = await api('/api/accounts');
    endLoad(holder);
    state.accounts = data.accounts || [];
    state.pendingLogin = data.pending_login || null;
    state.botUrl = data.bot_url || '';
    state.features = data.features || state.features;

    const sub = data.subscription || {};
    renderPiggyBank(sub.piggy_bank_days, sub.days_left);
    const badge = $('subBadge');
    badge.textContent = sub.active ? `активна · ${sub.days_left} дн.` : 'нет подписки';
    badge.classList.toggle('is-active', Boolean(sub.active));

    // шапка показывает те же дни, что и карточка подписки — держим их в согласии
    if (state.me) {
      state.me.subscription = {
        active: Boolean(sub.active),
        until: sub.until || null,
        days_left: sub.days_left || 0,
      };
      renderHeader();
    }

    const addBtn = $('addAccountBtn');
    if (addBtn) {
      addBtn.textContent = state.features.account_login_enabled
        ? '＋ Добавить аккаунт'
        : '⚙️ Нужен MTProto-вход';
    }

    renderAccountList();
    fillTaskAccounts();
  } catch (error) {
    failLoad(holder, error, 'loadAccounts');
  }
}

function renderAccountList() {
  const holder = $('accountList');
  if (!state.features.account_login_enabled) {
    holder.innerHTML = emptyHtml(
      '⚙️',
      'Подключение на настройке',
      'Кабинет, меню, подписки и платежи работают. Вход аккаунтов по телефону включится после подключения MTProto-шлюза сервиса.'
    );
    return;
  }

  const pending = state.pendingLogin;
  const pendingHtml = pending && pending.exists
    ? `<button class="card card--add card--resume" data-action="resume-login">
         ▶️ Продолжить вход ${esc(pending.phone || '')} · ${pending.step === 'password' ? 'ждём пароль 2FA' : 'ждём код'}
       </button>`
    : '';

  if (!state.accounts.length) {
    holder.innerHTML = pendingHtml + emptyHtml('👤', 'Аккаунтов нет', 'Добавьте первый аккаунт — он будет читать источники.');
    return;
  }

  holder.innerHTML = pendingHtml + state.accounts.map(accountHtml).join('');
}

/* Карточка аккаунта. Причина, по которой он офлайн, стоит отдельной строкой и с
   кнопкой: раньше её дописывали к «ID 2» мелким серым текстом — она обрезалась
   на 390 px и ничего не предлагала сделать, хотя пересылка в это время стояла.

   Сервис и сам возвращается к упавшему аккаунту каждые несколько минут, но
   ждать, глядя на «офлайн», незачем — «Попробовать снова» просит сразу. А
   мёртвую сессию повтор не оживит: если Telegram выкинул аккаунт, единственный
   путь — вход по номеру заново, поэтому там и кнопка другая. */
function accountHtml(account) {
  const trouble = account.online
    ? ''
    : `<div class="account__trouble">
         ${account.last_error ? `<div class="account__reason">⚠️ ${esc(account.last_error)}</div>` : ''}
         <button class="btn btn--sm" data-id="${account.id}"
                 data-action="${account.needs_login ? 'relogin-account' : 'retry-account'}">
           ${account.needs_login ? 'Подключить заново' : 'Попробовать снова'}
         </button>
       </div>`;
  return `
      <div class="account">
        <div class="account__row">
          <div class="account__avatar">${esc((account.phone || '?').replace('+', '').slice(0, 1))}</div>
          <div class="account__body">
            <div class="account__phone">${esc(account.phone)}</div>
            <div class="account__id">ID ${account.id}</div>
          </div>
          <div class="account__state ${account.online ? 'account__state--on' : 'account__state--off'}">
            ${account.online ? '🟢 на связи' : '🔴 офлайн'}
          </div>
          <button class="account__del" data-action="delete-account" data-id="${account.id}"
                  aria-label="Отключить аккаунт ${esc(account.phone)}" title="Отключить аккаунт">🗑</button>
        </div>
        ${trouble}
      </div>`;
}

/* ────────────────── Шторка подключения аккаунта ──────────────────────── */

/* Вход проходит целиком в кабинете: номер → код из Telegram → облачный пароль.
   Раньше кнопка «Добавить аккаунт» умела только открыть чат с ботом, и человек
   уходил из мини-аппа на середине пути. Шаг помнит сервер (строка в БД), так
   что закрытая шторка, перезапуск бота и переход в бота ничего не теряют. */
const LOGIN_STEPS = {
  phone: {
    title: 'Подключение аккаунта',
    lead: 'Номер того аккаунта, который будет читать источники и пересылать посты.',
    label: 'Номер телефона',
    placeholder: '+79001234567',
    type: 'tel',
    inputmode: 'tel',
    submit: 'Получить код',
    note: 'Международный формат, например +79001234567.',
  },
  code: {
    title: 'Код из Telegram',
    lead: 'Код пришёл в чат «Telegram» в приложении, а не по SMS.',
    label: 'Код подтверждения',
    placeholder: '12345',
    type: 'text',
    inputmode: 'numeric',
    submit: 'Подтвердить код',
    note: 'Код вида 1 2 3 4 5 — это 12345, без пробелов.',
  },
  password: {
    title: 'Облачный пароль',
    lead: 'На аккаунте включена двухэтапная проверка. Сервис хранит только полученную сессию, сам пароль не сохраняется.',
    label: 'Пароль 2FA',
    placeholder: 'Облачный пароль Telegram',
    type: 'password',
    inputmode: 'text',
    submit: 'Войти',
    note: '',
  },
};
const LOGIN_ORDER = ['phone', 'code', 'password'];
const LOGIN_PATHS = { phone: 'start', code: 'code', password: 'password' };
const LOGIN_FIELDS = { phone: 'phone', code: 'code', password: 'password' };

function loginReset() {
  state.login = { stage: 'phone', phone: '', attemptsLeft: null };
}

function openLoginSheet(phone) {
  if (!state.features.account_login_enabled) {
    toast('Вход аккаунтов пока на настройке');
    return;
  }
  const pending = state.pendingLogin;
  if (pending && pending.exists) {
    state.login = {
      stage: pending.step || 'code',
      phone: pending.phone || '',
      attemptsLeft: pending.attempts_left != null ? pending.attempts_left : null,
    };
  } else {
    loginReset();
    // Повторный вход тем же номером: подставляем его, чтобы не набирать заново.
    // Скрытый номер (в демо он под точками) не подставляем — его не отправить.
    if (phone && /^\+?[\d\s()-]{10,}$/.test(phone)) state.login.phone = phone;
  }
  renderLoginStage();
  $('loginSheet').classList.add('is-open');
  // Фокус — после появления шторки: пока она скрыта, клавиатура не поднимется.
  setTimeout(() => $('loginInput').focus(), 220);
}

function renderLoginStage(message) {
  const { stage, phone, attemptsLeft } = state.login;
  const spec = LOGIN_STEPS[stage] || LOGIN_STEPS.phone;
  const lead = stage !== 'phone' && phone ? `Номер ${phone}. ${spec.lead}` : spec.lead;
  const notes = [spec.note];
  if (stage === 'code' && attemptsLeft != null) notes.push(`Осталось попыток: ${attemptsLeft}.`);
  if (DEMO) notes.push(`Демо: код ${DEMO_CODE}, пароль ${DEMO_PASSWORD}.`);

  $('loginTitle').textContent = spec.title;
  $('loginLead').textContent = lead;
  $('loginLabel').textContent = spec.label;
  $('loginSubmit').textContent = spec.submit;
  $('loginNote').textContent = notes.filter(Boolean).join(' ');
  $('loginError').textContent = message || '';
  $('loginRestart').hidden = stage === 'phone';

  const input = $('loginInput');
  input.type = spec.type;
  input.inputMode = spec.inputmode;
  input.placeholder = spec.placeholder;
  input.value = stage === 'phone' ? phone : '';

  const current = LOGIN_ORDER.indexOf(stage);
  document.querySelectorAll('#loginSteps .steps__item').forEach((item) => {
    const idx = LOGIN_ORDER.indexOf(item.dataset.step);
    item.classList.toggle('is-done', idx < current);
    item.classList.toggle('is-current', idx === current);
  });
}

async function submitLogin() {
  const stage = state.login.stage;
  const value = ($('loginInput').value || '').trim();
  if (!value) {
    renderLoginStage(stage === 'phone' ? 'Введите номер телефона.' : 'Поле пустое.');
    return;
  }
  if (stage === 'phone') state.login.phone = value;

  try {
    const step = await withLoading($('loginSubmit'), () =>
      api(`/api/accounts/login/${LOGIN_PATHS[stage]}`, {
        method: 'POST',
        body: JSON.stringify({ [LOGIN_FIELDS[stage]]: value }),
      })
    );
    await applyLoginStep(step);
  } catch (error) {
    await loginFailed(error);
  }
}

/* Ответ сервера — это и есть следующий вопрос: code → password → done. */
async function applyLoginStep(step) {
  if (!step || !step.stage) return;
  if (step.stage === 'done') {
    closeSheets();
    loginReset();
    toast(`Аккаунт ${step.phone || ''} подключён`.replace('  ', ' '));
    await loadAccounts();
    return;
  }
  const wasStage = state.login.stage;
  state.login = {
    stage: step.stage,
    phone: step.phone || state.login.phone,
    attemptsLeft: step.attempts_left != null ? step.attempts_left : null,
  };
  renderLoginStage();
  if (step.stage === 'code' && wasStage === 'phone') toast('Код отправлен в Telegram');
  $('loginInput').focus();
}

/* 400 — ввод не подошёл, шаг тот же: опечатка в цифре кода не должна выбрасывать
   человека в начало (код-то ещё живой). 503 — шлюза нет, входить некуда.
   Остальное (409: код устарел, попытки кончились, сессия побилась) — вход
   закончился, возвращаемся к номеру и сверяем состояние с сервером. */
async function loginFailed(error) {
  if (error.status === 400) {
    // Остаток попыток берём из ответа: иначе подсказка под полем осталась бы с
    // прежним числом и спорила бы с текстом ошибки.
    const left = error.data ? error.data.attempts_left : null;
    if (left != null) state.login.attemptsLeft = left;
    renderLoginStage(error.message);
    $('loginInput').focus();
    $('loginInput').select();
    return;
  }
  if (error.status === 503) {
    closeSheets();
    toast(error.message);
    await loadAccounts();
    return;
  }
  const step = error.data && error.data.stage ? error.data : null;
  if (step) {
    // Сервер знает, где человек теперь стоит: код на номер уже ушёл — значит,
    // ждём код, а номер набирать заново незачем.
    state.login = {
      stage: step.stage,
      phone: step.phone || state.login.phone,
      attemptsLeft: step.attempts_left != null ? step.attempts_left : state.login.attemptsLeft,
    };
    renderLoginStage(error.message);
    $('loginInput').focus();
    await loadAccounts();
    return;
  }
  loginReset();
  renderLoginStage(error.message);
  await loadAccounts();
}

/* «Другой номер» — забыть незавершённый вход и начать с первого шага. */
async function restartLogin() {
  try {
    await withLoading($('loginRestart'), () =>
      api('/api/accounts/login/cancel', { method: 'POST' })
    );
  } catch (error) {
    toast(error.message);
  }
  loginReset();
  state.pendingLogin = null;
  renderLoginStage();
  $('loginInput').focus();
  await loadAccounts();
}

/* «Попробовать снова» под офлайн-аккаунтом: одна осечка не приговор, но и
   молчать в ответ на нажатие нельзя — показываем, чем кончилась попытка. */
async function retryAccount(id, button) {
  const account = state.accounts.find((item) => item.id === Number(id));
  const phone = account ? account.phone : `ID ${id}`;
  try {
    const data = await withLoading(button, () =>
      api(`/api/accounts/${id}/retry`, { method: 'POST' })
    );
    toast(
      data.online
        ? `Аккаунт ${data.phone || phone} на связи`
        : `Пока не выходит на связь: ${data.error || 'причина неизвестна'}`
    );
    await loadAccounts();
    // Задачи на этом аккаунте показывали «нет связи» — теперь метка другая.
    if (data.online) await refreshAllTaskLists();
  } catch (error) {
    toast(error.message);
  }
}

/* Отключение аккаунта необратимо: сессия стирается, задачи на нём встают.
   Поэтому спрашиваем подтверждение — нативным окном Telegram, если оно есть. */
async function deleteAccount(id, button) {
  const account = state.accounts.find((item) => item.id === Number(id));
  const phone = account ? account.phone : `ID ${id}`;
  const agreed = await confirmAction(
    `Отключить аккаунт ${phone}? Сохранённая сессия будет удалена, задачи на этом аккаунте остановятся.`
  );
  if (!agreed) return;
  try {
    const data = await withLoading(button, () => api(`/api/accounts/${id}`, { method: 'DELETE' }));
    toast(`Аккаунт ${data.phone || phone} отключён`);
    await loadAccounts();
    await refreshAllTaskLists();
  } catch (error) {
    toast(error.message);
  }
}

function confirmAction(question) {
  if (tg && tg.showConfirm) {
    return new Promise((resolve) => tg.showConfirm(question, (ok) => resolve(Boolean(ok))));
  }
  return Promise.resolve(window.confirm(question));
}

/* ─────────────────────────── Шторка задачи ───────────────────────────── */

function fillTaskAccounts() {
  const select = $('taskAccount');
  if (!select) return;
  if (!state.features.account_login_enabled) {
    select.innerHTML = '<option value="">Вход аккаунтов на настройке</option>';
    return;
  }
  if (!state.accounts.length) {
    select.innerHTML = '<option value="">Нет аккаунтов</option>';
    return;
  }
  select.innerHTML = state.accounts
    .map((account) => `<option value="${account.id}">${esc(account.phone)}</option>`)
    .join('');
  const firstActive = state.accounts.find((account) => account.online);
  if (firstActive) select.value = String(firstActive.id);
}

/* Разметка одного поля шторки по ключу из needs/optional команды. */
function fieldHtml(key) {
  const spec = FIELD_SPEC[key];
  if (!spec) return '';

  if (spec.control === 'select') {
    return `<label class="field"><span>${spec.label}</span><select id="taskAccount"></select></label>`;
  }
  if (spec.control === 'mode') {
    return `<div class="field"><span>${spec.label}</span>
      <div class="segmented segmented--sm" id="taskMode">
        <button type="button" class="seg is-active" data-mode="copy">Копия (без метки)</button>
        <button type="button" class="seg" data-mode="forward">Форвард</button>
      </div></div>`;
  }
  if (spec.control === 'textarea') {
    // Рассылка и постинг берут тексты из библиотеки, поэтому у их поля есть
    // кнопка выбора: перепечатывать сохранённое не нужно, а правка записи
    // доходит до обеих задач сразу. Другим командам библиотека не положена —
    // они читают только это поле.
    const fromLibrary = key === 'message'
      && state.activeCommand
      && OWN_TEXT_KINDS.includes(state.activeCommand.kind);
    // Под «Сообщением» — счёт: сколько сообщений уйдёт и с чего начинается
    // каждое. Правило «сообщения делит пустая строка» на глаз не проверить, а
    // ошибка дорогая: набранный прайс уходил десятком отдельных отправок.
    const counter = key === 'message'
      ? `<div class="field__count" id="count_${key}" hidden></div>` : '';
    return `<label class="field"><span>${spec.label}</span>
      <textarea id="task_${key}" rows="4" placeholder="${esc(spec.placeholder || '')}"></textarea>
      ${counter}
      ${spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : ''}
      ${fromLibrary ? `<div class="field__aside">
        <button type="button" class="btn btn--pick" data-pick-library="1">📚 из библиотеки</button>
      </div>
      <div class="picks" id="libraryPicks"></div>` : ''}
    </label>`;
  }
  if (spec.control === 'check') {
    return `<label class="field field--check">
      <input type="checkbox" id="task_${key}">
      <span>${spec.label}</span>
      ${spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : ''}
    </label>`;
  }
  const type = spec.control === 'number' ? 'number' : 'text';
  const input = `<input type="${type}" id="task_${key}"
      placeholder="${esc(spec.placeholder || '')}" autocomplete="off">`;
  const note = spec.note ? `<i class="field__note">${esc(spec.note)}</i>` : '';
  // Поле с выбором чата: рядом с ним кнопка, которая открывает список чатов
  // аккаунта. Раньше единственным способом было вписать @username или id.
  if (spec.pick) {
    // Чатов в поле может быть двести, и строка через запятую в одну строку
    // ввода не читается — под полем показываем счёт, первые имена и «очистить».
    const counter = spec.pick === 'many'
      ? `<div class="field__count" id="count_${key}" hidden></div>` : '';
    return `<label class="field"><span>${spec.label}</span>
      <div class="field__row">
        ${input}
        <button type="button" class="btn btn--pick" data-pick="${key}"
                data-multi="${spec.pick === 'many' ? '1' : ''}">💬 выбрать</button>
      </div>
      ${counter}
      ${note}
    </label>`;
  }
  return `<label class="field"><span>${spec.label}</span>
    ${input}
    ${note}
  </label>`;
}

function fieldValue(key) {
  const spec = FIELD_SPEC[key];
  if (!spec) return '';
  if (spec.control === 'select') {
    const select = $('taskAccount');
    return select ? select.value.trim() : '';
  }
  if (spec.control === 'mode') return state.mode;
  const node = $(`task_${key}`);
  if (spec.control === 'check') return node ? node.checked : false;
  return node && node.value ? String(node.value).trim() : '';
}

function splitList(value) {
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

/* Текст из поля «Сообщение» → сообщения. Делит их пустая строка — так же, как
   на сервере (`_split_messages`). По одному переносу строки резать нельзя:
   прайс или объявление в два абзаца — это одно сообщение, а не десять. */
function splitMessages(value) {
  return String(value || '')
    .replace(/\r\n?/g, '\n')
    .split(/\n[ \t]*\n+/)
    .map((block) => block.trim())
    .filter(Boolean);
}

/* Имя сообщения в списке библиотеки — его первая строка (так же считает
   сервер). Многострочный заголовок в одну строку списка не влезает. */
function messageTitle(text, limit = 48) {
  const head = String(text || '').split('\n').map((line) => line.trim()).find(Boolean) || '';
  return head.length > limit ? `${head.slice(0, limit)}…` : head;
}

/* «1 сообщение» / «2 сообщения» / «5 сообщений». */
function messageWord(n) {
  return pluralRu(n, 'сообщение', 'сообщения', 'сообщений');
}

/* «1 чат» / «2 чата» / «5 чатов»: счёт читают глазами, и «5 чат» выглядит
   недоделкой. */
function chatWord(n) {
  return pluralRu(n, 'чат', 'чата', 'чатов');
}

/* Итог под полем-списком: сколько чатов набрано, первые имена и «очистить».
   Двести ссылок через запятую видны в поле одной обрезанной строкой — по ней
   не понять ни счёт, ни что там вообще лежит. */
function renderFieldCount(key) {
  const holder = $(`count_${key}`);
  if (!holder) return;
  if (key === 'message') {
    renderMessageCount(holder);
    return;
  }
  const refs = splitList(fieldValue(key));
  holder.hidden = !refs.length;
  if (!refs.length) {
    holder.innerHTML = '';
    return;
  }
  // Имя вместо ссылки, если оно известно: у готовой задачи в поле стоят числовые
  // id, и строка «-1001234567890, -1009876543210» человеку ничего не говорит.
  const head = refs.slice(0, 2).map(chatRefName).join(', ');
  holder.innerHTML = `<b>${refs.length} ${chatWord(refs.length)}</b>
    <span>${esc(refs.length > 2 ? `${head} и ещё ${refs.length - 2}` : head)}</span>
    <button type="button" data-count-clear="${key}">очистить</button>`;
}

/* Ссылка на чат → как её назвать человеку. Названия кабинет запоминает там, где
   уже видел их с именами: в списке чатов аккаунта и в самой задаче (edit.names).
   Неизвестную ссылку показываем как есть — это то, что человек вписал сам. */
function chatRefName(ref) {
  return state.chatNames[ref] || ref;
}

/* Названия чатов из списка — в память кабинета. Список приходит с именами, и
   второй раз спрашивать сервер ради подписи под полем незачем. */
function rememberChatNames(chats) {
  (chats || []).forEach((chat) => {
    const title = chatTitle(chat);
    if (!title) return;
    const ref = chatToRef(chat);
    if (ref) state.chatNames[ref] = title;
    if (chat.id) state.chatNames[String(chat.id)] = title;
  });
}

/* Итог под «Сообщением»: сколько сообщений уйдёт и с чего начинается каждое.
   Кнопки «очистить» здесь нет намеренно: набранный текст стереть одним
   промахом — потеря, а ссылки в поле чатов набираются мышкой заново. */
function renderMessageCount(holder) {
  const blocks = splitMessages(fieldValue('message'));
  holder.hidden = !blocks.length;
  if (!blocks.length) {
    holder.innerHTML = '';
    return;
  }
  const heads = blocks.slice(0, 2).map((block) => messageTitle(block, 24));
  const tail = blocks.length > 2 ? ` и ещё ${blocks.length - 2}` : '';
  const single = blocks.length === 1 && blocks[0].includes('\n')
    ? ' · переносы строк сохранятся' : '';
  holder.innerHTML = `<b>${blocks.length} ${messageWord(blocks.length)}</b>
    <span>${esc(blocks.length === 1 ? heads[0] + single : heads.join(' · ') + tail)}</span>`;
}

/* Все итоги формы разом: поля пересобираются под каждую команду, поэтому
   ключи берём из разметки, а не из списка полей команды. */
function renderFieldCounts() {
  document.querySelectorAll('#taskFields .field__count').forEach((node) => {
    renderFieldCount(node.id.replace(/^count_/, ''));
  });
}

function openTaskSheet(command, prefill, task) {
  if (!state.features.account_login_enabled) {
    toast('Вход аккаунтов пока на настройке');
    switchTab('accounts');
    return;
  }
  if (!state.accounts.length) {
    toast('Сначала подключите аккаунт');
    switchTab('accounts');
    openLoginSheet();
    return;
  }

  state.activeCommand = command
    || state.commands.find((item) => item.id === 'copy_channel')
    || { id: 'copy_channel', kind: 'forward', title: 'Копирование канала', emoji: '🔁', needs: ['account', 'source', 'target'], optional: ['mode'] };
  // Правка идёт той же шторкой: у формы создания и формы настройки один набор
  // полей, и второй такой набор разошёлся бы с первым на первой же настройке.
  state.editTask = task && task.edit ? task : null;
  state.mode = 'copy';
  // Выбор из библиотеки живёт ровно одну форму: чужой выбор в новой задаче
  // молча отправил бы не те сообщения.
  state.libraryPick = [];

  const editing = Boolean(state.editTask);
  $('taskSheetTitle').textContent = editing
    ? '⚙️ Настройка задачи'
    : `${state.activeCommand.emoji || ''} ${state.activeCommand.title}`.trim();
  $('taskSheet').setAttribute('aria-label', editing ? 'Настройка задачи' : 'Новая задача');
  $('taskSheetLead').textContent = editing
    ? `«${task.title}» · ${state.activeCommand.title}. Меняется только то, что поправите: ` +
      'счётчики, номер задачи и место в круге рассылки останутся на месте.'
    : state.activeCommand.description || '';
  $('taskFields').innerHTML = [
    ...state.activeCommand.needs,
    ...(state.activeCommand.optional || []),
  ].map(fieldHtml).join('');
  $('taskHint').textContent = editing
    ? 'Тип задачи и аккаунт не меняются — это была бы уже другая задача. Новый чат в ' +
      'списке кабинет найдёт через аккаунт, для прежних чатов связь не нужна.' +
      windowMigrationHint(task)
    : state.activeCommand.hint
      || 'Аккаунт должен быть подписан на источник и иметь право писать в приёмник.';
  $('taskSubmit').textContent = editing ? '💾 Сохранить' : 'Запустить задачу';
  $('taskError').textContent = '';

  fillTaskAccounts();
  bindSheetFields();
  applyTaskPrefill(prefill || {});
  $('taskSheet').classList.add('is-open');
}

/* Задача, по которой нажали кнопку. Ищем во всех трёх списках: карточка одна и
   та же на «Главной», в активных и на паузе, а лежат они по разным спискам. */
function findTask(id) {
  const lists = state.tasksByStatus;
  return [...(lists.active || []), ...(lists.paused || []), ...(lists.done || [])]
    .find((task) => task.id === Number(id)) || null;
}

/* «⚙️ Настроить» — открыть задачу в форме с её же значениями.
   Раньше поменять интервал или текст можно было только пересозданием задачи:
   вместе с ней терялись счётчики, номер и место в круге рассылки. */
function openTaskEdit(id) {
  const task = findTask(id);
  if (!task || !task.edit) {
    toast('Задача не найдена');
    return;
  }
  if (task.archived) {
    toast('Сначала верните задачу из архива');
    return;
  }
  const command = state.commands.find((item) => item.kind === task.kind)
    || state.commands.find((item) => item.id === 'copy_channel');
  if (!command) {
    toast('Каталог команд ещё не загружен');
    return;
  }
  openTaskSheet(command, task.edit, task);
}

/* Заполняем поля формы значениями задачи или выбранных чатов. Применяется после
   построения разметки полей (DOM уже существует). Имена полей здесь те же, что
   в теле запроса: форма правки получает от сервера ровно то, что отправит
   обратно, — поэтому и разбор один. */
function applyTaskPrefill(prefill) {
  const setValue = (key, value) => {
    if (value == null || value === '') return;
    const node = $(`task_${key}`);
    if (node && node.value !== undefined) node.value = value;
  };
  // Названия чатов задачи приходят вместе с ней: в поле лежат ссылки (у готовой
  // задачи — числовые id), а под полем человек должен видеть имена.
  if (prefill.names) Object.assign(state.chatNames, prefill.names);
  setValue('target', prefill.target);
  if (Array.isArray(prefill.targets)) {
    setValue('targets', prefill.targets.join(', '));
  } else if (prefill.targets) {
    setValue('targets', prefill.targets);
  }
  setValue('source', prefill.source);
  setValue('target_user', prefill.target_user);
  // Настройки задачи — одним проходом: ключ формы и ключ задачи совпадают, а
  // лишние для этой команды поля просто не находятся в разметке.
  ['keywords', 'reaction', 'limit', 'message', 'interval', 'start', 'end', 'gap', 'cycle', 'repeats']
    .forEach((key) => setValue(key, prefill[key]));
  ['typing', 'random_pick'].forEach((key) => {
    const node = $(`task_${key}`);
    if (node) node.checked = Boolean(prefill[key]);
  });
  if (prefill.mode === 'copy' || prefill.mode === 'forward') {
    state.mode = prefill.mode;
    document.querySelectorAll('#taskMode .seg').forEach((seg) => {
      seg.classList.toggle('is-active', seg.dataset.mode === prefill.mode);
    });
  }
  // Аккаунт задачи не меняется: другой аккаунт — это другие чаты и другая
  // задача. Показываем его и запираем, чтобы это было видно, а не угадывалось.
  const account = $('taskAccount');
  if (account && state.editTask) {
    if (prefill.account_id) account.value = String(prefill.account_id);
    account.disabled = true;
  } else if (account) {
    account.disabled = false;
  }
  if (Array.isArray(prefill.library_ids) && prefill.library_ids.length) {
    state.libraryPick = prefill.library_ids.map(Number).filter(Boolean);
    renderLibraryPicks();
    // Имена сохранённых сообщений лежат в библиотеке: без неё чипсы показывали
    // бы «сообщение #7» — по номеру человек свой текст не узнаёт.
    if (!state.library.length) loadLibrary().then(renderLibraryPicks).catch(() => {});
  }
  renderFieldCounts();
}

function bindSheetFields() {
  document.querySelectorAll('#taskMode .seg').forEach((seg) => {
    seg.addEventListener('click', () => {
      state.mode = seg.dataset.mode;
      document.querySelectorAll('#taskMode .seg').forEach((item) => {
        item.classList.toggle('is-active', item === seg);
      });
    });
  });
}

/* ─────────────── Выбор мышкой: чаты и сообщения для поля ─────────────── */

/* Кнопка «💬 выбрать» у полей источника, приёмника и получателей и кнопка
   «📚 из библиотеки» у поля сообщения. До этого чат в форме можно было только
   вписать руками — @username или числовой id, — и задача падала на любой
   опечатке. Шторка одна на оба случая: список с отметками и поиск у них
   одинаковые, а два почти одинаковых экрана расходятся при первой же правке.
   Чаты берём те же, что на вкладке «Чаты»: /api/chats того аккаунта, который
   выбран в самой форме. */

/* Аккаунт для списка чатов: тот, что стоит в форме. Селект может быть ещё
   пустым (аккаунт один и не онлайн) — тогда берём первый известный. */
function pickerAccount() {
  const select = $('taskAccount');
  const id = select ? Number(select.value) : 0;
  return state.accounts.find((item) => item.id === id) || state.accounts[0] || null;
}

function openFieldPicker(key, multi) {
  if (!pickerAccount()) {
    toast('Сначала подключите аккаунт');
    return;
  }
  // Уже вписанное руками не теряем: разбираем поле на ссылки и отмечаем их.
  const current = splitList(fieldValue(key));
  state.picker = {
    mode: 'chats',
    key,
    multi: Boolean(multi),
    chosen: multi ? current : current.slice(0, 1),
    chats: [],
  };
  $('pickerTitle').textContent = multi ? 'Выбор чатов' : 'Выбор чата';
  $('pickerSearch').value = '';
  renderPickerFooter();
  $('pickerSheet').classList.add('is-open');
  loadPickerChats();
}

/* Сообщения для рассылки. Текст отмеченной записи встаёт прямо в поле — его
   видно и можно поправить, — а готовый пост уходит в задачу ссылкой: своего
   текста у него нет. */
function openLibraryPicker() {
  state.picker = {
    mode: 'library',
    key: 'message',
    multi: true,
    chosen: state.libraryPick.map(String),
    chats: [],
  };
  $('pickerTitle').textContent = 'Сообщения из библиотеки';
  $('pickerSearch').value = '';
  renderPickerFooter();
  $('pickerSheet').classList.add('is-open');
  loadPickerLibrary();
}

/* Подсказка и кнопка внизу шторки. Один чат встаёт в поле сразу по нажатию,
   поэтому кнопка «Готово» там лишняя — прячем, чтобы не искать её глазами. */
function renderPickerFooter() {
  const { mode, multi, chosen, key } = state.picker;
  const label = (FIELD_SPEC[key] || {}).label || 'Поле';
  const apply = $('pickerApply');
  if (mode === 'library') {
    $('pickerLead').textContent =
      `Отметьте сообщения — текст встанет в поле, готовый пост уйдёт ссылкой. Отмечено: ${chosen.length}.`;
  } else {
    $('pickerLead').textContent = multi
      ? `${label}: отмечайте — уйдут в поле через запятую. Чатов можно сколько угодно.`
      : `${label}: нажмите чат — он встанет в поле, шторка закроется.`;
  }
  apply.hidden = !multi;
  apply.textContent = chosen.length ? `Готово · ${chosen.length}` : 'Готово';
  // «Выбрать все» нужно только там, где чатов может быть много: в библиотеке
  // сообщений и при выборе одного чата эта строка ничего не значит.
  const bulk = $('pickerBulk');
  bulk.hidden = !(multi && mode === 'chats');
  $('pickerCount').innerHTML = chosen.length
    ? `отмечено <b>${chosen.length}</b>`
    : 'ничего не отмечено';
}

/* Отметить или снять всё, что видно на экране. Именно видно: поиск сужает
   список, и «выбрать все» после запроса «реклама» должно брать рекламные чаты,
   а не заодно и остальные двести. Отметки за пределами текущего списка
   не трогаем — человек набирал их раньше и не просил стирать. */
function pickerSelectVisible(select) {
  const picker = state.picker;
  if (!picker.multi || picker.mode !== 'chats') return;
  const refs = picker.chats.map(chatToRef).filter(Boolean);
  if (!refs.length) return;
  if (select) {
    refs.forEach((ref) => {
      if (!picker.chosen.includes(ref)) picker.chosen.push(ref);
    });
  } else {
    picker.chosen = picker.chosen.filter((ref) => !refs.includes(ref));
  }
  // Отметки расставляем одним проходом по списку: markPickerRow на каждый чат —
  // это поиск по всему списку, и на трёхсотом чате задержка уже видна.
  const chosen = new Set(picker.chosen);
  $('pickerList').querySelectorAll('[data-pick-ref]').forEach((row) => {
    const on = chosen.has(row.dataset.pickRef);
    row.classList.toggle('is-selected', on);
    const mark = row.querySelector('.chat__check');
    if (mark) mark.textContent = on ? '✓' : '';
  });
  renderPickerFooter();
}

/* Поиск в шторке: чаты ищет сервер, библиотеку фильтруем на месте — она
   маленькая и уже загружена целиком. */
function pickerReload() {
  if (state.picker.mode === 'library') renderPickerList();
  else loadPickerChats();
}

async function loadPickerChats() {
  const holder = $('pickerList');
  const account = pickerAccount();
  if (!account) {
    holder.innerHTML = emptyHtml('👤', 'Нет аккаунта', 'Подключите аккаунт во вкладке «Аккаунты».');
    return;
  }
  const query = encodeURIComponent($('pickerSearch').value || '');
  beginLoad(holder, 'plain', 4);
  try {
    const data = await api(`/api/chats?account_id=${account.id}&q=${query}`);
    endLoad(holder);
    state.picker.chats = data.chats || [];
    rememberChatNames(state.picker.chats);
    if (!data.online) {
      holder.innerHTML = emptyHtml('📴', 'Аккаунт не в сети', 'Перезапустите аккаунт в боте — список чатов читает он.');
      return;
    }
    renderPickerList();
  } catch (error) {
    failLoad(holder, error, 'loadPickerChats');
  }
}

async function loadPickerLibrary() {
  const holder = $('pickerList');
  beginLoad(holder, 'plain', 3);
  try {
    const data = await api('/api/library');
    endLoad(holder);
    state.library = data.items || [];
    // Отметки ставим по тому, что уже уйдёт: тексты видно в поле, посты — в чипсах.
    if (state.picker.mode === 'library') syncLibraryChosen();
    renderPickerList();
  } catch (error) {
    failLoad(holder, error, 'loadPickerLibrary');
  }
}

function renderPickerList() {
  if (state.picker.mode === 'library') {
    renderPickerLibrary();
    return;
  }
  const holder = $('pickerList');
  const chats = state.picker.chats;
  if (!chats.length) {
    holder.innerHTML = emptyHtml('💬', 'Ничего не найдено', 'Измените запрос — или впишите @username прямо в поле.');
    return;
  }
  holder.innerHTML = chats.map((chat) => {
    const ref = chatToRef(chat);
    const kind = chatKind(chat);
    const on = state.picker.chosen.includes(ref);
    return `
      <button type="button" class="chat${on ? ' is-selected' : ''}" data-pick-ref="${esc(ref)}">
        <div class="chat__emoji">${kind.emoji}</div>
        <div class="chat__body">
          <div class="chat__title">${esc(chatTitle(chat))}</div>
          <div class="chat__sub"><code>${esc(ref)}</code></div>
        </div>
        <span class="chat__kind">${kind.label}</span>
        ${pickerMarkHtml(on)}
      </button>`;
  }).join('');
}

/* Отметка справа. Когда чат нужен один, пустая рамка врёт: она обещает выбор
   пачкой, а нажатие сразу закрывает шторку — поэтому там шеврон «внутрь». */
function pickerMarkHtml(on) {
  if (!state.picker.multi) return '<span class="chat__go" aria-hidden="true">›</span>';
  return `<span class="chat__check" aria-hidden="true">${on ? '✓' : ''}</span>`;
}

function renderPickerLibrary() {
  const holder = $('pickerList');
  const query = String($('pickerSearch').value || '').trim().toLowerCase();
  const items = state.library.filter((item) =>
    !query || `${item.title || ''} ${item.text || ''}`.toLowerCase().includes(query)
  );
  if (!items.length) {
    holder.innerHTML = state.library.length
      ? emptyHtml('📚', 'Ничего не найдено', 'Измените запрос.')
      : emptyHtml('📚', 'Библиотека пуста', 'Наберите текст в поле «Сообщение» — он уйдёт в рассылку и сохранится сам.');
    return;
  }
  holder.innerHTML = items.map((item) => {
    const on = state.picker.chosen.includes(String(item.id));
    const { head, sub } = libraryLines(item, 60);
    return `
      <button type="button" class="chat${on ? ' is-selected' : ''}" data-pick-ref="${item.id}">
        <div class="chat__emoji">📄</div>
        <div class="chat__body">
          <div class="chat__title">${esc(head)}</div>
          ${sub ? `<div class="chat__sub">${esc(sub)}</div>` : ''}
        </div>
        ${pickerMarkHtml(on)}
      </button>`;
  }).join('');
}

function togglePickerRef(ref) {
  if (!ref) return;
  const picker = state.picker;
  if (!picker.multi) {
    picker.chosen = [ref];
    applyPicker();
    return;
  }
  const idx = picker.chosen.indexOf(ref);
  if (idx >= 0) picker.chosen.splice(idx, 1);
  else picker.chosen.push(ref);
  markPickerRow(ref, idx < 0);
  renderPickerFooter();
}

/* Отметку рисуем на месте, а не перерисовкой всего списка: перерисовка сбивает
   прокрутку в начало, и на длинном списке чатов каждая отметка отбрасывала
   человека к первому чату. */
function markPickerRow(ref, on) {
  const row = [...$('pickerList').querySelectorAll('[data-pick-ref]')]
    .find((node) => node.dataset.pickRef === String(ref));
  if (!row) {
    renderPickerList();
    return;
  }
  row.classList.toggle('is-selected', on);
  const mark = row.querySelector('.chat__check');
  if (mark) mark.textContent = on ? '✓' : '';
}

function applyPicker() {
  const { mode, key, chosen } = state.picker;
  if (mode === 'library') {
    applyLibraryPick(chosen.map(Number).filter(Boolean));
    closePicker();
    return;
  }
  const node = key ? $(`task_${key}`) : null;
  if (node) node.value = chosen.join(', ');
  renderFieldCount(key);
  closePicker();
}

/* Отмеченное в библиотеке: текст встаёт прямо в поле, а сохранённые посты —
   чипсами рядом. Раньше и то и другое уходило чипсами, поле оставалось пустым, и
   что именно отправится, было видно только по заголовкам записей — а набранный в
   поле текст молча пропадал, потому что выбор считался важнее.

   Набранное руками не трогаем: убираем из поля только те тексты, что сами и
   поставили, — то есть отметку с записи можно снять и она уйдёт из поля. */
function applyLibraryPick(ids) {
  const items = ids
    .map((id) => state.library.find((row) => Number(row.id) === id))
    .filter(Boolean);
  const texts = items.map(libraryText).filter(Boolean);
  state.libraryPick = items.filter((item) => !libraryText(item)).map((item) => Number(item.id));
  const node = $('task_message');
  if (node) {
    const known = new Set(state.library.map(libraryText).filter(Boolean));
    const parts = splitMessages(node.value).filter(
      (part) => !known.has(part) || texts.includes(part)
    );
    texts.forEach((text) => {
      if (!parts.includes(text)) parts.push(text);
    });
    node.value = parts.join('\n\n');
  }
  renderLibraryPicks();
  renderFieldCount('message');
}

/* Текст записи библиотеки — тем же правилом, каким его сравнивает сервер. У
   сохранённого поста своего текста нет: он лежит ссылкой на чат и сообщение. */
function libraryText(item) {
  return String((item && item.text) || '').trim();
}

/* Отметки в шторке — то, что уже уйдёт: посты стоят чипсами, тексты — в поле.
   Без этого повторный заход в шторку показывал пустой список отметок, и снять
   лишнее сообщение можно было только правкой поля. */
function syncLibraryChosen() {
  const inField = splitMessages(fieldValue('message'));
  const chosen = new Set(state.libraryPick.map(String));
  state.library.forEach((item) => {
    if (inField.includes(libraryText(item))) chosen.add(String(item.id));
  });
  state.picker.chosen = [...chosen];
  renderPickerFooter();
}

/* Отмеченные сообщения рядом с полем: видно, что уйдёт, и можно снять по одному
   не открывая шторку заново. */
function renderLibraryPicks() {
  const holder = $('libraryPicks');
  if (!holder) return;
  if (!state.libraryPick.length) {
    holder.innerHTML = '';
    return;
  }
  const chips = state.libraryPick.map((id) => {
    const item = state.library.find((row) => Number(row.id) === Number(id));
    const title = item ? (item.title || libraryPreview(item, 40)) : `сообщение #${id}`;
    return `<span class="pick"><span class="pick__t">${esc(title)}</span><button type="button" class="pick__x"
      data-library-drop="${id}" aria-label="Убрать сообщение">✕</button></span>`;
  }).join('');
  holder.innerHTML =
    '<i class="field__note">готовые посты — уйдут вместе с текстом из поля</i>' + chips;
}

/* Своя кнопка закрытия: общий обработчик [data-close] гасит все шторки сразу
   и увёл бы вместе с выбором саму форму задачи. */
function closePicker() {
  $('pickerSheet').classList.remove('is-open');
  state.picker = { mode: 'chats', key: null, multi: false, chosen: [], chats: [] };
}

function closeSheets() {
  const loginWasOpen = $('loginSheet').classList.contains('is-open');
  document.querySelectorAll('.sheet').forEach((sheet) => sheet.classList.remove('is-open'));
  state.activeCommand = null;
  state.editTask = null;
  state.picker = { mode: 'chats', key: null, multi: false, chosen: [], chats: [] };
  // Закрыли шторку на середине входа — в списке должна появиться карточка
  // «Продолжить вход»: шаг никуда не делся, он лежит в БД на сервере.
  if (loginWasOpen && state.login.stage !== 'phone') loadAccounts();
}

/* Значения полей → тело запроса. Одно и то же тело идёт и в POST /api/tasks, и в
   PATCH /api/tasks/{id}: на сервере правку разбирают те же правила, что и
   создание, поэтому второй сборки полей здесь быть не должно.

   Разница одна. Форма правки — это полный снимок задачи, поэтому пустое
   необязательное поле уходит как есть: человек стёр слова-ловушки, значит их
   надо убрать. У новой задачи пустое поле в тело не попадает — там за него
   отвечают умолчания сервера. */
function collectTaskPayload() {
  const command = state.activeCommand;
  const editing = Boolean(state.editTask);
  const values = {};
  [...command.needs, ...(command.optional || [])].forEach((key) => {
    values[key] = fieldValue(key);
  });

  const missing = command.needs
    // Сообщение можно не набирать, если выбрано из библиотеки: рассылка и
    // постинг возьмут тексты оттуда, и требовать копию того же текста в поле
    // незачем.
    .filter((key) => !values[key] && !(key === 'message' && state.libraryPick.length))
    .map((key) => (FIELD_SPEC[key] ? FIELD_SPEC[key].label.toLowerCase() : key));
  if (missing.length) return { error: 'Заполните: ' + missing.join(', ') };

  const body = { command: command.id, account_id: Number(values.account) || 0 };
  const text = (key, value) => {
    if (value === undefined) return;          // поля нет у этой команды
    if (value || editing) body[key] = value;
  };
  const number = (key, value) => {
    // Пустое число — «оставь как было», а не ноль: ноль минут и ноль участников
    // означали бы совсем другую задачу, чем та, которую не стали править.
    if (value === undefined || value === '') return;
    body[key] = Number(value) || 0;
  };
  const flag = (key, value) => {
    if (value === undefined) return;
    if (value || editing) body[key] = Boolean(value);
  };

  text('source', values.source);
  text('target', values.target);
  text('target_user', values.target_user);
  text('reaction', values.reaction);
  text('mode', values.mode);
  text('message', values.message);
  text('start', values.start);
  text('end', values.end);
  // Окно задаётся по часам того, кто его ставит, поэтому вместе с ЧЧ:ММ уходит
  // смещение этого устройства от UTC. Сервер стоит в UTC: без смещения окно
  // «10:00–20:00» у московского хозяина работало 13:00–23:00 по Москве.
  if (values.start !== undefined || values.end !== undefined) body.tz = browserTz();
  if (values.keywords !== undefined && (values.keywords || editing)) {
    body.keywords = splitList(values.keywords);
  }
  if (values.targets !== undefined && (values.targets || editing)) {
    body.targets = splitList(values.targets);
  }
  number('limit', values.limit);
  number('interval', values.interval);
  number('gap', values.gap);
  number('cycle', values.cycle);
  number('repeats', values.repeats);
  flag('typing', values.typing);
  flag('random_pick', values.random_pick);
  // Явный выбор из библиотеки важнее набранного текста — так же считает сервер.
  if (state.libraryPick.length) body.library_ids = state.libraryPick;
  else if (editing && OWN_TEXT_KINDS.includes(command.kind)) body.library_ids = [];
  return { body };
}

async function submitTask() {
  if (!state.activeCommand) return;

  const { error, body } = collectTaskPayload();
  if (error) {
    $('taskError').textContent = error;
    return;
  }

  const editing = state.editTask;
  const button = $('taskSubmit');
  try {
    const data = await withLoading(button, () => (editing
      ? api(`/api/tasks/${editing.id}`, { method: 'PATCH', body: JSON.stringify(body) })
      : api('/api/tasks', { method: 'POST', body: JSON.stringify(body) })));
    closeSheets();

    // Правка ничего не запускает и не переносит задачу между списками: она
    // остаётся там же, где была, — обновляем списки и остаёмся на месте.
    if (editing) {
      toast('Настройки сохранены');
      await refreshAllTaskLists();
      return;
    }

    const run = data.run;
    if (run) toast(runMessage(run));
    else toast('Задача запущена');

    // разовая задача отработала и ушла в архив — показываем архив, иначе активные
    state.taskStatus = data.task && data.task.archived ? 'done' : 'active';
    document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
      seg.classList.toggle('is-active', seg.dataset.status === state.taskStatus);
    });
    await refreshAllTaskLists();
    switchTab('tasks');
  } catch (requestError) {
    if (requestError.status === 402) {
      $('taskError').textContent = 'Лимит правил. Оформите абонемент.';
      setTimeout(() => openBot('subscribe'), 1200);
    } else if (requestError.status === 503) {
      $('taskError').textContent = requestError.message;
      setTimeout(() => openBot('add_account'), 1200);
    } else {
      $('taskError').textContent = requestError.message;
    }
  }
}

/* ─────────────────────── Копилка подписок ────────────────────────────── */

function renderPiggyBank(banked, daysLeft) {
  state.bankedDays = banked || 0;
  $('piggyDays').textContent = state.bankedDays;
  const distribute = $('distributeBtn');
  const freeze = $('freezeBtn');
  // Половина ряда — это ~158 px, подпись в две строки. Число от «дн.» отрывать
  // нельзя, поэтому между ними неразрывный пробел: перенос уйдёт до числа.
  if (distribute) {
    distribute.disabled = !state.bankedDays;
    distribute.textContent = state.bankedDays
      ? `Распределить ${state.bankedDays} дн.`
      : 'Копилка пуста';
  }
  if (freeze) {
    freeze.disabled = !daysLeft || daysLeft <= 1;
    freeze.textContent = daysLeft > 1 ? `❄️ Заморозить ${daysLeft - 1} дн.` : '❄️ Заморозить дни';
  }
}

/* ─────────────────────────────── Оплата Stars ─────────────────────────── */

/* Инвойс создаёт бот через Bot API, кабинет только открывает его:
   сумма и payload формируются на сервере, пользователь не покидает
   мини-апп. Зачисление приходит в хендлер successful_payment бота. */
async function payWithStars(button, months = 1) {
  // Вне Telegram (демо или обычный браузер) платёжный WebView не поднимется —
  // честно уводим в бота, а не делаем вид, что что-то произошло.
  if (DEMO || !tg || !tg.openInvoice) {
    openBot('subscribe');
    return;
  }

  try {
    const invoice = await withLoading(button, () =>
      api('/api/subscription/invoice', { method: 'POST', body: JSON.stringify({ months }) })
    );
    openStarsInvoice(invoice.url);
  } catch (error) {
    toast(error.message);
  }
}

function openStarsInvoice(url) {
  tg.openInvoice(url, (status) => {
    if (status === 'paid') {
      toast('Оплата прошла — абонемент активен.');
      loadAccounts();
    } else if (status === 'pending') {
      toast('Платёж обрабатывается, абонемент появится после подтверждения.');
    } else if (status === 'failed') {
      toast('Платёж не прошёл. Попробуйте ещё раз.');
    }
    // 'cancelled' — пользователь закрыл окно сам, молчим.
  });
}

/* На кнопке сразу видно цену: она приходит в /api/me вместе с тарифами. */
function renderTopUpButton() {
  const button = $('topUpBtn');
  if (!button) return;
  const stars = (state.me && state.me.tariffs && state.me.tariffs.stars) || 0;
  button.textContent = stars ? `⭐ Оплатить ${stars} звёзд` : '⭐ Оплатить звёздами';
}

/* ───────────────── Оплата вне Telegram: карта и крипта ───────────────── */

/* Внутри Telegram абонемент продаётся только за звёзды — так требуют правила
   Telegram (ToS для разработчиков, п. 6.2). Карта и USDT работают на странице
   сервиса, и кабинет открывает её через WebApp.openLink, то есть во внешнем
   браузере. Ничего не прячем: на кнопке прямо написано «на сайте». */
const WEB_PAY_LABELS = { yookassa: 'картой', usdt: 'криптой' };

function renderWebPayButton() {
  const button = $('webPayBtn');
  if (!button) return;
  const pay = (state.me && state.me.pay) || {};
  const methods = pay.external || [];
  button.hidden = !methods.length;
  if (!methods.length) return;
  const names = methods.map((method) => WEB_PAY_LABELS[method] || method).join(' или ');
  button.textContent = `🌐 Оплатить ${names} на сайте`;
}

async function payOnWeb(button) {
  if (DEMO) {
    toast('В демо страница оплаты не открывается — она работает из бота.');
    return;
  }
  try {
    // Ссылка подписана и живёт час, поэтому берём свежую в момент нажатия,
    // а не ту, что пришла с /api/me при открытии кабинета.
    const data = await withLoading(button, () => api('/api/pay/link'));
    openExternal(data.url);
  } catch (error) {
    toast(error.message);
  }
}

/* openLink — это внешний браузер, а не WebView Telegram: платёж проходит вне
   Telegram, как и задумано. Вне Telegram (обычный браузер) уходим в ту же
   ссылку обычным переходом: window.open после await блокируется. */
function openExternal(url) {
  if (!url) {
    toast('Оплата на сайте недоступна');
    return;
  }
  if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
  else location.href = url;
}

/* ───────────────── Подарок за подписку на канал ───────────────── */

/* Что дарим, за какой канал и забирали ли уже — решает сервер и присылает в
   /api/me → bonus. Кабинет ничего не придумывает: выключен подарок — блока в
   «Аккаунтах» нет вовсе, чтобы не обещать дни за несуществующий канал. */
function renderBonus() {
  const block = $('bonusBlock');
  if (!block) return;
  const info = (state.me && state.me.bonus) || {};
  state.bonus = info;
  block.hidden = !info.enabled;
  if (!info.enabled) return;

  const days = Number(info.days) || 0;
  const channel = info.channel || 'канал сервиса';
  $('bonusTitle').textContent = `${days} дн. бесплатно за подписку`;
  $('bonusDesc').textContent = info.claimed
    ? `Дни за подписку на ${channel} уже начислены.`
    : `Подпишитесь на ${channel} и нажмите «Проверить подписку» — дни начислятся сразу.`;

  const note = $('bonusNote');
  note.textContent = info.claimed
    ? '✅ Подарок получен — он даётся один раз на аккаунт.'
    : 'Подарок один на аккаунт. Дни складываются с текущим абонементом.';
  note.classList.toggle('bonus__note--done', Boolean(info.claimed));

  // Забранный подарок оставляет только ссылку на канал: кнопка «Проверить»
  // могла бы ответить лишь «уже получено» — такую кнопку рисовать незачем.
  $('bonusCheck').hidden = Boolean(info.claimed);
  $('bonusOpen').hidden = !info.url;
}

/* Ссылку на канал открываем именно как Telegram-ссылку: openLink увёл бы
   человека во внешний браузер, где он не подписан. */
function openBonusChannel() {
  const url = (state.bonus || {}).url;
  if (!url) {
    toast('Канал не настроен');
    return;
  }
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
  else openExternal(url);
}

/* Проверка подписки и начисление — один запрос: канал и число дней сервер
   берёт у себя, с клиента их принимать нельзя. */
async function claimBonus(button) {
  const info = state.bonus || {};
  try {
    const data = await withLoading(button, () =>
      api('/api/subscription/bonus', { method: 'POST', body: '{}' })
    );
    toast(data.message || 'Подарок начислен');
    if (state.me) state.me.bonus = { ...info, claimed: true, days: data.days };
    renderBonus();
    // Дни уже в подписке — обновляем копилку, бейдж и шапку одним запросом.
    await loadAccounts();
  } catch (error) {
    toast(error.message);
    // 403 — «не вижу вас в канале»: сразу открываем канал, чтобы человек не
    // искал его сам. Остальные отказы говорят за себя.
    if (error.status === 403) openBonusChannel();
  }
}

async function moveBankDays(direction, button) {
  const path = direction === 'freeze' ? '/api/subscription/bank' : '/api/subscription/distribute';
  try {
    const data = await withLoading(button, () => api(path, { method: 'POST', body: '{}' }));
    toast(
      direction === 'freeze'
        ? `${data.moved} дн. убрано в копилку`
        : `${data.moved} дн. вернулось в подписку`
    );
    await loadAccounts();
  } catch (error) {
    toast(error.message);
  }
}

/* ─────────────────────────── Шторка настроек ─────────────────────────── */

/* Одна разметка на «Ещё» и на шторку настроек: разница только в том, куда
   ведёт пункт — внутрь кабинета (data-tab) или в бота (data-start). */
function settingRowHtml(item) {
  const attr = item.tab
    ? `data-tab-goto="${item.tab}"${item.status ? ` data-tab-status="${item.status}"` : ''}`
    : `data-start="${item.start}"`;
  return `
    <button class="setting" ${attr}>
      <div class="setting__emoji" aria-hidden="true">${item.emoji}</div>
      <div class="setting__body">
        <div class="setting__title">${esc(item.title)}</div>
        <div class="setting__desc">${esc(item.desc)}</div>
      </div>
      <div class="setting__chev" aria-hidden="true">›</div>
    </button>`;
}

function renderSettings() {
  $('settingsList').innerHTML = SETTINGS.map(settingRowHtml).join('');
}

/* Экран «Ещё»: сначала разделы кабинета, потом то, что живёт в боте. */
function renderMore() {
  const holder = $('moreList');
  if (!holder) return;
  holder.innerHTML =
    MORE_ITEMS.map(settingRowHtml).join('') +
    '<div class="section-label">в боте</div>' +
    SETTINGS.map(settingRowHtml).join('');
  const about = $('aboutLine');
  if (about) {
    about.textContent = state.build
      ? `Кабинет автоматизаций Telegram · сборка ${state.build}`
      : 'Кабинет автоматизаций Telegram.';
  }
}

/* ────────────────────────────── Пустое состояние ─────────────────────── */

function emptyHtml(emoji, title, text) {
  return `
    <div class="empty">
      <div class="empty__big">${emoji}</div>
      <div class="empty__title">${esc(title)}</div>
      <div>${esc(text)}</div>
    </div>`;
}

/* ───────────────────── Загрузка, ошибка, индикатор ───────────────────── */

function skeletonHtml(kind, count) {
  const withIcon = `
    <div class="sk-card">
      <div class="sk sk-dot"></div>
      <div class="sk-card__body">
        <div class="sk sk-line"></div>
        <div class="sk sk-line--sm"></div>
      </div>
    </div>`;
  const plain = `
    <div class="sk-card">
      <div class="sk-card__body">
        <div class="sk sk-line" style="width:72%"></div>
        <div class="sk sk-line--sm"></div>
      </div>
    </div>`;
  const template = kind === 'plain' ? plain : withIcon;
  return Array.from({ length: count }, () => template).join('');
}

/* Скелетон показываем только когда списка ещё нет. Если данные уже на экране,
   обновляем их бесшовно — иначе при каждом переключении вкладки экран мигал бы. */
function beginLoad(holder, kind = 'card', count = 3) {
  if (holder.children.length && !holder.querySelector('.state')) return;
  holder.setAttribute('aria-busy', 'true');
  holder.innerHTML = skeletonHtml(kind, count);
}

function endLoad(holder) {
  holder.removeAttribute('aria-busy');
}

/* Ошибка обязана предлагать выход, а не только сообщать о беде. */
function errorHtml(message, retryFn) {
  return `
    <div class="state">
      <svg class="mascot mascot--md" viewBox="0 0 120 120" aria-hidden="true"><use href="#crown" /></svg>
      <div class="state__title">Не удалось загрузить</div>
      <div class="state__text">${esc(message)}</div>
      <button class="btn state__retry" data-retry="${retryFn}">Повторить</button>
    </div>`;
}

function failLoad(holder, error, retryFn) {
  endLoad(holder);
  holder.innerHTML = errorHtml(error.message, retryFn);
}

/* Пока идёт запрос, кнопка показывает спиннер: повторный тап ничего не сломает. */
async function withLoading(button, action) {
  if (!button) return action();
  const label = button.textContent;
  button.classList.add('is-loading');
  button.disabled = true;
  try {
    return await action();
  } finally {
    button.classList.remove('is-loading');
    button.disabled = false;
    button.textContent = label;
  }
}

/* ───────────────────────────────── Старт ─────────────────────────────── */

function bindEvents() {
  // нижняя навигация
  document.querySelectorAll('.nav__item').forEach((item) => {
    item.addEventListener('click', () => switchTab(item.dataset.tab));
  });
  // Корона в центре навигации — то же, что «создать задачу»: главное действие
  // кабинета должно быть под большим пальцем, а не в глубине экрана.
  $('crownBtn').addEventListener('click', () => openTaskSheet(null));

  // главная
  $('homeCreate').addEventListener('click', () => openTaskSheet(null));
  $('homeTiles').addEventListener('click', (event) => {
    const tile = event.target.closest('[data-command], [data-goto]');
    if (!tile) return;
    if (tile.dataset.goto) switchTab(tile.dataset.goto);
    else openCommand(tile.dataset.command);
  });
  // Карточки задач на «Главной» краткие: снять с паузы можно здесь же,
  // «открыть» уводит на вкладку с полным набором кнопок. Слушатель делегирован,
  // потому что содержимое списка перерисовывается на каждом обновлении.
  $('homeTasks').addEventListener('click', (event) => {
    const link = event.target.closest('[data-goto]');
    if (link) {
      switchTab(link.dataset.goto);
      return;
    }
    const button = event.target.closest('[data-action]');
    if (!button) return;
    taskAction(button.dataset.action, button.dataset.id, button);
  });
  $('promptForm').addEventListener('submit', (event) => {
    event.preventDefault();
    runSmartSearch();
  });
  $('promptInput').addEventListener('input', renderPromptHits);
  $('promptHits').addEventListener('click', (event) => {
    const hit = event.target.closest('[data-command], [data-goto]');
    if (!hit) return;
    if (hit.dataset.goto) switchTab(hit.dataset.goto);
    else openCommand(hit.dataset.command);
  });
  // «смотреть все» и прочие ссылки-переходы по кабинету
  document.querySelectorAll('[data-goto]').forEach((node) => {
    if (node.closest('#homeTiles, #promptHits, #homeTasks')) return; // у них свой делегат
    node.addEventListener('click', () => switchTab(node.dataset.goto));
  });

  // ещё
  $('moreList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-tab-goto], [data-start]');
    if (!item) return;
    if (item.dataset.start) {
      openBot(item.dataset.start);
      return;
    }
    // «Архив задач» ведёт на вкладку задач сразу в нужный статус.
    if (item.dataset.tabStatus) setTaskStatus(item.dataset.tabStatus);
    switchTab(item.dataset.tabGoto);
  });

  // команды
  $('commandSearch').addEventListener('input', renderCommands);
  $('commandTags').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    const group = chip.dataset.group || null;
    // Повторный тап по активному чипсу снимает фильтр: так работает вся
    // остальная фильтрация в кабинете (чипсы чатов), не будем удивлять.
    state.commandGroup = state.commandGroup === group ? null : group;
    renderCommands();
    // Ряд чипсов прокручивается по горизонтали, и выбранный часто остаётся
    // обрезанным у края. Подтягиваем его в центр — видно, что именно выбрано.
    // block: 'nearest' держит вертикальную прокрутку страницы на месте.
    const active = $('commandTags').querySelector('.chip.is-active');
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
    }
  });
  $('commandList').addEventListener('click', (event) => {
    const card = event.target.closest('[data-command]');
    if (!card) return;
    openCommand(card.dataset.command);
  });

  // задачи
  document.querySelectorAll('#taskStatus .seg').forEach((seg) => {
    seg.addEventListener('click', () => setTaskStatus(seg.dataset.status));
  });
  $('taskList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    taskAction(button.dataset.action, button.dataset.id, button);
  });
  // «＋ Запустить задачу» без выбранной команды — открываем пересылку
  $('addTaskBtn').addEventListener('click', () => openTaskSheet(null));

  // чаты
  renderChatTags();
  let chatTimer = null;
  $('chatSearch').addEventListener('input', () => {
    clearTimeout(chatTimer);
    chatTimer = setTimeout(loadChats, 350);
  });
  // Один делегированный слушатель: чаты перерисовываются при каждом поиске,
  // поэтому вешать обработчик на каждый .chat бессмысленно.
  $('chatList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-chat-id]');
    if (!item) return;
    const id = Number(item.dataset.chatId);
    const chat = state.chats.find((item) => item.id === id);
    if (!chat) return;
    const wasSelected = isChatSelected(id);
    toggleChatSelection(chat);
    // Визуально отражаем состояние без перерисовки всего списка — дешевле.
    const check = item.querySelector('.chat__check');
    if (wasSelected) {
      item.classList.remove('is-selected');
      if (check) check.textContent = '';
    } else {
      item.classList.add('is-selected');
      if (check) check.textContent = '✓';
    }
  });
  $('chatBar').addEventListener('click', (event) => {
    const button = event.target.closest('[data-chat-action]');
    if (!button) return;
    const action = button.dataset.chatAction;
    if (action === 'clear') {
      clearChatSelection();
      return;
    }
    if (!state.selectedChats.length) {
      toast('Сначала выберите чаты');
      return;
    }
    if (action === 'broadcast') {
      openTaskForSelection('broadcast');
    } else if (action === 'mailing') {
      openTaskForSelection('mailing');
    } else if (action === 'forward') {
      openTaskForSelection('copy_channel');
    } else if (action === 'parser') {
      openTaskForSelection('parser');
    } else if (action === 'poster') {
      openTaskForSelection('poster');
    }
  });
  $('chatTags').addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    const tag = chip.dataset.tag;
    const isActive = chip.classList.contains('is-active');
    document.querySelectorAll('#chatTags .chip').forEach((item) => {
      item.classList.remove('is-active');
    });
    if (isActive) {
      state.chatTag = null;
      $('chatSearch').value = '';
    } else {
      chip.classList.add('is-active');
      state.chatTag = tag;
      $('chatSearch').value = tag;
    }
    loadChats();
  });

  // аккаунты
  $('addAccountBtn').addEventListener('click', () => {
    // Вход проходит здесь же. В боте он остаётся запасным путём — на случай,
    // когда шлюз выключен и кабинету нечего показать.
    if (!state.features.account_login_enabled) {
      toast('Вход аккаунтов пока на настройке');
      openBot('add_account');
      return;
    }
    openLoginSheet();
  });
  $('accountList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const account = state.accounts.find((item) => item.id === Number(button.dataset.id));
    if (button.dataset.action === 'resume-login') openLoginSheet();
    else if (button.dataset.action === 'retry-account') retryAccount(button.dataset.id, button);
    else if (button.dataset.action === 'relogin-account') {
      openLoginSheet(account ? account.phone : '');
    } else if (button.dataset.action === 'delete-account') {
      deleteAccount(button.dataset.id, button);
    }
  });
  $('loginSubmit').addEventListener('click', submitLogin);
  $('loginInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitLogin();
    }
  });
  $('loginRestart').addEventListener('click', restartLogin);
  $('loginInBot').addEventListener('click', () => openBot('add_account'));
  $('topUpBtn').addEventListener('click', (event) => {
    // currentTarget, а не target: внутри кнопки может лежать <span>, и тогда
    // индикатор загрузки (withLoading) повесился бы не на ту кнопку.
    payWithStars(event.currentTarget);
  });
  $('webPayBtn').addEventListener('click', (event) => {
    payOnWeb(event.currentTarget);
  });
  $('distributeBtn').addEventListener('click', (event) => {
    moveBankDays('distribute', event.currentTarget);
  });
  $('freezeBtn').addEventListener('click', (event) => {
    moveBankDays('freeze', event.currentTarget);
  });
  // Подарок за подписку: канал открываем ссылкой, а дни начисляет сервер —
  // он же и проверяет подписку.
  $('bonusOpen').addEventListener('click', () => {
    openBonusChannel();
  });
  $('bonusCheck').addEventListener('click', (event) => {
    claimBonus(event.currentTarget);
  });

  // шторки
  $('settingsBtn').addEventListener('click', () => $('settingsSheet').classList.add('is-open'));
  $('settingsList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-start]');
    if (!item) return;
    openBot(item.dataset.start);
  });
  document.querySelectorAll('[data-close]').forEach((node) => {
    node.addEventListener('click', closeSheets);
  });

  // форма задачи: поля и переключатель режима собираются при каждом открытии
  // шторки (у каждой команды свой набор), поэтому слушатели вешаются в bindSheetFields
  $('taskSubmit').addEventListener('click', submitTask);

  // выбор чата мышкой: кнопка «💬 выбрать» живёт в пересобираемой разметке
  // полей, поэтому слушатель делегированный — на контейнер.
  $('taskFields').addEventListener('click', (event) => {
    // Кнопки лежат внутри <label>: без preventDefault клик заодно уходит в
    // поле и на телефоне выскакивает клавиатура поверх списка.
    const library = event.target.closest('[data-pick-library]');
    if (library) {
      event.preventDefault();
      openLibraryPicker();
      return;
    }
    const drop = event.target.closest('[data-library-drop]');
    if (drop) {
      event.preventDefault();
      const id = Number(drop.dataset.libraryDrop);
      state.libraryPick = state.libraryPick.filter((item) => Number(item) !== id);
      renderLibraryPicks();
      return;
    }
    // «Очистить» под полем-списком: набранные двести чатов иначе пришлось бы
    // выделять в поле руками.
    const clear = event.target.closest('[data-count-clear]');
    if (clear) {
      event.preventDefault();
      const key = clear.dataset.countClear;
      const node = $(`task_${key}`);
      if (node) node.value = '';
      renderFieldCount(key);
      return;
    }
    const button = event.target.closest('[data-pick]');
    if (!button) return;
    event.preventDefault();
    openFieldPicker(button.dataset.pick, button.dataset.multi === '1');
  });
  // Счёт под полем должен совпадать с полем и когда чаты вписывают руками.
  $('taskFields').addEventListener('input', (event) => {
    const key = String(event.target.id || '').replace(/^task_/, '');
    if (key && $(`count_${key}`)) renderFieldCount(key);
  });
  $('pickerList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-pick-ref]');
    if (!item) return;
    togglePickerRef(item.dataset.pickRef);
  });
  $('pickerApply').addEventListener('click', applyPicker);
  $('pickerAll').addEventListener('click', () => pickerSelectVisible(true));
  $('pickerNone').addEventListener('click', () => pickerSelectVisible(false));
  let pickerTimer = null;
  $('pickerSearch').addEventListener('input', () => {
    clearTimeout(pickerTimer);
    pickerTimer = setTimeout(pickerReload, 350);
  });
  document.querySelectorAll('[data-picker-close]').forEach((node) => {
    node.addEventListener('click', closePicker);
  });

  // библиотека сообщений
  $('libraryAdd').addEventListener('click', (event) => submitLibraryField(event.currentTarget));
  $('libraryCancel').addEventListener('click', cancelLibraryEdit);
  // Счёт под полем библиотеки живой: правило «делит пустая строка» видно сразу,
  // а не после сохранения десятка лишних записей.
  $('libraryText').addEventListener('input', renderLibraryDraft);
  $('libraryList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action="delete-library"], [data-action="edit-library"]');
    if (!button) return;
    if (button.dataset.action === 'edit-library') startLibraryEdit(button.dataset.id);
    else deleteLibraryItem(button.dataset.id, button);
  });

  // «Повторить» в состоянии ошибки. Один delegated-слушатель на весь документ:
  // блоки ошибки пересоздаются при каждой отрисовке, и вешать слушателя на
  // каждый из них было бы бессмысленно.
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-retry]');
    if (!button) return;
    const name = button.dataset.retry;
    if (name === 'openResults') openResults(state.lastResultsId);
    else if (typeof window[name] === 'function') window[name]();
  });
}

function showDemoBar() {
  const bar = document.createElement('div');
  bar.className = 'demo-bar';
  bar.innerHTML =
    '<span>Демо-данные: кабинет открыт вне Telegram. Внутри бота цифры будут ваши.</span>';
  document.body.insertBefore(bar, document.body.firstChild);
}

/* Свежесть бандла. Адрес кабинета несёт метку сборки (?v=…), а /api/health
   говорит, какая метка сейчас на сервере. Не совпали — значит WebView открыл
   кабинет по старому адресу и держит старые файлы: уходим на адрес с новой
   меткой (другой ключ кэша — файлы приедут с сервера).

   Метку сверяем один раз на метку: если перезагрузка почему-то не помогла,
   второй круг не начинаем, иначе кабинет зациклится вместо того, чтобы
   открыться хоть как-то. */
async function reloadIfBuildIsStale() {
  const params = new URLSearchParams(location.search);
  state.build = params.get('v') || '';
  let build = '';
  try {
    const response = await fetch('/api/health', { cache: 'no-store' });
    if (!response.ok) return false;
    build = (await response.json()).build || '';
  } catch (error) {
    return false; // сервер недоступен — это не повод не открывать кабинет
  }
  if (!build || params.get('v') === build) return false;

  const key = 'build-reload:' + build;
  try {
    if (sessionStorage.getItem(key)) return false;
    sessionStorage.setItem(key, '1');
  } catch (error) {
    // Приватный режим без sessionStorage: одна перезагрузка всё равно нужнее.
  }
  params.set('v', build);
  location.replace(location.pathname + '?' + params.toString() + location.hash);
  return true;
}

async function boot() {
  if (await reloadIfBuildIsStale()) return;
  if (tg) {
    tg.ready();
    tg.expand();
  }
  applyTheme();
  renderSettings();
  renderMore();
  bindEvents();
  if (DEMO) showDemoBar();

  try {
    try {
      state.me = await api('/api/me');
      renderHeader();
      // Цена в звёздах приходит с тарифами, поэтому подпись кнопки знает её
      // только здесь — до этого на кнопке нейтральный текст из index.html.
      renderTopUpButton();
      // Кнопка «на сайте» появляется только если контур внешней оплаты включён.
      renderWebPayButton();
      // Подарок за подписку на канал — тоже по ответу сервера: выключен, и
      // карточки в «Аккаунтах» просто нет.
      renderBonus();
    } catch (error) {
      toast(error.message);
    }

    await loadAccounts();
    await loadCommands();
    // Плитки «быстрого старта» и подсказки поиска живут на каталоге команд,
    // поэтому «Главную» собираем после него, а не на старте.
    renderHome();
    // Подгружаем задачи по всем трём статусам параллельно — бейджи команд
    // («N активных · M на паузе») считаются по сумме трёх списков, а не
    // только по текущей вкладке, иначе они врут при первом заходе.
    await Promise.all([
      loadTasks('active'),
      loadTasks('paused'),
      loadTasks('done'),
    ]);
  } finally {
    // Заставку убираем в любом случае: если часть запросов упала, пользователь
    // всё равно должен увидеть кабинет и кнопки «Повторить».
    $('boot').classList.add('is-hidden');
  }
}

boot();
