const form = document.querySelector('#lead-form');
const statusLine = document.querySelector('#form-status');
const button = form.querySelector('button');
let token = '';
async function refreshToken() {
  token = '';
  try {
    const response = await fetch('/api/form-token');
    if (!response.ok) throw new Error();
    token = (await response.json()).token;
  } catch { statusLine.textContent = 'Не удалось загрузить форму. Обновите страницу.'; }
}
refreshToken();
form.phone.addEventListener('input', event => {
  let digits = form.phone.value.replace(/\D/g, '');
  if (/^[78]/.test(digits)) digits = digits.slice(1);
  digits = digits.slice(0, 10);
  let value = '+7';
  if (digits.length) value += ' (' + digits.slice(0, 3);
  if (digits.length >= 3) value += ') ';
  if (digits.length > 3) value += digits.slice(3, 6);
  if (digits.length > 6) value += '-' + digits.slice(6, 8);
  if (digits.length > 8) value += '-' + digits.slice(8, 10);
  form.phone.value = event.inputType === 'deleteContentBackward' ? value.replace(/\D+$/, '') : value;
});
document.querySelectorAll('[data-door]').forEach(link => link.addEventListener('click', () => { form.door.value = link.dataset.door; }));
form.addEventListener('submit', async event => {
  event.preventDefault();
  button.disabled = true;
  statusLine.textContent = 'Отправляем заявку…';
  const data = Object.fromEntries(new FormData(form));
  data.consent = form.consent.checked;
  data.token = token;
  try {
    const response = await fetch('/api/lead', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Не удалось отправить заявку');
    statusLine.textContent = 'Спасибо! Заявка принята. Специалист скоро вам позвонит.';
    form.reset();
    await refreshToken();
  } catch (error) {
    statusLine.textContent = error.message === 'Failed to fetch' ? 'Нет связи. Проверьте интернет и попробуйте ещё раз.' : error.message;
    await refreshToken();
  } finally { button.disabled = false; }
});
