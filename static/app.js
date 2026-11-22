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

const mobileCta = document.querySelector('.mobile-cta');
const ctaObstacles = new Set();
const ctaObserver = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (entry.isIntersecting) ctaObstacles.add(entry.target);
    else ctaObstacles.delete(entry.target);
  });
  const hidden = ctaObstacles.size > 0;
  mobileCta.classList.toggle('is-hidden', hidden);
  mobileCta.inert = hidden;
  mobileCta.setAttribute('aria-hidden', String(hidden));
}, {rootMargin: '0px 0px 80px 0px'});
ctaObserver.observe(document.querySelector('#lead'));
ctaObserver.observe(document.querySelector('footer'));

form.noValidate = true;
function validateField(field) {
  let message = '';
  if (field === form.elements.namedItem('name') && field.value.trim().length < 2) message = 'Введите имя (не менее двух символов)';
  if (field === form.phone) {
    const value = field.value.trim().replace(/^(?:тел\.?|телефон|tel|phone)\s*:\s*/i, '');
    const digits = value.replace(/\D/g, '');
    const complete = value.startsWith('+') ? /^7\d{10}$/.test(digits) : /^(?:\d{10}|[78]\d{10})$/.test(digits);
    if (!/^\+?[0-9().\s-]+$/.test(value) || !complete) message = 'Введите номер полностью: +7 (XXX) XXX-XX-XX';
  }
  if (field === form.consent && !field.checked) message = 'Подтвердите согласие на обработку персональных данных';
  field.setCustomValidity(message);
}
[form.elements.namedItem('name'), form.phone, form.consent].forEach(field => {
  field.addEventListener('input', () => field.setCustomValidity(''));
  field.addEventListener('change', () => field.setCustomValidity(''));
});

document.querySelectorAll('[data-door]').forEach(link => link.addEventListener('click', () => { form.door.value = link.dataset.door; }));
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  [form.elements.namedItem('name'), form.phone, form.consent].forEach(validateField);
  if (!form.reportValidity()) return;
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
