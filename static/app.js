const form = document.querySelector('#lead-form');
const statusLine = document.querySelector('#form-status');
const button = form.querySelector('button');
const token = form.elements.token;

async function sendLead(data) {
  const response = await fetch(form.action, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  });
  const result = await response.json().catch(() => ({}));
  return {status: response.status, result};
}

document.querySelectorAll('[data-door]').forEach(link => link.addEventListener('click', () => { form.door.value = link.dataset.door; }));
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  button.disabled = true;
  form.querySelectorAll('[aria-invalid]').forEach(field => {
    field.removeAttribute('aria-invalid');
    field.removeAttribute('aria-describedby');
  });
  statusLine.textContent = 'Отправляем заявку…';
  const data = Object.fromEntries(new FormData(form));
  data.consent = form.consent.checked;
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
    statusLine.textContent = result.detail || 'Не удалось подтвердить отправку. Попробуйте ещё раз.';
    if (result.field) {
      const field = form.elements.namedItem(result.field);
      field.setAttribute('aria-invalid', 'true');
      field.setAttribute('aria-describedby', 'form-status');
      field.focus();
    }
  } catch {
    statusLine.textContent = 'Нет связи с сайтом. Проверьте интернет и попробуйте ещё раз.';
  } finally { button.disabled = false; }
});
