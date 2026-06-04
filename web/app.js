// SoulMap WebUI - 优化版：防抖搜索、特殊排序、本地缓存
let allFields = [];
let currentPage = 1;
let totalPages = 1;
let totalUsers = 0;
let isLoading = false;
let currentQuery = '';
let currentProfiles = {};  // 当前页数据
let statsCache = null;
let debugInfo = null;
let bulkMode = false;
const selectedCards = new Set();
const LOCAL_CACHE_KEY = 'soulmap_webui_profiles_cache_v2';
const API_BASE = window.location.pathname.replace(/\/[^/]*$/, '').replace(/\/$/, '');
const WEBUI_DEBUG = (typeof window.soulmapWebuiDebug === 'boolean')
  ? window.soulmapWebuiDebug
  : (localStorage.getItem('soulmap_debug') !== '0');
function uiLog(level, msg, data) {
  if (!WEBUI_DEBUG && level === 'debug') return;
  const fn = console[level] || console.log;
  if (data !== undefined) fn.call(console, `[SoulMap WebUI] ${msg}`, data);
  else fn.call(console, `[SoulMap WebUI] ${msg}`);
}
function apiUrl(path) {
  return API_BASE + path;
}

document.addEventListener('DOMContentLoaded', () => {
  uiLog('info', 'DOMContentLoaded，开始初始化', { API_BASE, hasLocalCache: !!loadLocalCache() });
  const ovStats = document.getElementById('ov-stats');
  const ovRecent = document.getElementById('ov-recent');
  if (ovStats) ovStats.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:24px;color:var(--outline)">加载中…</div>';
  if (ovRecent) ovRecent.innerHTML = '<div style="text-align:center;padding:24px;color:var(--outline)">加载中…</div>';
  loadPage(1);
});

// ===== 防抖 =====
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

const doSearch = debounce((q) => {
  currentQuery = q;
  currentPage = 1;
  clearList();
  loadPage(1);
}, 300);

function filterUsers() {
  doSearch(document.getElementById('q').value.trim());
}

function mergeProfileKeys(oldKeys, newKeys, mergedProfiles) {
  const seen = new Set();
  const result = [];
  const add = (key) => {
    const k = String(key || '').trim();
    if (!k || seen.has(k) || !mergedProfiles[k]) return;
    seen.add(k);
    result.push(k);
  };
  (oldKeys || []).forEach(add);
  (newKeys || []).forEach(add);
  Object.keys(mergedProfiles || {}).forEach(add);
  return sortProfileKeys(result);
}

function saveLocalCache(data) {
  try {
    if (!data || !data.profiles || !Object.keys(data.profiles).length) return;
    const old = loadLocalCache() || {};
    const mergedProfiles = Object.assign({}, old.profiles || {}, data.profiles || {});
    const mergedKeys = mergeProfileKeys(old.profile_keys, data.profile_keys, mergedProfiles);
    localStorage.setItem(LOCAL_CACHE_KEY, JSON.stringify({
      time: Date.now(),
      profiles: mergedProfiles,
      profile_keys: mergedKeys,
      fields: data.fields || old.fields || allFields,
      pagination: data.pagination || old.pagination || {}
    }));
    uiLog('debug', '已写入本地缓存', { mergedUsers: Object.keys(mergedProfiles).length, mergedKeys: mergedKeys.length });
  } catch (e) {
    uiLog('warn', '写入本地缓存失败', e);
  }
}

