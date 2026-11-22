const form = document.querySelector('#lead-form');
const statusLine = document.querySelector('#form-status');
const button = form.querySelector('button');
const token = form.elements.token;
const { name, phone, door, comment, consent } = form.elements;
const fields = [name, phone, door, comment, consent];
const required = new Map([
  [name, 'Укажите ваше имя.'],
  [phone, 'Укажите российский номер телефона.'],
  [consent, 'Подтвердите согласие на обработку персональных данных.'],
]);
const descriptions = new Map(fields.map(field => [field, (field.getAttribute('aria-describedby') || '')
  .split(' ').filter(id => id && id !== `${field.id}-error`)]));
let submitting = false;

function showFieldError(field, message) {
  const error = document.getElementById(`${field.id}-error`);
  const ids = descriptions.get(field);
  error.textContent = message;
  error.hidden = !message;
  if (message) {
    field.setAttribute('aria-invalid', 'true');
    field.setAttribute('aria-describedby', [error.id, ...ids].join(' '));
  } else {
    field.removeAttribute('aria-invalid');
    if (ids.length) field.setAttribute('aria-describedby', ids.join(' '));
    else field.removeAttribute('aria-describedby');
  }
}

function focusError(field, message) {
  showFieldError(field, message);
  if (document.activeElement === field) statusLine.textContent = message;
  else field.focus();
}

async function sendLead(data) {
  const response = await fetch(form.action, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  });
  const result = await response.json().catch(() => ({}));
  return {status: response.status, result};
}

fields.forEach(field => field.addEventListener('input', () => {
  if (!field.hasAttribute('aria-invalid')) return;
  showFieldError(field, '');
  statusLine.textContent = '';
}));
document.querySelectorAll('[data-door]').forEach(link => link.addEventListener('click', () => {
  door.value = link.dataset.door;
  showFieldError(door, '');
}));
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submitting) return;
  statusLine.textContent = '';
  fields.forEach(field => showFieldError(field, ''));
  const missing = [...required.keys()].find(field => field === consent ? !consent.checked : !field.value.trim());
  if (missing) {
    focusError(missing, required.get(missing));
    return;
  }
  submitting = true;
  button.setAttribute('aria-disabled', 'true');
  statusLine.textContent = 'Отправляем заявку…';
  const data = Object.fromEntries(new FormData(form));
  data.consent = consent.checked;
  try {
    let {status, result} = await sendLead(data);
    if (result.retry_token) {
      token.value = data.token = result.retry_token;
      await new Promise(resolve => setTimeout(resolve, 3100));
      ({status, result} = await sendLead(data));
    }
    if (result.ok === true || status === 409) {
      statusLine.textContent = result.detail || 'Спасибо! Заявка принята. Специалист скоро вам позвонит.';
      form.reset();
      return;
    }
    const field = result.field && form.elements.namedItem(result.field);
    if (fields.includes(field) && result.detail) {
      statusLine.textContent = '';
      focusError(field, result.detail);
    } else statusLine.textContent = result.detail || 'Не удалось подтвердить отправку. Попробуйте ещё раз.';
  } catch {
    statusLine.textContent = 'Нет связи с сайтом. Проверьте интернет и попробуйте ещё раз.';
  } finally {
    submitting = false;
    button.removeAttribute('aria-disabled');
  }
});
