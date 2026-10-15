// Call/customer text always uses textContent or input.value, never HTML.
const el = id => document.getElementById(id);
const labels = {new:'Новая',callback:'Перезвонить',done:'Завершено',declined:'Отказ'};
const interest = {interested:'Есть интерес',not_interested:'Не заинтересован',unclear:'Интерес не выяснен'};
const analysis = {active:'Идёт разговор',pending:'Готовится итог…',error:'Итог не готов',empty:'Нет расшифровки',ready:'Итог готов'};
let offset = 0, selected = null, dirty = false, revision = 0, requestVersion = 0, timer, busy = false, editing = false, disposed = false, signature = '';
const fieldVersions = new Map();
const editable = [['crmName','name'],['crmPhone','phone'],['crmTelegram','telegram'],['crmNotes','notes'],['crmFollowUp','follow_up'],['crmStatus','status']];

async function api(path, body) {
  const response = await fetch('/crm/calls' + path, {
    cache:'no-store', signal:AbortSignal.timeout(7000),
    ...(body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(result.error || 'Не удалось обновить CRM. Проверьте локальную программу.');
  }
  return response.json();
}
function text(tag, value, cls) {
  const node = document.createElement(tag);
  node.textContent = value;
  if (cls) node.className = cls;
  return node;
}
function date(value) { return value ? new Date(value).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : ''; }
function duration(value) { const seconds = Math.floor(value || 0); return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`; }
function render(result) {
  const key = JSON.stringify(result);
  if (key === signature) return;
  signature = key;
  el('crmRows').replaceChildren();
  for (const row of result.items) {
    const tr = document.createElement('tr');
    const when = text('td', date(row.started_at));
    when.append(text('small', `${row.mode === 'agents' ? 'Agents' : 'Свой диалог'} · ${duration(row.duration_seconds)}`));
    const contact = text('td', row.contact_name || row.name || row.call_display_name || 'Имя не указано');
    contact.append(text('small', row.call_number || row.phone || row.telegram || 'Контакт не указан'));
    const outcome = text('td', row.summary || analysis[row.analysis_status] || 'Ожидание');
    if (row.analysis_status === 'ready') outcome.prepend(text('strong', interest[row.interest] || interest.unclear));
    if (row.state === 'interrupted') outcome.append(text('small', 'Программа была прервана'));
    const status = text('td', labels[row.status] || labels.new);
    const open = text('button', 'Открыть', 'secondary');
    open.onclick = () => void openCard(row.id).catch(showError);
    status.append(open);
    tr.append(when, contact, outcome, status);
    el('crmRows').append(tr);
  }
  el('crmEmpty').hidden = result.items.length > 0;
  el('crmTable').hidden = result.items.length === 0;
  el('crmPrevious').disabled = offset === 0;
  el('crmNext').disabled = offset + 50 >= result.total;
  el('crmCount').textContent = result.total ? `${offset + 1}–${Math.min(offset + 50,result.total)} из ${result.total}` : 'Разговоров пока нет';
  el('crmError').textContent = result.warning || '';
}
function showError(error) { el('crmError').textContent = error.message; }
async function refresh() {
  if (busy || disposed) return;
  busy = true;
  try { render(await api(`?limit=50&offset=${offset}`)); }
  catch(error) { showError(error); }
  finally { busy = false; }
}
async function openCard(id) {
  // Keep unsaved work; make the action explicit instead of silently dropping it.
  if (dirty && selected !== id) {
    el('crmDetailStatus').textContent = 'Сначала сохраните правки или нажмите «Закрыть без сохранения».';
    el('crmDetail').scrollIntoView({behavior:'smooth',block:'nearest'});
    return;
  }
  const version = ++requestVersion;
  const row = await api('/' + encodeURIComponent(id));
  if (version !== requestVersion || dirty || editing || disposed) return;
  selected = id;
  fieldVersions.clear();
  el('crmLinkTarget').value = id;
  el('crmLinkTarget').dispatchEvent?.(new Event('change'));
  el('crmLinkedContact').textContent = row.contact_name ? `Контакт: ${row.contact_name}` : row.call_display_name ? `Имя из телефона: ${row.call_display_name}` : 'Контакт адресной книги не выбран.';
  el('crmCallNumber').textContent = row.call_number ? `Номер звонка: ${row.call_number}` : 'Windows не передала номер этого звонка.';
  el('crmDetail').hidden = false;
  el('crmTitle').textContent = 'Разговор ' + date(row.started_at);
  el('crmSummary').textContent = row.summary || analysis[row.analysis_status] || '';
  el('crmNextStep').textContent = row.next_step ? 'Дальше: ' + row.next_step : '';
  for (const [id,field] of editable) el(id).value = row[field] || (field === 'status' ? 'new' : '');
  el('crmTranscript').textContent = row.transcript.map(turn => `${turn.role === 'user' ? 'Абонент' : 'Агент'}: ${turn.text}`).join('\n\n') || 'Реплики не получены.';
  el('crmDetailStatus').textContent = row.analysis_error || (row.transcript_truncated || row.transcript_incomplete ? 'Расшифровка неполная: часть реплик не сохранена.' : analysis[row.analysis_status]);
  el('crmAnalyze').disabled = row.state === 'active' || !row.transcript.length || row.analyzing;
  el('crmAnalyze').textContent = row.analyzing ? 'Итог готовится…' : 'Сформировать итог заново';
}
el('crmSave').onclick = async () => {
  if (!selected || editing || !dirty) return;
  editing = true; el('crmSave').disabled = true;
  const savedVersions = new Map(fieldVersions);
  const changes = {};
  for (const [id,field] of editable) if (savedVersions.has(field)) changes[field] = el(id).value;
  try {
    await api('/' + encodeURIComponent(selected), changes);
    for (const [field, version] of savedVersions) if (fieldVersions.get(field) === version) fieldVersions.delete(field);
    dirty = fieldVersions.size > 0;
    el('crmDetailStatus').textContent = dirty ? 'Предыдущие правки сохранены. Сохраните новые изменения.' : 'Карточка сохранена.';
    signature = ''; await refresh();
  } catch(error) { showError(error); }
  finally { editing = false; el('crmSave').disabled = false; }
};
el('crmAnalyze').onclick = async () => {
  if (!selected || dirty || editing) {
    el('crmDetailStatus').textContent = 'Сначала сохраните правки карточки.'; return;
  }
  el('crmAnalyze').disabled = true;
  try {
    await api('/' + encodeURIComponent(selected) + '/analyze', {});
    el('crmDetailStatus').textContent = 'Итог готовится. Через несколько секунд нажмите «Обновить».';
  } catch(error) { showError(error); el('crmAnalyze').disabled = false; }
};
el('crmClose').onclick = () => {
  if (editing) return;
  requestVersion++; selected = null; dirty = false; fieldVersions.clear(); el('crmDetail').hidden = true;
  el('crmLinkTarget').value = ''; el('crmLinkTarget').dispatchEvent?.(new Event('change'));
};
el('crmChooseContact').onclick = () => { el('contacts').open = true; el('contacts').scrollIntoView({behavior:'smooth',block:'start'}); };
for (const [id, field] of editable) el(id).oninput = () => { dirty = true; fieldVersions.set(field, ++revision); };
el('crmRefresh').onclick = async () => {
  signature = ''; await refresh();
  if (selected && !dirty) await openCard(selected).catch(showError);
};
el('crmPrevious').onclick = () => { if (busy) return; offset = Math.max(0,offset-50); signature=''; void refresh(); };
el('crmNext').onclick = () => { if (busy) return; offset += 50; signature=''; void refresh(); };
el('crm').addEventListener('toggle', () => { if (el('crm').open) void refresh(); });
async function poll() {
  if (disposed) return;
  if (el('crm').open && !document.hidden) await refresh();
  if (!disposed) timer = setTimeout(poll, 4000);
}
window.addEventListener('pagehide', () => { disposed = true; clearTimeout(timer); });
void poll();