function loadLocalCache() {
  try {
    const raw = localStorage.getItem(LOCAL_CACHE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}

function applyLocalCache() {
  const c = loadLocalCache();
  if (!c || !c.profiles || !Object.keys(c.profiles).length) {
    uiLog('debug', '本地缓存未命中');
    return false;
  }
  allFields = c.fields || allFields;
  currentProfiles = c.profiles || {};
  currentProfileKeys = sortProfileKeys(c.profile_keys || Object.keys(currentProfiles));
  const pag = c.pagination || {};
  totalUsers = pag.total || Object.keys(currentProfiles).length;
  totalPages = pag.total_pages || 1;
  currentPage = pag.page || 1;
  renderCurrentTab(currentProfiles, true);
  uiLog('info', '已应用本地缓存', { users: Object.keys(currentProfiles).length, totalUsers, currentPage, totalPages });
  snk('已显示本地缓存数据');
  return true;
}

function showLoadError(msg) {
  const text = esc(msg || '加载失败');
  const html = `<div class="empty"><p>${text}</p></div>`;
  const active = document.querySelector('.tab.active');
  const tab = active ? active.dataset.tab : 'overview';
  if (tab === 'users') {
    const list = document.getElementById('ulist');
    if (list) list.innerHTML = html;
  } else if (tab === 'stats') {
    const stats = document.getElementById('stats-c');
    if (stats) stats.innerHTML = html;
  } else {
    const ovStats = document.getElementById('ov-stats');
    const ovRecent = document.getElementById('ov-recent');
    if (ovStats) ovStats.innerHTML = `<div style="grid-column:1/-1">${html}</div>`;
    if (ovRecent) ovRecent.innerHTML = '';
  }
}

// ===== 数据加载 =====
async function loadPage(page) {
  if (isLoading) {
    uiLog('debug', 'loadPage跳过：已有请求进行中', { page });
    return;
  }
  isLoading = true;
  const started = Date.now();
  try {
    let url = apiUrl('/api/profiles');
    if (currentQuery) url += `&q=${encodeURIComponent(currentQuery)}`;
    uiLog('info', '开始加载画像页', { page, url, query: currentQuery });
    const data = await apiJson(url);
    saveLocalCache(data);
    allFields = data.fields || [];
    const pag = data.pagination || {};
    currentPage = pag.page || 1;
    totalPages = pag.total_pages || 1;
    totalUsers = pag.total || 0;
    uiLog('info', '画像页加载成功', { page: currentPage, totalPages, totalUsers, pageUsers: Object.keys(data.profiles || {}).length, costMs: Date.now() - started });
    if (totalUsers === 0 && page === 1 && applyLocalCache()) return;
    // page=1 时清空旧数据，避免新旧数据混合
    if (page === 1) {
      currentProfiles = {};
    }
    // 合并到当前数据
    Object.assign(currentProfiles, data.profiles || {});
    // 保存服务端返回的排序顺序，并用前端同款特殊排序兜底，避免缓存/旧后端导致乱序
    currentProfileKeys = sortProfileKeys(data.profile_keys || Object.keys(data.profiles || {}));
    renderCurrentTab(currentProfiles, true);
  } catch (e) {
    uiLog('error', '画像页加载失败', { page, error: e && e.message ? e.message : String(e), costMs: Date.now() - started });
    if (!applyLocalCache()) showLoadError('加载失败: ' + e.message);
    snk('加载失败: ' + e.message);
  } finally {
    isLoading = false;
    uiLog('debug', 'loadPage结束', { page, isLoading });
  }
}

async function loadDebug() {
  if (debugInfo) {
    uiLog('debug', '复用debug缓存');
    return debugInfo;
  }
  try {
    uiLog('debug', '请求debug信息');
    debugInfo = await apiJson(apiUrl('/api/debug'));
    uiLog('debug', 'debug信息返回', debugInfo);
    return debugInfo;
  } catch (e) {
    uiLog('warn', 'debug信息请求失败', e);
    return null;
  }
}

async function loadStats() {
  if (statsCache) return statsCache;
  try {
    statsCache = await apiJson(apiUrl('/api/stats'));
    return statsCache;
  } catch (e) {
    snk('统计加载失败: ' + e.message);
    return null;
  }
}

async function refreshData() {
  currentProfiles = {};
  currentProfileKeys = [];
  statsCache = null;
  debugInfo = null;
  currentPage = 1;
  clearList();
  await loadPage(1);
  snk('已刷新');
}

// ===== 标签切换 =====
function switchTab(t) {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${t}"]`).classList.add('active');
  document.querySelectorAll('.tab-content').forEach(x => x.style.display = 'none');
  document.getElementById('tab-' + t).style.display = 'block';
  if (t === 'stats') renderStatsTab();
  else if (t === 'overview') renderOV();
  else {
    // 每次切换回用户列表时重置 DOM，避免重复追加
    document.getElementById('ulist').innerHTML = '';
    // 若没有加载过数据或数据为空，重新请求第一页
    if (!Object.keys(currentProfiles).length) {
      loadPage(1);
    } else {
      renderUL(currentProfiles, true);
    }
  }
}

function renderCurrentTab(pageData, isFirst) {
  const t = document.querySelector('.tab.active').dataset.tab;
  if (t === 'overview') renderOV(false);
  else if (t === 'users') renderUL(currentProfiles, isFirst);
  else renderStatsTab();
}

// ===== 概览 =====
async function renderOV(withStats) {
  if (withStats === undefined) withStats = true;
  const stats = withStats ? await loadStats() : statsCache;
  const n = stats && typeof stats.user_count !== 'undefined' ? stats.user_count : totalUsers;
  const fieldsCount = ((stats && stats.fields) || allFields).length;
  const avg = stats && typeof stats.avg_fields !== 'undefined' ? stats.avg_fields : 0;
  const platformCount = stats && typeof stats.platform_count !== 'undefined' ? stats.platform_count : 0;

  document.getElementById('ov-stats').innerHTML = `
    <div class="stat-card"><div class="v">${n}</div><div class="l">用户总数</div></div>
    <div class="stat-card"><div class="v">${platformCount}</div><div class="l">已获取平台</div></div>
    <div class="stat-card"><div class="v">${fieldsCount}</div><div class="l">可用字段</div></div>
    <div class="stat-card"><div class="v">${avg}</div><div class="l">平均字段</div></div>`;

  // 取特殊排序后的前 6 个，星标会优先展示
  const keys = sortProfileKeys(currentProfileKeys.length ? currentProfileKeys : Object.keys(currentProfiles)).slice(0, 6);
  const recent = document.getElementById('ov-recent');
  if (keys.length) {
    recent.innerHTML = renderCards(keys);
  } else {
    const dbg = await loadDebug();
    if (dbg) {
      recent.innerHTML = `<div class="empty"><p>暂无画像数据</p><p style="font-size:12px;line-height:1.6;color:var(--outline);word-break:break-all">数据文件：${esc(dbg.profiles_file || '')}<br>文件存在：${dbg.profiles_file_exists ? '是' : '否'}，用户数：${dbg.user_count || 0}</p></div>`;
    } else {
      recent.innerHTML = '<div class="empty"><p>暂无画像数据</p></div>';
    }
  }
}

// ===== 用户列表 =====
function renderUL(pageData) {
  const el = document.getElementById('ulist');
  const keys = sortProfileKeys(currentProfileKeys.length ? currentProfileKeys : Object.keys(pageData || {}));
  // 当前接口一次返回完整列表，统一重绘可避免旧分页追加导致重复卡片。
  el.innerHTML = renderCards(keys);
  updatePagInfo();
  if (!Object.keys(currentProfiles).length) {
    renderEmptyUsers(el);
  }
}



async function renderEmptyUsers(el) {
  const dbg = await loadDebug();
  if (dbg) {
    el.innerHTML = `<div class="empty"><div class="icon"><svg viewBox="0 0 24 24"><path d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H5V5h14v14zm-7-2h2v-4h4v-2h-4V7h-2v4H7v2h4v4z"/></svg></div><p>暂无数据</p><p style="font-size:12px;line-height:1.6;color:var(--outline);word-break:break-all">数据文件：${esc(dbg.profiles_file || '')}<br>文件存在：${dbg.profiles_file_exists ? '是' : '否'}，用户数：${dbg.user_count || 0}</p></div>`;
  } else {
    el.innerHTML = '<div class="empty"><p>暂无数据</p></div>';
  }
}

function clearList() {
  document.getElementById('ulist').innerHTML = '';
  currentProfiles = {};
}

function updatePagInfo() {
  let info = document.getElementById('pag-info');
  if (!info) {
    const el = document.getElementById('ulist');
    el.insertAdjacentHTML('afterend', '<div id="pag-info" style="text-align:center;padding:16px;font-size:12px;color:var(--outline)"></div>');
    info = document.getElementById('pag-info');
  }
  info.textContent = `共 ${totalUsers || Object.keys(currentProfiles).length} 位用户`;
}



function platformLabel(raw) {
const p = String(raw || '').toLowerCase().trim();
if (!p) return '';
if (p.includes('telegram') || p === 'tg') return 'TG';
if (p.includes('gewechat') || p === 'wechat' || p.includes('微信')) return '微信';
if (p.includes('aiocqhttp') || p.includes('aiohttp') || p.includes('qq')) return 'QQ';
return raw;
}

function profileDisplayName(key, profile) {
const p = profile || {};
const name = String(p['对用户的称呼'] || '').trim();
return name || String(key || '');
}

function profileSubTitle(key, profile) {
const p = profile || {};
const time = p._last_updated || '';
const name = String(p['对用户的称呼'] || '').trim();
return name && name !== key ? `${key}${time ? ' · ' + time : ''}` : time;
}

function isStarred(key) {
const p = currentProfiles[key];
return !!(p && p._starred);
}
async function toggleStar(key) {
const p = currentProfiles[key];
const isStarred = !!(p && p._starred);
try {
let d;
if (isStarred) {
d = await apiJson(apiUrl(`/api/profile/${enc(key)}/_starred`), { method: 'DELETE' });
} else {
d = await apiJson(apiUrl(`/api/profile/${enc(key)}`), {
method: 'PUT',
headers: { 'Content-Type': 'application/json' },
body: JSON.stringify({ field: '_starred', value: 'true' })
});
}
if (d.success) {
statsCache = null;
await refreshData();
}
} catch(e) { snk(e.message || '操作失败'); }
}
function hasProfileName(profile) {
return !!String((profile || {})['对用户的称呼'] || '').trim();
}

// 本地汉字→拼音映射（常用200字，覆盖常见昵称），无依赖
const PINYIN_MAP = {
'阿':'a','啊':'a','爱':'ai','安':'an','昂':'ang','暗':'an',
'吧':'ba','把':'ba','八':'ba','巴':'ba','白':'bai','百':'bai','半':'ban','帮':'bang','保':'bao','北':'bei','本':'ben','比':'bi','笔':'bi','必':'bi','变':'bian','边':'bian','别':'bie',
'程':'cheng','吃':'chi','出':'chu','春':'chun','次':'ci','从':'cong','村':'cun','错':'cuo',
'大':'da','的':'de','等':'deng','得':'de','灯':'deng','点':'dian','电':'dian','定':'ding','东':'dong','冬':'dong','懂':'dong','对':'dui','多':'duo','读':'du',
'饿':'e','儿':'er','二':'er',
'发':'fa','法':'fa','反':'fan','范':'fan','方':'fang','放':'fang','非':'fei','飞':'fei','分':'fen','风':'feng','福':'fu','父':'fu','复':'fu',
'高':'gao','个':'ge','给':'gei','跟':'gen','更':'geng','工':'gong','公':'gong','共':'gong','狗':'gou','古':'gu','故':'gu','关':'guan','管':'guan','光':'guang','广':'guang','国':'guo','果':'guo',
'还':'hai','害':'hai','汉':'han','好':'hao','号':'hao','喝':'he','黑':'hei','很':'hen','红':'hong','后':'hou','胡':'hu','湖':'hu','护':'hu','花':'hua','化':'hua','画':'hua','话':'hua','坏':'huai','换':'huan','黄':'huang','回':'hui','会':'hui','火':'huo','活':'huo',
'机':'ji','基':'ji','级':'ji','几':'ji','己':'ji','继':'ji','加':'jia','家':'jia','价':'jia','架':'jia','件':'jian','见':'jian','建':'jian','江':'jiang','讲':'jiang','交':'jiao','角':'jiao','觉':'jue','姐':'jie','今':'jin','金':'jin','尽':'jin','进':'jin','近':'jin','经':'jing','精':'jing','井':'jing','静':'jing','九':'jiu','酒':'jiu','久':'jiu','就':'jiu','居':'ju','局':'ju','举':'ju','巨':'ju','聚':'ju',
'开':'kai','看':'kan','康':'kang','靠':'kao','可':'ke','刻':'ke','客':'ke','口':'kou','苦':'ku','快':'kuai','宽':'kuan','况':'kuang','亏':'kui','困':'kun',
'拉':'la','来':'lai','蓝':'lan','浪':'lang','老':'lao','乐':'le','雷':'lei','冷':'leng','离':'li','里':'li','理':'li','礼':'li','力':'li','历':'li','立':'li','连':'lian','联':'lian','脸':'lian','练':'lian','恋':'lian','良':'liang','两':'liang','亮':'liang','量':'liang','林':'lin','领':'ling','另':'ling','留':'liu','流':'liu','六':'liu','龙':'long','楼':'lou','路':'lu','旅':'lv','绿':'lv','乱':'luan','略':'lve',
'马':'ma','吗':'ma','麦':'mai','满':'man','忙':'mang','毛':'mao','没':'mei','每':'mei','美':'mei','妹':'mei','门':'men','梦':'meng','米':'mi','面':'mian','民':'min','明':'ming','命':'ming','模':'mo','木':'mu','目':'mu',
'拿':'na','那':'na','娜':'na','男':'nan','南':'nan','呢':'ne','内':'nei','能':'neng','你':'ni','年':'nian','念':'nian','鸟':'niao','牛':'niu','农':'nong','努':'nu','女':'nv','暖':'nuan',
'怕':'pa','拍':'pai','派':'pai','盘':'pan','朋':'peng','皮':'pi','片':'pian','票':'piao','漂':'piao','品':'pin','平':'ping','普':'pu',
'七':'qi','期':'qi','其':'qi','奇':'qi','骑':'qi','起':'qi','气':'qi','器':'qi','强':'qiang','墙':'qiang','桥':'qiao','巧':'qiao','青':'qing','清':'qing','轻':'qing','情':'qing','求':'qiu','区':'qu','取':'qu','去':'qu','全':'quan','却':'que','群':'qun',
'然':'ran','让':'rang','热':'re','任':'ren','日':'ri','容':'rong','肉':'rou','如':'ru','入':'ru',
'三':'san','色':'se','杀':'sha','沙':'sha','山':'shan','上':'shang','少':'shao','社':'she','身':'shen','深':'shen','什':'shen','生':'sheng','声':'sheng','师':'shi','十':'shi','时':'shi','实':'shi','食':'shi','始':'shi','世':'shi','市':'shi','事':'shi','是':'shi','室':'shi','试':'shi','适':'shi','收':'shou','手':'shou','首':'shou','受':'shou','书':'shu','树':'shu','双':'shuang','水':'shui','睡':'shui','顺':'shun','思':'si','死':'si','四':'si','送':'song','素':'su','速':'su','算':'suan','虽':'sui','岁':'sui','所':'suo',
'他':'ta','她':'ta','它':'ta','太':'tai','态':'tai','谈':'tan','唐':'tang','糖':'tang','特':'te','疼':'teng','提':'ti','体':'ti','天':'tian','田':'tian','条':'tiao','铁':'tie','听':'ting','停':'ting','通':'tong','同':'tong','头':'tou','图':'tu','团':'tuan','推':'tui','腿':'tui','外':'wai','玩':'wan','完':'wan','晚':'wan','王':'wang','往':'wang','网':'wang','望':'wang','忘':'wang','危':'wei','位':'wei','文':'wen','闻':'wen','问':'wen','我':'wo','握':'wo',
'西':'xi','息':'xi','希':'xi','吸':'xi','系':'xi','下':'xia','夏':'xia','先':'xian','线':'xian','想':'xiang','向':'xiang','像':'xiang','小':'xiao','校':'xiao','笑':'xiao','些':'xie','写':'xie','谢':'xie','心':'xin','新':'xin','信':'xin','星':'xing','行':'xing','形':'xing','醒':'xing','姓':'xing','休':'xiu','修':'xiu','需':'xu','许':'xu','学':'xue','雪':'xue',
'牙':'ya','呀':'ya','压':'ya','亚':'ya','言':'yan','研':'yan','眼':'yan','演':'yan','阳':'yang','养':'yang','样':'yang','要':'yao','药':'yao','爷':'ye','也':'ye','夜':'ye','叶':'ye','业':'ye','一':'yi','医':'yi','衣':'yi','以':'yi','已':'yi','意':'yi','易':'yi','因':'yin','音':'yin','银':'yin','印':'yin','英':'ying','影':'ying','用':'yong','优':'you','由':'you','油':'you','游':'you','友':'you','有':'you','又':'you','右':'you','预':'yu','元':'yuan','原':'yuan','圆':'yuan','远':'yuan','院':'yuan','愿':'yuan','月':'yue','越':'yue','云':'yun','运':'yun','在':'zai','早':'zao','怎':'zen','扎':'zha','站':'zhan','张':'zhang','找':'zhao','照':'zhao','者':'zhe','这':'zhe','真':'zhen','正':'zheng','政':'zheng','知':'zhi','之':'zhi','只':'zhi','纸':'zhi','指':'zhi','至':'zhi','治':'zhi','中':'zhong','钟':'zhong','种':'zhong','重':'zhong','州':'zhou','周':'zhou','主':'zhu','住':'zhu','注':'zhu','祝':'zhu','专':'zhuan','转':'zhuan','装':'zhuang','准':'zhun','子':'zi','自':'zi','字':'zi','资':'zi','走':'zou','足':'zu','组':'zu','最':'zui','昨':'zuo','左':'zuo','作':'zuo','做':'zuo','坐':'zuo',
};

function pinyinSortKey(s) {
const text = String(s || '').trim();
if (!text) return '~';
try {
if (typeof pinyinPro !== 'undefined' && pinyinPro.pinyin) {
return pinyinPro.pinyin(text, { toneType: 'none', v: true }).replace(/\s+/g, '').toLowerCase();
}
} catch (_) {}
// 本地字典 fallback：逐字查拼音
let result = '';
for (const ch of text) {
result += (PINYIN_MAP[ch] || ch.toLowerCase());
}
return result;
}

function profileSortKey(key) {
const p = currentProfiles[key] || {};
const name = profileDisplayName(key, p);
return {
star: p._starred ? 0 : 1,
named: hasProfileName(p) ? 0 : 1,
name,
pName: pinyinSortKey(name),
key: String(key || ''),
pKey: pinyinSortKey(key)
};
}

function sortProfileKeys(keys) {
return (keys || []).slice().sort((a, b) => {
const ka = profileSortKey(a);
const kb = profileSortKey(b);
// 1. 星标优先
if (ka.star !== kb.star) return ka.star - kb.star;
// 2. 有称呼优先，避免纯ID夹在命名用户中间
if (ka.named !== kb.named) return ka.named - kb.named;
// 3. 显示名按拼音混排（pinyin-pro + 本地字典 fallback）
const pCmp = ka.pName.localeCompare(kb.pName);
if (pCmp !== 0) return pCmp;
// 4. key 拼音兜底
const pKeyCmp = ka.pKey.localeCompare(kb.pKey);
if (pKeyCmp !== 0) return pKeyCmp;
// 5. 原始名称本地化兜底
const cmp = ka.name.localeCompare(kb.name, 'zh-CN', { sensitivity: 'base', numeric: true });
if (cmp !== 0) return cmp;
// 6. 原始 key 稳定兜底
return ka.key.localeCompare(kb.key, 'zh-CN', { sensitivity: 'base', numeric: true });
});
}




function textRank(s) { return 0; }

function renderCards(keys) {
if (!keys.length) return '';
return keys.map(k => {
const p = currentProfiles[k];
if (!p) return '';
const name = profileDisplayName(k, p);
const subTitle = profileSubTitle(k, p);
const platform = platformLabel(p._platform || '');
const platformBadge = platform ? `<span class="platform-badge">${esc(platform)}</span>` : '';
const fs = Object.keys(p).filter(f => !f.startsWith('_'));
const chips = fs.slice(0, 4).map(f => `<span class="chip">${esc(f)}</span>`).join('');
const more = fs.length > 4 ? `<span class="chip">+${fs.length - 4}</span>` : '';
const checked = selectedCards.has(k) ? 'checked' : '';
const starred = isStarred(k);
const starIcon = starred ? '⭐' : '☆';
const pinBtn = `<button class="star-btn ${starred ? 'starred' : ''}" onclick="event.stopPropagation(); toggleStar(${jsArg(k)})" title="${starred ? '取消星标' : '添加星标'}" aria-label="${starred ? '取消星标' : '添加星标'}">${starIcon}</button>`;
return `<div class="card" onclick="handleCardClick(event, ${jsArg(k)})"><input class="select-box" type="checkbox" ${checked} onclick="toggleCardSelected(event, ${jsArg(k)})">${platformBadge}${pinBtn}<div class="card-top"><div class="card-info"><div class="card-name">${esc(name)}</div><div class="card-sub">${esc(subTitle)}</div></div></div><div class="chips">${chips}${more}</div></div>`;
}).join('');
}

// ===== 统计 =====
async function renderStatsTab() {
  const el = document.getElementById('stats-c');
  el.innerHTML = '<div style="text-align:center;padding:32px;color:var(--outline)">加载中…</div>';
  try {
    const data = await loadStats();
    if (!data || typeof data.user_count === 'undefined') {
      el.innerHTML = '<div class="empty"><div class="icon"><svg viewBox="0 0 24 24"><path d="M5 9.2h3V19H5V9.2zM10.6 5h2.8v14h-2.8V5zm5.6 8H19v6h-2.8v-6z"/></svg></div><p>暂无数据</p></div>';
      return;
    }
    const n = data.user_count || 0, counts = data.field_counts || {};
    if (n === 0) {
      el.innerHTML = '<div class="empty"><p>暂无用户数据</p></div>';
      return;
    }
    const fields = (data.fields || allFields).slice().sort((a, b) => {
      const ca = counts[a] || 0;
      const cb = counts[b] || 0;
      if ((ca > 0) !== (cb > 0)) return ca > 0 ? -1 : 1;
      if (ca !== cb) return cb - ca;
      return a.localeCompare(b, 'zh-Hans-CN', { sensitivity: 'base' });
    });
    el.innerHTML = fields.map(f => {
      const c = counts[f] || 0, pct = n > 0 ? (c / n * 100).toFixed(1) : '0.0';
      return `<div class="bar-item"><div class="bar-head"><span class="bar-name">${esc(f)}</span><span class="bar-val">${c}/${n} (${pct}%)</span></div><div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div></div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = `<div class="empty"><p>统计加载失败: ${esc(e.message || '未知错误')}</p></div>`;
  }
}

// ===== 用户详情对话框 =====
async function openD(key) {
  // 如果本地没有完整数据，从服务端获取
  let p = currentProfiles[key];
  if (!p) {
    try {
      const data = await apiJson(apiUrl(`/api/profile/${encodeURIComponent(key)}`));
      p = data.profile || {};
      currentProfiles[key] = p;
    } catch (e) { snk('加载失败'); return; }
  }
  const name = profileDisplayName(key, p);
  document.getElementById('dtitle').textContent = name;

  const keyId = safeId(key);
  let h = '<ul class="flist" id="flist-' + keyId + '">';
  const editIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';
  const delIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';
  
  const renderItem = (f) => {
    const fId = safeId(f);
    return `
    <li class="fitem" id="fitem-${keyId}-${fId}">
      <div class="fitem-c">
        <div class="fitem-l">${esc(f)}</div>
        <div class="fitem-v" onclick="startEdit(${jsArg(key)},${jsArg(f)})">${esc(p[f])}</div>
      </div>
      <div class="fitem-acts">
        <button class="btn btn-icon btn-sm" onclick="startEdit(${jsArg(key)},${jsArg(f)})" title="编辑">${editIcon}</button>
        <button class="btn btn-icon btn-sm btn-del" onclick="delField(${jsArg(key)},${jsArg(f)})" title="删除">${delIcon}</button>
      </div>
    </li>`;
  };
  
  for (const f of allFields) if (p[f]) h += renderItem(f);
  for (const f of Object.keys(p)) if (!f.startsWith('_') && !allFields.includes(f)) h += renderItem(f);
  h += '</ul>';
  
  // 添加新字段区域
  h += `<div class="add-sec">
    <div class="add-title">添加字段</div>
    <div class="field-chips" id="field-chips-${keyId}">
      ${allFields.map(f => p[f] ? `<span class="field-chip used">${esc(f)}</span>` : `<button class="field-chip" onclick="selectField(${jsArg(key)},${jsArg(f)})">${esc(f)}</button>`).join('')}
    </div>
    <div id="add-form-${keyId}" style="display:none">
      <input type="hidden" id="ef-${keyId}">
      <div class="add-row">
        <input class="md-inp" id="ev-${keyId}" placeholder="输入值..." autocomplete="off">
        <button class="btn btn-f" onclick="saveField(${jsArg(key)})">保存</button>
        <button class="btn btn-text" onclick="cancelAdd(${jsArg(key)})">取消</button>
      </div>
    </div>
  </div>`;

  document.getElementById('dbody').innerHTML = h;
  const delUserIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';
  document.getElementById('dacts').innerHTML = `<div style="font-size:11px;color:var(--outline);text-align:center">最后更新: ${esc(p._last_updated || '未知')}</div><button class="btn btn-e" onclick="delUser(${jsArg(key)})" style="width:100%">${delUserIcon} 删除用户</button>`;
  document.getElementById('scrim').classList.add('active');
}

function closeD(e) {
  if (e && e.target !== document.getElementById('scrim')) return;
  document.getElementById('scrim').classList.remove('active');
}

// ===== 编辑操作 =====
function startEdit(k, f) {
  const item = document.getElementById(`fitem-${safeId(k)}-${safeId(f)}`);
  const p = currentProfiles[k];
  if (!p) return;
  
  const editIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';
  const delIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';
  
  item.innerHTML = `
    <div class="fitem-c">
      <div class="fitem-l">${esc(f)}</div>
      <input class="fitem-edit" id="edit-${safeId(k)}-${safeId(f)}" value="${esc(p[f])}" autocomplete="off">
    </div>
    <div class="fitem-acts">
      <button class="btn btn-icon btn-sm btn-save" onclick="saveEdit(${jsArg(k)},${jsArg(f)})" title="保存">✓</button>
      <button class="btn btn-icon btn-sm" onclick="cancelEdit(${jsArg(k)},${jsArg(f)},${jsArg(p[f])})" title="取消">✕</button>
    </div>`;
  const editInput = document.getElementById(`edit-${safeId(k)}-${safeId(f)}`);
  editInput.focus();
  editInput.select();
}

async function saveEdit(k, f) {
  const inp = document.getElementById(`edit-${safeId(k)}-${safeId(f)}`);
  const v = inp.value.trim();
  if (!v) { snk('请输入值'); return; }
  try {
    const d = await apiJson(apiUrl(`/api/profile/${enc(k)}`), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ field: f, value: v }) });
    if (d.success) { 
      snk('已保存'); 
      statsCache = null; 
      await refreshData(); 
      openD(k); 
    } else snk(d.error || '失败');
  } catch (e) { snk(e.message || '请求失败'); }
}

function cancelEdit(k, f, v) {
  const editIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>';
  const delIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';
  
  const item = document.getElementById(`fitem-${safeId(k)}-${safeId(f)}`);
  item.innerHTML = `
    <div class="fitem-c">
      <div class="fitem-l">${esc(f)}</div>
      <div class="fitem-v" onclick="startEdit(${jsArg(k)},${jsArg(f)})">${esc(v)}</div>
    </div>
    <div class="fitem-acts">
      <button class="btn btn-icon btn-sm" onclick="startEdit(${jsArg(k)},${jsArg(f)})" title="编辑">${editIcon}</button>
      <button class="btn btn-icon btn-sm btn-del" onclick="delField(${jsArg(k)},${jsArg(f)})" title="删除">${delIcon}</button>
    </div>`;
}

function selectField(k, f) {
  const kId = safeId(k);
  const form = document.getElementById(`add-form-${kId}`);
  form.style.display = 'block';
  form.innerHTML = `<input type="hidden" id="ef-${kId}" value="${esc(f)}">
    <div class="selected-field">${esc(f)}</div>
    <div class="add-row">
      <input class="md-inp" id="ev-${kId}" placeholder="输入 ${esc(f)} 的值..." autocomplete="off">
      <button class="btn btn-f" onclick="saveField(${jsArg(k)})">保存</button>
      <button class="btn btn-text" onclick="cancelAdd(${jsArg(k)})">取消</button>
    </div>`;
  document.getElementById(`ev-${kId}`).focus();
  document.getElementById(`field-chips-${kId}`).style.display = 'none';
}

function cancelAdd(k) {
  const kId = safeId(k);
  document.getElementById(`add-form-${kId}`).style.display = 'none';
  document.getElementById(`field-chips-${kId}`).style.display = 'flex';
  // 清除输入值（input 可能已被重建，容错处理）
  const evInput = document.getElementById(`ev-${kId}`);
  if (evInput) evInput.value = '';
}

async function saveField(k) {
  const kId = safeId(k);
  const f = document.getElementById(`ef-${kId}`).value;
  const v = document.getElementById(`ev-${kId}`).value.trim();
  if (!v) { snk('请输入值'); return; }
  try {
    const d = await apiJson(apiUrl(`/api/profile/${enc(k)}`), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ field: f, value: v }) });
    if (d.success) { snk('已保存'); statsCache = null; await refreshData(); openD(k); }
    else snk(d.error || '失败');
  } catch (e) { snk(e.message || '请求失败'); }
}

// ===== API 操作 =====
async function delField(k, f) {
  const ok = await confirmAction({
    title: '删除字段',
    message: `确定删除「${f}」？`,
    okText: '删除'
  });
  if (!ok) return;
  try {
    const d = await apiJson(apiUrl(`/api/profile/${enc(k)}/${enc(f)}`), { method: 'DELETE' });
    if (d.success) { snk('已删除'); statsCache = null; await refreshData(); openD(k); }
    else snk(d.error || '失败');
  } catch (e) { snk(e.message || '请求失败'); }
}

function updateBulkCount() {
const el = document.getElementById('bulk-selected-count');
if (el) el.textContent = String(selectedCards.size);
const list = document.getElementById('ulist');
if (list) list.classList.toggle('bulk-mode', bulkMode);
const btn = document.getElementById('bulk-toggle');
if (btn) btn.textContent = bulkMode ? '退出选择' : '选择画像卡';
}

function toggleBulkMode() {
bulkMode = !bulkMode;
if (!bulkMode) selectedCards.clear();
renderUL(currentProfiles, true);
updateBulkCount();
}

function toggleCardSelected(event, key) {
event.stopPropagation();
if (selectedCards.has(key)) selectedCards.delete(key);
else selectedCards.add(key);
updateBulkCount();
}

function handleCardClick(event, key) {
if (bulkMode) {
if (selectedCards.has(key)) selectedCards.delete(key);
else selectedCards.add(key);
renderUL(currentProfiles, true);
updateBulkCount();
return;
}
openD(key);
}

function selectAllCards() {
bulkMode = true;
document.querySelectorAll('#ulist .card').forEach(card => {
const onclick = card.getAttribute('onclick') || '';
const m = onclick.match(/handleCardClick\(event,\s*'((?:\\'|[^'])*)'\)/);
if (m) selectedCards.add(m[1].replace(/\\'/g, "'"));
});
renderUL(currentProfiles, true);
updateBulkCount();
}

async function batchDeleteCards() {
if (!selectedCards.size) { snk('请先选择画像卡'); return; }
const keys = Array.from(selectedCards);
const ok = await confirmAction({
title: '批量删除画像卡',
message: `确定删除选中的 ${keys.length} 个画像？此操作不可撤销。`,
okText: '删除'
});
if (!ok) return;
try {
const d = await apiJson(apiUrl('/api/batch-delete'), {
method: 'POST',
headers: { 'Content-Type': 'application/json' },
body: JSON.stringify({ keys })
});
if (d.success) {
selectedCards.clear();
bulkMode = false;
localStorage.removeItem(LOCAL_CACHE_KEY);
statsCache = null;
debugInfo = null;
await refreshData();
snk(d.message || '批量删除完成');
} else snk(d.error || '批量删除失败');
} catch (e) { snk(e.message || '批量删除请求失败'); }
}

async function batchClean() {
const inp = document.getElementById('batch-clean-keyword');
const keyword = (inp && inp.value ? inp.value : '').trim();
if (!keyword) { snk('请输入要清理的关键词'); return; }
const ok = await confirmAction({
title: '批量清理词条',
message: `确定批量清理包含「${keyword}」的词条？此操作不可撤销。`,
okText: '清理'
});
if (!ok) return;
try {
const d = await apiJson(apiUrl('/api/batch-clean'), {
method: 'POST',
headers: { 'Content-Type': 'application/json' },
body: JSON.stringify({ keyword })
});
if (d.success) {
localStorage.removeItem(LOCAL_CACHE_KEY);
statsCache = null;
debugInfo = null;
await refreshData();
snk(d.message || '批量清理完成');
} else snk(d.error || '批量清理失败');
} catch (e) { snk(e.message || '批量清理请求失败'); }
}

async function delUser(k) {
  const ok = await confirmAction({
    title: '删除用户',
    message: `确定删除用户「${k}」的所有数据？此操作不可撤销。`,
    okText: '删除'
  });
  if (!ok) return;
  try {
    const d = await apiJson(apiUrl(`/api/profile/${enc(k)}`), { method: 'DELETE' });
    if (d.success) { snk('已删除'); closeD(); statsCache = null; await refreshData(); }
    else snk(d.error || '失败');
  } catch (e) { snk(e.message || '请求失败'); }
}
// ===== 工具函数 =====
let activeConfirmClose = null;

function confirmAction({ title = '确认操作', message = '', okText = '确认' } = {}) {
  if (activeConfirmClose) activeConfirmClose();
  return new Promise(resolve => {
    const scrim = document.getElementById('confirm-scrim');
    const titleEl = document.getElementById('confirm-title');
    const msgEl = document.getElementById('confirm-msg');
    const okBtn = document.getElementById('confirm-ok');
    const cancelBtn = document.getElementById('confirm-cancel');
    titleEl.textContent = title;
    msgEl.textContent = message;
    okBtn.textContent = okText;

    const cleanup = (result) => {
      scrim.classList.remove('active');
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      scrim.onclick = null;
      activeConfirmClose = null;
      resolve(result);
    };

    activeConfirmClose = () => cleanup(false);
    okBtn.onclick = () => cleanup(true);
    cancelBtn.onclick = () => cleanup(false);
    scrim.onclick = (e) => {
      if (e.target === scrim) cleanup(false);
    };

    scrim.classList.add('active');
    okBtn.focus();
  });
}

async function apiJson(url, options) {
  options = options || {};
  const started = Date.now();
  uiLog('debug', 'fetch开始', { url, method: options.method || 'GET' });
  const timeout = new Promise((_, reject) => {
    setTimeout(() => reject(new Error('请求超时')), 10000);
  });
  const request = fetch(url, options).then(async res => {
    let data = null;
    try { data = await res.json(); } catch (_) {}
    uiLog('debug', 'fetch返回', { url, status: res.status, ok: res.ok, costMs: Date.now() - started });
    if (res.status === 401) {
      window.location.href = '/login.html';
      throw new Error('未登录');
    }
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data || {};
  });
  return Promise.race([request, timeout]);
}
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function safeId(s) {
  return Array.from(String(s)).map(ch => ch.codePointAt(0).toString(16).padStart(4, '0')).join('_');
}
function jsArg(s) {
  let out = "'";
  for (const ch of String(s)) {
    if (ch === "\\") out += "\\\\";
    else if (ch === "'") out += "\\'";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\n") out += "\\n";
    else if (ch === "&") out += "\\x26";
    else if (ch === "<") out += "\\x3C";
    else if (ch === ">") out += "\\x3E";
    else if (ch === '"') out += "\\x22";
    else out += ch;
  }
  return out + "'";
}
function escJs(s) {
  return String(s).split('\\').join('\\\\').split("'").join("\\'");
}
function enc(s) { return encodeURIComponent(s); }
function snk(msg) { const el = document.getElementById('snk'); el.textContent = msg; el.className = 'snackbar show'; setTimeout(() => el.className = 'snackbar', 2500); }
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (activeConfirmClose) activeConfirmClose();
  else closeD();
});
