// The address book stays local. Imported names and numbers are plain text.
const contactEl = id => document.getElementById(id);
const MAX_VCARD_BYTES = 16 * 1024 * 1024;
const CONTACT_PAGE_SIZE = 50;
let contactOffset = 0, contactTotal = 0, contactQuery = '', contactBusy = false, contactDisposed = false;
let contactLinkButtons = [];
const selectedCall = () => contactEl('crmLinkTarget').value.trim();

function contactStatus(message) {
  if (!contactDisposed) contactEl('contactsStatus').textContent = message;
}
function contactControls() {
  for (const id of ['contactsSearchButton', 'contactsImport', 'contactsFile', 'contactsSource']) contactEl(id).disabled = contactBusy;
  contactEl('contactsPrevious').disabled = contactBusy || contactOffset === 0;
  contactEl('contactsNext').disabled = contactBusy || contactOffset + CONTACT_PAGE_SIZE >= contactTotal;
  for (const button of contactLinkButtons) button.disabled = contactBusy || !selectedCall();
}
async function contactsApi(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(30000), ...options});
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(typeof result.error === 'string' ? result.error.slice(0, 500) : 'Не удалось обновить контакты. Проверьте локальную программу.');
  }
  return response.json();
}
function contactText(tag, value) {
  const element = document.createElement(tag);
  element.textContent = value;
  return element;
}
function renderContacts(result) {
  if (!Array.isArray(result.items) || !Number.isInteger(result.total) || result.total < 0) throw new Error('Программа вернула некорректный список контактов.');
  contactTotal = result.total;
  contactLinkButtons = [];
  contactEl('contactsRows').replaceChildren();
  for (const row of result.items) {
    const tr = document.createElement('tr');
    const numbers = Array.isArray(row.phones) ? row.phones.filter(phone => typeof phone === 'string').join(', ') : '';
    const action = document.createElement('td');
    const button = contactText('button', 'Привязать');
    button.className = 'secondary';
    button.title = 'Привязать к открытой карточке разговора';
    button.onclick = () => linkContact(row.id);
    contactLinkButtons.push(button);
    action.append(button);
    tr.append(contactText('td', row.name || 'Без имени'), contactText('td', numbers || 'Телефон не указан'), action);
    contactEl('contactsRows').append(tr);
  }
  contactEl('contactsCount').textContent = contactTotal
    ? `${contactOffset + 1}–${Math.min(contactOffset + CONTACT_PAGE_SIZE, contactTotal)} из ${contactTotal}`
    : 'Контакты не найдены';
  contactControls();
}
async function loadContactPage() {
  if (!contactEl('contacts').open || contactDisposed) return;
  const query = new URLSearchParams({q: contactQuery, limit: String(CONTACT_PAGE_SIZE), offset: String(contactOffset)});
  const result = await contactsApi('/crm/contacts?' + query);
  if (!contactDisposed) renderContacts(result);
}
async function refreshContacts() {
  if (contactBusy || contactDisposed || !contactEl('contacts').open) return;
  contactBusy = true; contactControls();
  try {
    await loadContactPage();
    contactStatus(selectedCall() ? 'Выберите контакт для открытой карточки разговора.' : 'Для привязки сначала откройте карточку разговора в CRM.');
  } catch (error) { contactStatus(error.message || 'Не удалось загрузить контакты.'); }
  finally { contactBusy = false; contactControls(); }
}
async function linkContact(contactId) {
  if (contactBusy || contactDisposed) return;
  const callId = selectedCall();
  if (!callId) { contactControls(); contactStatus('Сначала откройте карточку разговора в CRM.'); return; }
  contactBusy = true; contactControls();
  try {
    await contactsApi('/crm/calls/' + encodeURIComponent(callId) + '/contact', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({contact_id: contactId}),
    });
    if (!contactDisposed) {
      await contactEl('crmRefresh').onclick?.();
      contactStatus(selectedCall() === callId ? 'Контакт привязан к разговору.' : 'Контакт привязан. Сейчас открыта другая карточка.');
    }
  } catch (error) { contactStatus(error.message || 'Не удалось привязать контакт.'); }
  finally { contactBusy = false; contactControls(); }
}
async function importContacts() {
  if (contactBusy || contactDisposed) return;
  const file = contactEl('contactsFile').files?.[0];
  if (!file) { contactStatus('Выберите файл контактов .vcf.'); return; }
  if (!file.size || file.size > MAX_VCARD_BYTES) { contactStatus('Нужен непустой файл vCard размером не больше 16 МБ.'); return; }
  const source = contactEl('contactsSource').value;
  if (!['google', 'iphone', 'vcard'].includes(source)) { contactStatus('Выберите источник контактов.'); return; }
  contactBusy = true; contactControls(); contactStatus('Импортирую контакты…');
  try {
    // Preserve original bytes; the server handles vCard charset declarations.
    const bytes = await file.arrayBuffer();
    if (contactDisposed) return;
    const result = await contactsApi('/crm/contacts/import?source=' + encodeURIComponent(source), {
      method: 'POST', headers: {'Content-Type': 'text/vcard'}, body: bytes,
    });
    contactEl('contactsFile').value = '';
    const count = field => Number.isInteger(result[field]) && result[field] >= 0 ? result[field] : 0;
    const summary = `Импорт завершён: добавлено ${count('added')}, обновлено ${count('updated')}, пропущено ${count('skipped')}.`;
    contactOffset = 0;
    try { await loadContactPage(); contactStatus(summary); }
    catch { contactStatus(summary + ' Не удалось обновить список; повторите поиск.'); }
  } catch (error) { contactStatus(error.message || 'Не удалось импортировать контакты.'); }
  finally { contactBusy = false; contactControls(); }
}
function searchContacts() {
  if (contactBusy) return;
  contactOffset = 0; contactQuery = contactEl('contactsSearch').value.trim();
  return refreshContacts();
}
contactEl('contactsSearchButton').onclick = searchContacts;
contactEl('contactsSearch').addEventListener('keydown', event => {
  if (event.key === 'Enter') { event.preventDefault(); void searchContacts(); }
});
contactEl('contactsImport').onclick = importContacts;
contactEl('contactsPrevious').onclick = () => {
  if (contactBusy || contactOffset === 0) return;
  contactOffset = Math.max(0, contactOffset - CONTACT_PAGE_SIZE); return refreshContacts();
};
contactEl('contactsNext').onclick = () => {
  if (contactBusy || contactOffset + CONTACT_PAGE_SIZE >= contactTotal) return;
  contactOffset += CONTACT_PAGE_SIZE; return refreshContacts();
};
contactEl('contacts').addEventListener('toggle', () => { if (contactEl('contacts').open) void refreshContacts(); });
contactEl('crmLinkTarget').addEventListener('change', contactControls);
let contactSelectionObserver;
if (typeof MutationObserver !== 'undefined') {
  contactSelectionObserver = new MutationObserver(contactControls);
  contactSelectionObserver.observe(contactEl('crmLinkTarget'), {attributes: true, attributeFilter: ['value']});
}
window.addEventListener('pagehide', () => { contactDisposed = true; contactSelectionObserver?.disconnect(); });
contactControls();
